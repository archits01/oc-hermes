"""Dashboard status must reflect the live Unipile connector without leaking secrets."""

from hermes_cli.web_server import (
    _build_catalog_entry,
    _messaging_platform_payload,
    _whatsapp_transport_conflict,
)


def test_unipile_card_is_explicitly_named_and_separate_from_native_bridge():
    entry = _build_catalog_entry("whatsapp_unipile")
    assert entry["name"] == "WhatsApp (Unipile)"
    assert "separate" in entry["description"]
    assert "WHATSAPP_UNIPILE_API_KEY" in entry["required_env"]


def test_connected_runtime_marks_unipile_configured_without_secret_values(monkeypatch):
    entry = _build_catalog_entry("whatsapp_unipile")
    runtime = {
        "gateway_state": "running",
        "platforms": {"whatsapp_unipile": {"state": "connected"}},
    }
    monkeypatch.setattr(
        "hermes_cli.web_server.resolve_gateway_liveness",
        lambda **_: type("Liveness", (), {"running": True})(),
    )
    monkeypatch.setattr(
        "hermes_cli.web_server._gateway_platform_config",
        lambda _platform_id: (_ for _ in ()).throw(RuntimeError("profile config unavailable")),
    )
    payload = _messaging_platform_payload(entry, {}, runtime)
    assert payload["configured"] is True
    assert payload["state"] == "connected"
    for field in payload["env_vars"]:
        assert field["redacted_value"] is None


def test_native_enable_is_blocked_when_live_unipile_is_connected():
    message = _whatsapp_transport_conflict(
        "whatsapp",
        True,
        {},
        runtime={
            "platforms": {"whatsapp_unipile": {"state": "connected"}},
        },
    )
    assert message is not None
    assert "WhatsApp Unipile" in message
