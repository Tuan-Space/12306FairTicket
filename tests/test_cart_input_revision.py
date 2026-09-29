"""Real Qt input events for the compact cart entry controls (no network)."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QDate, QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication, QAbstractItemView, QHBoxLayout, QLineEdit, QToolButton, QWidget

from ticket_app.configuration import SEAT_SPECS
from ticket_app.gui.app import _load_stylesheet
from ticket_app.gui.widgets import (
    CleanDoubleSpinBox, CleanSpinBox, DatePickerWidget, OrderedSeatSelector, TimeFieldsWidget,
)


@pytest.fixture(params=(True, False), ids=("dark", "light"))
def theme(request, qapp):
    old = qapp.styleSheet()
    qapp.setStyleSheet(_load_stylesheet(qapp, request.param))
    yield
    qapp.setStyleSheet(old)


@pytest.mark.parametrize("target", ("editor", "body", "arrow"))
def test_entire_date_field_opens_one_calendar_and_escape_closes(qtbot, theme, target):
    picker = DatePickerWidget()
    qtbot.addWidget(picker)
    picker.resize(350, 40)
    picker.show()
    if target == "editor":
        qtbot.mouseClick(picker.lineEdit(), Qt.MouseButton.LeftButton)
    else:
        point = QPoint(3, picker.height() // 2) if target == "body" else QPoint(picker.width() - 12, picker.height() // 2)
        qtbot.mouseClick(picker, Qt.MouseButton.LeftButton, pos=point)
    qtbot.waitUntil(picker.calendarWidget().isVisible)
    assert QApplication.activePopupWidget() is not None
    qtbot.keyClick(picker.calendarWidget(), Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: not picker.calendarWidget().isVisible())


def test_calendar_keeps_keyboard_open_and_date_selection(qtbot):
    picker = DatePickerWidget()
    qtbot.addWidget(picker)
    picker.show()
    picker.setFocus()
    qtbot.keyClick(picker, Qt.Key.Key_Down, Qt.KeyboardModifier.AltModifier)
    qtbot.waitUntil(picker.calendarWidget().isVisible)
    picker.calendarWidget().setSelectedDate(QDate.currentDate().addDays(2))
    qtbot.keyClick(picker.calendarWidget().findChild(QAbstractItemView), Qt.Key.Key_Return)
    assert picker.date() == QDate.currentDate().addDays(2)
    assert not picker.calendarWidget().isVisible()


@pytest.mark.parametrize("widget_type", (CleanSpinBox, CleanDoubleSpinBox))
def test_focused_numeric_input_ignores_wheel_but_keeps_keyboard(qtbot, widget_type):
    spin = widget_type()
    qtbot.addWidget(spin)
    spin.setRange(0, 20)
    spin.setValue(7)
    spin.show()
    spin.activateWindow()
    spin.setFocus()
    qtbot.waitUntil(spin.hasFocus)
    event = QWheelEvent(
        QPointF(8, 8), QPointF(8, 8), QPoint(), QPoint(0, 120),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.ScrollUpdate, False,
    )
    QApplication.sendEvent(spin, event)
    assert spin.value() == 7
    assert not event.isAccepted()
    qtbot.keyClick(spin, Qt.Key.Key_Up)
    assert spin.value() == 8


def test_two_time_fields_fit_one_compact_row_and_preserve_optional_values(qtbot, theme):
    host = QWidget()
    qtbot.addWidget(host)
    row = QHBoxLayout(host)
    start = TimeFieldsWidget(optional=True, disabled_label="立即开始")
    stop = TimeFieldsWidget(optional=True, disabled_label="不设停止时间")
    row.addWidget(start)
    row.addWidget(stop)
    host.resize(490, 130)
    host.show()
    start.setText("09:05:03")
    stop.setText("11:12:13")
    assert start.text() == "09:05:03"
    assert stop.text() == "11:12:13"
    assert [part.lineEdit().text() for part in start.parts] == ["09", "05", "03"]
    assert start.geometry().right() < stop.geometry().left()
    for field in (start, stop):
        for part in field.parts:
            assert field.rect().contains(part.geometry())
        assert field.optional_checkbox.geometry().right() <= field.width()
    qtbot.mouseClick(start.optional_checkbox, Qt.MouseButton.LeftButton)
    assert start.text() == ""
    assert stop.text() == "11:12:13"
    qtbot.mouseClick(start.optional_checkbox, Qt.MouseButton.LeftButton)
    assert start.text() == "09:05:03"


@pytest.mark.parametrize("height", (40, 48, 60))
def test_clear_action_is_centered_and_clears_without_resizing(qtbot, theme, height):
    line = QLineEdit("北京南")
    line.setClearButtonEnabled(True)
    qtbot.addWidget(line)
    line.resize(300, height)
    line.show()
    qtbot.wait(30)
    button = next(item for item in line.findChildren(QToolButton) if item.isVisible())
    assert abs(button.geometry().center().y() - line.rect().center().y()) <= 1
    assert button.geometry().bottom() < line.height()
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    assert line.text() == ""
    assert line.height() == height


def test_grouped_seats_have_all_choices_three_columns_and_start_empty(qtbot, theme):
    selector = OrderedSeatSelector()
    qtbot.addWidget(selector)
    selector.resize(490, 440)
    selector.show()
    assert set(selector.checkboxes) == set(SEAT_SPECS)
    assert selector.selected_seats() == []
    assert all(button.isChecked() for button in selector.group_toggles.values())
    for name, seats in selector.SEAT_GROUPS:
        grid = selector.groups[name].layout()
        for index, seat in enumerate(seats):
            assert grid.itemAtPosition(index // 3, index % 3).widget() is selector.checkboxes[seat]
            assert selector.groups[name].rect().contains(selector.checkboxes[seat].geometry())


def test_full_cell_click_and_keyboard_keep_selection_order_and_single_signal(qtbot, theme):
    selector = OrderedSeatSelector()
    qtbot.addWidget(selector)
    selector.resize(490, 440)
    selector.show()
    changes = []
    selector.changed.connect(lambda: changes.append(selector.selected_seats()))
    second = selector.checkboxes["二等座"]
    sleeper = selector.checkboxes["二等卧"]
    # Whitespace at the far edge must be as clickable as the indicator/text.
    qtbot.mouseClick(second, Qt.MouseButton.LeftButton, pos=QPoint(second.width() - 5, second.height() // 2))
    sleeper.setFocus()
    qtbot.keyClick(sleeper, Qt.Key.Key_Space)
    qtbot.mouseClick(second, Qt.MouseButton.LeftButton)
    qtbot.mouseClick(second, Qt.MouseButton.LeftButton)
    assert changes == [["二等座"], ["二等座", "二等卧"], ["二等卧"], ["二等卧", "二等座"]]
    assert sleeper.text().startswith("1")
    assert second.text().startswith("2")
    selector.setEnabled(False)
    qtbot.mouseClick(sleeper, Qt.MouseButton.LeftButton)
    assert selector.selected_seats() == ["二等卧", "二等座"]


def test_setting_and_collapsing_seats_preserves_order_and_returns_copy(qtbot):
    selector = OrderedSeatSelector()
    qtbot.addWidget(selector)
    selector.show()
    selector.set_selected_seats(["二等卧", "二等座", "二等卧", "不存在"])
    assert selector.selected_seats() == ["二等卧", "二等座"]
    values = selector.selected_seats()
    values.clear()
    qtbot.mouseClick(selector.group_toggles["卧铺"], Qt.MouseButton.LeftButton)
    assert not selector.groups["卧铺"].isVisible()
    assert selector.selected_seats() == ["二等卧", "二等座"]
