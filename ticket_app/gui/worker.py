"""Thread boundary, runtime events, cancellation and logging for the GUI."""

from __future__ import annotations

import logging
import traceback
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from PySide6.QtCore import QObject, QThread, Signal, Slot

from ticket_app.client import RailwayClient
from ticket_app.clock import ServerClock
from ticket_app.runner import TicketRunner
from ticket_app.runtime import CancellationToken, RunCancelled

from .async_logging import AsyncLogPipeline, GuiLogLine
from .passenger_widgets import contact_display_rows


class LogBridge(QObject):
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
    connects :attr:`LogBridge.messages` to a batch-aware log view.
    """

    def publish(lines: tuple[GuiLogLine, ...]) -> None:
        bridge.publish_batch(lines)

    return AsyncLogPipeline(gui_batch_sink=publish, level=level, batch_interval=batch_interval)


class _EventMethods:
    """Normalize runtime events for the persistent worker's signal."""

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

    # Runtime events arrive through the callable sink protocol.
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

    def phase(self, phase: str, message: str = "", **kwargs: Any) -> None:
        self._send("phase", {"phase": phase, "message": message, **kwargs})

class GuiCancelToken(CancellationToken):
    """Cancellation token with maintenance actions enabled for GUI jobs."""

    def __init__(self) -> None:
        super().__init__()
        self.supports_actions = True


CONNECTION_MODES = frozenset({"login", "contacts", "check_login", "sync_clock"})


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
                self.runner = TicketRunner(
                    request.cfg, event_sink=relay, cancel_token=request.cancel_token,
                    session=request.session, clock=request.clock,
                )
                result = int(self.runner.run())
                if request.cancel_token.is_cancelled and self._order_safety()[1]:
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
