"""Keep confirmation and navigation aligned with imported or emptied carts."""

from test_gui_app import main_window  # noqa: F401 - offline GUI fixture


def prepare_confirmation(window):
    window.cart_items = [{
        "from_station": "北京西", "to_station": "郑州东", "train_scope": "specific",
        "train_code": "G103", "seat_type": "二等座",
    }]
    window._cart_changed()
    window.passengers.setText("张三")
    window.account_state = "valid"
    window._go_to_step(2)
    assert "G103 · 二等座" in window.confirm_cart.text()
    assert window.start_button.isEnabled()


def assert_empty_cart_guidance(window):
    assert not window.start_button.isEnabled()
    assert not window.flow_error.isHidden()
    assert "购物车" in window.flow_error.text()
    assert "第一步" in window.flow_error.text()
    assert window._active_operation is None


def test_import_on_confirmation_replaces_route_train_seat_and_applicable_preferences(main_window):
    prepare_confirmation(main_window)
    imported = main_window._collect_mapping()
    imported["cart_items"] = [{
        "from_station": "郑州东", "to_station": "北京西", "train_scope": "specific",
        "train_code": "D17", "seat_type": "二等卧",
    }]

    main_window._apply_mapping(imported)

    assert main_window.current_step == 2
    summary = main_window.confirm_cart.text()
    assert "郑州东 → 北京西 · D17 · 二等卧" in summary
    assert "G103" not in summary
    assert "北京西 → 郑州东" not in summary
    tabs = main_window.position_preferences.tabs
    assert [tabs.tabText(i) for i in (0, 1)] == ["座位偏好（0项）", "铺位偏好（1项）"]
    assert main_window.position_preferences.seats.grid.isHidden()
    assert main_window.position_preferences.berths.spins["lower"].isEnabled()
    config = main_window._build_current_config()
    actual = config.cart_items[0]
    assert (actual.from_station, actual.to_station, actual.train_code, actual.seat_type) == (
        "郑州东", "北京西", "D17", "二等卧")
    assert main_window.start_button.isEnabled()
    assert main_window._active_operation is None


def test_emptying_cart_on_passenger_page_cannot_advance_and_keeps_return_guidance(main_window):
    prepare_confirmation(main_window)
    main_window._go_to_step(1)
    main_window.cart_items = []
    main_window._cart_changed()
    assert_empty_cart_guidance(main_window)

    main_window._next_step()

    assert main_window.current_step == 1
    assert_empty_cart_guidance(main_window)


def test_directly_revisiting_confirmation_with_empty_cart_has_explicit_guidance(main_window):
    prepare_confirmation(main_window)
    main_window._go_to_step(0)
    main_window.cart_items = []
    main_window._cart_changed()

    main_window._go_to_step(2)

    assert main_window.current_step == 2
    assert main_window.confirm_cart.items() == []
    assert main_window.confirm_cart.isHidden()
    assert "G103" not in main_window.confirm_cart.text()
    assert_empty_cart_guidance(main_window)


def test_importing_empty_cart_on_confirmation_removes_old_values_and_blocks_start(main_window, monkeypatch):
    prepare_confirmation(main_window)
    imported = main_window._collect_mapping()
    imported["cart_items"] = []
    dispatched = []
    monkeypatch.setattr(main_window, "_dispatch_operation", lambda *args, **kwargs: dispatched.append(args))

    main_window._apply_mapping(imported)

    assert main_window.current_step == 2
    assert main_window._collect_mapping()["cart_items"] == []
    assert main_window.cart_button.count == 0
    assert main_window.confirm_cart.items() == []
    assert main_window.confirm_cart.isHidden()
    assert "G103" not in main_window.confirm_cart.text()
    assert all("0项" in main_window.position_preferences.tabs.tabText(i) for i in (0, 1))
    assert_empty_cart_guidance(main_window)
    main_window.start_button.click()
    assert dispatched == []
