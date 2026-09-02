"""Durably claim live replies before dispatching the agent."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional


logger = logging.getLogger(__name__)
LEDGER_PATH = Path("/opt/opencomputer-v2-data/live_reply_guard.db")
LEDGER_CONNECT_TIMEOUT_SECONDS = 5
LEDGER_WRITE_PATIENCE_SECONDS = 30


def _ensure_schema(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS claimed_replies (
            channel TEXT NOT NULL,
            account_id TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            sender_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            occurred_at TEXT,
            fingerprint TEXT NOT NULL,
            claimed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            PRIMARY KEY (channel, account_id, message_id)
        )
        """
    )
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_claimed_fingerprint ON claimed_replies(channel, account_id, fingerprint)"
    )


def _fingerprint(chat_id: str, sender_id: str, text: str, occurred_at: Optional[str]) -> str:
    payload = "\x1f".join(
        [chat_id.strip(), sender_id.strip(), (text or "").strip(), occurred_at or ""]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _reserve_sync(
    *,
    channel: str,
    account_id: str,
    chat_id: str,
    sender_id: str,
    message_id: str,
    text: str,
    occurred_at: Optional[str],
) -> tuple[bool, str, Optional[str]]:
    normalized_message_id = (message_id or "").strip()
    if not normalized_message_id:
        return False, "missing_message_id", None

    fingerprint = _fingerprint(chat_id, sender_id, text, occurred_at)
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + LEDGER_WRITE_PATIENCE_SECONDS
    attempt = 0
    while True:
        db = sqlite3.connect(
            str(LEDGER_PATH), timeout=LEDGER_CONNECT_TIMEOUT_SECONDS
        )
        db.execute(
            f"PRAGMA busy_timeout={LEDGER_CONNECT_TIMEOUT_SECONDS * 1000}"
        )
        try:
            # Schema creation is a one-time DDL write. Commit it separately so
            # competing channel guards never hold that lock while inserting a
            # claim, and so a transient DDL collision can be retried safely.
            _ensure_schema(db)
            db.commit()
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    """
                    INSERT INTO claimed_replies (
                        channel, account_id, chat_id, sender_id, message_id, occurred_at, fingerprint
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        channel,
                        account_id,
                        chat_id,
                        sender_id,
                        normalized_message_id,
                        occurred_at,
                        fingerprint,
                    ),
                )
                db.commit()
                return True, "reserved", normalized_message_id
            except sqlite3.IntegrityError:
                db.rollback()
                existing = db.execute(
                    "SELECT message_id FROM claimed_replies WHERE channel=? AND account_id=? AND fingerprint=? LIMIT 1",
                    (channel, account_id, fingerprint),
                ).fetchone()
                if existing and str(existing[0]) == normalized_message_id:
                    return False, "duplicate_message", None
                return False, "already_claimed", None
        except sqlite3.OperationalError as exc:
            try:
                db.rollback()
            except sqlite3.Error:
                pass
            message = str(exc).lower()
            if "locked" not in message and "busy" not in message:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            # Bounded jitter avoids all three platform guards retrying in
            # lock-step while keeping the normal path effectively immediate.
            time.sleep(min(0.05 * (2 ** min(attempt, 5)), 0.75, remaining))
            attempt += 1
            continue
        finally:
            db.close()


async def reserve_live_reply(
    *,
    channel: str,
    account_id: str,
    chat_id: str,
    sender_id: str,
    message_id: str,
    text: str,
    occurred_at: Optional[str],
) -> tuple[bool, str, Optional[str]]:
    try:
        return await asyncio.to_thread(
            _reserve_sync,
            channel=channel,
            account_id=account_id,
            chat_id=chat_id,
            sender_id=sender_id,
            message_id=message_id,
            text=text,
            occurred_at=occurred_at,
        )
    except Exception as exc:
        logger.exception("[live-reply-guard] failed closed: %s", exc)
        return False, "guard_error", None
