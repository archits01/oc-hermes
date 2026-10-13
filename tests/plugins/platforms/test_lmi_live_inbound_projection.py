from __future__ import annotations

import sys
import types
from pathlib import Path

from plugins.platforms.lmi_unipile_overlay import _lmi_media_runtime as runtime


def test_persist_live_inbound_projection_uses_idempotent_projection(monkeypatch, tmp_path):
    calls = []
    fake = types.ModuleType("reply_sync")

    def persist(connection, payload):
        calls.append(dict(payload))
        return {"status": "persisted", "inserted": True, "inbound_id": 42}

    fake.persist_webhook_inbound = persist
    monkeypatch.setitem(sys.modules, "reply_sync", fake)

    payload = {
        "event": "message_received",
        "account_type": "WHATSAPP",
        "account_id": "acct",
        "chat_id": "chat",
        "message_id": "provider-message",
        "message": "send photos and video",
        "timestamp": "2026-09-12T00:00:00Z",
        "is_sender": False,
    }
    result = runtime.persist_live_inbound_projection(
        payload, db_path=str(tmp_path / "crm.db"), timeout_seconds=1
    )
    assert result == {
        "status": "persisted",
        "inserted": True,
        "inbound_id": 42,
        "reason": None,
    }
    assert calls == [payload]


def test_persist_live_inbound_projection_ignores_outbound(monkeypatch, tmp_path):
    fake = types.ModuleType("reply_sync")
    fake.persist_webhook_inbound = lambda *_args: (_ for _ in ()).throw(
        AssertionError("outbound events must not be projected")
    )
    monkeypatch.setitem(sys.modules, "reply_sync", fake)
    result = runtime.persist_live_inbound_projection(
        {"is_sender": True}, db_path=str(tmp_path / "crm.db")
    )
    assert result == {"status": "ignored", "reason": "outbound_event"}


def test_persist_live_inbound_projection_retries_transient_busy_for_media(monkeypatch, tmp_path):
    fake = types.ModuleType("reply_sync")
    calls = []

    def persist(connection, payload):
        calls.append(dict(payload))
        return {"status": "persisted", "inserted": True}

    fake.persist_webhook_inbound = persist
    monkeypatch.setitem(sys.modules, "reply_sync", fake)
    real_connect = runtime.sqlite3.connect
    connect_calls = []

    def connect(path, **kwargs):
        connect_calls.append(path)
        if len(connect_calls) == 1:
            raise runtime.sqlite3.OperationalError("database is locked")
        return real_connect(path, **kwargs)

    monkeypatch.setattr(runtime.sqlite3, "connect", connect)
    monkeypatch.setattr(runtime.time, "sleep", lambda _seconds: None)
    result = runtime.persist_live_inbound_projection(
        {"is_sender": False}, db_path=str(tmp_path / "crm.db"), timeout_seconds=5
    )
    assert result["status"] == "persisted"
    assert len(connect_calls) == 2
    assert len(calls) == 1


def test_live_adapters_project_before_reserving_reply():
    paths = (
        Path("/opt/opencomputer-v2/plugins/platforms/whatsapp_unipile/adapter.py"),
        Path("/opt/opencomputer-v2/plugins/platforms/instagram/adapter.py"),
        Path("/opt/opencomputer-v2/plugins/platforms/linkedin/adapter.py"),
    )
    for path in paths:
        source = path.read_text()
        projection = source.rindex("persist_live_inbound_projection")
        reservation = source.rindex("await reserve_live_reply(")
        assert projection < reservation, path
