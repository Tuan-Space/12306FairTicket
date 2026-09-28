"""Thread boundary, runtime events, cancellation and logging for the GUI."""

from __future__ import annotations

import inspect
import logging
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from PySide6.QtCore import QObject, QThread, Signal, Slot

from ticket_app.configuration import AppConfig
from ticket_app.client import RailwayClient
from ticket_app.clock import ServerClock
from ticket_app.logging_utils import RedactingFormatter, redact_text
from ticket_app.runner import TicketRunner
from ticket_app.runtime import CancellationToken, RunCancelled

from .async_logging import AsyncLogPipeline, AsyncQueueLogHandler, GuiLogLine
from .passenger_widgets import contact_display_rows


def redact_log_text(text: str) -> str:
    return redact_text(text)


class LogBridge(QObject):
    message = Signal(str, str)
    # The asynchronous listener emits one tuple of ``(line, level)`` values at
    # most every 100 ms.  Keeping the original single-line signal preserves a
    # compatibility path for tests and third-party integrations.
    messages = Signal(object)

    def publish_batch(self, lines: object) -> None:
        """Publish a listener-owned batch without rendering in a worker thread.

        ``AsyncLogPipeline`` invokes this method from its plain Python
        listener thread.  Qt queues delivery to slots whose receiver belongs
        to the UI thread, so neither a ticket request nor the listener ever
        edits a widget directly.
        """

        self.messages.emit(lines)


def create_async_log_pipeline(
    bridge: LogBridge,
    *,
    level: int = logging.DEBUG,
    batch_interval: float = 0.1,
) -> AsyncLogPipeline:
    """Create the GUI's non-blocking log transport.

    The application attaches ``pipeline.handler`` to the root logger and
    connects :attr:`LogBridge.messages` to a batch-aware log view.  The old
    ``QtLogHandler`` remains available for integrations that have not moved
    to the asynchronous transport yet.
    """

    def publish(lines: tuple[GuiLogLine, ...]) -> None:
        bridge.publish_batch(lines)

    return AsyncLogPipeline(gui_batch_sink=publish, level=level, batch_interval=batch_interval)


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


class _EventMethods:
    """Plain-Python normalization shared by Qt and persistent-job relays."""

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
        self._publish_event(event_name, data)

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


class EventRelay(QObject, _EventMethods):
    """Compatibility relay for standalone workers and integrations."""

    # ``QObject`` already owns event(QEvent); keep the signal name distinct.
    runtime_event = Signal(str, object)

    def _publish_event(self, kind: str, payload: Any) -> None:
        self.runtime_event.emit(kind, payload)


class GuiCancelToken(CancellationToken):
    """Cooperative token exposing common cancellation protocol spellings."""

    def __init__(self) -> None:
        super().__init__()
        self.supports_actions = True

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


CONNECTION_MODES = frozenset({"login", "contacts", "check_login", "sync_clock"})


def _make_ticket_runner(cfg: Any, relay: Any, cancel_token: GuiCancelToken,
                        session: Any, clock: Optional[ServerClock]) -> TicketRunner:
    """Keep constructor compatibility without creating a per-job QObject."""
    signature = inspect.signature(TicketRunner)
    kwargs: Dict[str, Any] = {}
    if "event_sink" in signature.parameters:
        kwargs["event_sink"] = relay
    if "cancel_token" in signature.parameters:
        kwargs["cancel_token"] = cancel_token
    if "session" in signature.parameters:
        kwargs["session"] = session
    elif "shared_session" in signature.parameters:
        kwargs["shared_session"] = session
    if "clock" in signature.parameters:
        kwargs["clock"] = clock
    runner = TicketRunner(cfg, **kwargs)
    if "event_sink" not in kwargs:
        setattr(runner, "event_sink", relay)
    if "cancel_token" not in kwargs:
        setattr(runner, "cancel_token", cancel_token)
    return runner


def _run_connection_operation(mode: str, cfg: Any, relay: Any,
                              cancel_token: GuiCancelToken, session: Any,
                              clock: Optional[ServerClock]) -> Any:
    """Execute only authentication, contact reading or clock maintenance."""
    cancel_token.checkpoint()
    if mode == "sync_clock":
        clock = clock if clock is not None else ServerClock(session, cfg)
        # One persistent worker owns all mutation of this in-memory clock.
        clock.cfg = cfg
        clock.timeout = cfg.request_timeout_seconds
        clock.session = session
        result: Any = clock.sync(cancel_token, relay, budget_seconds=10.0, source="manual")
    else:
        client = RailwayClient(cfg, relay, cancel_token, session)
        if mode == "check_login":
            result = client.check_session()
        else:
            if mode == "login":
                # Explicit login is the only idle operation allowed to scan.
                client.ensure_login(check_first=False)
                authenticated = True
            else:
                authenticated = client.check_session()
            cancel_token.checkpoint()
            result = {"authenticated": authenticated, "contacts": None}
            if authenticated:
                try:
                    result["contacts"] = contact_display_rows(client.get_passengers())
                except (InterruptedError, RunCancelled):
                    raise
                except Exception as exc:
                    cancel_token.checkpoint()
                    logging.warning("联系人读取失败（%s），保留已登录状态", type(exc).__name__)
                    result["contacts_error"] = "联系人读取失败，请重新读取或手动填写姓名。"
    cancel_token.checkpoint()
    return result


@dataclass(frozen=True)
class OperationRequest:
    """A prepared configuration snapshot passed to the persistent worker."""

    generation: int
    mode: str
    cfg: Any
    cancel_token: GuiCancelToken
    session: Any = None
    clock: Optional[ServerClock] = None
    force_login: bool = False

    def __post_init__(self) -> None:
        if self.mode not in CONNECTION_MODES | {"task"}:
            raise ValueError("未知后台操作")


@dataclass(frozen=True)
class OperationOutcome:
    """Terminal result and order safety survive cancellation and old UI state."""

    mode: str
    result: Any = None
    error: str = ""
    details: str = ""
    order_state: str = "safe"
    safe_to_restart: bool = True


class _OperationEventRelay(_EventMethods):
    """A per-job callback, deliberately not a QObject."""

    def __init__(self, owner: "OperationWorker", generation: int) -> None:
        self.owner = owner
        self.generation = generation

    def _publish_event(self, kind: str, payload: Any) -> None:
        if kind == "order_success":
            self.owner._observed_order_state = "success"
        self.owner.runtime_event.emit(self.generation, kind, payload)


class OperationWorker(QObject):
    """Run sequential jobs on one long-lived QObject and QThread.

    ``finished`` ends a job, not its thread. Only application shutdown should
    retire this object to the GUI thread before quitting its worker thread.
    """

    runtime_event = Signal(int, str, object)
    finished = Signal(int, object)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.runner: Optional[TicketRunner] = None
        self.current_request: Optional[OperationRequest] = None
        self._observed_order_state = "safe"

    @Slot(object)
    def retire_to_thread(self, destination: QThread) -> None:
        """Return ownership before the source thread exits.

        Queue this slot after the final job. Moving from the object's current
        thread is required by Qt; the GUI may delete it only after joining the
        source thread. In particular, do not destroy Python-backed QObjects
        in a thread.finished callback while the GUI creates other Qt objects.
        """
        source = self.thread()
        if source is destination:
            return
        self.moveToThread(destination)
        source.quit()

    def _order_safety(self) -> tuple[str, bool]:
        state = str(getattr(self.runner, "order_state", self._observed_order_state))
        if self._observed_order_state == "success":
            state = "success"
        if state not in {"safe", "confirm_sent", "queued", "success", "unknown"}:
            state = "unknown"
        safe = state == "safe" and bool(getattr(self.runner, "safe_to_restart", True))
        return state, safe

    @Slot(object)
    def execute(self, request: OperationRequest) -> None:
        self.current_request = request
        self.runner = None
        self._observed_order_state = "safe"
        relay = _OperationEventRelay(self, request.generation)
        result, error, details = None, "", ""
        try:
            request.cancel_token.checkpoint()
            if request.mode == "task":
                relay.phase("preparing", "正在准备任务")
                self.runner = _make_ticket_runner(
                    request.cfg, relay, request.cancel_token, request.session, request.clock,
                )
                result = int(self.runner.run())
                if request.cancel_token.cancelled and self._order_safety()[1]:
                    relay.phase("cancelled", "任务已停止")
            else:
                result = _run_connection_operation(
                    request.mode, request.cfg, relay, request.cancel_token, request.session, request.clock,
                )
        except (InterruptedError, RunCancelled):
            result = 130 if request.mode == "task" else None
            if request.mode == "task" and self._order_safety()[1]:
                relay.phase("cancelled", "任务已停止")
        except Exception as exc:
            # Do not replace an uncertain order error with a cancellation.
            error, details = str(exc), traceback.format_exc()
        finally:
            state, safe = self._order_safety()
            outcome = OperationOutcome(request.mode, result, error, details, state, safe)
            self.current_request = None
            self.finished.emit(request.generation, outcome)


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
        *,
        clock: Optional[ServerClock] = None,
    ) -> None:
        super().__init__()
        self.cfg = cfg
        self.relay = relay
        self.cancel_token = cancel_token
        self.session = session
        self.clock = clock
        self.runner: Optional[TicketRunner] = None

    def _make_runner(self) -> TicketRunner:
        return _make_ticket_runner(self.cfg, self.relay, self.cancel_token, self.session, self.clock)

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


class ConnectionWorker(QObject):
    """One idle connection operation, with no journey or order runner.

    The window owns a single QThread slot shared with TicketWorker. During a
    ticket run, maintenance is instead sent to that runner's action queue.
    """

    completed = Signal(str, object)
    failed = Signal(str, str)
    done = Signal()

    def __init__(self, mode: str, cfg: Any, relay: EventRelay, cancel_token: GuiCancelToken,
                 session: Any = None, *, force_login: bool = False,
                 clock: Optional[ServerClock] = None) -> None:
        super().__init__()
        if mode not in CONNECTION_MODES:
            raise ValueError("未知连接操作")
        self.mode = mode
        self.cfg = cfg
        self.relay = relay
        self.cancel_token = cancel_token
        self.session = session
        self.force_login = force_login
        self.clock = clock

    @Slot()
    def run(self) -> None:
        try:
            result = _run_connection_operation(
                self.mode, self.cfg, self.relay, self.cancel_token, self.session, self.clock,
            )
            self.completed.emit(self.mode, result)
        except (InterruptedError, RunCancelled):
            self.completed.emit(self.mode, None)
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
