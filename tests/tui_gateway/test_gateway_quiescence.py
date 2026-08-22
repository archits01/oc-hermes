"""Content-free restart-preflight aggregation for the TUI gateway."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from tui_gateway import server


@pytest.fixture()
def gateway_state(monkeypatch):
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_pending", {})

    import tools.approval as approval
    import tools.mcp_tool as mcp_tool
    import tools.process_registry as process_registry_module
    from tui_gateway import entry, mcp_oauth_sessions

    monkeypatch.setattr(approval, "get_pending_gateway_approval", lambda _key: None)
    monkeypatch.setattr(mcp_tool, "get_mcp_status", lambda: [])
    monkeypatch.setattr(mcp_oauth_sessions, "active_flow_count", lambda: 0)
    monkeypatch.setattr(entry, "mcp_discovery_in_flight", lambda: False)
    monkeypatch.setattr(
        process_registry_module,
        "process_registry",
        SimpleNamespace(
            count_running_by_session_keys=lambda _keys: {"owned": 0, "unowned": 0}
        ),
    )
    return server


def _call(srv):
    return srv._methods["gateway.quiescence"]("quiescence", {})["result"]


def test_gateway_quiescence_is_aggregate_only_and_blocks_transient_work(
    gateway_state, monkeypatch
):
    srv = gateway_state
    worker_lock = threading.Lock()
    worker_lock.acquire()
    worker = SimpleNamespace(
        proc=SimpleNamespace(poll=lambda: None),
        _lock=worker_lock,
    )
    try:
        srv._sessions.update({
            "live": {
                "session_key": "live-key",
                "history_lock": threading.Lock(),
                "running": True,
                "inflight_turn": {"streaming": True},
                "attached_images": ["private-path-a", "private-path-b"],
                "queued_prompt": {
                    "text": "private prompt",
                    "image_paths": ["private-path-c"],
                },
                "queued_prompts": [{"text": "another private prompt"}],
                "slash_worker": worker,
            },
            "idle": {
                "session_key": "idle-key",
                "history_lock": threading.Lock(),
                "running": False,
                "attached_images": [],
                "slash_worker": None,
            },
            "finished": {
                "session_key": "finished-key",
                "history_lock": threading.Lock(),
                "_finalized": True,
            },
        })
        srv._pending["clarify-request"] = ("live", threading.Event())

        import tools.approval as approval
        import tools.mcp_tool as mcp_tool
        import tools.process_registry as process_registry_module
        from tui_gateway import entry, mcp_oauth_sessions

        monkeypatch.setattr(
            approval,
            "get_pending_gateway_approval",
            lambda key: {"present": True} if key == "live-key" else None,
        )
        monkeypatch.setattr(
            process_registry_module,
            "process_registry",
            SimpleNamespace(
                count_running_by_session_keys=lambda keys: (
                    {"owned": 1, "unowned": 1}
                    if keys == {"live-key", "idle-key"}
                    else {"owned": 0, "unowned": 2}
                )
            ),
        )
        monkeypatch.setattr(
            mcp_tool,
            "get_mcp_status",
            lambda: [
                {"status": "connected"},
                {"status": "connecting"},
                {"status": "failed"},
            ],
        )
        monkeypatch.setattr(mcp_oauth_sessions, "active_flow_count", lambda: 1)
        monkeypatch.setattr(entry, "mcp_discovery_in_flight", lambda: True)

        result = _call(srv)
    finally:
        worker_lock.release()

    assert result["scope"] == "this_gateway_process_only"
    assert result["restart_readiness"] == "blocked"
    assert result["sessions"] == {
        "live": 2,
        "inflight_turns": 1,
        "queued_prompts": 2,
        "staged_image_or_pdf_attachments": 3,
        "pending_input_requests": 1,
        "pending_approvals": 1,
    }
    assert result["ownership"] == {
        "live_slash_workers": 1,
        "busy_slash_workers": 1,
        "managed_background_processes": 1,
        "unmanaged_background_processes": 1,
        "mcp_connected": 1,
        "mcp_connecting": 1,
        "mcp_unready": 1,
        "mcp_oauth_pending": 1,
        "mcp_discovery_in_flight": True,
    }
    assert result["observation_errors"] == 0
    assert set(result["restart_blockers"]) == {
        "live_sessions",
        "inflight_turns",
        "queued_prompts",
        "staged_attachments",
        "pending_input_requests",
        "pending_approvals",
        "busy_slash_workers",
        "managed_background_processes",
        "pending_mcp_oauth",
        "mcp_discovery_in_flight",
    }
    assert "live-key" not in repr(result)
    assert "private" not in repr(result)


def test_gateway_quiescence_is_ready_only_when_no_live_state_exists(gateway_state):
    result = _call(gateway_state)

    assert result["restart_readiness"] == "ready"
    assert result["restart_blockers"] == []
    assert result["sessions"]["live"] == 0
    assert result["observation_errors"] == 0


def test_gateway_quiescence_blocks_on_unobservable_approval_state(
    gateway_state, monkeypatch
):
    srv = gateway_state
    srv._sessions["live"] = {
        "session_key": "live-key",
        "history_lock": threading.Lock(),
    }

    import tools.approval as approval

    def unavailable(_key):
        raise RuntimeError("approval backend unavailable")

    monkeypatch.setattr(approval, "get_pending_gateway_approval", unavailable)

    result = _call(srv)

    assert result["restart_readiness"] == "blocked"
    assert result["observation_errors"] == 1
    assert "incomplete_observation" in result["restart_blockers"]


def test_mcp_oauth_active_flow_count_uses_no_identity_output(monkeypatch):
    from tui_gateway import mcp_oauth_sessions

    pending = SimpleNamespace(worker_done=False)
    complete = SimpleNamespace(worker_done=True)
    monkeypatch.setattr(
        mcp_oauth_sessions,
        "_sessions",
        {
            "private-session": {"server_name": "private-server", "flow": pending},
            "complete-session": {"server_name": "another-server", "flow": complete},
        },
    )

    assert mcp_oauth_sessions.active_flow_count() == 1
