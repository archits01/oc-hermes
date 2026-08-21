from __future__ import annotations

import json
from pathlib import Path

import pytest

from plugins.platforms.lmi_unipile_overlay.bridge import (
    FIXED_MEDIA_CAPTION_TEMPLATE_ID,
    FIXED_MEDIA_OFFER_TEMPLATE_ID,
    InstagramMediaOverlay,
    MediaOverlay,
    MediaOverlayError,
    WhatsAppMediaOverlay,
    register_adapter_media_tools,
)
from plugins.platforms.lmi_unipile_overlay.deployment import (
    MediaBridgeDeploymentConfig,
    SessionDatabaseMediaScopeResolver,
    VerifiedInboundMediaScopeRegistry,
    bind_verified_adapter_inbound_event,
    construct_reviewed_media_bridge,
    install_deployment_media_tools,
    open_session_database_scope_resolver,
)


def test_review_manifest_is_disabled_and_portable():
    manifest = (
        Path(__file__).parents[3]
        / "plugins/platforms/lmi_unipile_overlay/manifest.yaml"
    ).read_text(encoding="utf-8")
    assert "enabled: false" in manifest
    assert "provider_calls: prohibited" in manifest
    assert "/Users/" not in manifest
    assert "dd888ec3d50d1b6e071a7a59553a49933ac12e92" in manifest
    assert "411efe57a621183bef06b64478fe8defdaa5602999e834397eaea77aeb8d86bb" in manifest
    assert "371096adbf0af11eac1e84e7ddc3e792e80c07bfe530d300ae16fb33b57f60c9" in manifest
    assert "80f88937181c79f07a0b40e4e08ae01c86b3eb2dc04c0cbe433e5607ca85e15f" in manifest


class FakeReviewedBridge:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def offer_media(self, **kwargs):
        self.calls.append(("offer", kwargs))
        return {"status": "sent", "offer_message_id": "offer-1"}

    def send_approved_media(self, **kwargs):
        self.calls.append(("send", kwargs))
        return {"status": "sent", "provider_message_id": "media-1"}


class RefusingReviewedBridge(FakeReviewedBridge):
    def send_approved_media(self, **kwargs):
        self.calls.append(("send", kwargs))
        return {"status": "review", "reason": "do_not_contact"}


def test_tools_are_channel_scoped_and_require_exact_consent_id():
    overlay = InstagramMediaOverlay(FakeReviewedBridge())
    tools = {tool["name"]: tool for tool in overlay.get_native_tools()}

    assert {tool["name"] for tool in overlay.get_native_tools()} == {
        "instagram_offer_media",
        "instagram_send_approved_media",
    }
    assert all(tool["toolset"] == "instagram-tools" for tool in tools.values())
    assert "consent_message_id" in tools["instagram_send_approved_media"]["parameters"]["required"]
    assert tools["instagram_send_approved_media"]["parameters"]["additionalProperties"] is False


@pytest.mark.asyncio
async def test_offer_and_send_use_fixed_templates_and_channel():
    fake = FakeReviewedBridge()
    overlay = WhatsAppMediaOverlay(fake)

    offered = json.loads(await overlay.handle_native_tool("whatsapp_offer_media", {
        "idempotency_key": "offer-key-1",
        "account_id": "wa-account",
        "chat_id": "chat-1",
    }))
    sent = json.loads(await overlay.handle_native_tool("whatsapp_send_approved_media", {
        "idempotency_key": "media-key-1",
        "account_id": "wa-account",
        "chat_id": "chat-1",
        "consent_message_id": "inbound-provider-message-1",
        "media_ids": ["portfolio-1"],
    }))

    assert offered["status"] == "sent" and sent["status"] == "sent"
    assert fake.calls == [
        ("offer", {
            "idempotency_key": "offer-key-1", "channel": "whatsapp",
            "account_id": "wa-account", "chat_id": "chat-1",
            "offer_template_id": FIXED_MEDIA_OFFER_TEMPLATE_ID,
        }),
        ("send", {
            "idempotency_key": "media-key-1", "channel": "whatsapp",
            "account_id": "wa-account", "chat_id": "chat-1",
            "consent_message_id": "inbound-provider-message-1",
            "media_ids": ("portfolio-1",),
            "caption_template_id": FIXED_MEDIA_CAPTION_TEMPLATE_ID,
        }),
    ]


@pytest.mark.asyncio
async def test_invalid_media_inputs_never_reach_reviewed_bridge():
    fake = FakeReviewedBridge()
    overlay = WhatsAppMediaOverlay(fake)

    result = json.loads(await overlay.handle_native_tool("whatsapp_send_approved_media", {
        "idempotency_key": "media-key-2",
        "account_id": "wa-account",
        "chat_id": "chat-1",
        "consent_message_id": "inbound-provider-message-2",
        "media_ids": ["https://example.invalid/work.jpg"],
    }))

    assert result["status"] == "review"
    assert not fake.calls
    with pytest.raises(MediaOverlayError):
        MediaOverlay(FakeReviewedBridge(), channel="linkedin")


@pytest.mark.asyncio
async def test_authoritative_optout_result_passes_through_unchanged():
    bridge = RefusingReviewedBridge()
    overlay = WhatsAppMediaOverlay(bridge)

    result = json.loads(await overlay.handle_native_tool("whatsapp_send_approved_media", {
        "idempotency_key": "media-key-optout",
        "account_id": "wa-account",
        "chat_id": "chat-1",
        "consent_message_id": "inbound-provider-message-optout",
        "media_ids": ["portfolio-1"],
    }))

    assert result == {"status": "review", "reason": "do_not_contact"}
    assert len(bridge.calls) == 1


class FakeContext:
    def __init__(self):
        self.tools = {}

    def register_tool(self, **kwargs):
        assert kwargs["name"] not in self.tools, "global tool name collision"
        self.tools[kwargs["name"]] = kwargs


@pytest.mark.asyncio
async def test_adapter_registration_is_unique_and_injects_session_scope():
    context = FakeContext()
    wa_bridge = FakeReviewedBridge()
    ig_bridge = FakeReviewedBridge()
    sessions = {
        "wa-session": {"channel": "whatsapp", "account_id": "wa-account", "chat_id": "wa-chat"},
        "ig-session": {"channel": "instagram", "account_id": "ig-account", "chat_id": "ig-chat"},
    }

    def resolve_scope(session_id):
        return sessions[session_id]

    assert register_adapter_media_tools(
        context, wa_bridge, channel="whatsapp", scope_resolver=resolve_scope,
    ) == ("whatsapp_offer_media", "whatsapp_send_approved_media")
    assert register_adapter_media_tools(
        context, ig_bridge, channel="instagram", scope_resolver=resolve_scope,
    ) == ("instagram_offer_media", "instagram_send_approved_media")
    assert set(context.tools) == {
        "whatsapp_offer_media", "whatsapp_send_approved_media",
        "instagram_offer_media", "instagram_send_approved_media",
    }

    # The model schema cannot name another chat, and the handler injects the
    # exact account/chat resolved from the currently running live session.
    delivery = context.tools["whatsapp_send_approved_media"]
    assert "account_id" not in delivery["schema"]["parameters"]["properties"]
    assert "chat_id" not in delivery["schema"]["parameters"]["properties"]
    result = json.loads(await delivery["handler"]({
        "idempotency_key": "media-key-3",
        "consent_message_id": "inbound-provider-message-3",
        "media_ids": ["portfolio-1"],
    }, session_id="wa-session"))
    assert result["status"] == "sent"
    assert wa_bridge.calls[-1][1]["channel"] == "whatsapp"
    assert wa_bridge.calls[-1][1]["account_id"] == "wa-account"
    assert wa_bridge.calls[-1][1]["chat_id"] == "wa-chat"

    rejected = json.loads(await delivery["handler"]({
        "idempotency_key": "media-key-4",
        "account_id": "other-account",
        "consent_message_id": "inbound-provider-message-4",
        "media_ids": ["portfolio-1"],
    }, session_id="wa-session"))
    assert rejected == {"status": "review", "reason": "model may not override the live chat scope"}
    assert len(wa_bridge.calls) == 1

    no_scope = json.loads(await delivery["handler"]({
        "idempotency_key": "media-key-5",
        "consent_message_id": "inbound-provider-message-5",
        "media_ids": ["portfolio-1"],
    }, session_id="unknown-session"))
    assert no_scope == {"status": "review", "reason": "adapter could not resolve the live chat scope"}


class FakeSessionDb:
    def __init__(self, rows):
        self.rows = rows
        self.lookups = []

    def get_session(self, session_id):
        self.lookups.append(session_id)
        return self.rows.get(session_id)


class FakeProvider:
    def __init__(self, *, dsn, api_key):
        self.dsn = dsn
        self.api_key = api_key
        self.send_calls = []

    def send(self, **kwargs):
        self.send_calls.append(kwargs)
        return "provider-message"

    def send_offer(self, **kwargs):
        self.send_calls.append(kwargs)
        return "provider-offer"


class FakeDeploymentBridge(FakeReviewedBridge):
    instances = []

    def __init__(self, provider, *, db_path, media_root):
        super().__init__()
        self.provider = provider
        self.db_path = db_path
        self.media_root = media_root
        self.instances.append(self)


class FakeReviewedModule:
    UnipileV1Provider = FakeProvider
    MediaFollowupBridge = FakeDeploymentBridge


def deployment_config():
    return MediaBridgeDeploymentConfig(
        unipile_dsn="tenant.unipile.example",
        unipile_api_key="test-api-key",
        crm_db_path="/var/lib/lmi-dashboard/unipile_webhooks.db",
        approved_media_root="/var/lib/lmi-dashboard/approved_media",
        session_db_path="/opt/opencomputer-v2-data/state.db",
        channel_account_ids={
            "whatsapp": "whatsapp-account",
            "instagram": "instagram-account",
        },
        channel_adapter_platform_ids={
            "whatsapp": "whatsapp_unipile",
            "instagram": "instagram",
        },
    )


def gateway_row(adapter_platform, chat_id):
    return {
        "session_key": f"agent:main:{adapter_platform}:dm:{chat_id}",
        "source": adapter_platform,
        "chat_id": chat_id,
        "origin_json": json.dumps({"platform": adapter_platform, "chat_id": chat_id}),
    }


def verified_inbound_scopes(db):
    scopes = VerifiedInboundMediaScopeRegistry(deployment_config())
    for session_id, row in db.rows.items():
        if session_id in {"malformed", "local"}:
            continue
        adapter_platform = row["source"]
        channel = "whatsapp" if adapter_platform == "whatsapp_unipile" else "instagram"
        scopes.bind(
            session_key=row["session_key"],
            channel=channel,
            adapter_platform=adapter_platform,
            chat_id=row["chat_id"],
            inbound_payload={"account_id": deployment_config().channel_account_ids[channel]},
        )
    return scopes


def test_session_database_scope_resolver_requires_redundant_gateway_identity():
    db = FakeSessionDb({
        "wa-session": gateway_row("whatsapp_unipile", "wa-chat"),
        "malformed": {
            **gateway_row("whatsapp_unipile", "wa-chat"),
            "origin_json": json.dumps({"platform": "whatsapp_unipile", "chat_id": "other-chat"}),
        },
        "local": {
            **gateway_row("whatsapp_unipile", "wa-chat"),
            "session_key": "",
        },
    })
    resolver = SessionDatabaseMediaScopeResolver(
        db, config=deployment_config(), inbound_scopes=verified_inbound_scopes(db),
    )

    assert resolver("wa-session") == {
        "channel": "whatsapp",
        "account_id": "whatsapp-account",
        "chat_id": "wa-chat",
    }
    unbound_resolver = SessionDatabaseMediaScopeResolver(
        db,
        config=deployment_config(),
        inbound_scopes=VerifiedInboundMediaScopeRegistry(deployment_config()),
    )
    with pytest.raises(MediaOverlayError, match="no verified inbound account binding"):
        unbound_resolver("wa-session")
    with pytest.raises(MediaOverlayError, match="origin"):
        resolver("malformed")
    with pytest.raises(MediaOverlayError, match="gateway session_key"):
        resolver("local")
    with pytest.raises(MediaOverlayError, match="no durable"):
        resolver("missing")


def test_deployment_config_requires_exactly_the_two_adapter_accounts():
    kwargs = {
        "unipile_dsn": "dsn",
        "unipile_api_key": "key",
        "crm_db_path": "/tmp/crm.db",
        "approved_media_root": "/tmp/media",
        "session_db_path": "/tmp/state.db",
        "channel_adapter_platform_ids": {
            "whatsapp": "whatsapp_unipile", "instagram": "instagram",
        },
    }
    with pytest.raises(MediaOverlayError, match="exactly one account"):
        MediaBridgeDeploymentConfig(**kwargs, channel_account_ids={"whatsapp": "wa"})
    with pytest.raises(MediaOverlayError, match="absolute"):
        MediaBridgeDeploymentConfig(
            **{**kwargs, "session_db_path": "state.db"},
            channel_account_ids={"whatsapp": "wa", "instagram": "ig"},
        )
    with pytest.raises(MediaOverlayError, match="CRM and approved-media"):
        MediaBridgeDeploymentConfig(
            **{**kwargs, "crm_db_path": "crm.db"},
            channel_account_ids={"whatsapp": "wa", "instagram": "ig"},
        )


def test_bridge_construction_uses_only_explicit_deployment_values():
    bridge = construct_reviewed_media_bridge(
        deployment_config(), media_module=FakeReviewedModule,
    )

    assert isinstance(bridge, FakeDeploymentBridge)
    assert bridge.provider.dsn == "tenant.unipile.example"
    assert bridge.provider.api_key == "test-api-key"
    assert bridge.db_path == "/var/lib/lmi-dashboard/unipile_webhooks.db"
    assert bridge.media_root == Path("/var/lib/lmi-dashboard/approved_media")
    assert bridge.provider.send_calls == []


def test_scope_factory_uses_the_explicit_state_database_path():
    recorded = {}
    db = FakeSessionDb({})

    def factory(*, db_path):
        recorded["db_path"] = db_path
        return db

    resolver = open_session_database_scope_resolver(
        deployment_config(),
        inbound_scopes=VerifiedInboundMediaScopeRegistry(deployment_config()),
        session_db_factory=factory,
    )
    assert isinstance(resolver, SessionDatabaseMediaScopeResolver)
    assert recorded == {"db_path": "/opt/opencomputer-v2-data/state.db"}


@pytest.mark.asyncio
async def test_deployment_install_binds_each_tool_to_canonical_session_not_model_scope():
    context = FakeContext()
    db = FakeSessionDb({
        "wa-session": gateway_row("whatsapp_unipile", "wa-chat"),
        "ig-session": gateway_row("instagram", "ig-chat"),
    })
    inbound_scopes = verified_inbound_scopes(db)

    registered = install_deployment_media_tools(
        context,
        config=deployment_config(),
        media_module=FakeReviewedModule,
        session_db=db,
        inbound_scopes=inbound_scopes,
    )
    assert registered == {
        "instagram": ("instagram_offer_media", "instagram_send_approved_media"),
        "whatsapp": ("whatsapp_offer_media", "whatsapp_send_approved_media"),
    }

    whatsapp_offer = context.tools["whatsapp_offer_media"]
    sent = json.loads(await whatsapp_offer["handler"](
        {"idempotency_key": "offer-key-canonical-session"},
        session_id="wa-session",
    ))
    assert sent == {"offer_message_id": "offer-1", "status": "sent"}
    assert FakeDeploymentBridge.instances[-1].calls[-1] == (
        "offer",
        {
            "idempotency_key": "offer-key-canonical-session",
            "channel": "whatsapp",
            "account_id": "whatsapp-account",
            "chat_id": "wa-chat",
            "offer_template_id": FIXED_MEDIA_OFFER_TEMPLATE_ID,
        },
    )
    assert db.lookups == ["wa-session"]

    crossed = json.loads(await whatsapp_offer["handler"](
        {"idempotency_key": "offer-key-wrong-channel"},
        session_id="ig-session",
    ))
    assert crossed == {
        "status": "review",
        "reason": "adapter scope channel does not match this media tool",
    }


class FakeSourcePlatform:
    def __init__(self, value):
        self.value = value


class FakeInboundSource:
    def __init__(self, platform, chat_id):
        self.platform = FakeSourcePlatform(platform)
        self.chat_id = chat_id
        self.chat_type = "dm"
        self.user_id = "lead-1"
        self.user_id_alt = None
        self.thread_id = None
        self.prospective_thread_id = None
        self.scope_id = None


class FakeAdapterConfig:
    extra = {"group_sessions_per_user": True, "thread_sessions_per_user": False}


class FakeLiveAdapter:
    config = FakeAdapterConfig()

    def __init__(self, account_id):
        self._account_id = account_id
        self._session_store = None


def test_adapter_binding_rejects_missing_or_mismatched_raw_account_id():
    config = deployment_config()
    scopes = VerifiedInboundMediaScopeRegistry(config)
    adapter = FakeLiveAdapter("whatsapp-account")
    source = FakeInboundSource("whatsapp_unipile", "wa-chat")

    with pytest.raises(MediaOverlayError, match="inbound account_id"):
        bind_verified_adapter_inbound_event(
            adapter=adapter, channel="whatsapp", source=source,
            inbound_payload={}, config=config, inbound_scopes=scopes,
        )
    with pytest.raises(MediaOverlayError, match="does not match"):
        bind_verified_adapter_inbound_event(
            adapter=adapter, channel="whatsapp", source=source,
            inbound_payload={"account_id": "other-account"},
            config=config, inbound_scopes=scopes,
        )

    bound = bind_verified_adapter_inbound_event(
        adapter=adapter, channel="whatsapp", source=source,
        inbound_payload={"account_id": "whatsapp-account"},
        config=config, inbound_scopes=scopes,
    )
    assert bound == type(bound)("whatsapp", "whatsapp-account", "wa-chat")
    assert scopes.resolve("agent:main:whatsapp_unipile:dm:wa-chat") == bound
