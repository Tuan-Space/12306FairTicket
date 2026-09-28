"""Offline mouse and keyboard coverage of the atomic alternative editor."""

from __future__ import annotations

import os
from copy import deepcopy

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QDrag, QDropEvent, QMouseEvent
from PySide6.QtWidgets import QApplication, QDialog

from ticket_app.configuration import SEAT_SPECS
from ticket_app.gui import cart_widgets
from ticket_app.gui.cart_widgets import CartDialog


def _entry(train="G101", seat="二等座", origin="北京南", destination="上海虹桥", scope="specific"):
    return {"from_station": origin, "to_station": destination,
            "train_scope": scope, "train_code": train, "seat_type": seat}


@pytest.fixture
def cart_dialog(qtbot):
    dialog = CartDialog([
        _entry(), _entry("G102", "一等座"),
        _entry("T109", "硬卧", "北京", "上海"),
    ], station_names=["北京南", "上海虹桥", "北京", "上海"])
    qtbot.addWidget(dialog)
    dialog.show()
    return dialog


def test_lists_atomic_rows_and_all_seats(cart_dialog):
    assert cart_dialog.list.count() == 3
    assert "北京南 → 上海虹桥" in cart_dialog.list.item(0).text()
    assert "G101" in cart_dialog.list.item(0).text()
    assert "二等座" in cart_dialog.list.item(0).text()
    assert {cart_dialog.seat_type.itemText(i) for i in range(cart_dialog.seat_type.count())} == set(SEAT_SPECS)


def test_items_are_isolated_settings_and_reject_is_non_destructive(qtbot):
    source = [_entry(), _entry("", "二等卧", scope="all")]
    source[0]["secret_str"] = "must-not-survive"
    original = deepcopy(source)
    dialog = CartDialog(source)
    qtbot.addWidget(dialog)
    dialog.list.setCurrentRow(0)
    dialog.remove_current()
    result = dialog.items()
    result[0]["seat_type"] = "一等卧"
    assert dialog.items()[0]["seat_type"] == "二等卧"
    dialog.reject()
    assert source == original
    other = CartDialog(source)
    qtbot.addWidget(other)
    assert "secret_str" not in other.items()[0]


def test_mouse_moves_all_rows_without_dropping_or_copying(cart_dialog, qtbot):
    expected = cart_dialog.items()
    for offset in range(2):
        rect = cart_dialog.list.visualItemRect(cart_dialog.list.item(offset))
        assert rect.width() > 200
        qtbot.mouseClick(cart_dialog.list.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
        qtbot.mouseClick(cart_dialog.down_button, Qt.MouseButton.LeftButton)
    assert cart_dialog.items() == [expected[1], expected[2], expected[0]]
    assert [cart_dialog.list.item(i).text()[0] for i in range(3)] == ["1", "2", "3"]
    assert not cart_dialog.down_button.isEnabled()
    qtbot.mouseClick(cart_dialog.up_button, Qt.MouseButton.LeftButton)
    assert cart_dialog.items() == [expected[1], expected[0], expected[2]]


def test_keyboard_move_edit_delete(cart_dialog, qtbot):
    expected = cart_dialog.items()
    cart_dialog.activateWindow()
    cart_dialog.list.setFocus()
    qtbot.keyClick(cart_dialog.list, Qt.Key.Key_Down, Qt.KeyboardModifier.AltModifier)
    assert cart_dialog.items() == [expected[1], expected[0], expected[2]]
    qtbot.keyClick(cart_dialog.list, Qt.Key.Key_Return)
    assert cart_dialog.editor.isVisible()
    cart_dialog.train_code.setText("g103")
    qtbot.mouseClick(cart_dialog.save_edit_button, Qt.MouseButton.LeftButton)
    assert cart_dialog.items()[1]["train_code"] == "G103"
    qtbot.keyClick(cart_dialog.list, Qt.Key.Key_Delete)
    assert cart_dialog.items() == [expected[1], expected[2]]


@pytest.mark.parametrize("source,target,expected_indices", [
    (0, 2, [1, 2, 0]), (2, 0, [2, 0, 1]), (2, "gap", [0, 2, 1]),
])
def test_mouse_drag_preserves_rows_and_priority_numbers(
    cart_dialog, qtbot, monkeypatch, source, target, expected_indices,
):
    before = cart_dialog.items()
    view = cart_dialog.list
    origin = view.visualItemRect(view.item(source)).center()
    target_rect = view.visualItemRect(view.item(target if target != "gap" else 1))
    if target == "gap":
        target_point = target_rect.topLeft() + QPoint(20, -2)
        assert not view.indexAt(target_point).isValid()
    else:
        target_point = target_rect.bottomLeft() + QPoint(20, -2) if source < target else target_rect.topLeft() + QPoint(20, 2)
    completed = []

    class InternalDrop(QDropEvent):
        def source(self):
            return view

    class CompletingDrag(QDrag):
        def exec(self, actions, default_action):
            assert default_action == Qt.DropAction.MoveAction
            event = InternalDrop(
                QPointF(target_point), actions, self.mimeData(),
                Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
            )
            # Hover updates must not replace the source captured at drag start.
            view.setCurrentRow(target if target != "gap" else 1)
            view.dropEvent(event)
            assert event.isAccepted()
            completed.append(True)
            return event.dropAction()

    monkeypatch.setattr(cart_widgets, "QDrag", CompletingDrag)
    qtbot.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=origin)
    moved = origin + QPoint(QApplication.startDragDistance() + 8, 0)
    event = QMouseEvent(
        QEvent.Type.MouseMove, QPointF(moved), QPointF(view.viewport().mapToGlobal(moved)),
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(view.viewport(), event)
    qtbot.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=origin)
    assert completed == [True]
    assert cart_dialog.items() == [before[index] for index in expected_indices]
    assert [view.item(index).text()[0] for index in range(view.count())] == ["1", "2", "3"]


def test_edit_only_selected_item_and_explicit_any_train(cart_dialog, qtbot):
    before = cart_dialog.items()
    cart_dialog.list.setCurrentRow(1)
    qtbot.mouseClick(cart_dialog.edit_button, Qt.MouseButton.LeftButton)
    cart_dialog.from_station.setText("北京")
    cart_dialog.to_station.setText("上海")
    cart_dialog.seat_type.setCurrentText("一等卧")
    cart_dialog.train_scope.setCurrentIndex(cart_dialog.train_scope.findData("all"))
    assert not cart_dialog.train_code.isEnabled()
    assert not cart_dialog.list.isEnabled()
    qtbot.mouseClick(cart_dialog.save_edit_button, Qt.MouseButton.LeftButton)
    after = cart_dialog.items()
    assert after[0] == before[0] and after[2] == before[2]
    assert after[1] == _entry("", "一等卧", "北京", "上海", "all")


def test_duplicate_edit_keeps_original_and_editable(cart_dialog):
    before = cart_dialog.items()
    cart_dialog.list.setCurrentRow(1)
    cart_dialog.edit_current()
    cart_dialog.train_code.setText("g101")
    cart_dialog.seat_type.setCurrentText("二等座")
    cart_dialog.save_edit()
    assert "第 1 项" in cart_dialog.error.text()
    assert cart_dialog.items() == before
    assert cart_dialog.editor.isVisible()


@pytest.mark.parametrize("field,value,message", [
    ("from_station", "未知站", "有效的出发站"),
    ("to_station", "北京南", "不能相同"),
    ("train_code", "G101，G102", "一个有效车次"),
    ("train_code", "", "一个有效车次"),
    ("train_code", "abcd", "一个有效车次"),
])
def test_invalid_edit_does_not_silently_change_scope(cart_dialog, field, value, message):
    before = cart_dialog.items()
    cart_dialog.edit_current()
    getattr(cart_dialog, field).setText(value)
    cart_dialog.save_edit()
    assert message in cart_dialog.error.text()
    assert cart_dialog.items() == before


def test_unfinished_editor_requires_save_or_discard(cart_dialog):
    before = cart_dialog.items()
    cart_dialog.edit_current()
    cart_dialog.train_code.setText("G900")
    cart_dialog.accept()
    assert cart_dialog.result() == QDialog.DialogCode.Rejected
    assert cart_dialog.isVisible()
    assert "先保存" in cart_dialog.error.text()
    cart_dialog.cancel_edit()
    cart_dialog.accept()
    assert cart_dialog.result() == QDialog.DialogCode.Accepted
    assert cart_dialog.items() == before


def test_readonly_blocks_all_mutation_paths(qtbot):
    original = [_entry(), _entry("G102", "一等座")]
    dialog = CartDialog(original, read_only=True, migration_warnings=["请检查旧配置的尝试顺序。"])
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.move_current(1)
    dialog.remove_current()
    dialog.edit_current()
    qtbot.keyClick(dialog.list, Qt.Key.Key_Delete)
    qtbot.keyClick(dialog.list, Qt.Key.Key_Enter)
    assert dialog.items() == original
    assert not dialog.editor.isVisible()
    assert not dialog.edit_button.isVisible()
    assert "旧配置" in dialog.migration_notice.text()


def test_can_remove_last_item_then_return_to_add(qtbot):
    dialog = CartDialog([_entry()])
    qtbot.addWidget(dialog)
    dialog.remove_current()
    assert not dialog.items()
    assert "购物车为空" in dialog.summary.text()
    assert not dialog.edit_button.isEnabled()
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted
