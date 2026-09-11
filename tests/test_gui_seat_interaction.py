"""Mouse and keyboard regressions for the two-column seat selector."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QDrag, QDropEvent, QMouseEvent
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication, QStyle, QStyleOptionViewItem

from ticket_app.configuration import SEAT_SPECS
from ticket_app.gui.app import _load_stylesheet
from ticket_app.gui import widgets
from ticket_app.gui.widgets import PriorityListEditor


@pytest.fixture(params=[False, True], ids=["light", "dark"])
def seat_editor(qtbot, qapp, request):
    previous_style = qapp.styleSheet()
    qapp.setStyleSheet(_load_stylesheet(qapp, request.param))
    editor = PriorityListEditor(SEAT_SPECS)
    qtbot.addWidget(editor)
    editor.resize(720, 240)
    editor.show()
    qapp.processEvents()
    yield editor
    qapp.setStyleSheet(previous_style)


def _point(editor, row: int, target: str) -> QPoint:
    view = editor.list
    index = view.model().index(row, 0)
    rect = view.visualRect(index)
    if target == "indicator":
        option = QStyleOptionViewItem()
        view.initViewItemOption(option)
        view.itemDelegate().initStyleOption(option, index)
        option.rect = rect
        return view.style().subElementRect(
            QStyle.SubElement.SE_ItemViewItemCheckIndicator, option, view
        ).center()
    if target == "label":
        return QPoint(rect.left() + 70, rect.center().y())
    return QPoint(rect.right() - 12, rect.center().y())


@pytest.mark.parametrize("width", [500, 860])
def test_all_seat_cells_remain_clickable_in_two_columns(seat_editor, qtbot, width):
    editor = seat_editor
    editor.resize(width, 240)
    QApplication.processEvents()
    rectangles = [editor.list.visualItemRect(editor.list.item(row)) for row in range(10)]
    for row, rect in enumerate(rectangles):
        assert rect.width() >= editor.list.gridSize().width() - 2
        assert editor.list.indexAt(rect.center()).row() == row
        if row % 2:
            assert rect.top() == rectangles[row - 1].top()
            assert rect.left() >= rectangles[row - 1].right()
    assert rectangles[-1].bottom() < editor.list.viewport().height()


@pytest.mark.parametrize("row", [0, 1, 8, 9])
@pytest.mark.parametrize("target", ["indicator", "label", "trailing_space"])
def test_each_click_toggles_once_and_emits_once(seat_editor, qtbot, row, target):
    editor = seat_editor
    value = editor.list.item(row).data(Qt.ItemDataRole.UserRole)
    changes = QSignalSpy(editor.changed)
    for count in (1, 2):
        qtbot.mouseClick(editor.list.viewport(), Qt.MouseButton.LeftButton, pos=_point(editor, row, target))
        assert (value in editor.values()) == (count == 1)
        assert changes.count() == count


def test_space_toggles_but_right_click_does_not(seat_editor, qtbot):
    editor = seat_editor
    editor.list.setCurrentRow(1)
    editor.list.setFocus()
    value = editor.list.item(1).data(Qt.ItemDataRole.UserRole)
    changes = QSignalSpy(editor.changed)
    qtbot.keyClick(editor.list, Qt.Key.Key_Space)
    assert editor.values() == [value]
    qtbot.mouseClick(editor.list.viewport(), Qt.MouseButton.RightButton, pos=_point(editor, 1, "label"))
    assert editor.values() == [value]
    assert changes.count() == 1
    qtbot.keyClick(editor.list, Qt.Key.Key_Space)
    assert editor.values() == []
    assert changes.count() == 2


def test_moving_past_drag_threshold_and_back_does_not_toggle(seat_editor, qtbot, monkeypatch):
    editor = seat_editor
    view = editor.list
    origin = _point(editor, 0, "label")
    # Suppress the platform drag loop while still exercising actual mouse events.
    monkeypatch.setattr(view, "startDrag", lambda _actions: None)
    qtbot.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=origin)
    for point in (origin + QPoint(QApplication.startDragDistance() + 5, 0), origin):
        event = QMouseEvent(
            QEvent.Type.MouseMove, QPointF(point), QPointF(view.viewport().mapToGlobal(point)),
            Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(view.viewport(), event)
    qtbot.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=origin)
    assert editor.values() == []


@pytest.mark.parametrize("source_row", [0, 5], ids=["checked", "unchecked"])
def test_drag_completion_reorders_without_removing_or_toggling_items(seat_editor, qtbot, monkeypatch, source_row):
    editor = seat_editor
    editor.set_values(["硬卧", "软卧", "二等座"])
    QApplication.processEvents()
    view = editor.list
    original_items = {view.item(row).data(Qt.ItemDataRole.UserRole) for row in range(view.count())}
    origin = _point(editor, source_row, "label")
    target = _point(editor, 3, "indicator")
    completed = []
    changes = QSignalSpy(editor.changed)

    class InternalDrop(QDropEvent):
        def source(self):
            return view

    class CompletingDrag(QDrag):
        def exec(self, actions, default_action):
            # Offscreen has no OS drag loop. Complete it at this boundary while
            # exercising the real startDrag, MIME data, dropEvent and model move.
            assert actions == Qt.DropAction.MoveAction
            assert default_action == Qt.DropAction.MoveAction
            event = InternalDrop(
                QPointF(target), actions, self.mimeData(),
                Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
            )
            # Hover/current-item updates must not replace the original drag source.
            view.setCurrentRow(3)
            view.dropEvent(event)
            assert event.isAccepted()
            completed.append(True)
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
    QApplication.processEvents()

    assert completed == [True]
    assert view.count() == 10
    assert {view.item(row).data(Qt.ItemDataRole.UserRole) for row in range(view.count())} == original_items
    expected = ["软卧", "二等座", "硬卧"] if source_row == 0 else ["硬卧", "软卧", "二等座"]
    assert editor.values() == expected
    assert changes.count() == 1


def test_canceled_drag_does_not_toggle_on_release(seat_editor, qtbot, monkeypatch):
    editor = seat_editor
    view = editor.list
    editor.set_values(["硬卧"])
    QApplication.processEvents()
    origin = _point(editor, 0, "label")

    class CanceledDrag(QDrag):
        def exec(self, _actions, _default_action):
            return Qt.DropAction.IgnoreAction

    monkeypatch.setattr(widgets, "QDrag", CanceledDrag)
    qtbot.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=origin)
    moved = origin + QPoint(QApplication.startDragDistance() + 5, 0)
    event = QMouseEvent(
        QEvent.Type.MouseMove, QPointF(moved), QPointF(view.viewport().mapToGlobal(moved)),
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(view.viewport(), event)
    qtbot.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=origin)
    assert editor.values() == ["硬卧"]
    assert view.count() == 10
