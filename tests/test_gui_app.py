"""Offline pytest-qt coverage for the desktop window and event state machine."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QEvent, QIODevice, Qt  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QScrollArea, QToolButton  # noqa: E402

from ticket_app import client as client_module  # noqa: E402
from ticket_app.gui import app as gui_app  # noqa: E402
from ticket_app.gui.settings import load_gui_settings, save_gui_settings  # noqa: E402
from ticket_app.gui.worker import GuiCancelToken, OperationRequest, OperationWorker  # noqa: E402
from ticket_app.runtime import RuntimeEvent  # noqa: E402


_REAL_CLIENT_INIT = client_module.RailwayClient.__init__


@pytest.fixture
def main_window(qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[gui_app.MainWindow]:
    root_level = logging.getLogger().level
    network_calls: list[str] = []

    def reject_network(*_args: object, **_kwargs: object) -> None:
        network_calls.append("network")
        raise AssertionError("MainWindow construction must not initialize or request 12306")

    # Guard both the high-level client and requests' common dispatch point.  A
    # failing guard makes an accidental startup request immediately visible.
    monkeypatch.setattr(client_module.RailwayClient, "__init__", reject_network)
    monkeypatch.setattr(client_module.requests.sessions.Session, "request", reject_network)

    local_data_dir = tmp_path / "LocalAppData" / "12306FairTicket"
    monkeypatch.setattr(gui_app, "LOCAL_DATA_DIR", local_data_dir)
    monkeypatch.setattr(gui_app, "STATION_CACHE_FILE", local_data_dir / "stations.json")
    monkeypatch.setattr(gui_app, "cached_station_names", lambda: ["北京西", "郑州东"])

    def disable_tray(window: gui_app.MainWindow) -> None:
        window.tray = None

    monkeypatch.setattr(gui_app.MainWindow, "_setup_tray", disable_tray)
    window = gui_app.MainWindow()
    window._test_network_calls = network_calls  # type: ignore[attr-defined]
    try:
        yield window
    finally:
        window.clock_timer.stop()
        window.validation_timer.stop()
        window.query_ui_timer.stop()
        # Individual lifecycle tests use lightweight thread doubles.  Remove
        # them so closeEvent never opens a modal confirmation in teardown.
        if window._active_operation is not None and window._executor is not None:
            window._stop_task()
            qtbot.waitUntil(lambda: window._active_operation is None, timeout=5000)
        window._active_operation = None
        window.cancel_token = None
        window.close()
        qtbot.waitUntil(lambda: window._background_thread is None, timeout=3000)
        # ``closeEvent`` owns normal shutdown, but explicitly close here as
        # well so a test that closes the window early cannot leak a listener.
        logging.getLogger().removeHandler(window.log_pipeline.handler)
        window.log_pipeline.close()
        logging.getLogger().setLevel(root_level)
        # Destroy the closed window after resetting lifecycle doubles. Merely
        # hiding it leaves thousands of widgets participating in later theme tests.
        window.deleteLater()
        QApplication.sendPostedEvents(window, QEvent.Type.DeferredDelete)


def _system_alert_spy(window, monkeypatch):
    calls = []
    window.tray = SimpleNamespace(showMessage=lambda *args: calls.append(args), hide=lambda: None)
    monkeypatch.setattr(QApplication, "beep", lambda: calls.append("beep"))
    return calls


def _png_bytes() -> bytes:
    image = QImage(12, 12, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.white)
    payload = QByteArray()
    buffer = QBuffer(payload)
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    buffer.close()
    return bytes(payload)


def _add_seat(main_window: gui_app.MainWindow, label: str, *, train: str = "G79", qtbot=None) -> None:
    main_window.from_station.setText("北京西")
    main_window.to_station.setText("郑州东")
    main_window.train_scope.setCurrentIndex(main_window.train_scope.findData("specific" if train else "all"))
    main_window.preferred_trains.setText(train)
    main_window.cart_seat.set_selected_seats([label])
    if qtbot is None:
        assert main_window._add_cart_items()
    else:
        main_window.basic_scroll.ensureWidgetVisible(main_window.add_cart_button)
        QApplication.processEvents()
        qtbot.mouseClick(main_window.add_cart_button, Qt.MouseButton.LeftButton)


def _set_cart_seats(main_window, seats, train="G79"):
    main_window.cart_items = []
    main_window._cart_changed()
    for seat in seats:
        _add_seat(main_window, seat, train=train)


@pytest.mark.parametrize("sleeper", ["硬卧", "软卧", "高级软卧", "一等卧", "二等卧"])
@pytest.mark.parametrize("mixed", [False, True])
def test_real_sleeper_selection_activates_draft_and_removal_preserves_it(
    main_window: gui_app.MainWindow, qtbot, sleeper: str, mixed: bool
) -> None:
    main_window.show()
    main_window.passengers.setText("张三")
    _set_cart_seats(main_window, ["二等座"] if mixed else [])
    berths = main_window.position_preferences.berths
    main_window.position_preferences.tabs.setCurrentIndex(1)
    berths.set_values({"lower": 1})
    assert "berth_preference" not in main_window._validate_all()
    assert not berths.seat_type_hint.isHidden()

    _add_seat(main_window, sleeper, qtbot=qtbot)
    qtbot.waitUntil(lambda: "berth_preference" not in main_window._last_validation_errors)
    assert berths.seat_type_hint.isHidden()
    assert main_window._validate_all() == {}
    cfg = main_window._build_current_config()
    assert cfg.seat_types == (["二等座", sleeper] if mixed else [sleeper])
    assert cfg.berth_preference.lower == 1

    main_window.position_preferences.tabs.setCurrentIndex(1)
    main_window.cart_items = [item for item in main_window.cart_items if item["seat_type"] != sleeper]
    main_window._cart_changed()
    assert "berth_preference" not in main_window._validate_all()
    assert not berths.seat_type_hint.isHidden()
    assert main_window.position_preferences.tabs.currentIndex() == 1
    assert berths.values() == {"lower": 1, "middle": 0, "upper": 0}


def test_berth_guidance_navigates_to_seats_and_clear_keeps_seat_selection(
    main_window: gui_app.MainWindow, qtbot
) -> None:
    main_window.show()
    main_window.passengers.setText("张三")
    main_window.preferred_trains.setText("G79")
    berths = main_window.position_preferences.berths
    original_items = list(main_window.cart_items)
    main_window.position_preferences.tabs.setCurrentIndex(1)
    main_window.basic_scroll.ensureWidgetVisible(berths)
    berths.set_values({"lower": 1})
    assert "berth_preference" not in main_window._validate_all()
    berths.select_seat_types_button.click()
    assert main_window.current_step == 0
    target = main_window.cart_seat.checkboxes["硬卧"]
    qtbot.waitUntil(lambda: QApplication.focusWidget() is target)
    assert main_window.basic_scroll.viewport().rect().intersects(
        target.rect().translated(
            target.mapTo(main_window.basic_scroll.viewport(), target.rect().topLeft())
        )
    )
    assert main_window.cart_items == original_items
    berths.clear_button.click()
    qtbot.waitUntil(lambda: "berth_preference" not in main_window._last_validation_errors)
    assert berths.values() == {"lower": 0, "middle": 0, "upper": 0}
    assert not berths.clear_button.isEnabled()
    assert main_window.cart_items == original_items


@pytest.mark.parametrize("separator", [",", "，", "、", ";", "；", "\n", "\r\n", "\t", "，;、\t"])
def test_multi_value_form_collects_and_validates_the_same_lists(
    main_window: gui_app.MainWindow, separator: str
) -> None:
    main_window.passengers.setText(separator + separator.join([" 张三 ", "李四", "Mary Jane"]) + separator)
    _add_seat(main_window, "二等座", train=separator.join(["g79", " D123 ", "k45"]))
    values = main_window._collect_mapping()
    assert values["passenger_names"] == ["张三", "李四", "Mary Jane"]
    assert [item["train_code"] for item in values["cart_items"]] == ["G79", "D123", "K45"]
    assert main_window._validate_all() == {}
    cfg = main_window._build_current_config()
    assert cfg.passenger_names == values["passenger_names"]
    assert [item.train_code for item in cfg.cart_items] == ["G79", "D123", "K45"]


def test_main_window_can_be_created_offline_without_starting_a_task(main_window: gui_app.MainWindow) -> None:
    assert main_window.windowTitle() == "12306 Fair Ticket"
    assert main_window._active_operation is None
    assert main_window._executor is None
    assert not main_window.start_button.isEnabled()
    assert main_window.next_button.isEnabled()
    assert main_window.advanced["station_cache_days"].minimum() == 1
    assert main_window._test_network_calls == []  # type: ignore[attr-defined]


def test_profile_bar_only_has_save_and_import_json_buttons(main_window: gui_app.MainWindow) -> None:
    profile_bar = main_window.save_settings_button.parentWidget()
    assert profile_bar is not None
    buttons = profile_bar.findChildren(type(main_window.save_settings_button))

    assert [button.text() for button in buttons] == ["保存为…", "导入…"]
    assert not hasattr(main_window, "profile_combo")


def test_configuration_bar_explains_json_and_privacy(main_window: gui_app.MainWindow) -> None:
    assert "JSON" in main_window.save_settings_button.toolTip()
    assert "Cookie" in main_window.save_settings_button.toolTip()
    assert "Token" in main_window.save_settings_button.toolTip()
    assert "JSON" in main_window.import_settings_button.toolTip()
    assert "version 5" in main_window.import_settings_button.toolTip()
    assert "version 5" in main_window.save_settings_button.toolTip()


def test_pristine_form_does_not_show_default_cross_field_errors(main_window: gui_app.MainWindow, qtbot) -> None:
    main_window.show()
    qtbot.wait(320)

    assert main_window.passengers.property("validationState") in (None, "")
    assert main_window.preferred_trains.property("validationState") in (None, "")
    assert main_window.field_messages["passenger_names"].isHidden()
    assert main_window.field_messages["preferred_trains"].isHidden()


def test_live_validation_marks_only_touched_field_and_direct_dependencies(
    main_window: gui_app.MainWindow, qtbot
) -> None:
    main_window.show()
    main_window.passengers.setText("张三、张三")
    qtbot.waitUntil(lambda: main_window.passengers.property("validationState") == "error", timeout=1000)

    assert main_window.to_station.property("validationState") in (None, "")
    assert main_window.from_station.property("validationState") in (None, "")
    assert main_window.preferred_trains.property("validationState") in (None, "")


def test_specific_train_requires_input_without_implicitly_accepting_all(main_window: gui_app.MainWindow, qtbot) -> None:
    main_window.show()
    main_window.cart_seat.set_selected_seats(["二等座"])
    main_window.preferred_trains.clear()
    assert not main_window._add_cart_items()
    assert "指定车次" in main_window.cart_draft_status.text()
    assert not main_window.cart_items
    main_window.train_scope.setCurrentIndex(main_window.train_scope.findData("all"))
    assert main_window._add_cart_items()
    assert main_window.cart_items[0]["train_scope"] == "all"
    assert not main_window.preferred_trains.isEnabled()
    assert not hasattr(main_window, "only_preferred")


def test_basic_page_has_no_help_icons_and_status_panel_has_no_scroll_area(main_window: gui_app.MainWindow) -> None:
    assert main_window.advanced_content.isHidden()
    assert main_window.advanced_content.findChildren(QToolButton, "helpButton")
    assert not any(
        label.text() == "整组位置偏好"
        for label in main_window.position_preferences.parentWidget().findChildren(QLabel)
    )
    assert not hasattr(main_window, "timeline")
    assert isinstance(main_window.phase_badge, QLabel)
    assert not main_window.status_panel.findChildren(QScrollArea)


@pytest.mark.parametrize("dark_theme", [False, True], ids=["light", "dark"])
@pytest.mark.parametrize("window_size", [(640, 480), (900, 700), (1260, 850)])
def test_wizard_navigation_and_footer_remain_visible_when_body_scrolls(
    main_window: gui_app.MainWindow, qtbot, qapp, dark_theme: bool, window_size: tuple[int, int]
) -> None:
    previous_style = qapp.styleSheet()
    try:
        qapp.setStyleSheet(gui_app._load_stylesheet(qapp, dark_theme))
        main_window.account_state = "valid"
        main_window.resize(*window_size)
        main_window.show()
        qtbot.wait(30)
        for step, scroll in enumerate((main_window.basic_scroll, main_window.passenger_scroll,
                                       main_window.confirm_scroll, main_window.run_scroll)):
            main_window._go_to_step(step)
            main_window.logs_toggle.setChecked(True)
            qtbot.wait(10)
            for fraction in (0, 1):
                scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum() * fraction)
                qtbot.wait(10)
                primary = (main_window.next_button, main_window.next_button,
                           main_window.start_button, main_window.back_button)[step]
                for widget in (*main_window.step_buttons, primary):
                    assert widget.isVisible()
                    point = widget.mapTo(main_window, widget.rect().topLeft())
                    assert main_window.rect().contains(widget.rect().translated(point))
                    assert widget.width() >= widget.minimumSizeHint().width()
                assert scroll.horizontalScrollBar().maximum() == 0
    finally:
        qapp.setStyleSheet(previous_style)


def test_log_transport_batch_updates_log_view_once(main_window: gui_app.MainWindow, monkeypatch: pytest.MonkeyPatch) -> None:
    rendered: list[list[tuple[str, str]]] = []
    monkeypatch.setattr(main_window.log_view, "append_lines", lambda lines: rendered.append(list(lines)))

    main_window._on_log_batch((("line 1", "INFO"), ("line 2", "WARNING"), ("bad",)))

    assert rendered == [[("line 1", "INFO"), ("line 2", "WARNING")]]


def test_non_full_refresh_does_not_reveal_untouched_cross_field_errors(
    main_window: gui_app.MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gui_app.QMessageBox, "information", lambda *_args: None)

    main_window._on_station_refresh_finished({"北京西": "BXP", "郑州东": "ZAF"}, None)

    assert main_window.passengers.property("validationState") in (None, "")
    assert main_window.preferred_trains.property("validationState") in (None, "")
    assert main_window.field_messages["passenger_names"].isHidden()
    assert main_window.field_messages["preferred_trains"].isHidden()


def test_save_and_import_buttons_round_trip_every_editable_setting(
    main_window: gui_app.MainWindow,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "all-settings.json"
    main_window.passengers.setText("张三")
    _add_seat(main_window, "二等座")
    main_window.advanced["query_interval_seconds"].setValue(1.25)  # type: ignore[attr-defined]
    monkeypatch.setattr(gui_app.QFileDialog, "getSaveFileName", lambda *_args: (str(target), ""))
    monkeypatch.setattr(gui_app.QMessageBox, "information", lambda *_args: None)

    main_window._save_settings()

    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["version"] == 5
    assert document["settings"]["cart_items"] == main_window.cart_items
    assert document["settings"]["query_interval_seconds"] == 1.25
    serialized = target.read_text(encoding="utf-8").lower()
    assert "session_file" not in serialized
    assert "cookie" not in serialized
    assert "token" not in serialized

    main_window.from_station.setText("郑州东")
    main_window.advanced["query_interval_seconds"].setValue(2.5)  # type: ignore[attr-defined]
    monkeypatch.setattr(gui_app.QFileDialog, "getOpenFileName", lambda *_args: (str(target), ""))
    monkeypatch.setattr(gui_app.QMessageBox, "warning", lambda *_args: None)
    main_window._import_settings()

    assert main_window.from_station.text() == "北京西"
    assert main_window.advanced["query_interval_seconds"].value() == 1.25  # type: ignore[attr-defined]


def test_full_validation_switches_page_and_focuses_the_first_error(
    main_window: gui_app.MainWindow, qtbot
) -> None:
    main_window.show()
    main_window._go_to_step(1)
    main_window.cart_items = [{"from_station": "不存在的车站", "to_station": "郑州东",
                               "train_scope": "specific", "train_code": "G79", "seat_type": "二等座"}]
    main_window._cart_changed()

    errors = main_window._validate_all(focus_first=True)

    assert next(iter(errors)) == "cart_items"
    assert main_window.current_step == 0
    qtbot.waitUntil(lambda: QApplication.focusWidget() is main_window.add_cart_button, timeout=1000)


def test_time_editors_use_three_parts_and_optional_toggles(main_window: gui_app.MainWindow) -> None:
    start = main_window.start_at
    stop = main_window.stop_at
    assert len(start.parts) == len(stop.parts) == 3

    start.setText("12:34:56")
    assert [part.value() for part in start.parts] == [12, 34, 56]
    assert start.text() == "12:34:56"
    assert start.optional_checkbox is not None
    start.optional_checkbox.setChecked(True)
    assert start.text() == ""
    assert not any(part.isEnabled() for part in start.parts)
    start.optional_checkbox.setChecked(False)
    assert start.text() == "12:34:56"

    stop.setText("23:59:58")
    assert stop.optional_checkbox is not None
    stop.optional_checkbox.setChecked(True)
    assert stop.text() == ""
    assert not any(part.isEnabled() for part in stop.parts)


def test_unknown_station_is_marked_and_clears_after_correction(main_window: gui_app.MainWindow) -> None:
    _add_seat(main_window, "二等座")
    main_window.from_station.setText("不存在的车站")
    assert not main_window._add_cart_items()
    assert "出发站" in main_window.cart_draft_status.text()
    assert main_window.cart_items[0]["from_station"] == "北京西"
    # Only committed alternatives are validated for running the task.
    main_window.cart_items[0]["from_station"] = "不存在的车站"
    main_window._cart_changed()
    errors = main_window._validate_all()
    assert "cart_items" in errors
    assert main_window.add_cart_button.property("validationState") == "error"
    assert not main_window.field_messages["cart_items"].isHidden()
    main_window.cart_items[0]["from_station"] = "北京西"
    main_window._cart_changed()
    errors = main_window._validate_all()
    assert "cart_items" not in errors
    assert main_window.add_cart_button.property("validationState") in (None, "")
    assert main_window.field_messages["cart_items"].isHidden()


def test_all_twelve_seat_types_remain_available_and_cart_order_is_visible(main_window: gui_app.MainWindow) -> None:
    assert set(main_window.cart_seat.checkboxes) == set(gui_app.SEAT_SPECS)
    _set_cart_seats(main_window, ["二等卧", "一等卧", "无座"])
    assert [item["seat_type"] for item in main_window.cart_items] == ["二等卧", "一等卧", "无座"]
    assert main_window.cart_button.count == 3
    assert "3" in main_window.cart_button.accessibleName()
    main_window._refresh_confirmation()
    assert "二等卧" in main_window.confirm_cart.text()


def test_new_draft_has_no_cart_and_any_train_requires_explicit_choice(main_window):
    main_window.passengers.setText("张三")
    assert not hasattr(main_window, "empty_train_scope")
    assert "empty_train_scope" not in main_window.field_widgets
    assert main_window.cart_items == []
    assert main_window.cart_seat.selected_seats() == []
    assert main_window.position_preferences.seats.positions() == []
    assert main_window.position_preferences.berths.values() == {"lower": 0, "middle": 0, "upper": 0}
    assert main_window.train_scope.currentData() == "specific"
    assert "cart_items" in main_window._validate_all()
    _set_cart_seats(main_window, ["硬卧", "二等座"], train="")
    assert main_window._validate_all() == {}
    assert all(item.train_scope == "all" for item in main_window._build_current_config().cart_items)
    assert all(item["train_scope"] == "all" for item in main_window.cart_items)
    main_window._refresh_confirmation()
    assert "不限车次" in main_window.confirm_cart.text()
    assert "only_preferred_trains" not in main_window._collect_mapping()
    assert "priority_strategy" not in main_window._collect_mapping()


def test_unadded_train_edits_preserve_cart_order_and_both_preference_drafts(main_window):
    main_window.passengers.setText("张三")
    _set_cart_seats(main_window, ["硬卧", "二等座", "硬座", "无座"])
    main_window.position_preferences.seats.set_positions(["1A"])
    main_window.position_preferences.berths.set_values({"lower": 1})
    original = main_window._collect_mapping()
    for trains in ("G123；D45", "K123、1461", "G123，K45", "G", "G123;;bad"):
        main_window.preferred_trains.setText(trains)
        current = main_window._collect_mapping()
        for key in ("cart_items", "seat_position_preferences", "berth_preference"):
            assert current[key] == original[key]
    assert main_window._validate_all() == {}
    assert not main_window._add_cart_items()
    assert main_window._collect_mapping()["cart_items"] == original["cart_items"]


def test_cart_order_is_confirmed_and_config_round_trips(main_window, tmp_path):
    main_window.passengers.setText("张三")
    _set_cart_seats(main_window, ["二等座", "一等座"], train="G123，G125")
    main_window.cart_items[1], main_window.cart_items[2] = main_window.cart_items[2], main_window.cart_items[1]
    main_window._cart_changed()
    expected = list(main_window.cart_items)
    main_window._refresh_confirmation()
    summary = main_window.confirm_cart.text()
    assert summary.index("G123 · 二等座") < summary.index("G123 · 一等座") < summary.index("G125 · 二等座")
    target = tmp_path / "cart.json"
    save_gui_settings(target, main_window._collect_mapping())
    assert json.loads(target.read_text(encoding="utf-8"))["version"] == 5
    main_window.cart_items.reverse()
    errors = main_window._apply_mapping(load_gui_settings(target))
    assert not errors
    assert main_window._validate_all() == {}
    assert main_window.cart_items == expected
    assert [item.to_mapping() for item in main_window._build_current_config().cart_items] == expected


@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_old_config_import_is_rejected_without_changing_current_cart(main_window, monkeypatch, tmp_path, version):
    _set_cart_seats(main_window, ["二等座"])
    before = main_window._collect_mapping()
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"version": version, "settings": {"seat_types": ["硬卧"]}}), encoding="utf-8")
    monkeypatch.setattr(gui_app.QFileDialog, "getOpenFileName", lambda *_: (str(path), ""))
    notices = []
    monkeypatch.setattr(gui_app.QMessageBox, "warning", lambda _parent, _title, message: notices.append(message))
    main_window._import_settings()
    assert len(notices) == 1 and "5" in notices[0]
    assert main_window._collect_mapping() == before


def test_empty_cart_validation_focuses_add_action(main_window, qtbot):
    main_window.show()
    main_window.passengers.setText("张三")
    main_window.preferred_trains.setText("G123")
    assert "cart_items" in main_window._validate_all(focus_first=True)
    target = main_window.add_cart_button
    qtbot.waitUntil(lambda: QApplication.focusWidget() is target)
    main_window.cart_seat.set_selected_seats(["二等座"])
    qtbot.keyClick(target, Qt.Key.Key_Space)
    assert [item["seat_type"] for item in main_window.cart_items] == ["二等座"]
    assert main_window._validate_all() == {}


def test_inactive_preference_counts_do_not_block_until_reactivated(main_window):
    main_window.passengers.setText("张三")
    _set_cart_seats(main_window, ["硬座"], train="K123")
    main_window.position_preferences.seats.set_positions(["1A", "1F"])
    main_window.position_preferences.berths.set_values({"lower": 2})
    assert main_window._validate_all() == {}
    main_window._build_current_config()
    _set_cart_seats(main_window, ["二等座", "硬卧"])
    errors = main_window._validate_all()
    assert "seat_position_preferences" in errors
    assert "berth_preference" in errors


def test_new_policy_controls_are_locked_during_a_task(main_window):
    main_window._set_forms_enabled(False)
    assert not main_window.train_scope.isEnabled()
    assert not main_window.cart_seat.isEnabled()
    assert not main_window.add_cart_button.isEnabled()
    main_window._set_forms_enabled(True)
    assert main_window.train_scope.isEnabled()
    assert main_window.cart_seat.isEnabled()
    assert main_window.add_cart_button.isEnabled()


def test_station_update_is_disabled_and_not_started_while_task_runs(
    main_window: gui_app.MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RunningThread:
        @staticmethod
        def isRunning() -> bool:  # noqa: N802 - mirrors Qt
            return True

    main_window._active_operation = RunningThread()  # type: ignore[assignment]
    main_window._set_forms_enabled(False)
    assert not main_window.update_stations_button.isEnabled()
    assert not main_window.swap_stations_button.isEnabled()

    def unexpected_worker(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("station update must not start while task is running")

    monkeypatch.setattr(gui_app, "StationRefreshWorker", unexpected_worker)
    main_window._refresh_stations()
    assert main_window.station_refresh_worker is None


def test_successful_station_refresh_updates_completers_and_repairs_validation(
    main_window: gui_app.MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    imported_station = "测试新站"
    _add_seat(main_window, "二等座")
    main_window.from_station.setText(imported_station)
    main_window.cart_items[0]["from_station"] = imported_station
    main_window._cart_changed()
    assert "cart_items" in main_window._validate_all()
    assert main_window.add_cart_button.property("validationState") == "error"
    notices: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gui_app.QMessageBox,
        "information",
        lambda _parent, title, message: notices.append((title, message)),
    )

    main_window._on_station_refresh_finished({imported_station: "TST"}, None)

    assert imported_station in main_window.station_names
    assert "cart_items" not in main_window._last_validation_errors
    assert main_window.add_cart_button.property("validationState") in (None, "")
    completer = main_window.from_station.completer()
    assert completer is not None
    names = {
        completer.model().data(completer.model().index(row, 0))
        for row in range(completer.model().rowCount())
    }
    assert imported_station in names
    assert notices == [("站点已更新", f"已载入 {len(main_window.station_names)} 个站名。")]


def test_failed_station_refresh_keeps_existing_stations_and_reports_reason(
    main_window: gui_app.MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing_names = set(main_window.station_names)
    warnings: list[tuple[object, str, str]] = []
    monkeypatch.setattr(
        gui_app.QMessageBox,
        "warning",
        lambda parent, title, message: warnings.append((parent, title, message)),
    )

    main_window._on_station_refresh_finished(None, RuntimeError("离线测试失败"))

    assert main_window.station_names == existing_names
    assert warnings == [
        (main_window, "更新站点失败", "现有站点数据未改变。\n\n离线测试失败")
    ]


def test_main_configuration_scroll_areas_never_show_horizontal_scrollbars(main_window: gui_app.MainWindow) -> None:
    assert main_window.basic_scroll.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert main_window.advanced_scroll.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff


def test_high_frequency_query_events_are_rendered_once_per_batch(main_window: gui_app.MainWindow, qtbot) -> None:
    rendered_attempts: list[str] = []
    original_flush = main_window._flush_query_event

    def track_flush() -> None:
        original_flush()
        rendered_attempts.append(main_window.query_count.text())  # type: ignore[attr-defined]

    main_window.query_ui_timer.timeout.disconnect()
    main_window.query_ui_timer.timeout.connect(track_flush)
    for attempt in range(1, 41):
        main_window._on_runtime_event("query", {"attempt": attempt, "message": f"查询 {attempt}"})

    assert main_window.query_ui_timer.isActive()
    assert rendered_attempts == []
    qtbot.waitUntil(lambda: not main_window.query_ui_timer.isActive(), timeout=1000)
    assert rendered_attempts == ["40"]


def test_start_is_a_noop_while_an_existing_task_is_running(
    main_window: gui_app.MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RunningThread:
        @staticmethod
        def isRunning() -> bool:  # noqa: N802 - mirrors Qt
            return True

    def unexpected_config_build() -> None:
        raise AssertionError("a second task attempted to build or launch")

    main_window._active_operation = RunningThread()  # type: ignore[assignment]
    monkeypatch.setattr(main_window, "_build_current_config", unexpected_config_build)

    main_window._start_task()

    assert main_window._test_network_calls == []  # type: ignore[attr-defined]


def test_worker_completion_is_quiet_and_shows_one_information_dialog(
    main_window: gui_app.MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialogs: list[tuple[object, str, str]] = []
    notifications = _system_alert_spy(main_window, monkeypatch)
    monkeypatch.setattr(
        gui_app.QMessageBox,
        "information",
        lambda parent, title, message: dialogs.append((parent, title, message)),
    )

    main_window._on_worker_completed(1)
    # A duplicate completion signal must not produce duplicate user prompts.
    main_window._on_worker_completed(1)

    assert main_window._last_phase == "no_ticket"
    assert "结束未出票" in main_window.phase_badge.text()
    assert notifications == []
    assert dialogs == [
        (main_window, "任务结束，未出票", "已达停止时间或最大查询轮数，本次未出票。")
    ]


def test_worker_passes_the_process_memory_session_and_clock_to_the_runner(monkeypatch) -> None:
    shared_session, shared_clock, cfg = object(), object(), object()
    received = {}

    class FakeRunner:
        def __init__(self, cfg, event_sink=None, cancel_token=None, session=None, clock=None):
            received.update(cfg=cfg, event_sink=event_sink, cancel_token=cancel_token,
                            session=session, clock=clock)

        def run(self):
            return 1

    monkeypatch.setattr("ticket_app.gui.worker.TicketRunner", FakeRunner)
    token = GuiCancelToken()
    worker = OperationWorker()
    completed = []
    worker.finished.connect(lambda generation, outcome: completed.append((generation, outcome)))
    worker.execute(OperationRequest(3, "task", cfg, token, shared_session, shared_clock))

    assert received["cfg"] is cfg
    assert received["session"] is shared_session
    assert received["clock"] is shared_clock
    assert callable(received["event_sink"])
    assert received["cancel_token"] is token
    assert len(completed) == 1
    assert completed[0][0] == 3 and completed[0][1].result == 1
    assert not completed[0][1].error


def test_closing_the_window_destroys_its_in_memory_cookies(main_window) -> None:
    main_window.shared_session.cookies.set("temporary", "secret")

    main_window.close()

    assert not list(main_window.shared_session.cookies)


def test_worker_runtime_event_preserves_generation_message_data_and_timestamp(qtbot, monkeypatch) -> None:
    event = RuntimeEvent(
        kind="qr-status", message="请在手机上确认",
        data={"status": "scanned", "attempt": 2}, timestamp=1234.5,
    )

    class FakeRunner:
        def __init__(self, cfg, event_sink=None, **_kwargs):
            self.sink = event_sink

        def run(self):
            self.sink(event)
            return 1

    monkeypatch.setattr("ticket_app.gui.worker.TicketRunner", FakeRunner)
    worker = OperationWorker()
    events = []
    worker.runtime_event.connect(lambda generation, kind, payload: events.append((generation, kind, payload)))
    worker.execute(OperationRequest(8, "task", object(), GuiCancelToken()))
    assert events[-1] == (8, "qr_status", {
        "status": "scanned", "attempt": 2,
        "message": "请在手机上确认", "timestamp": 1234.5,
    })


def test_qr_waiting_scanned_confirmed_expired_and_refresh_states(
    main_window: gui_app.MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    beeps: list[bool] = []
    notifications = _system_alert_spy(main_window, monkeypatch)
    monkeypatch.setattr(QApplication, "beep", lambda: beeps.append(True))

    deadline = gui_app.time.time() + 60.0
    main_window._on_runtime_event(
        "qr_ready",
        {"image_bytes": _png_bytes(), "expires_at": deadline, "message": "等待扫码"},
    )
    assert not main_window.qr_image.pixmap().isNull()
    assert main_window.qr_status.text() == "等待扫码"
    assert main_window._qr_deadline == deadline
    assert not main_window.refresh_qr_button.isEnabled()

    main_window._on_runtime_event("qr_status", {"status": "waiting", "message": "尚未扫描"})
    assert main_window.qr_status.text() == "尚未扫描"

    main_window._on_runtime_event("qr_status", {"status": "scanned", "message": "已扫描，请确认"})
    assert main_window.qr_status.text() == "已扫描，请确认"
    assert beeps == []

    main_window._on_runtime_event("qr_status", {"status": "confirmed", "message": "登录成功"})
    assert main_window._qr_deadline == 0.0
    assert "登录成功" in main_window.qr_image.text()
    assert main_window.qr_countdown.text() == "已确认"
    assert notifications == []

    main_window._on_runtime_event(
        "qr_ready",
        {"image_bytes": _png_bytes(), "expires_at": deadline, "message": "新二维码"},
    )
    main_window._on_runtime_event("qr_status", {"status": "expired", "message": "二维码失效"})
    assert main_window._qr_deadline == 0.0
    assert main_window.qr_image.text() == "二维码已过期"
    assert main_window.qr_countdown.text() == "已过期"
    assert main_window.refresh_qr_button.isEnabled()

    restarts: list[str] = []
    monkeypatch.setattr(main_window, "_start_connection_operation", lambda mode: restarts.append(mode))
    main_window._active_operation = None
    main_window._restart_for_qr()
    assert restarts == ["login"]


def test_reused_login_clears_old_qr_without_notifying_about_a_new_scan(
    main_window: gui_app.MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    notifications = _system_alert_spy(main_window, monkeypatch)
    main_window._on_runtime_event(
        "qr_ready",
        {"image_bytes": _png_bytes(), "expires_at": gui_app.time.time() + 60},
    )
    # An old expired QR may have left its refresh button enabled.
    main_window.refresh_qr_button.setEnabled(True)

    main_window._on_runtime_event(
        "qr_status",
        {"status": "logged_in", "message": "当前登录会话仍然有效，无需重新扫码"},
    )
    main_window._update_countdowns()

    assert main_window.qr_image.pixmap().isNull()
    assert main_window.qr_image.text() == "✓\n已登录\n无需重新扫码"
    assert main_window.qr_status.text() == "当前登录会话仍然有效，无需重新扫码"
    assert main_window.qr_countdown.text() == "会话有效"
    assert main_window._qr_deadline == 0.0
    assert not main_window.refresh_qr_button.isEnabled()
    assert notifications == []


def test_start_stop_edit_restart_reuses_login_and_expired_session_requests_qr(
    main_window: gui_app.MainWindow, monkeypatch: pytest.MonkeyPatch, qtbot
) -> None:
    """Run real workers and Qt event delivery with only network inputs replaced."""
    from ticket_app.clock import ServerClock
    from ticket_app.runner import TicketRunner
    from ticket_app.stations import StationStore

    # Restore real client construction for this integration path. The fixture's
    # requests.Session.request guard remains active to reject accidental I/O.
    monkeypatch.setattr(client_module.RailwayClient, "__init__", _REAL_CLIENT_INIT)
    monkeypatch.setattr(ServerClock, "sync", lambda *args, **kwargs: True)
    monkeypatch.setattr(StationStore, "load", lambda *args: None)
    monkeypatch.setattr(StationStore, "code", lambda _store, station: station)
    monkeypatch.setattr(TicketRunner, "_resolve_target_start", lambda _runner: None)
    monkeypatch.setattr(client_module.RailwayClient, "_prefetch_login_cookies", lambda _client: None)
    monkeypatch.setattr(gui_app.QMessageBox, "question", lambda *args: gui_app.QMessageBox.StandardButton.Yes)
    failures = []
    for name in ("warning", "critical", "information"):
        monkeypatch.setattr(gui_app.QMessageBox, name, lambda *args: failures.append(args[1:]))
    notifications = _system_alert_spy(main_window, monkeypatch)
    shared_session = main_window.shared_session
    checked_sessions = []
    generated_qrs = []
    queried_configs = []
    image_bytes = _png_bytes()

    def check_session_response(url, **kwargs):
        assert url.endswith("/otn/login/checkUser")
        checked_sessions.append(shared_session.cookies.get("test_login") == "valid")
        return SimpleNamespace(status_code=200, json=lambda: {"data": {"flag": checked_sessions[-1]}})

    def create_qr(client):
        assert client.session is shared_session
        generated_qrs.append(client.cfg.cart_items)
        return image_bytes, f"test-qr-{len(generated_qrs)}"

    def check_qr(client, _uuid):
        if len(generated_qrs) == 1:
            return "2", "confirmed"
        # After the stored session expires, leave the new QR awaiting a scan.
        client.cancel_token.wait(30)
        raise AssertionError("test must stop the task while the replacement QR is displayed")

    def complete_login(client):
        client.session.cookies.set("test_login", "valid")
        return True, "OK"

    def query_until_stopped(client, _from_code, _to_code, before_request=None):
        queried_configs.append(client.cfg)
        client.cancel_token.wait(30)
        raise AssertionError("test must stop the running query")

    monkeypatch.setattr(shared_session, "post", check_session_response)
    monkeypatch.setattr(client_module.RailwayClient, "_create_qr_code", create_qr)
    monkeypatch.setattr(client_module.RailwayClient, "_check_qr_status", check_qr)
    monkeypatch.setattr(client_module.RailwayClient, "_complete_login", complete_login)
    monkeypatch.setattr(client_module.RailwayClient, "query_tickets_result", query_until_stopped)

    main_window.show()
    main_window.from_station.setText("北京西")
    main_window.to_station.setText("郑州东")
    main_window.auto_submit.setChecked(False)
    main_window.start_at.set_disabled(True)
    main_window.stop_at.set_disabled(True)
    _set_cart_seats(main_window, ["二等座"], train="G79")
    assert main_window._validate_all() == {}

    def stop_and_wait():
        if main_window._active_operation is not None:
            main_window._stop_task()
            qtbot.waitUntil(lambda: main_window._active_operation is None, timeout=3000)

    try:
        main_window.account_state = "valid"
        main_window._go_to_step(2)
        main_window._start_task()
        assert main_window.qr_image.text() == "正在检查登录状态…"
        assert main_window.qr_status.text() == "登录失效时将显示二维码"
        qtbot.waitUntil(lambda: len(queried_configs) == 1 and "登录成功" in main_window.qr_image.text())
        assert notifications == []
        stop_and_wait()
        assert shared_session.cookies.get("test_login") == "valid"
        assert main_window.preferred_trains.isEnabled()

        _set_cart_seats(main_window, ["硬卧"], train="K123")
        main_window.account_state = "valid"
        main_window._go_to_step(2)
        main_window._start_task()
        qtbot.waitUntil(lambda: len(queried_configs) == 2 and "无需重新扫码" in main_window.qr_image.text())
        assert [item.train_code for item in queried_configs[1].cart_items] == ["K123"]
        assert queried_configs[1].seat_types == ["硬卧"]
        assert main_window.qr_countdown.text() == "会话有效"
        assert not main_window.refresh_qr_button.isEnabled()
        assert checked_sessions == [False, True]
        assert len(generated_qrs) == 1
        assert notifications == []
        stop_and_wait()

        shared_session.cookies.clear()
        main_window.account_state = "valid"
        main_window._go_to_step(2)
        main_window._start_task()
        qtbot.waitUntil(lambda: main_window.qr_status.text() == "等待扫码")
        assert not main_window.qr_image.pixmap().isNull()
        assert main_window._qr_deadline > gui_app.time.time()
        assert checked_sessions == [False, True, False]
        assert len(generated_qrs) == 2
        assert len(queried_configs) == 2
        assert main_window._test_network_calls == []
        assert failures == []
    finally:
        stop_and_wait()


def test_refresh_while_waiting_queues_login_without_cancelling_task(main_window: gui_app.MainWindow) -> None:
    class RunningThread:
        @staticmethod
        def isRunning() -> bool:  # noqa: N802 - mirrors Qt
            return True

    token = GuiCancelToken()
    main_window._active_operation = RunningThread()  # type: ignore[assignment]
    main_window.cancel_token = token
    main_window._operation_mode = "task"
    main_window._maintenance_enabled = True
    token.set_actions_enabled(True)

    main_window._restart_for_qr()

    assert main_window._pending_restart is False
    assert token.is_cancelled is False
    assert token.next_action(0) == "login"


def test_sale_countdown_uses_server_anchor_and_monotonic_elapsed_time(
    main_window: gui_app.MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    monotonic = [52.0]
    fake_time = SimpleNamespace(
        perf_counter=lambda: monotonic[0],
        time=lambda: 9_999_999_999.0,
    )
    monkeypatch.setattr(gui_app, "time", fake_time)
    main_window._go_to_step(3)
    main_window._set_phase("waiting", "等待开售")
    main_window._server_anchor = (1_700_000_000.0, 50.0)
    main_window._target_timestamp = 1_700_000_005.0

    assert main_window._server_now_timestamp() == 1_700_000_002.0
    main_window._update_countdowns()
    assert main_window.sale_countdown.text() == "00:00:03.0"
    assert main_window.sale_caption.text() == "距离开售"

    monotonic[0] = 56.0
    main_window._update_countdowns()
    assert main_window.sale_countdown.text() == ""
    assert main_window.sale_caption.text() == ""
    assert main_window.sale_countdown.isHidden()
    assert main_window.sale_caption.isHidden()


def test_gui_smoke_mode_exits_offline_and_uses_temporary_local_appdata(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    local_appdata = tmp_path / "LocalAppData"
    script = "\n".join(
        (
            "import requests.sessions",
            "def reject_network(*args, **kwargs):",
            "    raise AssertionError('smoke test attempted a network request')",
            "requests.sessions.Session.request = reject_network",
            "from ticket_app.client import RailwayClient",
            "RailwayClient.__init__ = reject_network",
            "from ticket_app.gui.app import run_gui",
            "raise SystemExit(run_gui(['gui.py', '--smoke-test']))",
        )
    )
    environment = dict(os.environ)
    environment["LOCALAPPDATA"] = str(local_appdata)
    environment["QT_QPA_PLATFORM"] = "offscreen"

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not list(local_appdata.rglob("*.cookies"))
    assert not list(local_appdata.rglob("login_qr.png"))


def test_runtime_events_never_send_system_alerts_but_keep_dialogs(main_window, monkeypatch):
    alerts = _system_alert_spy(main_window, monkeypatch)
    dialogs = []
    monkeypatch.setattr(gui_app.QMessageBox, "information", lambda *args: dialogs.append(args[1]))
    monkeypatch.setattr(gui_app.QMessageBox, "critical", lambda *args: dialogs.append(args[1]))
    for _ in range(20):
        main_window._on_runtime_event("candidate", {"message": "发现二等卧票源"})
    assert main_window.phase_badge.text() == "查询余票"
    assert "发现二等卧票源" in main_window.phase_badge.toolTip()
    for status in ("waiting", "scanned", "confirmed", "logged_in", "expired"):
        main_window._on_runtime_event("qr_status", {"status": status})
    main_window._on_worker_failed("模拟失败", "测试")
    main_window._on_runtime_event("order_success", {"order_id": "test-order"})
    main_window._on_runtime_event("order_success", {"order_id": "test-order"})
    assert main_window.order_button.isEnabled()
    assert dialogs == ["任务失败", "出票成功"]
    assert alerts == []
