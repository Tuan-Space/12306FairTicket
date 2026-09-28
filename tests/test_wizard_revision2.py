"""Offline acceptance checks for the second four-step interaction revision."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtWidgets import QPushButton

from test_gui_app import main_window, _png_bytes  # noqa: F401 - isolated offline GUI fixture
from ticket_app.gui import app as gui_app
from ticket_app.gui.worker import GuiCancelToken, OperationOutcome, OperationRequest


@pytest.fixture(autouse=True)
def screen_and_dialogs(monkeypatch):
    """Give default sizing a known logical screen and never block in a modal."""
    monkeypatch.setattr(gui_app.QGuiApplication, "primaryScreen", staticmethod(
        lambda: SimpleNamespace(availableGeometry=lambda: QRect(0, 0, 1920, 1080))
    ))
    dialogs = []
    for method in ("information", "warning", "critical", "question"):
        def capture(*args, _method=method, **_kwargs):
            dialogs.append((_method, args[1:3]))
            return gui_app.QMessageBox.StandardButton.No
        monkeypatch.setattr(gui_app.QMessageBox, method, capture)
    return dialogs


@pytest.fixture
def dispatched_jobs(main_window, monkeypatch):
    """Hold simulated requests until the test supplies their terminal result."""
    jobs = []

    def dispatch(mode, cfg, *, force_login=False):
        if main_window._active_operation is not None:
            return
        token = GuiCancelToken()
        request = OperationRequest(main_window._operation_generation, mode, cfg, token,
                                   main_window.shared_session, main_window.shared_clock, force_login)
        main_window.cancel_token = token
        main_window._active_operation = request
        jobs.append(request)
        main_window._update_connection_controls()

    monkeypatch.setattr(main_window, "_dispatch_operation", dispatch)
    yield jobs
    if main_window._active_operation is not None:
        main_window.cancel_token.cancel()
        mode = main_window._active_operation.mode
        main_window._receive_operation_outcome(
            main_window._operation_generation,
            OperationOutcome(mode, 130 if mode == "task" else None),
        )


def prepare_confirmation(window):
    window.from_station.setText("北京西")
    window.to_station.setText("郑州东")
    window.train_date.setDate(gui_app.QDate.currentDate())
    window.start_at.set_disabled(True)
    window.stop_at.set_disabled(True)
    window.preferred_trains.setText("G79")
    window.seat_types.set_values(["二等座"])
    window.auto_submit.setChecked(True)
    window._on_connection_completed("login", {"authenticated": True, "contacts": [
        {"name": "学生甲", "passenger_type": "3"},
        {"name": "成人乙", "passenger_type": "1"},
    ]})
    window.passengers.setText("学生甲，成人乙")
    window._go_to_step(2)


def finish_cancelled(window, *, order_state="safe", safe_to_restart=None):
    if safe_to_restart is None:
        safe_to_restart = order_state == "safe"
    window._receive_operation_outcome(
        window._operation_generation,
        OperationOutcome("task", 130, order_state=order_state, safe_to_restart=safe_to_restart),
    )


def test_page2_login_is_in_account_area_and_footer_only_navigates(main_window, qtbot, dispatched_jobs):
    main_window.show()
    main_window._go_to_step(1)
    qtbot.wait(30)
    assert main_window.account_card.isAncestorOf(main_window.login_button)
    assert main_window.login_button.isVisible()
    assert main_window.next_button.isHidden()
    assert main_window.back_button.isVisible()
    assert "下一步" in main_window.next_button.text()
    assert not main_window.check_login_button.isVisible()
    assert all(
        button.text() != "检查登录状态" or not button.isVisible()
        for button in main_window.passenger_scroll.findChildren(QPushButton)
    )
    qtbot.mouseClick(main_window.login_button, Qt.MouseButton.LeftButton)
    assert [request.mode for request in dispatched_jobs] == ["login"]
    assert main_window.current_step == 1
    assert main_window._task_state == "idle"
    assert "任务未开始" in main_window.workflow_status.text()
    main_window._receive_operation_outcome(main_window._operation_generation, OperationOutcome(
        "login", {"authenticated": True, "contacts": []},
    ))
    assert main_window.next_button.isVisible()
    assert main_window.next_button.isEnabled()
    assert main_window.current_step == 1


def test_default_750_by_880_window_has_logs_outside_scrolling_pages(main_window, qtbot):
    assert main_window.size().width() == 750
    assert main_window.size().height() == 880
    main_window.show()
    qtbot.wait(30)
    assert main_window.logs_toggle.isChecked()
    assert not main_window.log_view.isVisible()
    assert not main_window.steps.isAncestorOf(main_window.log_view)
    assert not main_window.run_scroll.isAncestorOf(main_window.log_view)
    for step in range(4):
        main_window._go_to_step(step)
        qtbot.wait(15)
        assert main_window.log_view.isVisible() is (step == 3)
        if step == 3:
            top = main_window.log_view.mapTo(main_window, QPoint(0, 0)).y()
            body_bottom = main_window.steps.mapTo(main_window, QPoint(0, main_window.steps.height())).y()
            assert top >= body_bottom
    main_window.logs_toggle.click()
    assert main_window.log_view.isHidden()
    main_window.logs_toggle.click()
    assert main_window.log_view.isVisible()


def test_back_from_running_step_cancels_immediately_and_holds_forms_readonly(
    main_window, qtbot, dispatched_jobs,
):
    prepare_confirmation(main_window)
    main_window.show()
    main_window.start_button.click()
    assert len(dispatched_jobs) == 1
    request = dispatched_jobs[0]
    assert main_window.current_step == 3
    assert main_window.back_button.isEnabled() and main_window.back_button.isVisible()
    main_window.back_button.click()
    assert main_window.current_step == 2
    assert request.cancel_token.is_cancelled
    assert main_window._active_operation is request
    assert not main_window.from_station.isEnabled()
    assert not main_window.passengers.isEnabled()
    assert not main_window.start_button.isEnabled()
    assert not main_window.back_button.isEnabled()
    assert "停止" in main_window.workflow_status.text()
    old_generation = request.generation
    finish_cancelled(main_window)
    assert main_window._active_operation is None
    assert main_window.current_step == 2
    assert main_window.from_station.isEnabled()
    assert main_window.passengers.isEnabled()
    assert main_window.start_button.isEnabled()
    assert main_window.passengers.text() == "学生甲，成人乙"
    main_window._receive_runtime_event(old_generation, "phase", {"phase": "querying", "message": "迟到结果"})
    assert main_window.current_step == 2
    assert "迟到结果" not in main_window.workflow_status.text()


def test_stop_button_becomes_continue_and_restarts_original_configuration_from_zero(
    main_window, dispatched_jobs,
):
    prepare_confirmation(main_window)
    main_window._start_task()
    first = dispatched_jobs[0]
    main_window.query_metric.value_label.setText("57")
    main_window.stop_button.click()
    assert first.cancel_token.is_cancelled
    assert not main_window.stop_button.isEnabled()
    finish_cancelled(main_window)
    assert main_window.current_step == 3
    assert main_window.stop_button.text() == "继续任务"
    assert main_window.stop_button.isEnabled()
    assert not main_window.stop_button.isHidden()
    assert main_window.query_metric.value_label.text() == "57"
    # The original confirmed snapshot controls Continue, not later widget edits.
    main_window.preferred_trains.setText("G123")
    main_window.stop_button.click()
    assert len(dispatched_jobs) == 2
    restarted = dispatched_jobs[1]
    assert restarted.generation != first.generation
    assert restarted.cancel_token is not first.cancel_token
    assert restarted.cfg is not first.cfg
    assert restarted.cfg.preferred_trains == ["G79"]
    assert restarted.cfg.passenger_names == ["学生甲", "成人乙"]
    assert restarted.session is first.session
    assert restarted.clock is first.clock
    assert main_window.query_metric.value_label.text() == "0"
    assert main_window.current_step == 3
    assert main_window.stop_button.text() == "停止任务"
    main_window._continue_task()
    assert len(dispatched_jobs) == 2


def test_continue_with_expired_session_shows_recovery_on_same_running_page(
    main_window, dispatched_jobs,
):
    prepare_confirmation(main_window)
    main_window._start_task()
    main_window.stop_button.click()
    finish_cancelled(main_window)
    main_window.account_state = "expired"
    main_window.stop_button.click()
    assert len(dispatched_jobs) == 2
    assert main_window.current_step == 3
    request = dispatched_jobs[-1]
    main_window._receive_runtime_event(request.generation, "session_checked", {"state": "expired"})
    main_window._receive_runtime_event(request.generation, "qr_ready", {
        "image_bytes": _png_bytes(), "expires_at": main_window._server_now_timestamp() + 120,
    })
    assert not main_window.recovery_card.isHidden()
    assert main_window.recovery_card.isAncestorOf(main_window.authentication_panel)
    assert not main_window.start_button.isEnabled()
    main_window._receive_runtime_event(request.generation, "qr_status", {"status": "confirmed"})
    main_window._receive_runtime_event(request.generation, "phase", {"phase": "waiting", "message": "等待开售"})
    assert main_window.account_state == "valid"
    assert main_window.current_step == 3
    assert main_window._active_operation is request
    assert len(dispatched_jobs) == 2


@pytest.mark.parametrize("order_state", ["unknown", "confirm_sent", "queued", "success"])
def test_possible_or_confirmed_order_never_allows_continue(main_window, dispatched_jobs, order_state):
    prepare_confirmation(main_window)
    main_window._start_task()
    main_window.stop_button.click()
    finish_cancelled(main_window, order_state=order_state)
    assert not main_window._restart_allowed
    assert main_window.stop_button.isHidden() or not main_window.stop_button.isEnabled()
    main_window._continue_task()
    main_window._launch_task(dispatched_jobs[0].cfg)
    assert len(dispatched_jobs) == 1
    assert main_window._active_operation is None
    assert main_window._order_state == order_state


def test_safety_outcome_overrides_back_navigation_and_keeps_order_review_visible(main_window, dispatched_jobs):
    prepare_confirmation(main_window)
    main_window._start_task()
    main_window.back_button.click()
    assert main_window.current_step == 2
    finish_cancelled(main_window, order_state="unknown")
    assert main_window.current_step == 3
    assert "核对" in main_window.flow_error.text()
    assert not main_window._restart_allowed
    assert main_window._active_operation is None


def test_confirmation_uses_the_same_explicit_default_ticket_labels_as_editor(main_window):
    prepare_confirmation(main_window)
    summary = main_window.confirm_summary.text()
    assert "默认（12306：学生票）" in summary
    assert "默认（12306：成人票）" in summary
    assert "跟随联系人" not in summary
    student = main_window.passenger_ticket_types.rows["学生甲"]
    student.setCurrentIndex(student.findData("adult"))
    main_window._refresh_confirmation()
    assert "学生甲（成人票）" in main_window.confirm_summary.text()
    assert main_window.passenger_ticket_types.values() == {"学生甲": "adult"}
    main_window._on_connection_completed("contacts", {
        "authenticated": True, "contacts": None, "contacts_error": "读取失败，可手动填写姓名",
    })
    main_window._refresh_confirmation()
    assert "默认（按12306乘客信息）" in main_window.confirm_summary.text()


def test_continue_checks_a_past_stop_time_instead_of_silently_rolling_to_next_day(
    main_window, dispatched_jobs, monkeypatch,
):
    prepare_confirmation(main_window)
    main_window.stop_at.set_disabled(False)
    main_window.stop_at.setText("12:00:00")
    before_stop = datetime.combine(date.today(), datetime.strptime("11:59:00", "%H:%M:%S").time())
    monkeypatch.setattr(main_window, "_server_now_timestamp", lambda: before_stop.timestamp())
    main_window._start_task()
    assert len(dispatched_jobs) == 1
    main_window.stop_button.click()
    finish_cancelled(main_window)
    after_stop = before_stop.replace(hour=12, minute=1)
    monkeypatch.setattr(main_window, "_server_now_timestamp", lambda: after_stop.timestamp())
    main_window.stop_button.click()
    assert len(dispatched_jobs) == 1
    assert not main_window._restart_allowed
    assert main_window._active_operation is None
    assert "停止时间" in main_window.flow_error.text()


def test_continue_after_midnight_does_not_roll_original_stop_deadline_to_new_day(
    main_window, dispatched_jobs, monkeypatch,
):
    prepare_confirmation(main_window)
    main_window.train_date.setDate(gui_app.QDate.currentDate().addDays(1))
    main_window.stop_at.set_disabled(False)
    main_window.stop_at.setText("23:59:00")
    before_midnight = datetime.combine(date.today(), datetime.strptime("23:58:00", "%H:%M:%S").time())
    monkeypatch.setattr(main_window, "_server_now_timestamp", lambda: before_midnight.timestamp())
    main_window._start_task()
    assert len(dispatched_jobs) == 1
    assert main_window._last_run_stop_deadline == before_midnight.replace(minute=59)
    main_window.stop_button.click()
    finish_cancelled(main_window)
    after_midnight = before_midnight + timedelta(minutes=3)
    monkeypatch.setattr(main_window, "_server_now_timestamp", lambda: after_midnight.timestamp())
    main_window.stop_button.click()
    assert len(dispatched_jobs) == 1
    assert not main_window._restart_allowed
    assert main_window._active_operation is None
    assert "停止时间" in main_window.flow_error.text()


def test_old_terminal_outcome_cannot_release_or_replace_restarted_task(main_window, dispatched_jobs):
    prepare_confirmation(main_window)
    main_window._start_task()
    old_generation = main_window._operation_generation
    main_window.stop_button.click()
    finish_cancelled(main_window)
    main_window.stop_button.click()
    active_request = main_window._active_operation
    main_window._receive_operation_outcome(old_generation, OperationOutcome(
        "task", 130, error="旧操作迟到异常", order_state="unknown", safe_to_restart=False,
    ))
    assert main_window._active_operation is active_request
    assert main_window._order_state == "safe"
    assert "旧操作" not in main_window.flow_error.text()
    assert len(dispatched_jobs) == 2


def test_close_during_task_cancels_and_keeps_session_until_operation_finishes(
    main_window, dispatched_jobs, monkeypatch,
):
    prepare_confirmation(main_window)
    main_window.show()
    main_window._start_task()
    request = dispatched_jobs[0]
    closed = []
    monkeypatch.setattr(main_window.shared_session, "close", lambda: closed.append(True))
    monkeypatch.setattr(gui_app.QMessageBox, "question", lambda *_args, **_kwargs: gui_app.QMessageBox.StandardButton.Yes)
    main_window.close()
    assert request.cancel_token.is_cancelled
    assert main_window._active_operation is request
    assert main_window.isVisible()
    assert closed == []
    finish_cancelled(main_window)
    assert main_window._active_operation is None
    assert not main_window.isVisible()
    assert closed == [True]


def test_idle_close_waits_for_persistent_executor_exit_before_closing_session(main_window, qtbot, monkeypatch):
    exited = {"ready": False}
    actions = []
    thread = SimpleNamespace(
        quit=lambda: actions.append("quit"),
        wait=lambda timeout: exited["ready"],
        deleteLater=lambda: actions.append("delete_thread"),
    )
    main_window._background_thread = thread
    main_window._executor = SimpleNamespace(
        setParent=lambda parent: actions.append("parent_executor"),
        deleteLater=lambda: actions.append("delete_executor"),
    )
    main_window.retire_executor.connect(lambda destination: (actions.append("retire"), thread.quit()))
    monkeypatch.setattr(main_window.shared_session, "close", lambda: actions.append("close_session"))
    main_window.show()
    main_window.close()
    assert actions == ["retire", "quit"]
    assert main_window.isVisible()
    main_window._poll_executor_shutdown()
    assert actions == ["retire", "quit"]
    assert main_window._background_thread is thread
    exited["ready"] = True
    main_window._poll_executor_shutdown()
    assert actions == ["retire", "quit", "delete_thread", "parent_executor", "delete_executor", "close_session"]
    assert main_window._background_thread is None
    assert main_window._executor is None
    assert not main_window.isVisible()
    qtbot.wait(50)
    assert actions.count("close_session") == 1

@pytest.mark.parametrize('dark', [False, True])
def test_short_window_keeps_readable_log_and_footer(main_window, qtbot, qapp, dark):
    original = qapp.styleSheet()
    try:
        qapp.setStyleSheet(gui_app._load_stylesheet(qapp, dark))
        main_window.setMinimumHeight(360)
        main_window.resize(750, 405)
        main_window.show()
        main_window._go_to_step(3)
        qtbot.wait(30)
        log = main_window.log_view
        assert main_window.rect().contains(main_window.back_button.geometry())
        assert log.text.viewport().height() >= log.text.fontMetrics().lineSpacing()
        log_bottom = log.mapTo(main_window, QPoint(0, log.height())).y()
        assert log_bottom <= main_window.back_button.y()
        assert main_window.logs_panel.height() >= log.height()
        assert main_window.steps.height() >= 40
    finally:
        qapp.setStyleSheet(original)


def test_idle_maintenance_on_results_does_not_claim_booking_started(main_window, dispatched_jobs):
    main_window._go_to_step(3)
    main_window._set_phase('cancelled', '任务已停止')
    main_window._start_connection_operation('sync_clock')
    assert main_window._active_operation.mode == 'sync_clock'
    assert '任务已启动' not in main_window.workflow_status.text()
    assert main_window._task_state == 'cancelled'


def test_hidden_login_control_and_log_bridge_have_window_lifetime(main_window):
    # Hidden compatibility controls must not become Python-owned QObject cycles:
    # a worker's cyclic collection must never choose their destruction thread.
    assert main_window.check_login_button.parent() is main_window
    assert main_window.log_bridge.parent() is main_window
