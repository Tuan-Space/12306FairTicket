"""Reusable widgets used by the desktop window."""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional

from PySide6.QtCore import QDate, QEvent, QPoint, QPointF, QSize, Qt, Signal
from PySide6.QtGui import QKeyEvent, QMouseEvent, QTextCursor, QWheelEvent
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStyle,
    QStyleOptionSpinBox,
    QTabBar,
    QTabWidget,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..configuration import SEAT_SPECS
from ..preferences import BERTH_SEAT_TYPES, seat_layout_positions


def _asset_path(name: str) -> Path:
    """Return an asset path in both source and PyInstaller onedir builds."""

    frozen_root = getattr(sys, "_MEIPASS", None)
    root = Path(frozen_root) if frozen_root else Path(__file__).resolve().parents[2]
    return root / "assets" / name


def _repolish(widget: QWidget) -> None:
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    # Some item views expose only their more specific ``update(QRect)``
    # overload through PySide. Calling the QWidget implementation directly
    # keeps validation repaints generic across line edits and list widgets.
    QWidget.update(widget)


def set_validation_state(widget: QWidget, state: str = "", message: str = "") -> None:
    """Set the dynamic validation property consumed by the application QSS.

    The original tooltip is restored after an error is repaired, so validation
    messages do not permanently replace field help.
    """

    normalized = str(state).strip().lower()
    # ``warning`` is useful for incomplete drafts which can still be saved.
    # Keep the public API deliberately small while accepting the common yellow
    # aliases used by callers and stylesheets.
    if normalized in {"warn", "yellow"}:
        normalized = "warning"
    elif normalized not in {"error", "warning"}:
        normalized = ""
    previous = widget.property("validationState") or ""
    if normalized and previous != normalized:
        widget.setProperty("validationBaseToolTip", widget.toolTip())
    widget.setProperty("validationState", normalized)
    if normalized:
        widget.setToolTip(message)
        widget.setAccessibleDescription(message)
    elif previous:
        widget.setToolTip(str(widget.property("validationBaseToolTip") or ""))
        widget.setAccessibleDescription("")
    _repolish(widget)


def hide_spin_buttons(widget: QAbstractSpinBox) -> QAbstractSpinBox:
    """Apply the compact, keyboard-editable no-arrow spin-box treatment."""

    widget.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
    return widget


class CleanSpinBox(QSpinBox):
    """Integer spin box without the platform-specific up/down arrow chrome."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        hide_spin_buttons(self)

    def set_validation(self, state: str = "", message: str = "") -> None:
        set_validation_state(self, state, message)

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802 - Qt API
        """Keep scrolling the page even when the number currently has focus."""
        event.ignore()


class CleanDoubleSpinBox(QDoubleSpinBox):
    """Floating point spin box without the platform-specific arrow chrome."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        hide_spin_buttons(self)

    def set_validation(self, state: str = "", message: str = "") -> None:
        set_validation_state(self, state, message)

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802 - Qt API
        event.ignore()


class HelpLabel(QWidget):
    """A field label with a consistent, keyboard-accessible help affordance."""

    def __init__(self, text: str, help_text: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(5)
        self.label = QLabel(text)
        self.label.setObjectName("fieldLabel")
        self.help_button = QToolButton()
        self.help_button.setObjectName("helpButton")
        self.help_button.setText("?")
        self.help_button.setToolTip(help_text)
        self.help_button.setAccessibleName(f"{text}说明")
        self.help_button.setAccessibleDescription(help_text)
        self.help_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.help_button.setAutoRaise(True)
        self.help_button.setFixedSize(20, 20)
        row.addWidget(self.label)
        row.addWidget(self.help_button)
        row.addStretch(1)

    def text(self) -> str:
        return self.label.text()

    def setText(self, text: str) -> None:  # noqa: N802 - mirrors QLabel
        self.label.setText(text)


class HelpDetails(QWidget):
    """A brief heading with keyboard-accessible, initially folded details."""

    def __init__(self, title: str, details: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        row = QHBoxLayout()
        row.setSpacing(4)
        self.heading = QLabel(title)
        self.heading.setObjectName("muted")
        self.heading.setTextFormat(Qt.TextFormat.PlainText)
        self.heading.setWordWrap(False)
        row.addWidget(self.heading)
        self.button = QToolButton()
        self.button.setObjectName("helpButton")
        self.button.setText("?")
        self.button.setCheckable(True)
        self.button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.button.setFixedSize(20, 20)
        self.button.setAccessibleName(f"{title}说明")
        self.button.setToolTip("展开说明")
        row.addWidget(self.button)
        row.addStretch(1)
        layout.addLayout(row)
        self.details_label = QLabel()
        self.details_label.setObjectName("muted")
        self.details_label.setTextFormat(Qt.TextFormat.PlainText)
        self.details_label.setWordWrap(True)
        layout.addWidget(self.details_label)
        self.set_details(details)
        self.details_label.hide()
        self.button.toggled.connect(self._set_expanded)

    def set_details(self, details: str) -> None:
        self.details_label.setText(str(details))
        self.button.setAccessibleDescription(str(details))

    def _set_expanded(self, expanded: bool) -> None:
        self.details_label.setVisible(expanded)
        self.button.setToolTip("收起说明" if expanded else "展开说明")


class DatePickerWidget(QDateEdit):
    """Read-only date text with a calendar popup and no past dates."""

    changed = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("datePicker")
        self.setDisplayFormat("yyyy-MM-dd")
        self.setCalendarPopup(True)
        self.setMinimumDate(QDate.currentDate())
        self.setDate(QDate.currentDate())
        self.setToolTip("点击日期框选择乘车日期；不能选择过去的日期")
        if self.lineEdit() is not None:
            # Keep the popup active while preventing ambiguous hand-typed dates.
            self.lineEdit().setReadOnly(True)
            self.lineEdit().installEventFilter(self)
        # A dedicated object name keeps the calendar navigation neutral in
        # both application palettes instead of inheriting the platform blue.
        self.calendarWidget().setObjectName("dateCalendar")
        calendar_icon = _asset_path("calendar.svg").as_posix()
        self.setStyleSheet(
            'QDateEdit#datePicker::down-arrow {'
            f' image: url("{calendar_icon}"); width: 16px; height: 16px;'
            " }"
        )
        self.dateChanged.connect(lambda _date: self.changed.emit())

    def _open_calendar(self) -> None:
        if self.isEnabled() and not self.calendarWidget().isVisible():
            # QDateEdit has no public showPopup method. Delegate to its real
            # arrow hit target so Qt still owns positioning, Escape and dates.
            option = QStyleOptionSpinBox()
            self.initStyleOption(option)
            arrow = self.style().subControlRect(
                QStyle.ComplexControl.CC_SpinBox, option,
                QStyle.SubControl.SC_SpinBoxDown, self,
            )
            point = arrow.center()
            event = QMouseEvent(
                QEvent.Type.MouseButtonPress, QPointF(point), QPointF(self.mapToGlobal(point)),
                Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
            )
            super().mousePressEvent(event)
            release = QMouseEvent(
                QEvent.Type.MouseButtonRelease, QPointF(point), QPointF(self.mapToGlobal(point)),
                Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
            )
            super().mouseReleaseEvent(release)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt API
        if watched is self.lineEdit() and event.type() == QEvent.Type.MouseButtonPress:
            if event.button() == Qt.MouseButton.LeftButton:
                self._open_calendar()
                return True
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt API
        if event.button() == Qt.MouseButton.LeftButton:
            self._open_calendar()
            event.accept()
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt API
        if event.key() == Qt.Key.Key_F4 or (
            event.key() == Qt.Key.Key_Down and event.modifiers() & Qt.KeyboardModifier.AltModifier
        ):
            self._open_calendar()
            event.accept()
            return
        super().keyPressEvent(event)

    def set_validation(self, state: str = "", message: str = "") -> None:
        set_validation_state(self, state, message)


class _TimePartSpinBox(CleanSpinBox):
    def textFromValue(self, value: int) -> str:  # noqa: N802 - Qt API
        return f"{value:02d}"


class TimeFieldsWidget(QWidget):
    """Three explicit hour/minute/second inputs with an optional off state."""

    changed = Signal()
    _TIME_RE = re.compile(r"^(\d{1,2}):(\d{1,2}):(\d{1,2})$")

    def __init__(
        self,
        optional: bool = False,
        disabled_label: str = "不设置",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("timeFields")
        self.optional = bool(optional)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        column.addLayout(row)

        self.hour = self._part(0, 23, "时")
        self.minute = self._part(0, 59, "分")
        self.second = self._part(0, 59, "秒")
        self.parts = (self.hour, self.minute, self.second)
        for index, part in enumerate(self.parts):
            row.addWidget(part)
            if index < 2:
                separator = QLabel(":")
                separator.setObjectName("timeSeparator")
                row.addWidget(separator)

        self.optional_checkbox: Optional[QCheckBox] = None
        if self.optional:
            self.optional_checkbox = QCheckBox(disabled_label)
            self.optional_checkbox.setObjectName("timeDisabledToggle")
            self.optional_checkbox.toggled.connect(self._on_disabled_toggled)
            column.addWidget(self.optional_checkbox, 0, Qt.AlignmentFlag.AlignLeft)
        row.addStretch(1)

    def _part(self, minimum: int, maximum: int, accessible_name: str) -> CleanSpinBox:
        part = _TimePartSpinBox()
        part.setObjectName("timePart")
        part.setRange(minimum, maximum)
        part.setAlignment(Qt.AlignmentFlag.AlignCenter)
        part.setFixedWidth(44)
        part.setMinimumHeight(34)
        part.setStyleSheet("QSpinBox#timePart { padding-left: 2px; padding-right: 2px; }")
        part.setAccessibleName(accessible_name)
        part.setWrapping(False)
        part.valueChanged.connect(lambda _value: self.changed.emit())
        return part

    def text(self) -> str:
        if self.is_disabled():
            return ""
        return f"{self.hour.value():02d}:{self.minute.value():02d}:{self.second.value():02d}"

    def setText(self, value: str) -> None:  # noqa: N802 - field-like API
        raw = str(value or "").strip()
        if not raw:
            if self.optional:
                self.set_disabled(True)
                return
            raw = "00:00:00"
        match = self._TIME_RE.fullmatch(raw)
        if match is None:
            raise ValueError("时间必须使用 HH:MM:SS 格式")
        hour, minute, second = (int(part) for part in match.groups())
        if hour > 23 or minute > 59 or second > 59:
            raise ValueError("时间必须是有效的 24 小时时间")

        for part in self.parts:
            part.blockSignals(True)
        self.hour.setValue(hour)
        self.minute.setValue(minute)
        self.second.setValue(second)
        for part in self.parts:
            part.blockSignals(False)
        if self.optional:
            self.set_disabled(False, emit=False)
        self.changed.emit()

    def is_disabled(self) -> bool:
        return bool(self.optional_checkbox and self.optional_checkbox.isChecked())

    def set_disabled(self, disabled: bool, *, emit: bool = True) -> None:
        if not self.optional:
            # A required time has no off state.  Silently keep it enabled so
            # generic form code can safely call this method for both widgets.
            disabled = False
        disabled = bool(disabled)
        if self.optional_checkbox is not None:
            self.optional_checkbox.blockSignals(True)
            self.optional_checkbox.setChecked(disabled)
            self.optional_checkbox.blockSignals(False)
        self._sync_enabled(disabled)
        if emit:
            self.changed.emit()

    def _on_disabled_toggled(self, disabled: bool) -> None:
        self._sync_enabled(disabled)
        self.changed.emit()

    def _sync_enabled(self, disabled: Optional[bool] = None) -> None:
        disabled = self.is_disabled() if disabled is None else disabled
        for part in self.parts:
            part.setEnabled(self.isEnabled() and not disabled)

    def setEnabled(self, enabled: bool) -> None:  # noqa: N802 - Qt override
        super().setEnabled(enabled)
        if hasattr(self, "parts"):
            self._sync_enabled()

    def set_validation(self, state: str = "", message: str = "") -> None:
        set_validation_state(self, state, message)
        # The three parts are one logical time value. Drawing a border around
        # every spin box looks like three independent errors, so keep their
        # chrome neutral and let the containing time editor own one outline.
        for part in self.parts:
            set_validation_state(part)



class Card(QFrame):
    def __init__(self, title: str = "", subtitle: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(20, 18, 20, 18)
        self.body.setSpacing(12)
        if title:
            title_label = QLabel(title)
            title_label.setObjectName("cardTitle")
            self.body.addWidget(title_label)
        if subtitle:
            subtitle_label = QLabel(subtitle)
            subtitle_label.setObjectName("muted")
            subtitle_label.setWordWrap(True)
            self.body.addWidget(subtitle_label)


class _SeatChoiceBox(QCheckBox):
    """The whole seat cell is one checkbox hit target, including whitespace."""

    def hitButton(self, position: QPoint) -> bool:  # noqa: N802 - Qt API
        return self.rect().contains(position)


class OrderedSeatSelector(QWidget):
    """Grouped choices whose selection order is the order of user clicks."""

    changed = Signal()
    SEAT_GROUPS = (
        ("坐席", ("商务座", "特等座", "一等座", "二等座", "软座", "硬座", "无座")),
        ("卧铺", ("高级软卧", "软卧", "硬卧", "一等卧", "二等卧")),
    )

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("orderedSeatSelector")
        self._selected: List[str] = []
        self.checkboxes: Dict[str, QCheckBox] = {}
        self.groups: Dict[str, QWidget] = {}
        self.group_toggles: Dict[str, QToolButton] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        for name, labels in self.SEAT_GROUPS:
            toggle = QToolButton()
            toggle.setObjectName("seatGroupToggle")
            toggle.setText(name)
            toggle.setCheckable(True)
            toggle.setChecked(True)
            toggle.setArrowType(Qt.ArrowType.DownArrow)
            toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            toggle.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            toggle.setAccessibleName(f"展开或收起{name}")
            layout.addWidget(toggle)
            group = QWidget()
            grid = QGridLayout(group)
            grid.setContentsMargins(0, 0, 0, 4)
            grid.setHorizontalSpacing(8)
            grid.setVerticalSpacing(8)
            for index, label in enumerate(labels):
                checkbox = _SeatChoiceBox(label)
                checkbox.setObjectName("seatChoice")
                checkbox.setAccessibleName(label)
                checkbox.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
                checkbox.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
                checkbox.toggled.connect(lambda checked, seat=label: self._toggle(seat, checked))
                self.checkboxes[label] = checkbox
                grid.addWidget(checkbox, index // 3, index % 3)
            for column in range(3):
                grid.setColumnStretch(column, 1)
            toggle.toggled.connect(lambda expanded, content=group, button=toggle: self._expand_group(content, button, expanded))
            self.groups[name] = group
            self.group_toggles[name] = toggle
            layout.addWidget(group)

    @staticmethod
    def _expand_group(group: QWidget, button: QToolButton, expanded: bool) -> None:
        group.setVisible(expanded)
        button.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)

    def _toggle(self, seat: str, checked: bool) -> None:
        if checked and seat not in self._selected:
            self._selected.append(seat)
        elif not checked and seat in self._selected:
            self._selected.remove(seat)
        self._refresh()
        self.changed.emit()

    def _refresh(self) -> None:
        ranks = {seat: index for index, seat in enumerate(self._selected, 1)}
        for seat, checkbox in self.checkboxes.items():
            rank = ranks.get(seat)
            checkbox.blockSignals(True)
            checkbox.setChecked(rank is not None)
            checkbox.setText(f"{rank} · {seat}" if rank else seat)
            checkbox.setAccessibleDescription(f"第 {rank} 个加入" if rank else "未选择")
            checkbox.blockSignals(False)

    def selected_seats(self) -> List[str]:
        return list(self._selected)

    def set_selected_seats(self, seats: Iterable[str]) -> None:
        selected: List[str] = []
        for seat in seats:
            if seat in self.checkboxes and seat not in selected:
                selected.append(seat)
        if self._selected != selected:
            self._selected = selected
            self._refresh()
            self.changed.emit()








class SeatMapWidget(QWidget):
    changed = Signal()

    POSITION_LABELS = {
        "A": "靠窗",
        "B": "中间",
        "C": "过道",
        "D": "过道",
        "F": "靠窗",
    }

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._selected: List[str] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.guide = QLabel(
            "选中与乘车人数相同的格子。“前排/后排”仅表示同一订单的两排相对关系，"
            "不代表行驶方向、车厢位置或真实排号。无法满足时，接受 12306 在该席别内自动分配。"
        )
        self.guide.setObjectName("muted")
        self.guide.setWordWrap(True)
        self.guide.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.guide)

        self.inactive_hint = QLabel("座位偏好未启用：请先选择商务座、特等座、一等座或二等座。")
        self.inactive_hint.setObjectName("muted")
        self.inactive_hint.setWordWrap(True)
        self.inactive_hint.hide()
        layout.addWidget(self.inactive_hint)
        self.grid = QWidget()
        grid_layout = QVBoxLayout(self.grid)
        grid_layout.setContentsMargins(0, 0, 0, 0)
        grid_layout.setSpacing(10)
        layout.addWidget(self.grid)
        self.buttons: Dict[str, QToolButton] = {}
        for relation_row in (1, 2):
            seat_row = QHBoxLayout()
            seat_row.setSpacing(7)
            row_name = QLabel("前排" if relation_row == 1 else "后排")
            row_name.setObjectName("muted")
            row_name.setMinimumWidth(38)
            seat_row.addWidget(row_name)
            left_window = QLabel("窗")
            left_window.setObjectName("windowMarker")
            seat_row.addWidget(left_window)
            for letter in ("A", "B", "C"):
                seat_row.addWidget(self._seat_button(f"{relation_row}{letter}"))
            aisle = QLabel("过道")
            aisle.setObjectName("aisle")
            aisle.setAlignment(Qt.AlignmentFlag.AlignCenter)
            seat_row.addWidget(aisle, 1)
            for letter in ("D", "F"):
                seat_row.addWidget(self._seat_button(f"{relation_row}{letter}"))
            right_window = QLabel("窗")
            right_window.setObjectName("windowMarker")
            seat_row.addWidget(right_window)
            grid_layout.addLayout(seat_row)

        self.saved_summary = QLabel()
        self.saved_summary.setObjectName("muted")
        self.saved_summary.setWordWrap(True)
        saved_row = QHBoxLayout()
        saved_row.addWidget(self.saved_summary, 1)
        self.clear_button = QPushButton("清空座位偏好")
        self.clear_button.setEnabled(False)
        self.clear_button.clicked.connect(lambda: self.set_positions(()))
        saved_row.addWidget(self.clear_button, 0, Qt.AlignmentFlag.AlignRight)
        layout.addLayout(saved_row)
        self._available_positions = set(self.buttons)

        self._refresh()

    def _seat_button(self, token: str) -> QToolButton:
        letter = token[1]
        button = QToolButton()
        button.setObjectName("seatButton")
        button.setCheckable(True)
        button.setMinimumSize(48, 52)
        button.setToolTip(f"相对格子 {token} / {letter} 座 / {self.POSITION_LABELS[letter]}")
        button.clicked.connect(lambda checked, value=token: self._toggle(value, checked))
        self.buttons[token] = button
        self._refresh()
        return button

    def _toggle(self, token: str, checked: bool) -> None:
        if checked and token not in self._selected:
            self._selected.append(token)
        elif not checked and token in self._selected:
            self._selected.remove(token)
        self._refresh()
        self.changed.emit()

    def _refresh(self) -> None:
        for token, button in self.buttons.items():
            letter = token[1]
            if token in self._selected:
                button.setText(f"✓ {letter}\n{self.POSITION_LABELS[letter]}")
                button.setChecked(True)
            else:
                button.setText(f"{letter}\n{self.POSITION_LABELS[letter]}")
                button.setChecked(False)
        if hasattr(self, "saved_summary"):
            saved = "、".join(self._selected) or "无"
            self.saved_summary.setText(f"已保存座位偏好：{saved}")
            unavailable = [value for value in self._selected if value not in self._available_positions]
            if unavailable and self._available_positions:
                self.saved_summary.setText(
                    f"已保存座位偏好：{saved}；{'、'.join(unavailable)} 不适用于当前席别，请调整或清空。"
                )
            self.clear_button.setEnabled(bool(self._selected))

    def set_available_positions(self, positions: Iterable[str], *, business: bool = False) -> None:
        self._available_positions = set(positions)
        active = bool(self._available_positions)
        self.guide.setVisible(active)
        self.grid.setVisible(active)
        self.inactive_hint.setVisible(not active)
        for token, button in self.buttons.items():
            button.setEnabled(token in self._available_positions)
            button.setVisible(token in self._available_positions)
        self.guide.setText(
            "选中与乘车人数相同的格子。“前排/后排”仅表示同一订单的两排相对关系，"
            "不代表行驶方向、车厢位置或真实排号。无法满足时，接受 12306 在该席别内自动分配。\n"
            + ("仅显示所选席别可用的座位字母；商务座的 C 位是否提供，以实际车型为准。"
               if business else "仅显示所选席别可用的座位字母；实际开放情况以下单时 12306 返回为准。")
        )
        self._refresh()

    def positions(self) -> List[str]:
        return list(self._selected)

    def set_positions(self, positions: Iterable[str]) -> None:
        selected: List[str] = []
        for raw in positions:
            text = str(raw).upper().strip()
            if len(text) == 1 and text in self.POSITION_LABELS:
                text = "1" + text
            if text in self.buttons and text not in selected:
                selected.append(text)
        self._selected = selected
        self._refresh()
        self.changed.emit()


class BerthCountWidget(QWidget):
    changed = Signal()
    select_seat_types_requested = Signal()

    LABELS = (("lower", "下铺", "优先最高"), ("middle", "中铺", "仅部分卧铺"), ("upper", "上铺", "优先最低"))

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.help_details = HelpDetails("选择期望的铺位数量",
            "数量为 0 表示无此偏好。软卧等车型可能不提供中铺，是否支持选铺以下单时 12306 返回为准。"
            "移除卧铺备选后，已填写数量会保留但不提交。")
        layout.addWidget(self.help_details)

        self.seat_type_hint = QWidget()
        hint_layout = QVBoxLayout(self.seat_type_hint)
        hint_layout.setContentsMargins(0, 0, 0, 0)
        hint_layout.setSpacing(6)
        self.seat_type_message = QLabel("请先在第一步勾选卧铺席别（含一等卧、二等卧），并加入购物车。")
        self.seat_type_message.setObjectName("muted")
        self.seat_type_message.setWordWrap(True)
        hint_layout.addWidget(self.seat_type_message)
        self.select_seat_types_button = QPushButton("去选择卧铺席别")
        self.select_seat_types_button.clicked.connect(self.select_seat_types_requested)
        hint_layout.addWidget(self.select_seat_types_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.seat_type_hint)

        self.spins: Dict[str, QSpinBox] = {}
        self.minus_buttons: Dict[str, QToolButton] = {}
        self.plus_buttons: Dict[str, QToolButton] = {}
        for key, label, hint in self.LABELS:
            row = QHBoxLayout()
            row.setSpacing(7)
            name = QLabel(label)
            name.setMinimumWidth(52)
            minus = QToolButton()
            minus.setObjectName("berthStepButton")
            minus.setText("−")
            minus.setAccessibleName(f"减少{label}数量")
            minus.setToolTip(f"减少一张{label}")
            spin = CleanSpinBox()
            spin.setRange(0, 5)
            spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
            spin.setFixedWidth(48)
            spin.setAccessibleName(f"{label}数量")
            spin.setToolTip(hint)
            spin.valueChanged.connect(lambda _value: self._update_total())
            plus = QToolButton()
            plus.setObjectName("berthStepButton")
            plus.setText("+")
            plus.setAccessibleName(f"增加{label}数量")
            plus.setToolTip(f"增加一张{label}")
            minus.clicked.connect(lambda _checked=False, target=spin: target.stepDown())
            plus.clicked.connect(lambda _checked=False, target=spin: target.stepUp())
            self.spins[key] = spin
            self.minus_buttons[key] = minus
            self.plus_buttons[key] = plus
            row.addWidget(name)
            row.addWidget(minus)
            row.addWidget(spin)
            row.addWidget(plus)
            unit = QLabel("张")
            unit.setObjectName("muted")
            row.addWidget(unit)
            row.addStretch(1)
            layout.addLayout(row)

        self.total = QLabel("已选 0 张铺位偏好")
        self.total.setObjectName("muted")
        total_row = QHBoxLayout()
        total_row.addWidget(self.total)
        total_row.addStretch(1)
        self.clear_button = QPushButton("清空铺位偏好")
        self.clear_button.setEnabled(False)
        self.clear_button.clicked.connect(lambda: self.set_values({}))
        total_row.addWidget(self.clear_button)
        layout.addLayout(total_row)

    def set_sleeper_available(self, available: bool) -> None:
        self.seat_type_hint.setVisible(not available)
        self.seat_type_message.setText(
            "未启用：请先将卧铺备选加入购物车。"
        )
        for key, spin in self.spins.items():
            spin.setEnabled(available)
            self.minus_buttons[key].setEnabled(available)
            self.plus_buttons[key].setEnabled(available)

    def _update_total(self) -> None:
        count = sum(spin.value() for spin in self.spins.values())
        self.total.setText(f"已选 {count} 张铺位偏好")
        self.clear_button.setEnabled(count > 0)
        self.changed.emit()

    def values(self) -> Dict[str, int]:
        return {key: spin.value() for key, spin in self.spins.items()}

    def set_values(self, values: Mapping[str, object]) -> None:
        for key, spin in self.spins.items():
            spin.blockSignals(True)
            spin.setValue(max(0, int(values.get(key, 0) or 0)))
            spin.blockSignals(False)
        self._update_total()


class _ActualTabWheelBar(QTabBar):
    """Only let wheel navigation act on an actual, painted tab button."""

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802 - Qt API
        if self.tabAt(event.position().toPoint()) >= 0:
            super().wheelEvent(event)
        else:
            event.ignore()


class _CurrentPageTabs(QTabWidget):
    """Let the visible page determine height, including wrapped descriptions."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        policy = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.currentChanged.connect(self._current_changed)

    def _current_changed(self, _index: int) -> None:
        for index in range(self.count()):
            page = self.widget(index)
            page.setSizePolicy(QSizePolicy.Policy.Preferred,
                               QSizePolicy.Policy.Preferred if index == self.currentIndex() else QSizePolicy.Policy.Ignored)
            page.installEventFilter(self)
        self.updateGeometry()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if watched is self.currentWidget() and event.type() == QEvent.Type.LayoutRequest:
            self.updateGeometry()
        return super().eventFilter(watched, event)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        page = self.currentWidget()
        if page is None:
            return super().sizeHint().height()
        frame = 2 * self.style().pixelMetric(QStyle.PixelMetric.PM_DefaultFrameWidth, None, self)
        page_width = max(1, width - frame)
        height = page.layout().totalHeightForWidth(page_width) if page.layout().hasHeightForWidth() else page.sizeHint().height()
        return max(page.minimumSizeHint().height(), height) + self.tabBar().sizeHint().height() + frame

    def sizeHint(self) -> QSize:  # noqa: N802
        preferred = super().sizeHint()
        preferred.setHeight(self.heightForWidth(preferred.width()))
        return preferred

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        page = self.currentWidget()
        if page is None:
            return super().minimumSizeHint()
        preferred = page.minimumSizeHint()
        frame = 2 * self.style().pixelMetric(QStyle.PixelMetric.PM_DefaultFrameWidth, None, self)
        return QSize(max(preferred.width() + frame, self.tabBar().minimumSizeHint().width()),
                     preferred.height() + self.tabBar().minimumSizeHint().height() + frame)


class PositionPreferences(QWidget):
    changed = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.tabs = _CurrentPageTabs()
        self.tabs.setTabBar(_ActualTabWheelBar(self.tabs))
        self.tabs.setDocumentMode(True)
        self.seats = SeatMapWidget()
        self.berths = BerthCountWidget()
        self.tabs.addTab(self.seats, "座位偏好")
        self.tabs.addTab(self.berths, "铺位偏好")
        self.tabs._current_changed(0)
        self.seats.changed.connect(self.changed)
        self.berths.changed.connect(self.changed)
        layout.addWidget(self.tabs)

    def adapt_to_seats(self, seat_types: Iterable[str]) -> None:
        values = list(seat_types)
        codes = {SEAT_SPECS[value].submit_code for value in values if value in SEAT_SPECS}
        positions = {position for code in codes for position in seat_layout_positions(code, None)}
        sleeper = bool(codes.intersection(BERTH_SEAT_TYPES))
        seated = bool(positions)
        self.seats.set_available_positions(positions, business="9" in codes)
        self.berths.set_sleeper_available(sleeper)
        # Keep both pages reachable so users can clear preferences left over
        # from another seat type. Validation can otherwise fail on a hidden,
        # disabled page with no way to repair the configuration.
        self.tabs.setTabEnabled(0, True)
        self.tabs.setTabEnabled(1, True)
        self.tabs.setTabText(0, "座位偏好" if seated else "座位偏好（未启用）")
        self.tabs.setTabText(1, "铺位偏好" if sleeper else "铺位偏好（未启用）")
        self.tabs.setTabToolTip(0, "未启用；已保存偏好不会提交" if not seated else "选择同一订单中的相对座位")
        self.tabs.setTabToolTip(1, "未启用；已保存偏好不会提交" if not sleeper else "设置下、中、上铺数量")
        if sleeper and not seated:
            self.tabs.setCurrentIndex(1)
        elif seated and not sleeper and not any(self.berths.values().values()):
            self.tabs.setCurrentIndex(0)


class LogView(QWidget):
    MAX_LINES = 3000

    LEVEL_VALUE = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
    LEVEL_COLOR = {
        "DEBUG": "#7f8aa3",
        "INFO": "#c8d1e3",
        "WARNING": "#f0b35a",
        "ERROR": "#ff7682",
        "CRITICAL": "#ff5364",
    }
    LIGHT_LEVEL_COLOR = {
        "DEBUG": "#64748b",
        "INFO": "#334155",
        "WARNING": "#a35b00",
        "ERROR": "#c6283b",
        "CRITICAL": "#a4112a",
    }

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self._lines: List[tuple[str, str]] = []
        self._paused = False
        self._level_colors = self.LEVEL_COLOR
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.toolbar = QWidget()
        self.toolbar.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        controls = QHBoxLayout(self.toolbar)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(8)
        self.toggle_button = QToolButton()
        self.toggle_button.setText("详细日志")
        self.toggle_button.setToolTip("详细日志（已脱敏）")
        self.toggle_button.setAccessibleName("展开或收起详细日志")
        self.toggle_button.setCheckable(True)
        self.toggle_button.setChecked(True)
        self.toggle_button.setArrowType(Qt.ArrowType.DownArrow)
        self.toggle_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.level = QComboBox()
        self.level.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        self.level.setCurrentText("INFO")
        self.level.currentTextChanged.connect(self.refresh)
        self.level.setAccessibleName("日志级别筛选")
        self.pause = QPushButton("暂停滚动")
        self.pause.setCheckable(True)
        self.pause.toggled.connect(self._toggle_pause)
        self.clear_button = QPushButton("清空")
        self.clear_button.clicked.connect(self.clear)
        self.copy_button = QPushButton("复制")
        self.copy_button.clicked.connect(self.copy_all)
        self.export_button = QPushButton("导出")
        self.export_button.clicked.connect(self.export)
        controls.addWidget(self.toggle_button)
        controls.addWidget(self.level, 1)
        for button in (self.pause, self.clear_button, self.copy_button, self.export_button):
            controls.addWidget(button)
        layout.addWidget(self.toolbar)

        self.text = QTextEdit()
        self.text.setObjectName("logView")
        self.text.setReadOnly(True)
        self.text.setAcceptRichText(True)
        self.text.document().setMaximumBlockCount(self.MAX_LINES)
        layout.addWidget(self.text)
        self.toggle_button.toggled.connect(self._set_expanded)
        self._compact_rows = 3
        self.set_compact_rows(3)

    def _set_expanded(self, expanded: bool) -> None:
        self.text.setVisible(expanded)
        self.toggle_button.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)

    def set_compact_rows(self, rows: int) -> None:
        self._compact_rows = max(1, int(rows))
        self.text.ensurePolished()
        # Account for the QSS padding, document margin and native frame around
        # actual font line spacing, instead of fixing the whole log panel.
        height = self.text.fontMetrics().lineSpacing() * self._compact_rows
        height += 2 * int(self.text.document().documentMargin()) + 20
        self.text.setFixedHeight(height)
        self.updateGeometry()

    def set_light_palette(self, enabled: bool) -> None:
        self._level_colors = self.LIGHT_LEVEL_COLOR if enabled else self.LEVEL_COLOR
        self.set_compact_rows(self._compact_rows)
        self.refresh()

    def _toggle_pause(self, value: bool) -> None:
        self._paused = value
        self.pause.setText("继续滚动" if value else "暂停滚动")
        if not value:
            self.refresh()

    def append_line(self, line: str, level: str) -> None:
        self.append_lines(((line, level),))

    def append_lines(self, lines: Iterable[tuple[str, str]]) -> None:
        """Store and render one transport batch with a single document edit."""

        normalized: List[tuple[str, str]] = []
        for line, level in lines:
            normalized_level = level.upper() if level.upper() in self.LEVEL_VALUE else "INFO"
            normalized.append((str(line), normalized_level))
        if not normalized:
            return
        self._lines.extend(normalized)
        if len(self._lines) > self.MAX_LINES:
            del self._lines[: len(self._lines) - self.MAX_LINES]
        if self._paused:
            return
        chunks = [
            f'<div style="color:{self._level_colors[level]}; white-space:pre">{html.escape(line)}</div>'
            for line, level in normalized
            if self._matches(line, level)
        ]
        if not chunks:
            return
        cursor = self.text.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.beginEditBlock()
        if not self.text.document().isEmpty():
            cursor.insertBlock()
        cursor.insertHtml("".join(chunks))
        cursor.endEditBlock()
        self.text.setTextCursor(cursor)
        self.text.ensureCursorVisible()

    def _matches(self, line: str, level: str) -> bool:
        minimum = self.LEVEL_VALUE.get(self.level.currentText(), 20)
        return self.LEVEL_VALUE[level] >= minimum

    def refresh(self) -> None:
        self.text.clear()
        chunks = []
        for line, level in self._lines:
            if self._matches(line, level):
                chunks.append(
                    f'<div style="color:{self._level_colors[level]}; white-space:pre">{html.escape(line)}</div>'
                )
        self.text.setHtml("".join(chunks))
        self.text.moveCursor(QTextCursor.MoveOperation.End)

    def filtered_text(self) -> str:
        return "\n".join(line for line, level in self._lines if self._matches(line, level))

    def clear(self) -> None:  # type: ignore[override]
        self._lines.clear()
        self.text.clear()

    def copy_all(self) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(self.filtered_text())

    def export(self) -> None:
        name, _selected = QFileDialog.getSaveFileName(self, "导出脱敏日志", "12306FairTicket.log", "Log (*.log);;Text (*.txt)")
        if not name:
            return
        try:
            Path(name).write_text(self.filtered_text() + "\n", encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "导出失败", str(exc))
