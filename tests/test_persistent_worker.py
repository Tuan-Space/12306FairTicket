"""Offline lifetime, serialization and cancellation checks for one Qt worker."""

from dataclasses import FrozenInstanceError
import threading
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEvent, QObject, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtWidgets import QApplication
from shiboken6 import getCppPointer, isValid

from ticket_app.configuration import AppError, ConnectionConfig
from ticket_app.gui import worker as worker_module
from ticket_app.gui.passenger_widgets import InlinePassengerSelector
from ticket_app.gui.worker import GuiCancelToken, OperationRequest, OperationWorker
from ticket_app.runtime import RunCancelled, RuntimeEvent


class Dispatcher(QObject):
    requested = Signal(object)
    retire = Signal(object)


class Receiver(QObject):
    def __init__(self):
        super().__init__()
        self.events = []
        self.results = []
        self.contact_selector = None

    @Slot(int, str, object)
    def receive_event(self, generation, kind, payload):
        assert QThread.currentThread() is QApplication.instance().thread()
        self.events.append((generation, kind, payload))
        if kind == "session_checked" and payload.get("state") == "expired" and self.contact_selector:
            # Exercise the exact UI reconstruction that raced with native
            # per-operation QObject destruction in the former implementation.
            self.contact_selector.set_contacts([])

    @Slot(int, object)
    def finished(self, generation, result):
        assert QThread.currentThread() is QApplication.instance().thread()
        self.results.append((generation, result))


@pytest.fixture
def persistent_worker(qtbot):
    thread = QThread()
    worker = OperationWorker()
    dispatcher = Dispatcher()
    receiver = Receiver()
    worker.moveToThread(thread)
    dispatcher.requested.connect(worker.execute, Qt.ConnectionType.QueuedConnection)
    dispatcher.retire.connect(worker.retire_to_thread, Qt.ConnectionType.QueuedConnection)
    worker.runtime_event.connect(receiver.receive_event, Qt.ConnectionType.QueuedConnection)
    worker.finished.connect(receiver.finished, Qt.ConnectionType.QueuedConnection)
    thread.start()
    bundle = SimpleNamespace(worker=worker, thread=thread, dispatcher=dispatcher, receiver=receiver)
    try:
        yield bundle
    finally:
        if worker.current_request is not None:
            worker.current_request.cancel_token.cancel()
            qtbot.waitUntil(lambda: worker.current_request is None, timeout=4000)
        dispatcher.retire.emit(QApplication.instance().thread())
        qtbot.waitUntil(lambda: thread.wait(0), timeout=3000)
        assert worker.thread() is QApplication.instance().thread()
        worker.setParent(dispatcher)
        worker.deleteLater()
        QApplication.sendPostedEvents(worker, QEvent.Type.DeferredDelete)
        assert not isValid(worker)
        thread.deleteLater()


def request(generation, mode="check_login", *, token=None, session=None, clock=None):
    return OperationRequest(generation, mode,
                            ConnectionConfig.from_mapping({"persist_session": False}),
                            token if token is not None else GuiCancelToken(), session, clock)


def test_same_worker_and_thread_handle_200_expired_checks_while_gui_keeps_repainting(
    persistent_worker, qtbot, monkeypatch,
):
    bundle = persistent_worker
    caller_threads, relay_objects = [], []
    session = object()

    class Client:
        def __init__(self, _cfg, relay, token, shared):
            assert shared is session
            assert not isinstance(relay, QObject)
            self.relay = relay
            relay_objects.append(relay)

        def check_session(self):
            caller_threads.append(QThread.currentThread())
            time.sleep(0.003)
            self.relay("session_checked", {"state": "expired"})
            return False

    monkeypatch.setattr(worker_module, "RailwayClient", Client)
    selector = InlinePassengerSelector()
    qtbot.addWidget(selector)
    selector.show()
    bundle.receiver.contact_selector = selector
    worker_address = getCppPointer(bundle.worker)
    beats = []
    heartbeat = QTimer()
    heartbeat.timeout.connect(lambda: beats.append(time.monotonic()))
    heartbeat.start(5)
    try:
        for generation in range(1, 201):
            bundle.dispatcher.requested.emit(request(generation, session=session))
            qtbot.waitUntil(lambda: len(bundle.receiver.results) == generation, timeout=2000)
            actual_generation, outcome = bundle.receiver.results[-1]
            assert actual_generation == generation
            assert outcome.result is False and not outcome.error
            assert outcome.safe_to_restart and outcome.order_state == "safe"
            assert isValid(bundle.worker) and getCppPointer(bundle.worker) == worker_address
            assert bundle.thread.isRunning()
            assert bundle.worker.children() == []
    finally:
        heartbeat.stop()
    assert len(beats) > 20
    assert max(later - earlier for earlier, later in zip(beats, beats[1:])) < 1.0
    assert all(thread is bundle.thread for thread in caller_threads)
    assert len({id(relay) for relay in relay_objects}) == 200
    assert [generation for generation, _kind, _payload in bundle.receiver.events] == list(range(1, 201))


def test_connection_modes_share_one_session_and_do_not_create_ticket_runners(
    persistent_worker, qtbot, monkeypatch,
):
    calls = []
    session = object()

    class Client:
        def __init__(self, _cfg, relay, token, shared):
            assert shared is session
            assert not isinstance(relay, QObject)

        def check_session(self):
            calls.append("check")
            return True

        def ensure_login(self, *, check_first):
            calls.append(("login", check_first))

        def get_passengers(self):
            calls.append("contacts")
            return [{"passenger_name": "甲", "passenger_type": "3", "passenger_id_no": "PRIVATE"}]

    class Clock:
        def sync(self, token, relay, **kwargs):
            assert kwargs == {"budget_seconds": 10.0, "source": "manual"}
            assert self.session is session
            assert QThread.currentThread() is persistent_worker.thread
            calls.append("clock")
            return True

    monkeypatch.setattr(worker_module, "RailwayClient", Client)
    monkeypatch.setattr(worker_module, "TicketRunner", lambda *_a, **_k: pytest.fail("No order runner"))
    clock = Clock()
    modes = ("check_login", "login", "contacts", "sync_clock")
    for index, mode in enumerate(modes, 1):
        persistent_worker.dispatcher.requested.emit(request(index, mode, session=session, clock=clock))
    qtbot.waitUntil(lambda: len(persistent_worker.receiver.results) == 4)
    assert calls == ["check", ("login", False), "contacts", "check", "contacts", "clock"]
    assert [outcome.mode for _gen, outcome in persistent_worker.receiver.results] == list(modes)
    assert all(not outcome.error for _gen, outcome in persistent_worker.receiver.results)
    assert "PRIVATE" not in repr(persistent_worker.receiver.results)
    assert persistent_worker.worker.runner is None


def test_cancelling_slow_contact_read_drops_late_data_without_destroying_worker(
    persistent_worker, qtbot, monkeypatch,
):
    entered, release = threading.Event(), threading.Event()
    token = GuiCancelToken()

    class Client:
        def __init__(self, *_args):
            pass

        def check_session(self):
            return True

        def get_passengers(self):
            entered.set()
            assert release.wait(2)
            return [{"passenger_name": "迟到结果", "passenger_type": "1"}]

    monkeypatch.setattr(worker_module, "RailwayClient", Client)
    persistent_worker.dispatcher.requested.emit(request(7, "contacts", token=token))
    try:
        qtbot.waitUntil(entered.is_set)
        token.cancel()
    finally:
        release.set()
    qtbot.waitUntil(lambda: len(persistent_worker.receiver.results) == 1)
    generation, outcome = persistent_worker.receiver.results[0]
    assert generation == 7 and outcome.result is None and not outcome.error
    assert outcome.safe_to_restart
    assert isValid(persistent_worker.worker) and persistent_worker.thread.isRunning()
    persistent_worker.dispatcher.requested.emit(request(8))
    qtbot.waitUntil(lambda: len(persistent_worker.receiver.results) == 2)
    assert persistent_worker.receiver.results[-1][1].result is True


def test_retirement_waits_for_running_slot_then_preserves_worker_for_gui_deletion(
    persistent_worker, qtbot, monkeypatch,
):
    entered, release = threading.Event(), threading.Event()

    class Client:
        def __init__(self, *_args):
            pass

        def check_session(self):
            entered.set()
            assert release.wait(2)
            return False

    monkeypatch.setattr(worker_module, "RailwayClient", Client)
    bundle = persistent_worker
    gui_thread = QApplication.instance().thread()
    bundle.dispatcher.requested.emit(request(12))
    qtbot.waitUntil(entered.is_set)
    try:
        bundle.dispatcher.retire.emit(gui_thread)
        qtbot.wait(20)
        assert bundle.worker.thread() is bundle.thread
        assert bundle.thread.isRunning()
        assert bundle.receiver.results == []
    finally:
        release.set()
    qtbot.waitUntil(lambda: bundle.thread.wait(0))
    qtbot.waitUntil(lambda: len(bundle.receiver.results) == 1)
    assert isValid(bundle.worker)
    assert bundle.worker.thread() is gui_thread
    assert bundle.receiver.results[0][1].result is False
    # Repeated retirement cannot accidentally quit the GUI thread.
    bundle.worker.retire_to_thread(gui_thread)
    assert isValid(bundle.worker)


@pytest.mark.parametrize("state", ["confirm_sent", "queued", "unknown", "success"])
def test_task_failure_keeps_unsafe_order_state_even_after_user_cancellation(
    persistent_worker, qtbot, monkeypatch, state,
):
    class Runner:
        def __init__(self, cfg, event_sink=None, cancel_token=None, session=None, clock=None):
            self.token = cancel_token
            self.order_state = state
            self.safe_to_restart = False

        def run(self):
            self.token.cancel()
            raise AppError("提交后的结果无法确认")

    monkeypatch.setattr(worker_module, "TicketRunner", Runner)
    persistent_worker.dispatcher.requested.emit(request(9, "task"))
    qtbot.waitUntil(lambda: bool(persistent_worker.receiver.results))
    generation, outcome = persistent_worker.receiver.results[0]
    assert generation == 9 and outcome.order_state == state
    assert not outcome.safe_to_restart
    assert outcome.error == "提交后的结果无法确认"
    assert "AppError" in outcome.details
    assert not any(payload.get("phase") == "cancelled" for _gen, _kind, payload in persistent_worker.receiver.events)


def test_safe_task_cancel_uses_shared_session_clock_and_emits_terminal_once(
    persistent_worker, qtbot, monkeypatch,
):
    session, clock = object(), object()

    class Runner:
        def __init__(self, cfg, event_sink=None, cancel_token=None, session=None, clock=None):
            self.session, self.clock = session, clock
            self.relay, self.token = event_sink, cancel_token
            self.order_state, self.safe_to_restart = "safe", True

        def run(self):
            self.relay(RuntimeEvent("phase", "等待", {"phase": "waiting"}, timestamp=123))
            self.token.cancel()
            raise RunCancelled()

    monkeypatch.setattr(worker_module, "TicketRunner", Runner)
    persistent_worker.dispatcher.requested.emit(request(10, "task", session=session, clock=clock))
    qtbot.waitUntil(lambda: bool(persistent_worker.receiver.results))
    assert len(persistent_worker.receiver.results) == 1
    _generation, outcome = persistent_worker.receiver.results[0]
    assert outcome.result == 130 and not outcome.error and outcome.safe_to_restart
    assert persistent_worker.worker.runner.session is session
    assert persistent_worker.worker.runner.clock is clock
    event = persistent_worker.receiver.events[1]
    assert event == (10, "phase", {"phase": "waiting", "message": "等待", "timestamp": 123})


def test_order_success_event_cannot_be_relabelled_safe_or_cancelled(
    persistent_worker, qtbot, monkeypatch,
):
    class Runner:
        def __init__(self, cfg, event_sink=None, cancel_token=None, session=None, clock=None):
            self.relay, self.token = event_sink, cancel_token

        def run(self):
            self.relay("order_success", {"order_id": "MOCK"})
            self.token.cancel()
            return 0

    monkeypatch.setattr(worker_module, "TicketRunner", Runner)
    persistent_worker.dispatcher.requested.emit(request(11, "task"))
    qtbot.waitUntil(lambda: bool(persistent_worker.receiver.results))
    outcome = persistent_worker.receiver.results[0][1]
    assert outcome.result == 0 and outcome.order_state == "success"
    assert not outcome.safe_to_restart
    assert not any(payload.get("phase") == "cancelled" for _gen, _kind, payload in persistent_worker.receiver.events)


def test_request_mode_is_checked_and_request_metadata_is_immutable():
    with pytest.raises(ValueError, match="未知后台操作"):
        request(1, "unsupported")
    queued = request(1)
    with pytest.raises(FrozenInstanceError):
        queued.generation = 2
