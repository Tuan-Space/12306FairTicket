"""Compact cart rows retain accessible controls, endpoint data and ordering."""

from copy import deepcopy

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QAccessible
from PySide6.QtWidgets import QAbstractButton, QDialog, QPushButton

from ticket_app.gui.cart_widgets import CartDialog, CompactCartSummary, _CartRowDelegate


def entry(code="G103", seat="二等座", origin="北京南", destination="上海虹桥"):
    return {"from_station": origin, "to_station": destination, "train_scope": "specific",
            "train_code": code, "seat_type": seat}


def test_rows_are_single_line_and_only_save_cancel_remain_in_footer(qtbot):
    dialog = CartDialog([entry(), entry("D17", "二等卧"), entry("G105", "一等座")])
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.wait(10)
    assert dialog.height() < 360
    for row in range(dialog.list.count()):
        item = dialog.list.item(row)
        assert dialog.list.visualItemRect(item).height() == 44
        assert "\n" not in item.text()
    assert {button.text() for button in dialog.findChildren(QPushButton) if button.isVisible()} == {"保存修改", "取消"}


def test_native_icon_buttons_are_accessible_and_keyboard_activates_once(qtbot):
    dialog = CartDialog([entry(), entry("D17", "二等卧")])
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.activateWindow()
    button = dialog.list.action_button(1, "edit")
    assert isinstance(button, QAbstractButton)
    assert "Enter" in button.toolTip()
    accessible = QAccessible.queryAccessibleInterface(button)
    assert accessible.role() == QAccessible.Role.Button
    assert accessible.text(QAccessible.Text.Name) == "编辑第 2 项"
    assert "D17" in accessible.text(QAccessible.Text.Description)
    button.setFocus()
    qtbot.wait(10)
    qtbot.keyClick(button, Qt.Key.Key_Return)
    assert dialog.edit_title.text() == "编辑第 2 项"
    dialog.cancel_edit()
    remove = dialog.list.action_button(1, "remove")
    qtbot.mouseClick(remove, Qt.MouseButton.LeftButton)
    assert dialog.items() == [entry()]
    assert dialog.list.action_button(0, "remove").accessibleName() == "删除第 1 项"


def test_icon_action_follows_persistent_row_after_keyboard_reorder(qtbot):
    original = [entry(), entry("D17", "二等卧"), entry("G105", "一等座")]
    dialog = CartDialog(original)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.activateWindow()
    edit_second = dialog.list.action_button(1, "edit")
    edit_second.setFocus()
    qtbot.wait(10)
    qtbot.keyClick(edit_second, Qt.Key.Key_Up, Qt.KeyboardModifier.AltModifier)
    assert dialog.items() == [original[1], original[0], original[2]]
    qtbot.mouseClick(dialog.list.action_button(0, "edit"), Qt.MouseButton.LeftButton)
    assert dialog.train_code.text() == "D17"
    dialog.train_code.setText("D19")
    dialog.save_edit()
    assert dialog.items() == [entry("D19", "二等卧"), original[0], original[2]]


@pytest.mark.parametrize("width", [450, 690])
def test_long_endpoints_keep_separate_columns_and_full_accessible_values(qtbot, width):
    original = entry(origin="很长的出发站名称测试", destination="很长的到达站名称测试")
    dialog = CartDialog([original])
    qtbot.addWidget(dialog)
    dialog.resize(width, 260)
    dialog.show()
    rect = dialog.list.visualItemRect(dialog.list.item(0))
    columns = _CartRowDelegate.columns(rect)
    assert columns["from_station"].width() > 20
    assert columns["to_station"].width() > 20
    assert columns["from_station"].right() < columns["to_station"].left()
    assert columns["grip"].left() > _CartRowDelegate.action_rects(rect)["remove"].right()
    assert original["from_station"] in dialog.list.item(0).toolTip()
    assert original["to_station"] in dialog.list.item(0).data(Qt.ItemDataRole.AccessibleTextRole)
    assert dialog.items() == [original]


def test_readonly_summary_uses_same_rows_and_has_no_mutating_children(qtbot):
    source = [entry(), entry("D17", "二等卧")]
    original = deepcopy(source)
    summary = CompactCartSummary()
    qtbot.addWidget(summary)
    summary.resize(600, 100)
    summary.set_items(source)
    summary.show()
    qtbot.wait(10)
    assert summary.height() == 96
    assert summary.verticalScrollBar().maximum() == 0
    assert summary.horizontalScrollBar().maximum() == 0
    assert not summary.findChildren(QAbstractButton)
    assert summary.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert "D17" in summary.text()
    source[0]["seat_type"] = "硬卧"
    exposed = summary.items()
    exposed[0]["train_code"] = "T109"
    assert summary.items() == original
    qtbot.keyClick(summary, Qt.Key.Key_Delete)
    assert summary.items() == original
    summary.set_items([])
    assert summary.isHidden()
    assert summary.items() == []


def test_readonly_dialog_omits_icons_and_uses_single_close_action(qtbot):
    dialog = CartDialog([entry(), entry("D17", "二等卧")], read_only=True)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.wait(10)
    assert not dialog.list.findChildren(QAbstractButton)
    assert dialog.list.action_button(0, "edit") is None
    assert dialog.list.read_only
    assert {button.text() for button in dialog.findChildren(QPushButton) if button.isVisible()} == {"关闭"}
    qtbot.mouseClick(dialog.done_button, Qt.MouseButton.LeftButton)
    assert dialog.result() == QDialog.DialogCode.Accepted
