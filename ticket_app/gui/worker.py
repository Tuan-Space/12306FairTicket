"""Thread boundary, runtime events, cancellation and logging for the GUI."""

from __future__ import annotations

import inspect
import logging
import traceback
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from PySide6.QtCore import QObject, Signal, Slot

from ticket_app.configuration import AppConfig
from ticket_app.logging_utils import RedactingFormatter, redact_text
from ticket_app.runner import TicketRunner
from ticket_app.runtime import CancellationToken, RunCancelled


def redact_log_text(text: str) -> str:
    return redact_text(text)


class LogBridge(QObject):
    message = Signal(str, str)


class QtLogHandler(logging.Handler):
    def __init__(self, bridge: LogBridge) -> None:
        super().__init__()
        self.bridge = bridge
        self.set_sensitive_terms(())

    def set_sensitive_terms(self, terms: Any) -> None:
        self.setFormatter(
            RedactingFormatter(
                "%(asctime)s  %(levelname)s  %(message)s",
                "%H:%M:%S",
                sensitive_terms=terms or (),
            )
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.bridge.message.emit(redact_log_text(self.format(record)), record.levelname)
        except Exception:
            self.handleError(record)


class EventRelay(QObject):
    """Structural event sink compatible with several small callback APIs."""

    # ``QObject`` already owns an event(QEvent) method, so using ``event`` as
    # a signal name silently resolves to the Qt method on some bindings.
    runtime_event = Signal(str, object)

    def _send(self, kind: Any, payload: Any = None, **kwargs: Any) -> None:
        event_name = str(kind or "status").strip().lower().replace("-", "_").replace(" ", "_")
        if payload is None:
            data: Any = dict(kwargs)
        elif kwargs:
            if isinstance(payload, Mapping):
                data = dict(payload)
                data.update(kwargs)
            else:
                data = {"value": payload, **kwargs}
        else:
            data = payload
        self.runtime_event.emit(event_name, data)

    # Runtime's emit_event() prefers callable sinks. Do not define a method
    # named ``emit`` on a QObject: PySide uses that name internally when a
    # bound Signal is emitted, and overriding it breaks every signal here.
    def __call__(self, kind: Any, payload: Any = None, **kwargs: Any) -> None:
        if not isinstance(kind, str):
            event_kind = getattr(kind, "kind", None) or getattr(kind, "type", None) or kind.__class__.__name__
            event_data = dict(getattr(kind, "data", {}) or {})
            event_message = getattr(kind, "message", "")
            event_timestamp = getattr(kind, "timestamp", None)
            if event_message:
                event_data.setdefault("message", event_message)
            if event_timestamp is not None:
                event_data.setdefault("timestamp", event_timestamp)
            if payload is not None:
                event_data.setdefault("value", payload)
            event_data.update(kwargs)
            self._send(event_kind, event_data)
            return
        self._send(kind, payload, **kwargs)

    def publish(self, kind: Any, payload: Any = None, **kwargs: Any) -> None:
        self._send(kind, payload, **kwargs)

    def on_event(self, kind: Any, payload: Any = None, **kwargs: Any) -> None:
        self._send(kind, payload, **kwargs)

    def handle(self, event: Any, payload: Any = None, **kwargs: Any) -> None:
        if payload is None and not isinstance(event, str):
            kind = getattr(event, "kind", None) or getattr(event, "type", None) or event.__class__.__name__
            if hasattr(event, "to_mapping"):
                payload = event.to_mapping()
            elif hasattr(event, "__dict__"):
                payload = vars(event)
            else:
                payload = event
            self._send(kind, payload, **kwargs)
            return
        self._send(event, payload, **kwargs)

    # Named variants keep the GUI useful when the core uses a tiny observer
    # protocol instead of a generic event bus.
    def phase(self, phase: str, message: str = "", **kwargs: Any) -> None:
        self._send("phase", {"phase": phase, "message": message, **kwargs})

    on_phase = phase
    emit_phase = phase
    stage = phase
    on_stage = phase

    def qr_code(self, image: Any, **kwargs: Any) -> None:
        self._send("qr_code", {"image": image, **kwargs})

    on_qr_code = qr_code
    qr_ready = qr_code
    on_qr_ready = qr_code

    def qr_status(self, status: str, message: str = "", **kwargs: Any) -> None:
        self._send("qr_status", {"status": status, "message": message, **kwargs})

    on_qr_status = qr_status

    def countdown(self, name: str, remaining: float, **kwargs: Any) -> None:
        self._send("countdown", {"name": name, "remaining": remaining, **kwargs})

    on_countdown = countdown

    def query(self, payload: Any = None, **kwargs: Any) -> None:
        self._send("query", payload, **kwargs)

    on_query = query

    def candidate(self, payload: Any = None, **kwargs: Any) -> None:
        self._send("candidate", payload, **kwargs)

    on_candidate = candidate

    def order(self, payload: Any = None, **kwargs: Any) -> None:
        self._send("order", payload, **kwargs)

    on_order = order
    order_result = order
    on_order_result = order

    def warning(self, message: str, **kwargs: Any) -> None:
        self._send("warning", {"message": message, **kwargs})

    on_warning = warning


class GuiCancelToken(CancellationToken):
    """Cooperative token exposing common cancellation protocol spellings."""

    def __init__(self) -> None:
        super().__init__()

    def request_cancel(self) -> None:
        self.cancel()

    def set(self) -> None:
        self.cancel()

    @property
    def cancelled(self) -> bool:
        return self.is_cancelled

    @property
    def cancellation_requested(self) -> bool:
        return self.is_cancelled

    def is_set(self) -> bool:
        return self.is_cancelled

    def check(self) -> bool:
        return self.is_cancelled

    def throw_if_cancelled(self) -> None:
        self.checkpoint()

    raise_if_cancelled = throw_if_cancelled


class TicketWorker(QObject):
    completed = Signal(int)
    failed = Signal(str, str)
    done = Signal()

    def __init__(
        self,
        cfg: AppConfig,
        relay: EventRelay,
        cancel_token: GuiCancelToken,
        session: Any = None,
    ) -> None:
        super().__init__()
        self.cfg = cfg
        self.relay = relay
        self.cancel_token = cancel_token
        self.session = session
        self.runner: Optional[TicketRunner] = None

    def _make_runner(self) -> TicketRunner:
        signature = inspect.signature(TicketRunner)
        kwargs: Dict[str, Any] = {}
        if "event_sink" in signature.parameters:
            kwargs["event_sink"] = self.relay
        if "cancel_token" in signature.parameters:
            kwargs["cancel_token"] = self.cancel_token
        if "session" in signature.parameters:
            kwargs["session"] = self.session
        elif "shared_session" in signature.parameters:
            kwargs["shared_session"] = self.session
        runner = TicketRunner(self.cfg, **kwargs)
        # Older integration branches may accept attributes without constructor
        # arguments.  Setting them is harmless and avoids branching in the UI.
        if "event_sink" not in kwargs:
            setattr(runner, "event_sink", self.relay)
        if "cancel_token" not in kwargs:
            setattr(runner, "cancel_token", self.cancel_token)
        return runner

    @Slot()
    def run(self) -> None:
        try:
            self.relay.phase("preparing", "正在准备任务")
            self.runner = self._make_runner()
            code = int(self.runner.run())
            if self.cancel_token.cancelled:
                self.relay.phase("cancelled", "任务已停止")
            self.completed.emit(code)
        except (InterruptedError, RunCancelled):
            self.relay.phase("cancelled", "任务已停止")
            self.completed.emit(130)
        except Exception as exc:
            self.failed.emit(str(exc), traceback.format_exc())
        finally:
            self.done.emit()


def image_payload_to_bytes(value: Any) -> Optional[bytes]:
    """Extract QR bytes from bytes, a path, or a mapping event payload."""

    if value is None:
        return None
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, Path):
        try:
            return value.read_bytes()
        except OSError:
            return None
    if isinstance(value, str):
        path = Path(value)
        if path.exists():
            try:
                return path.read_bytes()
            except OSError:
                return None
        # Some core implementations expose raw base64 in the event.
        try:
            import base64

            return base64.b64decode(value, validate=True)
        except Exception:
            return None
    if isinstance(value, Mapping):
        for key in ("image", "image_bytes", "bytes", "data", "path", "file"):
            if key in value:
                result = image_payload_to_bytes(value[key])
                if result:
                    return result
    return None
