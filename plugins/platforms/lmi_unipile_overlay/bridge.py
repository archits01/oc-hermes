"""Safe adapter boundary for the reviewed LMI Unipile media bridge.

This module deliberately does not know how to call Unipile.  The reviewed
``unipile_media_followup.MediaFollowupBridge`` is injected by the deployment
that owns the CRM database and provider credentials.  Keeping that dependency
injected makes these rules unit-testable and prevents this overlay from
accidentally acquiring a network path during discovery or tests.

The public tool surface is intentionally smaller than the reviewed bridge:

* ``offer_media`` can send only the source-controlled offer sentence.
* ``send_approved_media`` can send only the source-controlled caption, and
  requires the exact inbound provider message id that supplied consent.

The reviewed bridge remains the authority for opt-out checks, exact-chat
consent, approved-media hashes/root confinement, idempotency, send locks, and
truthful provider outcomes.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Callable, Protocol


FIXED_MEDIA_OFFER_TEMPLATE_ID = "portfolio-media-v1"
FIXED_MEDIA_CAPTION_TEMPLATE_ID = "portfolio-delivery-v1"
SUPPORTED_CHANNELS = ("instagram", "whatsapp")
_MEDIA_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")


class MediaOverlayError(ValueError):
    """The adapter supplied an incomplete or unsafe media tool request."""


@dataclass(frozen=True)
class MediaChatScope:
    """The exact conversation scope resolved by a live adapter, never the model.

    The media bridge treats ``channel/account_id/chat_id`` as its authorization
    boundary. A global native tool must therefore receive those values from
    adapter session/event state, rather than a model tool call.
    """

    channel: str
    account_id: str
    chat_id: str


class ReviewedMediaBridge(Protocol):
    """Subset of the reviewed bridge used by the live adapter overlays."""

    def offer_media(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def send_approved_media(self, **kwargs: Any) -> Mapping[str, Any]: ...


def _required_text(args: Mapping[str, Any], name: str) -> str:
    value = str(args.get(name, "") or "").strip()
    if not value:
        raise MediaOverlayError(f"{name} is required")
    return value


def _media_ids(args: Mapping[str, Any]) -> tuple[str, ...]:
    value = args.get("media_ids")
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise MediaOverlayError("media_ids must be a list of approved media ids")
    ids = tuple(str(item or "").strip() for item in value)
    if not ids or len(ids) > 4 or any(not item for item in ids):
        raise MediaOverlayError("choose between 1 and 4 approved media ids")
    if len(set(ids)) != len(ids):
        raise MediaOverlayError("media_ids must be unique")
    # The reviewed bridge applies the authoritative identifier regex.  This
    # early check keeps malformed model output out of the bridge boundary.
    if any(not _MEDIA_ID.fullmatch(item) for item in ids):
        raise MediaOverlayError("media_ids contain an invalid identifier")
    return ids


async def _call_bridge(method: Any, **kwargs: Any) -> Mapping[str, Any]:
    """Call sync or async injected bridges without changing provider semantics."""
    if inspect.iscoroutinefunction(method):
        result = await method(**kwargs)
    else:
        # The reviewed bridge uses synchronous sqlite/HTTP operations. Keep
        # those off the live adapter event loop while retaining one call and
        # one truthful result per tool invocation.
        result = await asyncio.to_thread(method, **kwargs)
    if inspect.isawaitable(result):
        result = await result
    if not isinstance(result, Mapping):
        raise MediaOverlayError("reviewed media bridge returned a non-object result")
    return result


class MediaOverlay:
    """Channel-scoped adapter facade over the reviewed media bridge."""

    channel: str
    toolset: str

    def __init__(self, reviewed_bridge: ReviewedMediaBridge, *, channel: str):
        channel = str(channel or "").strip().lower()
        if channel not in SUPPORTED_CHANNELS:
            raise MediaOverlayError("media overlay supports only Instagram or WhatsApp")
        self.reviewed_bridge = reviewed_bridge
        self.channel = channel
        self.toolset = f"{channel}-tools"

    @property
    def offer_tool_name(self) -> str:
        """Unique global native-tool name for this channel's media offer."""
        return f"{self.channel}_offer_media"

    @property
    def delivery_tool_name(self) -> str:
        """Unique global native-tool name for this channel's media delivery."""
        return f"{self.channel}_send_approved_media"

    def get_native_tools(self) -> list[dict[str, Any]]:
        """Return the only media tools an LMI live adapter may register."""
        return [
            {
                "name": self.offer_tool_name,
                "description": (
                    "Offer a few relevant LMI portfolio visuals using the approved "
                    "sentence. Call only when visuals are relevant; never invent offer text."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "idempotency_key": {
                            "type": "string",
                            "description": "Stable key for this exact chat offer",
                        },
                        "account_id": {"type": "string"},
                        "chat_id": {"type": "string"},
                    },
                    "required": ["idempotency_key", "account_id", "chat_id"],
                    "additionalProperties": False,
                },
                "toolset": self.toolset,
            },
            {
                "name": self.delivery_tool_name,
                "description": (
                    "Send operator-approved visuals only after the lead explicitly "
                    "requested them or clearly accepted the sent offer. Pass the exact "
                    "inbound provider message id as consent_message_id."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "idempotency_key": {
                            "type": "string",
                            "description": "Stable key for this exact chat delivery",
                        },
                        "account_id": {"type": "string"},
                        "chat_id": {"type": "string"},
                        "consent_message_id": {
                            "type": "string",
                            "description": "Exact inbound provider message id proving consent",
                        },
                        "media_ids": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 4,
                            "items": {"type": "string"},
                            "description": "Operator-approved media ids, never paths or URLs",
                        },
                    },
                    "required": [
                        "idempotency_key",
                        "account_id",
                        "chat_id",
                        "consent_message_id",
                        "media_ids",
                    ],
                    "additionalProperties": False,
                },
                "toolset": self.toolset,
            },
        ]

    async def handle_native_tool(
        self, tool_name: str, arguments: Mapping[str, Any]
    ) -> str:
        """Dispatch a tool with fixed templates and channel scope."""
        if not isinstance(arguments, Mapping):
            return self._error("arguments must be an object")
        try:
            if tool_name == self.offer_tool_name:
                result = await _call_bridge(
                    self.reviewed_bridge.offer_media,
                    idempotency_key=_required_text(arguments, "idempotency_key"),
                    channel=self.channel,
                    account_id=_required_text(arguments, "account_id"),
                    chat_id=_required_text(arguments, "chat_id"),
                    offer_template_id=FIXED_MEDIA_OFFER_TEMPLATE_ID,
                )
            elif tool_name == self.delivery_tool_name:
                result = await _call_bridge(
                    self.reviewed_bridge.send_approved_media,
                    idempotency_key=_required_text(arguments, "idempotency_key"),
                    channel=self.channel,
                    account_id=_required_text(arguments, "account_id"),
                    chat_id=_required_text(arguments, "chat_id"),
                    consent_message_id=_required_text(arguments, "consent_message_id"),
                    media_ids=_media_ids(arguments),
                    caption_template_id=FIXED_MEDIA_CAPTION_TEMPLATE_ID,
                )
            else:
                return self._error(f"unknown media tool: {tool_name}")
            return json.dumps(dict(result), separators=(",", ":"), sort_keys=True)
        except (MediaOverlayError, TypeError, ValueError) as exc:
            return self._error(str(exc))

    @staticmethod
    def _error(message: str) -> str:
        return json.dumps({"status": "review", "reason": str(message)}, separators=(",", ":"))


class WhatsAppMediaOverlay(MediaOverlay):
    """WhatsApp-scoped media overlay facade."""

    def __init__(self, reviewed_bridge: ReviewedMediaBridge):
        super().__init__(reviewed_bridge, channel="whatsapp")


class InstagramMediaOverlay(MediaOverlay):
    """Instagram-scoped media overlay facade."""

    def __init__(self, reviewed_bridge: ReviewedMediaBridge):
        super().__init__(reviewed_bridge, channel="instagram")


def _scope_from_mapping(value: Mapping[str, Any], *, channel: str) -> MediaChatScope:
    """Validate the adapter-owned scope returned for a native-tool session."""
    if not isinstance(value, Mapping):
        raise MediaOverlayError("adapter did not resolve a usable live chat scope")
    actual_channel = str(value.get("channel", channel) or "").strip().lower()
    if actual_channel != channel:
        raise MediaOverlayError("adapter scope channel does not match this media tool")
    return MediaChatScope(
        channel=channel,
        account_id=_required_text(value, "account_id"),
        chat_id=_required_text(value, "chat_id"),
    )


def _scoped_schema(tool: Mapping[str, Any]) -> dict[str, Any]:
    """Return a schema that cannot override adapter-owned conversation scope."""
    schema = dict(tool)
    parameters = dict(schema["parameters"])
    properties = dict(parameters["properties"])
    properties.pop("account_id", None)
    properties.pop("chat_id", None)
    parameters["properties"] = properties
    parameters["required"] = [
        name for name in parameters.get("required", [])
        if name not in {"account_id", "chat_id"}
    ]
    parameters["additionalProperties"] = False
    schema["parameters"] = parameters
    return schema


def register_adapter_media_tools(
    ctx: Any,
    reviewed_bridge: ReviewedMediaBridge,
    *,
    channel: str,
    scope_resolver: Callable[[str], Mapping[str, Any]],
) -> tuple[str, str]:
    """Register media tools for one live adapter without trusting model scope.

    The deployment-owned ``scope_resolver`` maps the current Hermes
    ``session_id`` to the exact inbound account/chat. The adapter must bind that
    mapping before it dispatches the inbound turn. This function deliberately
    has no environment lookup or provider construction: a deployment has to
    supply the reviewed bridge and verified scope source explicitly.
    """
    if not callable(getattr(ctx, "register_tool", None)):
        raise MediaOverlayError("adapter media registration needs a plugin context")
    if not callable(scope_resolver):
        raise MediaOverlayError("adapter media registration needs a session scope resolver")

    overlay = MediaOverlay(reviewed_bridge, channel=channel)
    tools = {tool["name"]: _scoped_schema(tool) for tool in overlay.get_native_tools()}

    async def _dispatch(
        tool_name: str, arguments: Mapping[str, Any], **kwargs: Any,
    ) -> str:
        try:
            session_id = str(kwargs.get("session_id", "") or "").strip()
            if not session_id:
                raise MediaOverlayError("media tool has no bound live session")
            if any(name in arguments for name in ("channel", "account_id", "chat_id")):
                raise MediaOverlayError("model may not override the live chat scope")
            try:
                resolved_scope = scope_resolver(session_id)
            except Exception as exc:
                raise MediaOverlayError("adapter could not resolve the live chat scope") from exc
            scope = _scope_from_mapping(resolved_scope, channel=overlay.channel)
            merged = dict(arguments)
            merged.update({"account_id": scope.account_id, "chat_id": scope.chat_id})
            return await overlay.handle_native_tool(tool_name, merged)
        except (MediaOverlayError, TypeError, ValueError) as exc:
            return MediaOverlay._error(str(exc))

    for name, schema in tools.items():
        # The global registry dispatches handlers without the tool name. Bind
        # it at registration while retaining one strict scope boundary.
        async def named_handler(
            arguments: Mapping[str, Any], *, _name: str = name, **kwargs: Any,
        ) -> str:
            return await _dispatch(_name, arguments, **kwargs)

        ctx.register_tool(
            name=name,
            toolset=overlay.toolset,
            schema=schema,
            handler=named_handler,
            is_async=True,
        )
    return overlay.offer_tool_name, overlay.delivery_tool_name
