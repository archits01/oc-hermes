from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from plugins.platforms import _unipile_common as common


def _db(tmp_path, *, consent: bool = False, state: str | None = None) -> str:
    path = tmp_path / "crm.sqlite"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE media_channel_consent(account_id TEXT, chat_id TEXT, revoked_at TEXT, granted_at TEXT)"
    )
    con.execute(
        "CREATE TABLE media_followup_outbox(account_id TEXT, chat_id TEXT, state TEXT, updated_at TEXT)"
    )
    now = datetime.now(timezone.utc).isoformat()
    if consent:
        con.execute(
            "INSERT INTO media_channel_consent VALUES(?,?,NULL,?)",
            ("acct", "chat", now),
        )
    if state:
        con.execute(
            "INSERT INTO media_followup_outbox VALUES(?,?,?,?)",
            ("acct", "chat", state, now),
        )
    con.commit()
    con.close()
    return str(path)


def test_active_consent_refuses_media_failure_claim(tmp_path, monkeypatch):
    monkeypatch.setenv("LMI_CRM_DB", _db(tmp_path, consent=True))
    with pytest.raises(common.InternalTextRefused):
        common.refuse_stale_media_status_claim(
            account_id="acct", chat_id="chat",
            text="I can’t deliver the approved photos and videos yet because verification is pending.",
            where="test",
        )


def test_pending_outbox_refuses_media_failure_claim(tmp_path, monkeypatch):
    monkeypatch.setenv("LMI_CRM_DB", _db(tmp_path, state="processing"))
    with pytest.raises(common.InternalTextRefused):
        common.refuse_stale_media_status_claim(
            account_id="acct", chat_id="chat",
            text="The media is blocked right now.", where="test",
        )


def test_blocked_without_active_outbox_is_allowed(tmp_path, monkeypatch):
    monkeypatch.setenv("LMI_CRM_DB", _db(tmp_path, state="blocked"))
    common.refuse_stale_media_status_claim(
        account_id="acct", chat_id="chat",
        text="The approved media is blocked right now.", where="test",
    )


def test_non_media_failure_text_is_allowed(tmp_path, monkeypatch):
    monkeypatch.setenv("LMI_CRM_DB", _db(tmp_path, consent=True))
    common.refuse_stale_media_status_claim(
        account_id="acct", chat_id="chat",
        text="I can’t share pricing without the event details.", where="test",
    )
