"""Bounded queue handing hook work to a worker thread."""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")

DEFAULT_CAPACITY = 2048
_POLL_SECONDS = 0.05

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DispatchStats:
    """Every submission lands in exactly one of these buckets."""

    accepted: int = 0
    dropped: int = 0
    handled: int = 0
    failed: int = 0


class _Barrier:
    """Marks a position in the queue so a caller can wait for it to pass."""

    __slots__ = ("event",)

    def __init__(self) -> None:
        self.event = threading.Event()


class Dispatcher(Generic[T]):
    """A bounded queue drained by one worker thread."""

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self._queue: queue.Queue[object] = queue.Queue(maxsize=capacity)
        self._lock = threading.Lock()
        self._accepted = 0
        self._dropped = 0
        self._handled = 0
        self._failed = 0
        self._accepting = False
        self._stopping = threading.Event()
        self._worker: threading.Thread | None = None

    @property
    def depth(self) -> int:
        """Items waiting, not counting one already in the handler."""
        return self._queue.qsize()

    @property
    def worker_is_daemon(self) -> bool:
        worker = self._worker
        return bool(worker and worker.daemon)

    def start(self, handler: Callable[[T], None]) -> None:
        if self._worker is not None:
            return
        self._accepting = True
        self._worker = threading.Thread(
            target=self._run,
            args=(handler,),
            name="hermess-metrics",
            daemon=True,
        )
        self._worker.start()

    def submit(self, item: T) -> bool:
        """Queue an item without blocking. Reports whether it was taken."""
        if not self._accepting:
            with self._lock:
                self._dropped += 1
            return False
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            with self._lock:
                self._dropped += 1
            return False
        with self._lock:
            self._accepted += 1
        return True

    def flush(self, timeout: float) -> bool:
        """Wait until everything queued so far has been handled."""
        if self._worker is None:
            return True
        barrier = _Barrier()
        try:
            self._queue.put_nowait(barrier)
        except queue.Full:
            return False
        return barrier.event.wait(timeout)

    def stop(self, timeout: float) -> None:
        """Refuse new items, drain what is queued, and retire the worker."""
        self._accepting = False
        worker = self._worker
        if worker is None:
            return
        self._stopping.set()
        worker.join(timeout)
        self._worker = None

    def stats(self) -> DispatchStats:
        with self._lock:
            return DispatchStats(
                accepted=self._accepted,
                dropped=self._dropped,
                handled=self._handled,
                failed=self._failed,
            )

    def _run(self, handler: Callable[[T], None]) -> None:
        while True:
            try:
                item = self._queue.get(timeout=_POLL_SECONDS)
            except queue.Empty:
                if self._stopping.is_set():
                    return
                continue
            if isinstance(item, _Barrier):
                item.event.set()
                continue
            try:
                handler(item)  # type: ignore[arg-type]
            except Exception:
                with self._lock:
                    self._failed += 1
                logger.warning("hermess-metrics handler failed", exc_info=True)
            else:
                with self._lock:
                    self._handled += 1
