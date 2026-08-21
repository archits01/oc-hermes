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


def test_review_manifest_is_disabled_and_portable():
    manifest = (
        Path(__file__).parents[3]
        / "plugins/platforms/lmi_unipile_overlay/manifest.yaml"
    ).read_text(encoding="utf-8")
    assert "enabled: false" in manifest
    assert "provider_calls: prohibited" in manifest
    assert "/Users/" not in manifest
    assert "ca3f3c0aec6b90b134c200acfd4dbb3a8382d669" in manifest
    assert "f4c5e79114e315e4247c28041a6adfcb96820b8ffd8ffa7316406ff19a116f42" in manifest


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
