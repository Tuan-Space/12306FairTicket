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
from PySide6.QtCore import QDate, QThread, QTimer, QUrl, Qt
from PySide6.QtGui import QCloseEvent, QDesktopServices, QGuiApplication, QIcon, QPalette, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCompleter,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
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
    QInputDialog,
)

from ticket_app.configuration import AppConfig, AppError, SEAT_SPECS
from ticket_app.logging_utils import make_rotating_file_handler

from .compat import (
    DEFAULT_VALUES,
    LEGACY_CONFIG_FILE,
    LOCAL_DATA_DIR,
    PROJECT_ROOT,
    ProfileStore,
    build_app_config,
    cached_station_names,
    canonical_mapping,
    safe_read_legacy_config,
)
from .widgets import Card, LogView, PositionPreferences, PriorityListEditor, TimelineWidget
from .worker import EventRelay, GuiCancelToken, LogBridge, QtLogHandler, TicketWorker


ASSET_DIR = PROJECT_ROOT / "assets"
APP_ICON = ASSET_DIR / "app_icon.svg"
APP_QSS = ASSET_DIR / "app.qss"
ORDER_URL = "https://kyfw.12306.cn/otn/view/train_order.html"


def _split_names(text: str) -> list[str]:
    return [item.strip() for item in re.split(r"[,，;；\n]+", text) if item.strip()]


def _scroll_page() -> tuple[QScrollArea, QWidget, QVBoxLayout]:
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(4, 4, 10, 12)
    layout.setSpacing(14)
    scroll.setWidget(page)
    return scroll, page, layout


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("12306 Fair Ticket")
        self.resize(1260, 850)
        self.setMinimumSize(1200, 720)
        if APP_ICON.exists():
            self.setWindowIcon(QIcon(str(APP_ICON)))

        self.profile_store = ProfileStore()
        # Kept only in memory. Sequential tasks in this application process can
        # reuse a confirmed login without ever writing cookies to disk.
        self.shared_session = requests.Session()
        self.thread: Optional[QThread] = None
        self.worker: Optional[TicketWorker] = None
        self.event_relay: Optional[EventRelay] = None
        self.cancel_token: Optional[GuiCancelToken] = None
        self.file_log_handler: Optional[logging.Handler] = None
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

        self.log_bridge = LogBridge()
        self.gui_log_handler = QtLogHandler(self.log_bridge)
        logging.getLogger().addHandler(self.gui_log_handler)
        logging.getLogger().setLevel(logging.DEBUG)

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
        self.log_bridge.message.connect(self._on_log_message)
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
        subtitle = QLabel("校时、热身、偏好选座，所有运行状态一目了然")
        subtitle.setObjectName("muted")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header_layout.addWidget(logo)
        header_layout.addLayout(title_box)
        header_layout.addStretch(1)
        privacy = QLabel("●  每次启动均重新扫码")
        privacy.setObjectName("privacyBadge")
        privacy.setToolTip("不持久化登录 Cookie，二维码只在当前会话中展示")
        header_layout.addWidget(privacy)
        root.addWidget(header)

        root.addWidget(self._build_profile_bar())

        vertical = QSplitter(Qt.Orientation.Vertical)
        horizontal = QSplitter(Qt.Orientation.Horizontal)
        horizontal.addWidget(self._build_config_tabs())
        horizontal.addWidget(self._build_status_panel())
        horizontal.setMinimumHeight(360)
        horizontal.setStretchFactor(0, 6)
        horizontal.setStretchFactor(1, 5)
        horizontal.setSizes([690, 530])
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
        label = QLabel("配置档案")
        label.setObjectName("fieldLabel")
        self.profile_combo = QComboBox()
        self.profile_combo.setMinimumWidth(190)
        self.profile_combo.currentIndexChanged.connect(self._profile_selected)
        save = QPushButton("保存为…")
        save.clicked.connect(self._save_profile)
        delete = QPushButton("删除")
        delete.clicked.connect(self._delete_profile)
        import_json = QPushButton("导入 JSON")
        import_json.clicked.connect(self._import_profile)
        export_json = QPushButton("导出 JSON")
        export_json.clicked.connect(self._export_profile)
        legacy = QPushButton("安全读取 config.py")
        legacy.clicked.connect(self._import_legacy)
        layout.addWidget(label)
        layout.addWidget(self.profile_combo)
        layout.addWidget(save)
        layout.addWidget(delete)
        layout.addWidget(import_json)
        layout.addWidget(export_json)
        layout.addWidget(legacy)
        layout.addStretch(1)
        return bar

    def _build_config_tabs(self) -> QWidget:
        tabs = QTabWidget()
        tabs.setObjectName("configTabs")
        tabs.addTab(self._build_basic_page(), "基础参数")
        tabs.addTab(self._build_advanced_page(), "高级参数")
        return tabs

    def _build_basic_page(self) -> QWidget:
        scroll, _page, layout = _scroll_page()

        trip = Card("行程与时间", "站名需与 12306 显示完全一致。")
        form = QFormLayout()
        form.setSpacing(10)
        station_row = QHBoxLayout()
        self.from_station = QLineEdit()
        self.from_station.setPlaceholderText("出发站")
        self.from_station.setClearButtonEnabled(True)
        self.to_station = QLineEdit()
        self.to_station.setPlaceholderText("到达站")
        self.to_station.setClearButtonEnabled(True)
        swap = QToolButton()
        swap.setText("⇄")
        swap.setToolTip("交换出发站和到达站")
        swap.clicked.connect(self._swap_stations)
        station_row.addWidget(self.from_station)
        station_row.addWidget(swap)
        station_row.addWidget(self.to_station)
        form.addRow("路线", station_row)
        self.train_date = QDateEdit()
        self.train_date.setCalendarPopup(True)
        self.train_date.setDisplayFormat("yyyy-MM-dd")
        self.train_date.setMinimumDate(QDate.currentDate())
        form.addRow("乘车日期", self.train_date)
        self.start_at = QLineEdit()
        self.start_at.setPlaceholderText("HH:MM:SS 或 YYYY-MM-DD HH:MM:SS")
        self.start_at.textChanged.connect(self._target_edited)
        form.addRow("开始 / 开售时间", self.start_at)
        self.stop_at = QLineEdit()
        self.stop_at.setPlaceholderText("留空表示不按时间停止")
        form.addRow("停止时间", self.stop_at)
        trip.body.addLayout(form)
        layout.addWidget(trip)

        people = Card("乘车人与抢票顺序", "只保存常用乘车人姓名，不存储身份证号和手机号。")
        people_form = QFormLayout()
        people_form.setSpacing(10)
        self.passengers = QLineEdit()
        self.passengers.setPlaceholderText("张三，李四（最多 5 人）")
        self.passengers.setClearButtonEnabled(True)
        people_form.addRow("乘车人", self.passengers)
        self.preferred_trains = QLineEdit()
        self.preferred_trains.setPlaceholderText("G79, G95（从左到右优先）")
        self.preferred_trains.setClearButtonEnabled(True)
        people_form.addRow("优先车次", self.preferred_trains)
        self.only_preferred = QCheckBox("只尝试上述车次")
        people_form.addRow("", self.only_preferred)
        people.body.addLayout(people_form)
        seat_label = QLabel("席别优先级")
        seat_label.setObjectName("fieldLabel")
        people.body.addWidget(seat_label)
        self.seat_types = PriorityListEditor(SEAT_SPECS.keys())
        self.seat_types.changed.connect(self._seat_types_changed)
        people.body.addWidget(self.seat_types)
        layout.addWidget(people)

        position = Card("座位与铺位偏好", "偏好只提交一次；若 12306 未开放或无法满足，订单仍保留并由系统分配其他位置。")
        self.position_preferences = PositionPreferences()
        position.body.addWidget(self.position_preferences)
        layout.addWidget(position)

        action = Card("任务模式")
        self.auto_submit = QCheckBox("发现符合条件的票后自动提交订单")
        self.auto_submit.setChecked(True)
        hint = QLabel("结果出来后仍需在 12306 官方渠道手动完成支付。")
        hint.setObjectName("muted")
        action.body.addWidget(self.auto_submit)
        action.body.addWidget(hint)
        layout.addWidget(action)
        layout.addStretch(1)
        return scroll

    def _build_advanced_page(self) -> QWidget:
        scroll, _page, layout = _scroll_page()
        self.advanced: Dict[str, QWidget] = {}

        query = Card("查询与热身", "频率过高可能导致限流，建议优先使用默认值。")
        query_form = QFormLayout()
        self._add_double(query_form, "query_interval_seconds", "常规查询间隔", 0.05, 60.0, 0.05, " 秒")
        self._add_int(query_form, "max_retries", "最大查询轮数", 1, 100000)
        self._add_double(query_form, "pre_query_seconds", "提前热身", 0.0, 60.0, 0.1, " 秒")
        self._add_double(query_form, "hot_query_interval_seconds", "热身查询间隔", 0.05, 10.0, 0.05, " 秒")
        self._add_double(query_form, "hot_window_seconds", "开售后热身窗口", 0.0, 120.0, 0.5, " 秒")
        query.body.addLayout(query_form)
        layout.addWidget(query)

        network = Card("网络、登录与校时")
        network_form = QFormLayout()
        self._add_double(network_form, "request_timeout_seconds", "请求超时", 1.0, 120.0, 1.0, " 秒")
        self._add_double(network_form, "login_qr_timeout_seconds", "扫码超时", 30.0, 900.0, 10.0, " 秒")
        self._add_double(network_form, "login_qr_poll_seconds", "扫码状态间隔", 0.2, 10.0, 0.1, " 秒")
        self._add_int(network_form, "time_sync_samples", "校时采样数", 1, 30)
        self._add_double(network_form, "time_sync_max_rtt_seconds", "校时最大 RTT", 0.05, 10.0, 0.05, " 秒")
        remember = QCheckBox("记住登录状态")
        remember.setChecked(False)
        remember.setEnabled(False)
        remember.setToolTip("桌面端安全策略固定为每次启动重新扫码")
        network_form.addRow("会话策略", remember)
        network.body.addLayout(network_form)
        layout.addWidget(network)

        order = Card("出票等待与诊断")
        order_form = QFormLayout()
        self._add_int(order_form, "order_wait_attempts", "出票查询次数", 1, 1000)
        self._add_double(order_form, "order_wait_interval_seconds", "出票查询间隔", 0.1, 60.0, 0.1, " 秒")
        self._add_int(order_form, "station_cache_days", "站点缓存天数", 1, 365)
        self.log_level = QComboBox()
        self.log_level.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        self.advanced["log_level"] = self.log_level
        order_form.addRow("日志级别", self.log_level)
        self.perf_log = QCheckBox("显示关键阶段耗时")
        self.advanced["perf_log"] = self.perf_log
        order_form.addRow("性能日志", self.perf_log)
        purpose = QComboBox()
        purpose.addItem("ADULT")
        purpose.setEnabled(False)
        purpose.setToolTip("当前版本只暴露稳定的成人票查询参数")
        order_form.addRow("查询用途", purpose)
        order.body.addLayout(order_form)
        layout.addWidget(order)

        reset = QPushButton("恢复高级参数默认值")
        reset.clicked.connect(self._reset_advanced)
        layout.addWidget(reset, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)
        return scroll

    def _add_double(
        self,
        form: QFormLayout,
        key: str,
        label: str,
        minimum: float,
        maximum: float,
        step: float,
        suffix: str = "",
    ) -> None:
        spin = QDoubleSpinBox()
        spin.setRange(minimum, maximum)
        spin.setDecimals(2)
        spin.setSingleStep(step)
        spin.setSuffix(suffix)
        self.advanced[key] = spin
        form.addRow(label, spin)

    def _add_int(self, form: QFormLayout, key: str, label: str, minimum: int, maximum: int) -> None:
        spin = QSpinBox()
        spin.setRange(minimum, maximum)
        self.advanced[key] = spin
        form.addRow(label, spin)

    def _build_status_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 0, 2, 0)
        layout.setSpacing(10)

        # The status stack has a real minimum height (most importantly the QR
        # code). Put it in its own scroll area so a short window scrolls the
        # timeline instead of clipping the QR, countdown, or metrics. Primary
        # task controls remain fixed below the scroll viewport.
        status_scroll = QScrollArea()
        status_scroll.setWidgetResizable(True)
        status_scroll.setFrameShape(QFrame.Shape.NoFrame)
        status_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        status_content = QWidget()
        status_content.setMinimumHeight(420)
        status_layout = QVBoxLayout(status_content)
        status_layout.setContentsMargins(0, 0, 6, 0)
        status_layout.setSpacing(10)

        status = Card("运行状态与扫码登录")
        status.setMinimumHeight(225)
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
        status_layout.addWidget(status)

        timeline_card = Card("任务阶段")
        timeline_card.setMinimumHeight(175)
        timeline_card.body.setContentsMargins(16, 12, 16, 12)
        timeline_card.body.setSpacing(8)
        self.timeline = TimelineWidget()
        timeline_card.body.addWidget(self.timeline)
        status_layout.addWidget(timeline_card, 1)
        status_scroll.setWidget(status_content)
        layout.addWidget(status_scroll, 1)

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

    # ----- Profiles and configuration -------------------------------------
    def _load_initial_values(self) -> None:
        self._refresh_profile_combo()
        values: Mapping[str, Any]
        last = self.profile_store.last_profile
        stored = self.profile_store.get(last) if last else None
        if stored:
            values = stored
            self._select_profile_name(last)
        else:
            try:
                values = safe_read_legacy_config(LEGACY_CONFIG_FILE)
            except AppError as exc:
                values = DEFAULT_VALUES
                logging.warning("旧 config.py 读取失败: %s", exc)
        self._apply_mapping(values)

    def _refresh_profile_combo(self, selected: str = "") -> None:
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItem("临时配置", "")
        for name in self.profile_store.names():
            self.profile_combo.addItem(name, name)
        self.profile_combo.blockSignals(False)
        self._select_profile_name(selected)

    def _select_profile_name(self, name: str) -> None:
        index = self.profile_combo.findData(name)
        self.profile_combo.setCurrentIndex(max(0, index))

    def _profile_selected(self, _index: int) -> None:
        name = str(self.profile_combo.currentData() or "")
        if not name:
            return
        values = self.profile_store.get(name)
        if values:
            self._apply_mapping(values)
            self.profile_store.mark_last(name)
            logging.info("已加载配置档案: %s", name)

    def _save_profile(self) -> None:
        suggested = str(self.profile_combo.currentData() or "我的行程")
        name, ok = QInputDialog.getText(self, "保存配置档案", "档案名称", text=suggested)
        if not ok:
            return
        try:
            self.profile_store.put(name, self._collect_mapping())
        except (AppError, OSError, ValueError) as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        self._refresh_profile_combo(name.strip())
        logging.info("已保存配置档案: %s", name.strip())

    def _delete_profile(self) -> None:
        name = str(self.profile_combo.currentData() or "")
        if not name:
            return
        answer = QMessageBox.question(self, "删除档案", f"确定删除“{name}”吗？")
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.profile_store.delete(name)
        except OSError as exc:
            QMessageBox.warning(self, "删除失败", str(exc))
            return
        self._refresh_profile_combo()

    def _import_profile(self) -> None:
        name, _filter = QFileDialog.getOpenFileName(self, "导入配置档案", "", "JSON (*.json)")
        if not name:
            return
        try:
            profile_name = self.profile_store.import_file(Path(name))
            values = self.profile_store.get(profile_name)
            if values:
                self._apply_mapping(values)
        except (AppError, OSError, ValueError) as exc:
            QMessageBox.warning(self, "导入失败", str(exc))
            return
        self._refresh_profile_combo(profile_name)

    def _export_profile(self) -> None:
        profile_name = str(self.profile_combo.currentData() or "临时配置")
        name, _filter = QFileDialog.getSaveFileName(self, "导出配置档案", f"{profile_name}.json", "JSON (*.json)")
        if not name:
            return
        try:
            self.profile_store.export_file(Path(name), profile_name, self._collect_mapping())
        except OSError as exc:
            QMessageBox.warning(self, "导出失败", str(exc))

    def _import_legacy(self) -> None:
        name, _filter = QFileDialog.getOpenFileName(self, "安全读取旧 config.py", str(LEGACY_CONFIG_FILE), "Python config (config.py *.py)")
        if not name:
            return
        try:
            values = safe_read_legacy_config(Path(name))
        except AppError as exc:
            QMessageBox.warning(self, "配置读取失败", str(exc))
            return
        self._apply_mapping(values)
        self.profile_combo.setCurrentIndex(0)
        logging.info("已通过 AST 安全读取旧配置（未执行 Python 代码）")

    def _collect_mapping(self) -> Dict[str, Any]:
        values: Dict[str, Any] = {
            "from_station": self.from_station.text().strip(),
            "to_station": self.to_station.text().strip(),
            "train_date": self.train_date.date().toString("yyyy-MM-dd"),
            "passenger_names": _split_names(self.passengers.text()),
            "seat_types": self.seat_types.values(),
            "preferred_trains": [item.upper() for item in _split_names(self.preferred_trains.text())],
            "only_preferred_trains": self.only_preferred.isChecked(),
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

    def _apply_mapping(self, raw_values: Mapping[str, Any]) -> None:
        values = canonical_mapping(raw_values)
        self.from_station.setText(str(values["from_station"]))
        self.to_station.setText(str(values["to_station"]))
        parsed = QDate.fromString(str(values["train_date"]), "yyyy-MM-dd")
        self.train_date.setDate(parsed if parsed.isValid() and parsed >= QDate.currentDate() else QDate.currentDate())
        self.passengers.setText("，".join(values["passenger_names"]))
        self.preferred_trains.setText(", ".join(values["preferred_trains"]))
        self.only_preferred.setChecked(bool(values["only_preferred_trains"]))
        self.start_at.setText(str(values["start_at"]))
        self.stop_at.setText(str(values["stop_at"]))
        self.auto_submit.setChecked(bool(values["auto_submit"]))
        self.seat_types.set_values(values["seat_types"])
        self.position_preferences.seats.set_positions(values["seat_position_preferences"])
        self.position_preferences.berths.set_values(values["berth_preference"])
        for key, widget in self.advanced.items():
            value = values.get(key, DEFAULT_VALUES.get(key))
            if isinstance(widget, QDoubleSpinBox):
                widget.setValue(float(value))
            elif isinstance(widget, QSpinBox):
                widget.setValue(int(value))
            elif isinstance(widget, QComboBox):
                widget.setCurrentText(str(value))
            elif isinstance(widget, QCheckBox):
                widget.setChecked(bool(value))
        self._seat_types_changed()
        self._target_edited()

    def _reset_advanced(self) -> None:
        current = self._collect_mapping()
        for key in self.advanced:
            current[key] = DEFAULT_VALUES[key]
        self._apply_mapping(current)
        logging.info("已恢复高级参数默认值")

    def _build_current_config(self) -> AppConfig:
        return build_app_config(self._collect_mapping(), LEGACY_CONFIG_FILE)

    def _validate_clicked(self) -> None:
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
        try:
            cfg = self._build_current_config()
        except (AppError, TypeError, ValueError) as exc:
            QMessageBox.warning(self, "无法启动", str(exc))
            return

        seat_positions = self.position_preferences.seats.positions()
        berths = self.position_preferences.berths.values()
        position_text = "无指定"
        if seat_positions:
            position_text = "座位关系 " + "/".join(seat_positions)
        elif sum(berths.values()):
            position_text = f"下/中/上铺 {berths['lower']}/{berths['middle']}/{berths['upper']}"
        summary = (
            f"{cfg.from_station} → {cfg.to_station}   {cfg.train_date}\n"
            f"乘车人：{'、'.join(cfg.passenger_names) or '仅监控'}\n"
            f"车次：{', '.join(cfg.preferred_trains) or '不限'}\n"
            f"席别：{' → '.join(cfg.seat_types)}\n"
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
        self.gui_log_handler.setLevel(level)
        self.gui_log_handler.set_sensitive_terms(cfg.passenger_names)
        if self.file_log_handler is not None:
            logging.getLogger().removeHandler(self.file_log_handler)
            self.file_log_handler.close()
            self.file_log_handler = None
        try:
            self.file_log_handler = make_rotating_file_handler(
                LOCAL_DATA_DIR / "logs" / "app.log",
                level,
                sensitive_terms=cfg.passenger_names,
            )
            logging.getLogger().addHandler(self.file_log_handler)
        except OSError as exc:
            logging.warning("无法创建本地轮转日志: %s", exc)

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
            self._set_phase("querying", message)
            self.query_metric.value_label.setText(str(payload.get("attempt", "--")))  # type: ignore[attr-defined]
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

    def _on_log_message(self, line: str, level: str) -> None:
        self.log_view.append_line(line, level)
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
        names = cached_station_names()
        for field in (self.from_station, self.to_station):
            completer = QCompleter(names, field)
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            completer.setFilterMode(Qt.MatchFlag.MatchContains)
            completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
            completer.setMaxVisibleItems(12)
            field.setCompleter(completer)

    def _notify(self, title: str, message: str, sound: bool = True) -> None:
        if self.tray:
            self.tray.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, 7000)
        if sound:
            QApplication.beep()

    def _seat_types_changed(self) -> None:
        self.position_preferences.adapt_to_seats(self.seat_types.values())

    def _swap_stations(self) -> None:
        left, right = self.from_station.text(), self.to_station.text()
        self.from_station.setText(right)
        self.to_station.setText(left)

    def _set_forms_enabled(self, enabled: bool) -> None:
        # Keep stop/status controls active while preventing mid-run mutation.
        for widget in (
            self.from_station,
            self.to_station,
            self.train_date,
            self.passengers,
            self.preferred_trains,
            self.only_preferred,
            self.start_at,
            self.stop_at,
            self.seat_types,
            self.position_preferences,
            self.auto_submit,
            self.profile_combo,
        ):
            widget.setEnabled(enabled)
        for widget in self.advanced.values():
            widget.setEnabled(enabled)

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
        logging.getLogger().removeHandler(self.gui_log_handler)
        if self.file_log_handler:
            logging.getLogger().removeHandler(self.file_log_handler)
            self.file_log_handler.close()
            self.file_log_handler = None
        if self.tray:
            self.tray.hide()
        self.shared_session.cookies.clear()
        self.shared_session.close()
        event.accept()


def _load_stylesheet(application: QApplication) -> str:
    is_dark = application.palette().color(QPalette.ColorRole.Window).lightness() < 128
    try:
        base = APP_QSS.read_text(encoding="utf-8")
        if is_dark:
            return base
        return base + "\n" + (ASSET_DIR / "app_light.qss").read_text(encoding="utf-8")
    except OSError:
        return ""


def run_gui(argv: Optional[list[str]] = None) -> int:
    argv = list(argv or sys.argv)
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--screenshot")
    options, remaining = parser.parse_known_args(argv[1:])
    qt_argv = [argv[0], *remaining]
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    application = QApplication(qt_argv)
    application.setApplicationName("12306 Fair Ticket")
    application.setOrganizationName("Tuan-Space")
    application.setProperty(
        "darkTheme",
        application.palette().color(QPalette.ColorRole.Window).lightness() < 128,
    )
    if APP_ICON.exists():
        application.setWindowIcon(QIcon(str(APP_ICON)))
    application.setStyleSheet(_load_stylesheet(application))
    window = MainWindow()
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
