"""Fail-closed owner-confirmed meeting handoff tools for LMI messaging.

The active path sends Vaibhav a WhatsApp handoff for manual booking.  The
legacy Calendar client below remains import-compatible but is not registered.
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from send_lock import scoped_send_lock
import meeting_schedule
import meeting_reminder
import meeting_handoff
from reply_eligibility import (
    ComplianceStoreError,
    _is_chat_suppressed,
    _is_do_not_contact,
)
import gmail_reconcile
from .bridge import MediaOverlayError, SUPPORTED_CHANNELS

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_BLOCKED_LEAD_STATES = {"do_not_contact", "suppressed", "unsubscribed", "opted_out"}


class BookingError(MediaOverlayError):
    """A condition that must stop automation before a customer-facing action."""


class OwnerHandoffService:
    """Exact-chat meeting request handoff; never calls Calendar."""

    def __init__(
        self,
        *,
        lead_db_path: str,
        scope_resolver: Callable[[str], Mapping[str, Any]],
        history_reader: Callable[..., list[str]],
    ) -> None:
        self.lead_db_path = str(Path(lead_db_path))
        self.scope_resolver = scope_resolver
        self.history_reader = history_reader

    def _scope(self, session_id: str) -> dict[str, str]:
        raw = self.scope_resolver(_text(session_id, "session_id"))
        if not isinstance(raw, Mapping):
            raise BookingError("no durable exact-chat meeting scope exists")
        names = (
            "channel", "account_id", "chat_id", "lead_id",
            "inbound_provider_message_id", "inbound_text",
        )
        scope = {name: _text(raw.get(name), name) for name in names}
        scope["channel"] = scope["channel"].lower()
        if scope["channel"] not in SUPPORTED_CHANNELS:
            raise BookingError("meeting channel is not configured")
        return scope

    def request(self, session_id: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        scope = self._scope(session_id)
        missing = next((name for name in ("date", "time", "timezone", "email")
                        if not str(arguments.get(name) or "").strip()), None)
        if missing:
            return {"status": f"needs_{missing}"}
        customer_email = gmail_reconcile.normalize_email(arguments.get("email"))
        if not customer_email:
            return {"status": "needs_email_confirmation"}
        try:
            import calendar_sync

            history = list(self.history_reader(
                channel=scope["channel"], account_id=scope["account_id"],
                chat_id=scope["chat_id"],
            ) or [])
            # The live adapter can invoke this tool before the current inbound
            # row has been mirrored into ``chat_messages``.  Add the exact
            # scoped current turn so a valid request is not lost to that race.
            current_text = scope.get("inbound_text", "")
            if current_text and (not history or history[-1] != current_text):
                history.append(current_text)
            evidence_lines = [str(item).strip() for item in history if str(item).strip()]
            # Keep authorization bounded to the recent customer turns, while
            # selecting the newest complete date/time evidence explicitly.  A
            # whole-transcript parser can otherwise pick an old date embedded
            # in a prior QA marker or rejected proposal.
            evidence_lines = evidence_lines[-8:]
            evidence = "\n".join(evidence_lines)
            if customer_email not in evidence.lower():
                return {"status": "needs_email_confirmation"}
            requested = calendar_sync.agreed_start_utc_iso(
                f"{arguments['date']} at {arguments['time']} {arguments['timezone']}"
            )
            proven = next(
                (
                    parsed
                    for line in reversed(evidence_lines)
                    for parsed in (calendar_sync.agreed_start_utc_iso(line),)
                    if parsed
                ),
                None,
            )
            # Reuse the canonical authorization evaluator.  The previous
            # duplicate check called ``has_meeting_consent`` over the entire
            # transcript, so an earlier self-test such as "do not create an
            # event" vetoed a later explicit booking forever.  The canonical
            # evaluator treats the latest customer decision as authoritative
            # while still requiring meeting, invite, exact-time, and email
            # evidence before a Calendar side effect.
            authorization = calendar_sync.meeting_authorization(
                {"email": customer_email}, evidence
            )
            if authorization.get("status") == "customer_declined":
                return {"status": "customer_declined"}
            if not authorization.get("meeting_consent"):
                return {"status": "needs_confirmation"}
            if not authorization.get("invite_consent"):
                return {"status": "needs_invite_consent"}
            if not requested or requested != proven:
                return {"status": "needs_date_time_timezone"}
            zone = calendar_sync._explicit_timezone(str(arguments["timezone"]))
            if not zone:
                return {"status": "needs_timezone"}
        except Exception as exc:
            raise BookingError("exact chat meeting evidence is unavailable") from exc
        con = sqlite3.connect(self.lead_db_path, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute("SELECT * FROM leads WHERE member_id=?", (scope["lead_id"],)).fetchall()
            if len(rows) != 1:
                raise BookingError("exact lead identity is ambiguous")
            lead = dict(rows[0])
            if any(bool(lead.get(name)) for name in (
                "do_not_contact", "suppressed", "unsubscribed", "opted_out"
            )) or str(lead.get("stage") or "").lower() == "do_not_contact" or str(
                lead.get("outreach_status") or ""
            ).lower() == "do_not_contact":
                raise BookingError("do_not_contact")
            result = meeting_handoff.create_request(
                con,
                source_kind="messaging",
                source_id=scope["inbound_provider_message_id"],
                member_id=scope["lead_id"],
                customer_channel=scope["channel"],
                customer_account_id=scope["account_id"],
                customer_chat_id=scope["chat_id"],
                requested_start_at=requested,
                timezone_name=str(zone[0]),
                brief=meeting_handoff.conversation_brief(history),
                customer_name=" ".join(filter(None, [
                    str(lead.get("first_name") or "").strip(),
                    str(lead.get("last_name") or "").strip(),
                ])).strip(),
                customer_email=customer_email,
                customer_email_confirmed=True,
                customer_phone=str(lead.get("phone") or lead.get("whatsapp_phone") or ""),
                # Feed the exact inbound-only evidence into the canonical
                # Calendar authorization path.  Omitting this argument left
                # every live tool request with an empty evidence string, so
                # it could only fall back to owner_notified even when the
                # customer had explicitly consented and Calendar was enabled.
                calendar_evidence=evidence,
            )
            return result
        finally:
            con.close()


def register_owner_handoff_tools(
    ctx: Any, service: OwnerHandoffService, *, channel: str
) -> tuple[str]:
    if channel not in SUPPORTED_CHANNELS or not callable(getattr(ctx, "register_tool", None)):
        raise BookingError("meeting handoff tool registration is unavailable")
    name = f"{channel}_request_owner_confirmed_meeting"

    def handler(arguments: Mapping[str, Any], **kwargs: Any) -> str:
        if any(key in arguments for key in ("channel", "account_id", "chat_id", "lead_id")):
            return json.dumps({"status": "blocked", "reason": "model may not override meeting scope"})
        try:
            return json.dumps(
                service.request(str(kwargs.get("session_id") or ""), arguments),
                separators=(",", ":"), sort_keys=True,
            )
        except BookingError as exc:
            return json.dumps({"status": "blocked", "reason": str(exc)}, separators=(",", ":"))

    ctx.register_tool(
        name=name,
        toolset=f"{channel}-tools",
        schema={
            "name": name,
            "description": (
                "Persist the customer-requested time and verified email, send Vaibhav the exact "
                "conversation brief for manual booking, then wait for a real Meet URL. After the "
                "owner supplies the URL, email it to the customer and schedule a ten-minute chat reminder."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string"},
                    "time": {"type": "string"},
                    "timezone": {"type": "string"},
                    "email": {"type": "string"},
                },
                "required": ["date", "time", "timezone", "email"],
                "additionalProperties": False,
            },
        },
        handler=handler,
    )
    return (name,)


def _text(value: Any, name: str) -> str:
    value = str(value or "").strip()
    if not value:
        raise BookingError(f"{name} is required")
    return value


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class LiveBookingService:
    """Durable prepare/confirm flow; ambiguous provider outcomes are terminal."""

    def __init__(
        self,
        *,
        db_path: str,
        lead_db_path: str,
        calendar_create: Callable[..., Mapping[str, Any]],
        calendar_persist: Callable[..., Any],
        send_text: Callable[..., str | None],
        scope_resolver: Callable[[str], Mapping[str, Any]],
        history_reader: Callable[..., list[str]],
    ) -> None:
        self.db_path, self.lead_db_path = str(Path(db_path)), str(Path(lead_db_path))
        self.calendar_create, self.calendar_persist = calendar_create, calendar_persist
        self.send_text, self.scope_resolver, self.history_reader = (
            send_text,
            scope_resolver,
            history_reader,
        )
        self._ensure()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        return con

    def _ensure(self) -> None:
        con = self._connect()
        try:
            con.execute("""CREATE TABLE IF NOT EXISTS live_booking_actions(
                action_id TEXT PRIMARY KEY, channel TEXT NOT NULL, account_id TEXT NOT NULL,
                chat_id TEXT NOT NULL, lead_id TEXT NOT NULL, email TEXT NOT NULL,
                evidence TEXT NOT NULL, prepared_provider_message_id TEXT NOT NULL,
                latest_inbound_message_id TEXT NOT NULL, state TEXT NOT NULL,
                expires_at TEXT NOT NULL, confirm_message_id TEXT, calendar_event_id TEXT,
                meeting_url TEXT, link_provider_message_id TEXT, delivery_state TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )""")
            con.execute(
                "CREATE INDEX IF NOT EXISTS live_booking_scope ON live_booking_actions(channel,account_id,chat_id,state)"
            )
        finally:
            con.close()

    def _scope(self, session_id: str) -> dict[str, str]:
        raw = self.scope_resolver(_text(session_id, "session_id"))
        if not isinstance(raw, Mapping):
            raise BookingError("no durable exact-chat booking scope exists")
        names = (
            "channel",
            "account_id",
            "chat_id",
            "lead_id",
            "inbound_provider_message_id",
            "inbound_text",
        )
        scope = {name: _text(raw.get(name), name) for name in names}
        scope["channel"] = scope["channel"].lower()
        if scope["channel"] not in SUPPORTED_CHANNELS:
            raise BookingError("booking channel is not configured")
        return scope

    def _lead(self, lead_id: str, email: str) -> dict[str, Any]:
        email = _text(email, "invite_email").lower()
        if not _EMAIL.fullmatch(email):
            raise BookingError("needs_email")
        con = sqlite3.connect(self.lead_db_path, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                "SELECT member_id,first_name,last_name,company,platform,email,stage,outreach_status,do_not_contact,suppressed,unsubscribed,opted_out FROM leads WHERE member_id=?",
                (lead_id,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise BookingError("exact lead identity is unavailable") from exc
        finally:
            con.close()
        if len(rows) != 1:
            raise BookingError("exact lead identity is ambiguous")
        lead = dict(rows[0])
        if {
            str(lead.get("stage") or "").lower(),
            str(lead.get("outreach_status") or "").lower(),
        } & _BLOCKED_LEAD_STATES:
            raise BookingError("do_not_contact")
        if any(bool(lead.get(name)) for name in ("do_not_contact", "suppressed", "unsubscribed", "opted_out")):
            raise BookingError("do_not_contact")
        lead["email"] = email
        return lead

    def _fresh_scope_lead(
        self, scope: Mapping[str, str], email: str
    ) -> dict[str, Any]:
        """Recheck both durable lead and exact-chat suppression at send time."""
        con = sqlite3.connect(self.lead_db_path, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute(
                "SELECT member_id,first_name,last_name,company,platform,email,stage,outreach_status,do_not_contact,suppressed,unsubscribed,opted_out FROM leads WHERE member_id=?",
                (scope["lead_id"],),
            ).fetchall()
            if len(rows) != 1:
                raise BookingError("exact lead identity is ambiguous")
            lead = dict(rows[0])
            if {
                str(lead.get("stage") or "").lower(),
                str(lead.get("outreach_status") or "").lower(),
            } & _BLOCKED_LEAD_STATES or any(
                bool(lead.get(name))
                for name in ("do_not_contact", "suppressed", "unsubscribed", "opted_out")
            ):
                raise BookingError("do_not_contact")
            if _is_chat_suppressed(
                con, scope["channel"], scope["chat_id"], scope["account_id"]
            ) or _is_do_not_contact(
                con, scope["channel"], scope["chat_id"], scope["account_id"]
            ):
                raise BookingError("do_not_contact")
            lead["email"] = str(email).strip().lower()
            # Return the open transaction to the caller so the exact CRM/DNC
            # snapshot remains write-locked through Calendar dispatch.
            return {"lead": lead, "connection": con}
        except BookingError:
            con.rollback()
            con.close()
            raise
        except (sqlite3.Error, ComplianceStoreError, ValueError) as exc:
            con.rollback()
            con.close()
            raise BookingError("compliance_recheck_failed") from exc
        except Exception:
            con.rollback()
            con.close()
            raise

    @staticmethod
    def _missing_proof(
        calendar_sync: Any, lead: Mapping[str, Any], evidence: str
    ) -> str | None:
        auth = calendar_sync.meeting_authorization(lead, evidence)
        if auth.get("ok"):
            return None
        status = str(auth.get("status") or "")
        if status == "needs_exact_customer_email":
            return "email"
        if status == "needs_exact_date_time_timezone":
            if not calendar_sync._explicit_calendar_date(evidence):
                return "date"
            if not calendar_sync.parse_meeting_when(evidence):
                return "time"
            if not calendar_sync._explicit_timezone(evidence):
                return "timezone"
            return "date"
        return "confirmation"

    def prepare(self, session_id: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        scope = self._scope(session_id)
        email = str(arguments.get("invite_email") or "").strip().lower()
        missing = next(
            (
                name
                for name, value in (
                    ("email", email),
                    ("date", arguments.get("date")),
                    ("time", arguments.get("time")),
                    ("timezone", arguments.get("timezone")),
                )
                if not str(value or "").strip()
            ),
            None,
        )
        if missing:
            return {"status": f"needs_{missing}"}
        lead = self._lead(scope["lead_id"], email)
        try:
            import calendar_sync

            evidence = "\n".join(
                self.history_reader(
                    channel=scope["channel"],
                    account_id=scope["account_id"],
                    chat_id=scope["chat_id"],
                )
            )
            missing_proof = self._missing_proof(calendar_sync, lead, evidence)
            authorization = calendar_sync.meeting_authorization(lead, evidence)
            requested_start = calendar_sync.agreed_start_utc_iso(
                f"{arguments['date']} at {arguments['time']} {arguments['timezone']}"
            )
        except Exception as exc:
            raise BookingError("exact chat history validation is unavailable") from exc
        if missing_proof:
            return {"status": f"needs_{missing_proof}"}
        # Arguments only select the already-proven appointment. They may never
        # replace a customer-authored date/time/timezone in exact-chat history.
        if requested_start != authorization.get("start_utc_iso"):
            return {"status": "needs_date"}
        action_id = secrets.token_urlsafe(18)
        readback = (
            f"Please confirm: a Google Meet invite to {email} for {arguments['date']} "
            f"at {arguments['time']} {arguments['timezone']}, plus a reminder in this "
            "chat 10 minutes before. Reply yes to confirm."
        )
        now, expires = (
            _iso(),
            (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        )
        # Serialize provider delivery order and durable action order under the
        # same cross-process exact-chat lock used by every other sender.
        with scoped_send_lock(scope["channel"], scope["account_id"], scope["chat_id"]):
            con = self._connect()
            try:
                con.execute("BEGIN IMMEDIATE")
                prior = con.execute(
                    """SELECT action_id FROM live_booking_actions
                         WHERE channel=? AND account_id=? AND chat_id=?
                           AND state IN ('prepared','readback_sending')""",
                    (scope["channel"], scope["account_id"], scope["chat_id"]),
                ).fetchall()
                with sqlite3.connect(self.lead_db_path, timeout=30) as slot_con:
                    for previous in prior:
                        meeting_schedule.transition(slot_con, str(previous[0]), "released")
                    slot = meeting_schedule.reserve(
                        slot_con,
                        reservation_id=action_id,
                        requested_start=requested_start,
                        owner_kind="messaging",
                        owner_key=f"{scope['channel']}:{scope['account_id']}:{scope['chat_id']}",
                        member_id=scope["lead_id"],
                        channel=scope["channel"],
                        account_id=scope["account_id"],
                        chat_id=scope["chat_id"],
                    )
                if not slot.get("ok"):
                    con.rollback()
                    return {
                        "status": "needs_available_slot",
                        "reason": slot.get("status"),
                        "suggested_start_utc": slot.get("suggestions") or [],
                    }
                con.execute(
                    """UPDATE live_booking_actions SET state='superseded',
                              delivery_state='superseded',updated_at=?
                         WHERE channel=? AND account_id=? AND chat_id=? AND state='prepared'""",
                    (now, scope["channel"], scope["account_id"], scope["chat_id"]),
                )
                con.execute(
                    "INSERT INTO live_booking_actions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        action_id, scope["channel"], scope["account_id"], scope["chat_id"],
                        scope["lead_id"], email, evidence, "",
                        scope["inbound_provider_message_id"], "readback_sending", expires,
                        None, None, None, None, "readback_sending", now, now,
                    ),
                )
                con.commit()
            finally:
                con.close()
            try:
                provider_id = str(
                    self.send_text(
                        channel=scope["channel"], account_id=scope["account_id"],
                        chat_id=scope["chat_id"], text=readback,
                    ) or ""
                ).strip()
            except Exception as exc:
                self._mark_prepare_unknown(action_id)
                raise BookingError("prepare_delivery_unknown") from exc
            if not provider_id:
                self._mark_prepare_unknown(action_id)
                raise BookingError("prepare_delivery_unknown")
            con = self._connect()
            try:
                con.execute("BEGIN IMMEDIATE")
                changed = con.execute(
                    """UPDATE live_booking_actions SET prepared_provider_message_id=?,
                              state='prepared',delivery_state='prepare_provider_accepted',updated_at=?
                         WHERE action_id=? AND state='readback_sending'""",
                    (provider_id, _iso(), action_id),
                ).rowcount
                if changed != 1:
                    raise BookingError("prepare action state changed unexpectedly")
                con.commit()
            except Exception:
                con.rollback()
                raise
            finally:
                con.close()
        return {
            "status": "prepared",
            "action_id": action_id,
            "readback_provider_message_id": provider_id,
            "expires_at": expires,
        }

    def _mark_prepare_unknown(self, action_id: str) -> None:
        con = self._connect()
        try:
            con.execute(
                """UPDATE live_booking_actions SET state='readback_unknown',
                          delivery_state='readback_unknown',updated_at=?
                     WHERE action_id=? AND state='readback_sending'""",
                (_iso(), action_id),
            )
            con.commit()
        finally:
            con.close()

    def confirm(self, session_id: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        scope = self._scope(session_id)
        action_id, provider_message_id = (
            _text(arguments.get("action_id"), "action_id"),
            _text(arguments.get("confirmation_message_id"), "confirmation_message_id"),
        )
        if provider_message_id != scope["inbound_provider_message_id"]:
            return {
                "status": "blocked",
                "reason": "confirmation is not the exact latest inbound message",
            }
        try:
            import sarvam_locale

            affirmative = sarvam_locale.has_strict_affirmative(scope["inbound_text"])
        except Exception as exc:
            raise BookingError(
                "localized confirmation validation is unavailable"
            ) from exc
        if not affirmative:
            return {
                "status": "blocked",
                "reason": "confirmation is not a strict affirmative",
            }
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT * FROM live_booking_actions WHERE action_id=?", (action_id,)
            ).fetchone()
            if not row or any(
                str(row[key]) != scope[key]
                for key in ("channel", "account_id", "chat_id", "lead_id")
            ):
                con.rollback()
                return {
                    "status": "blocked",
                    "reason": "booking action is not bound to this chat",
                }
            newest = con.execute(
                """SELECT action_id FROM live_booking_actions
                     WHERE channel=? AND account_id=? AND chat_id=? AND state='prepared'
                     ORDER BY created_at DESC,rowid DESC LIMIT 1""",
                (scope["channel"], scope["account_id"], scope["chat_id"]),
            ).fetchone()
            if newest is None or str(newest[0]) != action_id:
                con.rollback()
                return {"status": "blocked", "reason": "booking action is not the latest read-back"}
            if provider_message_id == str(row["latest_inbound_message_id"]):
                con.rollback()
                return {"status": "blocked", "reason": "fresh confirmation after read-back is required"}
            if row["state"] != "prepared" or row["expires_at"] <= _iso():
                con.execute(
                    "UPDATE live_booking_actions SET state='blocked',updated_at=? WHERE action_id=?",
                    (_iso(), action_id),
                )
                con.commit()
                return {
                    "status": "blocked",
                    "reason": "booking confirmation is stale or already consumed",
                }
            con.execute(
                "UPDATE live_booking_actions SET state='calendar_processing',confirm_message_id=?,latest_inbound_message_id=?,updated_at=? WHERE action_id=?",
                (provider_message_id, provider_message_id, _iso(), action_id),
            )
            con.commit()
        finally:
            con.close()
        # Re-acquire the same exact-chat lock at the final provider boundary.
        # A new opt-out/DNC written after confirmation must win over Calendar.
        with scoped_send_lock(scope["channel"], scope["account_id"], scope["chat_id"]):
            with sqlite3.connect(self.lead_db_path, timeout=30) as slot_con:
                if not meeting_schedule.transition(slot_con, action_id, "confirming"):
                    self._finish(
                        action_id, "blocked",
                        result={"status": "blocked", "reason": "meeting slot expired or changed"},
                    )
                    return {
                        "status": "blocked",
                        "reason": "meeting slot expired or changed; prepare again",
                    }
            try:
                locked = self._fresh_scope_lead(scope, str(row["email"]))
                lead, compliance_con = locked["lead"], locked["connection"]
            except BookingError as exc:
                with sqlite3.connect(self.lead_db_path, timeout=30) as slot_con:
                    meeting_schedule.transition(slot_con, action_id, "released")
                self._finish(
                    action_id,
                    "blocked",
                    result={"status": "blocked", "reason": str(exc)},
                )
                return {"status": "blocked", "reason": str(exc)}
            try:
                result = dict(self.calendar_create(lead, str(row["evidence"])))
            except Exception:
                result = {"status": "unknown", "reason": "provider_outcome_unknown"}
            finally:
                try:
                    compliance_con.commit()
                except sqlite3.Error:
                    result = {"status": "unknown", "reason": "compliance_lock_release_unknown"}
                compliance_con.close()
        if (
            result.get("status") not in {"created", "skipped_duplicate", "reconciled"}
            or not result.get("event_id")
            or not result.get("meeting_url")
        ):
            provider_status = str(result.get("status") or "unknown").lower()
            terminal = "blocked" if provider_status in {"blocked", "unsupported"} else "unknown"
            with sqlite3.connect(self.lead_db_path, timeout=30) as slot_con:
                meeting_schedule.transition(
                    slot_con, action_id,
                    "released" if terminal == "blocked" else "unknown",
                )
            self._finish(action_id, terminal, result=result)
            return {
                "status": terminal,
                "reason": str(result.get("reason") or "calendar_result_not_proven"),
            }
        try:
            import calendar_sync

            start = calendar_sync.agreed_start_utc_iso(str(row["evidence"]))
            with sqlite3.connect(self.lead_db_path, timeout=30) as lead_con:
                self.calendar_persist(lead_con, lead, start, result)
                changed = lead_con.execute(
                    "UPDATE leads SET email=? WHERE member_id=? AND (email IS NULL OR trim(email)='' OR lower(email)=?)",
                    (row["email"], scope["lead_id"], row["email"]),
                ).rowcount
                if changed != 1:
                    raise BookingError("CRM email no longer matches this exact lead")
        except Exception:
            with sqlite3.connect(self.lead_db_path, timeout=30) as slot_con:
                meeting_schedule.transition(slot_con, action_id, "unknown")
            self._finish(action_id, "unknown", result=result)
            return {"status": "unknown", "reason": "calendar_persistence_unknown"}
        with sqlite3.connect(self.lead_db_path, timeout=30) as slot_con:
            meeting_schedule.transition(
                slot_con, action_id, "confirmed",
                event_id=str(result["event_id"]),
                meeting_url=str(result["meeting_url"]),
            )
        try:
            with sqlite3.connect(self.lead_db_path, timeout=30) as reminder_con:
                reminder = meeting_reminder.schedule(
                    reminder_con,
                    member_id=scope["lead_id"],
                    meeting_start_at=start,
                    event_id=str(result["event_id"]),
                    meeting_url=str(result["meeting_url"]),
                    source="messaging",
                    channel=scope["channel"],
                    account_id=scope["account_id"],
                    chat_id=scope["chat_id"],
                    summary=str(row["evidence"]),
                )
            reminder_status = str(reminder.get("status") or "unknown")
        except Exception:
            reminder_status = "unknown"
        try:
            link_id = str(
                self.send_text(
                    channel=scope["channel"],
                    account_id=scope["account_id"],
                    chat_id=scope["chat_id"],
                    text=f"Your Google Meet link: {result['meeting_url']}",
                )
                or ""
            ).strip()
        except Exception:
            link_id = ""
        if not link_id:
            self._finish(action_id, "link_delivery_unknown", result=result)
            return {
                "status": "unknown",
                "reason": "link_delivery_unknown",
                "event_id": result["event_id"],
            }
        self._finish(
            action_id, "completed", result=result, link_provider_message_id=link_id
        )
        return {
            "status": "booked",
            "event_id": result["event_id"],
            "meeting_url": result["meeting_url"],
            "link_provider_message_id": link_id,
            "reminder_status": reminder_status,
        }

    def _finish(
        self,
        action_id: str,
        state: str,
        *,
        result: Mapping[str, Any],
        link_provider_message_id: str | None = None,
    ) -> None:
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "UPDATE live_booking_actions SET state=?,calendar_event_id=?,meeting_url=?,link_provider_message_id=?,delivery_state=?,updated_at=? WHERE action_id=?",
                (
                    state,
                    result.get("event_id"),
                    result.get("meeting_url"),
                    link_provider_message_id,
                    state,
                    _iso(),
                    action_id,
                ),
            )
            con.commit()
        finally:
            con.close()


def register_adapter_booking_tools(
    ctx: Any, service: LiveBookingService, *, channel: str
) -> tuple[str, str]:
    if channel not in SUPPORTED_CHANNELS or not callable(
        getattr(ctx, "register_tool", None)
    ):
        raise BookingError("booking tool registration is unavailable")
    prepare_name, confirm_name = (
        f"{channel}_prepare_google_meet",
        f"{channel}_confirm_google_meet",
    )

    def handler(method: str) -> Callable[..., str]:
        def call(arguments: Mapping[str, Any], **kwargs: Any) -> str:
            if any(
                key in arguments
                for key in ("channel", "account_id", "chat_id", "lead_id")
            ):
                return json.dumps(
                    {
                        "status": "blocked",
                        "reason": "model may not override live booking scope",
                    }
                )
            try:
                return json.dumps(
                    getattr(service, method)(
                        str(kwargs.get("session_id") or ""), arguments
                    ),
                    separators=(",", ":"),
                    sort_keys=True,
                )
            except BookingError as exc:
                return json.dumps(
                    {"status": "blocked", "reason": str(exc)}, separators=(",", ":")
                )

        return call

    ctx.register_tool(
        name=prepare_name,
        toolset=f"{channel}-tools",
        schema={
            "name": prepare_name,
            "description": (
                "Reserve one exact shared 30-minute slot and send the complete exact-chat "
                "Meet plus 10-minute reminder read-back; never creates an event. If the "
                "slot is unavailable, use only suggested_start_utc and ask again."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "invite_email": {"type": "string"},
                    "date": {"type": "string"},
                    "time": {"type": "string"},
                    "timezone": {"type": "string"},
                },
                "required": ["invite_email", "date", "time", "timezone"],
                "additionalProperties": False,
            },
        },
        handler=handler("prepare"),
    )
    ctx.register_tool(
        name=confirm_name,
        toolset=f"{channel}-tools",
        schema={
            "name": confirm_name,
            "description": "After the latest exact affirmative reply, create and deliver one Meet link.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action_id": {"type": "string"},
                    "confirmation_message_id": {"type": "string"},
                },
                "required": ["action_id", "confirmation_message_id"],
                "additionalProperties": False,
            },
        },
        handler=handler("confirm"),
    )
    return prepare_name, confirm_name
