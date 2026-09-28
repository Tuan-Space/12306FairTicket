"""Runtime events and cooperative cancellation shared by CLI and GUI frontends."""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Protocol, Union


@dataclass(frozen=True)
class RuntimeEvent:
    """A small, serialisable progress event emitted by the booking engine."""

    kind: str
    message: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


class RuntimeEventSink(Protocol):
    def emit(self, event: RuntimeEvent) -> None: ...


EventSink = Optional[Union[RuntimeEventSink, Callable[[RuntimeEvent], None]]]


def emit_event(sink: EventSink, kind: str, message: str = "", **data: Any) -> None:
    """Emit an event without coupling the engine to a concrete UI toolkit."""

    if sink is None:
        return
    event = RuntimeEvent(kind=kind, message=message, data=data)
    if callable(sink):
        sink(event)
    else:
        sink.emit(event)


class RunCancelled(Exception):
    """Raised at a cooperative cancellation checkpoint."""


class CancellationToken:
    """Thread-safe cancellation primitive with interruptible waits."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._actions_condition = threading.Condition()
        self._actions: deque[str] = deque()
        self._actions_enabled = False
        self.supports_actions = False

    def cancel(self) -> None:
        self._event.set()
        with self._actions_condition:
            self._actions.clear()
            self._actions_condition.notify_all()

    def set_actions_enabled(self, enabled: bool, *, clear_pending: bool = False) -> None:
        """Only the session-owning worker enables maintenance at a safe boundary."""

        with self._actions_condition:
            self._actions_enabled = bool(enabled) and not self.is_cancelled
            if clear_pending:
                self._actions.clear()
            self._actions_condition.notify_all()

    def request_action(self, action: str) -> bool:
        """Queue a deduplicated command without touching the worker's session."""

        if action not in {"check_login", "sync_clock", "login"}:
            return False
        with self._actions_condition:
            if self.is_cancelled or not self._actions_enabled or action in self._actions:
                return False
            self._actions.append(action)
            self._actions_condition.notify_all()
            return True

    def discard_action(self, action: str) -> None:
        """Coalesce a button request racing with the same scheduled operation."""

        with self._actions_condition:
            self._actions = deque(item for item in self._actions if item != action)

    def next_action(self, timeout: float = 0.0) -> str | None:
        """Wake for a command or cancellation; cancellation always wins."""

        deadline = time.monotonic() + max(0.0, timeout)
        with self._actions_condition:
            while True:
                self.checkpoint()
                if self._actions:
                    return self._actions.popleft()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._actions_condition.wait(remaining)

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def checkpoint(self) -> None:
        if self._event.is_set():
            raise RunCancelled("任务已取消")

    def wait(self, seconds: float) -> None:
        """Wait for a duration, waking immediately when cancellation is requested."""

        if seconds <= 0:
            self.checkpoint()
            return
        if self._event.wait(seconds):
            raise RunCancelled("任务已取消")
