"""Reusable widgets used by the desktop window."""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional

from PySide6.QtCore import QDate, QEvent, QModelIndex, QPersistentModelIndex, QPoint, QPointF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QDrag, QDropEvent, QPainter, QPen, QTextCursor, QWheelEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QListView,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QStyledItemDelegate,
    QStyle,
    QStyleOptionViewItem,
    QTabBar,
    QTabWidget,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


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
        """Do not let a passing mouse wheel silently change a number.

        Ignoring an unfocused wheel event lets the containing scroll area use
        it for page scrolling.  Once a user has explicitly focused a field,
        Qt's normal keyboard-and-wheel adjustment remains available.
        """

        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class CleanDoubleSpinBox(QDoubleSpinBox):
    """Floating point spin box without the platform-specific arrow chrome."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        hide_spin_buttons(self)

    def set_validation(self, state: str = "", message: str = "") -> None:
        set_validation_state(self, state, message)

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802 - Qt API
        if self.hasFocus():
            super().wheelEvent(event)
        else:
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
        self.setToolTip("点击右侧日历按钮选择乘车日期；不能选择过去的日期")
        if self.lineEdit() is not None:
            # Keep the popup active while preventing ambiguous hand-typed dates.
            self.lineEdit().setReadOnly(True)
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

    def set_validation(self, state: str = "", message: str = "") -> None:
        set_validation_state(self, state, message)


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
        row = QHBoxLayout(self)
        row.setContentsMargins(4, 3, 4, 3)
        row.setSpacing(6)

        self.hour = self._part(0, 23, "时")
        self.minute = self._part(0, 59, "分")
        self.second = self._part(0, 59, "秒")
        self.parts = (self.hour, self.minute, self.second)
        for index, (part, suffix) in enumerate(zip(self.parts, ("时", "分", "秒"))):
            row.addWidget(part)
            label = QLabel(suffix)
            label.setObjectName("timeUnit")
            row.addWidget(label)
            if index < 2:
                separator = QLabel(":")
                separator.setObjectName("timeSeparator")
                row.addWidget(separator)

        self.optional_checkbox: Optional[QCheckBox] = None
        if self.optional:
            self.optional_checkbox = QCheckBox(disabled_label)
            self.optional_checkbox.setObjectName("timeDisabledToggle")
            self.optional_checkbox.toggled.connect(self._on_disabled_toggled)
            row.addSpacing(5)
            row.addWidget(self.optional_checkbox)
        row.addStretch(1)

    def _part(self, minimum: int, maximum: int, accessible_name: str) -> CleanSpinBox:
        part = CleanSpinBox()
        part.setObjectName("timePart")
        part.setRange(minimum, maximum)
        part.setAlignment(Qt.AlignmentFlag.AlignCenter)
        part.setFixedWidth(48)
        part.setMinimumHeight(34)
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


class _CheckmarkItemDelegate(QStyledItemDelegate):
    """Overlay a real tick on stylesheet-painted QListWidget indicators."""

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:  # noqa: N802
        view = self.parent()
        if isinstance(view, QListView):
            # IconMode does not expand an item's size hint to its grid cell.
            return QSize(max(1, view.gridSize().width()), max(1, view.gridSize().height() - 2))
        return super().sizeHint(option, index)

    def editorEvent(self, event, model, option, index) -> bool:  # type: ignore[no-untyped-def]  # noqa: N802
        if event.type() in {
            QEvent.Type.MouseButtonPress,
            QEvent.Type.MouseButtonRelease,
            QEvent.Type.MouseButtonDblClick,
        }:
            # The view owns whole-cell clicks; retaining the native indicator
            # handler here would toggle a checkbox twice on a single click.
            return False
        return super().editorEvent(event, model, option, index)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:  # type: ignore[no-untyped-def]
        super().paint(painter, option, index)
        if index.data(Qt.ItemDataRole.CheckStateRole) != Qt.CheckState.Checked.value:
            return
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        style = opt.widget.style() if opt.widget is not None else None
        if style is None:
            return
        rect = style.subElementRect(QStyle.SubElement.SE_ItemViewItemCheckIndicator, opt, opt.widget)
        if not rect.isValid():
            return
        painter.save()
        pen = QPen(QColor("#ffffff"), 2.0)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.drawLine(
            QPointF(rect.left() + rect.width() * 0.24, rect.top() + rect.height() * 0.52),
            QPointF(rect.left() + rect.width() * 0.43, rect.top() + rect.height() * 0.71),
        )
        painter.drawLine(
            QPointF(rect.left() + rect.width() * 0.43, rect.top() + rect.height() * 0.71),
            QPointF(rect.left() + rect.width() * 0.78, rect.top() + rect.height() * 0.30),
        )
        painter.restore()


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


class _TwoColumnPriorityList(QListWidget):
    """A fixed two-column item view that keeps the model in row-major order."""

    COLUMNS = 2
    CELL_HEIGHT = 34

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(True)
        self.setMovement(QListView.Movement.Snap)
        self.setUniformItemSizes(True)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self._click_index = QPersistentModelIndex()
        self._press_position = QPoint()
        self._dragged = False
        self._drag_index = QPersistentModelIndex()

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().resizeEvent(event)
        self.sync_grid_size()

    def sync_grid_size(self) -> None:
        width = max(1, self.viewport().width())
        # IconMode compares against the inclusive right edge when wrapping.
        self.setGridSize(QSize(max(1, (width - 1) // self.COLUMNS), self.CELL_HEIGHT))
        self.doItemsLayout()

    def mousePressEvent(self, event) -> None:  # type: ignore[no-untyped-def]  # noqa: N802
        self._click_index = QPersistentModelIndex()
        self._dragged = False
        if event.button() == Qt.MouseButton.LeftButton:
            self._click_index = QPersistentModelIndex(self.indexAt(event.position().toPoint()))
            self._press_position = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # type: ignore[no-untyped-def]  # noqa: N802
        if self._click_index.isValid() and event.buttons() & Qt.MouseButton.LeftButton:
            distance = (event.position().toPoint() - self._press_position).manhattanLength()
            if distance >= QApplication.startDragDistance():
                self._dragged = True
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[no-untyped-def]  # noqa: N802
        pressed = self._click_index
        point = event.position().toPoint()
        should_toggle = (
            event.button() == Qt.MouseButton.LeftButton
            and pressed.isValid()
            and not self._dragged
            and (point - self._press_position).manhattanLength() < QApplication.startDragDistance()
            and pressed == self.indexAt(point)
        )
        self._click_index = QPersistentModelIndex()
        super().mouseReleaseEvent(event)
        if should_toggle and pressed.isValid():
            item = self.itemFromIndex(QModelIndex(pressed))
            required = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
            if item is not None and item.flags() & required == required:
                item.setCheckState(
                    Qt.CheckState.Unchecked
                    if item.checkState() == Qt.CheckState.Checked
                    else Qt.CheckState.Checked
                )

    def startDrag(self, supported_actions) -> None:  # type: ignore[no-untyped-def]  # noqa: N802
        self._dragged = True
        self._drag_index = QPersistentModelIndex(self.currentIndex())
        self._click_index = QPersistentModelIndex()
        if not self._drag_index.isValid():
            return
        index = QModelIndex(self._drag_index)
        drag = QDrag(self)
        drag.setMimeData(self.model().mimeData([index]))
        rect = self.visualRect(index)
        drag.setPixmap(self.viewport().grab(rect))
        drag.setHotSpot(self._press_position - rect.topLeft())
        try:
            # dropEvent moves the model row itself. IconMode's native startDrag
            # would additionally remove the source row after a successful drop.
            drag.exec(supported_actions & Qt.DropAction.MoveAction, Qt.DropAction.MoveAction)
        finally:
            self._drag_index = QPersistentModelIndex()
            self.setState(QAbstractItemView.State.NoState)
            drag.deleteLater()

    def _move_item(self, source_row: int, target_row: int, *, after: bool = False) -> bool:
        """Move one model row and reset any free icon positions."""

        count = self.count()
        if not 0 <= source_row < count:
            return False
        destination = count if target_row < 0 else max(0, min(count, target_row + int(after)))
        if destination in {source_row, source_row + 1}:
            return False
        moved = self.model().moveRow(QModelIndex(), source_row, QModelIndex(), destination)
        if moved:
            self.doItemsLayout()
            self.setCurrentRow(destination - 1 if source_row < destination else destination)
        return bool(moved)

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802 - Qt API
        """Turn an icon drop into a deterministic row-major model move."""

        if event.source() not in {self, self.viewport()}:
            event.ignore()
            return
        source_row = self._drag_index.row() if self._drag_index.isValid() else self.currentRow()
        point = event.position().toPoint()
        target_index = self.indexAt(point)
        target_row = target_index.row() if target_index.isValid() else -1
        after = bool(target_index.isValid() and point.x() >= self.visualRect(target_index).center().x())
        if self._move_item(source_row, target_row, after=after):
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
        else:
            event.ignore()


class PriorityListEditor(QWidget):
    """Two-column, row-major seat priority editor with direct drag sorting."""

    changed = Signal()
    COLUMNS = _TwoColumnPriorityList.COLUMNS
    CELL_HEIGHT = _TwoColumnPriorityList.CELL_HEIGHT

    def __init__(self, all_values: Iterable[str], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        hint = QLabel("从左到右、从上到下优先 · 点击席别勾选，可直接拖动排序")
        hint.setObjectName("muted")
        layout.addWidget(hint)

        self.list = _TwoColumnPriorityList()
        self.list.setObjectName("priorityList")
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.list.setDragEnabled(True)
        self.list.setAcceptDrops(True)
        self.list.setDropIndicatorShown(True)
        self.list.setDragDropOverwriteMode(False)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.list.setItemDelegate(_CheckmarkItemDelegate(self.list))
        self.list.setToolTip("点击席别可勾选或取消；拖拽可排序，程序按从左到右、从上到下的顺序尝试")
        self._mutating = True
        for value in all_values:
            self.list.addItem(self._make_item(str(value), checked=False))
        self._mutating = False
        self._sync_list_height()
        self._refresh_priority_labels()
        self.list.itemChanged.connect(self._on_item_changed)
        model = self.list.model()
        model.rowsMoved.connect(self._on_structure_changed)
        model.rowsInserted.connect(self._on_structure_changed)
        model.rowsRemoved.connect(self._on_structure_changed)
        layout.addWidget(self.list)

    def _make_item(self, value: str, *, checked: bool) -> QListWidgetItem:
        item = QListWidgetItem(value)
        item.setData(Qt.ItemDataRole.UserRole, value)
        item.setFlags(
            item.flags()
            | Qt.ItemFlag.ItemIsUserCheckable
            | Qt.ItemFlag.ItemIsDragEnabled
            | Qt.ItemFlag.ItemIsDropEnabled
        )
        item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        return item

    @staticmethod
    def _item_value(item: QListWidgetItem) -> str:
        return str(item.data(Qt.ItemDataRole.UserRole) or item.text())

    def _sync_list_height(self) -> None:
        rows = max(1, (self.list.count() + self.COLUMNS - 1) // self.COLUMNS)
        height = rows * self.CELL_HEIGHT + self.list.frameWidth() * 2 + 10
        self.list.setFixedHeight(height)
        self.list.sync_grid_size()

    def _refresh_priority_labels(self) -> None:
        """Show ranks only for checked items without changing their data value."""

        rank = 0
        self.list.blockSignals(True)
        try:
            for index in range(self.list.count()):
                item = self.list.item(index)
                value = self._item_value(item)
                if item.checkState() == Qt.CheckState.Checked:
                    rank += 1
                    item.setText(f"{rank}. {value}")
                    item.setToolTip(f"优先级 {rank}: {value}")
                else:
                    item.setText(value)
                    item.setToolTip(value)
        finally:
            self.list.blockSignals(False)

    def _on_item_changed(self, _item: QListWidgetItem) -> None:
        if self._mutating:
            return
        self._refresh_priority_labels()
        self.changed.emit()

    def _on_structure_changed(self, *_args: object) -> None:
        if self._mutating:
            return
        self._refresh_priority_labels()
        self._sync_list_height()
        self.changed.emit()

    def values(self) -> List[str]:
        return [
            self._item_value(self.list.item(index))
            for index in range(self.list.count())
            if self.list.item(index).checkState() == Qt.CheckState.Checked
        ]

    def set_values(self, values: Iterable[str]) -> None:
        existing = [self._item_value(self.list.item(index)) for index in range(self.list.count())]
        # Imported drafts may contain stale seat names or duplicates.  The
        # editor must always retain the fixed complete list supplied by the
        # caller, while preserving the valid requested priority order.
        ordered: List[str] = []
        for value in values:
            text = str(value)
            if text in existing and text not in ordered:
                ordered.append(text)
        final = ordered + [value for value in existing if value not in ordered]
        self._mutating = True
        self.list.blockSignals(True)
        try:
            self.list.clear()
            for value in final:
                self.list.addItem(self._make_item(value, checked=value in ordered))
        finally:
            self.list.blockSignals(False)
            self._mutating = False
        self._sync_list_height()
        self._refresh_priority_labels()
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

        guide = QLabel(
            "选中与乘车人数相同的格子。“前排/后排”仅表示同一订单的两排相对关系，"
            "不代表行驶方向、车厢位置或真实排号。"
        )
        guide.setObjectName("muted")
        guide.setWordWrap(True)
        layout.addWidget(guide)

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
            layout.addLayout(seat_row)

        self.fallback = QCheckBox("偏好无法满足时，接受 12306 自动分配")
        self.fallback.setChecked(True)
        self.fallback.setEnabled(False)
        self.fallback.setToolTip("平台在确认前无法可靠判断具体座位，因此固定保留降级策略")
        layout.addWidget(self.fallback)

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

        note = QLabel("设置各铺位期望数量；数量为 0 表示无此偏好。软卧等车型可能不提供中铺。")
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.seat_type_hint = QWidget()
        hint_layout = QVBoxLayout(self.seat_type_hint)
        hint_layout.setContentsMargins(0, 0, 0, 0)
        hint_layout.setSpacing(6)
        self.seat_type_message = QLabel("请先在上方‘席别优先级’勾选硬卧、软卧或高级软卧。")
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


class PositionPreferences(QWidget):
    changed = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.tabs.setTabBar(_ActualTabWheelBar(self.tabs))
        self.tabs.setDocumentMode(True)
        self.seats = SeatMapWidget()
        self.berths = BerthCountWidget()
        self.tabs.addTab(self.seats, "座位偏好")
        self.tabs.addTab(self.berths, "铺位偏好")
        self.seats.changed.connect(self.changed)
        self.berths.changed.connect(self.changed)
        layout.addWidget(self.tabs)

    def adapt_to_seats(self, seat_types: Iterable[str]) -> None:
        values = list(seat_types)
        sleeper = any("卧" in value for value in values)
        seated = any("座" in value and value != "无座" for value in values)
        self.berths.set_sleeper_available(sleeper)
        # Keep both pages reachable so users can clear preferences left over
        # from another seat type. Validation can otherwise fail on a hidden,
        # disabled page with no way to repair the configuration.
        self.tabs.setTabEnabled(0, True)
        self.tabs.setTabEnabled(1, True)
        self.tabs.setTabToolTip(0, "当前席别包含座席时生效" if not seated else "选择同一订单中的相对座位")
        self.tabs.setTabToolTip(1, "当前席别包含卧铺时生效" if not sleeper else "设置下、中、上铺数量")
        if sleeper and not seated:
            self.tabs.setCurrentIndex(1)
        elif seated and not sleeper and not any(self.berths.values().values()):
            self.tabs.setCurrentIndex(0)


class CurrentPhaseWidget(QFrame):
    """Compact current-stage display; transitions may move forward or back."""

    PHASES = [
        ("preparing", "准备任务"),
        ("syncing", "校准服务器时间"),
        ("login", "登录 12306"),
        ("waiting", "等待热身窗口"),
        ("querying", "查询余票"),
        ("submitting", "提交订单"),
        ("queued", "排队出票"),
        ("success", "任务完成"),
    ]
    ALIASES = {
        "prepare": "preparing",
        "prepared": "preparing",
        "stations": "preparing",
        "clock": "syncing",
        "time_sync": "syncing",
        "clock_sync": "syncing",
        "logging_in": "login",
        "qr": "login",
        "wait": "waiting",
        "warmup": "waiting",
        "query": "querying",
        "polling": "querying",
        "candidate": "querying",
        "submit": "submitting",
        "order": "submitting",
        "queue": "queued",
        "queueing": "queued",
        "completed": "success",
        "finished": "success",
    }

    STATUS_LABELS = {
        "failed": "任务失败",
        "cancelled": "任务已停止",
        "stopped": "任务已结束（未出票）",
        "no_ticket": "任务已结束（未出票）",
    }

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("currentPhase")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMinimumHeight(52)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 8, 12, 8)
        row.setSpacing(9)
        self.phase_icon = QLabel("○")
        self.phase_icon.setObjectName("currentPhaseIcon")
        self.phase_title = QLabel()
        self.phase_title.setObjectName("currentPhaseTitle")
        self.phase_message = QLabel()
        self.phase_message.setObjectName("currentPhaseMessage")
        self.phase_message.setWordWrap(False)
        self.phase_message.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        row.addWidget(self.phase_icon)
        row.addWidget(self.phase_title)
        row.addWidget(self.phase_message, 1)
        self.current_phase = ""
        self.current_message = ""
        self.reset_timeline()

    def reset_timeline(self) -> None:
        self.current_phase = ""
        self.current_message = ""
        self._render()

    def set_phase(self, phase: str, message: str = "") -> None:
        phase = self.ALIASES.get(str(phase).lower(), str(phase).lower())
        if phase in {"failure", "error"}:
            phase = "failed"
        elif phase == "canceled":
            phase = "cancelled"
        # Deliberately assign rather than taking max(phase index): a retried
        # login/query can legitimately return to an earlier displayed stage.
        self.current_phase = phase
        self.current_message = str(message or "")
        self._render()

    def _render(self) -> None:
        phase_labels = dict(self.PHASES)
        label = self.STATUS_LABELS.get(self.current_phase, phase_labels.get(self.current_phase, "当前阶段"))
        if not self.current_phase:
            label, symbol, state = "等待开始", "○", "idle"
        elif self.current_phase == "success":
            symbol, state = "✓", "success"
        elif self.current_phase == "failed":
            symbol, state = "!", "failed"
        elif self.current_phase in {"cancelled", "stopped", "no_ticket"}:
            symbol, state = "■", "stopped"
        else:
            symbol, state = "●", "active"
        self.phase_icon.setText(symbol)
        self.phase_icon.setProperty("phaseState", state)
        self.phase_title.setText(label)
        self.phase_message.setText(self.current_message)
        self.phase_message.setToolTip(self.current_message)
        self.setToolTip(f"{label}{'：' + self.current_message if self.current_message else ''}")
        _repolish(self.phase_icon)


class TimelineWidget(CurrentPhaseWidget):
    """Backward-compatible name for code that still imports TimelineWidget."""

    pass


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
        self._lines: List[tuple[str, str]] = []
        self._paused = False
        self._level_colors = self.LEVEL_COLOR
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        controls = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索日志…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.refresh)
        self.level = QComboBox()
        self.level.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        self.level.setCurrentText("INFO")
        self.level.currentTextChanged.connect(self.refresh)
        self.pause = QPushButton("暂停滚动")
        self.pause.setCheckable(True)
        self.pause.toggled.connect(self._toggle_pause)
        clear = QPushButton("清空")
        clear.clicked.connect(self.clear)
        copy = QPushButton("复制")
        copy.clicked.connect(self.copy_all)
        export = QPushButton("导出")
        export.clicked.connect(self.export)
        controls.addWidget(self.search, 1)
        controls.addWidget(self.level)
        controls.addWidget(self.pause)
        controls.addWidget(clear)
        controls.addWidget(copy)
        controls.addWidget(export)
        layout.addLayout(controls)

        self.text = QTextEdit()
        self.text.setObjectName("logView")
        self.text.setReadOnly(True)
        self.text.setAcceptRichText(True)
        self.text.document().setMaximumBlockCount(self.MAX_LINES)
        self.text.setMinimumHeight(80)
        layout.addWidget(self.text)

    def set_light_palette(self, enabled: bool) -> None:
        self._level_colors = self.LIGHT_LEVEL_COLOR if enabled else self.LEVEL_COLOR
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
        needle = self.search.text().strip().casefold()
        return self.LEVEL_VALUE[level] >= minimum and (not needle or needle in line.casefold())

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
