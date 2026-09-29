"""Compact contact and ticket-type grids preserve names, order and overrides."""

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QPushButton

from ticket_app.gui.passenger_widgets import InlinePassengerSelector, PassengerTicketEditor


def contacts(names):
    return [{"name": name, "passenger_type": "3" if index == 0 else "1"}
            for index, name in enumerate(names)]


def test_contact_grid_has_three_columns_and_refresh_beside_first_row(qtbot):
    selector = InlinePassengerSelector()
    qtbot.addWidget(selector)
    selector.set_contacts(contacts([f"乘车人{i}" for i in range(7)]))
    refresh = QPushButton("刷新")
    selector.set_refresh_button(refresh)
    selector.resize(620, 210)
    selector.show()
    qtbot.wait(10)
    for index, box in enumerate(selector.checkboxes):
        assert selector._items.getItemPosition(selector._items.indexOf(box))[:2] == (index // 3, index % 3)
    assert selector._items.getItemPosition(selector._items.indexOf(refresh))[:2] == (0, 3)
    assert refresh.geometry().left() > selector.checkboxes[2].geometry().right()
    assert selector.title.text() == "乘车人（0/5）"
    assert selector.status.isHidden()


def test_contact_cells_preserve_click_order_keyboard_limit_and_duplicate_guard(qtbot):
    selector = InlinePassengerSelector()
    qtbot.addWidget(selector)
    selector.set_contacts(contacts([f"乘车人{i}" for i in range(6)] + ["同名", "同名"]))
    selector.resize(620, 250)
    selector.show()
    for index in (4, 0, 2, 1):
        box = selector.checkboxes[index]
        qtbot.mouseClick(box, Qt.MouseButton.LeftButton, pos=QPoint(box.width() - 3, box.height() // 2))
    selector.checkboxes[3].setFocus()
    qtbot.keyClick(selector.checkboxes[3], Qt.Key.Key_Space)
    assert selector.selected_names() == ["乘车人4", "乘车人0", "乘车人2", "乘车人1", "乘车人3"]
    assert selector.title.text() == "乘车人（5/5）"
    selector.checkboxes[5].click()
    assert not selector.checkboxes[5].isChecked()
    assert not selector.status.isHidden() and "最多选择 5" in selector.status.text()
    assert all(not box.isEnabled() for box in selector.checkboxes[-2:])
    selector.checkboxes[0].click()
    selector.checkboxes[0].click()
    assert selector.selected_names()[-1] == "乘车人0"
    assert selector.status.isHidden()


@pytest.mark.parametrize("width", [360, 620])
def test_long_names_do_not_expand_contact_grid_and_keep_full_accessibility(qtbot, width):
    long_name = "一位姓名很长的测试旅客" * 8
    selector = InlinePassengerSelector()
    qtbot.addWidget(selector)
    selector.set_contacts(contacts([long_name, "乙", "丙"]))
    selector.set_refresh_button(QPushButton("刷新"))
    selector.resize(width, 120)
    selector.show()
    qtbot.wait(10)
    first = selector.checkboxes[0]
    assert selector.width() == width
    assert abs(first.width() - selector.checkboxes[1].width()) <= 1
    assert long_name in first.text() and long_name in first.accessibleName()
    assert long_name in first.toolTip()
    qtbot.mouseClick(first, Qt.MouseButton.LeftButton)
    assert selector.selected_names() == [long_name]


def test_ticket_cells_use_three_columns_with_name_above_combo_and_hide_when_empty(qtbot):
    editor = PassengerTicketEditor()
    qtbot.addWidget(editor)
    assert editor.isHidden() and editor._grid.count() == 0
    names = [f"乘车人{i}" for i in range(5)]
    editor.set_names(names)
    editor.resize(600, 160)
    editor.show()
    qtbot.wait(10)
    for index, name in enumerate(names):
        assert editor._grid.getItemPosition(editor._grid.indexOf(editor.cells[name]))[:2] == (index // 3, index % 3)
        assert editor.name_labels[name].geometry().bottom() < editor.rows[name].geometry().top()
    editor.set_names([])
    assert editor.isHidden() and editor._grid.count() == 0 and editor.values() == {}


def test_short_default_labels_keep_full_meaning_and_stable_overrides(qtbot):
    editor = PassengerTicketEditor()
    qtbot.addWidget(editor)
    editor.set_names(["学生甲", "成人乙", "未知丙"])
    assert editor.rows["未知丙"].itemText(0) == "12306默认"
    editor.rows["学生甲"].setCurrentIndex(editor.rows["学生甲"].findData("adult"))
    changes = []
    editor.changed.connect(lambda: changes.append(editor.values()))
    editor.set_contact_types({"学生甲": "3", "成人乙": "1"})
    assert editor.rows["学生甲"].itemText(0) == "12306默认（学生）"
    assert editor.rows["成人乙"].currentText() == "12306默认（成人）"
    assert "按 12306 乘客信息购买" in editor.rows["学生甲"].itemData(0, Qt.ItemDataRole.ToolTipRole)
    assert "学生票" in editor.rows["学生甲"].toolTip()
    editor.set_names(["未知丙", "学生甲"])
    assert editor.values() == {"学生甲": "adult"}
    assert changes == []


def test_long_ticket_names_stay_in_equal_cells_and_keep_full_name(qtbot):
    name = "名字很长的旅客" * 12
    editor = PassengerTicketEditor()
    qtbot.addWidget(editor)
    editor.set_names([name, "乙", "丙"])
    editor.resize(480, 110)
    editor.show()
    qtbot.wait(10)
    assert editor.width() == 480
    assert abs(editor.cells[name].width() - editor.cells["乙"].width()) <= 1
    assert editor.name_labels[name].text() == name
    assert editor.name_labels[name].toolTip() == name
    assert name in editor.rows[name].accessibleName()
