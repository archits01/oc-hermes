"""Shared bounded FIFO for LMI live lead conversations.

The three Unipile adapters share one GatewayRunner.  Storing this dispatcher on
that runner gives LinkedIn, Instagram, and WhatsApp one combined capacity cap
instead of three independent caps.  Five conversations may actively run; later
events start in FIFO order as workers become free.

The provider webhook ledger remains the durable ingress record.  This queue is
deliberately process-local so it never serializes credentials or customer text
into a second database.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

from gateway.session import build_session_key


logger = logging.getLogger(__name__)

DEFAULT_MAX_CONCURRENT = 5
MAX_CONFIGURED_CONCURRENT = 20


# Explicit media requests must not be handed to the generic live LLM path.
# That path can acknowledge the request in prose, but it cannot itself prove
# an attachment receipt.  The durable recovery worker owns the approved-media
# catalog, provider upload, and history reconciliation.  Keep this predicate
# intentionally narrow: ordinary conversation that merely mentions a photo or
# video should retain the fast direct-reply path.
_MEDIA_WORDS = r"photo|photos|picture|pictures|image|images|video|videos|clip|clips"
_MEDIA_ACTIONS = r"send|share|show|provide|attach|forward|upload|give|deliver"
_EXPLICIT_MEDIA_REQUEST = re.compile(
    rf"\b(?:{_MEDIA_ACTIONS})\b[^.!?,;\n]{{0,100}}\b(?:{_MEDIA_WORDS})\b"
    rf"|\b(?:{_MEDIA_WORDS})\b[^.!?,;\n]{{0,100}}\b(?:{_MEDIA_ACTIONS})\b",
    re.IGNORECASE,
)


def has_explicit_media_request(text: Any) -> bool:
    """Return true only when the inbound text asks us to send stored media.

    This is an ingress-routing hint, not a delivery authorization.  The
    recovery worker still requires the exact scoped chat, catalog IDs, and
    provider receipt before recording success.  Negative statements such as
    ``do not send photos`` are excluded so a customer cannot accidentally
    trigger an upload by declining media.
    """
    normalized = re.sub(r"\s+", " ", str(text or "").strip().lower())
    if not normalized or not _EXPLICIT_MEDIA_REQUEST.search(normalized):
        return False
    if re.search(
        rf"\b(?:no|not|without|don't|do not|dont)\b"
        rf"(?:\s+\w+){{0,5}}\s+\b(?:{_MEDIA_ACTIONS})\b"
        rf"(?:\s+\w+){{0,5}}\s+\b(?:{_MEDIA_WORDS})\b",
        normalized,
        re.IGNORECASE,
    ):
        return False
    return True


def _configured_capacity() -> int:
    raw = os.environ.get("LMI_LIVE_MAX_CONCURRENT", str(DEFAULT_MAX_CONCURRENT))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_MAX_CONCURRENT
    return max(1, min(MAX_CONFIGURED_CONCURRENT, value))


@dataclass
class _WorkItem:
    sequence: int
    adapter: Any
    event: Any


class LiveReplyQueue:
    """One process-local FIFO with a fixed number of conversation workers."""

    def __init__(self, capacity: int | None = None):
        self.capacity = capacity or _configured_capacity()
        self.queue: asyncio.Queue[_WorkItem] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._next_sequence = 0
        self.active = 0

    def _ensure_workers(self) -> None:
        self._workers = [task for task in self._workers if not task.done()]
        while len(self._workers) < self.capacity:
            index = len(self._workers) + 1
            task = asyncio.create_task(
                self._worker(index), name=f"lmi-live-reply-{index}"
            )
            self._workers.append(task)

    async def submit(self, adapter: Any, event: Any) -> int:
        """Queue an event and return its one-based sequence number."""
        self._ensure_workers()
        self._next_sequence += 1
        item = _WorkItem(self._next_sequence, adapter, event)
        await self.queue.put(item)
        logger.info(
            "[lmi-live-queue] admitted sequence=%d active=%d queued=%d capacity=%d",
            item.sequence,
            self.active,
            self.queue.qsize(),
            self.capacity,
        )
        return item.sequence

    async def _worker(self, worker_index: int) -> None:
        while True:
            item = await self.queue.get()
            self.active += 1
            try:
                logger.info(
                    "[lmi-live-queue] starting sequence=%d worker=%d active=%d queued=%d",
                    item.sequence,
                    worker_index,
                    self.active,
                    self.queue.qsize(),
                )
                # Fresh inbound turns need a short, bounded human cadence:
                # never instant (which looks automated), never the multi-minute
                # catch-up delay used by cold work. The reservation is atomic
                # across all LMI channels and remains fail-open only for the
                # pacing helper itself; reply eligibility already claimed the
                # exact provider message before it entered this queue.
                try:
                    import sys as _sys
                    if "/root/leadgen" not in _sys.path:
                        _sys.path.insert(0, "/root/leadgen")
                    from human_pacing import async_wait_before_priority_reply

                    platform = getattr(getattr(item.event, "source", None), "platform", "")
                    channel = str(getattr(platform, "value", platform) or getattr(item.adapter, "name", ""))
                    channel = channel.strip().lower()
                    if "whatsapp" in channel:
                        channel = "whatsapp"
                    elif "instagram" in channel:
                        channel = "instagram"
                    elif "linkedin" in channel:
                        channel = "linkedin"
                    if channel in {"whatsapp", "instagram", "linkedin"}:
                        waited = await async_wait_before_priority_reply(channel)
                        logger.info(
                            "[lmi-live-queue] fresh pacing sequence=%d channel=%s waited=%.1fs",
                            item.sequence,
                            channel,
                            waited,
                        )
                except Exception as exc:
                    logger.warning(
                        "[lmi-live-queue] fresh pacing skipped sequence=%d (%s)",
                        item.sequence,
                        type(exc).__name__,
                    )
                await item.adapter.handle_message(item.event)
                await self._wait_for_session(item.adapter, item.event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "[lmi-live-queue] failed sequence=%d", item.sequence
                )
            finally:
                self.active -= 1
                self.queue.task_done()

    @staticmethod
    async def _wait_for_session(adapter: Any, event: Any) -> None:
        """Hold the worker until Hermes finishes this chat turn and follow-ups."""
        config = adapter.config
        session_key = build_session_key(
            event.source,
            group_sessions_per_user=config.extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=config.extra.get("thread_sessions_per_user", False),
        )
        while session_key in getattr(adapter, "_active_sessions", {}):
            task = getattr(adapter, "_session_tasks", {}).get(session_key)
            if task is None:
                await asyncio.sleep(0.05)
                continue
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                raise
            except Exception:
                # The adapter owns error reporting.  The queue only needs to
                # release the capacity slot once that turn has unwound.
                pass

    async def close(self) -> None:
        workers = list(self._workers)
        self._workers.clear()
        for task in workers:
            task.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)


async def submit_live_reply(adapter: Any, event: Any) -> int:
    """Submit to the one shared LMI dispatcher attached to the gateway runner."""
    runner = getattr(adapter, "gateway_runner", None)
    if runner is None:
        # Standalone adapter tests and recovery tools may not install a runner.
        # Preserve behavior rather than silently dropping the event.
        await adapter.handle_message(event)
        return 0

    dispatcher = getattr(runner, "_lmi_live_reply_queue", None)
    if dispatcher is None:
        dispatcher = LiveReplyQueue()
        runner._lmi_live_reply_queue = dispatcher
    return await dispatcher.submit(adapter, event)
