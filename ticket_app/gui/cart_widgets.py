"""Editable ordered booking alternatives, without touching live task state."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable, Mapping, Sequence

from PySide6.QtCore import QEvent, QModelIndex, QPoint, QPersistentModelIndex, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QDrag, QDropEvent, QKeySequence, QPainter, QPalette, QPen, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QCompleter, QDialog, QDialogButtonBox,
    QAbstractButton, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QPushButton, QScrollArea, QStyle, QStyledItemDelegate,
    QStyleOptionViewItem, QToolTip, QVBoxLayout, QWidget,
)

from ..configuration import SEAT_SPECS
from ..train_policy import TRAIN_CODE_PATTERN, normalize_train_codes


_CART_FIELDS = ("from_station", "to_station", "train_scope", "train_code", "seat_type")


def _setting_item(item: Mapping[str, Any]) -> dict[str, str]:
    """Keep only user settings, even if a caller supplied runtime ticket data."""
    return {field: str(item.get(field, "")) for field in _CART_FIELDS}


def _identity(item: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(str(item.get(field, "")).strip() for field in _CART_FIELDS)


def _row_text(entry: Mapping[str, str], number: int) -> str:
    train = "不限车次" if entry["train_scope"] == "all" else entry["train_code"] or "待填写车次"
    return f"{number}. {entry['from_station']} → {entry['to_station']} · {train} · {entry['seat_type']}"


class _CartRowDelegate(QStyledItemDelegate):
    """One 44-pixel itinerary row; both endpoint names keep their own space."""

    ROW_HEIGHT = 44

    @staticmethod
    def action_rects(rect: QRect) -> dict[str, QRect]:
        y = rect.center().y() - 14
        return {"edit": QRect(rect.right() - 90, y, 28, 28),
                "remove": QRect(rect.right() - 58, y, 28, 28)}

    @staticmethod
    def columns(rect: QRect, read_only: bool = False) -> dict[str, QRect]:
        left = rect.left() + 36
        right = rect.right() - (12 if read_only else 102)
        available = max(1, right - left)
        train_width = min(64, max(44, int(available * 0.22)))
        seat_width = min(72, max(52, int(available * 0.25)))
        route_width = max(20, available - train_width - seat_width - 16)
        endpoint_width = max(1, (route_width - 20) // 2)
        return {
            "number": QRect(rect.left() + 8, rect.top(), 24, rect.height()),
            "from_station": QRect(left, rect.top(), endpoint_width, rect.height()),
            "arrow": QRect(left + endpoint_width, rect.top(), 20, rect.height()),
            "to_station": QRect(left + endpoint_width + 20, rect.top(), endpoint_width, rect.height()),
            "train": QRect(left + route_width + 8, rect.top(), train_width, rect.height()),
            "seat": QRect(right - seat_width, rect.top(), seat_width, rect.height()),
            "grip": QRect(rect.right() - 24, rect.top(), 20, rect.height()),
        }

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        view = self.parent()
        dark = option.palette.color(QPalette.ColorRole.Text).lightness() > 128
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        ink = QColor("#dbe5f7" if dark else "#26344d")
        muted = QColor("#8f9bb3" if dark else "#687792")
        accent = QColor("#7fbdff" if dark else "#1769c2")
        surface = QColor("#12243d" if dark else "#eaf4ff") if selected else QColor("#11182a" if dark else "#ffffff")
        border = QColor("#3b82df") if selected else QColor("#253149" if dark else "#dce4f0")
        if hovered and not selected:
            border = QColor("#425a7d" if dark else "#b7cbe5")
        entry = index.data(Qt.ItemDataRole.UserRole)
        if not entry:
            return
        rect = option.rect.adjusted(1, 1, -1, -1)
        columns = self.columns(option.rect, view.read_only)
        painter.save()
        painter.setClipRect(option.rect)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not option.state & QStyle.StateFlag.State_Enabled:
            painter.setOpacity(0.6)
        painter.setPen(QPen(border, 1))
        painter.setBrush(surface)
        painter.drawRoundedRect(rect, 6, 6)
        painter.setFont(option.font)
        metrics = painter.fontMetrics()
        # Keep short routes together, while reserving at least half of the
        # route column for the destination when an origin needs elision.
        origin = columns["from_station"]
        origin.setWidth(min(origin.width(), metrics.horizontalAdvance(entry["from_station"]) + 3))
        columns["arrow"].moveLeft(origin.right() + 4)
        columns["to_station"].setLeft(columns["arrow"].right() + 4)
        for key, text, color in (
            ("number", str(index.row() + 1), accent if selected else muted),
            ("from_station", entry["from_station"], ink),
            ("arrow", "→", muted),
            ("to_station", entry["to_station"], ink),
            ("train", "不限车次" if entry["train_scope"] == "all" else entry["train_code"] or "待填写车次", accent),
            ("seat", entry["seat_type"], ink),
        ):
            target = columns[key]
            painter.setPen(color)
            align = Qt.AlignmentFlag.AlignVCenter
            if key in {"number", "arrow", "train", "seat"}:
                align |= Qt.AlignmentFlag.AlignHCenter
            painter.drawText(target, align, metrics.elidedText(text, Qt.TextElideMode.ElideRight, target.width()))
        if not view.read_only:
            grip = columns["grip"]
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(muted)
            for x in (grip.center().x() - 3, grip.center().x() + 3):
                for y in (grip.center().y() - 6, grip.center().y(), grip.center().y() + 6):
                    painter.drawEllipse(QPoint(x, y), 1, 1)
        if option.state & QStyle.StateFlag.State_HasFocus:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(accent, 1, Qt.PenStyle.DotLine))
            painter.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 4, 4)
        painter.restore()


class _CartActionButton(QAbstractButton):
    """A real accessible button; its icon never relies on a font glyph."""

    focused = Signal()

    def __init__(self, action: str, parent: QWidget) -> None:
        super().__init__(parent)
        self.action = action
        self.setFixedSize(28, 28)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)

    def focusInEvent(self, event: Any) -> None:  # noqa: N802
        super().focusInEvent(event)
        self.focused.emit()

    def keyPressEvent(self, event: Any) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.click()
            event.accept()
            return
        super().keyPressEvent(event)

    def event(self, event: QEvent) -> bool:
        if event.type() in (QEvent.Type.HoverEnter, QEvent.Type.HoverLeave, QEvent.Type.FocusIn, QEvent.Type.FocusOut):
            self.update()
        return super().event(event)

    def paintEvent(self, event: Any) -> None:  # noqa: N802
        dark = self.palette().color(QPalette.ColorRole.Text).lightness() > 128
        color = QColor(("#ef9b9b" if dark else "#b94747") if self.action == "remove"
                       else ("#7fbdff" if dark else "#1769c2"))
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            painter.setOpacity(0.5)
        if self.underMouse() or self.hasFocus() or self.isDown():
            painter.setPen(QPen(color, 1) if self.hasFocus() else Qt.PenStyle.NoPen)
            fill = QColor(color)
            fill.setAlpha(38 if self.isDown() else 20)
            painter.setBrush(fill)
            painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 5, 5)
        painter.setPen(QPen(color, 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if self.action == "edit":
            painter.drawPolyline([QPoint(8, 17), QPoint(17, 8), QPoint(21, 12), QPoint(12, 21), QPoint(7, 22), QPoint(8, 17)])
            painter.drawLine(15, 10, 19, 14)
        else:
            painter.drawLine(7, 9, 21, 9)
            painter.drawPolyline([QPoint(10, 9), QPoint(10, 6), QPoint(18, 6), QPoint(18, 9)])
            painter.drawPolyline([QPoint(9, 11), QPoint(10, 22), QPoint(18, 22), QPoint(19, 11)])
            painter.drawLine(12, 12, 12, 19)
            painter.drawLine(16, 12, 16, 19)
        painter.end()


class _CartPriorityList(QListWidget):
    """Move existing rows directly; a successful drop must never copy a row."""

    row_action = Signal(str, int)

    def __init__(self, parent: QWidget | None = None, *, read_only: bool = False) -> None:
        super().__init__(parent)
        self.read_only = read_only
        self._drag_index = QPersistentModelIndex()
        self._controls: list[tuple[QPersistentModelIndex, dict[str, _CartActionButton]]] = []
        self._buttons_pending = False
        self._button_timer = QTimer(self)
        self._button_timer.setSingleShot(True)
        self._button_timer.timeout.connect(self._sync_buttons)
        self.setItemDelegate(_CartRowDelegate(self))
        self.setMouseTracking(True)
        for signal in (self.model().rowsInserted, self.model().rowsRemoved, self.model().rowsMoved,
                       self.model().modelReset, self.model().dataChanged):
            signal.connect(self._schedule_buttons)

    def _schedule_buttons(self, *_args: Any) -> None:
        if not self.read_only and not self._buttons_pending:
            self._buttons_pending = True
            self._button_timer.start(0)

    def _sync_buttons(self) -> None:
        self._buttons_pending = False
        self._button_timer.stop()
        if self.read_only:
            return
        if len(self._controls) != self.count() or any(not index.isValid() for index, _buttons in self._controls):
            for _index, buttons in self._controls:
                for button in buttons.values():
                    button.hide()
                    button.deleteLater()
            self._controls.clear()
            for row in range(self.count()):
                index = QPersistentModelIndex(self.model().index(row, 0))
                buttons = {}
                for action in ("edit", "remove"):
                    button = _CartActionButton(action, self.viewport())
                    button.clicked.connect(lambda _checked=False, index=index, action=action: self._activate_action(index, action))
                    button.focused.connect(lambda index=index: self._focus_action_row(index))
                    buttons[action] = button
                self._controls.append((index, buttons))
        for index, buttons in self._controls:
            rect = self.visualRect(QModelIndex(index))
            actions = _CartRowDelegate.action_rects(rect)
            entry = index.data(Qt.ItemDataRole.UserRole)
            description = _row_text(entry, index.row() + 1)
            for action, button in buttons.items():
                label, key = ("编辑", "Enter") if action == "edit" else ("删除", "Delete")
                button.setGeometry(actions[action])
                button.setAccessibleName(f"{label}第 {index.row() + 1} 项")
                button.setAccessibleDescription(description)
                button.setToolTip(f"{label}此项（{key}）\n{description}")
                button.setVisible(self.viewport().rect().intersects(rect))

    def _focus_action_row(self, index: QPersistentModelIndex) -> None:
        if index.isValid():
            self.setCurrentIndex(QModelIndex(index))

    def _activate_action(self, index: QPersistentModelIndex, action: str) -> None:
        if not self.read_only and self.isEnabled() and index.isValid():
            self.row_action.emit(action, index.row())

    def action_button(self, row: int, action: str) -> _CartActionButton | None:
        """Return the native accessible action associated with a model row."""
        self._sync_buttons()
        return next((buttons[action] for index, buttons in self._controls if index.row() == row), None)

    def resizeEvent(self, event: Any) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._schedule_buttons()

    def showEvent(self, event: Any) -> None:  # noqa: N802
        super().showEvent(event)
        self._schedule_buttons()

    def scrollContentsBy(self, dx: int, dy: int) -> None:  # noqa: N802
        super().scrollContentsBy(dx, dy)
        self._sync_buttons()

    def viewportEvent(self, event: QEvent) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.ToolTip and not self.read_only:
            index = self.indexAt(event.pos())
            if index.isValid() and _CartRowDelegate.columns(self.visualRect(index))["grip"].contains(event.pos()):
                QToolTip.showText(event.globalPos(), "拖动调整顺序（也可用 Alt+↑ / Alt+↓）", self.viewport())
                return True
        return super().viewportEvent(event)

    def startDrag(self, supported_actions: Qt.DropAction) -> None:  # noqa: N802
        if not self.isEnabled() or self.dragDropMode() != QAbstractItemView.DragDropMode.InternalMove:
            return
        self._drag_index = QPersistentModelIndex(self.currentIndex())
        if not self._drag_index.isValid():
            return
        index = QModelIndex(self._drag_index)
        drag = QDrag(self)
        drag.setMimeData(self.model().mimeData([index]))
        drag.setPixmap(self.viewport().grab(self.visualRect(index)))
        try:
            # The model move in dropEvent owns removal, so do not also let the
            # native startDrag remove the source after MoveAction is returned.
            drag.exec(supported_actions & Qt.DropAction.MoveAction, Qt.DropAction.MoveAction)
        finally:
            self._drag_index = QPersistentModelIndex()
            self.setState(QAbstractItemView.State.NoState)
            drag.deleteLater()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        if event.source() not in {self, self.viewport()} or not self._drag_index.isValid():
            event.ignore()
            return
        source = self._drag_index.row()
        point = event.position().toPoint()
        # Spacing between rows has no model index. Compare row midpoints so a
        # drop in a gap inserts there rather than unexpectedly jumping to end.
        destination = next((row for row in range(self.count())
                            if point.y() <= self.visualItemRect(self.item(row)).center().y()),
                           self.count())
        if destination not in {source, source + 1}:
            if not self.model().moveRow(QModelIndex(), source, QModelIndex(), destination):
                event.ignore()
                return
            self.setCurrentRow(destination - 1 if source < destination else destination)
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()


class CompactCartSummary(_CartPriorityList):
    """Read-only compact routes sized for the confirmation page's own scroll.

    ``set_items`` accepts the same user-setting mappings as CartDialog. It
    deliberately has no internal vertical scrolling or mutating row actions.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, read_only=True)
        self.setObjectName("cartPriorityList")
        self.setStyleSheet("""
            QListWidget#cartPriorityList { border: none; background: transparent; padding: 0px; }
            QListWidget#cartPriorityList::item { padding: 0px; margin: 0px; border: none; }
        """)
        self.setAccessibleName("购物车尝试顺序")
        self.setAccessibleDescription("仅供核对；通过购物车按钮修改或排序。")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.NoDragDrop)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSpacing(2)
        self.set_items([])

    def set_items(self, items: Sequence[Mapping[str, Any]]) -> None:
        self.clear()
        for number, raw in enumerate(items, 1):
            entry = _setting_item(raw)
            text = _row_text(entry, number)
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, entry)
            item.setData(Qt.ItemDataRole.AccessibleTextRole, text)
            item.setToolTip(text)
            item.setSizeHint(QSize(0, _CartRowDelegate.ROW_HEIGHT))
            self.addItem(item)
        self.setFixedHeight(max(0, len(items) * (_CartRowDelegate.ROW_HEIGHT + 2 * self.spacing())))
        self.setVisible(bool(items))

    def items(self) -> list[dict[str, str]]:
        return deepcopy([self.item(index).data(Qt.ItemDataRole.UserRole) for index in range(self.count())])

    def text(self) -> str:
        return "\n".join(self.item(index).text() for index in range(self.count()))


class CartDialog(QDialog):
    """Work on a private copy; the caller commits ``items()`` only on acceptance.

    One row is one station pair, train scope and seat. The item view's own
    internal-move model keeps row data attached while dragging; the displayed
    priority numbers are regenerated after every move.
    """

    def __init__(
        self,
        items: Sequence[Mapping[str, Any]],
        parent: QWidget | None = None,
        *,
        station_names: Iterable[str] | None = None,
        read_only: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("cartDialog")
        self.setWindowTitle("购物车 · 按顺序尝试")
        self.setModal(True)
        available = self.screen().availableGeometry()
        preferred_height = max(240, min(560, 152 + len(items) * 48))
        self.resize(min(690, max(360, available.width() - 40)),
                    min(preferred_height, max(240, available.height() - 60)))
        self._station_names = frozenset(station_names or ())
        self._read_only = read_only
        self._editing_item: QListWidgetItem | None = None
        self._positioned = False
        self._browse_height: int | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(8)
        self.summary = QLabel("购物车")
        self.summary.setObjectName("cardTitle")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.cart_help = QLabel(
            "本轮任务的备选路线，仅供查看。" if read_only else
            "拖动调整尝试顺序；任意一项订票成功后，其他备选停止。",
        )
        self.cart_help.setObjectName("muted")
        self.cart_help.setWordWrap(True)
        layout.addWidget(self.cart_help)
        self.list = _CartPriorityList(read_only=read_only)
        self.list.setObjectName("cartPriorityList")
        self.list.setStyleSheet("""
            QListWidget#cartPriorityList { border: none; background: transparent; padding: 0px; }
            QListWidget#cartPriorityList::item { padding: 0px; margin: 0px; border: none; }
        """)
        self.list.setAccessibleName("购物车，排列顺序就是尝试顺序")
        self.list.setAccessibleDescription("Alt+上箭头或下箭头调整顺序，Enter编辑，Delete删除。")
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setDragDropMode(
            QAbstractItemView.DragDropMode.NoDragDrop if read_only
            else QAbstractItemView.DragDropMode.InternalMove
        )
        self.list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.list.setDragDropOverwriteMode(False)
        self.list.setWordWrap(True)
        self.list.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.list.setSpacing(2)
        self.list.setMinimumHeight(48)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.installEventFilter(self)
        for entry in items:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, _setting_item(entry))
            self.list.addItem(item)
        self.list.model().rowsMoved.connect(self._rows_moved)
        self.list.currentRowChanged.connect(self._update_controls)
        self.list.itemDoubleClicked.connect(lambda _item: self.edit_current())
        self.list.row_action.connect(self._row_action)
        layout.addWidget(self.list, 1)

        self.editor = QFrame()
        self.editor.setObjectName("cartRowEditor")
        edit_layout = QVBoxLayout(self.editor)
        edit_layout.setContentsMargins(0, 6, 0, 6)
        edit_layout.setSpacing(8)
        self.edit_title = QLabel()
        self.edit_title.hide()
        layout.addWidget(self.edit_title)
        fields = QGridLayout()
        fields.setColumnStretch(0, 1)
        fields.setColumnStretch(1, 1)
        self.from_station = QLineEdit()
        self.to_station = QLineEdit()
        self.from_station.setAccessibleName("编辑项目的出发站")
        self.to_station.setAccessibleName("编辑项目的到达站")
        self.from_station.setPlaceholderText("出发站，例如北京南")
        self.to_station.setPlaceholderText("到达站，例如上海虹桥")
        fields.addWidget(QLabel("出发站"), 0, 0)
        fields.addWidget(QLabel("到达站"), 0, 1)
        fields.addWidget(self.from_station, 1, 0)
        fields.addWidget(self.to_station, 1, 1)
        if self._station_names:
            for edit in (self.from_station, self.to_station):
                completer = QCompleter(sorted(self._station_names), edit)
                completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
                completer.setFilterMode(Qt.MatchFlag.MatchContains)
                edit.setCompleter(completer)
        self.train_scope = QComboBox()
        self.train_scope.setAccessibleName("编辑项目的车次范围")
        self.train_scope.addItem("指定车次", "specific")
        self.train_scope.addItem("不限车次", "all")
        self.train_code = QLineEdit()
        self.train_code.setAccessibleName("编辑项目的指定车次")
        self.train_code.setPlaceholderText("仅一个车次，例如 G101")
        self.train_scope.currentIndexChanged.connect(self._scope_changed)
        fields.addWidget(QLabel("车次范围"), 2, 0)
        fields.addWidget(QLabel("车次"), 2, 1)
        fields.addWidget(self.train_scope, 3, 0)
        fields.addWidget(self.train_code, 3, 1)
        self.seat_type = QComboBox()
        self.seat_type.setAccessibleName("编辑项目的席别")
        self.seat_type.addItems(list(SEAT_SPECS))
        fields.addWidget(QLabel("席别"), 4, 0, 1, 2)
        fields.addWidget(self.seat_type, 5, 0, 1, 2)
        edit_layout.addLayout(fields)
        edit_buttons = QHBoxLayout()
        self.save_edit_button = QPushButton("保存本项")
        self.save_edit_button.setObjectName("primaryButton")
        self.cancel_edit_button = QPushButton("取消编辑")
        for button in (self.save_edit_button, self.cancel_edit_button):
            button.setAutoDefault(False)
        self.save_edit_button.clicked.connect(self.save_edit)
        self.cancel_edit_button.clicked.connect(self.cancel_edit)
        edit_buttons.addWidget(self.save_edit_button)
        edit_buttons.addWidget(self.cancel_edit_button)
        edit_buttons.addStretch(1)
        edit_layout.addLayout(edit_buttons)
        self.editor_scroll = QScrollArea()
        self.editor_scroll.setWidgetResizable(True)
        self.editor_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.editor_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.editor_scroll.setWidget(self.editor)
        self.editor_scroll.setMaximumHeight(330)
        self.editor_scroll.hide()
        layout.addWidget(self.editor_scroll)

        self.error = QLabel()
        self.error.setObjectName("errorLabel")
        self.error.setWordWrap(True)
        self.error.setAccessibleName("购物车编辑提示")
        self.error.hide()
        layout.addWidget(self.error)
        self.buttons = QDialogButtonBox()
        self.done_button = self.buttons.addButton("关闭" if read_only else "保存修改", QDialogButtonBox.ButtonRole.AcceptRole)
        self.done_button.setObjectName("primaryButton")
        self.done_button.setAutoDefault(False)
        self.cancel_button = self.buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.setVisible(not read_only)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        # Widget scope prevents Delete/Enter in the text editors affecting rows.
        self._shortcuts: list[QShortcut] = []
        for sequence, callback in (("Alt+Up", lambda: self.move_current(-1)),
                                   ("Alt+Down", lambda: self.move_current(1))):
            shortcut = QShortcut(QKeySequence(sequence), self.list)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(callback)
            self._shortcuts.append(shortcut)
        self._refresh_rows()
        if self.list.count():
            self.list.setCurrentRow(0)
        self._update_controls()

    def showEvent(self, event: Any) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._positioned:
            self._positioned = True
            QTimer.singleShot(0, self._center_on_parent)

    def _center_on_parent(self) -> None:
        parent = self.parentWidget()
        available = (parent.screen() if parent is not None else self.screen()).availableGeometry()
        anchor = parent.frameGeometry().center() if parent is not None else available.center()
        frame = self.frameGeometry()
        frame.moveCenter(anchor)
        x = min(max(frame.left(), available.left()), max(available.left(), available.right() - frame.width() + 1))
        y = min(max(frame.top(), available.top()), max(available.top(), available.bottom() - frame.height() + 1))
        self.move(x, y)

    def _row_action(self, action: str, row: int) -> None:
        if self._read_only or self._editing_item is not None or not 0 <= row < self.list.count():
            return
        self.list.setCurrentRow(row)
        if action == "edit":
            self.edit_current()
        elif action == "remove":
            self.remove_current()

    def items(self) -> list[dict[str, str]]:
        """Return isolated user settings in the actual displayed order."""
        return deepcopy([
            self.list.item(index).data(Qt.ItemDataRole.UserRole)
            for index in range(self.list.count())
        ])

    def eventFilter(self, watched: Any, event: QEvent) -> bool:  # noqa: N802
        if watched is self.list and event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Delete:
                self.remove_current()
                return True
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.edit_current()
                return True
        return super().eventFilter(watched, event)

    def move_current(self, offset: int) -> None:
        if self._read_only or self._editing_item is not None:
            return
        source = self.list.currentRow()
        target = source + offset
        if source < 0 or target < 0 or target >= self.list.count():
            return
        destination = target + 1 if source < target else target
        if self.list.model().moveRow(QModelIndex(), source, QModelIndex(), destination):
            self.list.setCurrentRow(target)
            self.list.scrollToItem(self.list.item(target))

    def remove_current(self) -> None:
        if self._read_only or self._editing_item is not None:
            return
        row = self.list.currentRow()
        if row < 0:
            return
        self.list.takeItem(row)
        if self.list.count():
            self.list.setCurrentRow(min(row, self.list.count() - 1))
        self._refresh_rows()

    def edit_current(self) -> None:
        item = self.list.currentItem()
        if self._read_only or self._editing_item is not None or item is None:
            return
        self._editing_item = item
        if self.height() < 360:
            self._browse_height = self.height()
            available = self.screen().availableGeometry()
            self.resize(self.width(), min(480, max(self.height(), available.height() - 60)))
        entry = item.data(Qt.ItemDataRole.UserRole)
        self.edit_title.setText(f"编辑第 {self.list.row(item) + 1} 项")
        self.from_station.setText(entry["from_station"])
        self.to_station.setText(entry["to_station"])
        self.train_scope.setCurrentIndex(self.train_scope.findData(entry["train_scope"]))
        self.train_code.setText(entry["train_code"])
        self.seat_type.setCurrentIndex(self.seat_type.findText(entry["seat_type"]))
        self._scope_changed()
        self._show_error("")
        self.editor.show()
        self.edit_title.show()
        self.editor_scroll.show()
        self._update_controls()
        QTimer.singleShot(0, self._reveal_editing_row)
        self.from_station.setFocus()

    def save_edit(self) -> None:
        if self._editing_item is None or self._read_only:
            return
        entry = {
            "from_station": self.from_station.text().strip(),
            "to_station": self.to_station.text().strip(),
            "train_scope": str(self.train_scope.currentData() or ""),
            "train_code": "",
            "seat_type": self.seat_type.currentText(),
        }
        for field, title in (("from_station", "出发站"), ("to_station", "到达站")):
            station = entry[field]
            if not station or (self._station_names and station not in self._station_names):
                self._show_error(f"请填写有效的{title}，名称需与 12306 一致。")
                getattr(self, field).setFocus()
                return
        if entry["from_station"] == entry["to_station"]:
            self._show_error("出发站和到达站不能相同。")
            self.to_station.setFocus()
            return
        if entry["train_scope"] == "specific":
            codes = normalize_train_codes(self.train_code.text())
            if len(codes) != 1 or not TRAIN_CODE_PATTERN.fullmatch(codes[0]):
                self._show_error("编辑一项时请填写一个有效车次；添加多个车次请回到第一步。")
                self.train_code.setFocus()
                return
            entry["train_code"] = codes[0]
        elif entry["train_scope"] != "all":
            self._show_error("请选择指定车次或不限车次。")
            self.train_scope.setFocus()
            return
        if entry["seat_type"] not in SEAT_SPECS:
            self._show_error("请选择一种席别。")
            self.seat_type.setFocus()
            return
        for index in range(self.list.count()):
            other = self.list.item(index)
            if other is not self._editing_item and _identity(other.data(Qt.ItemDataRole.UserRole)) == _identity(entry):
                self._show_error(f"与第 {index + 1} 项完全相同，未保存重复项目。")
                return
        self._editing_item.setData(Qt.ItemDataRole.UserRole, entry)
        self.cancel_edit()
        self._refresh_rows()

    def cancel_edit(self) -> None:
        self._editing_item = None
        self.edit_title.hide()
        self.editor_scroll.hide()
        if self._browse_height is not None:
            self.resize(self.width(), self._browse_height)
            self._browse_height = None
        self._show_error("")
        self._update_controls()
        self.list.setFocus()

    def accept(self) -> None:
        if self._editing_item is not None:
            self._show_error("请先保存本项，或取消编辑，再保存购物车修改。")
            return
        super().accept()

    def _scope_changed(self) -> None:
        self.train_code.setEnabled(self.train_scope.currentData() == "specific")

    def _reveal_editing_row(self) -> None:
        if self._editing_item is not None:
            self.list.scrollToItem(self._editing_item)

    def _show_error(self, message: str) -> None:
        self.error.setText(message)
        self.error.setVisible(bool(message))

    def _rows_moved(self, *_args: Any) -> None:
        self._refresh_rows()

    def _refresh_rows(self) -> None:
        for index in range(self.list.count()):
            item = self.list.item(index)
            entry = item.data(Qt.ItemDataRole.UserRole)
            text = _row_text(entry, index + 1)
            item.setText(text)
            item.setToolTip(text)
            item.setData(Qt.ItemDataRole.AccessibleTextRole, text)
            item.setSizeHint(QSize(0, _CartRowDelegate.ROW_HEIGHT))
        count = self.list.count()
        self.summary.setText(f"购物车（{count}）" if count else "购物车为空，请返回第一步添加备选。")
        self._update_controls()

    def _update_controls(self, *_args: Any) -> None:
        self.list.setEnabled(self._editing_item is None)
