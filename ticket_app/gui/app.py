"""Modern single-window PySide6 application."""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

import requests
from PySide6.QtCore import QDate, QEvent, QObject, QThread, QTimer, QUrl, Qt, Signal
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
    QSplitter,
    QStyle,
    QSystemTrayIcon,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ticket_app import __version__
from ticket_app.configuration import AppConfig, AppError, SEAT_SPECS
from ticket_app.input_parsing import split_multi_value_text
from ticket_app.preferences import BERTH_SEAT_TYPES, SEATED_SEAT_TYPES
from ticket_app.train_policy import classify_train_codes, priority_preview, scope_summary

from .compat import (
    DEFAULT_VALUES,
    GuiConfigStore,
    LEGACY_CONFIG_FILE,
    LOCAL_DATA_DIR,
    PROJECT_ROOT,
    STATION_CACHE_FILE,
    build_app_config,
    cached_station_names,
    canonical_mapping,
)
from .station_worker import StationRefreshWorker
from .validation import validate_gui_mapping
from .widgets import (
    Card,
    CleanDoubleSpinBox,
    CleanSpinBox,
    CurrentPhaseWidget,
    DatePickerWidget,
    HelpLabel,
    LogView,
    PositionPreferences,
    GroupedSeatEditor,
    TimeFieldsWidget,
    set_validation_state,
)
from .worker import EventRelay, GuiCancelToken, LogBridge, TicketWorker, create_async_log_pipeline


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


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("12306 Fair Ticket")
        self.resize(1260, 850)
        self.setMinimumSize(1200, 720)
        if APP_ICON.exists():
            self.setWindowIcon(QIcon(str(APP_ICON)))

        self.config_store = GuiConfigStore()
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
        self.thread: Optional[QThread] = None
        self.worker: Optional[TicketWorker] = None
        self.event_relay: Optional[EventRelay] = None
        self.cancel_token: Optional[GuiCancelToken] = None
        self._pending_restart = False
        self._close_when_finished = False
        self._qr_deadline = 0.0
        self._target_timestamp: Optional[float] = None
        self._server_anchor: Optional[tuple[float, float]] = None
        self._order_id = ""
        self._order_succeeded = False
        self._success_prompt_shown = False
        self._completion_prompt_shown = False
        self._last_phase = ""

        self.validation_timer = QTimer(self)
        self.validation_timer.setSingleShot(True)
        self.validation_timer.setInterval(250)
        self.validation_timer.timeout.connect(self._validate_live)

        self.query_ui_timer = QTimer(self)
        self.query_ui_timer.setSingleShot(True)
        self.query_ui_timer.setInterval(100)
        self.query_ui_timer.timeout.connect(self._flush_query_event)
        self._pending_query_payload: Optional[Dict[str, Any]] = None

        self.log_bridge = LogBridge()
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
        logo.setObjectName("logoMark")
        title_box = QVBoxLayout()
        title = QLabel("12306 Fair Ticket")
        title.setObjectName("appTitle")
        subtitle = QLabel(f"v{__version__} · 车次范围、席别顺序与位置偏好")
        subtitle.setObjectName("muted")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header_layout.addWidget(logo)
        header_layout.addLayout(title_box)
        header_layout.addStretch(1)
        root.addWidget(header)

        root.addWidget(self._build_profile_bar())

        vertical = QSplitter(Qt.Orientation.Vertical)
        horizontal = QSplitter(Qt.Orientation.Horizontal)
        horizontal.addWidget(self._build_config_tabs())
        horizontal.addWidget(self._build_status_panel())
        horizontal.setMinimumHeight(360)
        horizontal.setStretchFactor(0, 46)
        horizontal.setStretchFactor(1, 54)
        horizontal.setSizes([530, 620])
        vertical.addWidget(horizontal)

        log_card = Card("运行日志 · 自动脱敏")
        log_card.body.setContentsMargins(16, 12, 16, 12)
        log_card.body.setSpacing(8)
        self.log_view = LogView()
        log_card.body.addWidget(self.log_view)
        vertical.addWidget(log_card)
        vertical.setStretchFactor(0, 4)
        vertical.setStretchFactor(1, 1)
        vertical.setSizes([650, 150])
        root.addWidget(vertical, 1)

    def _build_profile_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("profileBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(8)
        self.save_settings_button = QPushButton("保存为…")
        self.save_settings_button.setToolTip(
            "保存全部可编辑参数为 version 3 JSON；允许保存未完成草稿。"
            "不会保存 Cookie、Token、二维码、证件、手机号或本机路径。"
        )
        self.save_settings_button.clicked.connect(self._save_settings)
        self.import_settings_button = QPushButton("导入…")
        self.import_settings_button.setToolTip(
            "仅导入 JSON，兼容 version 1、version 2 和 version 3；导入后会立即检查并标出问题字段。"
        )
        self.import_settings_button.clicked.connect(self._import_settings)
        layout.addWidget(self.save_settings_button)
        layout.addWidget(self.import_settings_button)
        note = QLabel("仅 JSON · 保存可编辑参数 · 不含登录态和身份信息")
        note.setObjectName("muted")
        note.setToolTip("配置不包含 Cookie、Token、二维码、证件、手机号、缓存内容或本机路径。")
        layout.addWidget(note)
        layout.addStretch(1)
        return bar

    def _build_config_tabs(self) -> QWidget:
        self.config_tabs = QTabWidget()
        self.config_tabs.setObjectName("configTabs")
        self.config_tabs.setMinimumWidth(500)
        self.config_tabs.addTab(self._build_basic_page(), "基础参数")
        self.config_tabs.addTab(self._build_advanced_page(), "高级参数")
        return self.config_tabs

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

        trip = Card("行程与乘车人", "站名需与 12306 显示完全一致；乘车人只填写姓名。")
        trip_grid = QGridLayout()
        trip_grid.setHorizontalSpacing(8)
        trip_grid.setVerticalSpacing(10)
        trip_grid.setColumnStretch(0, 1)
        trip_grid.setColumnStretch(2, 1)
        self.from_station = QLineEdit()
        self.from_station.setPlaceholderText("出发站")
        self.from_station.setClearButtonEnabled(True)
        self.to_station = QLineEdit()
        self.to_station.setPlaceholderText("到达站")
        self.to_station.setClearButtonEnabled(True)
        self.swap_stations_button = QToolButton()
        self.swap_stations_button.setText("⇄")
        self.swap_stations_button.setFixedWidth(36)
        self.swap_stations_button.setToolTip("交换出发站和到达站")
        self.swap_stations_button.clicked.connect(self._swap_stations)
        self.update_stations_button = QPushButton("更新站点")
        self.update_stations_button.setToolTip("手动从 12306 获取最新站点列表；程序启动时不会自动联网")
        self.update_stations_button.clicked.connect(self._refresh_stations)
        trip_grid.addWidget(self._field_block("from_station", "出发站", self.from_station), 0, 0)
        trip_grid.addWidget(self.swap_stations_button, 0, 1, alignment=Qt.AlignmentFlag.AlignBottom)
        trip_grid.addWidget(self._field_block("to_station", "到达站", self.to_station), 0, 2)
        trip_grid.addWidget(self.update_stations_button, 0, 3, alignment=Qt.AlignmentFlag.AlignBottom)

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
            4,
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
            4,
        )
        trip_grid.addWidget(
            self._field_block(
                "stop_at",
                "停止时间",
                self.stop_at,
            ),
            3,
            0,
            1,
            4,
        )
        trip.body.addLayout(trip_grid)
        layout.addWidget(trip)

        self.passengers = QLineEdit()
        self.passengers.setPlaceholderText("张三，李四（最多 5 人）")
        self.passengers.setClearButtonEnabled(True)
        self.passengers.setToolTip(
            "自动提交时填写 1–5 个已在当前 12306 账户中的姓名；仅监控可留空。"
            "多个姓名可用中英文逗号、顿号、中英文分号、换行或制表符分隔。"
        )
        trip.body.addWidget(self._field_block("passenger_names", "乘车人", self.passengers))
        people = Card("车次范围", "车次留空时不限类型，按已选席别查询；实际尝试范围以下方说明为准。")
        self.preferred_trains = QLineEdit()
        self.preferred_trains.setPlaceholderText("G79, G95（从左到右优先）")
        self.preferred_trains.setClearButtonEnabled(True)
        self.preferred_trains.setToolTip(
            "多个车次可用中英文逗号、顿号、中英文分号、换行或制表符分隔，"
            "车次优先时先按填写顺序尝试；席别优先时，在同一席别内按填写顺序尝试。"
        )
        self.only_preferred = QCheckBox("只尝试上述车次")
        self.only_preferred.setToolTip("勾选后不会尝试优先车次输入框以外的其他车次。")
        train_editor = QWidget()
        train_layout = QVBoxLayout(train_editor)
        train_layout.setContentsMargins(0, 0, 0, 0)
        train_layout.setSpacing(7)
        train_layout.addWidget(self.preferred_trains)
        train_layout.addWidget(self.only_preferred)
        train_block = self._field_block(
            "preferred_trains",
            "优先车次",
            train_editor,
        )
        self.field_widgets["preferred_trains"] = self.preferred_trains
        people.body.addWidget(train_block)
        self.range_summary = QLabel()
        self.range_summary.setWordWrap(True)
        self.range_summary.setObjectName("muted")
        people.body.addWidget(self.range_summary)
        layout.addWidget(people)

        seating = Card("席别与优先顺序", "勾选可接受的席别，再排列顺序；切换车次不会更改已选席别。")
        self.seat_types = GroupedSeatEditor(SEAT_SPECS.keys())
        self.seat_types.changed.connect(self._seat_types_changed)
        seat_block = self._field_block(
            "seat_types",
            "席别优先级",
            self.seat_types,
        )
        self.field_widgets["seat_types"] = self.seat_types.list
        seating.body.addWidget(seat_block)
        self.priority_strategy = QComboBox()
        self.priority_strategy.setObjectName("priorityStrategy")
        self.priority_strategy.addItem("车次优先：先选车次，再选席别", "train_first")
        self.priority_strategy.addItem("席别优先：先选席别，再选车次", "seat_first")
        seating.body.addWidget(self._field_block("priority_strategy", "尝试策略", self.priority_strategy))
        self.order_preview = QLabel()
        self.order_preview.setWordWrap(True)
        self.order_preview.setObjectName("muted")
        seating.body.addWidget(self.order_preview)
        layout.addWidget(seating)

        position = Card("座位与铺位偏好", "偏好只提交一次；若 12306 未开放或无法满足，订单仍保留并由系统分配其他位置。")
        self.position_preferences = PositionPreferences()
        self.position_preferences.berths.select_seat_types_requested.connect(self._focus_sleeper_seats)
        position_block = self._field_block(
            "seat_position_preferences",
            "",
            self.position_preferences,
            show_label=False,
        )
        self.field_widgets["seat_position_preferences"] = self.position_preferences.seats
        self._add_field_alias("berth_preference", self.position_preferences.berths, position_block, 0)
        position.body.addWidget(position_block)
        layout.addWidget(position)

        action = Card("任务模式")
        self.auto_submit = QCheckBox("发现符合条件的票后自动提交订单")
        self.auto_submit.setChecked(True)
        self.auto_submit.setToolTip("关闭后仅查询并提示票源，不提交订单，也不要求填写乘车人。")
        hint = QLabel("结果出来后仍需在 12306 官方渠道手动完成支付。")
        hint.setObjectName("muted")
        action.body.addWidget(
            self._field_block(
                "auto_submit",
                "提交方式",
                self.auto_submit,
            )
        )
        action.body.addWidget(hint)
        layout.addWidget(action)

        self.from_station.textChanged.connect(lambda: self._schedule_validation("from_station", "to_station"))
        self.to_station.textChanged.connect(lambda: self._schedule_validation("to_station", "from_station"))
        self.train_date.changed.connect(lambda: self._schedule_validation("train_date"))
        self.start_at.changed.connect(self._target_edited)
        self.start_at.changed.connect(lambda: self._schedule_validation("start_at"))
        self.stop_at.changed.connect(lambda: self._schedule_validation("stop_at"))
        self.passengers.textChanged.connect(
            lambda: self._schedule_validation("passenger_names", "seat_position_preferences", "berth_preference")
        )
        self.preferred_trains.textChanged.connect(self._train_inputs_changed)
        self.only_preferred.toggled.connect(self._train_inputs_changed)
        self.priority_strategy.currentIndexChanged.connect(self._strategy_changed)
        self.seat_types.changed.connect(
            lambda: self._schedule_validation("seat_types", "seat_position_preferences", "berth_preference")
        )
        self.position_preferences.changed.connect(
            lambda: self._schedule_validation("seat_position_preferences", "berth_preference")
        )
        self.auto_submit.toggled.connect(lambda: self._schedule_validation("passenger_names"))
        layout.addStretch(1)
        return scroll

    def _build_advanced_page(self) -> QWidget:
        scroll, _page, layout = _scroll_page()
        self.advanced_scroll = scroll
        self.advanced: Dict[str, QWidget] = {}

        query = Card("查询与热身", "频率过高可能导致限流，建议优先使用默认值。")
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
        layout.setContentsMargins(8, 0, 2, 0)
        layout.setSpacing(10)

        status = Card("运行状态与扫码登录")
        status.setMinimumHeight(205)
        status.body.setContentsMargins(16, 12, 16, 12)
        status.body.setSpacing(8)
        overview = QHBoxLayout()
        overview.setSpacing(12)
        status_column = QVBoxLayout()
        status_column.setSpacing(6)
        self.phase_badge = QLabel("就绪")
        self.phase_badge.setObjectName("phaseBadge")
        self.sale_countdown = QLabel("--:--:--.-")
        self.sale_countdown.setObjectName("saleCountdown")
        self.sale_countdown.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.sale_caption = QLabel("设置开始时间后显示倒计时")
        self.sale_caption.setObjectName("muted")
        self.sale_caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        status_column.addWidget(self.phase_badge, alignment=Qt.AlignmentFlag.AlignLeft)
        status_column.addWidget(self.sale_countdown)
        status_column.addWidget(self.sale_caption)
        metrics = QHBoxLayout()
        metrics.setSpacing(6)
        self.query_metric = self._metric("查询", "0")
        self.rtt_metric = self._metric("RTT", "--")
        self.offset_metric = self._metric("时钟偏移", "--")
        metrics.addWidget(self.query_metric)
        metrics.addWidget(self.rtt_metric)
        metrics.addWidget(self.offset_metric)
        status_column.addLayout(metrics)

        self.qr_status = QLabel("未请求二维码")
        self.qr_status.setObjectName("fieldLabel")
        self.qr_status.setWordWrap(True)
        status_column.addWidget(self.qr_status)
        qr_state = QHBoxLayout()
        qr_state.setSpacing(6)
        self.qr_countdown = QLabel("有效期 --:--")
        self.qr_countdown.setObjectName("qrCountdown")
        self.refresh_qr_button = QPushButton("重新扫码")
        self.refresh_qr_button.setEnabled(False)
        self.refresh_qr_button.clicked.connect(self._restart_for_qr)
        qr_state.addWidget(self.qr_countdown)
        qr_state.addStretch(1)
        qr_state.addWidget(self.refresh_qr_button)
        status_column.addLayout(qr_state)

        self.qr_image = QLabel("开始任务后\n在此显示二维码")
        self.qr_image.setObjectName("qrImage")
        self.qr_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr_image.setFixedSize(160, 160)
        overview.addLayout(status_column, 1)
        overview.addWidget(self.qr_image, 0, Qt.AlignmentFlag.AlignTop)
        status.body.addLayout(overview)
        layout.addWidget(status)

        self.timeline = CurrentPhaseWidget()
        layout.addWidget(self.timeline)
        layout.addStretch(1)

        controls = QHBoxLayout()
        self.validate_button = QPushButton("检查配置")
        self.validate_button.clicked.connect(self._validate_clicked)
        self.start_button = QPushButton("开始任务")
        self.start_button.setObjectName("primaryButton")
        self.start_button.clicked.connect(self._start_task)
        self.stop_button = QPushButton("停止")
        self.stop_button.setObjectName("dangerButton")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop_task)
        self.order_button = QPushButton("打开 12306 订单")
        self.order_button.setEnabled(False)
        self.order_button.clicked.connect(self._open_order_page)
        controls.addWidget(self.validate_button)
        controls.addWidget(self.start_button)
        controls.addWidget(self.stop_button)
        controls.addWidget(self.order_button)
        layout.addLayout(controls)
        return panel

    def _metric(self, name: str, value: str) -> QFrame:
        frame = QFrame()
        frame.setObjectName("metric")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(10, 8, 10, 8)
        title = QLabel(name)
        title.setObjectName("metricName")
        number = QLabel(value)
        number.setObjectName("metricValue")
        number.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(number)
        frame.value_label = number  # type: ignore[attr-defined]
        return frame

    # ----- JSON settings and configuration -------------------------------
    def _load_initial_values(self) -> None:
        self._apply_mapping({**DEFAULT_VALUES, "seat_types": [], "seat_position_preferences": [],
                             "berth_preference": {"lower": 0, "middle": 0, "upper": 0},
                             "empty_train_scope": "all", "only_preferred_trains": False})

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
            self.config_store.save_file(path, self._collect_mapping())
        except (AppError, OSError, TypeError, ValueError) as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        logging.info("已保存 version 3 JSON 配置: %s", path)
        QMessageBox.information(self, "保存成功", "已保存全部可编辑参数；文件不包含登录态或身份信息。")

    def _import_settings(self) -> None:
        name, _filter = QFileDialog.getOpenFileName(self, "导入界面参数", "", "JSON 配置 (*.json)")
        if not name:
            return
        try:
            values = self.config_store.import_file(Path(name))
            import_errors, warnings = self._apply_mapping(values)
        except (AppError, OSError, TypeError, ValueError) as exc:
            QMessageBox.warning(self, "导入失败", str(exc))
            return
        errors = self._validate_all(extra_errors=import_errors)
        logging.info("已导入 JSON 配置: %s", name)
        if warnings:
            logging.warning("；".join(warnings))
        migration_note = "\n\n" + "\n".join(warnings) if warnings else ""
        if errors:
            self._focus_first_error(errors)
            QMessageBox.warning(self, "配置已导入", f"已导入，但有 {len(errors)} 项需要修改；已标出第一个问题。" + migration_note)
        else:
            QMessageBox.information(self, "导入成功", "全部可编辑参数已载入并通过字段检查。" + migration_note)

    def _collect_mapping(self) -> Dict[str, Any]:
        values: Dict[str, Any] = {
            "from_station": self.from_station.text().strip(),
            "to_station": self.to_station.text().strip(),
            "train_date": self.train_date.date().toString("yyyy-MM-dd"),
            "passenger_names": _split_names(self.passengers.text()),
            "seat_types": self.seat_types.values(),
            "preferred_trains": [item.upper() for item in _split_names(self.preferred_trains.text())],
            "only_preferred_trains": self.only_preferred.isChecked(),
            "empty_train_scope": "all",
            "priority_strategy": self.priority_strategy.currentData(),
            "start_at": self.start_at.text().strip(),
            "stop_at": self.stop_at.text().strip(),
            "auto_submit": self.auto_submit.isChecked(),
            "seat_position_preferences": self.position_preferences.seats.positions(),
            "berth_preference": self.position_preferences.berths.values(),
            "position_fallback": True,
            "persist_session": False,
            "purpose_codes": "ADULT",
            "session_file": str(LOCAL_DATA_DIR / "session.cookies"),
            "station_cache_file": str(LOCAL_DATA_DIR / "stations.json"),
            "qr_code_file": str(LOCAL_DATA_DIR / "login_qr.png"),
            "config_path": str(LEGACY_CONFIG_FILE),
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

    def _apply_mapping(self, raw_values: Mapping[str, Any]) -> tuple[Dict[str, str], list[str]]:
        values = canonical_mapping(raw_values)
        import_errors: Dict[str, str] = {}
        warnings: list[str] = []
        if values.get("empty_train_scope") != "all":
            warnings.append("已移除“未指定车次时的范围”：旧设置已转换为不限类型，车次留空时按已选席别查询所有列车。")
        self._applying_values = True
        try:
            self.from_station.setText(str(values["from_station"]))
            self.to_station.setText(str(values["to_station"]))
            raw_date = str(values["train_date"])
            parsed = QDate.fromString(raw_date, "yyyy-MM-dd")
            if raw_date and (not parsed.isValid() or parsed < QDate.currentDate()):
                import_errors["train_date"] = "导入的乘车日期无效或已经过去，请重新选择"
            self.train_date.setDate(parsed if parsed.isValid() and parsed >= QDate.currentDate() else QDate.currentDate())
            self.passengers.setText("，".join(values["passenger_names"]))
            self.preferred_trains.setText(", ".join(values["preferred_trains"]))
            self.only_preferred.setChecked(bool(values["only_preferred_trains"]))
            for key, combo in (("priority_strategy", self.priority_strategy),):
                index = combo.findData(values.get(key))
                if index < 0:
                    # Retain invalid imported values as an explicit item so a
                    # later validation cannot silently accept a different rule.
                    combo.addItem(f"无效设置：{values.get(key)}", values.get(key))
                    index = combo.count() - 1
                combo.setCurrentIndex(index)
            for key, editor in (("start_at", self.start_at), ("stop_at", self.stop_at)):
                raw_time = str(values[key] or "").strip()
                if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", raw_time):
                    raw_time = raw_time[-8:]
                    warnings.append(f"{key} 的旧版完整日期时间已转换为每日 {raw_time}")
                try:
                    editor.setText(raw_time)
                except ValueError:
                    editor.set_disabled(True)
                    import_errors[key] = "导入时间不是有效的 HH:MM:SS，请重新设置"
            self.auto_submit.setChecked(bool(values["auto_submit"]))
            requested_seats = list(values["seat_types"])
            unknown_seats = [seat for seat in requested_seats if seat not in SEAT_SPECS]
            self.seat_types.set_values(requested_seats)
            if unknown_seats:
                import_errors["seat_types"] = f"导入配置包含未知席别：{'、'.join(unknown_seats)}"
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
            self._seat_types_changed()
            self._refresh_train_guidance()
            self._target_edited()
        finally:
            self._applying_values = False
        return import_errors, warnings

    def _reset_advanced(self) -> None:
        current = self._collect_mapping()
        for key in self.advanced:
            current[key] = DEFAULT_VALUES[key]
        self._apply_mapping(current)
        self._touched_fields.update(self.advanced)
        self._validate_all(full=False)
        logging.info("已恢复高级参数默认值")

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt API
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
        self.config_tabs.setCurrentIndex(page_index)
        block = self.field_blocks[key]
        scroll = self.basic_scroll if page_index == 0 else self.advanced_scroll
        scroll.ensureWidgetVisible(block, 24, 24)
        widget = self.field_widgets[key]
        if key == "seat_position_preferences":
            self.position_preferences.tabs.setCurrentIndex(0)
            focus_target = next(iter(self.position_preferences.seats.buttons.values()))
        elif key == "berth_preference":
            self.position_preferences.tabs.setCurrentIndex(1)
            focus_target = self.position_preferences.berths.spins["lower"]
        elif key == "seat_types" and not self.seat_types.values():
            classification = classify_train_codes(_split_names(self.preferred_trains.text()))
            ordinary = classification == "conventional"
            self.seat_types.groups["sleeper" if ordinary else "seated"].setChecked(True)
            focus_target = self.seat_types.checkboxes["硬卧" if ordinary else "二等座"]
            scroll.ensureWidgetVisible(focus_target, 24, 24)
        elif isinstance(widget, TimeFieldsWidget):
            focus_target = widget.parts[0]
        else:
            focus_target = widget
        QTimer.singleShot(0, focus_target.setFocus)

    def _build_current_config(self) -> AppConfig:
        return build_app_config(self._collect_mapping(), LEGACY_CONFIG_FILE)

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
            f"{cfg.from_station} → {cfg.to_station}\n{cfg.train_date}\n乘车人 {len(cfg.passenger_names)} 位，席别 {len(cfg.seat_types)} 种",
        )

    # ----- Task lifecycle --------------------------------------------------
    def _start_task(self) -> None:
        if self.thread and self.thread.isRunning():
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

        seat_positions = self.position_preferences.seats.positions()
        berths = self.position_preferences.berths.values()
        codes = {SEAT_SPECS[label].submit_code for label in cfg.seat_types}
        position_parts = []
        if seat_positions and codes.intersection(SEATED_SEAT_TYPES):
            position_parts.append("座位关系 " + "/".join(seat_positions))
        if sum(berths.values()) and codes.intersection(BERTH_SEAT_TYPES):
            position_parts.append(f"下/中/上铺 {berths['lower']}/{berths['middle']}/{berths['upper']}")
        position_text = "；".join(position_parts) or "无启用的偏好"
        summary = (
            f"{cfg.from_station} → {cfg.to_station}   {cfg.train_date}\n"
            f"乘车人：{'、'.join(cfg.passenger_names) or '仅监控'}\n"
            f"优先车次：{', '.join(cfg.preferred_trains) or '未指定'}\n"
            f"{scope_summary(cfg.preferred_trains, cfg.only_preferred_trains, cfg.empty_train_scope)}\n"
            f"席别：{' → '.join(cfg.seat_types)}\n"
            f"策略：{'席别优先' if cfg.priority_strategy == 'seat_first' else '车次优先'}\n"
            f"尝试顺序示例：{priority_preview(cfg.preferred_trains, cfg.seat_types, cfg.priority_strategy, cfg.only_preferred_trains, cfg.empty_train_scope)}\n"
            f"位置：{position_text}（无法满足时自动分配）\n"
            f"模式：{'自动提交' if cfg.auto_submit else '仅监控'}"
        )
        answer = QMessageBox.question(
            self,
            "确认启动任务",
            summary,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        self._prepare_for_run(cfg)
        self.event_relay = EventRelay()
        self.event_relay.runtime_event.connect(self._on_runtime_event)
        self.cancel_token = GuiCancelToken()
        self.thread = QThread(self)
        self.worker = TicketWorker(cfg, self.event_relay, self.cancel_token, self.shared_session)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.completed.connect(self._on_worker_completed)
        self.worker.failed.connect(self._on_worker_failed)
        self.worker.done.connect(self.thread.quit)
        self.worker.done.connect(self.worker.deleteLater)
        self.thread.finished.connect(self._on_thread_finished)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.start()

    def _prepare_for_run(self, cfg: AppConfig) -> None:
        self.timeline.reset_timeline()
        self.timeline.set_phase("preparing", "正在启动")
        self._last_phase = "preparing"
        self._order_id = ""
        self._order_succeeded = False
        self._success_prompt_shown = False
        self._completion_prompt_shown = False
        self._qr_deadline = 0.0
        self._target_timestamp = None
        self._server_anchor = None
        self._pending_query_payload = None
        self.query_ui_timer.stop()
        self.qr_image.setPixmap(QPixmap())
        self.qr_image.setText("正在准备登录…")
        self.qr_status.setText("等待生成二维码")
        self.qr_countdown.setText("有效期 --:--")
        self.refresh_qr_button.setEnabled(False)
        self.query_metric.value_label.setText("0")  # type: ignore[attr-defined]
        self.rtt_metric.value_label.setText("--")  # type: ignore[attr-defined]
        self.offset_metric.value_label.setText("--")  # type: ignore[attr-defined]
        self.phase_badge.setText("正在启动")
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
        self.cancel_token.cancel()
        self.stop_button.setEnabled(False)
        self.phase_badge.setText("正在安全停止…")
        self.timeline.set_phase("cancelled", "等待当前请求返回")
        logging.warning("已发送停止请求，当前网络请求最多需等待超时时间")

    def _on_worker_completed(self, code: int) -> None:
        if self._order_succeeded:
            self._set_phase("success", "出票成功，请尽快支付")
        elif code == 130:
            self._set_phase("cancelled", "任务已停止")
        else:
            self._set_phase("no_ticket", "已达停止条件或轮询上限，未确认出票")
            if code == 1:
                message = "已达停止时间或最大查询轮数，本次未出票。"
            elif code == 0:
                message = "任务已结束，但未收到明确的出票成功事件。请到 12306 订单页核对。"
                self.order_button.setEnabled(True)
            else:
                message = f"任务已结束（退出码 {code}），本次未确认出票。"
            if not self._completion_prompt_shown:
                self._completion_prompt_shown = True
                self._notify("任务结束，未出票", message)
                QMessageBox.information(self, "任务结束，未出票", message)

    def _on_worker_failed(self, message: str, details: str) -> None:
        self._set_phase("failed", message)
        self.order_button.setEnabled(True)
        logging.error("任务异常: %s", message)
        logging.debug("%s", details)
        self._notify("任务失败", message)
        QMessageBox.critical(self, "任务失败", message)

    def _on_thread_finished(self) -> None:
        self.start_button.setEnabled(True)
        self.validate_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self._set_forms_enabled(True)
        self.worker = None
        self.thread = None
        self.event_relay = None
        self.cancel_token = None
        if self._close_when_finished:
            self._close_when_finished = False
            self.close()
            return
        if self._pending_restart:
            self._pending_restart = False
            QTimer.singleShot(0, self._start_task)

    def _restart_for_qr(self) -> None:
        if self.thread and self.thread.isRunning():
            self._pending_restart = True
            self._stop_task()
        else:
            self._start_task()

    # ----- Runtime events, logs and clocks --------------------------------
    def _on_runtime_event(self, kind: str, raw_payload: object) -> None:
        payload = dict(raw_payload) if isinstance(raw_payload, Mapping) else {"value": raw_payload}
        message = str(payload.get("message", ""))
        kind = str(kind).lower()
        if kind == "phase":
            self._set_phase(str(payload.get("phase", "preparing")), message)
        elif kind == "clock_sync":
            self._set_phase("syncing", message)
            offset = float(payload.get("offset_seconds", 0.0) or 0.0)
            rtt = payload.get("rtt_ms")
            server_timestamp = payload.get("server_timestamp")
            monotonic_timestamp = payload.get("monotonic_timestamp")
            if server_timestamp is not None and monotonic_timestamp is not None:
                self._server_anchor = (float(server_timestamp), float(monotonic_timestamp))
            self.offset_metric.value_label.setText(f"{offset:+.3f}s")  # type: ignore[attr-defined]
            self.rtt_metric.value_label.setText("--" if rtt is None else f"{float(rtt):.0f}ms")  # type: ignore[attr-defined]
        elif kind == "qr_ready":
            self._set_phase("login", message)
            self._show_qr(payload.get("image_bytes") or payload.get("image"))
            self._qr_deadline = float(payload.get("expires_at", 0.0) or 0.0)
            self.qr_status.setText(message or "等待扫码")
            self.refresh_qr_button.setEnabled(False)
        elif kind == "qr_status":
            status = str(payload.get("status", ""))
            self.qr_status.setText(message or status)
            if status in {"confirmed", "success", "logged_in"}:
                self._qr_deadline = 0.0
                self.qr_image.setPixmap(QPixmap())
                self.qr_image.setText("✓\n登录成功")
                self.qr_countdown.setText("已确认")
                self._notify("登录成功", "扫码已确认，任务继续运行")
            elif status == "scanned":
                QApplication.beep()
            elif status in {"expired", "timeout"}:
                self._qr_deadline = 0.0
                self.qr_image.setPixmap(QPixmap())
                self.qr_image.setText("二维码已过期")
                self.qr_countdown.setText("已过期")
                self.refresh_qr_button.setEnabled(True)
        elif kind == "query":
            # Query events can arrive much faster than a screen can repaint.
            # Keep the newest metrics and render at most once per 100 ms;
            # candidate/order/result events below remain immediate.
            self._pending_query_payload = payload
            if not self.query_ui_timer.isActive():
                self.query_ui_timer.start()
        elif kind == "candidate":
            self._set_phase("querying", message)
            self.phase_badge.setText(message or "发现候选票")
            self._notify("发现票源", message or "发现符合偏好的候选票", sound=False)
        elif kind in {"order_wait", "queue", "queued"}:
            self._set_phase("queued", message)
        elif kind == "order_success":
            first_success = not self._order_succeeded
            self._order_succeeded = True
            self._order_id = str(payload.get("order_id", ""))
            self.order_button.setEnabled(True)
            self._set_phase("success", message or "抢票成功")
            if first_success:
                self._notify("出票成功", "请尽快前往 12306 完成支付")
            if first_success and not self._success_prompt_shown:
                self._success_prompt_shown = True
                details = "订单已提交，请尽快前往 12306 完成支付。"
                if self._order_id:
                    details += f"\n\n订单号：{self._order_id}"
                QMessageBox.information(self, "出票成功", details)
        elif kind == "error":
            self._set_phase("failed", message)
        elif kind == "finished" and int(payload.get("exit_code", 1) or 0) == 130:
            self._set_phase("cancelled", message)

        target = payload.get("target_timestamp")
        if target is not None:
            self._target_timestamp = float(target)

    def _flush_query_event(self) -> None:
        payload = self._pending_query_payload
        self._pending_query_payload = None
        if not payload:
            return
        self._set_phase("querying", str(payload.get("message", "")))
        self.query_metric.value_label.setText(str(payload.get("attempt", "--")))  # type: ignore[attr-defined]

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
        self._last_phase = phase
        self.timeline.set_phase(phase, message)
        self.phase_badge.setText(message or phase)

    def _on_log_batch(self, lines: object) -> None:
        if not isinstance(lines, Iterable) or isinstance(lines, (str, bytes, bytearray)):
            return
        batch: list[tuple[str, str]] = []
        for item in lines:
            if isinstance(item, (tuple, list)) and len(item) == 2:
                batch.append((str(item[0]), str(item[1])))
        self.log_view.append_lines(batch)
        for line, _level in batch:
            self._update_phase_from_log(line)

    def _on_log_message(self, line: str, level: str) -> None:
        self.log_view.append_line(line, level)
        self._update_phase_from_log(line)

    def _update_phase_from_log(self, line: str) -> None:
        # Compatibility fallback for older cores that only log text.
        if "等待热身查询窗口" in line:
            self._set_phase("waiting", "等待热身查询窗口")
        elif "开始提交订单" in line:
            self._set_phase("submitting", "正在提交订单")
        elif "已提交排队" in line:
            self._set_phase("queued", "等待出票")
        elif "抢票成功" in line:
            self._set_phase("success", "抢票成功")

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
        target = self._resolve_display_target()
        if target is None:
            self.sale_countdown.setText("--:--:--.-")
            self.sale_caption.setText("设置开始时间后显示倒计时")
        else:
            remaining = target - self._server_now_timestamp()
            self.sale_countdown.setText(self._format_remaining(remaining, tenths=True))
            self.sale_caption.setText("距离开始时间" if remaining > 0 else "已到开始时间")
        if self._qr_deadline > 0:
            remaining = self._qr_deadline - time.time()
            self.qr_countdown.setText(f"有效期 {self._format_remaining(remaining)}")
            if remaining <= 0:
                self._qr_deadline = 0.0
                self.qr_status.setText("二维码已过期")
                self.refresh_qr_button.setEnabled(True)

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
        if self.thread and self.thread.isRunning():
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
        task_running = bool(self.thread and self.thread.isRunning())
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

    def _notify(self, title: str, message: str, sound: bool = True) -> None:
        if self.tray:
            self.tray.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, 7000)
        if sound:
            QApplication.beep()

    def _seat_types_changed(self) -> None:
        self.position_preferences.adapt_to_seats(self.seat_types.values())
        self._refresh_order_preview()

    def _train_inputs_changed(self, *_args: object) -> None:
        if self._applying_values:
            return
        if self.sender() is self.preferred_trains and not _split_names(self.preferred_trains.text()):
            self.only_preferred.setChecked(False)
        self._refresh_train_guidance()
        self._schedule_validation("preferred_trains")

    def _strategy_changed(self, *_args: object) -> None:
        if not self._applying_values:
            self._refresh_order_preview()
            self._schedule_validation("priority_strategy")

    def _refresh_train_guidance(self) -> None:
        preferred = _split_names(self.preferred_trains.text())
        # An invalid legacy checked/empty configuration stays repairable: the
        # user may uncheck it, but cannot check it again without entering trains.
        self.only_preferred.setEnabled(bool(preferred) or self.only_preferred.isChecked())
        classification = classify_train_codes(preferred)
        labels = {"high_speed": "高铁/动车", "conventional": "普通列车", "all": "高铁/动车与普通列车"}
        detected = f"已识别：{labels[classification]}\n" if classification in labels else ""
        self.range_summary.setText(detected + scope_summary(preferred, self.only_preferred.isChecked(), "all"))
        self._refresh_order_preview()

    def _refresh_order_preview(self) -> None:
        self.order_preview.setText(
            "尝试顺序示例\n" + priority_preview(
                _split_names(self.preferred_trains.text()), self.seat_types.values(),
                self.priority_strategy.currentData(), self.only_preferred.isChecked(),
                "all",
            )
        )

    def _focus_seat_types(self) -> None:
        self.config_tabs.setCurrentIndex(0)
        self.basic_scroll.ensureWidgetVisible(self.field_blocks["seat_types"], 24, 24)
        self.seat_types.list.setFocus()

    def _focus_sleeper_seats(self) -> None:
        self.config_tabs.setCurrentIndex(0)
        self.seat_types.focus_sleeper_group()
        target = self.seat_types.checkboxes["硬卧"]
        self.basic_scroll.ensureWidgetVisible(target, 24, 24)
        QTimer.singleShot(0, target.setFocus)

    def _swap_stations(self) -> None:
        left, right = self.from_station.text(), self.to_station.text()
        self.from_station.setText(right)
        self.to_station.setText(left)

    def _set_forms_enabled(self, enabled: bool) -> None:
        # Keep stop/status controls active while preventing mid-run mutation.
        for widget in (
            self.from_station,
            self.to_station,
            self.swap_stations_button,
            self.train_date,
            self.passengers,
            self.preferred_trains,
            self.only_preferred,
            self.priority_strategy,
            self.start_at,
            self.stop_at,
            self.seat_types,
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
            self._refresh_train_guidance()

    def _open_order_page(self) -> None:
        QDesktopServices.openUrl(QUrl(ORDER_URL))

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        if self.thread and self.thread.isRunning():
            answer = QMessageBox.question(
                self,
                "任务仍在运行",
                "先安全停止任务再退出吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Yes,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._close_when_finished = True
            self._stop_task()
            event.ignore()
            return
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
    parser.add_argument("--window-size", choices=("1200x720", "1260x850"))
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
