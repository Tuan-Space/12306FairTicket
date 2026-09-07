"""Runtime events and cooperative cancellation shared by CLI and GUI frontends."""

from __future__ import annotations

import threading
import time
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

    def cancel(self) -> None:
        self._event.set()

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
