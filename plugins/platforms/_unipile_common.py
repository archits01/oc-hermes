"""
Shared Unipile utilities for Hermes platform adapters.

Extracted from linkedin/adapter.py. Provides:
  - UnipileClient: async HTTP client for Unipile REST API
  - UnipileWebhookMixin: webhook server setup, dedup, parsing
  - format_plain_text(): strip markdown for plain-text platforms
  - build_routing_context(): prefix messages with routing metadata

The underscore prefix (_unipile_common) prevents Hermes from loading
this as a platform plugin — it's a shared library, not an adapter.
"""
from __future__ import annotations

import asyncio
import collections
import datetime
import hashlib
import logging
import os
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    from aiohttp import web
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

DEDUP_CACHE_SIZE = 256
DEDUP_TTL_SECONDS = 300  # 5 minutes

# System messages Hermes sends through send() that must never reach real users
SYSTEM_MESSAGE_SIGNALS = [
    "\U0001f4ec No home channel",       # home channel setup
    "\u26a0\ufe0f **Dangerous",          # dangerous command warning
    "HEARTBEAT_OK",                      # health check
    "execute_code",                      # tool approval
    "hermes_tools",                      # tool approval
    "approval is one-shot",             # tool approval
    "spawn subprocesses",               # tool approval
    "I need \u2589",                     # agent reasoning fragment
]


# ── Unipile HTTP Client ─────────────────────────────────────────────────────



# Two customers received raw internal text on 2026-09-03: "API call failed after 3 retries:
# HTTP 404: Upstream error: 404" and "Operation interrupted: waiting for model response
# (21.1s elapsed)." Both came out of the agent loop and were delivered as ordinary messages.
# Every real send funnels through UnipileClient.send_message / start_chat, so this is the one
# place that covers every adapter and every channel. Kept narrow: each pattern is text no
# genuine customer message would contain.
_INTERNAL_TEXT_MARKERS = re.compile(
    r"API call failed"
    r"|Upstream error"
    r"|exhausted retries"
    r"|Operation interrupted"
    r"|waiting for model response"
    r"|Traceback \(most recent call last\)"
    r"|\bHTTP [45]\d{2}\b"
    r"|\bLLM(?:Error|Unavailable|Truncated)\b",
    re.I,
)


class InternalTextRefused(RuntimeError):
    """Raised instead of sending agent diagnostics to a human."""


def _refuse_internal_text(text: str, where: str) -> None:
    """Raise if the outbound body is diagnostics rather than a message to a customer."""
    hit = _INTERNAL_TEXT_MARKERS.search(str(text or ""))
    if hit:
        logger.error(
            "[unipile] REFUSED to send internal text via %s (matched %r): %s",
            where, hit.group(0), str(text or "")[:120],
        )
        raise InternalTextRefused(
            f"refusing to send internal text to a customer (matched {hit.group(0)!r})"
        )
# The same question was sent to one WhatsApp chat 16 times across two days, and four other
# chats saw 7-13 repeats. _dedup() below cannot catch that: it keys on inbound webhook ids,
# holds 256 entries for 300 seconds, and is lost on restart. This is a durable, per-chat
# check on what was actually delivered.
_REPEAT_DB = os.environ.get("LMI_REPEAT_GUARD_DB", "/var/lib/lmi-dashboard/unipile_webhooks.db")
_REPEAT_WINDOW_DAYS = 14


def _repeat_limit() -> int:
    raw = os.environ.get("LMI_SEND_REPEAT_LIMIT", "").strip()
    try:
        return max(1, int(raw)) if raw else 3
    except ValueError:
        return 3


class RepeatedTextRefused(RuntimeError):
    """Raised instead of sending a human the same sentence yet again."""


def _refuse_repeat(chat_id: str, text: str, where: str) -> None:
    """Raise if this exact body has already gone to this chat too many times.

    Fails open: any database problem lets the send through, because a mute agent is worse
    than a duplicate. Comparison is on the exact delivered text, so a reworded follow-up is
    never blocked - only literal repetition.
    """
    if os.environ.get("LMI_SEND_REPEAT_GUARD", "1").strip().lower() in {"0", "false", "no", "off"}:
        return
    body = str(text or "").strip()
    chat = str(chat_id or "").strip()
    if not body or not chat:
        return
    limit = _repeat_limit()
    try:
        cutoff = time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - _REPEAT_WINDOW_DAYS * 86400)
        )
        con = sqlite3.connect("file:%s?mode=ro" % _REPEAT_DB, uri=True, timeout=2)
        try:
            seen = con.execute(
                "SELECT COUNT(*) FROM chat_messages"
                " WHERE chat_id=? AND direction='out' AND text=? AND msg_ts > ?",
                (chat, body, cutoff),
            ).fetchone()[0]
        finally:
            con.close()
    except Exception:  # noqa: BLE001 - fail open
        return
    if seen >= limit:
        logger.error(
            "[unipile] REFUSED repeat via %s: this exact text has gone to %s %d times already",
            where, chat[:16], seen,
        )
        raise RepeatedTextRefused(
            "refusing to send the same message to this chat again (already sent %d times)" % seen
        )




_MEDIA_STATUS_TERMS = re.compile(
    r"\b(?:media|visuals?|photos?|pictures?|images?|videos?|clips?)\b", re.I
)
_MEDIA_FAILURE_CLAIM = re.compile(
    r"(?:can(?:not|'t)|could(?: not|n't)|unable|not able|won't|will not)"
    r".{0,160}\b(?:media|visuals?|photos?|pictures?|images?|videos?|clips?)\b"
    r"|\b(?:media|visuals?|photos?|pictures?|images?|videos?|clips?)\b.{0,160}"
    r"(?:blocked|pending|not (?:sent|delivered|gone through)|can(?:not|'t)|unable|not able)",
    re.I,
)
_MEDIA_ACTIVE_STATES = ("pending", "processing", "retry", "review", "sent")
_MEDIA_STATUS_WINDOW_SECONDS = 15 * 60


def _looks_like_media_failure_claim(text: str) -> bool:
    """Recognize only customer-facing claims that a media send failed/pends."""
    normalized = re.sub(r"\s+", " ", str(text or "")).strip().replace("’", "'")
    return bool(_MEDIA_STATUS_TERMS.search(normalized) and _MEDIA_FAILURE_CLAIM.search(normalized))


def refuse_stale_media_status_claim(
    *, account_id: str, chat_id: str, text: str, where: str
) -> None:
    """Refuse a stale media-failure bubble while an approved send is active.

    Multipart media can outlive the model tool timeout: the provider may accept
    the upload after the model has already tried to send a failure sentence.
    Query the exact account/chat ledger before any text POST and fail closed for
    that contradictory sentence only. Missing/locked ledgers fail open so
    ordinary text delivery is never muted.
    """
    if not _looks_like_media_failure_claim(text):
        return
    account = str(account_id or "").strip()
    chat = str(chat_id or "").strip()
    db_path = str(
        os.environ.get("LMI_CRM_DB") or "/var/lib/lmi-dashboard/unipile_webhooks.db"
    ).strip()
    if not account or not chat or not db_path or not os.path.isabs(db_path):
        return
    cutoff = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(seconds=_MEDIA_STATUS_WINDOW_SECONDS)
    ).isoformat()
    active = False
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        try:
            try:
                active = db.execute(
                    """SELECT 1 FROM media_channel_consent
                        WHERE account_id=? AND chat_id=? AND revoked_at IS NULL
                          AND granted_at>=? LIMIT 1""",
                    (account, chat, cutoff),
                ).fetchone() is not None
            except sqlite3.Error:
                pass
            if not active:
                placeholders = ",".join("?" for _ in _MEDIA_ACTIVE_STATES)
                active = db.execute(
                    f"""SELECT 1 FROM media_followup_outbox
                         WHERE account_id=? AND chat_id=?
                           AND state IN ({placeholders}) AND updated_at>=? LIMIT 1""",
                    (account, chat, *_MEDIA_ACTIVE_STATES, cutoff),
                ).fetchone() is not None
        finally:
            db.close()
    except Exception:  # noqa: BLE001 - this guard must not block ordinary sends
        return
    if not active:
        return
    logger.error(
        "[unipile] REFUSED stale media-status text via %s for chat %s",
        where,
        chat[:16],
    )
    raise InternalTextRefused(
        "refusing a media failure message while the approved media send is active"
    )

def _nested_provider_value(value: Any, keys: tuple[str, ...], depth: int = 0) -> str:
    """Find a bounded scalar provider field without retaining the raw response."""
    if depth > 4:
        return ""
    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, (str, int, float)) and not isinstance(candidate, bool):
                text = str(candidate).strip()
                if text:
                    return text[:255]
        for child in value.values():
            found = _nested_provider_value(child, keys, depth + 1)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _nested_provider_value(child, keys, depth + 1)
            if found:
                return found
    return ""


def persist_live_outbound_projection(
    *,
    channel: str,
    account_id: str,
    chat_id: str,
    text: str,
    provider_result: Any = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Mirror one provider-accepted live-tool send into the exact chat ledger.

    Native tool handlers POST directly to Unipile, so no adapter webhook path
    is guaranteed before the customer sends the next turn. This best-effort
    receipt closes that context gap without ever changing the provider result:
    a ledger failure is logged and returned as a status only.
    """
    canonical = {
        "wa": "whatsapp", "whatsapp_unipile": "whatsapp",
        "ig": "instagram", "insta": "instagram",
        "li": "linkedin",
    }.get(str(channel or "").strip().lower(), str(channel or "").strip().lower())
    account = str(account_id or "").strip()
    body = str(text or "").strip()
    if canonical not in {"whatsapp", "instagram", "linkedin"}:
        return {"status": "not_persisted", "reason": "unsupported_channel"}
    if not account or not body:
        return {"status": "not_persisted", "reason": "incomplete_scope"}
    effective_chat = str(chat_id or "").strip()
    if not effective_chat:
        effective_chat = _nested_provider_value(
            provider_result, ("chat_id", "conversation_id")
        )
    if not effective_chat:
        return {"status": "not_persisted", "reason": "provider_chat_id_missing"}
    provider_id = _nested_provider_value(
        provider_result, ("provider_message_id", "message_id", "id")
    )
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    payload = {
        "id": provider_id,
        "message_id": provider_id,
        "text": body[:4000],
        "timestamp": stamp,
        "is_sender": True,
    }
    target = str(
        db_path or os.environ.get("LMI_CRM_DB")
        or "/var/lib/lmi-dashboard/unipile_webhooks.db"
    ).strip()
    if not target or not os.path.isabs(target):
        return {"status": "not_persisted", "reason": "crm_db_path_not_absolute"}
    def _persist_once() -> dict[str, Any]:
        import inbox_transcript

        with sqlite3.connect(target, timeout=5) as con:
            con.execute("PRAGMA busy_timeout=5000")
            stored = inbox_transcript.persist_chat_messages(
                con,
                channel=canonical,
                account_id=account,
                chat_id=effective_chat,
                messages=[payload],
                source="lmi_live_tool",
            )
            # Keep the dashboard's send ledger aligned with the transcript
            # when the provider accepted the tool call. Resolve a member only
            # from the same exact provider scope; an unmapped new chat stays
            # auditable with an empty member id and is never guessed.
            con.execute(
                """CREATE TABLE IF NOT EXISTS outbound_messages(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT, channel TEXT, member_id TEXT, company TEXT,
                    live INTEGER, ok INTEGER, error TEXT, preview TEXT,
                    account_id TEXT, provider_message_id TEXT,
                    send_key TEXT, job_id TEXT)"""
            )
            columns = {str(row[1]) for row in con.execute("PRAGMA table_info(outbound_messages)")}
            for name, ddl in (
                ("account_id", "TEXT"),
                ("provider_message_id", "TEXT"),
                ("send_key", "TEXT"),
                ("job_id", "TEXT"),
            ):
                if name not in columns:
                    con.execute(f"ALTER TABLE outbound_messages ADD COLUMN {name} {ddl}")
            member_id = ""
            try:
                exact = con.execute(
                    """SELECT DISTINCT member_id
                         FROM inbound_conversation_identity
                        WHERE lower(channel)=? AND account_id=? AND chat_id=?
                          AND trim(COALESCE(member_id,''))<>'' LIMIT 2""",
                    (canonical, account, effective_chat),
                ).fetchall()
                if len(exact) == 1:
                    member_id = str(exact[0][0] or "").strip()
            except sqlite3.Error:
                pass
            send_key = "live-tool:" + hashlib.sha256(
                "|".join((canonical, account, effective_chat, provider_id, body[:4000], stamp[:16])).encode("utf-8")
            ).hexdigest()[:48]
            con.execute(
                """INSERT OR IGNORE INTO outbound_messages(
                       ts,channel,member_id,company,live,ok,error,preview,
                       account_id,provider_message_id,send_key,job_id)
                   VALUES(?,?,?,?,1,1,NULL,?,?,?,?,?)""",
                (
                    stamp, canonical, member_id, "", body[:200], account,
                    provider_id, send_key, "lmi_live_tool",
                ),
            )
            con.commit()
        return {
            "status": "persisted",
            "chat_id_present": True,
            "provider_message_id_present": bool(provider_id),
            "transcript_rows": int(stored),
        }

    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            return _persist_once()
        except sqlite3.OperationalError as exc:
            last_exc = exc
            busy = any(marker in str(exc).lower() for marker in ("locked", "busy"))
            if not busy or attempt == 1:
                break
            time.sleep(0.25)
        except Exception as exc:
            last_exc = exc
            break
    reason = type(last_exc).__name__ if last_exc is not None else "OperationalError"
    logger.warning(
        "[unipile] live outbound ledger unavailable channel=%s reason=%s",
        canonical, reason,
    )
    return {"status": "not_persisted", "reason": reason}


class UnipileClient:
    """Async HTTP client wrapping common Unipile REST API calls."""

    def __init__(self, dsn: str, api_key: str, http_client: "httpx.AsyncClient"):
        self.base_url = f"https://{dsn}/api/v1"
        self.api_key = api_key
        self._http = http_client

    @property
    def _headers(self) -> Dict[str, str]:
        return {
            "X-API-KEY": self.api_key,
            "accept": "application/json",
        }

    # ── Chat operations ──────────────────────────────────────────────────

    async def send_message(self, chat_id: str, text: str,
                           account_id: Optional[str] = None) -> Dict[str, Any]:
        """Send a message to an existing chat.

        POST /chats/{chat_id}/messages (JSON body)
        """
        _refuse_internal_text(text, "send_message")
        refuse_stale_media_status_claim(
            account_id=account_id or "", chat_id=chat_id, text=text,
            where="send_message",
        )
        _refuse_repeat(chat_id, text, "send_message")
        payload = {"text": text}
        if account_id:
            payload["account_id"] = account_id
        resp = await self._http.post(
            f"{self.base_url}/chats/{chat_id}/messages",
            headers={**self._headers, "Content-Type": "application/json"},
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()

    async def start_chat(self, account_id: str, attendees_ids: List[str],
                         text: Optional[str] = None) -> Dict[str, Any]:
        """Start a new chat (and optionally send first message).

        POST /chats (json body)
        """
        _refuse_internal_text(text, "start_chat")
        payload: Dict[str, Any] = {
            "account_id": account_id,
            "attendees_ids": attendees_ids,
        }
        if text:
            payload["text"] = text
        resp = await self._http.post(
            f"{self.base_url}/chats",
            headers={**self._headers, "Content-Type": "application/json"},
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()

    async def get_chats(self, account_id: str,
                        limit: int = 20) -> Dict[str, Any]:
        """List chats for an account. GET /chats"""
        resp = await self._http.get(
            f"{self.base_url}/chats",
            headers=self._headers,
            params={"account_id": account_id, "limit": limit},
        )
        resp.raise_for_status()
        return resp.json()

    async def get_messages(self, chat_id: str,
                           limit: int = 20,
                           account_id: Optional[str] = None) -> Dict[str, Any]:
        """List messages in a chat, optionally scoped to one provider account."""
        params: Dict[str, Any] = {"limit": limit}
        if account_id:
            params["account_id"] = account_id
        resp = await self._http.get(
            f"{self.base_url}/chats/{chat_id}/messages",
            headers=self._headers,
            params=params,
        )
        resp.raise_for_status()
        return resp.json()

    async def get_attendees(self, chat_id: str) -> Dict[str, Any]:
        """List attendees of a chat. GET /chats/{chat_id}/attendees"""
        resp = await self._http.get(
            f"{self.base_url}/chats/{chat_id}/attendees",
            headers=self._headers,
        )
        resp.raise_for_status()
        return resp.json()

    async def get_user_profile(self, identifier: str,
                               account_id: str,
                               linkedin_sections: Optional[str] = None,
                               ) -> Dict[str, Any]:
        """Get user profile. GET /users/{identifier}"""
        params: Dict[str, str] = {"account_id": account_id}
        if linkedin_sections:
            params["linkedin_sections"] = linkedin_sections
        resp = await self._http.get(
            f"{self.base_url}/users/{identifier}",
            headers=self._headers,
            params=params,
        )
        resp.raise_for_status()
        return resp.json()

    async def sync_chat(self, chat_id: str,
                        account_id: Optional[str] = None) -> Dict[str, Any]:
        """Trigger chat history sync. GET /chats/{chat_id}/sync (GET not POST!)"""
        params = {}
        if account_id:
            params["account_id"] = account_id
        resp = await self._http.get(
            f"{self.base_url}/chats/{chat_id}/sync",
            headers=self._headers,
            params=params,
        )
        resp.raise_for_status()
        return resp.json()


# ── Webhook Mixin ────────────────────────────────────────────────────────────

class UnipileWebhookMixin:
    """Mixin providing webhook server setup, message dedup, and payload parsing.

    Subclass must define:
      - self._webhook_port: int
      - self._platform_tag: str (e.g. "instagram", "whatsapp")
      - async _dispatch_payload(payload): handle parsed webhook event
    """

    def _init_webhook_mixin(self):
        """Call from adapter __init__ after setting _webhook_port and _platform_tag."""
        self._seen_messages: collections.OrderedDict = collections.OrderedDict()
        self._webhook_runner = None

    async def _setup_webhook_server(self, port: int, path: str = "/webhooks/unipile"):
        """Start aiohttp server listening for Unipile webhooks."""
        app = web.Application()
        app.router.add_get("/health", self._handle_health)
        app.router.add_post(path, self._handle_webhook_request)
        self._webhook_runner = web.AppRunner(app)
        await self._webhook_runner.setup()
        await web.TCPSite(self._webhook_runner, "0.0.0.0", port).start()
        logger.info("[%s] Webhook server listening on 0.0.0.0:%d%s",
                     self._platform_tag, port, path)

    async def _cleanup_webhook_server(self):
        """Stop the webhook server."""
        if self._webhook_runner:
            await self._webhook_runner.cleanup()
            self._webhook_runner = None

    async def _handle_health(self, request) -> Any:
        return web.Response(
            text='{"status":"ok"}', content_type="application/json"
        )

    async def _handle_webhook_request(self, request) -> Any:
        """Parse webhook JSON, return 200 immediately, dispatch async."""
        try:
            payload = await request.json()
        except Exception:
            return web.Response(status=400, text="invalid json")

        event = payload.get("event", "unknown")
        logger.info("[%s] webhook received: event=%s account_type=%s",
                     self._platform_tag, event,
                     payload.get("account_type", "?"))

        # Always 200 — Unipile retries on non-200
        asyncio.ensure_future(self._dispatch_payload(payload))
        return web.Response(status=200)

    def _dedup(self, message_id: str) -> bool:
        """Return True if message_id was already seen (duplicate).

        Uses FIFO cache with TTL to avoid reprocessing webhook retries.
        """
        if not message_id:
            return False

        now = time.monotonic()

        # Evict expired entries
        while self._seen_messages:
            oldest_key, oldest_time = next(iter(self._seen_messages.items()))
            if now - oldest_time > DEDUP_TTL_SECONDS:
                self._seen_messages.pop(oldest_key)
            else:
                break

        if message_id in self._seen_messages:
            return True  # duplicate

        self._seen_messages[message_id] = now

        # Cap size
        while len(self._seen_messages) > DEDUP_CACHE_SIZE:
            self._seen_messages.popitem(last=False)

        return False

    @staticmethod
    def parse_message_payload(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Extract common fields from a message_received webhook payload.

        Returns dict with: text, chat_id, message_id, sender_id, sender_name,
                          is_sender, attachments, timestamp, account_type
        Returns None if payload is not a processable message.
        """
        event = payload.get("event", "")
        if event != "message_received":
            return None

        is_sender = payload.get("is_sender", False)
        if is_sender:
            return None  # ignore our own outbound messages

        text = payload.get("message", "") or ""
        chat_id = payload.get("chat_id", "")
        message_id = payload.get("message_id", "")

        # Extract sender info — try sender dict first, fall back to attendees
        sender = payload.get("sender", {})
        sender_id = sender.get("provider_id", "") or sender.get("attendee_provider_id", "")
        sender_name = sender.get("name", "") or sender.get("attendee_name", "")

        if not sender_id:
            attendees = payload.get("attendees", [])
            if attendees:
                sender_id = attendees[0].get("attendee_provider_id", "")
                sender_name = sender_name or attendees[0].get("attendee_name", "")

        attachments = payload.get("attachments", [])
        timestamp = payload.get("timestamp", "")
        account_type = payload.get("account_type", "")

        if not chat_id:
            return None

        return {
            "text": text,
            "chat_id": chat_id,
            "message_id": message_id,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "is_sender": is_sender,
            "attachments": attachments,
            "timestamp": timestamp,
            "account_type": account_type,
        }

    async def _dispatch_payload(self, payload: Dict[str, Any]) -> None:
        """Override in subclass to handle webhook events."""
        raise NotImplementedError


# ── Text Formatting Helpers ──────────────────────────────────────────────────

def format_plain_text(content: str) -> str:
    """Strip markdown and formatting artifacts for plain-text platforms.

    Removes: bold/italic markers, bullet points, em dashes, numbered lists,
    headers, code blocks, excessive whitespace.
    """
    if not content:
        return ""

    text = content

    # Remove markdown formatting
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)  # bold
    text = re.sub(r'\*(.+?)\*', r'\1', text)       # italic
    text = re.sub(r'__(.+?)__', r'\1', text)       # bold
    text = re.sub(r'_(.+?)_', r'\1', text)         # italic
    text = re.sub(r'`(.+?)`', r'\1', text)         # inline code
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)  # headers
    text = re.sub(r'```[\s\S]*?```', '', text)     # code blocks

    # Remove bullet points and numbered lists
    text = re.sub(r'^[\s]*[-*+]\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^[\s]*\d+\.\s+', '', text, flags=re.MULTILINE)

    # Replace em dashes with commas or periods
    text = text.replace('\u2014', ',')  # em dash
    text = text.replace('\u2013', '-')  # en dash

    # Collapse excessive whitespace
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r' {2,}', ' ', text)

    return text.strip()


def build_routing_context(platform: str, chat_id: str,
                          member_id: str) -> str:
    """Build routing context prefix for enriched message text.

    Format: [platform=INSTAGRAM chat_id=abc123 member_id=xyz789]
    """
    return f"[platform={platform.upper()} chat_id={chat_id} member_id={member_id}]"


def is_system_message(content: str) -> bool:
    """Check if content is a Hermes system message that should not be sent to users."""
    if not content:
        return False
    prefix = content[:200]
    return any(sig in prefix for sig in SYSTEM_MESSAGE_SIGNALS)
