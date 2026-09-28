"""Offline behavioral coverage of the four-step booking workflow."""

from datetime import date
import threading
from types import SimpleNamespace

import pytest
from cart_helpers import set_cart

from test_gui_app import main_window  # noqa: F401 - isolated, network-blocked fixture
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from ticket_app.configuration import AppError
from ticket_app.gui import app as gui_app
from ticket_app.gui import worker as worker_module
from ticket_app.gui.compat import load_gui_settings, save_gui_settings
from ticket_app.gui.worker import GuiCancelToken


@pytest.fixture(autouse=True)
def no_modal_dialogs(monkeypatch):
    for method in ("information", "warning", "critical"):
        monkeypatch.setattr(gui_app.QMessageBox, method, lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gui_app.QMessageBox, "question", lambda *_args, **_kwargs: gui_app.QMessageBox.StandardButton.Yes)


def set_valid_trip(window, *, monitor=False):
    window.from_station.setText("北京西")
    window.to_station.setText("郑州东")
    window.train_date.setDate(gui_app.QDate.currentDate())
    window.preferred_trains.setText("G79")
    set_cart(window, ["二等座"])
    window.start_at.set_disabled(True)
    window.stop_at.set_disabled(True)
    window.auto_submit.setChecked(not monitor)


def confirm_login(window, contacts=None, error=None):
    window._on_connection_completed("login", {
        "authenticated": True,
        "contacts": contacts if contacts is not None else [{"name": "学生甲", "passenger_type": "3"}],
        "contacts_error": error,
    })


def test_new_window_starts_at_trip_step_without_authentication_or_booking(main_window):
    assert main_window.current_step == main_window.steps.currentIndex() == 0
    assert main_window.steps.count() == len(main_window.step_buttons) == 4
    assert main_window.account_state == "unknown"
    assert main_window._task_state == "idle"
    assert main_window._active_operation is None
    assert not main_window.start_button.isEnabled()
    assert main_window.start_button.isHidden()
    assert main_window.cart_items == []
    assert not main_window.advanced_toggle.isChecked()
    assert main_window.advanced_content.isHidden()
    main_window.step_buttons[2].click()
    assert main_window.current_step == 0
    assert main_window._test_network_calls == []


def test_trip_next_validates_only_trip_fields_and_never_logs_in(main_window):
    set_valid_trip(main_window)
    # Passenger and position drafts are intentionally invalid until later pages.
    main_window.passengers.setText("学生甲，学生甲")
    main_window.position_preferences.seats.set_positions(["1A"])
    main_window._next_step()
    assert main_window.current_step == main_window.steps.currentIndex() == 1
    assert main_window.account_state == "unknown"
    assert main_window._active_operation is None
    assert main_window._test_network_calls == []


def test_trip_step_blocks_missing_seats_even_if_account_is_already_valid(main_window):
    set_valid_trip(main_window)
    set_cart(main_window, [])
    main_window.account_state = "valid"
    main_window._next_step()
    assert main_window.current_step == 0
    assert main_window.flow_error.text()


@pytest.mark.parametrize("state", ["unknown", "expired"])
def test_passenger_step_requires_login_even_with_manually_filled_names(main_window, state):
    set_valid_trip(main_window)
    main_window._go_to_step(1)
    main_window.account_state = state
    main_window.passengers.setText("学生甲")
    main_window._render_workflow()
    main_window._next_step()
    assert main_window.current_step == 1
    assert "登录" in main_window.flow_error.text()
    assert main_window._active_operation is None


def test_passenger_next_checks_names_but_leaves_position_validation_for_confirmation(main_window):
    set_valid_trip(main_window)
    main_window._go_to_step(1)
    confirm_login(main_window)
    main_window._next_step()
    assert main_window.current_step == 1  # Auto-submit needs a passenger.
    main_window.passengers.setText("学生甲，学生甲")
    main_window._next_step()
    assert main_window.current_step == 1
    main_window.passengers.setText("学生甲")
    main_window.position_preferences.seats.set_positions(["1A", "1F"])
    main_window._next_step()
    assert main_window.current_step == 2  # Preference count is a step 3 concern.
    main_window._start_task()
    assert main_window._active_operation is None
    assert main_window.current_step == 2
    assert main_window._task_state == "idle"


def test_monitoring_can_use_zero_passengers_but_still_requires_login(main_window):
    set_valid_trip(main_window, monitor=True)
    main_window._next_step()
    main_window._next_step()
    assert main_window.current_step == 1
    confirm_login(main_window, contacts=[])
    main_window._next_step()
    assert main_window.current_step == 2
    assert main_window.passengers.text() == ""
    assert "仅监控" in main_window.confirm_summary.text()


def test_explicit_student_override_is_checked_against_loaded_contact_before_confirmation(main_window):
    set_valid_trip(main_window)
    main_window._go_to_step(1)
    confirm_login(main_window, [{"name": "成人乙", "passenger_type": "1"}])
    main_window.passengers.setText("成人乙")
    combo = main_window.passenger_ticket_types.rows["成人乙"]
    combo.setCurrentIndex(combo.findData("student"))
    main_window._next_step()
    assert main_window.current_step == 1
    assert "学生" in main_window.flow_error.text()


@pytest.mark.parametrize("contacts", [[], [{"name": "学生甲", "passenger_type": "3"}]])
def test_loaded_contacts_block_handwritten_unknown_name(main_window, contacts):
    set_valid_trip(main_window)
    main_window._next_step()
    confirm_login(main_window, contacts=contacts)
    main_window.manual_toggle.setChecked(True)
    main_window.passengers.setText("账号外乘车人")

    main_window._next_step()

    assert main_window._contacts_loaded
    assert main_window.current_step == 1
    assert "找不到乘车人" in main_window.flow_error.text()
    assert "账号外乘车人" in main_window.flow_error.text()
    assert main_window._active_operation is None
    assert main_window._test_network_calls == []


def test_contact_refresh_failure_clears_stale_known_names_and_allows_manual_fallback(main_window):
    set_valid_trip(main_window)
    main_window._next_step()
    confirm_login(main_window, contacts=[{"name": "成人乙", "passenger_type": "1"}])
    main_window.passengers.setText("账号外乘车人")
    main_window._next_step()
    assert main_window.current_step == 1
    assert main_window._contacts_loaded

    main_window._on_connection_completed("contacts", {
        "authenticated": True,
        "contacts": None,
        "contacts_error": "联系人读取失败，可重新读取或手动填写姓名。",
    })

    assert main_window.account_state == "valid"
    assert not main_window._contacts_loaded
    assert main_window._contacts == []
    assert main_window.contact_selector.checkboxes == []
    assert main_window.manual_toggle.isChecked()
    assert main_window.passengers.text() == "账号外乘车人"
    assert "读取失败" in main_window.contacts_status.text()
    main_window._next_step()
    assert main_window.current_step == 2
    assert main_window._active_operation is None
    assert main_window._test_network_calls == []


def test_keyboard_space_advances_trip_selects_contact_and_advances_confirmation(main_window, qtbot):
    set_valid_trip(main_window)
    main_window.show()
    main_window.activateWindow()
    main_window.next_button.setFocus()
    qtbot.waitUntil(lambda: QApplication.focusWidget() is main_window.next_button)
    assert main_window.next_button.isVisible()
    qtbot.keyClick(main_window.next_button, Qt.Key.Key_Space)
    assert main_window.current_step == 1

    confirm_login(main_window)
    contact = main_window.contact_selector.checkboxes[0]
    main_window.passenger_scroll.ensureWidgetVisible(contact)
    contact.setFocus()
    qtbot.waitUntil(lambda: QApplication.focusWidget() is contact)
    assert contact.isVisible()
    qtbot.keyClick(contact, Qt.Key.Key_Space)
    assert contact.isChecked()
    assert main_window.passengers.text() == "学生甲"
    # A second key press cancels, and a third restores the one selected person.
    qtbot.keyClick(contact, Qt.Key.Key_Space)
    assert not contact.isChecked()
    assert main_window.passengers.text() == ""
    qtbot.keyClick(contact, Qt.Key.Key_Space)
    assert main_window.passengers.text() == "学生甲"

    main_window.next_button.setFocus()
    qtbot.waitUntil(lambda: QApplication.focusWidget() is main_window.next_button)
    assert main_window.next_button.isVisible()
    qtbot.keyClick(main_window.next_button, Qt.Key.Key_Space)
    assert main_window.current_step == 2
    assert main_window.start_button.isVisible()
    assert main_window.start_button.isEnabled()
    assert main_window._active_operation is None
    assert main_window._task_state == "idle"
    assert main_window._test_network_calls == []


@pytest.mark.parametrize("change", ["passenger_count", "seat_layout", "berth_count"])
def test_back_edit_revalidates_preserved_preferences_and_focuses_confirmation(
    main_window, qtbot, monkeypatch, change,
):
    set_valid_trip(main_window)
    if change == "berth_count":
        set_cart(main_window, ["硬卧"])
    main_window.show()
    main_window.activateWindow()
    main_window._next_step()
    confirm_login(main_window, contacts=[
        {"name": "学生甲", "passenger_type": "3"},
        {"name": "成人乙", "passenger_type": "1"},
    ])
    main_window.passengers.setText("学生甲，成人乙")
    main_window._next_step()
    assert main_window.current_step == 2
    if change == "berth_count":
        main_window.position_preferences.berths.set_values({"lower": 2})
    else:
        main_window.position_preferences.seats.set_positions(["1A", "1B"])
    assert main_window._validate_all() == {}

    main_window.back_button.click()
    main_window.back_button.click()
    assert main_window.current_step == 0
    if change == "seat_layout":
        set_cart(main_window, ["商务座"])
    main_window._next_step()
    assert main_window.current_step == 1
    if change != "seat_layout":
        main_window.passengers.setText("学生甲")
    main_window._next_step()
    assert main_window.current_step == 2
    monkeypatch.setattr(main_window, "_build_current_config", lambda: pytest.fail("Invalid preferences must stop before task construction"))

    main_window.start_button.click()

    assert main_window.current_step == main_window.steps.currentIndex() == 2
    assert main_window._active_operation is None
    assert main_window._task_state == "idle"
    if change == "berth_count":
        assert "berth_preference" in main_window._last_validation_errors
        assert main_window.position_preferences.tabs.currentIndex() == 1
        target = main_window.position_preferences.berths.spins["lower"]
        assert main_window.position_preferences.berths.values()["lower"] == 2
    else:
        assert "seat_position_preferences" in main_window._last_validation_errors
        assert main_window.position_preferences.tabs.currentIndex() == 0
        target = next(iter(main_window.position_preferences.seats.buttons.values()))
        assert main_window.position_preferences.seats.positions() == ["1A", "1B"]
    qtbot.waitUntil(lambda: QApplication.focusWidget() is target)
    assert target.isVisible()
    assert main_window._test_network_calls == []


def test_login_loads_inline_contacts_without_advancing_or_starting_task(main_window, qtbot, monkeypatch):
    calls = []

    class Client:
        def __init__(self, cfg, relay, token, session):
            self.relay = relay

        def ensure_login(self, **kwargs):
            calls.append("login")
            self.relay("qr_status", {"status": "confirmed"})

        def get_passengers(self):
            calls.append("contacts")
            return [{"passenger_name": "学生甲", "passenger_type": "3", "passenger_id_no": "private-id"}]

    monkeypatch.setattr(worker_module, "RailwayClient", Client)
    monkeypatch.setattr(worker_module, "TicketRunner", lambda *_args, **_kwargs: pytest.fail("Login must not create a booking runner"))
    set_valid_trip(main_window)
    main_window._next_step()
    main_window._start_connection_operation("login")
    qtbot.waitUntil(lambda: main_window._active_operation is None)
    assert calls == ["login", "contacts"]
    assert main_window.account_state == "valid"
    assert main_window.current_step == 1
    assert main_window._task_state == "idle"
    assert len(main_window.contact_selector.checkboxes) == 1
    main_window.contact_selector.checkboxes[0].click()
    assert main_window.passengers.text() == "学生甲"
    assert main_window.passenger_ticket_types.values() == {}
    assert "private-id" not in repr(main_window._collect_mapping())
    assert main_window._active_operation is None


def test_login_remains_valid_when_automatic_contact_fetch_fails(main_window, qtbot, monkeypatch):
    class Client:
        def __init__(self, cfg, relay, token, session):
            self.relay = relay

        def ensure_login(self, **kwargs):
            self.relay("qr_status", {"status": "confirmed"})

        def get_passengers(self):
            raise AppError("private-response-must-not-appear")

    monkeypatch.setattr(worker_module, "RailwayClient", Client)
    set_valid_trip(main_window)
    main_window._next_step()
    main_window._start_connection_operation("login")
    qtbot.waitUntil(lambda: main_window._active_operation is None)
    assert main_window.account_state == "valid"
    assert main_window.current_step == 1
    assert "读取失败" in main_window.contacts_status.text()
    assert "private-response" not in main_window.contacts_status.text()
    main_window.manual_toggle.setChecked(True)
    main_window.passengers.setText("学生甲")
    main_window._next_step()
    assert main_window.current_step == 2
    assert main_window._active_operation is None


def test_contact_refresh_never_opens_an_implicit_qr_flow(main_window, qtbot, monkeypatch):
    class Client:
        def __init__(self, cfg, relay, token, session):
            self.relay = relay

        def check_session(self):
            self.relay("session_checked", {"state": "expired"})
            return False

        def ensure_login(self, **kwargs):
            pytest.fail("Reading contacts must not open QR login")

        def get_passengers(self):
            pytest.fail("Expired account must not fetch contacts")

    monkeypatch.setattr(worker_module, "RailwayClient", Client)
    main_window._go_to_step(1)
    main_window.account_state = "valid"
    main_window._start_connection_operation("contacts")
    qtbot.waitUntil(lambda: main_window._active_operation is None)
    assert main_window.account_state == "expired"
    assert main_window.current_step == 1
    assert main_window._task_state == "idle"


def test_recalibration_on_trip_page_never_starts_authentication_or_booking(main_window, qtbot, monkeypatch):
    def sync(token, relay, **kwargs):
        relay("clock_sync", {"success": True, "offset_seconds": 0.25, "rtt_ms": 30,
                             "server_timestamp": 100, "monotonic_timestamp": 90, "source": "manual"})
        return True

    monkeypatch.setattr(main_window.shared_clock, "sync", sync)
    monkeypatch.setattr(worker_module, "TicketRunner", lambda *_args, **_kwargs: pytest.fail("Recalibration must not create a runner"))
    main_window._start_connection_operation("sync_clock")
    qtbot.waitUntil(lambda: main_window._active_operation is None)
    assert main_window.current_step == 0
    assert main_window.account_state == "unknown"
    assert main_window._task_state == "idle"


@pytest.mark.parametrize("step", [0, 1, 3])
def test_start_task_is_guarded_outside_confirmation_even_when_called_directly(main_window, monkeypatch, step):
    main_window.account_state = "valid"
    main_window._go_to_step(step)
    monkeypatch.setattr(main_window, "_build_current_config", lambda: pytest.fail("Only confirmation may start the task"))
    main_window._start_task()
    assert main_window._active_operation is None
    assert main_window.current_step == step
    assert not main_window.start_button.isEnabled()


def test_start_stop_modify_and_restart_preserve_draft_and_process_session(main_window, qtbot, monkeypatch):
    sessions = []
    configs = []
    started = threading.Event()

    class Runner:
        def __init__(self, cfg, event_sink=None, cancel_token=None, session=None, clock=None):
            configs.append(cfg)
            sessions.append(session)
            self.token = cancel_token
            self.relay = event_sink

        def run(self):
            self.relay("phase", {"phase": "waiting", "message": "等待开售"})
            started.set()
            self.token.wait(30)
            pytest.fail("The simulated task should be cancelled")

    monkeypatch.setattr(worker_module, "TicketRunner", Runner)
    set_valid_trip(main_window)
    main_window._next_step()
    confirm_login(main_window)
    main_window.passengers.setText("学生甲")
    combo = main_window.passenger_ticket_types.rows["学生甲"]
    combo.setCurrentIndex(combo.findData("adult"))
    main_window._next_step()
    shared_session = main_window.shared_session
    shared_session.cookies.set("wizard-test", "in-memory")
    main_window._start_task()
    try:
        qtbot.waitUntil(started.is_set)
        assert main_window.current_step == 3
        assert main_window._task_state in {"running", "waiting"}
        assert main_window._operation_mode == "task"
        assert main_window.back_button.isEnabled()
        main_window._modify_configuration()
        assert main_window.current_step == 3
        main_window._stop_task()
        qtbot.waitUntil(lambda: main_window._active_operation is None)
        assert main_window.current_step == 3
        assert main_window._task_state == "cancelled"
        main_window._modify_configuration()
        assert main_window.current_step == 0
        assert main_window.account_state == "valid"
        assert main_window.passenger_ticket_types.values() == {"学生甲": "adult"}
        assert shared_session.cookies.get("wizard-test") == "in-memory"
        main_window.preferred_trains.setText("G123")
        set_cart(main_window, ["二等座"])
        main_window._next_step()
        main_window._next_step()
        assert main_window.current_step == 2
        started.clear()
        main_window._start_task()
        qtbot.waitUntil(started.is_set)
        assert sessions == [shared_session, shared_session]
        assert [item.train_code for item in configs[-1].cart_items] == ["G123"]
        assert configs[-1].passenger_ticket_types == {"学生甲": "adult"}
    finally:
        if main_window._active_operation is not None:
            main_window._stop_task()
            qtbot.waitUntil(lambda: main_window._active_operation is None)


def test_cancelled_login_and_old_generation_cannot_advance_or_authenticate_wizard(main_window, qtbot, monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    class Client:
        def __init__(self, cfg, relay, token, session):
            self.relay = relay

        def ensure_login(self, **kwargs):
            entered.set()
            assert release.wait(3)
            self.relay("qr_status", {"status": "confirmed"})

        def get_passengers(self):
            pytest.fail("Cancelled login must not proceed to contacts")

    monkeypatch.setattr(worker_module, "RailwayClient", Client)
    main_window._go_to_step(1)
    main_window._start_connection_operation("login")
    qtbot.waitUntil(entered.is_set)
    generation = main_window._operation_generation
    main_window._stop_task()
    release.set()
    qtbot.waitUntil(lambda: main_window._active_operation is None)
    assert main_window.current_step == 1
    assert main_window.account_state == "unknown"
    main_window._on_connection_completed("login", {"authenticated": True, "contacts": []}, generation=generation)
    assert main_window.account_state == "unknown"
    assert main_window.current_step == 1


def test_running_qr_recovery_queues_authentication_without_restarting_task(main_window):
    token = GuiCancelToken()
    token.set_actions_enabled(True)
    main_window._active_operation = SimpleNamespace(isRunning=lambda: True)
    main_window.cancel_token = token
    main_window._operation_mode = "task"
    main_window._task_state = "running"
    main_window._go_to_step(3)
    main_window._on_runtime_event("maintenance_availability", {"enabled": True, "busy": False, "login_required": True})
    main_window._on_runtime_event("qr_status", {"status": "expired"})
    generation = main_window._operation_generation
    main_window._restart_for_qr()
    assert token.next_action() == "login"
    assert not token.is_cancelled
    assert main_window.current_step == 3
    assert main_window._operation_generation == generation


def test_imported_ticket_types_restore_without_signing_in_or_starting(main_window, tmp_path):
    path = tmp_path / "wizard-config.json"
    save_gui_settings(path, {
        "from_station": "北京西", "to_station": "郑州东", "train_date": date.today().isoformat(),
        "passenger_names": ["学生甲", "学生乙"], "passenger_ticket_types": {"学生甲": "adult", "学生乙": "student"},
        "seat_types": ["二等座"], "quiet_carriage_preference": True,
    })
    main_window._apply_mapping(load_gui_settings(path))
    assert main_window.passenger_ticket_types.values() == {"学生甲": "adult", "学生乙": "student"}
    assert main_window.quiet_carriage.isChecked()
    assert main_window.current_step == 0
    assert main_window.account_state == "unknown"
    assert main_window._active_operation is None
