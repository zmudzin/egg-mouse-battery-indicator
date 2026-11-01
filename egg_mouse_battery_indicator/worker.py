"""Dedicated I/O Actor managing all device transactions safely and sequentially."""

from __future__ import annotations

import itertools
import logging
import queue
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QThread, Signal

logger = logging.getLogger(__name__)


class HidController(QThread):
    """Single-threaded I/O Actor executing all hardware transactions sequentially.

    Guarantees:
    - Atomicity of multi-phase transactions (no interleaving between phases).
    - Zero references to QObject/QWidget in the worker thread (eliminates Shiboken SIGSEGV).
    - Thread-safety without complex lock hierarchies.
    - Priority queuing: User interactions (priority 0) preempt polling (priority 10).
    - Callbacks map (task_id -> callback) kept strictly on the Qt main GUI thread.
    """

    task_finished = Signal(int, object, object)  # (task_id, result, error)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._queue: queue.PriorityQueue = queue.PriorityQueue()
        self._counter = itertools.count()
        self._callbacks: dict[int, Callable[[Any, Optional[Exception]], None]] = {}
        self._running = True
        self.task_finished.connect(self._dispatch_result)

    def submit(
        self,
        priority: int,
        pure_fn: Callable[..., Any],
        *args: Any,
        callback: Optional[Callable[[Any, Optional[Exception]], None]] = None,
    ) -> int:
        """Submit an I/O task. pure_fn MUST be a pure module function from protocol.py, never a QWidget method!"""
        task_id = next(self._counter)
        if callback is not None:
            self._callbacks[task_id] = callback
        self._queue.put((priority, task_id, pure_fn, args))
        return task_id

    def cancel_callback(self, task_id: int) -> None:
        """Cancel callback on main GUI thread if dialog/widget was closed."""
        self._callbacks.pop(task_id, None)

    def _dispatch_result(self, task_id: int, result: object, error: object) -> None:
        """Dispatches result back to callback in the Qt main GUI thread."""
        cb = self._callbacks.pop(task_id, None)
        if cb is not None:
            try:
                cb(result, error)  # type: ignore
            except Exception as e:
                logger.exception("Error executing HID callback: %s", e)

    def run(self) -> None:
        logger.debug("HidController worker thread started.")
        while self._running:
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if item is None:
                self._queue.task_done()
                break

            _, task_id, fn, args = item
            try:
                res = fn(*args)
                self.task_finished.emit(task_id, res, None)
            except Exception as exc:
                logger.exception(
                    "HID action %s raised exception: %s",
                    getattr(fn, '__name__', str(fn)),
                    exc,
                )
                self.task_finished.emit(task_id, None, exc)
            finally:
                # Explicitly clear references before next blocking get()
                item = fn = args = None
                self._queue.task_done()

        logger.debug("HidController worker thread stopped.")

    def stop(self) -> None:
        """Stop the controller thread gracefully."""
        self._running = False
        self._queue.put(None)
        self.wait(1500)
