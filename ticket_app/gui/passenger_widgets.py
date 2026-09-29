"""Small, privacy-conscious passenger editors for the desktop form."""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Mapping

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QGridLayout, QLabel, QSizePolicy, QStyle,
    QStyleOptionButton, QStylePainter, QVBoxLayout, QWidget,
)


def default_ticket_label(name: str, contact_types: Mapping[str, Any] | None = None) -> str:
    """Explain the default without changing the serialized override value."""
    category = str((contact_types or {}).get(name, ""))
    label = {"1": "成人", "3": "学生"}.get(category)
    return f"12306默认（{label}）" if label else "12306默认"


def _default_ticket_tooltip(name: str, contact_types: Mapping[str, Any]) -> str:
    category = str(contact_types.get(name, ""))
    label = {"1": "成人票", "3": "学生票"}.get(category)
    return "默认按 12306 乘客信息购买" + (f"；{name}当前为{label}。" if label else "，登录读取联系人后确认票种。")


class _PassengerNameLabel(QLabel):
    """Keep complete names accessible while eliding within one grid cell."""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setToolTip(text)
        self.setAccessibleName(text)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(0)

    def paintEvent(self, event: Any) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        text = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, self.contentsRect().width())
        painter.drawText(self.contentsRect(), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)


class PassengerTicketEditor(QWidget):
    """Overrides are keyed by names so reordering never moves a ticket type."""

    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.rows: dict[str, QComboBox] = {}
        self.cells: dict[str, QWidget] = {}
        self.name_labels: dict[str, QLabel] = {}
        self._contact_types: dict[str, str] = {}
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(8)
        self._grid.setVerticalSpacing(8)
        for column in range(3):
            self._grid.setColumnStretch(column, 1)
        self.set_names([])

    def values(self) -> dict[str, str]:
        return {name: str(combo.currentData()) for name, combo in self.rows.items() if combo.currentData()}

    def set_contact_types(self, contact_types: Mapping[str, Any]) -> None:
        """Update displayed defaults silently; explicit choices remain intact."""
        self._contact_types = {name: str(value) for name, value in contact_types.items()}
        for name, combo in self.rows.items():
            blocked = combo.blockSignals(True)
            combo.setItemText(0, default_ticket_label(name, self._contact_types))
            combo.setItemData(0, _default_ticket_tooltip(name, self._contact_types), Qt.ItemDataRole.ToolTipRole)
            combo.setToolTip(_default_ticket_tooltip(name, self._contact_types))
            combo.blockSignals(blocked)

    def set_names(self, names: Iterable[str], overrides: Mapping[str, str] | None = None) -> None:
        selected = self.values() if overrides is None else dict(overrides)
        while self._grid.count():
            item = self._grid.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        self.rows = {}
        self.cells = {}
        self.name_labels = {}
        for index, name in enumerate(dict.fromkeys(names)):
            cell = QWidget()
            cell.setMinimumWidth(0)
            cell.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            layout = QVBoxLayout(cell)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(4)
            label = _PassengerNameLabel(name)
            layout.addWidget(label)
            combo = QComboBox()
            combo.setObjectName("passengerTicketType")
            combo.setAccessibleName(f"{name}的车票类型")
            combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            combo.setMinimumWidth(0)
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(8)
            combo.addItem(default_ticket_label(name, self._contact_types), "")
            combo.setItemData(0, _default_ticket_tooltip(name, self._contact_types), Qt.ItemDataRole.ToolTipRole)
            combo.setToolTip(_default_ticket_tooltip(name, self._contact_types))
            combo.addItem("成人票", "adult")
            combo.addItem("学生票", "student")
            combo.setCurrentIndex(max(0, combo.findData(selected.get(name, ""))))
            combo.currentIndexChanged.connect(lambda _index: self.changed.emit())
            layout.addWidget(combo)
            self._grid.addWidget(cell, index // 3, index % 3)
            self.rows[name] = combo
            self.cells[name] = cell
            self.name_labels[name] = label
        self.setVisible(bool(self.rows))


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

    def sizeHint(self) -> QSize:  # noqa: N802
        original = super().sizeHint()
        return QSize(min(original.width(), 160), original.height())

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(0, super().minimumSizeHint().height())

    def paintEvent(self, event: Any) -> None:  # noqa: N802
        option = QStyleOptionButton()
        self.initStyleOption(option)
        rect = self.style().subElementRect(QStyle.SubElement.SE_CheckBoxContents, option, self)
        option.text = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, max(0, rect.width() - 4))
        painter = QStylePainter(self)
        painter.drawControl(QStyle.ControlElement.CE_CheckBox, option)


class InlinePassengerSelector(QWidget):
    """Account contacts embedded in the preparation page, in selection order."""

    changed = Signal()
    title_changed = Signal(str)

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
        self.title = QLabel()
        self.title.setObjectName("fieldLabel")
        layout.addWidget(self.title)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setObjectName("warningText")
        self.status.hide()
        layout.addWidget(self.status)
        self._items = QGridLayout()
        self._items.setContentsMargins(0, 0, 0, 0)
        self._items.setHorizontalSpacing(8)
        self._items.setVerticalSpacing(8)
        for column in range(3):
            self._items.setColumnStretch(column, 1)
        self._refresh_button: QWidget | None = None
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

    def set_refresh_button(self, button: QWidget) -> None:
        """Place the page-owned refresh action beside the first contact row."""
        if self._refresh_button is not None and self._refresh_button is not button:
            self._items.removeWidget(self._refresh_button)
        self._refresh_button = button
        self._items.addWidget(button, 0, 3, alignment=Qt.AlignmentFlag.AlignTop)

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
            box.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            box.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            box.setMinimumWidth(0)
            box.toggled.connect(lambda checked, item=box: self._toggle(item, checked))
            self._contact_boxes.append(box)
            index = len(self._contact_boxes) - 1
            self._items.addWidget(box, index // 3, index % 3)
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
            box.setToolTip(f"{name} · {category}")
            if counts[name] > 1:
                box.setText(box.text() + "（重名，无法按姓名唯一匹配）")
                box.setToolTip(f"{name} · {category}\n账号内有同名联系人，请先在 12306 确认并处理；不能按姓名直接选择。")
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
        title = f"乘车人（{len(self._selection)}/5）"
        self.title.setText(title)
        self.title_changed.emit(title)
        self.status.clear()
        self.status.hide()

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
                self.status.show()
                return
            self._selection.append(name)
        elif not checked and name in self._selection:
            self._selection.remove(name)
        self._update_status()
        self.changed.emit()

    def selected_names(self) -> list[str]:
        return list(self._selection)
