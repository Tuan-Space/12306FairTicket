"""Editable ordered booking alternatives, without touching live task state."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable, Mapping, Sequence

from PySide6.QtCore import QEvent, QModelIndex, QPersistentModelIndex, QSize, Qt, QTimer
from PySide6.QtGui import QDrag, QDropEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QCompleter, QDialog, QDialogButtonBox,
    QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from ..configuration import SEAT_SPECS
from ..train_policy import TRAIN_CODE_PATTERN, normalize_train_codes


_CART_FIELDS = ("from_station", "to_station", "train_scope", "train_code", "seat_type")


def _setting_item(item: Mapping[str, Any]) -> dict[str, str]:
    """Keep only user settings, even if a caller supplied runtime ticket data."""
    return {field: str(item.get(field, "")) for field in _CART_FIELDS}


def _identity(item: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(str(item.get(field, "")).strip() for field in _CART_FIELDS)


class _CartPriorityList(QListWidget):
    """Move existing rows directly; a successful drop must never copy a row."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._drag_index = QPersistentModelIndex()

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
        migration_warnings: Sequence[str] | None = None,
        read_only: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("cartDialog")
        self.setWindowTitle("购物车 · 按顺序尝试")
        self.setModal(True)
        available = self.screen().availableGeometry()
        self.resize(min(690, max(360, available.width() - 40)),
                    min(650, max(320, available.height() - 60)))
        self._station_names = frozenset(station_names or ())
        self._read_only = read_only
        self._editing_item: QListWidgetItem | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        hint = QLabel("从上到下尝试；任意一项订票成功后，其他备选停止。")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.migration_notice = QLabel("\n".join(migration_warnings or ()))
        self.migration_notice.setObjectName("warningText")
        self.migration_notice.setWordWrap(True)
        self.migration_scroll = QScrollArea()
        self.migration_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.migration_scroll.setWidgetResizable(True)
        self.migration_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.migration_scroll.setMinimumHeight(32)
        self.migration_scroll.setMaximumHeight(96)
        self.migration_scroll.setWidget(self.migration_notice)
        self.migration_scroll.setVisible(bool(migration_warnings))
        layout.addWidget(self.migration_scroll)

        self.list = _CartPriorityList()
        self.list.setObjectName("cartPriorityList")
        self.list.setAccessibleName("购物车，排列顺序就是尝试顺序")
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setDragDropMode(
            QAbstractItemView.DragDropMode.NoDragDrop if read_only
            else QAbstractItemView.DragDropMode.InternalMove
        )
        self.list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.list.setDragDropOverwriteMode(False)
        self.list.setWordWrap(True)
        self.list.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.list.setSpacing(4)
        self.list.setMinimumHeight(80)
        self.list.installEventFilter(self)
        for entry in items:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, _setting_item(entry))
            self.list.addItem(item)
        self.list.model().rowsMoved.connect(self._rows_moved)
        self.list.currentRowChanged.connect(self._update_controls)
        self.list.itemDoubleClicked.connect(lambda _item: self.edit_current())
        layout.addWidget(self.list, 1)

        tools = QHBoxLayout()
        tools.setSpacing(8)
        self.up_button = QPushButton("上移")
        self.down_button = QPushButton("下移")
        self.edit_button = QPushButton("编辑")
        self.remove_button = QPushButton("删除")
        for button in (self.up_button, self.down_button, self.edit_button, self.remove_button):
            button.setAutoDefault(False)
            tools.addWidget(button)
            button.setVisible(not read_only)
        tools.addStretch(1)
        self.up_button.setToolTip("上移一项（Alt+↑）")
        self.down_button.setToolTip("下移一项（Alt+↓）")
        self.edit_button.setToolTip("编辑选中项目（Enter）")
        self.remove_button.setToolTip("删除选中项目（Delete）")
        self.up_button.clicked.connect(lambda: self.move_current(-1))
        self.down_button.clicked.connect(lambda: self.move_current(1))
        self.edit_button.clicked.connect(self.edit_current)
        self.remove_button.clicked.connect(self.remove_current)
        layout.addLayout(tools)

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
        self.done_button = self.buttons.addButton("完成", QDialogButtonBox.ButtonRole.AcceptRole)
        self.done_button.setObjectName("primaryButton")
        self.done_button.setAutoDefault(False)
        self.cancel_button = self.buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        self.cancel_button.setAutoDefault(False)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        # Widget scope prevents Delete/Enter in the text editors affecting rows.
        self._shortcuts: list[QShortcut] = []
        for sequence, callback in (("Alt+Up", lambda: self.move_current(-1)),
                                   ("Alt+Down", lambda: self.move_current(1))):
            shortcut = QShortcut(QKeySequence(sequence), self.list)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(callback)
            self._shortcuts.append(shortcut)
        self._refresh_rows()
        if self.list.count():
            self.list.setCurrentRow(0)
        self._update_controls()

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
        item = self.list.takeItem(source)
        self.list.insertItem(target, item)
        self.list.setCurrentItem(item)
        self.list.scrollToItem(item)
        self._refresh_rows()

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
        self._show_error("")
        self._update_controls()
        self.list.setFocus()

    def accept(self) -> None:
        if self._editing_item is not None:
            self._show_error("请先保存本项，或取消编辑后再完成。")
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
            train = "不限车次" if entry["train_scope"] == "all" else entry["train_code"] or "待填写车次"
            text = f"{index + 1}. {entry['from_station']} → {entry['to_station']}\n    {train}  ·  {entry['seat_type']}"
            item.setText(text)
            item.setToolTip(text)
            item.setData(Qt.ItemDataRole.AccessibleTextRole, text.replace("\n", "，"))
            item.setSizeHint(QSize(0, self.list.fontMetrics().lineSpacing() * 3 + 12))
        count = self.list.count()
        self.summary.setText(f"购物车（{count}）" if count else "购物车为空，请返回第一步添加备选。")
        self._update_controls()

    def _update_controls(self, *_args: Any) -> None:
        editing = self._editing_item is not None
        row = self.list.currentRow()
        enabled = not self._read_only and not editing and row >= 0
        self.up_button.setEnabled(enabled and row > 0)
        self.down_button.setEnabled(enabled and row < self.list.count() - 1)
        self.edit_button.setEnabled(enabled)
        self.remove_button.setEnabled(enabled)
        self.list.setEnabled(not editing)
