from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

DB_PATH = "/var/lib/lmi-dashboard/unipile_webhooks.db"
UTC = timezone.utc
_PHONE_RE = re.compile(r"[^0-9]")


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _digits(value: str) -> str:
    return _PHONE_RE.sub("", str(value or ""))


def normalize_indian_phone(value: str) -> str:
    digits = _digits(value)
    if len(digits) == 10:
        digits = "91" + digits
    if len(digits) < 11:
        return ""
    return "+" + digits


def mask_phone(value: str) -> str:
    digits = _digits(value)
    if len(digits) < 4:
        return "+****"
    return "+" + digits[:2] + "*" * max(0, len(digits) - 6) + digits[-4:]


def has_opt_out(text: str) -> bool:
    clean = str(text or "").lower()
    needles = (
        "stop",
        "unsubscribe",
        "do not call",
        "don't call",
        "dont call",
        "not interested",
        "no call",
        "never call",
    )
    return any(needle in clean for needle in needles)


def has_call_consent(text: str) -> bool:
    clean = str(text or "").lower()
    patterns = (
        r"\bcall\s+me\b",
        r"\bcall\s+now\b",
        r"\bphone\s+me\b",
        r"\byou\s+can\s+call\s+me\b",
        r"\bcan\s+you\s+call\s+me\b",
        r"\bgive\s+me\s+a\s+call\b",
        r"\bi\s+want\s+a\s+call\b",
        r"\bi\s+would\s+like\s+a\s+call\b",
        r"\bpermission\s+to\s+call\b",
    )
    return any(re.search(pattern, clean) for pattern in patterns)


def _chat_messages_has_member_id() -> bool:
    con = sqlite3.connect(DB_PATH)
    try:
        rows = con.execute("PRAGMA table_info(chat_messages)").fetchall()
        return any(str(row[1]) == "member_id" for row in rows)
    finally:
        con.close()


def extract_recent_inbound_messages(channel: str, account_id: str, chat_id: str, limit: int = 12) -> list[dict[str, Any]]:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        select_member_id = ", member_id" if _chat_messages_has_member_id() else ", '' as member_id"
        rows = con.execute(
            f"""
            SELECT direction, text, msg_ts{select_member_id}
            FROM chat_messages
            WHERE lower(channel)=? AND account_id=? AND chat_id=?
            ORDER BY msg_ts DESC, id DESC
            LIMIT ?
            """,
            (channel.lower(), account_id, chat_id, max(1, min(limit, 50))),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        con.close()


def verify_consented_callback(channel: str, account_id: str, chat_id: str, phone_number: str, member_id: str = "") -> tuple[bool, str]:
    normalized_phone = normalize_indian_phone(phone_number)
    if not normalized_phone:
        return False, "invalid_phone"
    rows = extract_recent_inbound_messages(channel, account_id, chat_id)
    if not rows:
        return False, "missing_chat_history"
    inbound_rows = [row for row in rows if str(row.get("direction") or "").lower() in {"in", "inbound"}]
    if not inbound_rows:
        return False, "missing_inbound_history"
    if member_id:
        scoped = [row for row in inbound_rows if str(row.get("member_id") or "") == member_id]
        if scoped:
            inbound_rows = scoped
    for row in inbound_rows:
        if has_opt_out(str(row.get("text") or "")):
            return False, "opt_out_present"
    latest_consent = None
    for row in inbound_rows:
        if has_call_consent(str(row.get("text") or "")):
            latest_consent = row
            break
    if latest_consent is None:
        return False, "missing_call_consent"
    joined = "\n".join(str(row.get("text") or "") for row in inbound_rows)
    if normalize_indian_phone(joined) != normalized_phone and _digits(phone_number) not in _digits(joined):
        return False, "phone_not_found_in_thread"
    # Stash the row that actually carried the consent so the caller can record
    # it as evidence. Deliberately NOT returned in the tuple: three adapters
    # unpack this as (bool, str) and are not being changed.
    global _LAST_CONSENT_ROW
    _LAST_CONSENT_ROW = dict(latest_consent or {})
    return True, "ok"


_LAST_CONSENT_ROW: dict[str, Any] = {}


def record_callback_evidence(*, channel: str, account_id: str, chat_id: str,
                             member_id: str, phone_e164: str, attempt_id: str,
                             outcome: str) -> None:
    """Durably record WHY this callback was allowed: the customer's own words.

    Best-effort by design. An audit write must never block or fail a callback
    the customer explicitly asked for, so every failure is swallowed.
    """
    row = dict(_LAST_CONSENT_ROW or {})
    try:
        con = sqlite3.connect(DB_PATH, timeout=30)
        con.execute(
            """CREATE TABLE IF NOT EXISTS adapter_callback_evidence(
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   created_at TEXT NOT NULL,
                   channel TEXT NOT NULL,
                   account_id TEXT NOT NULL,
                   chat_id TEXT NOT NULL,
                   member_id TEXT NOT NULL,
                   phone_e164 TEXT NOT NULL,
                   consent_text TEXT NOT NULL,
                   consent_msg_ts TEXT NOT NULL,
                   opt_out_checked INTEGER NOT NULL,
                   attempt_id TEXT NOT NULL,
                   outcome TEXT NOT NULL,
                   UNIQUE(channel, account_id, chat_id, phone_e164, consent_msg_ts)
               )"""
        )
        con.execute(
            """INSERT OR IGNORE INTO adapter_callback_evidence(
                   created_at, channel, account_id, chat_id, member_id,
                   phone_e164, consent_text, consent_msg_ts, opt_out_checked,
                   attempt_id, outcome)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                _iso_now(), channel, account_id, chat_id, member_id, phone_e164,
                str(row.get("text") or "")[:2000],
                str(row.get("msg_ts") or ""),
                1,                       # verify_consented_callback always scans for opt-out first
                attempt_id, outcome,
            ),
        )
        con.commit()
        con.close()
    except Exception:
        pass


def log_callback_attempt(*, channel: str, chat_id: str, member_id: str, phone_number: str, attempt_id: str, status: str, detail: str = "") -> None:
    try:
        con = sqlite3.connect(DB_PATH)
        con.execute(
            "INSERT INTO escalations (created_at, type, detail, member_id, chat_id, platform, status) VALUES (?,?,?,?,?,?,?)",
            (
                _iso_now(),
                "direct_callback_attempt",
                json.dumps(
                    {
                        "attempt_id": attempt_id,
                        "phone": mask_phone(phone_number),
                        "channel": channel,
                        "detail": detail,
                    },
                    sort_keys=True,
                )[:1000],
                member_id,
                chat_id,
                channel,
                status,
            ),
        )
        con.commit()
        con.close()
    except Exception:
        pass


def build_sarvam_caller(
    *,
    platform_source: str,
    required_env: Callable[[], dict[str, str]],
    webhook_url: str = "",
    webhook_secret: str = "",
    outbounds_base_url: str = "https://apps.sarvam.ai/api/outbounds",
) -> Callable[[dict[str, Any]], str]:
    base_url = (outbounds_base_url or "https://apps.sarvam.ai/api/outbounds").rstrip("/")

    def _call(args: dict[str, Any] | None = None, **kwargs) -> str:
        # Tool runtimes may pass task_id/etc as kwargs; keep them ignored.
        if args is None:
            args = kwargs
        elif kwargs:
            merged = dict(args)
            for key, value in kwargs.items():
                if key not in merged and key not in {"task_id"}:
                    merged[key] = value
            args = merged
        phone_number = str(args.get("phone_number") or "").strip()
        recipient_name = str(args.get("recipient_name") or "").strip()
        context = str(args.get("context") or "").strip()
        channel = str(args.get("channel") or platform_source).strip().lower()
        account_id = str(args.get("account_id") or "").strip()
        chat_id = str(args.get("chat_id") or "").strip()
        member_id = str(args.get("member_id") or "").strip()
        normalized_phone = normalize_indian_phone(phone_number)
        if not normalized_phone:
            return json.dumps({"status": "error", "message": "Provide a valid phone_number"})
        if not account_id or not chat_id:
            return json.dumps({"status": "error", "message": "chat_id and account_id are required for direct_callback"})
        ok, reason = verify_consented_callback(channel, account_id, chat_id, normalized_phone, member_id)
        # Some threads are stored under a different channel label (IG/WA mixups).
        if not ok and reason in {"missing_chat_history", "missing_inbound_history", "missing_call_consent", "phone_not_found_in_thread"}:
            for alt_channel in ("instagram", "whatsapp", "linkedin"):
                if alt_channel == channel:
                    continue
                alt_ok, alt_reason = verify_consented_callback(alt_channel, account_id, chat_id, normalized_phone, member_id)
                if alt_ok:
                    ok, reason = alt_ok, alt_reason
                    channel = alt_channel
                    break
        if not ok:
            return json.dumps({"status": "blocked", "message": reason})
        required = required_env()
        missing = [name for name, value in required.items() if not value]
        if missing:
            return json.dumps({"status": "error", "message": "missing configuration", "missing": missing})
        try:
            app_version = int(required["SARVAM_APP_VERSION"])
        except ValueError:
            return json.dumps({"status": "error", "message": "SARVAM_APP_VERSION must be an integer"})
        app_config: dict[str, Any] = {
            "app_id": required["SARVAM_APP_ID"],
            "app_version": app_version,
            "app_type": "agent",
            "connection_config": {
                "connection_id": required["SARVAM_CONNECTION_ID"],
                "agent_phone_number": required["SARVAM_AGENT_PHONE_NUMBER"],
            },
        }
        agent_variables: dict[str, Any] = {}
        if agent_variables:
            app_config["agent_variables"] = agent_variables
        payload: dict[str, Any] = {
            "app_config": app_config,
            "user_config": {"user_phone_number": normalized_phone},
        }
        if webhook_url:
            webhook_target = webhook_url
            if webhook_secret:
                separator = "&" if "?" in webhook_target else "?"
                webhook_target = f"{webhook_target}{separator}secret={webhook_secret}"
            separator = "&" if "?" in webhook_url else "?"
            payload["webhook_config"] = {
                "url": webhook_target,
                "metadata": {
                    "source": platform_source,
                    "user_phone_number": normalized_phone,
                    "recipient_name": recipient_name,
                    "member_id": member_id,
                    "chat_id": chat_id,
                    "account_id": account_id,
                },
            }
        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(
                    f"{base_url}/v1/orgs/{required['SARVAM_ORG_ID']}/workspaces/{required['SARVAM_WORKSPACE_ID']}/outbounds",
                    headers={
                        "X-API-Key": required["SARVAM_API_KEY"],
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                if response.is_error:
                    return json.dumps({
                        "status": "error",
                        "message": f"HTTP {response.status_code}",
                        "response_text": response.text[:1000],
                    })
                result = response.json()
                attempt_id = str(result.get("attempt_id") or "unknown")
                log_callback_attempt(
                    channel=channel,
                    chat_id=chat_id,
                    member_id=member_id,
                    phone_number=normalized_phone,
                    attempt_id=attempt_id,
                    status="open",
                    detail="queued_direct_callback",
                )
                record_callback_evidence(
                    channel=channel, account_id=account_id, chat_id=chat_id,
                    member_id=member_id, phone_e164=normalized_phone,
                    attempt_id=attempt_id, outcome="queued",
                )
                return json.dumps({
                    "status": "queued",
                    "attempt_id": attempt_id,
                    "execution_id": attempt_id,
                    "phone_masked": mask_phone(normalized_phone),
                })
        except Exception as exc:
            log_callback_attempt(
                channel=channel,
                chat_id=chat_id,
                member_id=member_id,
                phone_number=normalized_phone,
                attempt_id="",
                status="open",
                detail=f"direct_callback_error:{str(exc)[:300]}",
            )
            record_callback_evidence(
                channel=channel, account_id=account_id, chat_id=chat_id,
                member_id=member_id, phone_e164=normalized_phone,
                attempt_id="", outcome=f"error:{str(exc)[:120]}",
            )
            return json.dumps({"status": "error", "message": str(exc)})

    return _call
