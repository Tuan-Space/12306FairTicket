"""Focused offline coverage for the reusable GUI input widgets."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import QAbstractSpinBox, QLabel

from ticket_app.gui.widgets import (
    BerthCountWidget,
    CleanDoubleSpinBox,
    CleanSpinBox,
    DatePickerWidget,
    HelpLabel,
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


def test_priority_list_is_fully_expanded_and_keeps_drag_order(qtbot) -> None:
    editor = PriorityListEditor([f"席别 {index}" for index in range(10)])
    qtbot.addWidget(editor)
    editor.set_values(["席别 4", "席别 2", "席别 4", "已废弃席别"])

    assert editor.list.count() == 10
    assert editor.list.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert editor.list.height() >= 10 * 32
    assert editor.values() == ["席别 4", "席别 2"]
    assert editor.list.dragDropMode() == editor.list.DragDropMode.InternalMove


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
