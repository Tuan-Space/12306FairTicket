"""Reusable widgets used by the desktop window."""

from __future__ import annotations

import html
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


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


class PriorityListEditor(QWidget):
    changed = Signal()

    def __init__(self, all_values: Iterable[str], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        self.list = QListWidget()
        self.list.setObjectName("priorityList")
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setMinimumHeight(132)
        self.list.setToolTip("勾选并拖拽排序，程序会按从上到下的顺序尝试")
        for value in all_values:
            item = QListWidgetItem(str(value))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsDragEnabled)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.list.addItem(item)
        self.list.itemChanged.connect(lambda _item: self.changed.emit())
        self.list.model().rowsMoved.connect(lambda *_args: self.changed.emit())
        row.addWidget(self.list, 1)

        controls = QVBoxLayout()
        controls.setSpacing(6)
        up = QToolButton()
        up.setText("↑")
        up.setToolTip("提高优先级")
        down = QToolButton()
        down.setText("↓")
        down.setToolTip("降低优先级")
        up.clicked.connect(lambda: self._move(-1))
        down.clicked.connect(lambda: self._move(1))
        controls.addWidget(up)
        controls.addWidget(down)
        controls.addStretch(1)
        row.addLayout(controls)

    def _move(self, offset: int) -> None:
        current = self.list.currentRow()
        target = current + offset
        if current < 0 or target < 0 or target >= self.list.count():
            return
        item = self.list.takeItem(current)
        self.list.insertItem(target, item)
        self.list.setCurrentRow(target)
        self.changed.emit()

    def values(self) -> List[str]:
        return [
            self.list.item(index).text()
            for index in range(self.list.count())
            if self.list.item(index).checkState() == Qt.CheckState.Checked
        ]

    def set_values(self, values: Iterable[str]) -> None:
        ordered = [str(value) for value in values]
        existing = [self.list.item(index).text() for index in range(self.list.count())]
        final = ordered + [value for value in existing if value not in ordered]
        self.list.blockSignals(True)
        self.list.clear()
        for value in final:
            item = QListWidgetItem(value)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsDragEnabled)
            item.setCheckState(Qt.CheckState.Checked if value in ordered else Qt.CheckState.Unchecked)
            self.list.addItem(item)
        self.list.blockSignals(False)
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

        guide = QLabel("选中与乘车人数相同的格子。这是相对关系图，不是真实排号；可用字母由车型和席别决定。")
        guide.setObjectName("muted")
        guide.setWordWrap(True)
        layout.addWidget(guide)

        self.buttons: Dict[str, QToolButton] = {}
        for relation_row in (1, 2):
            seat_row = QHBoxLayout()
            seat_row.setSpacing(7)
            row_name = QLabel(f"关系 {relation_row}")
            row_name.setObjectName("muted")
            row_name.setMinimumWidth(48)
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
        button.setMinimumSize(58, 58)
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

        self.spins: Dict[str, QSpinBox] = {}
        for key, label, hint in self.LABELS:
            row = QHBoxLayout()
            name = QLabel(label)
            name.setMinimumWidth(52)
            spin = QSpinBox()
            spin.setRange(0, 5)
            spin.setSuffix(" 张")
            spin.setToolTip(hint)
            spin.valueChanged.connect(lambda _value: self._update_total())
            self.spins[key] = spin
            row.addWidget(name)
            row.addWidget(spin)
            row.addStretch(1)
            layout.addLayout(row)

        self.total = QLabel("已选 0 张铺位偏好")
        self.total.setObjectName("muted")
        layout.addWidget(self.total)

    def _update_total(self) -> None:
        count = sum(spin.value() for spin in self.spins.values())
        self.total.setText(f"已选 {count} 张铺位偏好")
        self.changed.emit()

    def values(self) -> Dict[str, int]:
        return {key: spin.value() for key, spin in self.spins.items()}

    def set_values(self, values: Mapping[str, object]) -> None:
        for key, spin in self.spins.items():
            spin.blockSignals(True)
            spin.setValue(max(0, int(values.get(key, 0) or 0)))
            spin.blockSignals(False)
        self._update_total()


class PositionPreferences(QWidget):
    changed = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
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
        # Keep both pages reachable so users can clear preferences left over
        # from another seat type. Validation can otherwise fail on a hidden,
        # disabled page with no way to repair the configuration.
        self.tabs.setTabEnabled(0, True)
        self.tabs.setTabEnabled(1, True)
        self.tabs.setTabToolTip(0, "当前席别包含座席时生效" if not seated else "选择同一订单中的相对座位")
        self.tabs.setTabToolTip(1, "当前席别包含卧铺时生效" if not sleeper else "设置下、中、上铺数量")
        if sleeper and not seated:
            self.tabs.setCurrentIndex(1)
        elif seated and not sleeper:
            self.tabs.setCurrentIndex(0)


class TimelineWidget(QListWidget):
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

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("timeline")
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMinimumHeight(120)
        self._active = -1
        self._status = ""
        self.reset_timeline()

    def reset_timeline(self) -> None:
        self._active = -1
        self._status = ""
        self._render()

    def set_phase(self, phase: str, message: str = "") -> None:
        phase = self.ALIASES.get(str(phase).lower(), str(phase).lower())
        if phase in {"failed", "failure", "error", "cancelled", "canceled", "stopped", "no_ticket"}:
            if phase in {"stopped", "no_ticket"}:
                self._status = "stopped"
            else:
                self._status = "cancelled" if phase in {"cancelled", "canceled"} else "failed"
        else:
            for index, (key, _label) in enumerate(self.PHASES):
                if key == phase:
                    self._active = max(self._active, index)
                    break
        self._render(message)

    def _render(self, message: str = "") -> None:
        self.clear()
        for index, (_key, label) in enumerate(self.PHASES):
            if index < self._active:
                prefix, color = "✓", QColor("#2fb171")
            elif index == self._active:
                prefix, color = "●", QColor("#3388ff")
            else:
                prefix, color = "○", QColor("#77829a")
            suffix = f"  {message}" if index == self._active and message else ""
            item = QListWidgetItem(f"{prefix}   {label}{suffix}")
            item.setForeground(color)
            item.setSizeHint(item.sizeHint().expandedTo(self.viewport().size()).boundedTo(item.sizeHint()))
            self.addItem(item)
        if self._status:
            if self._status == "cancelled":
                label = "任务已停止"
            elif self._status == "stopped":
                label = "任务已结束（未出票）"
            else:
                label = "任务失败"
            item = QListWidgetItem(f"●   {label}{'  ' + message if message else ''}")
            item.setForeground(QColor("#e35d6a" if self._status == "failed" else "#ef9f43"))
            self.addItem(item)


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
        level = level.upper() if level.upper() in self.LEVEL_VALUE else "INFO"
        self._lines.append((line, level))
        if len(self._lines) > self.MAX_LINES:
            del self._lines[: len(self._lines) - self.MAX_LINES]
        if not self._paused and self._matches(line, level):
            color = self._level_colors[level]
            self.text.append(f'<span style="color:{color}; white-space:pre">{html.escape(line)}</span>')
            self.text.moveCursor(QTextCursor.MoveOperation.End)

    def _matches(self, line: str, level: str) -> bool:
        minimum = self.LEVEL_VALUE.get(self.level.currentText(), 20)
        needle = self.search.text().strip().casefold()
        return self.LEVEL_VALUE[level] >= minimum and (not needle or needle in line.casefold())

    def refresh(self) -> None:
        self.text.clear()
        chunks = []
        for line, level in self._lines:
            if self._matches(line, level):
                chunks.append(f'<span style="color:{self._level_colors[level]}; white-space:pre">{html.escape(line)}</span>')
        self.text.setHtml("<br>".join(chunks))
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
