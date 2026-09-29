"""Offline contracts for the single login path and embedded contact choices."""

from __future__ import annotations

import pytest
from cart_helpers import set_cart
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from test_gui_app import main_window  # noqa: F401 - isolated offline GUI fixture

from ticket_app.configuration import ConnectionConfig
from ticket_app.gui import worker as worker_module
from ticket_app.gui import app as gui_app
from ticket_app.gui.passenger_widgets import (
    InlinePassengerSelector, PassengerTicketEditor, default_ticket_label,
)
from ticket_app.gui.worker import GuiCancelToken, OperationRequest, OperationWorker
from ticket_app.runtime import RunCancelled


def run_connection(monkeypatch, mode, *, valid=True, contact_error=None, login_error=None,
                   cancel_on_read=False):
    calls = []
    token = GuiCancelToken()

    class Client:
        def __init__(self, *_args):
            pass

        def check_session(self):
            calls.append("check")
            return valid

        def ensure_login(self, *, check_first):
            calls.append(("login", check_first))
            if login_error:
                raise login_error
            return True

        def get_passengers(self):
            calls.append("contacts")
            if cancel_on_read:
                token.cancel()
            if contact_error:
                raise contact_error
            return [{"passenger_name": "测试甲", "passenger_type": "3",
                     "passenger_id_no": "PRIVATE-ID", "allEncStr": "PRIVATE-TOKEN"}]

    monkeypatch.setattr(worker_module, "RailwayClient", Client)
    monkeypatch.setattr(worker_module, "TicketRunner", lambda *_a, **_kw: pytest.fail("No ticket runner"))
    worker = OperationWorker()
    completed, failed, done = [], [], []

    def finished(generation, outcome):
        assert generation == 1
        done.append(True)
        if outcome.error:
            failed.append((outcome.error, outcome.details))
        else:
            completed.append((outcome.mode, outcome.result))

    worker.finished.connect(finished)
    worker.execute(OperationRequest(1, mode, ConnectionConfig.from_mapping({"persist_session": False}), token))
    assert done == [True]
    return calls, completed, failed


def test_explicit_login_authenticates_then_reads_contacts(qtbot, monkeypatch):
    calls, completed, failed = run_connection(monkeypatch, "login")
    assert calls == [("login", False), "contacts"]
    assert completed == [("login", {
        "authenticated": True, "contacts": [{"name": "测试甲", "passenger_type": "3"}],
    })]
    assert failed == []
    assert "PRIVATE" not in repr(completed)


@pytest.mark.parametrize("mode", ["login", "contacts"])
def test_read_failure_keeps_authentication_and_sanitizes_error(qtbot, monkeypatch, caplog, mode):
    calls, completed, failed = run_connection(
        monkeypatch, mode, contact_error=RuntimeError("PRIVATE-RESPONSE PRIVATE-COOKIE"),
    )
    assert "contacts" in calls
    assert completed[0][1]["authenticated"] is True
    assert completed[0][1]["contacts"] is None
    assert "重新读取" in completed[0][1]["contacts_error"]
    assert "手动填写" in completed[0][1]["contacts_error"]
    assert "PRIVATE" not in repr(completed) + caplog.text
    assert failed == []


def test_refresh_checks_session_and_never_authenticates(qtbot, monkeypatch):
    calls, completed, failed = run_connection(monkeypatch, "contacts")
    assert calls == ["check", "contacts"]
    assert completed[0][1]["authenticated"] is True
    assert failed == []


def test_expired_refresh_requests_explicit_login_without_reading(qtbot, monkeypatch):
    calls, completed, failed = run_connection(monkeypatch, "contacts", valid=False)
    assert calls == ["check"]
    assert completed == [("contacts", {"authenticated": False, "contacts": None})]
    assert failed == []


def test_status_check_still_returns_bool_without_reading_contacts(qtbot, monkeypatch):
    calls, completed, failed = run_connection(monkeypatch, "check_login", valid=False)
    assert calls == ["check"]
    assert completed == [("check_login", False)]
    assert failed == []


def test_login_failure_does_not_fetch_contacts(qtbot, monkeypatch):
    calls, completed, failed = run_connection(monkeypatch, "login", login_error=RuntimeError("无法登录"))
    assert calls == [("login", False)]
    assert completed == []
    assert failed[0][0] == "无法登录"


@pytest.mark.parametrize("error", [None, RuntimeError("discard response"), RunCancelled()])
def test_cancelled_contact_read_never_returns_late_contacts(qtbot, monkeypatch, error):
    _calls, completed, failed = run_connection(
        monkeypatch, "contacts", cancel_on_read=True, contact_error=error,
    )
    assert completed == [("contacts", None)]
    assert failed == []


def contact_rows(count=7):
    return [{"name": f"乘车人{i}", "passenger_type": "1"} for i in range(count)]


def test_inline_mouse_selection_keeps_order_limits_five_and_blocks_ambiguity(qtbot):
    selector = InlinePassengerSelector()
    qtbot.addWidget(selector)
    changes = []
    selector.changed.connect(lambda: changes.append(selector.selected_names()))
    selector.set_contacts(contact_rows() + [{"name": "同名", "passenger_type": kind} for kind in ("1", "3")])
    selector.show()
    for index in (3, 1, 5, 0, 2, 6):
        qtbot.mouseClick(selector.checkboxes[index], Qt.MouseButton.LeftButton)
    assert selector.selected_names() == ["乘车人3", "乘车人1", "乘车人5", "乘车人0", "乘车人2"]
    assert len(changes) == 5
    assert not selector.checkboxes[6].isChecked()
    assert "最多选择 5" in selector.status.text()
    qtbot.mouseClick(selector.checkboxes[1], Qt.MouseButton.LeftButton)
    qtbot.mouseClick(selector.checkboxes[1], Qt.MouseButton.LeftButton)
    assert selector.selected_names()[-1] == "乘车人1"
    assert all(not box.isEnabled() for box in selector.checkboxes[-2:])
    assert all("重名" in box.text() for box in selector.checkboxes[-2:])


def test_inline_import_and_refresh_are_silent_and_preserve_provided_order(qtbot):
    selector = InlinePassengerSelector()
    qtbot.addWidget(selector)
    changes = []
    selector.changed.connect(lambda: changes.append(True))
    selector.set_contacts(contact_rows(), ["乘车人4", "不存在", "乘车人1", "乘车人4"])
    assert selector.selected_names() == ["乘车人4", "乘车人1"]
    selector.set_selected_names(["乘车人2", "乘车人1"])
    assert selector.selected_names() == ["乘车人2", "乘车人1"]
    selector.set_contacts(list(reversed(contact_rows())), selector.selected_names())
    assert selector.selected_names() == ["乘车人2", "乘车人1"]
    assert changes == []


def test_inline_space_key_toggles_and_selection_return_value_is_a_copy(qtbot):
    selector = InlinePassengerSelector()
    qtbot.addWidget(selector)
    selector.set_contacts(contact_rows(2))
    selector.show()
    selector.checkboxes[1].setFocus()
    qtbot.keyClick(selector.checkboxes[1], Qt.Key.Key_Space)
    assert selector.selected_names() == ["乘车人1"]
    result = selector.selected_names()
    result.clear()
    assert selector.selected_names() == ["乘车人1"]
    qtbot.keyClick(selector.checkboxes[1], Qt.Key.Key_Space)
    assert selector.selected_names() == []


def test_inline_empty_contacts_and_replacements_clear_old_visible_selection(qtbot):
    selector = InlinePassengerSelector()
    qtbot.addWidget(selector)
    selector.set_contacts(contact_rows(2), ["乘车人1"])
    selector.set_contacts([])
    assert selector.selected_names() == []
    assert selector.checkboxes == []
    assert selector.title.text() == "乘车人（0/5）"
    assert selector.status.isHidden()


def test_repeated_empty_contact_events_reuse_hint_and_controls(qtbot):
    selector = InlinePassengerSelector()
    qtbot.addWidget(selector)
    hint = selector.empty_hint
    ambiguity = selector.ambiguity_hint
    initial_children = tuple(selector.children())
    for _ in range(30):
        selector.set_contacts([])
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert selector.empty_hint is hint and isValid(hint)
    assert selector.ambiguity_hint is ambiguity and isValid(ambiguity)
    assert tuple(selector.children()) == initial_children
    assert not hint.isHidden()
    assert ambiguity.isHidden()


def test_identical_contact_refresh_and_expiry_reuse_checkboxes_silently(qtbot):
    selector = InlinePassengerSelector()
    qtbot.addWidget(selector)
    changes = []
    selector.changed.connect(lambda: changes.append(selector.selected_names()))
    selector.set_contacts(contact_rows(3), ["乘车人2"])
    boxes = tuple(selector.checkboxes)
    for _ in range(15):
        selector.set_contacts(contact_rows(3), ["乘车人2", "乘车人0"])
        assert tuple(selector.checkboxes) == boxes
    for _ in range(15):
        selector.set_contacts([])
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert all(isValid(box) and box.isHidden() and not box.isChecked() for box in boxes)
    assert selector.checkboxes == []
    selector.set_contacts(contact_rows(3), ["乘车人1"])
    assert tuple(selector.checkboxes) == boxes
    assert changes == []
    selector.checkboxes[0].click()
    assert changes == [["乘车人1", "乘车人0"]]


def test_reused_contact_rows_reset_ambiguity_and_use_current_name(qtbot):
    selector = InlinePassengerSelector()
    qtbot.addWidget(selector)
    selector.set_contacts([{"name": "同名", "passenger_type": "1"}] * 2)
    first_box = selector.checkboxes[0]
    assert not first_box.isEnabled()
    selector.set_contacts([{"name": "新乘车人", "passenger_type": "3"}])
    assert selector.checkboxes[0] is first_box
    assert first_box.isEnabled() and "新乘车人" in first_box.toolTip()
    assert "同名" not in first_box.toolTip()
    assert "同名" not in first_box.text()
    first_box.click()
    assert selector.selected_names() == ["新乘车人"]
    assert selector.ambiguity_hint.isHidden()


def test_ticket_type_defaults_explain_known_passenger_category_without_changing_values(qtbot):
    editor = PassengerTicketEditor()
    qtbot.addWidget(editor)
    editor.set_names(["学生甲", "成人乙", "未知丙"])
    assert editor.rows["学生甲"].itemText(0) == "12306默认"
    student_combo = editor.rows["学生甲"]
    student_combo.setCurrentIndex(student_combo.findData("adult"))
    changes = []
    editor.changed.connect(lambda: changes.append(editor.values()))
    editor.set_contact_types({"学生甲": "3", "成人乙": "1"})
    assert editor.rows["学生甲"] is student_combo
    assert student_combo.itemText(0) == "12306默认（学生）"
    assert editor.rows["成人乙"].currentText() == "12306默认（成人）"
    assert editor.rows["未知丙"].currentText() == "12306默认"
    assert "车票类型" in student_combo.accessibleName()
    assert editor.values() == {"学生甲": "adult"}
    editor.set_contact_types({})
    assert student_combo.itemText(0) == "12306默认"
    assert editor.values() == {"学生甲": "adult"}
    assert changes == []
    editor.set_contact_types({"学生甲": 3})
    editor.set_names(["未知丙", "学生甲"])
    assert editor.rows["学生甲"].itemText(0) == "12306默认（学生）"
    assert editor.values() == {"学生甲": "adult"}


@pytest.mark.parametrize("category, expected", [
    ("1", "12306默认（成人）"), ("3", "12306默认（学生）"),
    ("2", "12306默认"), ("", "12306默认"),
])
def test_ticket_type_default_label_shared_by_editor_and_confirmation(category, expected):
    assert default_ticket_label("乘车人", {"乘车人": category}) == expected


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("width", [640, 1000])
def test_confirmation_summary_fits_all_lines_and_last_line_can_be_scrolled_into_view(
    main_window, qtbot, dark, width,
):
    application = QApplication.instance()
    previous_stylesheet = application.styleSheet()
    try:
        application.setStyleSheet(gui_app._load_stylesheet(application, dark))
        main_window.resize(width, 720)
        main_window.passengers.setText("示例乘车人甲、示例乘车人乙、示例乘车人丙、示例乘车人丁、示例乘车人戊")
        set_cart(main_window, ["二等座", "一等座", "商务座", "一等卧", "二等卧"])
        main_window.preferred_trains.setText("G79，G81，D123，K45")
        main_window.account_state = "valid"
        main_window.show()
        main_window._go_to_step(2)
        qtbot.wait(100)
        label = main_window.confirm_summary
        assert "停止：" in label.text()
        assert label.text().count("\n") == 2
        assert main_window.confirm_cart.items() == main_window.cart_items
        for entry in main_window.cart_items:
            assert entry["seat_type"] in main_window.confirm_cart.text()
        required_height = label.heightForWidth(label.width())
        assert label.height() >= required_height, (
            f"summary is clipped: {label.width()}x{label.height()}, needs {required_height}"
        )
        bottom_on_page = label.mapTo(main_window.confirm_scroll.widget(), QPoint(0, label.height() - 1))
        main_window.confirm_scroll.ensureVisible(bottom_on_page.x(), bottom_on_page.y(), 0, 4)
        qtbot.wait(50)
        bottom_in_view = label.mapTo(main_window.confirm_scroll.viewport(), QPoint(0, label.height() - 1))
        assert main_window.confirm_scroll.viewport().rect().contains(bottom_in_view)
    finally:
        application.setStyleSheet(previous_stylesheet)
