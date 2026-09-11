"""Focused offline coverage for the reusable GUI input widgets."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QDate, QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication, QAbstractSpinBox, QLabel

from ticket_app.gui.widgets import (
    BerthCountWidget,
    CleanDoubleSpinBox,
    CleanSpinBox,
    DatePickerWidget,
    HelpLabel,
    CurrentPhaseWidget,
    PositionPreferences,
    PriorityListEditor,
    SeatMapWidget,
    TimeFieldsWidget,
    set_validation_state,
)


def test_help_label_exposes_a_readable_tooltip(qtbot) -> None:
    label = HelpLabel("查询间隔", "过高频率可能被限流")
    qtbot.addWidget(label)

    assert label.text() == "查询间隔"
    assert label.help_button.toolTip() == "过高频率可能被限流"
    assert label.help_button.accessibleDescription() == "过高频率可能被限流"


def test_date_picker_uses_calendar_and_disallows_past_dates(qtbot) -> None:
    picker = DatePickerWidget()
    qtbot.addWidget(picker)
    changes: list[bool] = []
    picker.changed.connect(lambda: changes.append(True))

    picker.setDate(QDate.currentDate().addDays(1))

    assert picker.calendarPopup()
    assert picker.lineEdit().isReadOnly()
    assert picker.calendarWidget().objectName() == "dateCalendar"
    assert picker.date() == QDate.currentDate().addDays(1)
    assert changes


def test_time_fields_round_trip_and_optional_disabled_state(qtbot) -> None:
    fields = TimeFieldsWidget(optional=True, disabled_label="不设停止时间")
    qtbot.addWidget(fields)
    fields.setText("8:09:3")

    assert fields.text() == "08:09:03"
    fields.set_disabled(True)
    assert fields.is_disabled()
    assert fields.text() == ""
    assert all(not part.isEnabled() for part in fields.parts)

    fields.setText("11:12:13")
    assert not fields.is_disabled()
    assert fields.text() == "11:12:13"

    fields.set_validation("error", "时间有误")
    assert fields.property("validationState") == "error"
    assert all(part.property("validationState") in (None, "") for part in fields.parts)


def test_clean_spin_boxes_hide_platform_arrows_and_support_validation(qtbot) -> None:
    integer = CleanSpinBox()
    decimal = CleanDoubleSpinBox()
    qtbot.addWidget(integer)
    qtbot.addWidget(decimal)

    assert integer.buttonSymbols() == QAbstractSpinBox.ButtonSymbols.NoButtons
    assert decimal.buttonSymbols() == QAbstractSpinBox.ButtonSymbols.NoButtons
    set_validation_state(integer, "yellow", "请检查范围")
    assert integer.property("validationState") == "warning"
    assert integer.toolTip() == "请检查范围"
    set_validation_state(integer)
    assert integer.property("validationState") == ""


def test_log_view_appends_a_batch_as_separate_lines(qtbot) -> None:
    from ticket_app.gui.widgets import LogView

    view = LogView()
    qtbot.addWidget(view)
    view.append_lines((("first", "INFO"), ("second", "WARNING"), ("third", "ERROR")))

    assert view.text.toPlainText().splitlines() == ["first", "second", "third"]
    assert view.text.document().blockCount() == 3


def test_unfocused_clean_spin_boxes_ignore_wheel_changes(qtbot) -> None:
    spin = CleanSpinBox()
    spin.setRange(0, 10)
    spin.setValue(5)
    qtbot.addWidget(spin)
    spin.show()
    spin.clearFocus()
    event = QWheelEvent(
        QPointF(8, 8), QPointF(8, 8), QPoint(), QPoint(0, 120),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.ScrollUpdate, False,
    )

    QApplication.sendEvent(spin, event)

    assert spin.value() == 5
    assert not event.isAccepted()


def test_priority_list_is_three_column_and_shows_selected_priority_rank(qtbot) -> None:
    editor = PriorityListEditor([f"席别 {index}" for index in range(10)])
    qtbot.addWidget(editor)
    editor.set_values(["席别 4", "席别 2", "席别 4", "已废弃席别"])

    assert editor.list.count() == 10
    assert editor.COLUMNS == 3
    assert editor.list.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert editor.list.viewMode() == editor.list.ViewMode.IconMode
    assert editor.list.flow() == editor.list.Flow.LeftToRight
    assert editor.list.height() < 10 * 32
    assert editor.values() == ["席别 4", "席别 2"]
    assert editor.list.item(0).text() == "1. 席别 4"
    assert editor.list.item(1).text() == "2. 席别 2"
    assert editor.list.dragDropMode() == editor.list.DragDropMode.InternalMove

    assert editor.list._move_item(1, 0)
    assert editor.values() == ["席别 2", "席别 4"]
    assert editor.list.item(0).text() == "1. 席别 2"


def test_seat_and_berth_widgets_use_friendly_relation_and_step_controls(qtbot) -> None:
    seats = SeatMapWidget()
    berth = BerthCountWidget()
    qtbot.addWidget(seats)
    qtbot.addWidget(berth)

    labels = [label.text() for label in seats.findChildren(QLabel)]
    assert "前排" in labels
    assert "后排" in labels
    assert "不代表行驶方向" in labels[0]
    assert "关系 1" not in labels
    assert "关系 2" not in labels

    berth.plus_buttons["lower"].click()
    berth.plus_buttons["lower"].click()
    berth.minus_buttons["lower"].click()
    assert berth.values()["lower"] == 1
    assert berth.spins["lower"].buttonSymbols() == QAbstractSpinBox.ButtonSymbols.NoButtons


def test_position_preferences_tab_wheel_requires_an_actual_tab_button(qtbot) -> None:
    preferences = PositionPreferences()
    qtbot.addWidget(preferences)
    preferences.resize(500, 260)
    preferences.show()
    qtbot.wait(10)
    tabs = preferences.tabs
    tab_bar = tabs.tabBar()
    on_tab = QPointF(tab_bar.tabRect(0).center())
    switch_event = QWheelEvent(
        on_tab, on_tab, QPoint(), QPoint(0, -120),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.ScrollUpdate, False,
    )

    QApplication.sendEvent(tab_bar, switch_event)

    assert tabs.currentIndex() == 1
    assert switch_event.isAccepted()

    empty_tab_bar = QPointF(tab_bar.tabRect(tabs.count() - 1).right() + 30, tab_bar.height() // 2)
    ignored_event = QWheelEvent(
        empty_tab_bar, empty_tab_bar, QPoint(), QPoint(0, 120),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.ScrollUpdate, False,
    )

    QApplication.sendEvent(tab_bar, ignored_event)

    assert tabs.currentIndex() == 1
    assert not ignored_event.isAccepted()


def test_current_phase_widget_keeps_only_current_phase_and_allows_fallback(qtbot) -> None:
    phase = CurrentPhaseWidget()
    qtbot.addWidget(phase)

    phase.set_phase("querying", "查询余票")
    assert phase.current_phase == "querying"
    assert phase.phase_title.text() == "查询余票"
    phase.set_phase("login", "重新扫码")
    assert phase.current_phase == "login"
    assert phase.phase_title.text() == "登录 12306"
    assert phase.phase_message.text() == "重新扫码"
