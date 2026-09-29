"""Cart presentation and draft handling for the four-step window (offline)."""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox, QDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QToolButton,
)

from ticket_app.cart import (
    cart_seat_types, normalize_cart_items,
    serialize_cart_items, validate_cart_items,
)
from ticket_app.configuration import SEAT_SPECS
from ticket_app.preferences import BERTH_SEAT_TYPES, SEATED_SEAT_TYPES
from ticket_app.train_policy import normalize_train_codes
from .cart_widgets import CartDialog
from .widgets import Card, OrderedSeatSelector
from .floating_cart import FloatingCartButton


def cart_row_text(item, index):
    scope = "不限车次" if item.get("train_scope") == "all" else item.get("train_code", "未填写车次")
    return f"{index}. {item.get('from_station', '')} → {item.get('to_station', '')} · {scope} · {item.get('seat_type', '')}"


class CartFlow:
    def _build_cart_shortcut(self):
        self.cart_button = FloatingCartButton(self.steps)
        self.cart_button.clicked.connect(self._open_cart)
        self.steps.installEventFilter(self)
        self._position_cart_shortcut()

    def _position_cart_shortcut(self):
        if not hasattr(self, "cart_button"):
            return
        self.cart_button.move(max(0, self.steps.width() - 88), max(0, self.steps.height() - 76))
        self.cart_button.raise_()

    def _build_cart_entry(self):
        self.cart_items = []
        self._cart_draft_baseline = None
        card = Card("添加备选")
        self._cart_feedback_generation = 0
        self._cart_draft_error_shown = False
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(2, 1)
        self.from_station = QLineEdit()
        self.to_station = QLineEdit()
        for editor, placeholder in ((self.from_station, "例如：北京南"), (self.to_station, "例如：上海虹桥")):
            editor.setPlaceholderText(placeholder)
            editor.setClearButtonEnabled(True)
        self.swap_stations_button = QToolButton()
        self.swap_stations_button.setText("⇄")
        self.swap_stations_button.setToolTip("交换出发站和到达站")
        self.swap_stations_button.clicked.connect(self._swap_stations)
        grid.addWidget(self._field_block("from_station", "出发站", self.from_station), 0, 0)
        grid.addWidget(self.swap_stations_button, 0, 1, alignment=Qt.AlignmentFlag.AlignBottom)
        grid.addWidget(self._field_block("to_station", "到达站", self.to_station), 0, 2)
        self.update_stations_button = QPushButton("更新站点")
        self.update_stations_button.clicked.connect(self._refresh_stations)
        grid.addWidget(self.update_stations_button, 0, 3, alignment=Qt.AlignmentFlag.AlignBottom)
        self.train_scope = QComboBox()
        self.train_scope.addItem("指定车次", "specific")
        self.train_scope.addItem("不限车次", "all")
        self.train_scope.setToolTip("不限车次：接受这一站对的所有列车，但仅购买已加入购物车的席别。")
        train_row = QHBoxLayout()
        train_row.setSpacing(8)
        scope_block = self._field_block("cart_train_scope", "车次范围", self.train_scope)
        scope_block.setMaximumWidth(170)
        train_row.addWidget(scope_block, 1)
        self.preferred_trains = QLineEdit()
        self.preferred_trains.setPlaceholderText("例如：G103，G105")
        self.preferred_trains.setClearButtonEnabled(True)
        self.preferred_trains.setToolTip("多个车次按填写顺序分别加入；支持中英文逗号、分号、顿号、换行和制表符。")
        train_row.addWidget(self._field_block("preferred_trains", "指定车次", self.preferred_trains), 3)
        card.body.addLayout(grid)
        card.body.addLayout(train_row)
        self.cart_seat = OrderedSeatSelector()
        card.body.addWidget(self._field_block("cart_seat", "席别", self.cart_seat))
        self.cart_preview = QLabel()
        self.cart_preview.setTextFormat(Qt.TextFormat.PlainText)
        self.cart_preview.setWordWrap(True)
        self.cart_preview.setObjectName("muted")
        self.cart_preview.hide()
        self.cart_preview_toggle = QPushButton("预览")
        self.cart_preview_toggle.setCheckable(True)
        self.cart_preview_toggle.toggled.connect(self.cart_preview.setVisible)
        self.add_cart_button = QPushButton("加入购物车")
        self.add_cart_button.setObjectName("primaryButton")
        self.add_cart_button.clicked.connect(self._add_cart_items)
        add_row = QHBoxLayout()
        add_row.addWidget(self._field_block("cart_items", "", self.add_cart_button, show_label=False), 1)
        add_row.addWidget(self.cart_preview_toggle, alignment=Qt.AlignmentFlag.AlignTop)
        card.body.addLayout(add_row)
        card.body.addWidget(self.cart_preview)
        self.cart_draft_status = QLabel()
        self.cart_draft_status.setWordWrap(True)
        self.cart_draft_status.setObjectName("validationMessage")
        self.cart_draft_status.hide()
        card.body.addWidget(self.cart_draft_status)
        for editor in (self.from_station, self.to_station, self.preferred_trains):
            editor.textChanged.connect(self._cart_draft_changed)
        self.train_scope.currentIndexChanged.connect(self._cart_draft_changed)
        self.cart_seat.changed.connect(self._cart_draft_changed)
        return card

    def _cart_draft_signature(self):
        return (self.from_station.text().strip(), self.to_station.text().strip(),
                self.train_scope.currentData(), tuple(normalize_train_codes(self.preferred_trains.text())),
                tuple(self.cart_seat.selected_seats()))

    def _cart_draft_changed(self, *_args):
        self._cart_feedback_generation += 1
        specific = self.train_scope.currentData() == "specific"
        self.preferred_trains.setEnabled(specific and self._active_operation is None)
        try:
            items = self._draft_cart_items()
        except ValueError as exc:
            self.add_cart_button.setText("加入购物车")
            self.cart_preview.clear()
            self.cart_preview_toggle.setEnabled(False)
            if self._cart_draft_error_shown and not self._applying_values:
                self.cart_draft_status.setText(str(exc))
                self.cart_draft_status.show()
            return
        duplicates = sum(item in self.cart_items for item in items)
        count = len(items) - duplicates
        self.add_cart_button.setText(f"加入购物车（{count}项）" if count else "加入购物车")
        self.cart_preview_toggle.setEnabled(True)
        self.cart_preview.setText("\n".join(cart_row_text(item, i) for i, item in enumerate(items, 1)))
        self.cart_draft_status.clear()
        self.cart_draft_status.hide()
        self._cart_draft_error_shown = False

    def _draft_cart_items(self):
        scope = self.train_scope.currentData()
        trains = normalize_train_codes(self.preferred_trains.text()) if scope == "specific" else [""]
        if not trains:
            raise ValueError("请填写指定车次，或明确选择“不限车次”。")
        seats = self.cart_seat.selected_seats()
        if not seats:
            raise ValueError("请至少勾选一种席别。")
        items = [{"from_station": self.from_station.text().strip(), "to_station": self.to_station.text().strip(),
                  "train_scope": scope, "train_code": train, "seat_type": seat}
                 for seat in seats for train in dict.fromkeys(trains)]
        errors = validate_cart_items(normalize_cart_items(items), SEAT_SPECS, self.station_names)
        if errors:
            raise ValueError(errors[0])
        return items

    def _add_cart_items(self, *_args):
        if self._active_operation is not None:
            return False
        try:
            items = self._draft_cart_items()
        except ValueError as exc:
            self._cart_draft_error_shown = True
            self.cart_draft_status.setText(str(exc))
            self.cart_draft_status.show()
            return False
        added = 0
        for item in items:
            if item not in self.cart_items:
                self.cart_items.append(item)
                added += 1
        self._cart_draft_baseline = self._cart_draft_signature()
        self._cart_changed()
        self.add_cart_button.setText("已加入" if added else "已在购物车")
        generation = self._cart_feedback_generation
        def restore_button():
            if generation == self._cart_feedback_generation:
                self._cart_draft_changed()
        QTimer.singleShot(1200, self, restore_button)
        self.flow_error.hide()
        return True

    def _handle_unadded_cart_draft(self):
        if self._cart_draft_signature() == self._cart_draft_baseline:
            return True
        box = QMessageBox(self)
        box.setWindowTitle("还有未加入的备选")
        try:
            items = self._draft_cart_items()
            trains = "、".join(dict.fromkeys(item["train_code"] for item in items)) if self.train_scope.currentData() == "specific" else "不限车次"
            seats = "、".join(self.cart_seat.selected_seats())
            box.setText(f"是否将{self.from_station.text().strip()} → {self.to_station.text().strip()}的"
                        f" {trains}，{seats}，共 {len(items)} 个备选加入购物车？")
            duplicates = sum(item in self.cart_items for item in items)
            if duplicates:
                box.setInformativeText(f"其中 {duplicates} 项已存在，会保留原位置，不重复加入。")
        except ValueError as exc:
            box.setText(f"这组备选还不能加入购物车：{exc}")
            box.setInformativeText("可以返回修改，或不加入，继续使用购物车中已有的备选。")
        add = box.addButton("加入并继续", QMessageBox.ButtonRole.AcceptRole)
        use = box.addButton("不加入，继续下一步", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("返回修改", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(add)
        box.exec()
        if box.clickedButton() is add:
            return self._add_cart_items()
        if box.clickedButton() is use:
            self._cart_draft_baseline = self._cart_draft_signature()
            return True
        return False

    def _cart_changed(self):
        count = len(self.cart_items)
        self.cart_button.set_count(count)
        if hasattr(self, "position_preferences"):
            self.position_preferences.adapt_to_seats(cart_seat_types(normalize_cart_items(self.cart_items)))
            self._refresh_preference_scope()
        self._cart_draft_changed()
        if not self._applying_values:
            self._schedule_validation("cart_items", "seat_position_preferences", "berth_preference")
            if self.current_step == 2:
                self._refresh_confirmation()
            self._render_workflow()
            if not count and self.current_step in (1, 2):
                self._show_flow_error("购物车为空，请返回第一步添加备选。")
            elif self.flow_error.text() == "购物车为空，请返回第一步添加备选。":
                self.flow_error.clear()
                self.flow_error.hide()

    def _refresh_preference_scope(self):
        if not hasattr(self, "position_preferences"):
            return
        counts = []
        for allowed in (SEATED_SEAT_TYPES, BERTH_SEAT_TYPES):
            count = 0
            for item in self.cart_items:
                seat = item.get("seat_type", "")
                code = SEAT_SPECS[seat].submit_code if seat in SEAT_SPECS else None
                if seat != "无座" and code in allowed:
                    count += 1
            counts.append(count)
        for index, title in enumerate(("座位偏好", "铺位偏好")):
            self.position_preferences.tabs.setTabText(index, f"{title}（{counts[index]}项）")

    def _open_cart(self, *_args):
        read_only = self._active_operation is not None or self.current_step == 3
        items = self.cart_items
        if (self.current_step == 3 or self._operation_mode == "task") and self._last_run_config is not None:
            items = serialize_cart_items(self._last_run_config.cart_items or [])
        dialog = CartDialog(items, self, station_names=self.station_names, read_only=read_only)
        if dialog.exec() == QDialog.DialogCode.Accepted and not read_only:
            self.cart_items = dialog.items()
            self._cart_changed()

    def _update_cart_runtime(self, payload):
        index = payload.get("cart_index")
        if not index:
            return
        self._last_candidate_context = dict(payload)
        terminal = self._task_state in {"success", "cancelled", "failed", "no_ticket", "unknown"}
        title = "已购" if self._order_succeeded else "最后尝试" if terminal else "当前"
        prefix = f"{title} {index}/{payload.get('cart_total', len(self.cart_items))}"
        route = f"{payload.get('from_station', '')} → {payload.get('to_station', '')}"
        train = payload.get("train_code") or payload.get("train") or "不限车次"
        seat = payload.get("seat_label") or payload.get("seat") or ""
        self.current_cart_item.setText(f"{prefix} · {route} · {train} · {seat}")
        self.current_cart_item.show()
