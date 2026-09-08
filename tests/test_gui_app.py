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

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from ticket_app import client as client_module  # noqa: E402
from ticket_app.gui import app as gui_app  # noqa: E402
from ticket_app.gui.worker import EventRelay, GuiCancelToken  # noqa: E402
from ticket_app.gui.worker import TicketWorker  # noqa: E402
from ticket_app.runtime import RuntimeEvent  # noqa: E402


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


def _png_bytes() -> bytes:
    image = QImage(12, 12, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.white)
    payload = QByteArray()
    buffer = QBuffer(payload)
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    buffer.close()
    return bytes(payload)


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


def test_save_and_import_buttons_round_trip_every_editable_setting(
    main_window: gui_app.MainWindow,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "all-settings.json"
    main_window.passengers.setText("张三")
    main_window.preferred_trains.setText("G79")
    main_window.from_station.setText("北京西")
    main_window.to_station.setText("郑州东")
    main_window.advanced["query_interval_seconds"].setValue(1.25)  # type: ignore[attr-defined]
    monkeypatch.setattr(gui_app.QFileDialog, "getSaveFileName", lambda *_args: (str(target), ""))
    monkeypatch.setattr(gui_app.QMessageBox, "information", lambda *_args: None)

    main_window._save_settings()

    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["version"] == 2
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


def test_all_ten_seat_types_are_expanded_without_internal_scrolling(main_window: gui_app.MainWindow) -> None:
    seat_list = main_window.seat_types.list

    assert seat_list.count() == 10
    assert seat_list.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert seat_list.height() >= sum(seat_list.sizeHintForRow(index) for index in range(seat_list.count()))


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
