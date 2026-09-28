"""Offline coverage for passenger choices and independent connection actions."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QThread
from shiboken6 import getCppPointer, isValid

from test_gui_app import main_window  # noqa: F401 - shared isolated window fixture
from ticket_app.configuration import AppError, ConnectionConfig
from ticket_app.gui import app as gui_app
from ticket_app.gui import worker as worker_module
from ticket_app.gui.compat import load_gui_settings, save_gui_settings
from ticket_app.gui.passenger_widgets import InlinePassengerSelector, contact_display_rows
from ticket_app.gui.worker import ConnectionWorker, EventRelay, GuiCancelToken, OperationOutcome


@pytest.fixture(autouse=True)
def no_unexpected_message_boxes(monkeypatch):
    """Turn unexpected async failures into assertions, never native modals.

    An assertion in a worker double (including a delayed Event.wait timeout)
    travels through the application's failure signal. Without this guard its
    message box can block the test's event loop indefinitely and hide the error.
    Tests intentionally exercising an error may install their own dialog spy.
    """
    unexpected = []
    for name in ("warning", "critical", "information", "question"):
        def capture(*args, _name=name, **_kwargs):
            unexpected.append((_name, args[1:3]))
            return gui_app.QMessageBox.StandardButton.Cancel

        monkeypatch.setattr(gui_app.QMessageBox, name, capture)
    yield
    assert unexpected == [], f"Unexpected modal dialogs: {unexpected!r}"


def test_ticket_choices_follow_names_and_removed_people_lose_overrides(main_window):
    main_window.passengers.setText("学生甲、成人乙")
    assert main_window.passenger_ticket_types.values() == {}
    student = main_window.passenger_ticket_types.rows["学生甲"]
    student.setCurrentIndex(student.findData("adult"))
    main_window.passengers.setText("成人乙；学生甲")
    assert main_window._collect_mapping()["passenger_ticket_types"] == {"学生甲": "adult"}
    main_window.passengers.setText("成人乙")
    assert main_window.passenger_ticket_types.values() == {}
    main_window.passengers.setText("成人乙，成人乙")
    assert "passenger_names" in main_window._validate_all()


def test_account_checkboxes_keep_click_order_enforce_five_and_block_ambiguous_names(qtbot):
    contacts = [{"name": f"乘车人{i}", "passenger_type": "1"} for i in range(7)]
    contacts += [{"name": "同名", "passenger_type": value} for value in ("1", "3")]
    selector = InlinePassengerSelector()
    selector.set_contacts(contacts)
    qtbot.addWidget(selector)
    for index in (3, 1, 5, 0, 2, 6):
        selector.checkboxes[index].click()
    assert selector.selected_names() == ["乘车人3", "乘车人1", "乘车人5", "乘车人0", "乘车人2"]
    assert not selector.checkboxes[6].isChecked()
    selector.checkboxes[1].click()
    selector.checkboxes[1].click()
    assert selector.selected_names()[-1] == "乘车人1"
    assert all(not item.isEnabled() for item in selector.checkboxes[-2:])


def test_contact_worker_boundary_removes_identity_data():
    assert contact_display_rows([{
        "passenger_name": "测试甲", "passenger_type": "3", "passenger_id_no": "PRIVATE-ID",
        "mobile_no": "PRIVATE-MOBILE", "allEncStr": "PRIVATE-TOKEN",
    }]) == [{"name": "测试甲", "passenger_type": "3"}]


def test_v4_roundtrip_new_settings_and_old_versions_remain_compatible(main_window, tmp_path):
    assert main_window.seat_types.values() == []
    assert not main_window.quiet_carriage.isChecked()
    main_window.passengers.setText("测试甲，测试乙")
    combo = main_window.passenger_ticket_types.rows["测试甲"]
    combo.setCurrentIndex(combo.findData("student"))
    main_window.quiet_carriage.setChecked(True)
    path = tmp_path / "trip.json"
    values = main_window._collect_mapping()
    values.update(contacts=[{"id": "PRIVATE-ID"}], cookie="PRIVATE-COOKIE", checked_at="PRIVATE-TIME")
    save_gui_settings(path, values)
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["version"] == 4
    assert document["settings"]["passenger_ticket_types"] == {"测试甲": "student"}
    assert document["settings"]["quiet_carriage_preference"] is True
    assert "PRIVATE" not in path.read_text(encoding="utf-8")
    main_window._apply_mapping(load_gui_settings(path))
    assert main_window.passenger_ticket_types.values() == {"测试甲": "student"}
    assert main_window.quiet_carriage.isChecked()
    for version in (1, 2, 3):
        path.write_text(json.dumps({"version": version, "values" if version == 1 else "settings": {
            "passenger_names": ["测试甲"], "seat_types": ["二等座"],
        }}), encoding="utf-8")
        old = load_gui_settings(path)
        assert old["passenger_ticket_types"] == {}
        assert old["quiet_carriage_preference"] is False


@pytest.mark.parametrize("overrides", [None, [], {"测试甲": {"id": "PRIVATE"}}, {"测试甲": "child"}])
def test_malformed_ticket_choices_cannot_be_persisted(tmp_path, overrides):
    with pytest.raises(AppError):
        save_gui_settings(tmp_path / "bad.json", {"passenger_ticket_types": overrides})


@pytest.mark.parametrize("mode", ["check_login", "login", "contacts", "sync_clock"])
def test_independent_worker_never_creates_ticket_runner(qtbot, monkeypatch, mode):
    calls = []
    relay = EventRelay()
    token = GuiCancelToken()
    cfg = ConnectionConfig.from_mapping({"persist_session": False})
    session = object()

    class Client:
        def __init__(self, config, sink, cancel, shared):
            assert config is cfg and sink is relay and cancel is token and shared is session

        def check_session(self):
            calls.append("check")
            return False

        def ensure_login(self, check_first=True):
            calls.append(("login", check_first))

        def get_passengers(self):
            calls.append("contacts")
            return [{"passenger_name": "测试甲", "passenger_type": "3", "passenger_id_no": "PRIVATE"}]

    class Clock:
        def __init__(self, shared, config):
            assert shared is session and config is cfg

        def sync(self, cancel, sink, **kwargs):
            assert kwargs == {"budget_seconds": 10.0, "source": "manual"}
            calls.append("clock")
            return True

    monkeypatch.setattr(worker_module, "RailwayClient", Client)
    monkeypatch.setattr(worker_module, "ServerClock", Clock)
    monkeypatch.setattr(worker_module, "TicketRunner", lambda *_args, **_kwargs: pytest.fail("No ticket runner"))
    worker = ConnectionWorker(mode, cfg, relay, token, session)
    completed = []
    worker.completed.connect(lambda action, result: completed.append((action, result)))
    worker.run()
    expected = {"check_login": ["check"], "login": [("login", False), "contacts"],
                "contacts": ["check"], "sync_clock": ["clock"]}
    assert calls == expected[mode]
    assert completed[0][0] == mode
    if mode == "contacts":
        assert completed[0][1] == {"authenticated": False, "contacts": None}
    elif mode == "login":
        assert completed[0][1] == {
            "authenticated": True, "contacts": [{"name": "测试甲", "passenger_type": "3"}],
        }


def test_idle_status_command_with_empty_draft_uses_shared_executor_and_never_scans(main_window, qtbot, monkeypatch):
    calls = []

    class Client:
        def __init__(self, cfg, relay, cancel, session):
            self.relay = relay
            assert isinstance(cfg, ConnectionConfig)
            assert session is main_window.shared_session
            assert not hasattr(cfg, "from_station")

        def check_session(self):
            assert QThread.currentThread() is not gui_app.QApplication.instance().thread()
            calls.append("check")
            self.relay("session_checked", {"state": "expired", "checked_at": 100})
            return False

        def ensure_login(self, **_kwargs):
            pytest.fail("Status checks must not initiate QR login")

    monkeypatch.setattr(worker_module, "RailwayClient", Client)
    main_window.passengers.clear()
    main_window.from_station.clear()
    main_window.seat_types.set_values([])
    main_window._go_to_step(1)
    main_window._start_connection_operation("check_login")
    qtbot.waitUntil(lambda: main_window._active_operation is None)
    assert calls == ["check"]
    assert "失效" in main_window.session_check_status.text()
    assert main_window.login_button.isEnabled()
    assert main_window.refresh_qr_button.isEnabled()
    assert "任务未开始" in main_window.workflow_status.text()
    assert main_window._background_thread.isRunning()
    assert isValid(main_window._executor)
    assert main_window._test_network_calls == []


def test_waiting_commands_are_serial_and_forbidden_during_query(main_window):
    token = GuiCancelToken()
    token.set_actions_enabled(True)
    main_window._active_operation = SimpleNamespace(mode="task")
    main_window.cancel_token = token
    main_window._operation_mode = "task"
    main_window._on_runtime_event("maintenance_availability", {"enabled": True, "busy": False, "login_required": False})
    assert main_window.check_login_button.isEnabled()
    main_window._start_connection_operation("check_login")
    assert token.next_action(0) == "check_login"
    main_window._start_connection_operation("sync_clock")
    assert token.next_action(0) is None
    assert not main_window.sync_clock_button.isEnabled()
    token.set_actions_enabled(False)
    main_window._on_runtime_event("maintenance_availability", {"enabled": False, "busy": False, "login_required": False})
    main_window._start_connection_operation("login")
    assert token.next_action(0) is None
    assert not main_window.login_button.isEnabled()
    assert not token.cancelled


def test_failed_recalibration_keeps_anchor_and_login_valid_replaces_stale_qr(main_window):
    main_window._on_runtime_event("clock_sync", {"success": False, "checked_at": 100})
    assert "本地时间" in main_window.clock_check_status.text()
    main_window._on_runtime_event("clock_sync", {
        "success": True, "offset_seconds": 0.5, "rtt_ms": 20, "server_timestamp": 100, "monotonic_timestamp": 90,
    })
    main_window._on_runtime_event("clock_sync", {
        "success": False, "offset_seconds": 0, "rtt_ms": None, "server_timestamp": 300, "monotonic_timestamp": 290,
    })
    assert main_window._server_anchor == (100, 90)
    assert main_window.offset_metric.value_label.text() == "+0.500s"
    assert main_window.rtt_metric.value_label.text() == "20ms"
    assert "保留上次校准" in main_window.clock_check_status.text()
    main_window._on_runtime_event("session_checked", {"state": "expired"})
    main_window._on_runtime_event("session_checked", {"state": "valid"})
    assert "检查时已登录" in main_window.session_check_status.text()
    assert "已登录" in main_window.qr_image.text()
    assert main_window.qr_countdown.text() == "会话有效"


def test_stopping_real_async_contact_fetch_discards_late_event_and_result(main_window, qtbot, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    contact_updates = []

    class Client:
        def __init__(self, cfg, relay, token, session):
            self.relay = relay

        def check_session(self):
            return True

        def ensure_login(self, **_kwargs):
            pytest.fail("Refreshing contacts must not open QR login")

        def get_passengers(self):
            started.set()
            assert release.wait(3)
            # Simulate an in-flight response arriving after Stop was clicked.
            self.relay("session_checked", {"state": "valid", "checked_at": 100})
            self.relay("clock_sync", {"success": True, "server_timestamp": 999, "monotonic_timestamp": 998})
            return [{"passenger_name": "旧请求乘车人", "passenger_type": "1"}]

    monkeypatch.setattr(worker_module, "RailwayClient", Client)
    monkeypatch.setattr(main_window.contact_selector, "set_contacts", lambda *args: contact_updates.append(args))
    main_window.account_state = "valid"
    main_window._go_to_step(1)
    main_window.passengers.setText("当前乘车人")
    before_status = main_window.session_check_status.text()
    main_window._start_connection_operation("contacts")
    qtbot.waitUntil(started.is_set)
    old_generation = main_window._operation_generation
    main_window._stop_task()
    release.set()
    qtbot.waitUntil(lambda: main_window._active_operation is None)
    assert main_window.session_check_status.text() == before_status
    assert main_window._server_anchor is None
    assert contact_updates == []
    assert main_window.passengers.text() == "当前乘车人"
    assert main_window._operation_generation > old_generation
    # A queued signal carrying a completed operation's generation also cannot
    # alter the next task, even when its cancellation token has been replaced.
    main_window._operation_generation += 1
    main_window.cancel_token = GuiCancelToken()
    main_window._receive_runtime_event(old_generation, "session_checked", {"state": "expired", "checked_at": 200})
    assert main_window.session_check_status.text() == before_status


def test_successful_clock_survives_failed_idle_and_task_sync_in_real_workers(main_window, qtbot, monkeypatch, caplog):
    """Use real QThreads, ServerClock and runner scheduling; fake only network."""
    import requests
    from ticket_app import runner as runner_module
    from ticket_app.stations import StationStore

    main_window.advanced["time_sync_samples"].setValue(1)
    calls = []

    def head(_url, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            ahead = datetime.now(timezone.utc) + timedelta(seconds=120)
            return SimpleNamespace(status_code=200, headers={"Date": format_datetime(ahead, usegmt=True)})
        raise requests.ConnectionError("simulated offline clock")

    monkeypatch.setattr(main_window.shared_session, "head", head)
    main_window._start_connection_operation("sync_clock")
    qtbot.waitUntil(lambda: main_window._active_operation is None)
    shared = main_window.shared_clock
    anchor = main_window._server_anchor
    assert shared.has_synchronized
    assert anchor is not None
    assert shared.offset_seconds > 118
    first_rtt = main_window.rtt_metric.value_label.text()

    main_window.advanced["request_timeout_seconds"].setValue(7)
    main_window._start_connection_operation("sync_clock")
    qtbot.waitUntil(lambda: main_window._active_operation is None)
    assert main_window.shared_clock is shared
    assert shared.timeout == 7
    assert main_window._server_anchor == anchor
    assert main_window.rtt_metric.value_label.text() == first_rtt
    assert "保留上次成功校时结果" in caplog.text
    assert "使用本地时间" not in caplog.text

    class Client:
        def __init__(self, cfg, event_sink=None, cancel_token=None, session=None):
            self.session = session
            self.sink = event_sink

        def ensure_login(self):
            self.sink("qr_status", {"status": "logged_in", "message": "测试登录仍有效"})
            return False

    monkeypatch.setattr(runner_module, "RailwayClient", Client)
    monkeypatch.setattr(StationStore, "load", lambda *_args: None)
    monkeypatch.setattr(StationStore, "code", lambda _self, name: name)
    errors = []
    monkeypatch.setattr(gui_app.QMessageBox, "critical", lambda *args: errors.append(args))
    monkeypatch.setattr(gui_app.QMessageBox, "warning", lambda *args: errors.append(args))
    scheduled = shared.now() + timedelta(seconds=90)
    main_window.start_at.setText(scheduled.strftime("%H:%M:%S"))
    main_window.stop_at.set_disabled(True)
    main_window.auto_submit.setChecked(False)
    main_window.seat_types.set_values(["二等座"])
    main_window.account_state = "valid"
    main_window._go_to_step(2)
    main_window._start_task()
    try:
        qtbot.waitUntil(lambda: main_window._maintenance_enabled and main_window._target_timestamp is not None)
        assert main_window.worker.runner.clock is shared
        assert main_window._server_anchor == anchor
        assert main_window.rtt_metric.value_label.text() == first_rtt
        assert len(calls) == 3  # First success, idle failure, task startup failure.
        assert 85 < main_window._target_timestamp - shared.now_timestamp() < 91
        assert abs(main_window._server_now_timestamp() - shared.now_timestamp()) < 0.1
        assert shared.now_timestamp() - time.time() > 118
        assert "使用本地时间" not in caplog.text
        assert errors == []
    finally:
        main_window._stop_task()
        qtbot.waitUntil(lambda: main_window._active_operation is None)


@pytest.mark.parametrize("mode", ["check_login", "task"])
def test_repeated_operations_reuse_qt_objects_and_reject_old_queued_results(main_window, qtbot, monkeypatch, mode):
    """Finish jobs while the UI is held; the executor and thread stay alive."""
    entered = threading.Event()
    release = threading.Event()
    callbacks = []
    rounds = {"index": 0}

    class Client:
        def __init__(self, cfg, relay, token, session):
            self.relay = relay

        def check_session(self):
            entered.set()
            assert release.wait(3)
            self.relay("session_checked", {"state": "valid", "checked_at": 100 + rounds["index"]})
            if rounds["index"] % 4 == 3:
                raise AppError("模拟连接异常")
            return True

    class Runner:
        def __init__(self, cfg, event_sink=None, cancel_token=None, session=None, clock=None):
            self.relay = event_sink

        def run(self):
            entered.set()
            assert release.wait(3)
            self.relay("phase", {"phase": "waiting", "message": "测试等待"})
            if rounds["index"] % 4 == 3:
                raise AppError("模拟任务异常")
            return 130

    monkeypatch.setattr(worker_module, "RailwayClient", Client)
    monkeypatch.setattr(worker_module, "TicketRunner", Runner)
    monkeypatch.setattr(gui_app.QMessageBox, "warning", lambda *_args: None)
    monkeypatch.setattr(gui_app.QMessageBox, "critical", lambda *_args: None)
    # Identity comes from explicit generations, never QObject.sender().
    monkeypatch.setattr(main_window, "sender", lambda: pytest.fail("GUI callback must not read QObject.sender"))
    for name in ("_on_runtime_event", "_on_connection_completed", "_on_connection_failed",
                 "_on_worker_completed", "_on_worker_failed", "_release_operation"):
        original = getattr(main_window, name)

        def record(*args, _original=original, _name=name, **kwargs):
            assert QThread.currentThread() is gui_app.QApplication.instance().thread()
            callbacks.append((_name, kwargs["generation"]))
            return _original(*args, **kwargs)

        monkeypatch.setattr(main_window, name, record)

    main_window.auto_submit.setChecked(False)
    main_window.seat_types.set_values(["二等座"])
    generations = []
    executor = background_thread = None
    executor_address = None
    for index in range(24):
        rounds["index"] = index
        entered.clear()
        release.clear()
        if mode == "task":
            main_window.account_state = "valid"
            main_window._go_to_step(2)
            main_window._start_task()
        else:
            main_window._start_connection_operation(mode)
        qtbot.waitUntil(entered.is_set)
        generation = main_window._operation_generation
        generations.append(generation)
        if executor is None:
            executor = main_window._executor
            executor_address = getCppPointer(executor)
            background_thread = main_window._background_thread
        assert main_window.worker is main_window._executor is executor
        assert main_window._background_thread is background_thread
        release.set()
        deadline = time.monotonic() + 3
        while executor.current_request is not None and time.monotonic() < deadline:
            time.sleep(0.005)  # Release the GIL without pumping GUI callbacks.
        assert executor.current_request is None
        assert isValid(executor) and getCppPointer(executor) == executor_address
        assert background_thread.isRunning()
        qtbot.waitUntil(lambda: main_window._active_operation is None)
        assert ("_release_operation", generation) in callbacks
        snapshot = (main_window.phase_badge.text(), main_window.session_check_status.text(),
                    main_window.account_state, main_window._operation_generation)
        main_window._receive_runtime_event(generation, "session_checked", {"state": "expired"})
        main_window._receive_operation_outcome(generation, OperationOutcome(mode, error="旧结果不应显示"))
        assert snapshot == (main_window.phase_badge.text(), main_window.session_check_status.text(),
                            main_window.account_state, main_window._operation_generation)
    assert len(set(generations)) == 24
    assert sum(name == "_release_operation" for name, _generation in callbacks) == 24
