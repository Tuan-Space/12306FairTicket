"""Four-step presentation and navigation; no network or order work here."""

from __future__ import annotations

from collections import Counter

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

from .validation import validate_gui_mapping
from .widgets import Card, LogView
from .passenger_widgets import InlinePassengerSelector, default_ticket_label


class WizardFlow:
    """Keep page, account, background operation and task status independent."""

    def _build_wizard(self, root):
        from .app import _scroll_page

        self.current_step = 0
        self._visited_step = 0
        self.account_state = "unknown"
        self._task_state = "idle"
        self._operation_message = ""
        self._contacts = []
        self._contacts_loaded = False
        self._contacts_error = ""

        navigation = QHBoxLayout()
        self.step_buttons = []
        for index, title in enumerate(("设置行程", "登录与乘车人", "确认任务", "等待结果")):
            button = QPushButton(f"{index + 1}  {title}")
            button.setObjectName("stepButton")
            button.clicked.connect(lambda _checked=False, step=index: self._visit_step(step))
            navigation.addWidget(button, 1)
            self.step_buttons.append(button)
        root.addLayout(navigation)
        self.workflow_status = QLabel("任务未开始")
        self.workflow_status.setObjectName("workflowStatus")
        self.workflow_status.setWordWrap(True)
        root.addWidget(self.workflow_status)
        self.flow_error = QLabel()
        self.flow_error.setObjectName("validationMessage")
        self.flow_error.setWordWrap(True)
        self.flow_error.hide()
        root.addWidget(self.flow_error)

        self.steps = QStackedWidget()
        self.passenger_scroll, _, self.passenger_layout = _scroll_page()
        self.confirm_scroll, _, self.confirm_layout = _scroll_page()
        self.account_card = Card("12306 账号", "")
        self.passenger_layout.addWidget(self.account_card)
        self.account_heading = QLabel("未登录")
        self.account_heading.setWordWrap(True)
        self.account_card.body.addWidget(self.account_heading)
        self.account_actions = QHBoxLayout()
        self.account_card.body.addLayout(self.account_actions)

        self.passenger_card = Card("选择乘车人", "")
        self.contact_selector = InlinePassengerSelector()
        self.contact_selector.changed.connect(self._contacts_selected)
        self.passenger_card.body.addWidget(self.contact_selector)
        self.contacts_status = QLabel()
        self.contacts_status.setWordWrap(True)
        self.contacts_status.setObjectName("muted")
        self.passenger_card.body.addWidget(self.contacts_status)
        self.refresh_contacts_button = QPushButton("重新读取联系人")
        self.refresh_contacts_button.clicked.connect(lambda: self._start_connection_operation("contacts"))
        self.passenger_card.body.addWidget(self.refresh_contacts_button, alignment=Qt.AlignmentFlag.AlignLeft)
        self.manual_toggle = QPushButton("手动填写姓名 ▾")
        self.manual_toggle.setCheckable(True)
        self.passenger_card.body.addWidget(self.manual_toggle, alignment=Qt.AlignmentFlag.AlignLeft)
        self.passenger_layout.addWidget(self.passenger_card)
        self.summary_card = Card("确认本次任务", "")
        self.confirm_summary = QLabel()
        self.confirm_summary.setTextFormat(Qt.TextFormat.PlainText)
        self.confirm_summary.setWordWrap(True)
        self.confirm_summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.summary_card.body.addWidget(self.confirm_summary)
        self.confirm_layout.addWidget(self.summary_card)

        self.steps.addWidget(self._build_basic_page())
        self.steps.addWidget(self.passenger_scroll)
        self.steps.addWidget(self.confirm_scroll)
        self.run_scroll, _, self.run_layout = _scroll_page()
        self.run_layout.addWidget(self._build_status_panel())
        self.steps.addWidget(self.run_scroll)
        self.passenger_layout.addStretch(1)
        self.confirm_layout.addStretch(1)

        # Advanced controls are part of step 1 and only expanded on demand.
        self.advanced_toggle = QPushButton("高级参数 ▾")
        self.advanced_toggle.setCheckable(True)
        advanced_scroll = self._build_advanced_page()
        self.advanced_content = advanced_scroll.takeWidget()
        advanced_scroll.deleteLater()
        self.advanced_content.hide()
        self.advanced_toggle.toggled.connect(self.advanced_content.setVisible)
        layout = self.basic_scroll.widget().layout()
        layout.insertWidget(layout.count() - 1, self.advanced_toggle)
        layout.insertWidget(layout.count() - 1, self.advanced_content)
        for key in self.advanced:
            self.field_pages[key] = 0
        self.advanced_scroll = self.basic_scroll

        self.logs_toggle = QPushButton("详细日志（已脱敏） ▾")
        self.logs_toggle.setCheckable(True)
        self.logs_toggle.setChecked(True)
        self.log_view = LogView()
        self.log_view.text.setMinimumHeight(44)
        self.log_view.setFixedHeight(160)
        self.logs_toggle.toggled.connect(self.log_view.setVisible)
        self.run_layout.addStretch(1)
        root.addWidget(self.steps, 1)
        self.logs_panel = QWidget()
        logs_layout = QVBoxLayout(self.logs_panel)
        logs_layout.setContentsMargins(0, 0, 0, 0)
        logs_layout.setSpacing(4)
        logs_layout.addWidget(self.logs_toggle)
        logs_layout.addWidget(self.log_view)
        root.addWidget(self.logs_panel)

        footer = QHBoxLayout()
        self.back_button = QPushButton("上一步")
        self.back_button.clicked.connect(self._back_step)
        footer.addWidget(self.back_button)
        footer.addStretch(1)
        self.modify_button = QPushButton("修改配置")
        self.modify_button.clicked.connect(self._modify_configuration)
        self.next_button = QPushButton()
        self.next_button.setObjectName("primaryButton")
        self.next_button.clicked.connect(self._next_step)
        for button in (self.modify_button, self.stop_button, self.order_button,
                       self.next_button, self.start_button):
            footer.addWidget(button)
        root.addLayout(footer)
        self._render_workflow()

    def _visit_step(self, step):
        if self._active_operation is None and step <= self._visited_step and step < 3 and self.current_step < 3:
            self._go_to_step(step)

    def _go_to_step(self, step):
        self.current_step = max(0, min(3, step))
        self._visited_step = max(self._visited_step, self.current_step)
        self.steps.setCurrentIndex(self.current_step)
        self.flow_error.clear()
        self.flow_error.hide()
        if self.current_step == 2:
            self._refresh_confirmation()
        # One QR view follows the current operation, never creating a second login path.
        host = self.recovery_layout if self.current_step == 3 else self.account_card.body
        host.addWidget(self.authentication_panel)
        self._render_workflow()

    def _next_step(self):
        if self._active_operation is not None or self.current_step not in (0, 1):
            return
        if self.current_step == 1 and self.account_state != "valid":
            self._show_flow_error("请先扫码登录 12306，再选择乘车人。")
            return
        errors = self._step_errors(self.current_step)
        if errors:
            self._touched_fields.update(errors)
            self._apply_validation(errors)
            self._focus_first_error(errors)
            self._show_flow_error(next(iter(errors.values())))
            return
        self._go_to_step(self.current_step + 1)

    def _step_errors(self, step):
        errors = validate_gui_mapping(self._collect_mapping(), self.station_names)
        errors.update(self._contact_errors())
        return {key: value for key, value in errors.items() if self.field_pages.get(key, 0) == step}

    def _contact_errors(self):
        from .app import _split_names
        counts = Counter(row["name"] for row in self._contacts)
        names = _split_names(self.passengers.text())
        duplicate = [name for name in names if counts[name] > 1]
        if duplicate:
            return {"passenger_names": "账号存在同名联系人，无法唯一匹配：" + "、".join(duplicate)}
        missing = [name for name in names if name not in counts]
        if self._contacts_loaded and missing:
            return {"passenger_names": "当前账号中找不到乘车人：" + "、".join(missing) + "；请核对姓名或重新读取联系人"}
        categories = {row["name"]: row.get("passenger_type") for row in self._contacts}
        for name, kind in self.passenger_ticket_types.values().items():
            if kind == "student" and name in categories and categories[name] != "3":
                return {"passenger_ticket_types": f"{name}不是学生联系人，不能选择学生票"}
        return {}

    def _show_flow_error(self, text):
        self.flow_error.setText(text)
        self.flow_error.setVisible(bool(text))

    def _contacts_selected(self):
        self.passengers.setText("，".join(self.contact_selector.selected_names()))

    def _modify_configuration(self):
        if self._active_operation is not None or self._order_state != "safe":
            return
        self._restart_allowed = False
        self._task_state = "idle"
        self._operation_message = ""
        self._go_to_step(0)

    def _back_step(self):
        if self.current_step == 3:
            if self._active_operation is not None:
                if self._operation_mode != "task":
                    return
                self._return_after_stop = True
                self._restart_allowed = False
                self._stop_task()
                self._go_to_step(2)
            elif self._order_state == "safe":
                self._restart_allowed = False
                self._go_to_step(2)
            else:
                self._show_flow_error("请先到 12306 核对已有订单。停止不会撤销订单。")
        elif self._active_operation is None and self.current_step > 0:
            self._go_to_step(self.current_step - 1)

    def _refresh_confirmation(self):
        from .app import _split_names
        values = self._collect_mapping()
        labels = {"adult": "成人票", "student": "学生票"}
        contact_types = {row["name"]: row.get("passenger_type", "") for row in self._contacts}
        people = "、".join(f"{name}（{labels.get(values['passenger_ticket_types'].get(name), default_ticket_label(name, contact_types))}）"
                          for name in _split_names(self.passengers.text())) or "未选择（仅监控）"
        self.confirm_summary.setText(
            f"{values['from_station']} → {values['to_station']}    {values['train_date']}\n\n"
            f"乘车人：{people}\n"
            f"席别顺序：{' → '.join(values['seat_types']) or '尚未选择'}\n"
            f"优先车次：{'、'.join(values['preferred_trains']) or '未指定'}\n"
            f"{self.range_summary.text()}\n"
            f"尝试策略：{'席别优先' if values['priority_strategy'] == 'seat_first' else '车次优先'}\n"
            f"{self.order_preview.text()}\n\n"
            f"任务模式：{'自动提交订单，之后手动支付' if values['auto_submit'] else '仅监控，不提交订单'}\n"
            f"开始 / 开售时间：{values['start_at'] or '点击开始后立即查询'}\n"
            f"停止时间：{values['stop_at'] or '不设停止时间（仍受最大查询轮数限制）'}"
        )

    def _render_workflow(self):
        if not hasattr(self, "next_button"):
            return
        from .app import _split_names
        active = self._active_operation is not None
        running = self._operation_mode == "task" and active
        cancelling = bool(self.cancel_token and self.cancel_token.is_cancelled)
        step = self.current_step
        count = len(_split_names(self.passengers.text()))
        self.steps.setCurrentIndex(step)
        for index, button in enumerate(self.step_buttons):
            state = "current" if index == step else "complete" if index < step else "pending"
            button.setProperty("stepState", state)
            button.style().unpolish(button)
            button.style().polish(button)
            button.setEnabled(not active and step < 3 and index <= self._visited_step and index < 3)
        self.back_button.setVisible(step in (1, 2, 3))
        self.back_button.setEnabled((not active and self._order_state == "safe") or (step == 3 and running and not cancelling))
        self.next_button.setVisible(step == 0 or (step == 1 and self.account_state == "valid"))
        self.next_button.setText("下一步：登录与乘车人" if step == 0 else "下一步：确认任务")
        self.next_button.setEnabled(not active)
        self.login_button.setVisible(step == 1 and self.account_state != "valid")
        self.login_button.setEnabled(not active)
        self.refresh_qr_button.setVisible(self._login_required and self._qr_deadline <= 0)
        self.start_button.setVisible(step == 2)
        self.start_button.setEnabled(not active and step == 2 and self.account_state == "valid" and self._order_state == "safe")
        can_continue = step == 3 and not active and self._restart_allowed and self._order_state == "safe"
        self.stop_button.setVisible(active or can_continue)
        self.stop_button.setText("继续任务" if can_continue else "正在停止…" if cancelling else "停止任务" if running else "取消当前操作")
        self.stop_button.setEnabled(can_continue or (active and not cancelling))
        self.modify_button.hide()
        self.order_button.setVisible(step == 3 and self.order_button.isEnabled())
        self.order_button.setText("前往 12306 支付" if self._order_succeeded else "前往 12306 核对订单")
        self.order_button.setObjectName("primaryButton" if self._order_succeeded else "")
        self.refresh_contacts_button.setEnabled(not active and self.account_state == "valid")
        self.contact_selector.setEnabled(not active and self.account_state == "valid")
        self.contact_selector.empty_hint.setText("登录后显示乘车人。" if self.account_state != "valid" else "没有可选择的联系人，可重新读取或手动填写姓名。")
        self.manual_toggle.setEnabled(not active)
        self.contacts_status.setText(self._contacts_error)
        self.contacts_status.setVisible(bool(self._contacts_error))
        account_text = "已登录" if self.account_state == "valid" else "正在登录…" if active and self._operation_mode == "login" else "未登录"
        self.account_heading.setText(account_text)
        self.authentication_panel.setVisible((self._login_required or (active and self._operation_mode == "login")) and self.account_state != "valid")
        self.recovery_card.setVisible(step == 3 and self._login_required)
        self.logs_panel.setVisible(step == 3)
        if step < 3:
            self.workflow_status.setText("正在停止上一轮任务" if self._return_after_stop and active else "任务未开始")
        else:
            self.workflow_status.setText("任务已启动 · " + self.phase_badge.text() if running else self.phase_badge.text())
        self.prep_clock_status.setText(self.clock_check_status.text() + " · RTT " + self.rtt_metric.value_label.text()
                                       + " · 偏移 " + self.offset_metric.value_label.text())
        self.prep_sync_clock_button.setEnabled(self.sync_clock_button.isEnabled())
        self.run_check_login_button.setEnabled(self.check_login_button.isEnabled())
        self.run_session_status.setText(self.session_check_status.text())
