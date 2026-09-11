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
from PySide6.QtWidgets import QApplication  # noqa: E402

from ticket_app import client as client_module  # noqa: E402
from ticket_app.gui import app as gui_app  # noqa: E402
from ticket_app.gui.worker import EventRelay, GuiCancelToken  # noqa: E402
from ticket_app.gui.worker import TicketWorker  # noqa: E402
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
        window.thread = None
        window.worker = None
        window.cancel_token = None
        window.close()
        # ``closeEvent`` owns normal shutdown, but explicitly close here as
        # well so a test that closes the window early cannot leak a listener.
        logging.getLogger().removeHandler(window.log_pipeline.handler)
        window.log_pipeline.close()
        logging.getLogger().setLevel(root_level)
        # Destroy the closed window after resetting lifecycle doubles. Merely
        # hiding it leaves thousands of widgets participating in later theme tests.
        window.deleteLater()
        QApplication.sendPostedEvents(window, QEvent.Type.DeferredDelete)


def _png_bytes() -> bytes:
    image = QImage(12, 12, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.white)
    payload = QByteArray()
    buffer = QBuffer(payload)
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    buffer.close()
    return bytes(payload)


def _click_seat(main_window: gui_app.MainWindow, qtbot, label: str) -> None:
    main_window.seat_types.adapt_to_trains("all")
    checkbox = main_window.seat_types.checkboxes[label]
    main_window.basic_scroll.ensureWidgetVisible(checkbox)
    QApplication.processEvents()
    qtbot.mouseClick(checkbox, Qt.MouseButton.LeftButton)


@pytest.mark.parametrize("sleeper", ["硬卧", "软卧", "高级软卧"])
@pytest.mark.parametrize("mixed", [False, True])
def test_real_sleeper_selection_activates_draft_and_removal_preserves_it(
    main_window: gui_app.MainWindow, qtbot, sleeper: str, mixed: bool
) -> None:
    main_window.show()
    main_window.passengers.setText("张三")
    main_window.preferred_trains.setText("G79")
    main_window.seat_types.set_values(["二等座"] if mixed else [])
    berths = main_window.position_preferences.berths
    main_window.position_preferences.tabs.setCurrentIndex(1)
    berths.set_values({"lower": 1})
    assert "berth_preference" not in main_window._validate_all()
    assert not berths.seat_type_hint.isHidden()

    _click_seat(main_window, qtbot, sleeper)
    qtbot.waitUntil(lambda: "berth_preference" not in main_window._last_validation_errors)
    assert berths.seat_type_hint.isHidden()
    assert main_window._validate_all() == {}
    cfg = main_window._build_current_config()
    assert cfg.seat_types == (["二等座", sleeper] if mixed else [sleeper])
    assert cfg.berth_preference.lower == 1

    main_window.position_preferences.tabs.setCurrentIndex(1)
    _click_seat(main_window, qtbot, sleeper)
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
    original_seats = main_window.seat_types.values()
    main_window.position_preferences.tabs.setCurrentIndex(1)
    main_window.basic_scroll.ensureWidgetVisible(berths)
    berths.set_values({"lower": 1})
    assert "berth_preference" not in main_window._validate_all()
    berths.select_seat_types_button.click()
    assert main_window.config_tabs.currentIndex() == 0
    target = main_window.seat_types.checkboxes["硬卧"]
    qtbot.waitUntil(lambda: QApplication.focusWidget() is target)
    assert main_window.basic_scroll.viewport().rect().intersects(
        target.rect().translated(
            target.mapTo(main_window.basic_scroll.viewport(), target.rect().topLeft())
        )
    )
    assert main_window.seat_types.values() == original_seats
    berths.clear_button.click()
    qtbot.waitUntil(lambda: "berth_preference" not in main_window._last_validation_errors)
    assert berths.values() == {"lower": 0, "middle": 0, "upper": 0}
    assert not berths.clear_button.isEnabled()
    assert main_window.seat_types.values() == original_seats


@pytest.mark.parametrize("separator", [",", "，", "、", ";", "；", "\n", "\r\n", "\t", "，;、\t"])
def test_multi_value_form_collects_and_validates_the_same_lists(
    main_window: gui_app.MainWindow, separator: str
) -> None:
    main_window.seat_types.set_values(["二等座"])
    main_window.passengers.setText(separator + separator.join([" 张三 ", "李四", "Mary Jane"]) + separator)
    main_window.preferred_trains.setText(separator.join(["g79", " D123 ", "k45"]))
    values = main_window._collect_mapping()
    assert values["passenger_names"] == ["张三", "李四", "Mary Jane"]
    assert values["preferred_trains"] == ["G79", "D123", "K45"]
    assert main_window._validate_all() == {}
    cfg = main_window._build_current_config()
    assert cfg.passenger_names == values["passenger_names"]
    assert cfg.preferred_trains == values["preferred_trains"]


def test_main_window_can_be_created_offline_without_starting_a_task(main_window: gui_app.MainWindow) -> None:
    assert main_window.windowTitle() == "12306 Fair Ticket"
    assert main_window.thread is None
    assert main_window.worker is None
    assert main_window.start_button.isEnabled()
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
    assert "version 1" in main_window.import_settings_button.toolTip()
    assert "version 2" in main_window.import_settings_button.toolTip()


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
    main_window.from_station.setText("不存在的车站")
    qtbot.waitUntil(lambda: main_window.from_station.property("validationState") == "error", timeout=1000)

    assert main_window.to_station.property("validationState") in (None, "")
    assert main_window.passengers.property("validationState") in (None, "")
    assert main_window.preferred_trains.property("validationState") in (None, "")


def test_train_checkbox_precedes_its_error_and_uses_one_error_boundary(main_window: gui_app.MainWindow, qtbot) -> None:
    main_window._apply_mapping({**main_window._collect_mapping(), "only_preferred_trains": True})
    main_window.show()
    main_window._validate_all()
    qtbot.wait(20)
    message = main_window.field_messages["preferred_trains"]
    train_input = main_window.preferred_trains

    assert train_input.property("validationState") == "error"
    assert main_window.field_blocks["preferred_trains"].property("validationState") in (None, "")
    assert main_window.only_preferred.mapToGlobal(main_window.only_preferred.rect().topLeft()).x() == train_input.mapToGlobal(train_input.rect().topLeft()).x()
    assert main_window.only_preferred.mapToGlobal(main_window.only_preferred.rect().bottomLeft()).y() < message.mapToGlobal(message.rect().topLeft()).y()


def test_basic_page_has_no_help_icons_and_status_panel_has_no_scroll_area(main_window: gui_app.MainWindow) -> None:
    assert not main_window.basic_scroll.findChildren(gui_app.QToolButton, "helpButton")
    assert main_window.advanced_scroll.findChildren(gui_app.QToolButton, "helpButton")
    assert not any(
        label.text() == "整组位置偏好"
        for label in main_window.position_preferences.parentWidget().findChildren(gui_app.QLabel)
    )
    assert isinstance(main_window.timeline, gui_app.CurrentPhaseWidget)
    assert not main_window.status_panel.findChildren(gui_app.QScrollArea)


def test_status_controls_remain_visible_at_minimum_window_size(main_window: gui_app.MainWindow, qtbot) -> None:
    main_window.resize(1200, 720)
    main_window.show()
    qtbot.wait(30)

    panel_rect = main_window.status_panel.rect()
    for widget in (
        main_window.qr_image,
        main_window.timeline,
        main_window.validate_button,
        main_window.start_button,
        main_window.stop_button,
        main_window.order_button,
    ):
        top_left = widget.mapTo(main_window.status_panel, widget.rect().topLeft())
        bottom_right = widget.mapTo(main_window.status_panel, widget.rect().bottomRight())
        assert panel_rect.contains(top_left)
        assert panel_rect.contains(bottom_right)


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
    main_window.seat_types.set_values(["二等座"])
    main_window.passengers.setText("张三")
    main_window.preferred_trains.setText("G79")
    main_window.from_station.setText("北京西")
    main_window.to_station.setText("郑州东")
    main_window.advanced["query_interval_seconds"].setValue(1.25)  # type: ignore[attr-defined]
    monkeypatch.setattr(gui_app.QFileDialog, "getSaveFileName", lambda *_args: (str(target), ""))
    monkeypatch.setattr(gui_app.QMessageBox, "information", lambda *_args: None)

    main_window._save_settings()

    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["version"] == 3
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
    main_window.config_tabs.setCurrentIndex(1)
    main_window.from_station.setText("不存在的车站")

    errors = main_window._validate_all(focus_first=True)

    assert next(iter(errors)) == "from_station"
    assert main_window.config_tabs.currentIndex() == 0
    qtbot.waitUntil(lambda: QApplication.focusWidget() is main_window.from_station, timeout=1000)


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
    main_window.from_station.setText("不存在的车站")
    errors = main_window._validate_all()

    assert "from_station" in errors
    assert main_window.from_station.property("validationState") == "error"
    assert not main_window.field_messages["from_station"].isHidden()

    main_window.from_station.setText("北京西")
    errors = main_window._validate_all()

    assert "from_station" not in errors
    assert main_window.from_station.property("validationState") in (None, "")
    assert main_window.field_messages["from_station"].isHidden()


def test_all_ten_seat_types_remain_available_and_selected_order_is_visible(main_window: gui_app.MainWindow) -> None:
    seat_list = main_window.seat_types.list

    assert len(main_window.seat_types.checkboxes) == 10
    assert seat_list.count() == len(main_window.seat_types.values())
    assert seat_list.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff


def test_new_draft_has_no_seats_and_empty_trains_allow_all_types(main_window):
    main_window.passengers.setText("张三")
    assert not hasattr(main_window, "empty_train_scope")
    assert "empty_train_scope" not in main_window.field_widgets
    assert main_window.seat_types.values() == []
    assert main_window.seat_types.list.count() == 0
    assert all(not checkbox.isChecked() for checkbox in main_window.seat_types.checkboxes.values())
    assert main_window.position_preferences.seats.positions() == []
    assert main_window.position_preferences.berths.values() == {"lower": 0, "middle": 0, "upper": 0}
    assert not main_window.only_preferred.isChecked()
    assert not main_window.only_preferred.isEnabled()
    assert main_window._validate_all() == {"seat_types": "请至少选择一种席别"}
    main_window.seat_types.set_values(["硬卧", "二等座"])
    assert main_window._validate_all() == {}
    assert main_window._build_current_config().empty_train_scope == "all"
    assert "不限类型" in main_window.range_summary.text()
    main_window.preferred_trains.setText("G123")
    assert main_window.only_preferred.isEnabled()
    assert "含其他类型列车" in main_window.range_summary.text()
    main_window.only_preferred.setChecked(True)
    assert "仅 G123" in main_window.range_summary.text()
    main_window.preferred_trains.clear()
    assert not main_window.only_preferred.isChecked()
    assert not main_window.only_preferred.isEnabled()
    assert main_window._collect_mapping()["empty_train_scope"] == "all"


def test_auto_guidance_preserves_selection_order_and_both_preference_drafts(main_window):
    main_window.passengers.setText("张三")
    main_window.seat_types.set_values(["硬卧", "二等座", "硬座", "无座"])
    main_window.position_preferences.seats.set_positions(["1A"])
    main_window.position_preferences.berths.set_values({"lower": 1})
    original = main_window._collect_mapping()
    main_window.seat_types.groups["seated"].setChecked(False)
    main_window.seat_types.groups["sleeper"].setChecked(True)
    for trains in ("G123；D45", "K123、1461", "G123，K45", "G", "G123;;bad"):
        main_window.preferred_trains.setText(trains)
        assert not main_window.seat_types.groups["seated"].isChecked()
        assert main_window.seat_types.groups["sleeper"].isChecked()
        current = main_window._collect_mapping()
        for key in ("seat_types", "seat_position_preferences", "berth_preference"):
            assert current[key] == original[key]
    assert "preferred_trains" in main_window._validate_all()
    assert "修正车次格式" in main_window.range_summary.text()


def test_priority_preview_changes_strategy_and_config_round_trips(main_window, tmp_path):
    main_window.passengers.setText("张三")
    main_window.preferred_trains.setText("G123，G125")
    main_window.only_preferred.setChecked(True)
    main_window.seat_types.set_values(["二等座", "一等座"])
    assert "G123 二等座 → G123 一等座 → G125 二等座" in main_window.order_preview.text()
    main_window.priority_strategy.setCurrentIndex(main_window.priority_strategy.findData("seat_first"))
    assert "G123 二等座 → G125 二等座 → G123 一等座" in main_window.order_preview.text()
    assert main_window._build_current_config().priority_strategy == "seat_first"
    target = tmp_path / "strategy.json"
    main_window.config_store.save_file(target, main_window._collect_mapping())
    assert json.loads(target.read_text(encoding="utf-8"))["version"] == 3
    main_window.priority_strategy.setCurrentIndex(0)
    errors, _ = main_window._apply_mapping(main_window.config_store.import_file(target))
    assert not errors
    assert main_window._validate_all() == {}
    assert main_window.priority_strategy.currentData() == "seat_first"


def test_imported_empty_exact_scope_is_not_silently_broadened(main_window):
    main_window._apply_mapping({**main_window._collect_mapping(), "only_preferred_trains": True, "empty_train_scope": "all"})
    assert main_window.only_preferred.isChecked()
    assert main_window.only_preferred.isEnabled()  # one explicit uncheck repairs the imported draft
    assert "preferred_trains" in main_window._validate_all()
    main_window.only_preferred.click()
    assert not main_window.only_preferred.isChecked()
    assert not main_window.only_preferred.isEnabled()
    assert "preferred_trains" not in main_window._validate_all()


@pytest.mark.parametrize("scope", [None, "high_speed", "conventional", "future"])
@pytest.mark.parametrize("preferred", [[], ["G1"]])
def test_old_scope_import_is_normalized_and_explained(main_window, monkeypatch, tmp_path, scope, preferred):
    path = tmp_path / "old-preview.json"
    settings = {**main_window._collect_mapping(), "passenger_names": ["张三"], "seat_types": ["硬卧", "二等座"],
                "preferred_trains": preferred, "empty_train_scope": scope}
    path.write_text(json.dumps({"version": 3, "settings": settings}), encoding="utf-8")
    monkeypatch.setattr(gui_app.QFileDialog, "getOpenFileName", lambda *_: (str(path), ""))
    notices = []
    monkeypatch.setattr(gui_app.QMessageBox, "information", lambda _parent, _title, message: notices.append(message))
    main_window._import_settings()
    assert len(notices) == 1
    assert "旧设置已转换为不限类型" in notices[0]
    assert main_window.seat_types.values() == ["硬卧", "二等座"]
    assert main_window._validate_all() == {}
    assert main_window._build_current_config().empty_train_scope == "all"
    assert "empty_train_scope" not in main_window.field_widgets
    main_window.config_store.save_file(path, main_window._collect_mapping())
    assert json.loads(path.read_text(encoding="utf-8"))["settings"]["empty_train_scope"] == "all"


def test_empty_seat_validation_focuses_an_actionable_checkbox(main_window, qtbot):
    main_window.show()
    main_window.passengers.setText("张三")
    main_window.preferred_trains.setText("G123")
    main_window.seat_types.set_values([])
    assert "seat_types" in main_window._validate_all(focus_first=True)
    target = main_window.seat_types.checkboxes["二等座"]
    qtbot.waitUntil(lambda: QApplication.focusWidget() is target)
    qtbot.keyClick(target, Qt.Key.Key_Space)
    assert main_window.seat_types.values() == ["二等座"]
    assert main_window._validate_all() == {}


def test_inactive_preference_counts_do_not_block_until_reactivated(main_window):
    main_window.passengers.setText("张三")
    main_window.preferred_trains.setText("K123")
    main_window.seat_types.set_values(["硬座"])
    main_window.position_preferences.seats.set_positions(["1A", "1F"])
    main_window.position_preferences.berths.set_values({"lower": 2})
    assert main_window._validate_all() == {}
    main_window._build_current_config()
    main_window.seat_types.set_values(["二等座", "硬卧"])
    errors = main_window._validate_all()
    assert "seat_position_preferences" in errors
    assert "berth_preference" in errors


def test_new_policy_controls_are_locked_during_a_task(main_window):
    main_window._set_forms_enabled(False)
    assert not main_window.priority_strategy.isEnabled()
    assert not main_window.seat_types.isEnabled()
    main_window._set_forms_enabled(True)
    assert main_window.priority_strategy.isEnabled()
    assert not main_window.only_preferred.isEnabled()


def test_station_update_is_disabled_and_not_started_while_task_runs(
    main_window: gui_app.MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RunningThread:
        @staticmethod
        def isRunning() -> bool:  # noqa: N802 - mirrors Qt
            return True

    main_window.thread = RunningThread()  # type: ignore[assignment]
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
    main_window.from_station.setText(imported_station)
    assert "from_station" in main_window._validate_all()
    assert main_window.from_station.property("validationState") == "error"
    notices: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gui_app.QMessageBox,
        "information",
        lambda _parent, title, message: notices.append((title, message)),
    )

    main_window._on_station_refresh_finished({imported_station: "TST"}, None)

    assert imported_station in main_window.station_names
    assert "from_station" not in main_window._last_validation_errors
    assert main_window.from_station.property("validationState") in (None, "")
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
        rendered_attempts.append(main_window.query_metric.value_label.text())  # type: ignore[attr-defined]

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

    main_window.thread = RunningThread()  # type: ignore[assignment]
    monkeypatch.setattr(main_window, "_build_current_config", unexpected_config_build)

    main_window._start_task()

    assert main_window._test_network_calls == []  # type: ignore[attr-defined]


def test_worker_completion_without_ticket_notifies_and_shows_one_information_dialog(
    main_window: gui_app.MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    notifications: list[tuple[str, str]] = []
    dialogs: list[tuple[object, str, str]] = []
    monkeypatch.setattr(
        main_window,
        "_notify",
        lambda title, message, sound=True: notifications.append((title, message)),
    )
    monkeypatch.setattr(
        gui_app.QMessageBox,
        "information",
        lambda parent, title, message: dialogs.append((parent, title, message)),
    )

    main_window._on_worker_completed(1)
    # A duplicate completion signal must not produce duplicate user prompts.
    main_window._on_worker_completed(1)

    assert main_window._last_phase == "no_ticket"
    assert "未确认出票" in main_window.phase_badge.text()
    assert notifications == [("任务结束，未出票", "已达停止时间或最大查询轮数，本次未出票。")]
    assert dialogs == [
        (main_window, "任务结束，未出票", "已达停止时间或最大查询轮数，本次未出票。")
    ]


def test_worker_passes_the_process_memory_session_to_the_runner(monkeypatch) -> None:
    shared_session = object()
    received = {}

    class FakeRunner:
        def __init__(self, cfg, event_sink=None, cancel_token=None, session=None):
            received.update(
                cfg=cfg,
                event_sink=event_sink,
                cancel_token=cancel_token,
                session=session,
            )

    monkeypatch.setattr("ticket_app.gui.worker.TicketRunner", FakeRunner)
    relay = EventRelay()
    token = GuiCancelToken()
    worker = TicketWorker(object(), relay, token, shared_session)

    worker._make_runner()

    assert received["session"] is shared_session
    assert received["event_sink"] is relay
    assert received["cancel_token"] is token


def test_closing_the_window_destroys_its_in_memory_cookies(main_window) -> None:
    main_window.shared_session.cookies.set("temporary", "secret")

    main_window.close()

    assert not list(main_window.shared_session.cookies)


def test_runtime_event_relay_preserves_message_data_and_timestamp(qtbot) -> None:
    relay = EventRelay()
    event = RuntimeEvent(
        kind="qr-status",
        message="请在手机上确认",
        data={"status": "scanned", "attempt": 2},
        timestamp=1234.5,
    )

    with qtbot.waitSignal(relay.runtime_event, timeout=1000) as emitted:
        relay(event)

    kind, payload = emitted.args
    assert kind == "qr_status"
    assert payload == {
        "status": "scanned",
        "attempt": 2,
        "message": "请在手机上确认",
        "timestamp": 1234.5,
    }


def test_qr_waiting_scanned_confirmed_expired_and_refresh_states(
    main_window: gui_app.MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    notifications: list[tuple[str, str]] = []
    beeps: list[bool] = []
    monkeypatch.setattr(
        main_window,
        "_notify",
        lambda title, message, sound=True: notifications.append((title, message)),
    )
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
    assert beeps == [True]

    main_window._on_runtime_event("qr_status", {"status": "confirmed", "message": "登录成功"})
    assert main_window._qr_deadline == 0.0
    assert "登录成功" in main_window.qr_image.text()
    assert main_window.qr_countdown.text() == "已确认"
    assert notifications == [("登录成功", "扫码已确认，任务继续运行")]

    main_window._on_runtime_event(
        "qr_ready",
        {"image_bytes": _png_bytes(), "expires_at": deadline, "message": "新二维码"},
    )
    main_window._on_runtime_event("qr_status", {"status": "expired", "message": "二维码失效"})
    assert main_window._qr_deadline == 0.0
    assert main_window.qr_image.text() == "二维码已过期"
    assert main_window.qr_countdown.text() == "已过期"
    assert main_window.refresh_qr_button.isEnabled()

    restarts: list[bool] = []
    monkeypatch.setattr(main_window, "_start_task", lambda: restarts.append(True))
    main_window.thread = None
    main_window._restart_for_qr()
    assert restarts == [True]


def test_reused_login_clears_old_qr_without_notifying_about_a_new_scan(
    main_window: gui_app.MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    notifications = []
    monkeypatch.setattr(main_window, "_notify", lambda *args, **kwargs: notifications.append(args))
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
    monkeypatch.setattr(ServerClock, "sync", lambda *args: None)
    monkeypatch.setattr(StationStore, "load", lambda *args: None)
    monkeypatch.setattr(StationStore, "code", lambda _store, station: station)
    monkeypatch.setattr(TicketRunner, "_resolve_target_start", lambda _runner: None)
    monkeypatch.setattr(client_module.RailwayClient, "_prefetch_login_cookies", lambda _client: None)
    monkeypatch.setattr(gui_app.QMessageBox, "question", lambda *args: gui_app.QMessageBox.StandardButton.Yes)
    failures = []
    for name in ("warning", "critical", "information"):
        monkeypatch.setattr(gui_app.QMessageBox, name, lambda *args: failures.append(args[1:]))
    notifications = []
    monkeypatch.setattr(main_window, "_notify", lambda *args, **kwargs: notifications.append(args))
    shared_session = main_window.shared_session
    checked_sessions = []
    generated_qrs = []
    queried_configs = []
    image_bytes = _png_bytes()

    def check_session_response(url, **kwargs):
        assert url.endswith("/otn/login/checkUser")
        checked_sessions.append(shared_session.cookies.get("test_login") == "valid")
        return SimpleNamespace(json=lambda: {"data": {"flag": checked_sessions[-1]}})

    def create_qr(client):
        assert client.session is shared_session
        generated_qrs.append(client.cfg.preferred_trains)
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

    def query_until_stopped(client, _from_code, _to_code):
        queried_configs.append(client.cfg)
        client.cancel_token.wait(30)
        raise AssertionError("test must stop the running query")

    monkeypatch.setattr(shared_session, "post", check_session_response)
    monkeypatch.setattr(client_module.RailwayClient, "_create_qr_code", create_qr)
    monkeypatch.setattr(client_module.RailwayClient, "_check_qr_status", check_qr)
    monkeypatch.setattr(client_module.RailwayClient, "_complete_login", complete_login)
    monkeypatch.setattr(client_module.RailwayClient, "query_tickets", query_until_stopped)

    main_window.show()
    main_window.from_station.setText("北京西")
    main_window.to_station.setText("郑州东")
    main_window.auto_submit.setChecked(False)
    main_window.preferred_trains.setText("G79")
    main_window.seat_types.set_values(["二等座"])
    assert main_window._validate_all() == {}

    def stop_and_wait():
        if main_window.thread is not None:
            main_window._stop_task()
            qtbot.waitUntil(lambda: main_window.thread is None, timeout=3000)

    try:
        main_window._start_task()
        assert main_window.qr_image.text() == "正在检查登录状态…"
        assert main_window.qr_status.text() == "登录失效时将显示二维码"
        qtbot.waitUntil(lambda: len(queried_configs) == 1 and "登录成功" in main_window.qr_image.text())
        assert notifications == [("登录成功", "扫码已确认，任务继续运行")]
        stop_and_wait()
        assert shared_session.cookies.get("test_login") == "valid"
        assert main_window.preferred_trains.isEnabled()

        main_window.preferred_trains.setText("K123")
        main_window.seat_types.set_values(["硬卧"])
        main_window._start_task()
        qtbot.waitUntil(lambda: len(queried_configs) == 2 and "无需重新扫码" in main_window.qr_image.text())
        assert queried_configs[1].preferred_trains == ["K123"]
        assert queried_configs[1].seat_types == ["硬卧"]
        assert main_window.qr_countdown.text() == "会话有效"
        assert not main_window.refresh_qr_button.isEnabled()
        assert checked_sessions == [False, True]
        assert len(generated_qrs) == 1
        assert notifications == [("登录成功", "扫码已确认，任务继续运行")]
        stop_and_wait()

        shared_session.cookies.clear()
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


def test_refresh_while_running_requests_cancel_before_restart(main_window: gui_app.MainWindow) -> None:
    class RunningThread:
        @staticmethod
        def isRunning() -> bool:  # noqa: N802 - mirrors Qt
            return True

    token = GuiCancelToken()
    main_window.thread = RunningThread()  # type: ignore[assignment]
    main_window.cancel_token = token

    main_window._restart_for_qr()

    assert main_window._pending_restart is True
    assert token.cancelled is True
    assert not main_window.stop_button.isEnabled()


def test_sale_countdown_uses_server_anchor_and_monotonic_elapsed_time(
    main_window: gui_app.MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    monotonic = [52.0]
    fake_time = SimpleNamespace(
        perf_counter=lambda: monotonic[0],
        time=lambda: 9_999_999_999.0,
    )
    monkeypatch.setattr(gui_app, "time", fake_time)
    main_window._server_anchor = (1_700_000_000.0, 50.0)
    main_window._target_timestamp = 1_700_000_005.0

    assert main_window._server_now_timestamp() == 1_700_000_002.0
    main_window._update_countdowns()
    assert main_window.sale_countdown.text() == "00:00:03.0"
    assert main_window.sale_caption.text() == "距离开始时间"

    monotonic[0] = 56.0
    main_window._update_countdowns()
    assert main_window.sale_countdown.text() == "00:00:00.0"
    assert main_window.sale_caption.text() == "已到开始时间"


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
