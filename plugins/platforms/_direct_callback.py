from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable

import httpx
from send_lock import scoped_send_lock

DB_PATH = "/var/lib/lmi-dashboard/unipile_webhooks.db"
UTC = timezone.utc
_PHONE_RE = re.compile(r"[^0-9]")
_PLATFORM_SOURCES = {
    "instagram": "instagram",
    "linkedin": "linkedin",
    "whatsapp": "whatsapp_unipile",
}
_ACCOUNT_ENVS = {
    "instagram": "INSTAGRAM_ACCOUNT_ID",
    "linkedin": "LINKEDIN_ACCOUNT_ID",
    "whatsapp": "WHATSAPP_ACCOUNT_ID",
}


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _digits(value: str) -> str:
    return _PHONE_RE.sub("", str(value or ""))


def normalize_indian_phone(value: str) -> str:
    digits = _digits(value)
    if len(digits) == 10:
        digits = "91" + digits
    if len(digits) < 11:
        return ""
    return "+" + digits


def mask_phone(value: str) -> str:
    digits = _digits(value)
    if len(digits) < 4:
        return "+****"
    return "+" + digits[:2] + "*" * max(0, len(digits) - 6) + digits[-4:]


def has_opt_out(text: str) -> bool:
    clean = str(text or "").lower()
    needles = (
        "stop",
        "unsubscribe",
        "do not call",
        "don't call",
        "dont call",
        "not interested",
        "no call",
        "never call",
    )
    return any(needle in clean for needle in needles)


def has_call_consent(text: str) -> bool:
    clean = str(text or "").lower()
    patterns = (
        r"\bcall\s+me\b",
        r"\bcall\s+now\b",
        r"\bphone\s+me\b",
        r"\byou\s+can\s+call\s+me\b",
        r"\bcan\s+you\s+call\s+me\b",
        r"\bgive\s+me\s+a\s+call\b",
        r"\bi\s+want\s+a\s+call\b",
        r"\bi\s+would\s+like\s+a\s+call\b",
        r"\bpermission\s+to\s+call\b",
    )
    return any(re.search(pattern, clean) for pattern in patterns)


def _chat_messages_has_member_id(con: sqlite3.Connection) -> bool:
    rows = con.execute("PRAGMA table_info(chat_messages)").fetchall()
    return any(str(row[1]) == "member_id" for row in rows)


def extract_recent_inbound_messages(channel: str, account_id: str, chat_id: str, limit: int = 12, *, con: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    owned = con is None
    con = con or sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        select_member_id = ", member_id" if _chat_messages_has_member_id(con) else ", '' as member_id"
        rows = con.execute(
            f"""
            SELECT direction, text, msg_ts{select_member_id}
            FROM chat_messages
            WHERE lower(channel)=? AND account_id=? AND chat_id=?
            ORDER BY msg_ts DESC, id DESC
            LIMIT ?
            """,
            (channel.lower(), account_id, chat_id, max(1, min(limit, 50))),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        if owned:
            con.close()


def verify_consented_callback(
    channel: str,
    account_id: str,
    chat_id: str,
    phone_number: str,
    member_id: str = "",
    *,
    con: sqlite3.Connection | None = None,
    evidence_out: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    normalized_phone = normalize_indian_phone(phone_number)
    if not normalized_phone:
        return False, "invalid_phone"
    owned = con is None
    con = con or sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    rows = extract_recent_inbound_messages(channel, account_id, chat_id, con=con)
    if not rows:
        if owned: con.close()
        return False, "missing_chat_history"
    inbound_rows = [row for row in rows if str(row.get("direction") or "").lower() in {"in", "inbound"}]
    if not inbound_rows:
        if owned: con.close()
        return False, "missing_inbound_history"
    if member_id:
        scoped = [row for row in inbound_rows if str(row.get("member_id") or "") == member_id]
        if scoped:
            inbound_rows = scoped
        try:
            leads = con.execute(
                """SELECT do_not_contact,suppressed,unsubscribed,opted_out,stage,outreach_status
                     FROM leads WHERE member_id=?""",
                (member_id,),
            ).fetchall()
        except sqlite3.Error:
            leads = []
        if len(leads) != 1 or any(bool(leads[0][index]) for index in range(4)) or any(
            str(leads[0][index] or "").strip().lower() == "do_not_contact"
            for index in (4, 5)
        ):
            if owned: con.close()
            return False, "lead_opted_out_or_identity_ambiguous"
    for row in inbound_rows:
        if has_opt_out(str(row.get("text") or "")):
            if owned: con.close()
            return False, "opt_out_present"
    latest_consent = None
    for row in inbound_rows:
        if has_call_consent(str(row.get("text") or "")):
            latest_consent = row
            break
    if latest_consent is None:
        if owned: con.close()
        return False, "missing_call_consent"
    joined = "\n".join(str(row.get("text") or "") for row in inbound_rows)
    if normalize_indian_phone(joined) != normalized_phone and _digits(phone_number) not in _digits(joined):
        if owned: con.close()
        return False, "phone_not_found_in_thread"
    # Keep the row that actually carried consent request-local.  A module-level
    # cache can cross-contaminate simultaneous callbacks from different chats.
    # The optional output preserves the public (bool, str) return contract used
    # by the platform adapters while giving the execution path durable evidence.
    if evidence_out is not None:
        evidence_out.clear()
        evidence_out.update(dict(latest_consent or {}))
    if owned: con.close()
    return True, "ok"


def _callback_enabled() -> bool:
    return (
        os.environ.get("LMI_SARVAM_LIVE_ACTIONS_ENABLED", "").strip() == "1"
        and os.environ.get("LMI_SARVAM_DIRECT_CALLBACK_ENABLED", "").strip() == "1"
    )


def _claim_key(
    channel: str,
    account_id: str,
    chat_id: str,
    phone: str,
    consent_row: dict[str, Any] | None = None,
) -> str:
    consent_ts = str((consent_row or {}).get("msg_ts") or "").strip()
    material = "\x1f".join((channel, account_id, chat_id, phone, consent_ts))
    return hashlib.sha256(material.encode()).hexdigest()


def _ensure_claims(con: sqlite3.Connection) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS adapter_callback_claims(
        claim_key TEXT PRIMARY KEY,channel TEXT NOT NULL,account_id TEXT NOT NULL,
        chat_id TEXT NOT NULL,member_id TEXT NOT NULL,phone_e164 TEXT NOT NULL,
        consent_msg_ts TEXT NOT NULL,state TEXT NOT NULL,attempt_id TEXT,
        detail TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)""")


def record_callback_evidence(*, channel: str, account_id: str, chat_id: str,
                             member_id: str, phone_e164: str, attempt_id: str,
                             outcome: str,
                             consent_row: dict[str, Any] | None = None) -> None:
    """Durably record WHY this callback was allowed: the customer's own words.

    Best-effort by design. An audit write must never block or fail a callback
    the customer explicitly asked for, so every failure is swallowed.
    """
    row = dict(consent_row or {})
    try:
        con = sqlite3.connect(DB_PATH, timeout=30)
        con.execute(
            """CREATE TABLE IF NOT EXISTS adapter_callback_evidence(
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   created_at TEXT NOT NULL,
                   channel TEXT NOT NULL,
                   account_id TEXT NOT NULL,
                   chat_id TEXT NOT NULL,
                   member_id TEXT NOT NULL,
                   phone_e164 TEXT NOT NULL,
                   consent_text TEXT NOT NULL,
                   consent_msg_ts TEXT NOT NULL,
                   opt_out_checked INTEGER NOT NULL,
                   attempt_id TEXT NOT NULL,
                   outcome TEXT NOT NULL,
                   UNIQUE(channel, account_id, chat_id, phone_e164, consent_msg_ts)
               )"""
        )
        con.execute(
            """INSERT OR IGNORE INTO adapter_callback_evidence(
                   created_at, channel, account_id, chat_id, member_id,
                   phone_e164, consent_text, consent_msg_ts, opt_out_checked,
                   attempt_id, outcome)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                _iso_now(), channel, account_id, chat_id, member_id, phone_e164,
                str(row.get("text") or "")[:2000],
                str(row.get("msg_ts") or ""),
                1,                       # verify_consented_callback always scans for opt-out first
                attempt_id, outcome,
            ),
        )
        con.commit()
        con.close()
    except Exception:
        pass


def log_callback_attempt(*, channel: str, chat_id: str, member_id: str, phone_number: str, attempt_id: str, status: str, detail: str = "") -> None:
    try:
        con = sqlite3.connect(DB_PATH)
        con.execute(
            "INSERT INTO escalations (created_at, type, detail, member_id, chat_id, platform, status) VALUES (?,?,?,?,?,?,?)",
            (
                _iso_now(),
                "direct_callback_attempt",
                json.dumps(
                    {
                        "attempt_id": attempt_id,
                        "phone": mask_phone(phone_number),
                        "channel": channel,
                        "detail": detail,
                    },
                    sort_keys=True,
                )[:1000],
                member_id,
                chat_id,
                channel,
                status,
            ),
        )
        con.commit()
        con.close()
    except Exception:
        pass


def build_sarvam_caller(
    *,
    platform_source: str,
    required_env: Callable[[], dict[str, str]],
    webhook_url: str = "",
    webhook_secret: str = "",
    outbounds_base_url: str = "https://apps.sarvam.ai/api/outbounds",
    scope_resolver: Callable[[str, str], dict[str, str]] | None = None,
) -> Callable[[dict[str, Any]], str]:
    base_url = (outbounds_base_url or "https://apps.sarvam.ai/api/outbounds").rstrip("/")

    def resolve_scope(session_id: str, channel: str) -> dict[str, str]:
        if scope_resolver is not None:
            return scope_resolver(session_id, channel)
        if channel not in _PLATFORM_SOURCES:
            raise ValueError("callback channel is unsupported")
        state_path = os.path.join(
            os.environ.get("HERMES_HOME", "/opt/opencomputer-v2-data"), "state.db"
        )
        con = sqlite3.connect(state_path)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                """SELECT source,user_id,chat_id FROM sessions
                     WHERE id=? OR session_key=? LIMIT 2""",
                (session_id, session_id),
            ).fetchall()
        finally:
            con.close()
        if len(rows) != 1 or str(rows[0]["source"] or "") != _PLATFORM_SOURCES[channel]:
            raise ValueError("callback session does not match this platform")
        account_id = os.environ.get(_ACCOUNT_ENVS[channel], "").strip()
        member_id = str(rows[0]["user_id"] or "").strip()
        chat_id = str(rows[0]["chat_id"] or "").strip()
        if not account_id or not member_id or not chat_id:
            raise ValueError("callback session scope is incomplete")
        return {
            "channel": channel,
            "account_id": account_id,
            "chat_id": chat_id,
            "member_id": member_id,
        }

    def _call(args: dict[str, Any] | None = None, **kwargs) -> str:
        if not _callback_enabled():
            return json.dumps({"status": "blocked", "message": "direct callbacks are disabled"})
        # Tool runtimes pass the canonical session id out-of-band. Customer
        # scope is resolved from that row, never trusted from model arguments.
        session_id = str(kwargs.pop("session_id", "") or "").strip()
        if args is None:
            args = kwargs
        elif kwargs:
            merged = dict(args)
            for key, value in kwargs.items():
                if key not in merged and key not in {"task_id"}:
                    merged[key] = value
            args = merged
        phone_number = str(args.get("phone_number") or "").strip()
        recipient_name = str(args.get("recipient_name") or "").strip()
        context = str(args.get("context") or "").strip()
        try:
            canonical = resolve_scope(session_id, platform_source)
        except Exception:
            return json.dumps({"status": "blocked", "message": "callback session scope is unavailable"})
        supplied = {
            "channel": str(args.get("channel") or "").strip().lower(),
            "account_id": str(args.get("account_id") or "").strip(),
            "chat_id": str(args.get("chat_id") or "").strip(),
            "member_id": str(args.get("member_id") or "").strip(),
        }
        if any(supplied[name] and supplied[name] != canonical[name] for name in supplied):
            return json.dumps({"status": "blocked", "message": "model callback scope mismatch"})
        channel = canonical["channel"]
        account_id = canonical["account_id"]
        chat_id = canonical["chat_id"]
        member_id = canonical["member_id"]
        normalized_phone = normalize_indian_phone(phone_number)
        if not normalized_phone:
            return json.dumps({"status": "error", "message": "Provide a valid phone_number"})
        required = required_env()
        missing = [name for name, value in required.items() if not value]
        if missing:
            return json.dumps({"status": "error", "message": "missing configuration", "missing": missing})
        try:
            app_version = int(required["SARVAM_APP_VERSION"])
        except ValueError:
            return json.dumps({"status": "error", "message": "SARVAM_APP_VERSION must be an integer"})
        app_config: dict[str, Any] = {
            "app_id": required["SARVAM_APP_ID"],
            "app_version": app_version,
            "app_type": "agent",
            "connection_config": {
                "connection_id": required["SARVAM_CONNECTION_ID"],
                "agent_phone_number": required["SARVAM_AGENT_PHONE_NUMBER"],
            },
        }
        # Sarvam Conversatio v6 rejects arbitrary ``agent_variables`` on the
        # outbound endpoint with HTTP 422.  The exact callback scope is already
        # claimed in the local database and mirrored in webhook metadata below;
        # do not put provider-unknown fields into ``app_config``.
        payload: dict[str, Any] = {
            "app_config": app_config,
            "user_config": {"user_phone_number": normalized_phone},
        }
        if webhook_url:
            webhook_target = webhook_url
            if webhook_secret:
                separator = "&" if "?" in webhook_target else "?"
                webhook_target = f"{webhook_target}{separator}secret={webhook_secret}"
            separator = "&" if "?" in webhook_url else "?"
            payload["webhook_config"] = {
                "url": webhook_target,
                "metadata": {
                    "source": platform_source,
                    "user_phone_number": normalized_phone,
                    "recipient_name": recipient_name,
                    "member_id": member_id,
                    "chat_id": chat_id,
                    "account_id": account_id,
                },
            }
        with scoped_send_lock(channel, account_id, chat_id):
            con = sqlite3.connect(DB_PATH, timeout=30)
            con.row_factory = sqlite3.Row
            try:
                con.execute("PRAGMA busy_timeout=30000")
                con.execute("BEGIN IMMEDIATE")
                consent_evidence: dict[str, Any] = {}
                ok, reason = verify_consented_callback(
                    channel, account_id, chat_id, normalized_phone, member_id,
                    con=con, evidence_out=consent_evidence,
                )
                if not ok:
                    con.rollback()
                    return json.dumps({"status": "blocked", "message": reason})
                _ensure_claims(con)
                claim_key = _claim_key(
                    channel, account_id, chat_id, normalized_phone, consent_evidence
                )
                prior = con.execute(
                    "SELECT state,attempt_id FROM adapter_callback_claims WHERE claim_key=?",
                    (claim_key,),
                ).fetchone()
                if prior:
                    con.rollback()
                    return json.dumps({
                        "status": str(prior[0]),
                        "attempt_id": str(prior[1] or ""),
                        "message": "callback request is terminal and cannot be retried",
                    })
                now = _iso_now()
                con.execute(
                    "INSERT INTO adapter_callback_claims VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (claim_key, channel, account_id, chat_id, member_id,
                     normalized_phone, str(consent_evidence.get("msg_ts") or ""),
                     "sending", None, None, now, now),
                )
                # Keep the CRM write transaction through the provider boundary.
                # Inbound opt-out writers cannot land after this final check.
                with httpx.Client(timeout=30.0) as client:
                    response = client.post(
                        f"{base_url}/v1/orgs/{required['SARVAM_ORG_ID']}/workspaces/{required['SARVAM_WORKSPACE_ID']}/outbounds",
                        headers={
                            "X-API-Key": required["SARVAM_API_KEY"],
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    )
                if response.is_error:
                    con.execute(
                        "UPDATE adapter_callback_claims SET state='blocked',detail=?,updated_at=? WHERE claim_key=?",
                        (f"provider_http_{response.status_code}", _iso_now(), claim_key),
                    )
                    con.commit()
                    return json.dumps({"status": "blocked", "message": f"HTTP {response.status_code}"})
                result = response.json()
                attempt_id = str(result.get("attempt_id") or "").strip()
                if not attempt_id:
                    con.execute(
                        "UPDATE adapter_callback_claims SET state='unknown',detail='provider_attempt_id_missing',updated_at=? WHERE claim_key=?",
                        (_iso_now(), claim_key),
                    )
                    con.commit()
                    return json.dumps({"status": "unknown", "message": "provider outcome is unknown and will not be retried"})
                con.execute(
                    "UPDATE adapter_callback_claims SET state='queued',attempt_id=?,updated_at=? WHERE claim_key=?",
                    (attempt_id, _iso_now(), claim_key),
                )
                con.commit()
                log_callback_attempt(
                    channel=channel,
                    chat_id=chat_id,
                    member_id=member_id,
                    phone_number=normalized_phone,
                    attempt_id=attempt_id,
                    status="open",
                    detail="queued_direct_callback",
                )
                record_callback_evidence(
                    channel=channel, account_id=account_id, chat_id=chat_id,
                    member_id=member_id, phone_e164=normalized_phone,
                    attempt_id=attempt_id, outcome="queued",
                    consent_row=consent_evidence,
                )
                return json.dumps({
                    "status": "queued",
                    "attempt_id": attempt_id,
                    "execution_id": attempt_id,
                    "phone_masked": mask_phone(normalized_phone),
                })
            except Exception as exc:
                try:
                    if 'claim_key' in locals():
                        con.execute(
                            "UPDATE adapter_callback_claims SET state='unknown',detail='provider_outcome_unknown',updated_at=? WHERE claim_key=?",
                            (_iso_now(), claim_key),
                        )
                        con.commit()
                    else:
                        con.rollback()
                except Exception:
                    con.rollback()
                finally:
                    con.close()
                log_callback_attempt(
                    channel=channel, chat_id=chat_id, member_id=member_id,
                    phone_number=normalized_phone, attempt_id="", status="unknown",
                    detail="direct_callback_provider_outcome_unknown",
                )
                return json.dumps({"status": "unknown", "message": "provider outcome is unknown and will not be retried"})
            finally:
                try: con.close()
                except Exception: pass

    return _call
