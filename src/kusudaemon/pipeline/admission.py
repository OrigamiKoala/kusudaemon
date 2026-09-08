"""Shared run-level admission controller (PLAN-CONCURRENCY-AND-SHARED-STATE.md §B7).

Provides:
- One admission controller per run, shared by role calls and writer episodes (§B7.2).
- Global Retry-After tracking and gate (§B7.5).
- AIMD wave size tracking (§B7.3).
"""

from __future__ import annotations

import asyncio
import os
import random
import threading
import time
from typing import Any, Callable


class RunAdmissionController:
    """Run-level admission controller coordinating HTTP role calls and writer episodes."""

    def __init__(
        self,
        concurrency: int = 4,
        *,
        initial_wave_cap: int | None = None,
        on_backoff: Callable[[float], None] | None = None,
    ) -> None:
        self.concurrency = max(1, concurrency)
        self._async_sem: asyncio.Semaphore | None = None
        self._thread_sem = threading.Semaphore(self.concurrency)
        self._lock = threading.Lock()
        self._closed_until = 0.0
        self.on_backoff = on_backoff
        # AIMD: start wave cap at initial or 2 (§B7.3)
        self.current_wave_cap = max(1, initial_wave_cap or min(2, self.concurrency))

    def _ensure_async_sem(self) -> asyncio.Semaphore:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if self._async_sem is None or getattr(self._async_sem, "_loop", None) is not loop:
            self._async_sem = asyncio.Semaphore(self.concurrency)
        return self._async_sem

    @property
    def closed_until(self) -> float:
        with self._lock:
            return self._closed_until

    def record_backoff(self, delay_seconds: float) -> float:
        """Publish a global closed-until window when 429 or Retry-After is encountered (§B7.5)."""
        if delay_seconds <= 0:
            return self.closed_until
        now = time.time()
        with self._lock:
            target = now + delay_seconds
            if target > self._closed_until:
                self._closed_until = target
            effective = self._closed_until
        if self.on_backoff is not None:
            try:
                self.on_backoff(delay_seconds)
            except Exception:
                pass
        return effective

    def remaining_closed_seconds(self) -> float:
        with self._lock:
            rem = self._closed_until - time.time()
            return max(0.0, rem)

    def wait_sync(self) -> None:
        """Synchronous wait if endpoint is closed by Retry-After."""
        rem = self.remaining_closed_seconds()
        if rem > 0:
            time.sleep(rem)

    async def wait_async(self) -> None:
        """Async wait if endpoint is closed by Retry-After."""
        rem = self.remaining_closed_seconds()
        if rem > 0:
            await asyncio.sleep(rem)

    def acquire_sync(self, timeout: float | None = None) -> bool:
        """Synchronous acquire for HTTP role callers."""
        self.wait_sync()
        if timeout is not None:
            return self._thread_sem.acquire(timeout=timeout)
        return self._thread_sem.acquire()

    def release_sync(self) -> None:
        """Synchronous release for HTTP role callers."""
        self._thread_sem.release()

    async def acquire_async(self) -> None:
        """Async acquire for writer episodes."""
        await self.wait_async()
        sem = self._ensure_async_sem()
        await sem.acquire()

    def release_async(self) -> None:
        """Async release for writer episodes."""
        sem = self._ensure_async_sem()
        sem.release()

    def record_wave_outcome(self, throttled_count: int, max_parallel: int) -> int:
        """AIMD adjustment on wave size (§B7.3).

        Start at 2; after a wave completes with zero throttles, +1;
        on any 429, halve.
        """
        with self._lock:
            if throttled_count == 0:
                self.current_wave_cap = min(max_parallel, self.current_wave_cap + 1)
            else:
                self.current_wave_cap = max(1, self.current_wave_cap // 2)
            return self.current_wave_cap


_GLOBAL_CONTROLLER: RunAdmissionController | None = None
_GLOBAL_LOCK = threading.Lock()


def get_global_admission_controller(concurrency: int = 4) -> RunAdmissionController:
    global _GLOBAL_CONTROLLER
    with _GLOBAL_LOCK:
        if _GLOBAL_CONTROLLER is None:
            _GLOBAL_CONTROLLER = RunAdmissionController(concurrency)
        return _GLOBAL_CONTROLLER


def set_global_admission_controller(controller: RunAdmissionController | None) -> None:
    global _GLOBAL_CONTROLLER
    with _GLOBAL_LOCK:
        _GLOBAL_CONTROLLER = controller
