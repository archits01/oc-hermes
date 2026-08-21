"""Explicit, fail-closed wiring for the reviewed LMI media bridge.

This module is intentionally inert at plugin discovery time.  A deployment
must explicitly provide its credentials, authoritative state database, and
the reviewed ``unipile_media_followup`` module before any model-visible tools
are registered.  It never reads environment variables, never discovers a
provider module by name, and never sends a provider request during setup.

The trust boundary is deliberately narrow:

* Hermes supplies the current ``session_id`` to a native-tool handler.
* The resolver reads the durable gateway session row for that id and requires
  its recorded source *and* ``origin_json`` to agree on channel/chat.
* The account id comes from a verified binding created from the raw inbound
  payload by a deployment-owned adapter patch, then checked against deployment
  configuration.  It is never derived from a model argument or a session row.

If a session row is absent, malformed, non-gateway, lacks a verified inbound
account binding, or belongs to the other channel, no media bridge call occurs.
This makes a partial/in-flight session write a safe failure instead of a guess
about a customer conversation.
"""
from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

from .bridge import (
    MediaChatScope,
    MediaOverlayError,
    ReviewedMediaBridge,
    SUPPORTED_CHANNELS,
    register_adapter_media_tools,
)


class SessionRowReader(Protocol):
    """The read-only subset of :class:`hermes_state.SessionDB` we require."""

    def get_session(self, session_id: str) -> Mapping[str, Any] | None: ...


class ReviewedMediaModule(Protocol):
    """The reviewed deployment module, supplied explicitly by deployment code."""

    UnipileV1Provider: Any
    MediaFollowupBridge: Any


def _text(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise MediaOverlayError(f"deployment {name} is required")
    return text


@dataclass(frozen=True)
class MediaBridgeDeploymentConfig:
    """All non-model inputs needed to wire the reviewed bridge.

    The caller owns this configuration and its secret lifecycle.  Keeping it
    as an explicit value prevents a global plugin import from silently reading
    a different profile's environment or state database.
    """

    unipile_dsn: str = field(repr=False)
    unipile_api_key: str = field(repr=False)
    crm_db_path: str
    approved_media_root: str
    session_db_path: str
    channel_account_ids: Mapping[str, str]
    channel_adapter_platform_ids: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "unipile_dsn", _text(self.unipile_dsn, "unipile_dsn"))
        object.__setattr__(
            self, "unipile_api_key", _text(self.unipile_api_key, "unipile_api_key")
        )
        crm_db_path = Path(_text(self.crm_db_path, "crm_db_path"))
        approved_media_root = Path(_text(self.approved_media_root, "approved_media_root"))
        if not crm_db_path.is_absolute() or not approved_media_root.is_absolute():
            raise MediaOverlayError("deployment CRM and approved-media paths must be absolute")
        object.__setattr__(self, "crm_db_path", str(crm_db_path))
        object.__setattr__(self, "approved_media_root", str(approved_media_root))
        session_db_path = Path(_text(self.session_db_path, "session_db_path"))
        if not session_db_path.is_absolute():
            raise MediaOverlayError("deployment session_db_path must be absolute")
        object.__setattr__(self, "session_db_path", str(session_db_path))

        if not isinstance(self.channel_account_ids, Mapping):
            raise MediaOverlayError("deployment channel_account_ids must be a mapping")
        accounts: dict[str, str] = {}
        for raw_channel, raw_account in self.channel_account_ids.items():
            channel = str(raw_channel or "").strip().lower()
            if channel not in SUPPORTED_CHANNELS:
                raise MediaOverlayError("deployment has an unsupported media channel")
            accounts[channel] = _text(raw_account, f"{channel} account_id")
        if set(accounts) != set(SUPPORTED_CHANNELS):
            raise MediaOverlayError(
                "deployment must bind exactly one account for whatsapp and instagram"
            )
        object.__setattr__(self, "channel_account_ids", MappingProxyType(accounts))

        if not isinstance(self.channel_adapter_platform_ids, Mapping):
            raise MediaOverlayError(
                "deployment channel_adapter_platform_ids must be a mapping"
            )
        platforms: dict[str, str] = {}
        for raw_channel, raw_platform in self.channel_adapter_platform_ids.items():
            channel = str(raw_channel or "").strip().lower()
            if channel not in SUPPORTED_CHANNELS:
                raise MediaOverlayError("deployment has an unsupported adapter channel")
            platforms[channel] = _text(raw_platform, f"{channel} adapter platform")
        if set(platforms) != set(SUPPORTED_CHANNELS) or len(set(platforms.values())) != len(
            SUPPORTED_CHANNELS
        ):
            raise MediaOverlayError(
                "deployment must bind exactly one distinct adapter platform for whatsapp and instagram"
            )
        object.__setattr__(
            self, "channel_adapter_platform_ids", MappingProxyType(platforms)
        )


class VerifiedInboundMediaScopeRegistry:
    """One-process proof that a session key came from an exact inbound account.

    Hermes's stock ``SessionSource`` persists platform/chat but has no Unipile
    account field.  The deployment-owned adapter patch must therefore call
    :meth:`bind` after it has strictly validated the raw webhook account and
    before it submits the event to the live-reply queue.  A restart drops this
    cache and deliberately causes tools to fail closed until another verified
    inbound event recreates the binding.
    """

    def __init__(self, config: MediaBridgeDeploymentConfig) -> None:
        self._accounts = dict(config.channel_account_ids)
        self._platforms = dict(config.channel_adapter_platform_ids)
        self._bindings: dict[str, MediaChatScope] = {}
        self._lock = threading.RLock()

    def bind(
        self,
        *,
        session_key: str,
        channel: str,
        adapter_platform: str,
        chat_id: str,
        inbound_payload: Mapping[str, Any],
    ) -> MediaChatScope:
        """Record one adapter-validated inbound route or reject it.

        ``inbound_payload`` is intentionally required instead of an
        ``account_id`` string so a caller cannot accidentally turn the fixed
        adapter account into pretend provider evidence.  Missing payload
        account ids are rejected just as mismatches are.
        """
        if not isinstance(inbound_payload, Mapping):
            raise MediaOverlayError("inbound payload is required for media scope binding")
        session_key = _text(session_key, "gateway session_key")
        channel = _text(channel, "inbound channel").lower()
        if channel not in self._accounts:
            raise MediaOverlayError("inbound media channel is not configured")
        platform = _text(adapter_platform, "inbound adapter platform")
        if platform != self._platforms[channel]:
            raise MediaOverlayError("inbound adapter platform does not match deployment configuration")
        raw_account = _text(inbound_payload.get("account_id"), "inbound account_id")
        if raw_account != self._accounts[channel]:
            raise MediaOverlayError("inbound account_id does not match the configured adapter")
        scope = MediaChatScope(
            channel=channel,
            account_id=raw_account,
            chat_id=_text(chat_id, "inbound chat_id"),
        )
        with self._lock:
            existing = self._bindings.get(session_key)
            if existing is not None and existing != scope:
                raise MediaOverlayError("gateway session key cannot be rebound to another media chat")
            self._bindings[session_key] = scope
        return scope

    def resolve(self, session_key: str) -> MediaChatScope:
        session_key = _text(session_key, "gateway session_key")
        with self._lock:
            scope = self._bindings.get(session_key)
        if scope is None:
            raise MediaOverlayError("no verified inbound account binding exists for this session")
        return scope


def bind_verified_adapter_inbound_event(
    *,
    adapter: Any,
    channel: str,
    source: Any,
    inbound_payload: Mapping[str, Any],
    config: MediaBridgeDeploymentConfig,
    inbound_scopes: VerifiedInboundMediaScopeRegistry,
) -> MediaChatScope:
    """Bind one live adapter event before it reaches the reply queue.

    This is the exact call the deployment-owned WhatsApp and Instagram
    adapters must add immediately after ``build_source(...)`` and before
    ``submit_live_reply(...)``.  It repeats the strict account check rather
    than trusting an adapter's historical best-effort account filter.
    """
    channel = _text(channel, "inbound channel").lower()
    if channel not in config.channel_account_ids:
        raise MediaOverlayError("inbound media channel is not configured")
    adapter_account = _text(getattr(adapter, "_account_id", None), "adapter account_id")
    if adapter_account != config.channel_account_ids[channel]:
        raise MediaOverlayError("adapter account_id does not match deployment configuration")
    source_platform = getattr(getattr(source, "platform", None), "value", None)
    source_platform = _text(source_platform, "source platform")
    if source_platform != config.channel_adapter_platform_ids[channel]:
        raise MediaOverlayError("source platform does not match deployment configuration")
    source_chat_id = _text(getattr(source, "chat_id", None), "source chat_id")

    # This mirrors BasePlatformAdapter.handle_message exactly, including a
    # multiplexed profile's namespace. A mismatch after a future core routing
    # change becomes a missing binding at tool time, never a cross-chat send.
    from gateway.session import build_session_key

    profile = None
    store = getattr(adapter, "_session_store", None)
    resolver = getattr(store, "_resolve_profile_for_key", None)
    if callable(resolver):
        profile = resolver(source)
    adapter_config = getattr(adapter, "config", None)
    extra = getattr(adapter_config, "extra", None) or {}
    session_key = build_session_key(
        source,
        group_sessions_per_user=extra.get("group_sessions_per_user", True),
        thread_sessions_per_user=extra.get("thread_sessions_per_user", False),
        profile=profile,
    )
    return inbound_scopes.bind(
        session_key=session_key,
        channel=channel,
        adapter_platform=source_platform,
        chat_id=source_chat_id,
        inbound_payload=inbound_payload,
    )


class SessionDatabaseMediaScopeResolver:
    """Resolve a media scope from one canonical Hermes session id.

    ``SessionStore.get_or_create_session`` writes the session row and its
    gateway peer before the gateway creates ``AIAgent``. The deployment adapter
    binds a raw, exact inbound account to the deterministic gateway session key
    before queue submission. The model dispatcher then passes that agent's
    real session id here. We validate both identities so a local or malformed
    session can never acquire a customer-facing media scope.
    """

    def __init__(
        self,
        session_db: SessionRowReader,
        *,
        config: MediaBridgeDeploymentConfig,
        inbound_scopes: VerifiedInboundMediaScopeRegistry,
    ) -> None:
        if not callable(getattr(session_db, "get_session", None)):
            raise MediaOverlayError("deployment session reader must support get_session")
        self._session_db = session_db
        self._accounts = dict(config.channel_account_ids)
        self._platforms = dict(config.channel_adapter_platform_ids)
        self._inbound_scopes = inbound_scopes
        if set(self._accounts) != set(SUPPORTED_CHANNELS):
            raise MediaOverlayError("session resolver needs both configured media accounts")
        if set(self._platforms) != set(SUPPORTED_CHANNELS):
            raise MediaOverlayError("session resolver needs both configured adapter platforms")
        if not isinstance(inbound_scopes, VerifiedInboundMediaScopeRegistry):
            raise MediaOverlayError("session resolver needs verified inbound account bindings")

    @staticmethod
    def _origin(row: Mapping[str, Any]) -> Mapping[str, Any]:
        raw_origin = row.get("origin_json")
        if isinstance(raw_origin, str):
            try:
                raw_origin = json.loads(raw_origin)
            except (TypeError, ValueError) as exc:
                raise MediaOverlayError("live session has malformed origin metadata") from exc
        if not isinstance(raw_origin, Mapping):
            raise MediaOverlayError("live session has no verified inbound origin")
        return raw_origin

    def __call__(self, session_id: str) -> Mapping[str, str]:
        session_id = _text(session_id, "session_id")
        try:
            row = self._session_db.get_session(session_id)
        except Exception as exc:
            raise MediaOverlayError("could not read the live session scope") from exc
        if not isinstance(row, Mapping):
            raise MediaOverlayError("no durable live session scope exists")

        # Gateway-origin rows always carry a routing key.  Requiring it denies
        # local/manual rows that merely use a similar platform source string.
        _text(row.get("session_key"), "gateway session_key")
        adapter_platform = _text(row.get("source"), "session source")
        channels = {
            channel
            for channel, configured_platform in self._platforms.items()
            if configured_platform == adapter_platform
        }
        if len(channels) != 1:
            raise MediaOverlayError("session source is not a configured media channel")
        channel = channels.pop()
        chat_id = _text(row.get("chat_id"), "session chat_id")

        origin = self._origin(row)
        origin_platform = _text(origin.get("platform"), "origin platform")
        origin_chat_id = _text(origin.get("chat_id"), "origin chat_id")
        if origin_platform != adapter_platform or origin_chat_id != chat_id:
            raise MediaOverlayError("live session origin does not match its gateway row")

        scope = self._inbound_scopes.resolve(_text(row.get("session_key"), "gateway session_key"))
        if (
            scope.channel != channel
            or scope.account_id != self._accounts[channel]
            or scope.chat_id != chat_id
        ):
            raise MediaOverlayError("verified inbound account binding does not match the live session")
        return {
            "channel": scope.channel,
            "account_id": scope.account_id,
            "chat_id": scope.chat_id,
        }


def open_session_database_scope_resolver(
    config: MediaBridgeDeploymentConfig,
    *,
    inbound_scopes: VerifiedInboundMediaScopeRegistry,
    session_db_factory: Callable[..., SessionRowReader] | None = None,
) -> SessionDatabaseMediaScopeResolver:
    """Open the deployment-selected state database for an explicit install.

    The factory is injectable for tests.  The default import occurs only when
    an operator calls this function, not when Hermes discovers this package.
    """
    if session_db_factory is None:
        from hermes_state import SessionDB

        session_db_factory = SessionDB
    try:
        session_db = session_db_factory(db_path=config.session_db_path)
    except Exception as exc:
        raise MediaOverlayError("could not open configured Hermes session database") from exc
    return SessionDatabaseMediaScopeResolver(
        session_db, config=config, inbound_scopes=inbound_scopes
    )


def construct_reviewed_media_bridge(
    config: MediaBridgeDeploymentConfig,
    *,
    media_module: ReviewedMediaModule,
) -> ReviewedMediaBridge:
    """Construct the pinned deployment bridge without making a provider call.

    ``media_module`` is deliberately injected rather than imported from an
    arbitrary config string.  Deployment review must first verify that the
    loaded module is the manifest-pinned ``unipile_media_followup`` source.
    """
    provider_cls = getattr(media_module, "UnipileV1Provider", None)
    bridge_cls = getattr(media_module, "MediaFollowupBridge", None)
    if not callable(provider_cls) or not callable(bridge_cls):
        raise MediaOverlayError("reviewed media module has the wrong bridge interface")
    try:
        provider = provider_cls(dsn=config.unipile_dsn, api_key=config.unipile_api_key)
        bridge = bridge_cls(
            provider,
            db_path=config.crm_db_path,
            media_root=Path(config.approved_media_root),
        )
    except Exception as exc:
        raise MediaOverlayError("could not construct the reviewed media bridge") from exc
    if not callable(getattr(bridge, "offer_media", None)) or not callable(
        getattr(bridge, "send_approved_media", None)
    ):
        raise MediaOverlayError("reviewed media bridge has the wrong tool interface")
    return bridge


def install_deployment_media_tools(
    ctx: Any,
    *,
    config: MediaBridgeDeploymentConfig,
    media_module: ReviewedMediaModule,
    session_db: SessionRowReader,
    inbound_scopes: VerifiedInboundMediaScopeRegistry,
) -> dict[str, tuple[str, str]]:
    """Register both channel tool pairs with verified session scope.

    This is the only intended live-install entry point. It registers tools
    after all validation succeeds and returns their stable names for deployment
    diagnostics. Construction alone does not call the provider; the reviewed
    bridge is invoked only when a live, canonical session reaches a tool call
    with a prior verified inbound account binding.
    """
    scope_resolver = SessionDatabaseMediaScopeResolver(
        session_db, config=config, inbound_scopes=inbound_scopes
    )
    reviewed_bridge = construct_reviewed_media_bridge(config, media_module=media_module)
    return {
        channel: register_adapter_media_tools(
            ctx,
            reviewed_bridge,
            channel=channel,
            scope_resolver=scope_resolver,
        )
        for channel in SUPPORTED_CHANNELS
    }
