"""Shopping-cart card actions keep the same transactional, ordered model."""

from copy import deepcopy

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QDrag, QDropEvent, QMouseEvent
from PySide6.QtWidgets import QApplication, QDialog, QWidget

from ticket_app.configuration import SEAT_SPECS
from ticket_app.gui import cart_widgets
from ticket_app.gui.app import _load_stylesheet
from ticket_app.gui.cart_widgets import CartDialog


def entry(code="G103", seat="二等座"):
    return {"from_station": "北京南", "to_station": "上海虹桥", "train_scope": "specific",
            "train_code": code, "seat_type": seat}


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_inline_actions_target_the_clicked_row_and_cancel_keeps_input(qtbot, qapp, dark):
    previous = qapp.styleSheet()
    original = [entry(), entry("D17", "二等卧"), entry("G105", "一等座")]
    source = deepcopy(original)
    try:
        qapp.setStyleSheet(_load_stylesheet(qapp, dark))
        dialog = CartDialog(source)
        qtbot.addWidget(dialog)
        dialog.show()
        qtbot.wait(10)
        assert dialog.done_button.text() == "保存修改"
        qtbot.mouseClick(dialog.list.action_button(1, "edit"), Qt.MouseButton.LeftButton)
        assert dialog.edit_title.text() == "编辑第 2 项"
        dialog.train_code.setText("D19")
        dialog.save_edit()
        assert dialog.items() == [original[0], entry("D19", "二等卧"), original[2]]
        qtbot.mouseClick(dialog.list.action_button(0, "remove"), Qt.MouseButton.LeftButton)
        assert dialog.items() == [entry("D19", "二等卧"), original[2]]
        qtbot.mouseClick(dialog.cancel_button, Qt.MouseButton.LeftButton)
        assert dialog.result() == QDialog.DialogCode.Rejected
        assert source == original
    finally:
        qapp.setStyleSheet(previous)


def test_sliding_pointer_off_delete_does_not_delete_or_reorder(qtbot):
    original = [entry(), entry("D17", "二等卧")]
    dialog = CartDialog(original)
    qtbot.addWidget(dialog)
    dialog.show()
    button = dialog.list.action_button(0, "remove")
    origin = button.rect().center()
    qtbot.mousePress(button, Qt.MouseButton.LeftButton, pos=origin)
    moved = origin - QPoint(QApplication.startDragDistance() + 20, 0)
    event = QMouseEvent(QEvent.Type.MouseMove, QPointF(moved),
                        QPointF(button.mapToGlobal(moved)),
                        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(button, event)
    qtbot.mouseRelease(button, Qt.MouseButton.LeftButton, pos=moved)
    assert dialog.items() == original
    assert not dialog.editor.isVisible()


def test_readonly_cards_have_no_edit_hit_targets_and_one_close_action(qtbot):
    original = [entry(), entry("D17", "二等卧")]
    dialog = CartDialog(original, read_only=True)
    qtbot.addWidget(dialog)
    dialog.show()
    assert dialog.done_button.text() == "关闭"
    assert not dialog.cancel_button.isVisible()
    assert dialog.list.action_button(0, "remove") is None
    dialog.list.row_action.emit("remove", 0)
    dialog.list.row_action.emit("edit", 0)
    assert dialog.items() == original
    assert not dialog.editor.isVisible()


def test_popup_centers_on_parent_and_footer_stays_available_while_editing(qtbot):
    parent = QWidget()
    qtbot.addWidget(parent)
    parent.resize(760, 700)
    parent.show()
    dialog = CartDialog([entry(), entry("D17", "二等卧")], parent)
    qtbot.addWidget(dialog)
    dialog.resize(500, 410)
    dialog.show()
    qtbot.wait(10)
    available = parent.screen().availableGeometry()
    assert available.contains(dialog.frameGeometry())
    # The frame is centered when the parent's center leaves room on the screen.
    if available.contains(parent.frameGeometry()):
        assert (dialog.frameGeometry().center() - parent.frameGeometry().center()).manhattanLength() <= 4
    dialog.edit_current()
    qtbot.wait(10)
    assert dialog.height() <= 410
    assert dialog.done_button.mapTo(dialog, dialog.done_button.rect().bottomRight()).y() < dialog.height()
    assert dialog.list.horizontalScrollBar().maximum() == 0
    dialog.accept()
    assert dialog.isVisible()
    assert "先保存" in dialog.error.text()


def test_grip_drag_and_keyboard_keep_all_twelve_seats_bound_to_their_trains(qtbot, monkeypatch):
    original = [entry(f"G{index + 100}", seat) for index, seat in enumerate(SEAT_SPECS)]
    dialog = CartDialog(original)
    qtbot.addWidget(dialog)
    dialog.show()
    view = dialog.list
    completed = []

    class InternalDrop(QDropEvent):
        def source(self):
            return view

    class CompletingDrag(QDrag):
        def exec(self, actions, default_action):
            destination = view.visualItemRect(view.item(2)).bottomLeft() + QPoint(20, -2)
            drop = InternalDrop(QPointF(destination), actions, self.mimeData(),
                                Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            view.setCurrentRow(2)
            view.dropEvent(drop)
            assert drop.isAccepted()
            completed.append(True)
            return drop.dropAction()

    monkeypatch.setattr(cart_widgets, "QDrag", CompletingDrag)
    first_rect = view.visualItemRect(view.item(0))
    origin = cart_widgets._CartRowDelegate.columns(first_rect)["grip"].center()
    qtbot.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=origin)
    moved = origin - QPoint(QApplication.startDragDistance() + 8, 0)
    move = QMouseEvent(QEvent.Type.MouseMove, QPointF(moved), QPointF(view.viewport().mapToGlobal(moved)),
                       Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(view.viewport(), move)
    qtbot.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=origin)
    assert completed == [True]
    expected = [original[1], original[2], original[0], *original[3:]]
    assert dialog.items() == expected
    dialog.activateWindow()
    view.setFocus()
    qtbot.wait(10)
    qtbot.keyClick(view, Qt.Key.Key_Up, Qt.KeyboardModifier.AltModifier)
    expected[1], expected[2] = expected[2], expected[1]
    assert dialog.items() == expected
    assert len({tuple(item.values()) for item in dialog.items()}) == 12
    assert [view.item(index).text().split(".")[0] for index in range(12)] == [str(index) for index in range(1, 13)]
