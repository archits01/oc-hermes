"""Shared, provider-free inbound media-scope seam for LMI adapters.

The deployment bootstrap owns the real binder.  It must configure this single
object once, after it creates the reviewed media overlay's deployment config
and verified inbound-scope registry. The WhatsApp, Instagram, and LinkedIn adapters use
the object only to bind a raw webhook payload to their deterministic Hermes
source before admitting an event to the live-reply queue.

This module deliberately does not read environment variables, construct a
provider client, or install model tools.  Until deployment configures it,
inbound direct-reply admission fails closed.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import sqlite3
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from threading import RLock
from typing import Any

try:
    # In production this is the reviewed overlay error type, so adapter error
    # handling catches exactly the deployment binding failure.
    from plugins.platforms.lmi_unipile_overlay import MediaOverlayError
except ImportError:  # Local source checks must not require the overlay package.
    class MediaOverlayError(RuntimeError):
        """Fail-closed media binding error when the reviewed overlay is absent."""


InboundBinder = Callable[..., Any]


class DeploymentMediaRuntime:
    """One shared, explicitly configured inbound-scope binder.

    A deployment supplies a callable with the same keyword-only contract as
    ``bind_verified_adapter_inbound_event``.  Keeping the callable injectable
    makes the adapter boundary testable without credentials or provider calls.
    """

    def __init__(self) -> None:
        self._binder: InboundBinder | None = None
        self._lock = RLock()

    def configure(self, binder: InboundBinder) -> None:
        if not callable(binder):
            raise TypeError("media runtime binder must be callable")
        with self._lock:
            self._binder = binder

    def clear_for_test(self) -> None:
        """Remove the binder so a test can exercise the fail-closed default."""
        with self._lock:
            self._binder = None

    def bind_inbound(
        self,
        *,
        adapter: Any,
        channel: str,
        source: Any,
        inbound_payload: Mapping[str, Any],
    ) -> Any:
        with self._lock:
            binder = self._binder
        if binder is None:
            raise MediaOverlayError("deployment media runtime is not configured")
        return binder(
            adapter=adapter,
            channel=channel,
            source=source,
            inbound_payload=inbound_payload,
        )


# Adapters import this stable object.  Replacing it would give each adapter a
# stale reference, so deployment configures the object in place instead.
media_runtime = DeploymentMediaRuntime()


def configure_media_runtime(binder: InboundBinder) -> None:
    """Configure the shared deployment binder once from the live bootstrap."""
    media_runtime.configure(binder)


def _load_reply_sync_module() -> Any:
    """Load the existing CRM webhook projector without provider access.

    The gateway and the lead-generation jobs run from different source roots.
    Prefer an already-installed ``reply_sync`` module, then load the canonical
    lead-generation source explicitly.  This helper is intentionally limited
    to the projector function; it does not start the polling worker.
    """
    try:
        return importlib.import_module("reply_sync")
    except Exception:
        source = Path("/root/leadgen/reply_sync.py")
        if not source.is_file():
            raise
        module_name = "_lmi_live_reply_sync"
        module = sys.modules.get(module_name)
        if module is not None:
            return module
        spec = importlib.util.spec_from_file_location(module_name, source)
        if spec is None or spec.loader is None:
            raise ImportError("reply_sync projector is unavailable")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(module_name, None)
            raise
        return module


def persist_live_inbound_projection(
    payload: Mapping[str, Any] | Any,
    *,
    db_path: str | None = None,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Project one inbound webhook before the live model can use media tools.

    The media bridge requires the exact provider message id to already exist in
    ``inbound_messages``.  Webhook projection normally runs asynchronously, so
    a fast live turn could reach ``*_send_approved_media`` first and be refused
    even though the inbound was valid.  This bounded, idempotent write closes
    that race.  Outbound echoes are ignored, and failures return safe status
    values so adapters can fail closed only for explicit media requests.
    """
    if not isinstance(payload, Mapping):
        return {"status": "ignored", "reason": "payload_not_object"}

    sender = payload.get("is_sender")
    if sender in (1, True, "1", "true", "TRUE", "yes", "YES"):
        return {"status": "ignored", "reason": "outbound_event"}

    target = str(
        db_path or os.environ.get("LMI_CRM_DB")
        or "/var/lib/lmi-dashboard/unipile_webhooks.db"
    ).strip()
    if not target or not Path(target).is_absolute():
        return {"status": "blocked", "reason": "crm_db_path_not_absolute"}
    try:
        timeout = float(timeout_seconds if timeout_seconds is not None else 5.0)
    except (TypeError, ValueError):
        timeout = 5.0
    timeout = max(1.0, min(10.0, timeout))

    con = None
    try:
        projector = _load_reply_sync_module()
        persist = getattr(projector, "persist_webhook_inbound", None)
        if not callable(persist):
            return {"status": "blocked", "reason": "projector_unavailable"}
        con = sqlite3.connect(target, timeout=timeout)
        con.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
        result = persist(con, dict(payload))
        con.commit()
        if isinstance(result, Mapping):
            safe = dict(result)
        else:
            safe = {"status": "persisted", "inserted": False}
        safe.setdefault("reason", None)
        return safe
    except sqlite3.OperationalError:
        if con is not None:
            try:
                con.rollback()
            except Exception:
                pass
        return {"status": "blocked", "reason": "crm_db_busy"}
    except Exception:
        if con is not None:
            try:
                con.rollback()
            except Exception:
                pass
        # Do not expose import paths, provider values, or exception text to the
        # live adapter/model.  The adapter logs only this stable reason.
        return {"status": "blocked", "reason": "projection_error"}
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
