"""Independent cross-flow regressions for connection state presentation."""

import threading
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEvent, QObject, QThread, QTimer, Qt, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QLabel
from shiboken6 import isValid

from test_gui_app import main_window  # noqa: F401
from test_gui_issue_features import no_unexpected_message_boxes  # noqa: F401
from ticket_app.configuration import AppError
from ticket_app.gui import app as gui_app
from ticket_app.gui import worker as worker_module
from ticket_app.gui.worker import GuiCancelToken


def test_cancelled_connection_leaves_booking_idle_after_worker_releases_session(main_window, qtbot, monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    class Client:
        def __init__(self, cfg, relay, token, session):
            self.token = token

        def check_session(self):
            return True

        def get_passengers(self):
            entered.set()
            assert release.wait(3)
            return [{"passenger_name": "迟到结果", "passenger_type": "1"}]

    monkeypatch.setattr(worker_module, "RailwayClient", Client)
    main_window._start_connection_operation("contacts")
    qtbot.waitUntil(entered.is_set)
    main_window._stop_task()
    release.set()
    qtbot.waitUntil(lambda: main_window._active_operation is None)
    assert "正在安全停止" not in main_window.phase_badge.text()
    assert main_window._task_state == "idle"
    assert "任务未开始" in main_window.workflow_status.text()
    assert not main_window.start_button.isEnabled()
    assert not main_window.stop_button.isEnabled()
    assert main_window.passengers.text() == ""


def test_failed_idle_login_check_keeps_last_confirmed_login_display(main_window, qtbot, monkeypatch):
    monkeypatch.setattr(gui_app.QMessageBox, "warning", lambda *args: None)
    main_window._on_runtime_event("session_checked", {"state": "valid", "checked_at": 100})
    original_display = main_window.qr_image.text()

    class Client:
        def __init__(self, cfg, relay, token, session):
            self.relay = relay

        def check_session(self):
            self.relay("session_checked", {"state": "failed", "checked_at": 200})
            raise AppError("检查失败，无法确认")

    monkeypatch.setattr(worker_module, "RailwayClient", Client)
    main_window._start_connection_operation("check_login")
    qtbot.waitUntil(lambda: main_window._active_operation is None)
    assert main_window.qr_image.text() == original_display
    assert "检查失败" in main_window.session_check_status.text()
    assert not main_window._login_required
    assert main_window.login_button.isEnabled()


@pytest.mark.parametrize("outcome", ["cancelled", "failed", "success"])
def test_delayed_query_cannot_replace_terminal_task_status(main_window, qtbot, monkeypatch, outcome):
    monkeypatch.setattr(gui_app.QMessageBox, "information", lambda *args: None)
    monkeypatch.setattr(gui_app.QMessageBox, "critical", lambda *args: None)
    main_window._active_operation = SimpleNamespace(mode="task")
    main_window.cancel_token = GuiCancelToken()
    main_window._operation_mode = "task"
    main_window._go_to_step(3)
    main_window._on_runtime_event("query", {"attempt": 10, "message": "正在查询余票"})
    assert main_window.query_ui_timer.isActive()

    if outcome == "cancelled":
        main_window._stop_task()
        main_window._release_operation()
    elif outcome == "failed":
        main_window._on_worker_failed("订单状态无法确认，请核对", "模拟响应异常")
    else:
        main_window._on_runtime_event("order_success", {"order_id": "MOCK-ORDER", "message": "订单已提交"})

    terminal_message = main_window.phase_badge.text()
    qtbot.wait(150)
    assert main_window._task_state == outcome
    assert main_window.phase_badge.text() == terminal_message
    if main_window._active_operation is not None:
        main_window._release_operation()


def test_normal_login_recheck_does_not_show_recovery_instructions(main_window):
    main_window._active_operation = SimpleNamespace(mode="task")
    main_window.cancel_token = GuiCancelToken()
    main_window._operation_mode = "task"
    main_window.account_state = "valid"
    main_window._login_required = False
    main_window._go_to_step(3)

    main_window._on_runtime_event("phase", {"phase": "login", "message": "正在复查登录状态"})

    assert main_window.recovery_card.isHidden()
    assert main_window.authentication_panel.isHidden()
    assert not main_window._login_required


@pytest.mark.parametrize("state", ["cancelling", "new_task"])
@pytest.mark.parametrize("delivery", ["batch", "single"])
def test_delayed_text_logs_are_visible_but_cannot_change_current_operation_state(main_window, state, delivery):
    main_window._active_operation = SimpleNamespace(mode="task")
    main_window.cancel_token = GuiCancelToken()
    main_window._operation_mode = "task"
    main_window._go_to_step(3)
    if state == "cancelling":
        main_window._stop_task()
    else:
        main_window._operation_generation += 2
        main_window._on_runtime_event("phase", {"phase": "preparing", "message": "新一轮正在准备"})
    snapshot = (main_window._task_state, main_window.phase_badge.text(), main_window._operation_generation)
    lines = ["旧任务等待热身查询窗口", "旧任务开始提交订单: G1 二等座", "旧任务已提交排队", "旧任务抢票成功"]

    if delivery == "batch":
        main_window._on_log_batch([(line, "INFO") for line in lines])
    else:
        for line in lines:
            main_window._on_log_message(line, "INFO")

    assert snapshot == (main_window._task_state, main_window.phase_badge.text(), main_window._operation_generation)
    assert all(line in main_window.log_view.text.toPlainText() for line in lines)


def test_close_polls_persistent_thread_without_blocking_or_closing_session_before_join(main_window, qtbot, monkeypatch):
    """The one executor is joined only at shutdown, without a blocking wait."""
    thread = QThread(main_window)
    main_window._background_thread = thread
    joined = {"allowed": False}
    calls = []

    def wait_for_cleanup(_timeout):
        assert _timeout == 0
        calls.append(("join", joined["allowed"]))
        return joined["allowed"]

    monkeypatch.setattr(thread, "wait", wait_for_cleanup)
    monkeypatch.setattr(main_window.shared_session.cookies, "clear", lambda: calls.append(("cookies", None)))
    monkeypatch.setattr(main_window.shared_session, "close", lambda: calls.append(("session", None)))
    main_window.show()
    event = QCloseEvent()

    main_window.closeEvent(event)

    assert not event.isAccepted()
    assert main_window._closing_executor
    assert not thread.isRunning()
    assert calls == []
    qtbot.waitUntil(lambda: bool(calls))
    assert all(name == "join" for name, _value in calls)
    assert main_window._background_thread is thread
    joined["allowed"] = True
    qtbot.waitUntil(lambda: ("session", None) in calls)
    assert main_window._background_thread is None
    assert calls.index(("join", True)) < calls.index(("cookies", None)) < calls.index(("session", None))


def test_50_window_executor_shutdowns_destroy_workers_only_on_gui_thread(main_window, qtbot, monkeypatch):
    """Stress the native deletion/layout race with completed and active jobs."""
    gui_thread_id = threading.get_ident()
    destroyed_threads = []
    beats = []
    entered = threading.Event()

    class DestructionObserver(QObject):
        @Slot()
        def destroyed_on(self):
            destroyed_threads.append(threading.get_ident())

    class Client:
        def __init__(self, _cfg, _relay, token, _session):
            self.token = token

        def check_session(self):
            entered.set()
            self.token.wait(0.015)
            return False

    monkeypatch.setattr(worker_module, "RailwayClient", Client)
    monkeypatch.setattr(gui_app.QMessageBox, "question", lambda *args: gui_app.QMessageBox.StandardButton.Yes)
    observer = DestructionObserver(main_window)
    heartbeat = QTimer(main_window)

    def repaint_while_retiring():
        beats.append(time.monotonic())
        # Construct a Qt object during shutdown, matching the previous native
        # deadlock between worker destruction and pytest-qt's event loop.
        label = QLabel("界面保持响应", main_window)
        label.deleteLater()

    heartbeat.timeout.connect(repaint_while_retiring)
    heartbeat.start(1)
    try:
        for index in range(50):
            window = gui_app.MainWindow()
            entered.clear()
            window._start_connection_operation("check_login")
            executor = window._executor
            thread = window._background_thread
            executor.destroyed.connect(observer.destroyed_on, Qt.ConnectionType.DirectConnection)
            qtbot.waitUntil(entered.is_set)
            if index % 2 == 0:
                qtbot.waitUntil(lambda: window._active_operation is None)
            window.close()
            qtbot.waitUntil(lambda: window._session_closed and window._background_thread is None, timeout=3000)
            assert thread.wait(0) if isValid(thread) else True
            if isValid(executor):
                assert executor.thread() is QApplication.instance().thread()
                QApplication.sendPostedEvents(executor, QEvent.Type.DeferredDelete)
            assert not isValid(executor)
            window.deleteLater()
            QApplication.sendPostedEvents(window, QEvent.Type.DeferredDelete)
    finally:
        heartbeat.stop()
    assert destroyed_threads == [gui_thread_id] * 50
    assert len(beats) >= 50
    max_gap = max(later - earlier for earlier, later in zip(beats, beats[1:]))
    assert max_gap < 1.0
    print(f"executor_shutdowns=50 destroyed_on_gui_thread=True heartbeat_count={len(beats)} max_gap_seconds={max_gap:.6f}")
