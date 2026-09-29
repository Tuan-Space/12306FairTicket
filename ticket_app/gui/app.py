"""Modern single-window PySide6 application."""

from __future__ import annotations

import argparse
from copy import deepcopy
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

import requests
from PySide6.QtCore import QDate, QEvent, QObject, QThread, QTimer, QUrl, Qt, Signal, Slot
from PySide6.QtGui import QCloseEvent, QDesktopServices, QGuiApplication, QIcon, QPalette, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCompleter,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStyle,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from ticket_app import __version__
from ticket_app.clock import ServerClock
from ticket_app.configuration import AppConfig, AppError, ConnectionConfig, SEAT_SPECS, DEFAULT_CONFIG_FILE
from ticket_app.input_parsing import split_multi_value_text
from ticket_app.preferences import BERTH_SEAT_TYPES, SEATED_SEAT_TYPES

from .settings import (
    DEFAULT_VALUES,
    LOCAL_DATA_DIR,
    PROJECT_ROOT,
    STATION_CACHE_FILE,
    build_app_config,
    cached_station_names,
    canonical_mapping,
    load_gui_settings,
    save_gui_settings,
)
from .station_worker import StationRefreshWorker
from .passenger_widgets import PassengerTicketEditor
from .wizard import WizardFlow
from .cart_flow import CartFlow
from .scroll_state import preserve_reading_position
from .validation import validate_gui_mapping
from .widgets import (
    Card,
    CleanDoubleSpinBox,
    CleanSpinBox,
    DatePickerWidget,
    HelpLabel,
    PositionPreferences,
    TimeFieldsWidget,
    set_validation_state,
)
from .worker import (GuiCancelToken, LogBridge, OperationRequest, OperationOutcome,
                     OperationWorker, create_async_log_pipeline)


ASSET_DIR = PROJECT_ROOT / "assets"
APP_ICON = ASSET_DIR / "app_icon.svg"
APP_QSS = ASSET_DIR / "app.qss"
ORDER_URL = "https://kyfw.12306.cn/otn/view/train_order.html"


def _split_names(text: str) -> list[str]:
    return split_multi_value_text(text)


def _scroll_page() -> tuple[QScrollArea, QWidget, QVBoxLayout]:
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(4, 4, 10, 12)
    layout.setSpacing(14)
    scroll.setWidget(page)
    return scroll, page, layout


class StationRefreshRelay(QObject):
    """Move the plain Python station worker callback onto the GUI thread."""

    completed = Signal(object, object)


class MainWindow(CartFlow, WizardFlow, QMainWindow):
    submit_operation = Signal(object)
    retire_executor = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("12306 Fair Ticket")
        self.resize(750, 880)
        # Qt reports logical screen pixels; fit the first window at high DPI.
        self.setMinimumSize(640, 480)
        screen = QGuiApplication.primaryScreen()
        if screen:
            available = screen.availableGeometry()
            self.setMinimumSize(min(640, max(480, available.width() - 40)),
                                min(480, max(360, available.height() - 60)))
            self.resize(min(750, max(480, available.width() - 40)),
                        min(880, max(360, available.height() - 60)))
        if APP_ICON.exists():
            self.setWindowIcon(QIcon(str(APP_ICON)))

        self.station_names = set(cached_station_names())
        self.station_refresh_worker: Optional[StationRefreshWorker] = None
        self.station_refresh_relay = StationRefreshRelay(self)
        self.station_refresh_relay.completed.connect(self._on_station_refresh_finished)
        self.field_widgets: Dict[str, QWidget] = {}
        self.field_blocks: Dict[str, QWidget] = {}
        self.field_messages: Dict[str, QLabel] = {}
        self.field_pages: Dict[str, int] = {}
        self._applying_values = False
        self._touched_fields: set[str] = set()
        self._last_validation_errors: Dict[str, str] = {}
        # Kept only in memory. Sequential tasks in this application process can
        # reuse a confirmed login without ever writing cookies to disk.
        self.shared_session = requests.Session()
        # Construction reads the local clock only. Network synchronization and
        # settings changes belong exclusively to the active worker thread.
        self.shared_clock = ServerClock(
            self.shared_session, ConnectionConfig.from_mapping(DEFAULT_VALUES, base_dir=LOCAL_DATA_DIR),
        )
        # The executor is window-owned and remains alive between operations.
        self._background_thread: Optional[QThread] = None
        self._executor: Optional[OperationWorker] = None
        self._active_operation: Optional[OperationRequest] = None
        self._closing_executor = False
        self._session_closed = False
        self._last_run_config: Optional[AppConfig] = None
        self._last_run_stop_deadline: Optional[datetime] = None
        self._restart_allowed = False
        self._order_state = "safe"
        self._return_after_stop = False
        self.cancel_token: Optional[GuiCancelToken] = None
        self._pending_restart = False
        self._operation_mode: Optional[str] = None
        self._operation_generation = 0
        self._qr_operation = "login"
        self._pending_contacts: Optional[list[dict[str, str]]] = None
        self._maintenance_enabled = False
        self._maintenance_busy = False
        self._login_required = False
        self._close_when_finished = False
        self._qr_deadline = 0.0
        self._target_timestamp: Optional[float] = None
        self._server_anchor: Optional[tuple[float, float]] = None
        self._order_id = ""
        self._order_succeeded = False
        self._success_prompt_shown = False
        self._completion_prompt_shown = False
        self._last_phase = ""
        self._last_candidate_context = None
        self._session_check_text = ""
        self._clock_check_text = ""
        self._rtt_value = "--"
        self._offset_value = "--"

        self.validation_timer = QTimer(self)
        self.validation_timer.setSingleShot(True)
        self.validation_timer.setInterval(250)
        self.validation_timer.timeout.connect(self._validate_live)

        self.query_ui_timer = QTimer(self)
        self.query_ui_timer.setSingleShot(True)
        self.query_ui_timer.setInterval(100)
        self.query_ui_timer.timeout.connect(self._flush_query_event)
        self._pending_query_payload: Optional[Dict[str, Any]] = None

        self.log_bridge = LogBridge(self)
        self.log_pipeline = create_async_log_pipeline(self.log_bridge, batch_interval=0.1)

        self._build_ui()
        application = QApplication.instance()
        theme_property = application.property("darkTheme") if application else None
        is_dark_theme = (
            bool(theme_property)
            if theme_property is not None
            else bool(application and application.palette().color(QPalette.ColorRole.Window).lightness() < 128)
        )
        self.log_view.set_light_palette(not is_dark_theme)
        self._install_station_completers()
        self.log_bridge.messages.connect(self._on_log_batch)
        logging.getLogger().addHandler(self.log_pipeline.handler)
        logging.getLogger().setLevel(logging.DEBUG)
        self._setup_tray()
        self._load_initial_values()

        self.clock_timer = QTimer(self)
        self.clock_timer.setInterval(100)
        self.clock_timer.timeout.connect(self._update_countdowns)
        self.clock_timer.start()
        logging.info("GUI 已就绪；启动前不会访问 12306")

    # ----- UI construction -------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("appRoot")
        root = QVBoxLayout(central)
        root.setContentsMargins(22, 18, 22, 20)
        root.setSpacing(14)
        self.setCentralWidget(central)

        header = QFrame()
        header.setObjectName("header")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(2, 0, 2, 0)
        logo = QLabel("◈")
        self._header_logo = logo
        logo.setObjectName("logoMark")
        title_box = QVBoxLayout()
        title = QLabel("12306 Fair Ticket")
        title.setObjectName("appTitle")
        subtitle = QLabel(f"v{__version__}")
        self._header_subtitle = subtitle
        subtitle.setObjectName("muted")
        subtitle.setWordWrap(True)
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header_layout.addWidget(logo)
        header_layout.addLayout(title_box, 1)
        header_layout.addWidget(self._build_settings_bar())
        root.addWidget(header)

        self._build_wizard(root)

    def _build_settings_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("headerActions")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.save_settings_button = QPushButton("保存为…")
        self.save_settings_button.setToolTip(
            "保存购物车与公共参数为 version 5 JSON；添加区未加入的内容不保存。"
            "不会保存 Cookie、Token、二维码、证件、手机号或本机路径。"
        )
        self.save_settings_button.clicked.connect(self._save_settings)
        self.import_settings_button = QPushButton("导入…")
        self.import_settings_button.setToolTip(
            "仅导入 version 5 JSON 购物车配置；旧版配置或未解决的历史范围限制请重新建立购物车。"
        )
        self.import_settings_button.clicked.connect(self._import_settings)
        layout.addWidget(self.save_settings_button)
        layout.addWidget(self.import_settings_button)
        return bar

    def _field_block(
        self,
        key: str,
        label: str,
        widget: QWidget,
        *,
        help_text: str = "",
        page_index: int = 0,
        show_label: bool = True,
    ) -> QWidget:
        """Wrap one editor with a label and its own inline validation text."""

        block = QWidget()
        block.setObjectName("fieldBlock")
        block_layout = QVBoxLayout(block)
        block_layout.setContentsMargins(0, 0, 0, 0)
        block_layout.setSpacing(5)
        if show_label:
            if help_text:
                block_layout.addWidget(HelpLabel(label, help_text))
            else:
                field_label = QLabel(label)
                field_label.setObjectName("fieldLabel")
                block_layout.addWidget(field_label)
        block_layout.addWidget(widget)
        error = QLabel()
        error.setObjectName("validationMessage")
        error.setWordWrap(True)
        error.setVisible(False)
        block_layout.addWidget(error)
        self.field_widgets[key] = widget
        self.field_blocks[key] = block
        self.field_messages[key] = error
        self.field_pages[key] = page_index
        widget.installEventFilter(self)
        for child in widget.findChildren(QWidget):
            child.installEventFilter(self)
        return block

    def _add_field_alias(self, key: str, widget: QWidget, block: QWidget, page_index: int) -> None:
        error = QLabel()
        error.setObjectName("validationMessage")
        error.setWordWrap(True)
        error.setVisible(False)
        assert isinstance(block.layout(), QVBoxLayout)
        block.layout().addWidget(error)
        self.field_widgets[key] = widget
        self.field_blocks[key] = block
        self.field_messages[key] = error
        self.field_pages[key] = page_index
        widget.installEventFilter(self)
        for child in widget.findChildren(QWidget):
            child.installEventFilter(self)

    def _build_basic_page(self) -> QWidget:
        scroll, _page, layout = _scroll_page()
        self.basic_scroll = scroll

        trip = Card("日期与时间")
        trip_grid = QGridLayout()
        trip_grid.setHorizontalSpacing(8)
        trip_grid.setVerticalSpacing(10)
        trip_grid.setColumnStretch(0, 1)
        trip_grid.setColumnStretch(1, 1)
        self.train_date = DatePickerWidget()
        trip_grid.addWidget(
            self._field_block(
                "train_date",
                "乘车日期",
                self.train_date,
            ),
            1,
            0,
            1,
            2,
        )
        self.start_at = TimeFieldsWidget(optional=True, disabled_label="立即开始")
        self.stop_at = TimeFieldsWidget(optional=True, disabled_label="不设停止时间")
        self.start_at.setToolTip("按时、分、秒设置每日开始时间；勾选“立即开始”后不等待。")
        self.stop_at.setToolTip("到达该时间后安全停止查询；勾选“不设停止时间”则由最大查询轮数控制。")
        trip_grid.addWidget(
            self._field_block(
                "start_at",
                "开始 / 开售时间",
                self.start_at,
            ),
            2,
            0,
            1,
            1,
        )
        trip_grid.addWidget(
            self._field_block(
                "stop_at",
                "停止时间",
                self.stop_at,
            ),
            2,
            1,
            1,
            1,
        )
        trip.body.addLayout(trip_grid)
        layout.addWidget(trip)
        layout.addWidget(self._build_cart_entry())

        self.passengers = QLineEdit()
        self.passengers.setPlaceholderText("张三，李四（最多 5 人）")
        self.passengers.setClearButtonEnabled(True)
        self.passengers.setToolTip(
            "自动提交时填写 1–5 个已在当前 12306 账户中的姓名；仅监控可留空。"
            "多个姓名可用中英文逗号、顿号、中英文分号、换行或制表符分隔。"
        )
        manual_block = self._field_block("passenger_names", "", self.passengers, page_index=1, show_label=False)
        self.manual_row.addWidget(manual_block, 1)
        self.passenger_name_label.setBuddy(self.passengers)
        # Compatibility attribute: it now only refreshes contacts and cannot start QR login.
        self.passenger_ticket_types = PassengerTicketEditor()
        self.passenger_card.body.addWidget(self._field_block("passenger_ticket_types", "购票类型", self.passenger_ticket_types, page_index=1))
        self.passenger_ticket_types.changed.connect(lambda: self._schedule_validation("passenger_ticket_types"))
        position = Card("座位与铺位偏好")
        self.preference_note = QLabel("偏好仅在对应席别和服务端支持时生效，不改变车次或席别；无法满足时接受系统分配。")
        self.preference_note.setObjectName("muted")
        self.preference_note.setWordWrap(True)
        position.body.addWidget(self.preference_note)
        self.position_preferences = PositionPreferences()
        self.position_preferences.berths.select_seat_types_requested.connect(self._focus_sleeper_seats)
        position_block = self._field_block(
            "seat_position_preferences",
            "",
            self.position_preferences,
            page_index=2,
            show_label=False,
        )
        self.field_widgets["seat_position_preferences"] = self.position_preferences.seats
        self._add_field_alias("berth_preference", self.position_preferences.berths, position_block, 2)
        position.body.addWidget(position_block)
        self.quiet_carriage = QCheckBox("静音车厢")
        self.quiet_carriage.setToolTip("仅在列车提供静音服务且当前候选为二等座时提交偏好；不保证分配成功。")
        position.body.addWidget(self._field_block("quiet_carriage_preference", "", self.quiet_carriage, page_index=2, show_label=False))
        self.confirm_layout.addWidget(position)

        action = Card("任务模式")
        self.auto_submit = QCheckBox("自动提交订单")
        self.auto_submit.setChecked(True)
        self.auto_submit.setToolTip("关闭后仅查询并提示票源，不提交订单，也不要求填写乘车人。")
        action.body.addWidget(
            self._field_block(
                "auto_submit",
                "",
                self.auto_submit,
                show_label=False,
            )
        )
        layout.addWidget(action)

        self.train_date.changed.connect(lambda: self._schedule_validation("train_date"))
        self.start_at.changed.connect(self._target_edited)
        self.start_at.changed.connect(lambda: self._schedule_validation("start_at"))
        self.stop_at.changed.connect(lambda: self._schedule_validation("stop_at"))
        self.passengers.textChanged.connect(self._passenger_names_changed)
        self.position_preferences.changed.connect(
            lambda: self._schedule_validation("seat_position_preferences", "berth_preference")
        )
        self.auto_submit.toggled.connect(lambda: self._schedule_validation("passenger_names"))
        self.quiet_carriage.toggled.connect(lambda: self._schedule_validation("quiet_carriage_preference"))
        layout.addStretch(1)
        return scroll

    def _build_advanced_page(self) -> QWidget:
        scroll, _page, layout = _scroll_page()
        self.advanced_scroll = scroll
        self.advanced: Dict[str, QWidget] = {}

        query = Card("查询与热身")
        query_grid = QGridLayout()
        query_grid.setSpacing(10)
        query_grid.setColumnStretch(0, 1)
        query_grid.setColumnStretch(1, 1)
        self._add_double(query_grid, "query_interval_seconds", "常规查询间隔", 0.05, 60.0, 0.05, " 秒", 0, 0, "默认 0.60 秒，范围 0.05–60 秒。过低可能触发限流。")
        self._add_int(query_grid, "max_retries", "最大查询轮数", 1, 100000, 0, 1, "默认 1000 轮，范围 1–100000。数值越大，任务可能运行越久。")
        self._add_double(query_grid, "pre_query_seconds", "提前热身", 0.0, 60.0, 0.1, " 秒", 1, 0, "默认 1.50 秒，范围 0–60 秒。在开始时间前进入热身。")
        self._add_double(query_grid, "hot_query_interval_seconds", "热身查询间隔", 0.05, 10.0, 0.05, " 秒", 1, 1, "默认 0.25 秒，范围 0.05–10 秒。过低可能触发限流。")
        self._add_double(query_grid, "hot_window_seconds", "开售后热身窗口", 0.0, 120.0, 0.5, " 秒", 2, 0, "默认 5 秒，范围 0–120 秒。窗口后恢复常规查询间隔。")
        query.body.addLayout(query_grid)
        layout.addWidget(query)

        network = Card("网络、登录与校时")
        network_grid = QGridLayout()
        network_grid.setSpacing(10)
        network_grid.setColumnStretch(0, 1)
        network_grid.setColumnStretch(1, 1)
        self._add_double(network_grid, "request_timeout_seconds", "请求超时", 1.0, 120.0, 1.0, " 秒", 0, 0, "默认 10 秒，范围 1–120 秒。过短会把慢响应误判为失败。")
        self._add_double(network_grid, "login_qr_timeout_seconds", "扫码超时", 30.0, 900.0, 10.0, " 秒", 0, 1, "默认 180 秒，范围 30–900 秒。到期后可手动刷新二维码。")
        self._add_double(network_grid, "login_qr_poll_seconds", "扫码状态间隔", 0.2, 10.0, 0.1, " 秒", 1, 0, "默认 1 秒，范围 0.2–10 秒。过低会增加登录接口请求。")
        self._add_int(network_grid, "time_sync_samples", "校时采样数", 1, 30, 1, 1, "默认 7 次，范围 1–30。更多采样会延长准备阶段。")
        self._add_double(network_grid, "time_sync_max_rtt_seconds", "校时最大 RTT", 0.05, 10.0, 0.05, " 秒", 2, 0, "默认 1 秒，范围 0.05–10 秒。高延迟样本将被丢弃。")
        network.body.addLayout(network_grid)
        layout.addWidget(network)

        order = Card("出票等待与诊断")
        order_grid = QGridLayout()
        order_grid.setSpacing(10)
        order_grid.setColumnStretch(0, 1)
        order_grid.setColumnStretch(1, 1)
        self._add_int(order_grid, "order_wait_attempts", "出票查询次数", 1, 1000, 0, 0, "默认 300 次，范围 1–1000。用于提交后的排队结果查询。")
        self._add_double(order_grid, "order_wait_interval_seconds", "出票查询间隔", 0.1, 60.0, 0.1, " 秒", 0, 1, "默认 2 秒，范围 0.1–60 秒。过低会增加排队接口请求。")
        self._add_int(order_grid, "station_cache_days", "站点缓存天数", 1, 365, 1, 0, "默认 7 天，范围 1–365。仅影响购票任务对缓存新鲜度的判断。")
        self.log_level = QComboBox()
        self.log_level.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        self.advanced["log_level"] = self.log_level
        order_grid.addWidget(
            self._field_block("log_level", "日志级别", self.log_level, help_text="默认 INFO。DEBUG 信息最多，ERROR 只显示错误。", page_index=1),
            1,
            1,
        )
        self.perf_log = QCheckBox("显示关键阶段耗时")
        self.advanced["perf_log"] = self.perf_log
        order_grid.addWidget(
            self._field_block("perf_log", "性能日志", self.perf_log, help_text="默认开启；记录校时、查询、提交等阶段耗时，不改变请求节奏。", page_index=1),
            2,
            0,
        )
        order.body.addLayout(order_grid)
        layout.addWidget(order)

        for grid in (query_grid, network_grid, order_grid):
            fields = [grid.itemAt(index).widget() for index in range(grid.count())]
            for field in fields:
                grid.removeWidget(field)
            for index, field in enumerate(fields):
                grid.addWidget(field, index // 3, index % 3)
            for column in range(3):
                grid.setColumnStretch(column, 1)

        self.log_level.currentTextChanged.connect(lambda: self._schedule_validation("log_level"))
        self.perf_log.toggled.connect(lambda: self._schedule_validation("perf_log"))

        self.reset_advanced_button = QPushButton("恢复高级参数默认值")
        self.reset_advanced_button.clicked.connect(self._reset_advanced)
        layout.addWidget(self.reset_advanced_button, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)
        return scroll

    def _add_double(
        self,
        grid: QGridLayout,
        key: str,
        label: str,
        minimum: float,
        maximum: float,
        step: float,
        suffix: str,
        row: int,
        column: int,
        help_text: str,
    ) -> None:
        spin = CleanDoubleSpinBox()
        spin.setRange(minimum, maximum)
        spin.setDecimals(2)
        spin.setSingleStep(step)
        spin.setSuffix(suffix)
        self.advanced[key] = spin
        spin.valueChanged.connect(lambda _value, field=key: self._schedule_validation(field))
        grid.addWidget(self._field_block(key, label, spin, help_text=help_text, page_index=1), row, column)

    def _add_int(
        self,
        grid: QGridLayout,
        key: str,
        label: str,
        minimum: int,
        maximum: int,
        row: int,
        column: int,
        help_text: str,
    ) -> None:
        spin = CleanSpinBox()
        spin.setRange(minimum, maximum)
        self.advanced[key] = spin
        spin.valueChanged.connect(lambda _value, field=key: self._schedule_validation(field))
        grid.addWidget(self._field_block(key, label, spin, help_text=help_text, page_index=1), row, column)

    def _build_status_panel(self) -> QWidget:
        panel = QWidget()
        self.status_panel = panel
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        status = Card()
        self.phase_badge = QLabel()
        self.phase_badge.setObjectName("phaseBadge")
        self.phase_badge.setWordWrap(True)
        status.body.addWidget(self.phase_badge)
        self.current_cart_item = QLabel()
        self.current_cart_item.setTextFormat(Qt.TextFormat.PlainText)
        self.current_cart_item.setWordWrap(True)
        self.current_cart_item.hide()
        status.body.addWidget(self.current_cart_item)
        self.cart_query_status = QLabel()
        self.cart_query_status.setWordWrap(True)
        self.cart_query_status.setObjectName("muted")
        self.cart_query_status.hide()
        status.body.addWidget(self.cart_query_status)
        self.run_cart_button = QPushButton("查看购物车")
        self.run_cart_button.clicked.connect(self._open_cart)
        self.sale_countdown = QLabel("--:--:--.-")
        self.sale_countdown.setObjectName("saleCountdown")
        self.sale_countdown.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.sale_caption = QLabel("距离开售")
        self.sale_caption.setObjectName("muted")
        self.sale_caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        status.body.addWidget(self.sale_countdown)
        status.body.addWidget(self.sale_caption)
        metrics = QHBoxLayout()
        self.query_metric = self._metric("查询轮数", "0")
        self.rtt_metric = self._metric("RTT", "--")
        self.offset_metric = self._metric("时钟偏移", "--")
        self.query_count = self.query_metric.value_label
        for metric in (self.query_metric, self.rtt_metric, self.offset_metric):
            metrics.addWidget(metric, 1)
        status.body.addLayout(metrics)
        layout.addWidget(status)

        self.recovery_card = Card("重新登录后自动继续")
        self.recovery_layout = self.recovery_card.body
        layout.addWidget(self.recovery_card)
        self.authentication_panel = QWidget()
        authentication = QHBoxLayout(self.authentication_panel)
        authentication.setContentsMargins(0, 0, 0, 0)
        self.qr_image = QLabel("正在准备二维码")
        self.qr_image.setObjectName("qrImage")
        self.qr_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr_image.setFixedSize(160, 160)
        authentication.addWidget(self.qr_image)
        qr_text = QVBoxLayout()
        self.qr_status = QLabel("使用 12306 扫码")
        self.qr_status.setWordWrap(True)
        self.qr_countdown = QLabel("有效期 --:--")
        self.qr_countdown.setObjectName("qrCountdown")
        self.refresh_qr_button = QPushButton("重新扫码")
        self.refresh_qr_button.clicked.connect(self._restart_for_qr)
        qr_text.addWidget(self.qr_status)
        qr_text.addWidget(self.qr_countdown)
        qr_text.addWidget(self.refresh_qr_button, alignment=Qt.AlignmentFlag.AlignLeft)
        authentication.addLayout(qr_text, 1)
        self.account_card.body.addWidget(self.authentication_panel)

        self.login_button = QPushButton("扫码登录 12306")
        self.login_button.setObjectName("primaryButton")
        self.login_button.clicked.connect(lambda: self._start_connection_operation("login"))
        self.account_actions.addWidget(self.login_button)
        self.account_actions.addStretch(1)
        controls = QHBoxLayout()
        self.run_check_login_button = QPushButton("检查登录状态")
        self.check_login_button = self.run_check_login_button
        self.run_check_login_button.clicked.connect(lambda: self._start_connection_operation("check_login"))
        self.sync_clock_button = QPushButton("校准时间")
        self.sync_clock_button.clicked.connect(lambda: self._start_connection_operation("sync_clock"))
        for button in (self.run_cart_button, self.run_check_login_button, self.sync_clock_button):
            controls.addWidget(button, 1)
        status.body.addLayout(controls)
        self.connection_status = QLabel()
        self.connection_status.setObjectName("muted")
        self.connection_status.setWordWrap(True)
        self.connection_status.hide()
        status.body.addWidget(self.connection_status)
        self.validate_button = QPushButton("检查配置", self)
        self.validate_button.hide()
        self.start_button = QPushButton("开始任务")
        self.start_button.setObjectName("primaryButton")
        self.start_button.clicked.connect(self._start_task)
        self.stop_button = QPushButton("停止任务")
        self.stop_button.setObjectName("dangerButton")
        self.stop_button.clicked.connect(self._stop_or_continue)
        self.order_button = QPushButton("前往 12306 支付")
        self.order_button.setEnabled(False)
        self.order_button.clicked.connect(self._open_order_page)
        return panel

    @staticmethod
    def _metric(name: str, value: str) -> QFrame:
        frame = QFrame()
        frame.setObjectName("metric")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)
        title = QLabel(name)
        title.setObjectName("metricName")
        number = QLabel(value)
        number.setObjectName("metricValue")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        number.setAlignment(Qt.AlignmentFlag.AlignCenter)
        number.setAccessibleName(name)
        layout.addWidget(title)
        layout.addWidget(number)
        frame.value_label = number
        return frame

    # ----- JSON settings and configuration -------------------------------
    def _load_initial_values(self) -> None:
        self._apply_mapping(DEFAULT_VALUES)
        self.from_station.setText("北京西")
        self.to_station.setText("郑州东")
        self._cart_draft_baseline = self._cart_draft_signature()

    def _save_settings(self) -> None:
        name, _filter = QFileDialog.getSaveFileName(
            self,
            "保存全部界面参数",
            "我的行程.json",
            "JSON 配置 (*.json)",
        )
        if not name:
            return
        path = Path(name)
        if path.suffix.lower() != ".json":
            path = path.with_suffix(".json")
        try:
            save_gui_settings(path, self._collect_mapping())
        except (AppError, OSError, TypeError, ValueError) as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        logging.info("已保存 version 5 JSON 配置: %s", path)
        QMessageBox.information(self, "保存成功", "已保存购物车与公共参数；添加区未加入的内容不保存。文件不含登录态或身份信息。")

    def _import_settings(self) -> None:
        name, _filter = QFileDialog.getOpenFileName(self, "导入界面参数", "", "JSON 配置 (*.json)")
        if not name:
            return
        try:
            values = load_gui_settings(Path(name))
            import_errors = self._apply_mapping(values)
        except (AppError, OSError, TypeError, ValueError) as exc:
            QMessageBox.warning(self, "导入失败", str(exc))
            return
        errors = self._validate_all(extra_errors=import_errors)
        logging.info("已导入 JSON 配置: %s", name)
        if errors:
            self._focus_first_error(errors)
            QMessageBox.warning(self, "配置已导入", f"已导入，但有 {len(errors)} 项需要修改；已标出第一个问题。")
        else:
            QMessageBox.information(self, "导入成功", "全部可编辑参数已载入并通过字段检查。")

    def _collect_mapping(self) -> Dict[str, Any]:
        values: Dict[str, Any] = {
            "cart_items": deepcopy(self.cart_items),
            "train_date": self.train_date.date().toString("yyyy-MM-dd"),
            "passenger_names": _split_names(self.passengers.text()),
            "passenger_ticket_types": self.passenger_ticket_types.values(),
            "quiet_carriage_preference": self.quiet_carriage.isChecked(),
            "start_at": self.start_at.text().strip(),
            "stop_at": self.stop_at.text().strip(),
            "auto_submit": self.auto_submit.isChecked(),
            "seat_position_preferences": self.position_preferences.seats.positions(),
            "berth_preference": self.position_preferences.berths.values(),
            "persist_session": False,
            "purpose_codes": "ADULT",
            "session_file": str(LOCAL_DATA_DIR / "session.cookies"),
            "station_cache_file": str(LOCAL_DATA_DIR / "stations.json"),
            "qr_code_file": str(LOCAL_DATA_DIR / "login_qr.png"),
        }
        for key, widget in self.advanced.items():
            if isinstance(widget, QDoubleSpinBox):
                values[key] = widget.value()
            elif isinstance(widget, QSpinBox):
                values[key] = widget.value()
            elif isinstance(widget, QComboBox):
                values[key] = widget.currentText()
            elif isinstance(widget, QCheckBox):
                values[key] = widget.isChecked()
        return values

    def _apply_mapping(self, raw_values: Mapping[str, Any]) -> Dict[str, str]:
        values = canonical_mapping(raw_values)
        import_errors: Dict[str, str] = {}
        self._applying_values = True
        try:
            self.cart_items = deepcopy(values["cart_items"])
            first = self.cart_items[0] if self.cart_items else values
            self.from_station.setText(str(first.get("from_station", "")))
            self.to_station.setText(str(first.get("to_station", "")))
            self.train_scope.setCurrentIndex(0)
            self.preferred_trains.clear()
            self.cart_seat.set_selected_seats([])
            self._cart_draft_error_shown = False
            self.cart_draft_status.clear()
            self.cart_draft_status.hide()
            self.cart_preview_toggle.setChecked(False)
            raw_date = str(values["train_date"])
            parsed = QDate.fromString(raw_date, "yyyy-MM-dd")
            if raw_date and (not parsed.isValid() or parsed < QDate.currentDate()):
                import_errors["train_date"] = "导入的乘车日期无效或已经过去，请重新选择"
            self.train_date.setDate(parsed if parsed.isValid() and parsed >= QDate.currentDate() else QDate.currentDate())
            self.passengers.setText("，".join(values["passenger_names"]))
            overrides = values["passenger_ticket_types"]
            if set(overrides) - set(values["passenger_names"]):
                import_errors["passenger_ticket_types"] = "导入票种包含未选择的乘车人，请核对并重新设置"
            self.passenger_ticket_types.set_names(values["passenger_names"], overrides)
            self.quiet_carriage.setChecked(values["quiet_carriage_preference"])
            for key, editor in (("start_at", self.start_at), ("stop_at", self.stop_at)):
                raw_time = str(values[key] or "").strip()
                try:
                    editor.setText(raw_time)
                except ValueError:
                    editor.set_disabled(True)
                    import_errors[key] = "导入时间不是有效的 HH:MM:SS，请重新设置"
            self.auto_submit.setChecked(bool(values["auto_submit"]))
            self.position_preferences.seats.set_positions(values["seat_position_preferences"])
            self.position_preferences.berths.set_values(values["berth_preference"])
            for key, widget in self.advanced.items():
                value = values.get(key, DEFAULT_VALUES.get(key))
                if isinstance(widget, QDoubleSpinBox):
                    number = float(value)
                    if number < widget.minimum() or number > widget.maximum():
                        import_errors[key] = f"导入值 {number:g} 超出允许范围 {widget.minimum():g}–{widget.maximum():g}"
                    widget.setValue(number)
                elif isinstance(widget, QSpinBox):
                    number = int(value)
                    if number < widget.minimum() or number > widget.maximum():
                        import_errors[key] = f"导入值 {number} 超出允许范围 {widget.minimum()}–{widget.maximum()}"
                    widget.setValue(number)
                elif isinstance(widget, QComboBox):
                    if widget.findText(str(value)) < 0:
                        import_errors[key] = f"导入值“{value}”不受支持"
                    else:
                        widget.setCurrentText(str(value))
                elif isinstance(widget, QCheckBox):
                    widget.setChecked(bool(value))
            self._cart_changed()
            self._cart_draft_baseline = self._cart_draft_signature()
            self._cart_draft_changed()
            self._target_edited()
        finally:
            self._applying_values = False
        self.contact_selector.set_selected_names(_split_names(self.passengers.text()))
        self._render_workflow()
        self.passenger_ticket_types.set_contact_types({row["name"]: row.get("passenger_type", "") for row in self._contacts})
        if self.current_step == 2:
            self._refresh_confirmation()
        if not self.cart_items and self.current_step in (1, 2):
            self._show_flow_error("购物车为空，请返回第一步添加备选。")
        return import_errors

    def _reset_advanced(self) -> None:
        draft = (self.from_station.text(), self.to_station.text(), self.train_scope.currentIndex(),
                 self.preferred_trains.text(), self.cart_seat.selected_seats(), self._cart_draft_baseline)
        current = self._collect_mapping()
        for key in self.advanced:
            current[key] = DEFAULT_VALUES[key]
        self._apply_mapping(current)
        self.from_station.setText(draft[0])
        self.to_station.setText(draft[1])
        self.train_scope.setCurrentIndex(draft[2])
        self.preferred_trains.setText(draft[3])
        self.cart_seat.set_selected_seats(draft[4])
        self._cart_draft_baseline = draft[5]
        self._cart_draft_changed()
        self._touched_fields.update(self.advanced)
        self._validate_all(full=False)
        logging.info("已恢复高级参数默认值")

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt API
        if watched is getattr(self, "steps", None) and event.type() in {QEvent.Type.Resize, QEvent.Type.Show}:
            self._position_cart_shortcut()
        if event.type() == QEvent.Type.FocusOut:
            key = self._field_key_for_widget(watched)
            if key:
                self._schedule_validation(key)
        return super().eventFilter(watched, event)

    def _field_key_for_widget(self, watched: QObject) -> str:
        """Return the logical field that owns an editor or one of its children."""

        if not isinstance(watched, QWidget):
            return ""
        for key, block in self.field_blocks.items():
            if watched is block or block.isAncestorOf(watched):
                return key
        return ""

    def _schedule_validation(self, *fields: str) -> None:
        if not self._applying_values:
            self._touched_fields.update(str(field) for field in fields if str(field) in self.field_widgets)
            self.validation_timer.start(250)

    def _validate_live(self) -> None:
        self._validate_all(full=False)

    def _validate_all(
        self,
        *,
        focus_first: bool = False,
        extra_errors: Optional[Mapping[str, str]] = None,
        full: bool = True,
    ) -> Dict[str, str]:
        errors = validate_gui_mapping(self._collect_mapping(), self.station_names)
        errors.update(self._contact_errors())
        if extra_errors:
            errors.update({str(key): str(value) for key, value in extra_errors.items()})
        if full:
            self._touched_fields.update(self.field_widgets)
            visible_errors = errors
        else:
            visible_errors = {key: message for key, message in errors.items() if key in self._touched_fields}
        self._apply_validation(visible_errors)
        if focus_first and errors:
            self._focus_first_error(errors)
        return errors

    def _apply_validation(self, errors: Mapping[str, str]) -> None:
        self._last_validation_errors = dict(errors)
        touched_blocks = set(self.field_blocks.values())
        for block in touched_blocks:
            set_validation_state(block)
        for key, widget in self.field_widgets.items():
            message = str(errors.get(key, ""))
            setter = getattr(widget, "set_validation", None)
            if callable(setter):
                setter("error" if message else "", message)
            else:
                set_validation_state(widget, "error" if message else "", message)
            label = self.field_messages[key]
            label.setText(f"⚠ {message}" if message else "")
            label.setVisible(bool(message))
            # Standard editors draw their own precise outline. Composite
            # editors and checkable lists have no matching QSS selector, so
            # their field block is the one and only visible error boundary.
            direct_outline = isinstance(
                widget,
                (QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, DatePickerWidget, TimeFieldsWidget),
            )
            if message and not direct_outline:
                set_validation_state(self.field_blocks[key], "error", message)

    def _focus_first_error(self, errors: Mapping[str, str]) -> None:
        key = next((name for name in errors if name in self.field_widgets), "")
        if not key:
            return
        page_index = self.field_pages.get(key, 0)
        self._go_to_step(page_index)
        if key in self.advanced:
            self.advanced_toggle.setChecked(True)
        block = self.field_blocks[key]
        scroll = (self.basic_scroll, self.passenger_scroll, self.confirm_scroll)[page_index]
        scroll.ensureWidgetVisible(block, 24, 24)
        widget = self.field_widgets[key]
        if key == "seat_position_preferences":
            self.position_preferences.tabs.setCurrentIndex(0)
            focus_target = next(iter(self.position_preferences.seats.buttons.values()))
        elif key == "berth_preference":
            self.position_preferences.tabs.setCurrentIndex(1)
            focus_target = self.position_preferences.berths.spins["lower"]
        elif isinstance(widget, TimeFieldsWidget):
            focus_target = widget.parts[0]
        else:
            focus_target = widget
        QTimer.singleShot(0, focus_target.setFocus)

    def _build_current_config(self) -> AppConfig:
        return build_app_config(self._collect_mapping(), DEFAULT_CONFIG_FILE)

    def _passenger_names_changed(self) -> None:
        if self._applying_values:
            return
        self.passenger_ticket_types.set_names(_split_names(self.passengers.text()))
        self.contact_selector.set_selected_names(_split_names(self.passengers.text()))
        self._render_workflow()
        self._schedule_validation("passenger_names", "passenger_ticket_types", "seat_position_preferences", "berth_preference")

    def _connection_config(self) -> ConnectionConfig:
        keys = (
            "request_timeout_seconds", "login_qr_timeout_seconds", "login_qr_poll_seconds",
            "time_sync_samples", "time_sync_max_rtt_seconds",
        )
        values = {key: self.advanced[key].value() for key in keys}
        values["persist_session"] = False
        return ConnectionConfig.from_mapping(values, base_dir=LOCAL_DATA_DIR)

    @preserve_reading_position
    def _start_connection_operation(self, mode: str) -> None:
        if self._active_operation is not None:
            if (self._operation_mode == "task" and mode in {"login", "check_login", "sync_clock"}
                    and self._maintenance_enabled and not self._maintenance_busy and self.cancel_token):
                if self.cancel_token.request_action(mode):
                    self._maintenance_busy = True
                    self._update_connection_controls()
            return
        try:
            cfg = self._connection_config()
        except (AppError, ValueError, TypeError) as exc:
            QMessageBox.warning(self, "连接参数需要修改", str(exc))
            return
        self.flow_error.clear()
        self.flow_error.hide()
        self._operation_mode = mode
        self._operation_generation += 1
        if mode == "login":
            self._qr_operation = "login"
            self._qr_deadline = 0.0
            self.qr_image.setPixmap(QPixmap())
            self.qr_image.setText("正在准备扫码登录…" if mode == "login" else "正在检查账号登录…")
            self.qr_status.setText("本次只登录并读取乘车人" if mode == "contacts" else "本次只登录，不启动订票")
        self.start_button.setEnabled(False)
        self.validate_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self._set_forms_enabled(False)
        self._set_phase("syncing" if mode == "sync_clock" else "login", {
            "login": "扫码登录", "contacts": "读取账号乘车人", "sync_clock": "正在重新校时", "check_login": "正在检查登录",
        }[mode])
        self._dispatch_operation(mode, cfg)

    def _ensure_executor(self) -> None:
        if self._executor is not None:
            return
        self._background_thread = QThread(self)
        self._executor = OperationWorker()
        self._executor.moveToThread(self._background_thread)
        self.submit_operation.connect(self._executor.execute, Qt.ConnectionType.QueuedConnection)
        self.retire_executor.connect(self._executor.retire_to_thread, Qt.ConnectionType.QueuedConnection)
        self._executor.runtime_event.connect(self._receive_runtime_event, Qt.ConnectionType.QueuedConnection)
        self._executor.finished.connect(self._receive_operation_outcome, Qt.ConnectionType.QueuedConnection)
        self._background_thread.start()

    def _dispatch_operation(self, mode: str, cfg: object) -> None:
        if self._active_operation is not None or self._closing_executor:
            return
        self._ensure_executor()
        self.cancel_token = GuiCancelToken()
        request = OperationRequest(self._operation_generation, mode, cfg, self.cancel_token,
                                   self.shared_session, self.shared_clock)
        self._active_operation = request
        self._update_connection_controls()
        self.submit_operation.emit(request)

    @Slot(int, str, object)
    def _receive_runtime_event(self, generation: int, kind: str, payload: object) -> None:
        self._on_runtime_event(kind, payload, generation=generation)

    @Slot(int, object)
    def _receive_operation_outcome(self, generation: int, outcome: OperationOutcome) -> None:
        if not self._accept_operation_callback(generation, allow_cancelled=True):
            return
        if outcome.mode == "task":
            self._order_state = "success" if self._order_succeeded else outcome.order_state
            if self._order_state not in {"safe", "confirm_sent", "queued", "success", "unknown"}:
                self._order_state = "unknown"
            if self._order_state == "success":
                self._order_succeeded = True
            self._restart_allowed = (bool(self.cancel_token and self.cancel_token.is_cancelled)
                                     and self._order_state == "safe" and outcome.safe_to_restart and not outcome.error)
            if outcome.error:
                self._on_worker_failed(outcome.error, outcome.details, generation=generation)
            else:
                self._on_worker_completed(int(outcome.result), generation=generation)
            if self._order_state != "safe":
                self.order_button.setEnabled(True)
        elif outcome.error:
            self._on_connection_failed(outcome.error, outcome.details, generation=generation)
            checked = self._format_checked_time(None)
            if outcome.mode == "check_login":
                self._session_check_text = f"登录状态：检查失败，暂时无法确认 · {checked}"
                self._refresh_connection_status()
            elif outcome.mode == "sync_clock":
                retained = "保留上次校准" if self._server_anchor else "使用本地时间"
                self._clock_check_text = f"校时状态：失败，{retained} · {checked}"
                self._refresh_connection_status()
        else:
            self._on_connection_completed(outcome.mode, outcome.result, generation=generation)
        self._release_operation(generation=generation)

    @preserve_reading_position
    def _on_connection_completed(self, mode: str, result: object, *, generation: Optional[int] = None) -> None:
        if not self._accept_operation_callback(generation, allow_cancelled=result is None):
            return
        if mode in {"login", "contacts"} and isinstance(result, Mapping):
            if result.get("authenticated") is True:
                self.account_state = "valid"
                self._login_required = False
                contacts = result.get("contacts")
                if isinstance(contacts, list):
                    self._contacts = contacts
                    self._contacts_loaded = True
                    self.contact_selector.set_contacts(contacts, _split_names(self.passengers.text()))
                self._contacts_error = str(result.get("contacts_error") or "")
                if self._contacts_error:
                    self._contacts = []
                    self._contacts_loaded = False
                    self.contact_selector.set_contacts([])
            elif result.get("authenticated") is False:
                self.account_state = "expired"
                self._login_required = True
                self._contacts = []
                self._contacts_loaded = False
                self.contact_selector.set_contacts([])
                self._show_flow_error("登录已失效，请点击“扫码登录 12306”，登录后重新读取联系人。")
        elif mode == "check_login":
            if result is True:
                self.account_state = "valid"
            elif result is False:
                self.account_state = "expired"
        self._operation_message = "操作已停止" if result is None else "操作完成"
        self.passenger_ticket_types.set_contact_types({row["name"]: row.get("passenger_type", "") for row in self._contacts})
        self._render_workflow()

    def _on_connection_failed(self, message: str, details: str, *, generation: Optional[int] = None) -> None:
        if not self._accept_operation_callback(generation):
            return
        self._operation_message = "操作未完成，可重试"
        self._show_flow_error(message)
        logging.error("连接操作失败: %s", message)
        logging.debug("%s", details)
        self._render_workflow()

    def _choose_account_passengers(self, contacts: list[dict[str, str]], generation: Optional[int] = None) -> None:
        if generation is not None and generation != self._operation_generation:
            return
        self._contacts = contacts
        self._contacts_loaded = True
        self.contact_selector.set_contacts(contacts, _split_names(self.passengers.text()))
        self.passenger_ticket_types.set_contact_types({row["name"]: row.get("passenger_type", "") for row in contacts})

    def _update_connection_controls(self) -> None:
        active = self._active_operation is not None
        waiting = active and self._operation_mode == "task" and self._maintenance_enabled and not self._maintenance_busy
        allowed = not active or waiting
        self.check_login_button.setEnabled(allowed)
        self.sync_clock_button.setEnabled(allowed)
        self.refresh_qr_button.setEnabled(allowed and self._qr_deadline <= 0 and (self._login_required or self.qr_countdown.text() == "已过期"))
        self._render_workflow()

    def _accept_operation_callback(self, generation: Optional[int] = None, *, allow_cancelled: bool = False) -> bool:
        if generation is not None and generation != self._operation_generation:
            return False
        return allow_cancelled or self.cancel_token is None or not self.cancel_token.is_cancelled

    def _validate_clicked(self) -> None:
        errors = self._validate_all(focus_first=True)
        if errors:
            first = next(iter(errors.values()))
            QMessageBox.warning(self, "配置需要修改", f"发现 {len(errors)} 项问题。\n\n{first}")
            return
        try:
            cfg = self._build_current_config()
        except (AppError, TypeError, ValueError) as exc:
            QMessageBox.warning(self, "配置需要修改", str(exc))
            return
        QMessageBox.information(
            self,
            "配置有效",
            f"购物车 {len(cfg.cart_items or [])} 项\n{cfg.train_date}\n乘车人 {len(cfg.passenger_names)} 位",
        )

    # ----- Task lifecycle --------------------------------------------------
    def _start_task(self) -> None:
        if self.current_step != 2:
            return
        if self.account_state != "valid":
            self._go_to_step(1)
            self._show_flow_error("请先登录 12306，再确认任务。")
            return
        if self._active_operation is not None:
            return
        if self.station_refresh_worker and self.station_refresh_worker.is_alive():
            QMessageBox.information(self, "站点正在更新", "请等待站点列表更新完成后再开始任务。")
            return
        errors = self._validate_all(focus_first=True)
        if errors:
            first = next(iter(errors.values()))
            QMessageBox.warning(self, "无法启动", f"请先修改标红的 {len(errors)} 项参数。\n\n{first}")
            return
        try:
            cfg = self._build_current_config()
        except (AppError, TypeError, ValueError) as exc:
            QMessageBox.warning(self, "无法启动", str(exc))
            return

        self._launch_task(cfg)

    def _launch_task(self, cfg: AppConfig, *, restarting: bool = False) -> None:
        if self._active_operation is not None or self._closing_executor:
            return
        if self._order_state != "safe":
            self._go_to_step(3)
            self._show_flow_error("请先到 12306 核对已有订单，当前不能再次提交。")
            return
        self._last_run_config = deepcopy(cfg)
        if not restarting:
            self._last_run_stop_deadline = None
            if cfg.stop_at:
                now = datetime.fromtimestamp(self._server_now_timestamp())
                self._last_run_stop_deadline = (
                    datetime.combine(now.date(), datetime.strptime(cfg.stop_at, "%H:%M:%S").time())
                    if len(cfg.stop_at) == 8 else datetime.strptime(cfg.stop_at, "%Y-%m-%d %H:%M:%S"))
        self._restart_allowed = False
        self._return_after_stop = False
        self._prepare_for_run(cfg)
        self._dispatch_operation("task", cfg)

    def _stop_or_continue(self) -> None:
        if self._active_operation is not None:
            self._stop_task()
        else:
            self._continue_task()

    def _continue_task(self) -> None:
        if (self._active_operation is not None or self.current_step != 3
                or not self._restart_allowed or self._last_run_config is None):
            return
        cfg = deepcopy(self._last_run_config)
        now = datetime.fromtimestamp(self._server_now_timestamp())
        if datetime.strptime(cfg.train_date, "%Y-%m-%d").date() < now.date():
            self._restart_allowed = False
            self._focus_first_error({"train_date": "乘车日期已过，请修改行程"})
            self._show_flow_error("乘车日期已过，请修改行程后重新开始。")
            return
        if cfg.stop_at:
            stop = self._last_run_stop_deadline
            if stop is None:
                stop = (datetime.combine(now.date(), datetime.strptime(cfg.stop_at, "%H:%M:%S").time())
                        if len(cfg.stop_at) == 8 else datetime.strptime(cfg.stop_at, "%Y-%m-%d %H:%M:%S"))
            if now >= stop:
                self._restart_allowed = False
                self._focus_first_error({"stop_at": "停止时间已过，请修改"})
                self._show_flow_error("原停止时间已过，请修改后重新开始。")
                return
        self._launch_task(cfg, restarting=True)

    def _prepare_for_run(self, cfg: AppConfig) -> None:
        self._operation_generation += 1
        self._operation_mode = "task"
        self._task_state = "running"
        self._go_to_step(3)
        self._qr_operation = "login"
        self._maintenance_enabled = False
        self._maintenance_busy = False
        self._login_required = False
        self._last_phase = "preparing"
        self._task_state = "preparing"
        self._last_candidate_context = None
        self.flow_error.clear()
        self.flow_error.hide()
        self._order_id = ""
        self._order_succeeded = False
        self._success_prompt_shown = False
        self._completion_prompt_shown = False
        self._qr_deadline = 0.0
        self._target_timestamp = None
        self._pending_query_payload = None
        self.query_ui_timer.stop()
        self.qr_image.setPixmap(QPixmap())
        self.qr_image.setText("正在检查登录状态…")
        self.qr_status.setText("登录失效时将显示二维码")
        self.qr_countdown.setText("有效期 --:--")
        self.refresh_qr_button.setEnabled(False)
        self.query_count.setText("0")
        self.current_cart_item.clear()
        self.current_cart_item.hide()
        self.cart_query_status.clear()
        self.cart_query_status.hide()
        self._set_phase("preparing", "正在准备任务")
        self.start_button.setEnabled(False)
        self.validate_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.order_button.setEnabled(False)
        self._set_forms_enabled(False)

        level = getattr(logging, cfg.log_level, logging.INFO)
        try:
            self.log_pipeline.configure_task(
                level,
                cfg.passenger_names,
                LOCAL_DATA_DIR / "logs" / "app.log",
            )
        except RuntimeError as exc:
            logging.warning("无法配置异步日志: %s", exc)

    def _stop_task(self) -> None:
        if not self.cancel_token:
            return
        if self._pending_query_payload:
            self.query_count.setText(str(self._pending_query_payload.get('attempt', '--')))
        self._pending_query_payload = None
        self.query_ui_timer.stop()
        self.cancel_token.cancel()
        self._maintenance_enabled = False
        self._maintenance_busy = True
        self._update_connection_controls()
        self.stop_button.setEnabled(False)
        if self._operation_mode == "task":
            self._set_phase("stopping", "正在等待当前请求返回并安全停止")
        else:
            self._operation_message = "正在取消当前操作…"
        self._render_workflow()
        logging.warning("已发送停止请求，当前网络请求最多需等待超时时间")

    def _on_worker_completed(self, code: int, *, generation: Optional[int] = None) -> None:
        if not self._accept_operation_callback(generation, allow_cancelled=True):
            return
        if self._order_succeeded or self._order_state == "success":
            self._order_succeeded = True
            self._set_phase("success", "出票成功，请尽快支付")
        elif self._order_state != "safe" or code == 0:
            self._order_state = "unknown"
            self._restart_allowed = False
            self.order_button.setEnabled(True)
            self._set_phase("unknown", "未收到明确的出票结果，请到 12306 核对订单。")
        elif code == 130 or bool(self.cancel_token and self.cancel_token.is_cancelled):
            self._set_phase("cancelled", "任务已停止")
        else:
            self._set_phase("no_ticket", "已达停止条件或轮询上限，未确认出票")
            if code == 1:
                message = "已达停止时间或最大查询轮数，本次未出票。"
            else:
                message = f"任务已结束（退出码 {code}），本次未确认出票。"
            if not self._completion_prompt_shown:
                self._completion_prompt_shown = True
                QMessageBox.information(self, "任务结束，未出票", message)

    def _on_worker_failed(self, message: str, details: str, *, generation: Optional[int] = None) -> None:
        if not self._accept_operation_callback(generation, allow_cancelled=True):
            return
        if self._order_succeeded or self._order_state == "success":
            self._set_phase("success", "出票成功，请尽快支付")
        elif self._order_state != "safe":
            self._restart_allowed = False
            self._set_phase("unknown", message)
        else:
            self._set_phase("failed", message)
        self.order_button.setEnabled(True)
        logging.error("任务异常: %s", message)
        logging.debug("%s", details)
        if not self._order_succeeded:
            QMessageBox.critical(self, "订单状态待核对" if self._task_state == "unknown" else "任务失败", message)

    @preserve_reading_position
    def _release_operation(self, *, generation: Optional[int] = None) -> None:
        if not self._accept_operation_callback(generation, allow_cancelled=True):
            return
        was_cancelled = self.cancel_token is not None and self.cancel_token.is_cancelled
        self._pending_query_payload = None
        self.query_ui_timer.stop()
        if was_cancelled and self.account_state != "valid":
            self._qr_deadline = 0.0
            self._login_required = True
            self.qr_image.setPixmap(QPixmap())
            self.qr_image.setText("扫码已取消")
            self.qr_countdown.setText("已取消")
        if self._operation_mode == "task":
            if self._order_succeeded or self._order_state == "success":
                self._set_phase("success", "出票成功，请尽快支付")
            elif self._order_state != "safe":
                self._restart_allowed = False
                if self._last_phase != "unknown":
                    self._set_phase("unknown", "订单可能已提交；停止不会撤销订单。")
            elif was_cancelled and self._last_phase not in {"failed", "no_ticket", "unknown"}:
                self._set_phase("cancelled", "任务已停止")
        self._active_operation = None
        self.cancel_token = None
        self._operation_generation += 1
        self._operation_mode = None
        self._maintenance_enabled = False
        self._maintenance_busy = False
        self._refresh_run_presentation()
        self._set_forms_enabled(True)
        if self._return_after_stop:
            self._return_after_stop = False
            if self._order_state != "safe":
                self._go_to_step(3)
            else:
                self._restart_allowed = False
        self._update_connection_controls()
        if self._close_when_finished:
            self.close()

    def _restart_for_qr(self) -> None:
        # Refreshing authentication never starts or restarts ticket ordering.
        if self._operation_mode == "task":
            self._start_connection_operation("login")
            return
        self._start_connection_operation(self._qr_operation)

    # ----- Runtime events, logs and clocks --------------------------------
    @preserve_reading_position
    def _on_runtime_event(self, kind: str, raw_payload: object, *, generation: Optional[int] = None) -> None:
        payload = dict(raw_payload) if isinstance(raw_payload, Mapping) else {"value": raw_payload}
        message = str(payload.get("message", ""))
        kind = str(kind).lower()
        terminal = kind in {"order_success", "error", "finished"} or (
            kind == "phase" and payload.get("phase") in {"cancelled", "stopped", "failed", "success", "unknown"}
        )
        if not self._accept_operation_callback(generation, allow_cancelled=terminal):
            return
        if self._task_state in {"cancelled", "failed", "success", "no_ticket", "unknown"}:
            if kind in {"query", "candidate", "cart_item", "query_empty", "query_failed", "query_route_mismatch",
                        "order_wait", "queue", "queued", "phase", "finished"}:
                return
            # The same job may still have queued maintenance/auth callbacks.
            # Only an explicitly started independent operation may update
            # these after a task has ended; its new generation is checked above.
            if self._operation_mode in {None, "task"} and kind in {
                "clock_sync", "qr_ready", "qr_status", "maintenance_availability", "session_checked",
            }:
                return
        target = payload.get("target_timestamp")
        if target is not None and self._task_state not in {"cancelled", "failed", "success", "no_ticket", "unknown"}:
            self._target_timestamp = float(target)
        self._update_cart_runtime(payload)
        if kind == "cart_item":
            self.cart_query_status.clear()
            self.cart_query_status.hide()
        elif kind in {"query_failed", "query_empty", "query_route_mismatch"}:
            self.cart_query_status.setText(message)
            self.cart_query_status.setVisible(bool(message))
        elif kind == "phase":
            self._set_phase(str(payload.get("phase", "preparing")), message)
        elif kind == "maintenance_availability":
            self._maintenance_enabled = bool(payload.get("enabled"))
            self._maintenance_busy = bool(payload.get("busy"))
            self._login_required = bool(payload.get("login_required"))
            self._update_connection_controls()
        elif kind == "session_checked":
            state = str(payload.get("state", "failed"))
            checked = self._format_checked_time(payload.get("checked_at"))
            descriptions = {"valid": "检查时已登录", "expired": "已失效，请扫码", "failed": "检查失败，暂时无法确认"}
            self._session_check_text = f"登录状态：{descriptions.get(state, descriptions['failed'])} · {checked}"
            if state == "expired":
                self.account_state = "expired"
                self._contacts = []
                self._contacts_loaded = False
                self.contact_selector.set_contacts([])
                self.passenger_ticket_types.set_contact_types({})
                self._login_required = True
                self._qr_deadline = 0.0
                self._qr_operation = "login"
                self.qr_image.setPixmap(QPixmap())
                self.qr_image.setText("登录已失效\n请扫码登录")
                self.qr_status.setText("等待扫码恢复" if self._operation_mode == "task" else "点击“扫码登录”可重新登录，不会启动订票")
            elif state == "valid":
                self.account_state = "valid"
                self._login_required = False
                self._qr_deadline = 0.0
                self.qr_image.setPixmap(QPixmap())
                self.qr_image.setText("✓\n已登录\n无需重新扫码")
                self.qr_countdown.setText("会话有效")
                self.qr_status.setText("检查时登录会话有效，无需重新扫码")
            self._refresh_connection_status()
            self._update_connection_controls()
        elif kind == "clock_sync":
            success = payload.get("success", True) is True
            checked = self._format_checked_time(payload.get("checked_at"))
            clock_state = "成功" if success else ("失败，保留上次校准" if self._server_anchor else "失败，使用本地时间")
            self._clock_check_text = f"校时状态：{clock_state} · {checked}"
            if self._operation_mode == "task" and payload.get("source") == "manual":
                self._set_phase("waiting", "等待热身查询窗口")
            elif self._operation_mode not in {None, "check_login", "contacts", "login"}:
                self._set_phase("syncing", message)
            offset = float(payload.get("offset_seconds", 0.0) or 0.0)
            rtt = payload.get("rtt_ms")
            server_timestamp = payload.get("server_timestamp")
            monotonic_timestamp = payload.get("monotonic_timestamp")
            if success and server_timestamp is not None and monotonic_timestamp is not None:
                self._server_anchor = (float(server_timestamp), float(monotonic_timestamp))
            if success:
                self._offset_value = f"{offset:+.3f}s"
                self._rtt_value = "--" if rtt is None else f"{float(rtt):.0f}ms"
            self._refresh_connection_status()
        elif kind == "qr_ready":
            self.account_state = "expired"
            self._login_required = True
            self._set_phase("login", message)
            self._show_qr(payload.get("image_bytes") or payload.get("image"))
            self._qr_deadline = float(payload.get("expires_at", 0.0) or 0.0)
            self.qr_status.setText(message or "等待扫码")
            self.refresh_qr_button.setEnabled(False)
            self._maintenance_busy = True
            self._update_connection_controls()
        elif kind == "qr_status":
            status = str(payload.get("status", ""))
            self.qr_status.setText(message or status)
            if status in {"confirmed", "success", "logged_in"}:
                self.account_state = "valid"
                self._login_required = False
                self._session_check_text = f"登录状态：有效 · {self._format_checked_time(payload.get('checked_at'))}"
                self._refresh_connection_status()
                self._qr_deadline = 0.0
                self.qr_image.setPixmap(QPixmap())
                self.refresh_qr_button.setEnabled(False)
                if status == "logged_in":
                    self.qr_image.setText("✓\n已登录\n无需重新扫码")
                    self.qr_countdown.setText("会话有效")
                else:
                    self.qr_image.setText("✓\n登录成功")
                    self.qr_countdown.setText("已确认")
            elif status in {"expired", "timeout"}:
                self._login_required = True
                self._qr_deadline = 0.0
                self.qr_image.setPixmap(QPixmap())
                self.qr_image.setText("二维码已过期")
                self.qr_countdown.setText("已过期")
                self.refresh_qr_button.setEnabled(True)
                if self._active_operation is not None:
                    self._update_connection_controls()
        elif kind == "query":
            # Query events can arrive much faster than a screen can repaint.
            # Keep the newest metrics and render at most once per 100 ms;
            # candidate/order/result events below remain immediate.
            self._pending_query_payload = payload
            if not self.query_ui_timer.isActive():
                self.query_ui_timer.start()
        elif kind == "candidate":
            self.cart_query_status.clear()
            self.cart_query_status.hide()
            self._set_phase("querying", message)
        elif kind in {"order_wait", "queue", "queued"}:
            self._set_phase("queued", message)
        elif kind == "order_success":
            first_success = not self._order_succeeded
            self._order_succeeded = True
            self._order_state = "success"
            self._restart_allowed = False
            self._order_id = str(payload.get("order_id", ""))
            self.order_button.setEnabled(True)
            self._set_phase("success", message or "抢票成功")
            if first_success and not self._success_prompt_shown:
                self._success_prompt_shown = True
                details = "订单已提交，请尽快前往 12306 完成支付。"
                if payload.get("cart_index"):
                    details += "\n\n" + self.current_cart_item.text()
                if self._order_id:
                    details += f"\n\n订单号：{self._order_id}"
                QMessageBox.information(self, "出票成功", details)
        elif kind == "error":
            self._set_phase("unknown" if self._order_state != "safe" and not self._order_succeeded else "failed", message)
        elif kind == "finished" and int(payload.get("exit_code", 1) or 0) == 130:
            self._set_phase("cancelled", message)

        self._render_workflow()
        self._refresh_run_presentation()

    @staticmethod
    def _format_checked_time(value: object) -> str:
        try:
            timestamp = float(value) if value is not None else time.time()
            return datetime.fromtimestamp(timestamp).strftime("%H:%M:%S")
        except (TypeError, ValueError, OverflowError, OSError):
            return str(value)[:19]

    def _flush_query_event(self) -> None:
        payload = self._pending_query_payload
        self._pending_query_payload = None
        if not payload or self._task_state in {"cancelled", "failed", "success", "no_ticket", "unknown", "stopping"}:
            return
        self._set_phase("querying", str(payload.get("message", "")))
        self.query_count.setText(str(payload.get('attempt', '--')))

    def _show_qr(self, image: object) -> None:
        pixmap = QPixmap()
        if isinstance(image, (bytes, bytearray, memoryview)):
            pixmap.loadFromData(bytes(image))
        elif isinstance(image, (str, Path)):
            pixmap.load(str(image))
        if pixmap.isNull():
            self.qr_image.setText("二维码图片无法解析")
            return
        self.qr_image.setText("")
        self.qr_image.setPixmap(
            pixmap.scaled(
                148,
                148,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )
        )

    def _set_phase(self, phase: str, message: str = "") -> None:
        if self._operation_mode not in {None, "task"}:
            self._operation_message = message
            self._render_workflow()
            return
        phase = {"stopped": "no_ticket", "clock": "syncing", "queueing": "queued", "stations": "preparing"}.get(phase, phase)
        if self._order_succeeded or self._order_state == "success":
            phase = "success"
        elif phase in {"cancelled", "failed", "no_ticket"} and self._order_state != "safe":
            phase = "unknown"
        if phase in {"cancelled", "failed", "success", "no_ticket", "unknown"}:
            if self._pending_query_payload:
                self.query_count.setText(str(self._pending_query_payload.get('attempt', '--')))
            self._pending_query_payload = None
            self.query_ui_timer.stop()
            self._target_timestamp = None
            if phase in {"success", "unknown"}:
                self._restart_allowed = False
                self.order_button.setEnabled(True)
            if phase == "unknown":
                self._order_state = "unknown"
        elif phase == "queued":
            self._order_state = "queued"
        self._last_phase = phase
        self._task_state = phase
        titles = {
            "idle": "任务未开始", "running": "正在准备", "preparing": "正在准备", "syncing": "正在校时",
            "login": "等待登录" if self._login_required else "检查登录", "waiting": "等待开售",
            "querying": "查询余票", "submitting": "提交订单", "queued": "排队出票",
            "stopping": "正在停止…", "cancelled": "已停止", "failed": "任务失败",
            "success": "出票成功", "no_ticket": "结束未出票", "unknown": "结果待核对",
        }
        title = titles.get(phase, "正在处理")
        if phase == "failed" and message:
            title += f" · {message}"
        elif phase == "unknown":
            title += " · 请前往 12306 核对订单"
            if message and "请到 12306 核对" not in message:
                title += f"\n{message}"
        self.phase_badge.setText(title)
        if self.phase_badge.property("phaseState") != phase:
            self.phase_badge.setProperty("phaseState", phase)
            self.phase_badge.style().unpolish(self.phase_badge)
            self.phase_badge.style().polish(self.phase_badge)
            self.phase_badge.update()
        if phase in {"failed", "unknown"}:
            self.flow_error.clear()
            self.flow_error.hide()
        self.phase_badge.setToolTip(message)
        self._refresh_run_presentation()
        self._render_workflow()

    def _refresh_connection_status(self) -> None:
        lines = [text for text in (self._session_check_text, self._clock_check_text) if text]
        self.connection_status.setText("\n".join(lines))
        self.connection_status.setVisible(bool(lines))
        self.rtt_metric.value_label.setText(self._rtt_value)
        self.offset_metric.value_label.setText(self._offset_value)

    def _refresh_run_presentation(self) -> None:
        terminal = self._task_state in {"cancelled", "failed", "success", "no_ticket", "unknown"}
        if terminal or self._task_state in {"idle", "preparing", "stopping"}:
            self.cart_query_status.clear()
            self.cart_query_status.hide()
        if self._last_candidate_context:
            self._update_cart_runtime(self._last_candidate_context)
        else:
            self.current_cart_item.clear()
            self.current_cart_item.hide()
        self._update_countdowns()

    def _on_log_batch(self, lines: object) -> None:
        if not isinstance(lines, Iterable) or isinstance(lines, (str, bytes, bytearray)):
            return
        batch: list[tuple[str, str]] = []
        for item in lines:
            if isinstance(item, (tuple, list)) and len(item) == 2:
                batch.append((str(item[0]), str(item[1])))
        self.log_view.append_lines(batch)

    def _on_log_message(self, line: str, level: str) -> None:
        self.log_view.append_line(line, level)

    def _server_now_timestamp(self) -> float:
        if self._server_anchor:
            server_timestamp, monotonic_timestamp = self._server_anchor
            return server_timestamp + (time.perf_counter() - monotonic_timestamp)
        return time.time()

    def _target_edited(self) -> None:
        self._target_timestamp = None
        self._update_countdowns()

    def _resolve_display_target(self) -> Optional[float]:
        if self._target_timestamp is not None:
            return self._target_timestamp
        text = self.start_at.text().strip()
        if not text:
            return None
        now = datetime.fromtimestamp(self._server_now_timestamp())
        for fmt in ("%Y-%m-%d %H:%M:%S", "%H:%M:%S"):
            try:
                parsed = datetime.strptime(text, fmt)
            except ValueError:
                continue
            if fmt == "%H:%M:%S":
                parsed = datetime.combine(now.date(), parsed.time())
            return parsed.timestamp()
        return None

    def _update_countdowns(self) -> None:
        target = self._resolve_display_target() if self._task_state == "waiting" else None
        remaining = target - self._server_now_timestamp() if target is not None else 0
        show_countdown = self.current_step == 3 and self._task_state == "waiting" and remaining > 0
        self.sale_countdown.setVisible(show_countdown)
        self.sale_caption.setVisible(show_countdown)
        if show_countdown:
            self.sale_countdown.setText(self._format_remaining(remaining, tenths=True))
            self.sale_caption.setText("距离开售")
        else:
            self.sale_countdown.clear()
            self.sale_caption.clear()
        if self._qr_deadline > 0:
            remaining = self._qr_deadline - time.time()
            self.qr_countdown.setText(f"有效期 {self._format_remaining(remaining)}")
            if remaining <= 0:
                self._qr_deadline = 0.0
                self.qr_status.setText("二维码已过期")
                self._update_connection_controls()

    @staticmethod
    def _format_remaining(seconds: float, tenths: bool = False) -> str:
        if seconds <= 0:
            return "00:00:00.0" if tenths else "00:00"
        total_tenths = int(seconds * 10)
        total = total_tenths // 10
        hours, remainder = divmod(total, 3600)
        minutes, secs = divmod(remainder, 60)
        if tenths:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}.{total_tenths % 10}"
        return f"{minutes + hours * 60:02d}:{secs:02d}"

    # ----- Miscellaneous ---------------------------------------------------
    def _setup_tray(self) -> None:
        self.tray: Optional[QSystemTrayIcon] = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        icon = self.windowIcon()
        if icon.isNull():
            icon = self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip("12306 Fair Ticket")
        self.tray.show()

    def _install_station_completers(self) -> None:
        names = sorted(self.station_names)
        for field in (self.from_station, self.to_station):
            completer = QCompleter(names, field)
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            completer.setFilterMode(Qt.MatchFlag.MatchContains)
            completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
            completer.setMaxVisibleItems(12)
            field.setCompleter(completer)

    def _refresh_stations(self) -> None:
        if self._active_operation is not None:
            return
        if self.station_refresh_worker and self.station_refresh_worker.is_alive():
            return
        timeout_widget = self.advanced.get("request_timeout_seconds")
        timeout_seconds = float(timeout_widget.value()) if isinstance(timeout_widget, QDoubleSpinBox) else 10.0
        self.update_stations_button.setEnabled(False)
        self.update_stations_button.setText("更新中…")
        self.start_button.setEnabled(False)
        self.station_refresh_worker = StationRefreshWorker(
            cache_path=STATION_CACHE_FILE,
            timeout_seconds=timeout_seconds,
            callback=lambda stations, error: self.station_refresh_relay.completed.emit(stations, error),
        )
        logging.info("正在手动更新站点列表")
        self.station_refresh_worker.start()

    def _on_station_refresh_finished(self, stations: object, error: object) -> None:
        self.station_refresh_worker = None
        self.update_stations_button.setText("更新站点")
        task_running = bool(self._active_operation is not None)
        self.update_stations_button.setEnabled(not task_running)
        self.start_button.setEnabled(not task_running)
        if error is not None:
            message = str(error)
            logging.warning("站点列表更新失败，继续使用现有数据: %s", message)
            QMessageBox.warning(self, "更新站点失败", f"现有站点数据未改变。\n\n{message}")
            return
        if isinstance(stations, Mapping):
            self.station_names.update(str(name).strip() for name in stations if str(name).strip())
        else:
            self.station_names = set(cached_station_names())
        self._install_station_completers()
        # Refresh only already-visible station validation. A manual cache
        # update must not make untouched passenger/train drafts turn red.
        self._validate_all(full=False)
        logging.info("站点列表已更新，共 %d 个站名", len(self.station_names))
        QMessageBox.information(self, "站点已更新", f"已载入 {len(self.station_names)} 个站名。")

    def _focus_sleeper_seats(self) -> None:
        self._go_to_step(0)
        self.cart_seat.group_toggles["卧铺"].setChecked(True)
        target = self.cart_seat.checkboxes["硬卧"]
        self.basic_scroll.ensureWidgetVisible(target, 24, 24)
        target.setFocus()
        self.cart_draft_status.setText("请勾选可接受的卧铺席别，并加入购物车。")
        self.cart_draft_status.show()

    def _swap_stations(self) -> None:
        left, right = self.from_station.text(), self.to_station.text()
        self.from_station.setText(right)
        self.to_station.setText(left)

    @preserve_reading_position
    def _set_forms_enabled(self, enabled: bool) -> None:
        # Keep stop/status controls active while preventing mid-run mutation.
        for widget in (
            self.from_station,
            self.to_station,
            self.swap_stations_button,
            self.train_date,
            self.passengers,
            self.passenger_ticket_types,
            self.contact_selector,
            self.quiet_carriage,
            self.refresh_contacts_button,
            self.preferred_trains,
            self.train_scope,
            self.cart_seat,
            self.add_cart_button,
            self.start_at,
            self.stop_at,
            self.position_preferences,
            self.auto_submit,
            self.save_settings_button,
            self.import_settings_button,
    ):
            widget.setEnabled(enabled)
        for widget in self.advanced.values():
            widget.setEnabled(enabled)
        self.update_stations_button.setEnabled(
            enabled and not bool(self.station_refresh_worker and self.station_refresh_worker.is_alive())
        )
        self.reset_advanced_button.setEnabled(enabled)
        if enabled:
            self._cart_draft_changed()

    def _open_order_page(self) -> None:
        QDesktopServices.openUrl(QUrl(ORDER_URL))

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        if hasattr(self, "log_view"):
            self.log_view.set_compact_rows(2 if self.height() < 650 else 3)
            root = self.centralWidget().layout()
            compact = self.height() < 650
            root.setContentsMargins(12 if compact else 22, 8 if compact else 18,
                                   12 if compact else 22, 8 if compact else 20)
            root.setSpacing(8 if compact else 14)
            short = self.height() < 500
            if getattr(self, "_compact_chrome", None) != short:
                self._compact_chrome = short
                self._header_logo.setVisible(not short)
                self._header_subtitle.setVisible(not short)
                for button in self.step_buttons:
                    button.setStyleSheet("padding: 4px 4px;" if short else "")
                self.workflow_status.setStyleSheet("padding: 4px 12px;" if short else "")

    def _poll_executor_shutdown(self) -> None:
        thread = self._background_thread
        if thread is not None and not thread.wait(0):
            QTimer.singleShot(25, self._poll_executor_shutdown)
            return
        if thread is not None:
            thread.deleteLater()
        if self._executor is not None:
            # The queued retirement slot moved this object back before quit.
            # Joining first also ensures the final Python slot has returned.
            self._executor.setParent(self)
            self._executor.deleteLater()
        self._background_thread = None
        self._executor = None
        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        if self._active_operation is not None:
            if not self._close_when_finished:
                answer = QMessageBox.question(self, "操作仍在进行", "停止当前操作并退出吗？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Yes)
                if answer != QMessageBox.StandardButton.Yes:
                    event.ignore()
                    return
            self._close_when_finished = True
            self._stop_task()
            event.ignore()
            return
        if self._background_thread is not None:
            if not self._closing_executor:
                self._closing_executor = True
                if self._executor is not None:
                    self.retire_executor.emit(self.thread())
                else:
                    self._background_thread.quit()
                QTimer.singleShot(0, self._poll_executor_shutdown)
            event.ignore()
            return
        if not self._session_closed:
            self._session_closed = True
            logging.getLogger().removeHandler(self.log_pipeline.handler)
            self.log_pipeline.close()
            if self.tray:
                self.tray.hide()
            self.shared_session.cookies.clear()
            self.shared_session.close()
        event.accept()


def _load_stylesheet(application: QApplication, dark_override: Optional[bool] = None) -> str:
    is_dark = (
        application.palette().color(QPalette.ColorRole.Window).lightness() < 128
        if dark_override is None
        else dark_override
    )
    try:
        def with_asset_paths(stylesheet: str) -> str:
            for name in ("check.svg", "chevron-down-dark.svg", "chevron-down-light.svg"):
                stylesheet = stylesheet.replace(
                    f"url(assets/{name})", f'url("{(ASSET_DIR / name).as_posix()}")'
                )
            return stylesheet

        base = with_asset_paths(APP_QSS.read_text(encoding="utf-8"))
        if is_dark:
            return base
        light = with_asset_paths((ASSET_DIR / "app_light.qss").read_text(encoding="utf-8"))
        return base + "\n" + light
    except OSError:
        return ""


def run_gui(argv: Optional[list[str]] = None) -> int:
    argv = list(argv or sys.argv)
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--screenshot")
    parser.add_argument("--window-size", choices=("640x480", "900x700", "1000x800", "1200x720", "1260x850"))
    parser.add_argument("--theme", choices=("system", "light", "dark"), default="system")
    options, remaining = parser.parse_known_args(argv[1:])
    qt_argv = [argv[0], *remaining]
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    application = QApplication(qt_argv)
    application.setApplicationName("12306 Fair Ticket")
    application.setApplicationVersion(__version__)
    application.setOrganizationName("Tuan-Space")
    system_dark = application.palette().color(QPalette.ColorRole.Window).lightness() < 128
    is_dark = system_dark if options.theme == "system" else options.theme == "dark"
    application.setProperty("darkTheme", is_dark)
    if APP_ICON.exists():
        application.setWindowIcon(QIcon(str(APP_ICON)))
    application.setStyleSheet(_load_stylesheet(application, is_dark))
    window = MainWindow()
    if options.window_size:
        width, height = (int(part) for part in options.window_size.split("x", 1))
        window.resize(width, height)
    window.show()

    if options.smoke_test or options.screenshot:
        def finish_smoke() -> None:
            if options.screenshot:
                path = Path(options.screenshot).resolve()
                path.parent.mkdir(parents=True, exist_ok=True)
                window.grab().save(str(path))
            window.close()
            application.quit()

        QTimer.singleShot(700, finish_smoke)
    return application.exec()
