from __future__ import annotations

import sqlite3
import sys
import types


def test_live_outbound_projection_retries_transient_busy(monkeypatch, tmp_path):
    from plugins.platforms import _unipile_common as common

    fake = types.ModuleType("inbox_transcript")
    fake.persist_chat_messages = lambda *args, **kwargs: 1
    monkeypatch.setitem(sys.modules, "inbox_transcript", fake)

    real_connect = common.sqlite3.connect
    connect_calls = []

    def connect(path, **kwargs):
        connect_calls.append(path)
        if len(connect_calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_connect(path, **kwargs)

    monkeypatch.setattr(common.sqlite3, "connect", connect)
    monkeypatch.setattr(common.time, "sleep", lambda _seconds: None)
    result = common.persist_live_outbound_projection(
        channel="whatsapp",
        account_id="account-1",
        chat_id="chat-1",
        text="test",
        provider_result={"provider_message_id": "provider-1", "id": "legacy-1"},
        db_path=str(tmp_path / "crm.db"),
    )

    assert result["status"] == "persisted"
    assert len(connect_calls) == 2
    assert result["provider_message_id_present"] is True
