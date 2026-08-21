"""Shared, provider-free inbound media-scope seam for LMI adapters.

The deployment bootstrap owns the real binder.  It must configure this single
object once, after it creates the reviewed media overlay's deployment config
and verified inbound-scope registry.  The WhatsApp and Instagram adapters use
the object only to bind a raw webhook payload to their deterministic Hermes
source before admitting an event to the live-reply queue.

This module deliberately does not read environment variables, construct a
provider client, or install model tools.  Until deployment configures it,
inbound direct-reply admission fails closed.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
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

