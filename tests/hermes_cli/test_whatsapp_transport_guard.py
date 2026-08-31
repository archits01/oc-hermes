"""The native WhatsApp bridge and Unipile transport must not be co-enabled."""

from hermes_cli.web_server import _whatsapp_transport_conflict


def test_native_whatsapp_is_blocked_when_unipile_is_configured():
    env = {
        "WHATSAPP_UNIPILE_DSN": "api.example.test",
        "WHATSAPP_UNIPILE_API_KEY": "redacted-test-key",
        "WHATSAPP_ACCOUNT_ID": "account-id",
    }
    message = _whatsapp_transport_conflict("whatsapp", True, env)
    assert message is not None
    assert "WhatsApp Unipile" in message


def test_unipile_is_blocked_when_native_whatsapp_is_enabled():
    message = _whatsapp_transport_conflict(
        "whatsapp_unipile", True, {"WHATSAPP_ENABLED": "true"}
    )
    assert message is not None
    assert "native WhatsApp" in message


def test_separate_transports_are_not_blocked_when_only_one_is_selected():
    assert _whatsapp_transport_conflict("whatsapp", True, {}) is None
    assert _whatsapp_transport_conflict(
        "whatsapp_unipile",
        True,
        {
            "WHATSAPP_UNIPILE_DSN": "api.example.test",
            "WHATSAPP_UNIPILE_API_KEY": "redacted-test-key",
            "WHATSAPP_ACCOUNT_ID": "account-id",
        },
    ) is None
    assert _whatsapp_transport_conflict("whatsapp", False, {}) is None
