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
import logging
import re
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

        POST /chats/{chat_id}/messages (multipart form)
        """
        data = {"text": text}
        if account_id:
            data["account_id"] = account_id
        resp = await self._http.post(
            f"{self.base_url}/chats/{chat_id}/messages",
            headers=self._headers,
            data=data,
        )
        resp.raise_for_status()
        return resp.json()

    async def start_chat(self, account_id: str, attendees_ids: List[str],
                         text: Optional[str] = None) -> Dict[str, Any]:
        """Start a new chat (and optionally send first message).

        POST /chats (json body)
        """
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
                           limit: int = 20) -> Dict[str, Any]:
        """List messages in a chat. GET /chats/{chat_id}/messages"""
        resp = await self._http.get(
            f"{self.base_url}/chats/{chat_id}/messages",
            headers=self._headers,
            params={"limit": limit},
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
