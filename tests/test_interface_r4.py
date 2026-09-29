"""Direct status feedback, persistent passenger input and active-tab geometry."""

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QAbstractButton

from test_gui_app import main_window  # noqa: F401
from test_compact_runtime import dialogs, jobs  # noqa: F401
from ticket_app.gui.app import _load_stylesheet
from ticket_app.gui.worker import OperationOutcome


@pytest.mark.parametrize("failed_request", [False, True])
def test_metrics_retain_successful_values_after_recalibration_failure(main_window, jobs, failed_request):
    main_window._go_to_step(3)
    cards = (main_window.query_metric, main_window.rtt_metric, main_window.offset_metric)
    assert [card.value_label.text() for card in cards] == ["0", "--", "--"]
    assert all(not card.findChildren(QAbstractButton) for card in cards)
    assert all(card.focusPolicy() == Qt.FocusPolicy.NoFocus for card in cards)
    main_window._start_connection_operation("sync_clock")
    main_window._on_runtime_event("clock_sync", {
        "success": True, "offset_seconds": -0.125, "rtt_ms": 43,
        "checked_at": 100, "server_timestamp": 100, "monotonic_timestamp": 50,
    })
    main_window._receive_operation_outcome(main_window._operation_generation, OperationOutcome("sync_clock", {}))
    anchor = main_window._server_anchor
    main_window._start_connection_operation("sync_clock")
    if failed_request:
        outcome = OperationOutcome("sync_clock", error="网络连接失败")
    else:
        main_window._on_runtime_event("clock_sync", {
            "success": False, "offset_seconds": 99, "rtt_ms": 9999, "checked_at": 200,
        })
        outcome = OperationOutcome("sync_clock", {})
    main_window._receive_operation_outcome(main_window._operation_generation, outcome)
    assert [card.value_label.text() for card in cards] == ["0", "43ms", "-0.125s"]
    assert main_window._server_anchor == anchor
    assert not main_window.connection_status.isHidden()
    assert "失败，保留上次校准" in main_window.connection_status.text()
    assert "·" in main_window.connection_status.text()  # Check time is shown directly.
    main_window._start_connection_operation("check_login")
    main_window._on_runtime_event("session_checked", {"state": "valid", "checked_at": 300})
    main_window._receive_operation_outcome(main_window._operation_generation, OperationOutcome("check_login", True))
    assert "已登录" in main_window.connection_status.text()
    assert "失败，保留上次校准" in main_window.connection_status.text()
    assert main_window._test_network_calls == []


def test_name_input_survives_login_failure_import_and_back_navigation(main_window, jobs, qtbot):
    main_window._go_to_step(1)
    main_window.show()
    assert main_window.passengers.isVisible() and main_window.passengers.isEnabled()
    assert not hasattr(main_window, "manual_toggle")
    main_window._start_connection_operation("login")
    assert main_window.passengers.isVisible() and not main_window.passengers.isEnabled()
    main_window._receive_operation_outcome(main_window._operation_generation, OperationOutcome("login", {
        "authenticated": True, "contacts": None, "contacts_error": "读取失败",
    }))
    assert main_window.account_state == "valid"
    assert main_window.passengers.isVisible() and main_window.passengers.isEnabled()
    main_window._on_connection_completed("contacts", {"authenticated": True, "contacts": [
        {"name": "张三", "passenger_type": "1"}, {"name": "李四", "passenger_type": "3"},
    ]})
    for index in (1, 0):
        main_window.contact_selector.checkboxes[index].click()
    assert main_window._collect_mapping()["passenger_names"] == ["李四", "张三"]
    main_window.passengers.setText("张三；李四")
    assert main_window.contact_selector.selected_names() == ["张三", "李四"]
    assert list(main_window.passenger_ticket_types.rows) == ["张三", "李四"]
    saved = main_window._collect_mapping()
    main_window.passengers.clear()
    main_window._apply_mapping(saved)
    main_window._go_to_step(2)
    main_window.back_button.click()
    qtbot.wait(20)
    assert main_window.current_step == 1
    assert main_window.passengers.isVisible()
    assert main_window._collect_mapping()["passenger_names"] == ["张三", "李四"]
    assert main_window._test_network_calls == []


@pytest.mark.parametrize("seats", [("二等座",), ("商务座",), ("二等座", "二等卧")])
@pytest.mark.parametrize("width", [640, 750])
def test_preference_height_tracks_visible_tab_wrapping_and_errors(main_window, qtbot, qapp, seats, width):
    previous = qapp.styleSheet()
    try:
        qapp.setStyleSheet(_load_stylesheet(qapp, True))
        main_window.cart_items = [
            {"from_station": "北京西", "to_station": "郑州东", "train_scope": "specific",
             "train_code": "D17", "seat_type": seat} for seat in seats
        ]
        main_window._cart_changed()
        main_window.passengers.setText("张三")
        main_window._go_to_step(2)
        main_window.resize(width, 880)
        main_window.show()
        prefs = main_window.position_preferences
        prefs.seats.set_positions(["1A"])
        prefs.tabs.setCurrentIndex(0)
        qtbot.wait(30)
        original_height = prefs.tabs.height()
        # Long, hidden berth guidance must not reserve space on the seat page.
        prefs.berths.help_details.set_details("铺位说明。" * 100)
        prefs.berths.help_details.button.setChecked(True)
        qtbot.wait(30)
        assert prefs.tabs.height() == original_height
        for index in (1, 0):
            prefs.tabs.setCurrentIndex(index)
            qtbot.wait(30)
            page = prefs.tabs.currentWidget()
            assert page.height() >= page.layout().totalHeightForWidth(page.width())
        assert prefs.tabs.height() == original_height
        guide = prefs.seats.guide
        assert guide.height() >= guide.heightForWidth(guide.width())
        clear_bottom = prefs.seats.clear_button.mapTo(main_window, prefs.seats.clear_button.rect().bottomLeft()).y()
        quiet_top = main_window.quiet_carriage.mapTo(main_window, QPoint()).y()
        assert 0 < quiet_top - clear_bottom <= 20
        main_window._apply_validation({"seat_position_preferences": "座位偏好数量应与乘车人数一致。" * 5})
        qtbot.wait(30)
        error = main_window.field_messages["seat_position_preferences"]
        assert not error.isHidden()
        assert error.height() >= error.heightForWidth(error.width())
        assert main_window.quiet_carriage.mapTo(main_window, QPoint()).y() > error.mapTo(main_window, error.rect().bottomLeft()).y()
        main_window._apply_validation({})
        qtbot.wait(30)
        assert prefs.tabs.height() == original_height
        assert prefs.seats.positions() == ["1A"]
    finally:
        qapp.setStyleSheet(previous)
