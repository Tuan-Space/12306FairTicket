"""Terminal-state safety and compact run presentation, with simulated jobs."""

from copy import deepcopy

import pytest

from test_gui_app import main_window  # noqa: F401
from ticket_app.gui import app as gui_app
from ticket_app.gui.worker import GuiCancelToken, OperationOutcome, OperationRequest


CONTEXT = {"cart_index": 1, "cart_total": 2, "from_station": "北京西", "to_station": "郑州东",
           "train_code": "G103", "seat_label": "二等座"}


@pytest.fixture
def dialogs(monkeypatch):
    seen = []
    for kind in ("information", "critical", "warning"):
        monkeypatch.setattr(gui_app.QMessageBox, kind, lambda *args, mode=kind: seen.append((mode, args[1], args[2])))
    return seen


@pytest.fixture
def jobs(main_window, monkeypatch, dialogs):
    seen = []

    def dispatch(mode, cfg):
        token = GuiCancelToken()
        request = OperationRequest(main_window._operation_generation, mode, cfg, token,
                                   main_window.shared_session, main_window.shared_clock)
        main_window._active_operation = request
        main_window.cancel_token = token
        seen.append(request)
        main_window._update_connection_controls()

    monkeypatch.setattr(main_window, "_dispatch_operation", dispatch)
    return seen


def start(window):
    window.from_station.setText("北京西")
    window.to_station.setText("郑州东")
    window.passengers.setText("张三")
    window.start_at.set_disabled(True)
    window.stop_at.set_disabled(True)
    window.cart_items = [{"from_station": "北京西", "to_station": "郑州东", "train_scope": "specific",
                          "train_code": "G103", "seat_type": "二等座"}]
    window._cart_changed()
    window.account_state = "valid"
    window._launch_task(window._build_current_config())
    return window._operation_generation


@pytest.mark.parametrize("state", ["confirm_sent", "queued", "unknown"])
@pytest.mark.parametrize("has_error", [False, True])
def test_uncertain_order_cannot_become_stopped_or_no_ticket_after_cancel(main_window, jobs, state, has_error):
    generation = start(main_window)
    main_window._on_runtime_event("cart_item", CONTEXT, generation=generation)
    main_window._on_runtime_event("query_empty", {**CONTEXT, "message": "旧的无票提示"}, generation=generation)
    main_window._stop_task()
    main_window._receive_operation_outcome(generation, OperationOutcome(
        "task", 130, "服务器未返回可确认的订单结果" if has_error else "", "", state, False,
    ))
    assert main_window._task_state == "unknown"
    assert main_window.phase_badge.text().startswith("结果待核对")
    assert "12306" in main_window.phase_badge.text()
    if has_error:
        assert "服务器未返回" in main_window.phase_badge.text()
    assert "未出票" not in main_window.phase_badge.text()
    assert not main_window._restart_allowed
    assert main_window.stop_button.isHidden()
    assert main_window.order_button.isEnabled()
    assert "最后尝试" in main_window.current_cart_item.text()
    assert main_window.cart_query_status.isHidden()
    assert main_window.cart_query_status.text() == ""
    assert main_window.sale_countdown.isHidden() and main_window.sale_countdown.text() == ""
    assert main_window.flow_error.isHidden()
    main_window._continue_task()
    assert len(jobs) == 1


def test_safe_stop_can_continue_from_zero_without_old_candidate_or_countdown(main_window, jobs):
    generation = start(main_window)
    main_window._on_runtime_event("candidate", CONTEXT, generation=generation)
    main_window._on_runtime_event("query", {"attempt": 12, "message": "旧查询"}, generation=generation)
    main_window._stop_task()
    assert main_window.query_count.text() == "12"
    main_window._receive_operation_outcome(generation, OperationOutcome("task", 130))
    assert main_window.phase_badge.text() == "已停止"
    assert main_window.stop_button.text() == "继续任务"
    assert main_window._restart_allowed
    assert main_window.current_cart_item.text().startswith("最后尝试")
    main_window._continue_task()
    assert len(jobs) == 2
    assert main_window.query_count.text() == "0"
    assert main_window.current_cart_item.text() == "" and main_window.current_cart_item.isHidden()
    assert main_window._last_candidate_context is None
    assert main_window.phase_badge.text() == "正在准备"
    main_window._on_runtime_event("candidate", CONTEXT, generation=generation)
    assert main_window.current_cart_item.isHidden()
    assert main_window._test_network_calls == []


@pytest.mark.parametrize("terminal", ["no_ticket", "unknown", "success", "failed"])
def test_late_query_events_do_not_replace_terminal_item_status_or_count(main_window, jobs, terminal, qtbot):
    generation = start(main_window)
    main_window._on_runtime_event("cart_item", CONTEXT, generation=generation)
    main_window._on_runtime_event("query", {"attempt": 7}, generation=generation)
    if terminal == "success":
        main_window._on_runtime_event("order_success", {**CONTEXT, "order_id": "TEST"}, generation=generation)
    else:
        main_window._set_phase(terminal, "需要保留的具体原因")
    snapshot = (main_window.phase_badge.text(), main_window.current_cart_item.text(), main_window.query_count.text())
    other = {**CONTEXT, "train_code": "D17", "cart_index": 2}
    for kind in ("query", "candidate", "cart_item", "query_failed", "phase"):
        main_window._on_runtime_event(kind, {**other, "attempt": 99, "phase": "querying"}, generation=generation)
    qtbot.wait(130)
    assert (main_window.phase_badge.text(), main_window.current_cart_item.text(), main_window.query_count.text()) == snapshot
    assert main_window.query_count.text() == "7"
    if terminal == "success":
        assert main_window.current_cart_item.text().startswith("已购")
    if terminal in {"failed", "unknown"}:
        assert "需要保留的具体原因" in main_window.phase_badge.text()
    assert main_window.phase_badge.property("phaseState") == terminal


def test_success_safety_outcome_remains_success_when_stop_overlaps(main_window, jobs):
    generation = start(main_window)
    main_window._on_runtime_event("cart_item", CONTEXT, generation=generation)
    main_window._stop_task()
    main_window._receive_operation_outcome(generation, OperationOutcome("task", 0, order_state="success", safe_to_restart=False))
    assert main_window._task_state == "success"
    assert main_window.phase_badge.text() == "出票成功"
    assert main_window.current_cart_item.text().startswith("已购")
    assert main_window.order_button.text() == "前往 12306 支付"
    assert not main_window._restart_allowed


def test_safe_exhaustion_ends_without_ticket_and_zero_success_without_confirmation_is_uncertain(main_window, jobs):
    generation = start(main_window)
    main_window._receive_operation_outcome(generation, OperationOutcome("task", 1))
    assert main_window.phase_badge.text() == "结束未出票"
    assert main_window.current_cart_item.isHidden()
    assert not main_window._restart_allowed
    generation = start(main_window)
    main_window._receive_operation_outcome(generation, OperationOutcome("task", 0))
    assert main_window._task_state == "unknown"
    assert main_window._order_state == "unknown"


def test_countdown_only_shows_future_waiting_and_auxiliary_results_preserve_terminal(main_window, jobs, monkeypatch):
    generation = start(main_window)
    monkeypatch.setattr(main_window, "_server_now_timestamp", lambda: 100.0)
    main_window._on_runtime_event("phase", {"phase": "waiting", "target_timestamp": 120.0}, generation=generation)
    assert not main_window.sale_countdown.isHidden()
    monkeypatch.setattr(main_window, "_server_now_timestamp", lambda: 120.0)
    main_window._update_countdowns()
    assert main_window.sale_countdown.isHidden() and not main_window.sale_countdown.text()
    main_window._receive_operation_outcome(generation, OperationOutcome("task", 1))
    phase = main_window.phase_badge.text()
    main_window._start_connection_operation("sync_clock")
    main_window._on_runtime_event("clock_sync", {"success": True, "offset_seconds": 0.25, "rtt_ms": 50, "checked_at": 100})
    assert main_window.phase_badge.text() == phase
    assert "校时状态：成功" in main_window.connection_status.text()
    assert main_window.offset_metric.value_label.text() == "+0.250s"
    assert main_window.rtt_metric.value_label.text() == "50ms"
    assert not main_window.connection_status.isHidden()
    main_window._receive_operation_outcome(main_window._operation_generation, OperationOutcome("sync_clock", {}))
    assert main_window.phase_badge.text() == phase
    assert main_window.sale_countdown.isHidden()


def test_failed_manual_check_keeps_valid_session_and_previous_clock_numbers(main_window, jobs):
    main_window.account_state = "valid"
    main_window._rtt_value, main_window._offset_value = "25ms", "+0.050s"
    main_window.shared_session.cookies.set("existing", "kept")
    main_window._start_connection_operation("check_login")
    main_window._receive_operation_outcome(main_window._operation_generation,
        OperationOutcome("check_login", error="检查失败，请重试"))
    assert main_window.account_state == "valid"
    assert main_window.shared_session.cookies.get("existing") == "kept"
    assert "检查失败" in main_window.connection_status.text()
    assert main_window._rtt_value == "25ms" and main_window._offset_value == "+0.050s"
    assert main_window._task_state == "idle"


@pytest.mark.parametrize("terminal", ["unknown", "failed", "no_ticket", "cancelled", "success"])
@pytest.mark.parametrize("kind,payload", [
    ("clock_sync", {"success": True, "offset_seconds": 0.75, "rtt_ms": 10, "source": "manual"}),
    ("qr_ready", {"image_bytes": b"", "expires_at": 1000}),
    ("qr_status", {"status": "expired"}),
    ("maintenance_availability", {"enabled": True, "busy": False, "login_required": True}),
    ("session_checked", {"state": "expired", "checked_at": 1000}),
    ("finished", {"exit_code": 130}),
])
def test_same_generation_late_maintenance_and_qr_cannot_revive_terminal(main_window, jobs, terminal, kind, payload):
    generation = start(main_window)
    main_window._on_runtime_event("cart_item", CONTEXT, generation=generation)
    if terminal == "success":
        main_window._on_runtime_event("order_success", {**CONTEXT, "order_id": "TEST"}, generation=generation)
    else:
        main_window._set_phase(terminal, "终态原因必须保留")
    snapshot = (main_window._task_state, main_window.phase_badge.text(), main_window.current_cart_item.text(),
                main_window.account_state, main_window._login_required, main_window._maintenance_enabled,
                main_window._server_anchor, main_window._clock_check_text, main_window._session_check_text,
                main_window._rtt_value, main_window._offset_value)
    main_window._on_runtime_event(kind, payload, generation=generation)
    assert (main_window._task_state, main_window.phase_badge.text(), main_window.current_cart_item.text(),
            main_window.account_state, main_window._login_required, main_window._maintenance_enabled,
            main_window._server_anchor, main_window._clock_check_text, main_window._session_check_text,
            main_window._rtt_value, main_window._offset_value) == snapshot
    assert main_window.sale_countdown.isHidden()


@pytest.mark.parametrize("error", ["", "出票后遇到诊断异常"])
def test_confirmed_success_dominates_incorrect_safe_outcome_and_keeps_start_back_locked(main_window, jobs, error):
    generation = start(main_window)
    main_window._on_runtime_event("order_success", {**CONTEXT, "order_id": "TEST"}, generation=generation)
    main_window._receive_operation_outcome(generation, OperationOutcome("task", 130, error, order_state="safe"))
    assert main_window._order_state == "success"
    assert main_window._order_succeeded
    assert main_window._task_state == "success"
    assert not main_window._restart_allowed
    assert not main_window.back_button.isEnabled()
    assert not main_window.start_button.isEnabled()
    main_window._launch_task(deepcopy(main_window._last_run_config))
    assert len(jobs) == 1


@pytest.mark.parametrize("terminal", ["unknown", "success", "no_ticket"])
def test_new_independent_login_check_after_terminal_updates_only_account_details(main_window, jobs, terminal):
    generation = start(main_window)
    if terminal == "unknown":
        outcome = OperationOutcome("task", 130, order_state="unknown", safe_to_restart=False)
    elif terminal == "success":
        outcome = OperationOutcome("task", 0, order_state="success", safe_to_restart=False)
    else:
        outcome = OperationOutcome("task", 1)
    main_window._receive_operation_outcome(generation, outcome)
    phase = main_window.phase_badge.text()
    main_window._start_connection_operation("check_login")
    assert main_window._operation_generation > generation
    main_window._on_runtime_event("session_checked", {"state": "valid", "checked_at": 100},
                                  generation=main_window._operation_generation)
    main_window._receive_operation_outcome(main_window._operation_generation, OperationOutcome("check_login", True))
    assert "已登录" in main_window.connection_status.text()
    assert main_window.phase_badge.text() == phase
    assert main_window._task_state == terminal
