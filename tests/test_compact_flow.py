"""Offline integration of the compact preparation pages and their saved data."""

from copy import deepcopy

from PySide6.QtCore import Qt

from test_gui_app import main_window  # noqa: F401 - rejects every network request


def fill_cart_draft(window, trains="G103，D17", seats=("二等座", "二等卧")):
    window.from_station.setText("北京西")
    window.to_station.setText("郑州东")
    window.preferred_trains.setText(trains)
    window.cart_seat.set_selected_seats(seats)


def sign_in(window):
    window._on_connection_completed("login", {
        "authenticated": True,
        "contacts": [{"name": "张三", "passenger_type": "1"},
                     {"name": "李四", "passenger_type": "3"},
                     {"name": "王五", "passenger_type": "1"}],
    })


def test_preparation_shows_controls_and_only_required_preference_text(main_window):
    assert main_window.workflow_status.text() == ""
    assert main_window.workflow_status.isHidden()
    assert main_window.cart_draft_status.isHidden()
    assert not hasattr(main_window, "cart_migration_label")
    assert main_window.cart_preview.isHidden()
    assert not main_window.cart_preview_toggle.isChecked()
    assert main_window.add_cart_button.text() == "加入购物车"
    assert not hasattr(main_window, "cart_preview_title")
    assert main_window.cart_seat.selected_seats() == []
    assert main_window.field_blocks["passenger_ticket_types"].isHidden()
    assert not hasattr(main_window, "preference_help")
    assert not hasattr(main_window, "quiet_help")
    assert main_window.preference_note.text() == "偏好仅在对应席别和服务端支持时生效，不改变车次或席别；无法满足时接受系统分配。"
    assert main_window.position_preferences.berths.help_details.details_label.isHidden()
    assert main_window._active_operation is None
    assert main_window._test_network_calls == []


def test_add_count_preview_and_short_feedback_preserve_complete_expansion(main_window, qtbot):
    fill_cart_draft(main_window, trains="G103，D17，G105")
    assert main_window.add_cart_button.text() == "加入购物车（6项）"
    assert main_window.cart_preview.isHidden()
    qtbot.mouseClick(main_window.cart_preview_toggle, Qt.MouseButton.LeftButton)
    assert not main_window.cart_preview.isHidden()
    preview = main_window.cart_preview.text().splitlines()
    assert len(preview) == 6
    assert [row.split(" · ")[-2:] for row in preview] == [
        ["G103", "二等座"], ["D17", "二等座"], ["G105", "二等座"],
        ["G103", "二等卧"], ["D17", "二等卧"], ["G105", "二等卧"],
    ]
    main_window.add_cart_button.click()
    assert len(main_window.cart_items) == 6
    assert main_window.add_cart_button.text() == "已加入"
    assert main_window.cart_draft_status.isHidden()
    qtbot.waitUntil(lambda: main_window.add_cart_button.text() == "加入购物车", timeout=1800)
    original = deepcopy(main_window.cart_items)
    main_window.add_cart_button.click()
    assert main_window.add_cart_button.text() == "已在购物车"
    assert main_window.cart_items == original
    assert main_window.cart_button.count == 6
    qtbot.mouseClick(main_window.cart_preview_toggle, Qt.MouseButton.LeftButton)
    assert main_window.cart_preview.isHidden()


def test_feedback_timer_does_not_replace_new_draft_count(main_window, qtbot):
    fill_cart_draft(main_window, trains="G103", seats=("二等座",))
    main_window.add_cart_button.click()
    assert main_window.add_cart_button.text() == "已加入"
    main_window.preferred_trains.setText("G103，G105，G107")
    assert main_window.add_cart_button.text() == "加入购物车（2项）"
    qtbot.wait(1300)
    assert main_window.add_cart_button.text() == "加入购物车（2项）"
    assert len(main_window.cart_items) == 1
    assert len(main_window.cart_preview.text().splitlines()) == 3


def test_passenger_manual_input_shares_row_and_ticket_types_show_only_with_names(main_window, qtbot):
    fill_cart_draft(main_window)
    main_window._add_cart_items()
    main_window._go_to_step(1)
    main_window.show()
    sign_in(main_window)
    assert main_window.account_heading.text() == "已登录"
    assert main_window.contacts_status.isHidden()
    assert main_window.contact_selector.title.text() == "乘车人（0/5）"
    assert main_window.field_blocks["passenger_ticket_types"].isHidden()
    qtbot.wait(20)
    manual_block = main_window.field_blocks["passenger_names"]
    assert main_window.manual_row.indexOf(main_window.passenger_name_label) == 0
    assert main_window.manual_row.indexOf(manual_block) == 1
    toggle_rect = main_window.passenger_name_label.geometry()
    block_rect = manual_block.geometry()
    assert toggle_rect.right() < block_rect.left()
    assert toggle_rect.top() < block_rect.bottom() and block_rect.top() < toggle_rect.bottom()
    main_window.passengers.setText("李四，张三，王五")
    assert main_window.contact_selector.title.text() == "乘车人（3/5）"
    assert not main_window.field_blocks["passenger_ticket_types"].isHidden()
    editor = main_window.passenger_ticket_types
    assert list(editor.rows) == ["李四", "张三", "王五"]
    assert [editor.rows[name].currentText() for name in editor.rows] == [
        "12306默认（学生）", "12306默认（成人）", "12306默认（成人）",
    ]
    assert main_window._active_operation is None
    assert main_window._task_state == "idle"
    main_window.passengers.clear()
    assert main_window.contact_selector.title.text() == "乘车人（0/5）"
    assert main_window.field_blocks["passenger_ticket_types"].isHidden()


def test_confirmation_lists_all_items_and_cart_edits_refresh_applicable_preferences(main_window):
    fill_cart_draft(main_window, trains="G103，D17，G105，G107")
    main_window._add_cart_items()
    sign_in(main_window)
    main_window.passengers.setText("张三")
    main_window._go_to_step(2)
    assert main_window.confirm_summary.text().count("\n") == 2
    assert "张三（成人）" in main_window.confirm_summary.text()
    assert "G103" not in main_window.confirm_summary.text()
    assert main_window.confirm_cart.items() == main_window.cart_items
    assert len(main_window.confirm_cart.text().splitlines()) == 8
    assert main_window.confirm_cart.text().splitlines()[-1].endswith("G107 · 二等卧")
    assert not hasattr(main_window, "confirm_migration")
    tabs = main_window.position_preferences.tabs
    assert [tabs.tabText(i) for i in (0, 1)] == ["座位偏好（4项）", "铺位偏好（4项）"]
    # The saved cart is the source for both the visible order and preference
    # applicability; removing a seat group must not retain its old scope.
    main_window.cart_items = list(reversed(main_window.cart_items[4:]))
    main_window._cart_changed()
    assert main_window.confirm_cart.items() == main_window.cart_items
    assert main_window.confirm_cart.text().splitlines()[0].endswith("G107 · 二等卧")
    assert "二等座" not in main_window.confirm_cart.text()
    assert [tabs.tabText(i) for i in (0, 1)] == ["座位偏好（0项）", "铺位偏好（4项）"]
    assert main_window.position_preferences.seats.grid.isHidden()
    assert main_window.position_preferences.berths.spins["lower"].isEnabled()
    assert main_window.start_button.isEnabled()
    assert main_window._build_current_config().cart_items[0].seat_type == "二等卧"
    assert main_window._active_operation is None


def test_return_to_passengers_and_change_ticket_type_updates_compact_confirmation(main_window):
    fill_cart_draft(main_window)
    main_window._add_cart_items()
    sign_in(main_window)
    main_window.passengers.setText("李四，张三")
    main_window._go_to_step(2)
    assert "李四（学生）" in main_window.confirm_summary.text()
    main_window.back_button.click()
    combo = main_window.passenger_ticket_types.rows["李四"]
    combo.setCurrentIndex(combo.findData("adult"))
    main_window._next_step()
    assert main_window.current_step == 2
    assert "李四（成人）" in main_window.confirm_summary.text()
    assert "李四（学生）" not in main_window.confirm_summary.text()
    assert main_window._collect_mapping()["passenger_ticket_types"] == {"李四": "adult"}
    assert main_window._active_operation is None
