"""Offline acceptance checks for the floating cart and multi-seat entry."""

from copy import deepcopy

import pytest
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from test_gui_app import main_window  # noqa: F401 - isolated, network-rejecting fixture
from ticket_app.gui import cart_flow
from ticket_app.gui.worker import GuiCancelToken, OperationOutcome, OperationRequest


def fill_entry(window, trains="G103，D17", seats=()):
    window.from_station.setText("北京西")
    window.to_station.setText("郑州东")
    window.train_scope.setCurrentIndex(window.train_scope.findData("specific"))
    window.preferred_trains.setText(trains)
    window.cart_seat.set_selected_seats(seats)


def tuples(items):
    return [(item["train_code"], item["seat_type"]) for item in items]


def test_click_order_expands_seats_then_trains_and_rechecking_appends(main_window, qtbot):
    main_window.show()
    fill_entry(main_window)
    selector = main_window.cart_seat
    qtbot.mouseClick(selector.checkboxes["二等座"], Qt.MouseButton.LeftButton)
    qtbot.mouseClick(selector.checkboxes["二等卧"], Qt.MouseButton.LeftButton)
    assert tuples(main_window._draft_cart_items()) == [
        ("G103", "二等座"), ("D17", "二等座"), ("G103", "二等卧"), ("D17", "二等卧"),
    ]
    assert "4" in main_window.add_cart_button.text()
    preview = main_window.cart_preview.text()
    assert preview.index("G103 · 二等座") < preview.index("D17 · 二等座") < preview.index("G103 · 二等卧")
    qtbot.mouseClick(selector.checkboxes["二等座"], Qt.MouseButton.LeftButton)
    qtbot.mouseClick(selector.checkboxes["二等座"], Qt.MouseButton.LeftButton)
    assert selector.selected_seats() == ["二等卧", "二等座"]
    assert main_window._add_cart_items()
    assert tuples(main_window.cart_items) == [
        ("G103", "二等卧"), ("D17", "二等卧"), ("G103", "二等座"), ("D17", "二等座"),
    ]
    previous = deepcopy(main_window.cart_items)
    assert main_window._add_cart_items()
    assert main_window.cart_items == previous
    assert main_window.cart_button.count == 4


def test_any_train_makes_one_item_per_clicked_seat_and_preserves_input(main_window):
    fill_entry(main_window, seats=["二等卧", "一等座"])
    main_window.train_scope.setCurrentIndex(main_window.train_scope.findData("all"))
    assert main_window._add_cart_items()
    assert tuples(main_window.cart_items) == [("", "二等卧"), ("", "一等座")]
    assert all(item["train_scope"] == "all" for item in main_window.cart_items)
    assert main_window.preferred_trains.text() == "G103，D17"
    assert main_window.cart_seat.selected_seats() == ["二等卧", "一等座"]


def test_unadded_question_names_exact_route_trains_seats_and_count(main_window):
    fill_entry(main_window, trains="G101", seats=["二等座"])
    main_window._add_cart_items()
    fill_entry(main_window, seats=["二等座", "二等卧"])
    seen = []

    def examine_and_cancel():
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, QMessageBox)
        seen.append(dialog.text())
        assert {button.text() for button in dialog.buttons()} == {
            "加入并继续", "不加入，继续下一步", "返回修改",
        }
        next(button for button in dialog.buttons() if button.text() == "返回修改").click()

    QTimer.singleShot(0, examine_and_cancel)
    assert not main_window._handle_unadded_cart_draft()
    assert len(seen) == 1
    assert all(text in seen[0] for text in ("北京西 → 郑州东", "G103、D17", "二等座、二等卧", "4 个备选"))
    assert "尚未处理" not in seen[0]
    assert tuples(main_window.cart_items) == [("G101", "二等座")]


@pytest.mark.parametrize("remove_all", [False, True])
def test_confirmation_cart_popup_refreshes_order_scope_and_start_availability(main_window, monkeypatch, remove_all):
    fill_entry(main_window, trains="G103", seats=["二等座"])
    main_window._add_cart_items()
    fill_entry(main_window, trains="D17", seats=["二等卧"])
    main_window._add_cart_items()
    main_window.account_state = "valid"
    main_window.passengers.setText("张三")
    main_window._go_to_step(2)
    assert main_window.start_button.isEnabled()
    assert all("1项" in main_window.position_preferences.tabs.tabText(i) for i in (0, 1))
    expected = [] if remove_all else list(reversed(deepcopy(main_window.cart_items)))

    class EditedDialog:
        def __init__(self, items, *args, **kwargs):
            assert not kwargs["read_only"]
            assert len(items) == 2

        def exec(self):
            return QDialog.DialogCode.Accepted

        def items(self):
            return expected

    monkeypatch.setattr(cart_flow, "CartDialog", EditedDialog)
    main_window._open_cart()
    assert main_window.cart_items == expected
    assert main_window.cart_button.count == len(expected)
    if remove_all:
        assert main_window.confirm_cart.items() == []
        assert main_window.confirm_cart.text() == ""
        assert main_window.confirm_cart.isHidden()
        assert "购物车" in main_window.flow_error.text()
        assert all("0项" in main_window.position_preferences.tabs.tabText(i) for i in (0, 1))
        assert not main_window.start_button.isEnabled()
    else:
        text = main_window.confirm_cart.text()
        assert text.index("D17 · 二等卧") < text.index("G103 · 二等座")
        assert all("1项" in main_window.position_preferences.tabs.tabText(i) for i in (0, 1))
        assert main_window.position_preferences.seats.buttons["1A"].isEnabled()
        assert main_window.position_preferences.berths.spins["lower"].isEnabled()
        assert main_window.start_button.isEnabled()


@pytest.mark.parametrize("key", [Qt.Key.Key_Space, Qt.Key.Key_Return])
def test_floating_cart_count_keyboard_and_position_across_preparation_pages(main_window, qtbot, monkeypatch, key):
    fill_entry(main_window, seats=["二等座", "二等卧"])
    main_window._add_cart_items()
    opened = []

    class ReadDialog:
        def __init__(self, items, *args, **kwargs):
            opened.append((deepcopy(items), kwargs["read_only"]))

        def exec(self):
            return QDialog.DialogCode.Rejected

    monkeypatch.setattr(cart_flow, "CartDialog", ReadDialog)
    main_window.show()
    for step, scroll in enumerate((main_window.basic_scroll, main_window.passenger_scroll, main_window.confirm_scroll)):
        main_window._go_to_step(step)
        qtbot.wait(15)
        shortcut = main_window.cart_button
        assert shortcut.isVisible()
        assert shortcut.count == 4 and "4 个备选" in shortcut.accessibleName()
        before = shortcut.mapTo(main_window, shortcut.rect().topLeft())
        scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
        qtbot.wait(15)
        assert shortcut.mapTo(main_window, shortcut.rect().topLeft()) == before
        shortcut.setFocus()
        qtbot.keyClick(shortcut, key)
        assert len(opened) == step + 1
        assert opened[-1][1] is False
        assert len(opened[-1][0]) == 4
    main_window._go_to_step(3)
    assert main_window.cart_button.isHidden()


@pytest.mark.parametrize("focus_name", ["preferred_trains", "advanced"])
def test_auxiliary_start_completion_and_release_keep_scroll_position(main_window, qtbot, monkeypatch, focus_name):
    jobs = []

    def dispatch(mode, cfg):
        token = GuiCancelToken()
        request = OperationRequest(main_window._operation_generation, mode, cfg, token,
                                   main_window.shared_session, main_window.shared_clock)
        main_window.cancel_token = token
        main_window._active_operation = request
        jobs.append(request)
        main_window._update_connection_controls()

    monkeypatch.setattr(main_window, "_dispatch_operation", dispatch)
    main_window.resize(750, 880)
    main_window.show()
    main_window.advanced_toggle.setChecked(True)
    target = main_window.preferred_trains if focus_name == "preferred_trains" else main_window.advanced["query_interval_seconds"]
    scroll = main_window.basic_scroll
    for _ in range(3):
        scroll.ensureWidgetVisible(target)
        target.setFocus()
        qtbot.wait(30)
        before = scroll.verticalScrollBar().value()
        main_window._start_connection_operation("sync_clock")
        qtbot.wait(30)
        assert scroll.verticalScrollBar().value() == before
        main_window._receive_operation_outcome(main_window._operation_generation, OperationOutcome("sync_clock", {}))
        qtbot.wait(30)
        assert scroll.verticalScrollBar().value() == before
        assert main_window._active_operation is None
        assert main_window.current_step == 0
    assert len(jobs) == 3
    assert main_window._test_network_calls == []
