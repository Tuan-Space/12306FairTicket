"""Grouped selection preserves draft choices while changing available controls."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QDrag, QDropEvent, QMouseEvent
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication

from ticket_app.configuration import SEAT_SPECS
from ticket_app.gui import widgets
from ticket_app.gui.app import _load_stylesheet
from ticket_app.gui.widgets import GroupedSeatEditor, PositionPreferences


@pytest.fixture(params=[False, True], ids=["light", "dark"])
def editor(qtbot, qapp, request):
    previous_style = qapp.styleSheet()
    qapp.setStyleSheet(_load_stylesheet(qapp, request.param))
    editor = GroupedSeatEditor()
    editor.resize(720, 620)
    qtbot.addWidget(editor)
    editor.show()
    QApplication.processEvents()
    yield editor
    qapp.setStyleSheet(previous_style)


@pytest.mark.parametrize("width", [500, 860])
@pytest.mark.parametrize("target", ["indicator", "label", "trailing_space"])
def test_every_group_cell_toggles_once(editor, qtbot, width, target):
    editor.resize(width, 720)
    QApplication.processEvents()
    changes = QSignalSpy(editor.changed)
    for name, checkbox in editor.checkboxes.items():
        x = {"indicator": 10, "label": 42, "trailing_space": checkbox.width() - 8}[target]
        point = QPoint(x, checkbox.height() // 2)
        assert checkbox.width() > 100
        qtbot.mouseClick(checkbox, Qt.MouseButton.LeftButton, pos=point)
        assert name in editor.values()
        qtbot.mouseClick(checkbox, Qt.MouseButton.LeftButton, pos=point)
        assert name not in editor.values()
    assert changes.count() == 20


@pytest.mark.parametrize("width", [500, 860])
def test_two_expanded_groups_and_selected_list_have_three_columns(editor, width):
    editor.resize(width, 720)
    expected_groups = {
        "seated": ["商务座", "特等座", "一等座", "二等座", "软座", "硬座", "无座"],
        "sleeper": ["高级软卧", "软卧", "硬卧"],
    }
    assert list(editor.groups) == list(expected_groups)
    assert [header.text() for header in editor.groups.values()] == ["坐席", "卧铺"]
    assert all(header.isChecked() for header in editor.groups.values())
    assert editor.values() == []
    assert not any(checkbox.isChecked() for checkbox in editor.checkboxes.values())
    editor.set_values(SEAT_SPECS)
    QApplication.processEvents()
    for key, names in expected_groups.items():
        content = editor.group_contents[key]
        layout = content.layout()
        assert layout.columnCount() == 3
        for index, name in enumerate(names):
            checkbox = editor.checkboxes[name]
            assert layout.itemAtPosition(index // 3, index % 3).widget() is checkbox
            assert checkbox.width() >= checkbox.minimumSizeHint().width()
            assert checkbox.rect().translated(checkbox.pos()).right() < content.width()
            if index % 3:
                previous = editor.checkboxes[names[index - 1]].geometry()
                assert checkbox.y() == previous.y()
                assert checkbox.x() > previous.right()
            elif index:
                assert checkbox.y() > editor.checkboxes[names[index - 1]].geometry().bottom()
    rectangles = [editor.list.visualItemRect(editor.list.item(row)) for row in range(10)]
    for index, rect in enumerate(rectangles):
        assert rect.left() == rectangles[index % 3].left()
        assert rect.top() == rectangles[index // 3 * 3].top()
        assert editor.list.indexAt(rect.center()).row() == index
        assert rect.right() < editor.list.viewport().width()
    assert rectangles[-1].bottom() < editor.list.viewport().height()


def test_group_catalog_covers_all_seats_and_retains_priority_across_classification(editor):
    assert set(editor.checkboxes) == set(SEAT_SPECS)
    editor.set_values(["硬卧", "二等座", "软座", "二等座", "未知席别"])
    changes = QSignalSpy(editor.changed)
    for classification in ("high_speed", "conventional", "all", None):
        editor.adapt_to_trains(classification)
        assert editor.values() == ["硬卧", "二等座", "软座"]
        assert editor.list.count() == 3
    assert changes.count() == 0
    assert all(editor.groups[key].isChecked() for key in editor.groups)


def test_train_input_preserves_manual_expansion_and_sleeper_focus(editor, qtbot):
    qtbot.mouseClick(editor.groups["seated"], Qt.MouseButton.LeftButton)
    qtbot.mouseClick(editor.groups["sleeper"], Qt.MouseButton.LeftButton)
    for classification in ("high_speed", "conventional", "all", None, "invalid"):
        editor.adapt_to_trains(classification)
        assert all(not header.isChecked() for header in editor.groups.values())
        assert all(content.isHidden() for content in editor.group_contents.values())
    editor.focus_sleeper_group()
    assert editor.groups["sleeper"].isChecked()
    assert not editor.groups["seated"].isChecked()
    assert not editor.group_contents["sleeper"].isHidden()
    assert editor.checkboxes["硬卧"].hasFocus()


def test_group_checkbox_and_selected_row_keep_one_shared_selection(editor, qtbot):
    changes = QSignalSpy(editor.changed)
    for name in ("硬卧", "二等座", "商务座"):
        editor.checkboxes[name].click()
    assert editor.values() == ["硬卧", "二等座", "商务座"]
    assert changes.count() == 3
    QApplication.processEvents()
    rect = editor.list.visualItemRect(editor.list.item(1))
    qtbot.mouseClick(editor.list.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    assert editor.values() == ["硬卧", "商务座"]
    assert not editor.checkboxes["二等座"].isChecked()
    assert changes.count() == 4
    editor.list.setCurrentRow(0)
    editor.list.setFocus()
    qtbot.keyClick(editor.list, Qt.Key.Key_Space)
    assert editor.values() == ["商务座"]
    assert not editor.checkboxes["硬卧"].isChecked()
    assert changes.count() == 5
    editor.checkboxes["硬卧"].setFocus()
    qtbot.keyClick(editor.checkboxes["硬卧"], Qt.Key.Key_Space)
    assert editor.values() == ["商务座", "硬卧"]
    assert changes.count() == 6


@pytest.mark.parametrize("source_row,target_row,after", [(0, 9, True), (9, 0, False), (2, 6, True)])
def test_grouped_selected_drag_keeps_every_choice_and_emits_once(
    editor, qtbot, monkeypatch, source_row, target_row, after
):
    editor.set_values(SEAT_SPECS)
    QApplication.processEvents()
    view = editor.list
    origin = view.visualItemRect(view.item(source_row)).center()
    target_rect = view.visualItemRect(view.item(target_row))
    target = QPoint(target_rect.right() - 12 if after else target_rect.left() + 12, target_rect.center().y())
    changes = QSignalSpy(editor.changed)

    class InternalDrop(QDropEvent):
        def source(self):
            return view

    class CompletingDrag(QDrag):
        def exec(self, actions, default_action):
            event = InternalDrop(
                QPointF(target), actions, self.mimeData(),
                Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
            )
            view.dropEvent(event)
            assert event.isAccepted()
            return event.dropAction()

    monkeypatch.setattr(widgets, "QDrag", CompletingDrag)
    qtbot.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=origin)
    moved = origin + QPoint(QApplication.startDragDistance() + 5, 0)
    event = QMouseEvent(
        QEvent.Type.MouseMove, QPointF(moved), QPointF(view.viewport().mapToGlobal(moved)),
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(view.viewport(), event)
    qtbot.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=origin)
    expected = list(SEAT_SPECS)
    value = expected.pop(source_row)
    destination = target_row + int(after) - int(source_row < target_row)
    expected.insert(destination, value)
    assert editor.values() == expected
    assert view.count() == 10
    assert all(checkbox.isChecked() for checkbox in editor.checkboxes.values())
    assert changes.count() == 1


@pytest.mark.parametrize("seat_types", [["硬座"], ["软座"], ["无座"], []])
def test_ordinary_seats_hide_inactive_map_and_preserve_clearable_draft(qtbot, seat_types):
    preferences = PositionPreferences()
    qtbot.addWidget(preferences)
    preferences.seats.set_positions(["1A", "1F"])
    preferences.adapt_to_seats(seat_types)
    assert preferences.seats.grid.isHidden()
    assert preferences.seats.guide.isHidden()
    assert preferences.seats.fallback.isHidden()
    assert preferences.seats.layout().alignment() & Qt.AlignmentFlag.AlignTop
    assert not preferences.seats.inactive_hint.isHidden()
    assert preferences.seats.positions() == ["1A", "1F"]
    assert "1A、1F" in preferences.seats.saved_summary.text()
    assert all(not button.isEnabled() for button in preferences.seats.buttons.values())
    assert "未启用" in preferences.tabs.tabText(0)
    preferences.seats.clear_button.click()
    assert preferences.seats.positions() == []


@pytest.mark.parametrize(
    "seat_types,letters",
    [(["二等座"], "ABCDF"), (["一等座"], "ACDF"), (["商务座"], "ACF"),
     (["特等座"], "ACF"), (["商务座", "二等座"], "ABCDF")],
)
def test_seat_map_uses_actual_seat_code_capabilities(qtbot, seat_types, letters):
    preferences = PositionPreferences()
    qtbot.addWidget(preferences)
    preferences.adapt_to_seats(seat_types)
    assert not preferences.seats.grid.isHidden()
    assert not preferences.seats.guide.isHidden()
    assert not preferences.seats.fallback.isHidden()
    assert {token for token, button in preferences.seats.buttons.items() if button.isEnabled()} == {
        f"{row}{letter}" for row in (1, 2) for letter in letters
    }
    if "商务座" in seat_types:
        assert "C 位是否提供" in preferences.seats.availability_note.text()


def test_inactive_berth_and_seat_drafts_restore_on_matching_selection(qtbot):
    preferences = PositionPreferences()
    qtbot.addWidget(preferences)
    preferences.seats.set_positions(["1B"])
    preferences.berths.set_values({"lower": 2, "middle": 1})
    preferences.adapt_to_seats(["硬座"])
    assert not preferences.berths.spins["lower"].isEnabled()
    assert preferences.berths.clear_button.isEnabled()
    assert "未启用" in preferences.tabs.tabText(1)
    preferences.adapt_to_seats(["硬卧", "二等座"])
    assert preferences.berths.spins["lower"].isEnabled()
    assert preferences.berths.values() == {"lower": 2, "middle": 1, "upper": 0}
    assert preferences.seats.positions() == ["1B"]
    assert preferences.seats.buttons["1B"].isEnabled()
    preferences.adapt_to_seats(["商务座"])
    assert preferences.seats.positions() == ["1B"]
    assert "不适用于当前席别" in preferences.seats.saved_summary.text()
    assert not preferences.seats.buttons["1B"].isEnabled()
    preferences.berths.clear_button.click()
    assert not any(preferences.berths.values().values())
