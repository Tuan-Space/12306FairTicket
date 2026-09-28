"""Cart presentation and draft handling for the four-step window (offline)."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QToolButton, QVBoxLayout, QWidget,
)

from ticket_app.cart import (
    cart_migration_messages, cart_seat_types, normalize_cart_items,
    serialize_cart_items, validate_cart_items,
)
from ticket_app.configuration import SEAT_SPECS
from ticket_app.train_policy import normalize_train_codes
from .cart_widgets import CartDialog
from .widgets import Card


def cart_row_text(item, index):
    scope = "不限车次" if item.get("train_scope") == "all" else item.get("train_code", "未填写车次")
    return f"{index}. {item.get('from_station', '')} → {item.get('to_station', '')} · {scope} · {item.get('seat_type', '')}"


class CartFlow:
    def _build_cart_bar(self):
        """Pinned above the scrolling first page; the actual cart stays visible."""
        self.cart_bar = QWidget()
        layout = QVBoxLayout(self.cart_bar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        row = QHBoxLayout()
        self.cart_button = QPushButton("购物车（0） · 查看 / 排序")
        self.cart_button.clicked.connect(self._open_cart)
        row.addWidget(self.cart_button)
        self.cart_summary = QLabel("请添加至少一个备选")
        self.cart_summary.setWordWrap(True)
        self.cart_summary.setObjectName("muted")
        row.addWidget(self.cart_summary, 1)
        layout.addLayout(row)
        self.cart_migration_label = QLabel()
        self.cart_migration_label.setWordWrap(True)
        self.cart_migration_label.setObjectName("validationMessage")
        layout.addWidget(self.cart_migration_label)
        self.resolve_cart_button = QPushButton("确认按当前购物车范围执行")
        self.resolve_cart_button.clicked.connect(self._resolve_cart_migration)
        layout.addWidget(self.resolve_cart_button, alignment=Qt.AlignmentFlag.AlignLeft)
        self.cart_migration_label.hide()
        self.resolve_cart_button.hide()
        return self.cart_bar

    def _build_cart_entry(self):
        self.cart_items = []
        self.cart_migration = {"notes": [], "issues": []}
        self._cart_draft_baseline = None
        card = Card("添加备选", "一次选一种席别。多个车次会拆成多项；任意一项成功，全车停止。")
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
        grid.addWidget(self.update_stations_button, 1, 2, alignment=Qt.AlignmentFlag.AlignRight)
        self.train_scope = QComboBox()
        self.train_scope.addItem("指定车次", "specific")
        self.train_scope.addItem("不限车次（该站对所有车次）", "all")
        grid.addWidget(self._field_block("cart_train_scope", "车次范围", self.train_scope), 2, 0, 1, 3)
        self.preferred_trains = QLineEdit()
        self.preferred_trains.setPlaceholderText("例如：G103，G105；支持逗号、顿号、分号")
        self.preferred_trains.setClearButtonEnabled(True)
        self.preferred_trains.setToolTip("多个车次按填写顺序分别加入；支持中英文逗号、分号、顿号、换行和制表符。")
        grid.addWidget(self._field_block("preferred_trains", "指定车次", self.preferred_trains), 3, 0, 1, 3)
        self.cart_seat = QComboBox()
        self.cart_seat.addItem("请选择一种席别", "")
        for seat in SEAT_SPECS:
            self.cart_seat.addItem(seat, seat)
        grid.addWidget(self._field_block("cart_seat", "席别", self.cart_seat), 4, 0, 1, 3)
        card.body.addLayout(grid)
        self.add_cart_button = QPushButton("加入购物车")
        self.add_cart_button.setObjectName("primaryButton")
        self.add_cart_button.clicked.connect(self._add_cart_items)
        card.body.addWidget(self._field_block("cart_items", "", self.add_cart_button, show_label=False))
        self.cart_draft_status = QLabel("填写后请点击“加入购物车”，只有购物车中的项目参与任务。")
        self.cart_draft_status.setWordWrap(True)
        self.cart_draft_status.setObjectName("muted")
        card.body.addWidget(self.cart_draft_status)
        for editor in (self.from_station, self.to_station, self.preferred_trains):
            editor.textChanged.connect(self._cart_draft_changed)
        self.train_scope.currentIndexChanged.connect(self._cart_draft_changed)
        self.cart_seat.currentIndexChanged.connect(self._cart_draft_changed)
        return card

    def _cart_draft_signature(self):
        return (self.from_station.text().strip(), self.to_station.text().strip(),
                self.train_scope.currentData(), tuple(normalize_train_codes(self.preferred_trains.text())),
                self.cart_seat.currentData())

    def _cart_draft_changed(self, *_args):
        specific = self.train_scope.currentData() == "specific"
        self.preferred_trains.setEnabled(specific and self._active_operation is None)
        self.field_blocks["preferred_trains"].setVisible(specific)
        if not self._applying_values and self._cart_draft_baseline is not None:
            dirty = self._cart_draft_signature() != self._cart_draft_baseline
            self.cart_draft_status.setText("有尚未加入的更改，当前任务仍仅使用购物车。" if dirty else "填写内容已处理，可修改席别或站点继续添加。")

    def _draft_cart_items(self):
        scope = self.train_scope.currentData()
        trains = normalize_train_codes(self.preferred_trains.text()) if scope == "specific" else [""]
        if not trains:
            raise ValueError("请填写指定车次，或明确选择“不限车次”。")
        items = [{"from_station": self.from_station.text().strip(), "to_station": self.to_station.text().strip(),
                  "train_scope": scope, "train_code": train, "seat_type": self.cart_seat.currentData() or ""}
                 for train in dict.fromkeys(trains)]
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
            self.cart_draft_status.setText(str(exc))
            self._show_flow_error(str(exc))
            return False
        added = 0
        for item in items:
            if item not in self.cart_items:
                self.cart_items.append(item)
                added += 1
        self._cart_draft_baseline = self._cart_draft_signature()
        self._cart_changed()
        duplicate = len(items) - added
        self.cart_draft_status.setText(f"已加入 {added} 项" + (f"；{duplicate} 项已存在，原顺序保持不变。" if duplicate else "，可修改后继续添加。"))
        self.flow_error.hide()
        return True

    def _handle_unadded_cart_draft(self):
        if self._cart_draft_signature() == self._cart_draft_baseline:
            return True
        box = QMessageBox(self)
        box.setWindowTitle("还有未加入的备选")
        box.setText("添加区域有尚未处理的更改。此次任务只会使用购物车中的项目。")
        add = box.addButton("加入并继续", QMessageBox.ButtonRole.AcceptRole)
        use = box.addButton("仅使用购物车", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("返回编辑", QMessageBox.ButtonRole.RejectRole)
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
        self.cart_button.setText(f"购物车（{count}） · 查看 / 排序")
        pairs = {(row.get("from_station"), row.get("to_station")) for row in self.cart_items}
        text = f"{len(pairs)} 个站对 · 从上到下尝试，成功一项即停止" if count else "请添加至少一个备选"
        if count:
            text += "\n" + cart_row_text(self.cart_items[0], 1)
            if count > 1:
                text += f" · 另 {count - 1} 项"
        self.cart_summary.setText(text)
        notes, issues = cart_migration_messages(self.cart_migration)
        self.cart_migration_label.setText(
            "旧配置有待修正的限制，请在购物车中核对后确认范围。" if issues else
            "旧配置部分尝试顺序已调整，详情见购物车与确认页。" if notes else "")
        self.cart_migration_label.setToolTip("\n".join(issues + notes))
        self.cart_migration_label.setVisible(bool(notes or issues))
        self.resolve_cart_button.setVisible(bool(issues))
        if hasattr(self, "position_preferences"):
            self.position_preferences.adapt_to_seats(cart_seat_types(normalize_cart_items(self.cart_items)))
        if not self._applying_values:
            self._schedule_validation("cart_items", "seat_position_preferences", "berth_preference")

    def _open_cart(self, *_args):
        read_only = self._active_operation is not None or self.current_step == 3
        items = self.cart_items
        if (self.current_step == 3 or self._operation_mode == "task") and self._last_run_config is not None:
            items = serialize_cart_items(self._last_run_config.cart_items or [])
        notes, issues = cart_migration_messages(self.cart_migration)
        dialog = CartDialog(items, self, station_names=self.station_names,
                            migration_warnings=issues + notes, read_only=read_only)
        if dialog.exec() == QDialog.DialogCode.Accepted and not read_only:
            self.cart_items = dialog.items()
            self._cart_changed()

    def _resolve_cart_migration(self):
        notes, issues = cart_migration_messages(self.cart_migration)
        if not issues or self._active_operation is not None:
            return
        # The former restriction is never silently discarded by editing a row.
        text = "\n".join(issues) + "\n\n请先在购物车中核对或编辑每一项。确认后仅按当前可见车次范围执行；“不限车次”会接受该站对所有列车。"
        answer = QMessageBox.question(self, "确认旧配置的范围调整", text,
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                                      QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Yes:
            self.cart_migration["issues"] = []
            self._cart_changed()

    def _cart_confirmation_text(self):
        rows = [cart_row_text(item, i) for i, item in enumerate(self.cart_items, 1)]
        notes, issues = cart_migration_messages(self.cart_migration)
        return "购物车（按此顺序尝试）\n" + ("\n".join(rows) or "尚未添加") + ("\n\n" + "\n".join(issues + notes) if notes or issues else "")

    def _update_cart_runtime(self, payload):
        index = payload.get("cart_index")
        if not index:
            return
        prefix = f"备选 {index} / {payload.get('cart_total', len(self.cart_items))}"
        route = f"{payload.get('from_station', '')} → {payload.get('to_station', '')}"
        train = payload.get("train_code") or payload.get("train") or "不限车次"
        seat = payload.get("seat_label") or payload.get("seat") or ""
        date = payload.get("train_date", "")
        self.current_cart_item.setText(f"{prefix}  {date}\n{route} · {train} · {seat}")
