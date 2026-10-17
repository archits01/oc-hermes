"""Regression coverage for ephemeral channel-context persistence."""

from gateway.run import GatewayRunner


def test_history_stays_in_live_input_but_not_durable_user_text():
    context = "[Exact conversation history]\nLead: We need a projection show"
    live = context + "\n\n[New message]\nCan you share options?"
    assert context in live
    assert GatewayRunner._strip_ephemeral_channel_context(live, context) == "Can you share options?"


def test_attachment_prefix_survives_context_strip():
    context = "[Exact conversation history]\nLead: venue is Bengaluru"
    live = "[The user sent an image attachment]\n\n" + context + f"\n\n[New message]\nPlease review it"
    assert GatewayRunner._strip_ephemeral_channel_context(live, context) == (
        "[The user sent an image attachment]\n\nPlease review it"
    )


def test_missing_or_absent_context_is_unchanged():
    assert GatewayRunner._strip_ephemeral_channel_context("hello", None) == "hello"
    assert GatewayRunner._strip_ephemeral_channel_context("hello", "other") == "hello"
