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
from dataclasses import dataclass
from typing import Any

from gateway.session import build_session_key


logger = logging.getLogger(__name__)

DEFAULT_MAX_CONCURRENT = 5
MAX_CONFIGURED_CONCURRENT = 20


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
