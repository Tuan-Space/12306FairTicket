"""Small, privacy-conscious passenger editors for the desktop form."""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Mapping

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QGridLayout, QLabel,
    QScrollArea, QVBoxLayout, QWidget,
)


def default_ticket_label(name: str, contact_types: Mapping[str, Any] | None = None) -> str:
    """Explain the default without changing the serialized override value."""
    category = str((contact_types or {}).get(name, ""))
    label = {"1": "成人票", "3": "学生票"}.get(category)
    return f"默认（12306：{label}）" if label else "默认（按12306乘客信息）"


class PassengerTicketEditor(QWidget):
    """Overrides are keyed by names so reordering never moves a ticket type."""

    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.rows: dict[str, QComboBox] = {}
        self._contact_types: dict[str, str] = {}
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setColumnStretch(0, 1)
        self.set_names([])

    def values(self) -> dict[str, str]:
        return {name: str(combo.currentData()) for name, combo in self.rows.items() if combo.currentData()}

    def set_contact_types(self, contact_types: Mapping[str, Any]) -> None:
        """Update displayed defaults silently; explicit choices remain intact."""
        self._contact_types = {name: str(value) for name, value in contact_types.items()}
        for name, combo in self.rows.items():
            blocked = combo.blockSignals(True)
            combo.setItemText(0, default_ticket_label(name, self._contact_types))
            combo.blockSignals(blocked)

    def set_names(self, names: Iterable[str], overrides: Mapping[str, str] | None = None) -> None:
        selected = self.values() if overrides is None else dict(overrides)
        while self._grid.count():
            item = self._grid.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        self.rows = {}
        for row, name in enumerate(dict.fromkeys(names)):
            combo = QComboBox()
            combo.setObjectName("passengerTicketType")
            combo.setAccessibleName(f"{name}的车票类型")
            combo.addItem(default_ticket_label(name, self._contact_types), "")
            combo.addItem("成人票", "adult")
            combo.addItem("学生票", "student")
            combo.setCurrentIndex(max(0, combo.findData(selected.get(name, ""))))
            combo.currentIndexChanged.connect(lambda _index: self.changed.emit())
            self._grid.addWidget(QLabel(name), row, 0)
            self._grid.addWidget(combo, row, 1)
            self.rows[name] = combo
        if not self.rows:
            hint = QLabel("填写或选择乘车人后，可逐人设置车票类型；默认按 12306 乘客信息购买。")
            hint.setObjectName("muted")
            hint.setWordWrap(True)
            self._grid.addWidget(hint, 0, 0, 1, 2)


def contact_display_rows(contacts: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Pass only names and categories across the worker boundary, never IDs."""

    rows = []
    for contact in contacts:
        name = str(contact.get("passenger_name") or "").strip()
        if name:
            rows.append({"name": name, "passenger_type": str(contact.get("passenger_type") or "")})
    return rows


class _ContactCheckBox(QCheckBox):
    """Make the full visible contact row clickable, including its whitespace."""

    def hitButton(self, position: Any) -> bool:
        return self.rect().contains(position)


class InlinePassengerSelector(QWidget):
    """Account contacts embedded in the preparation page, in selection order."""

    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("inlinePassengerSelector")
        self._selection: list[str] = []
        self._available_names: set[str] = set()
        self.checkboxes: list[QCheckBox] = []
        self._contact_boxes: list[QCheckBox] = []
        self._contact_rows: tuple[tuple[str, str], ...] | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self._items = QVBoxLayout()
        self._items.setContentsMargins(0, 0, 0, 0)
        self._items.setSpacing(8)
        layout.addLayout(self._items)
        self.empty_hint = QLabel("没有可选择的联系人，可重新读取，或手动填写姓名。")
        self.empty_hint.setObjectName("muted")
        self.empty_hint.setWordWrap(True)
        layout.addWidget(self.empty_hint)
        self.ambiguity_hint = QLabel("账号内存在同名联系人，已禁用对应选项；请先在 12306 处理重名。")
        self.ambiguity_hint.setObjectName("warningText")
        self.ambiguity_hint.setWordWrap(True)
        layout.addWidget(self.ambiguity_hint)
        self.set_contacts([])

    def set_contacts(
        self, rows: Iterable[Mapping[str, str]], selected_names: Iterable[str] = (),
    ) -> None:
        """Refresh data without replacing controls on repeated state events."""
        contacts = tuple(
            (str(row.get("name", "")).strip(), str(row.get("passenger_type", "")))
            for row in rows if str(row.get("name", "")).strip()
        )
        if contacts == self._contact_rows:
            self.set_selected_names(selected_names)
            return
        self._contact_rows = contacts
        counts = Counter(name for name, _category in contacts)
        self._available_names = {name for name, count in counts.items() if name and count == 1}
        labels = {"1": "成人", "2": "儿童", "3": "学生", "4": "残军"}
        while len(self._contact_boxes) < len(contacts):
            box = _ContactCheckBox()
            box.setObjectName("contactChoice")
            box.toggled.connect(lambda checked, item=box: self._toggle(item, checked))
            self._contact_boxes.append(box)
            self._items.addWidget(box)
        self.checkboxes = self._contact_boxes[:len(contacts)]
        for index, box in enumerate(self._contact_boxes):
            if index >= len(contacts):
                blocked = box.blockSignals(True)
                box.setChecked(False)
                box.blockSignals(blocked)
                box.hide()
                continue
            name, raw_category = contacts[index]
            category = labels.get(raw_category, "账号联系人")
            box.setText(f"{name} · {category}")
            box.setProperty("passengerName", name)
            box.setAccessibleName(f"选择乘车人{name}，{category}")
            box.setEnabled(counts[name] == 1)
            box.setToolTip("")
            if counts[name] > 1:
                box.setText(box.text() + "（重名，无法按姓名唯一匹配）")
                box.setToolTip("账号内有同名联系人，请先在 12306 确认并处理；不能按姓名直接选择。")
            box.show()
        self.empty_hint.setVisible(not self.checkboxes)
        self.ambiguity_hint.setVisible(any(count > 1 for count in counts.values()))
        self.set_selected_names(selected_names)

    def set_selected_names(self, names: Iterable[str]) -> None:
        """Silently align with imported or manually entered names."""
        self._selection = [
            name for name in dict.fromkeys(names) if name in self._available_names
        ][:5]
        for box in self.checkboxes:
            blocked = box.blockSignals(True)
            box.setChecked(str(box.property("passengerName")) in self._selection)
            box.blockSignals(blocked)
        self._update_status()

    def _update_status(self) -> None:
        self.status.setText(f"已选择 {len(self._selection)} / 5 人")

    def _toggle(self, checkbox: QCheckBox, checked: bool) -> None:
        name = str(checkbox.property("passengerName"))
        if checkbox not in self.checkboxes or name not in self._available_names:
            return
        if checked and name not in self._selection:
            if len(self._selection) >= 5:
                blocked = checkbox.blockSignals(True)
                checkbox.setChecked(False)
                checkbox.blockSignals(blocked)
                self.status.setText("最多选择 5 位乘车人，请先取消一位。")
                return
            self._selection.append(name)
        elif not checked and name in self._selection:
            self._selection.remove(name)
        self._update_status()
        self.changed.emit()

    def selected_names(self) -> list[str]:
        return list(self._selection)


class PassengerSelectionDialog(QDialog):
    """Choose up to five unique contacts in checkbox-selection order."""

    def __init__(self, contacts: Iterable[Mapping[str, str]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("从 12306 账号选择乘车人")
        self.setObjectName("passengerSelectionDialog")
        self.resize(430, 440)
        self._selection: list[str] = []
        self.checkboxes: list[QCheckBox] = []
        layout = QVBoxLayout(self)
        description = QLabel("最多选择 5 位，按勾选先后回填。取消或未选择时保留当前乘车人。")
        description.setWordWrap(True)
        layout.addWidget(description)
        self.status = QLabel("已选择 0 / 5 位")
        layout.addWidget(self.status)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        panel = QWidget()
        items = QVBoxLayout(panel)
        rows = list(contacts)
        counts = Counter(item.get("name", "") for item in rows)
        labels = {"1": "成人", "2": "儿童", "3": "学生", "4": "残军"}
        for row in rows:
            name = str(row.get("name", ""))
            if not name:
                continue
            category = labels.get(str(row.get("passenger_type", "")), "账号联系人")
            box = QCheckBox(f"{name} · {category}")
            box.setProperty("passengerName", name)
            if counts[name] > 1:
                box.setText(box.text() + "（重名，无法按姓名唯一匹配）")
                box.setEnabled(False)
            box.toggled.connect(lambda checked, item=box: self._toggle(item, checked))
            self.checkboxes.append(box)
            items.addWidget(box)
        if not self.checkboxes:
            items.addWidget(QLabel("账号中没有可用的乘车人，请先在 12306 添加。"))
        items.addStretch(1)
        scroll.setWidget(panel)
        layout.addWidget(scroll, 1)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("使用所选乘车人")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def _toggle(self, checkbox: QCheckBox, checked: bool) -> None:
        name = str(checkbox.property("passengerName"))
        if checked and name not in self._selection:
            if len(self._selection) >= 5:
                checkbox.blockSignals(True)
                checkbox.setChecked(False)
                checkbox.blockSignals(False)
                self.status.setText("最多选择 5 位乘车人，请先取消一位。")
                return
            self._selection.append(name)
        elif not checked and name in self._selection:
            self._selection.remove(name)
        self.status.setText(f"已选择 {len(self._selection)} / 5 位")

    def selected_names(self) -> list[str]:
        return list(self._selection)
