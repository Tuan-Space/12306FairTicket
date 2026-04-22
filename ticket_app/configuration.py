import importlib.util
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


BASE_URL = "https://kyfw.12306.cn"
STATION_URL = f"{BASE_URL}/otn/resources/js/framework/station_name.js"
DEFAULT_CONFIG_FILE = Path(__file__).resolve().parent.parent / "config.py"


class AppError(Exception):
    """Expected runtime failure that can be shown cleanly."""


@dataclass(frozen=True)
class SeatSpec:
    stock_key: str
    submit_code: str


@dataclass(frozen=True)
class PreparedPassengerSet:
    passengers: List[Dict[str, Any]]
    passenger_ticket_str: str
    old_passenger_str: str


SEAT_SPECS: Dict[str, SeatSpec] = {
    "商务座": SeatSpec("swz", "9"),
    "特等座": SeatSpec("tz", "P"),
    "一等座": SeatSpec("ydz", "M"),
    "二等座": SeatSpec("edz", "O"),
    "高级软卧": SeatSpec("gr", "6"),
    "软卧": SeatSpec("rw", "4"),
    "硬卧": SeatSpec("yw", "3"),
    "软座": SeatSpec("rz", "2"),
    "硬座": SeatSpec("yz", "1"),
    "无座": SeatSpec("wz", ""),
}

WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTH_NAMES = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


@dataclass
class AppConfig:
    from_station: str
    to_station: str
    train_date: str
    passenger_names: List[str]
    seat_types: List[str]
    preferred_trains: List[str]
    only_preferred_trains: bool
    start_at: str
    stop_at: str
    pre_query_seconds: float
    hot_query_interval_seconds: float
    hot_window_seconds: float
    query_interval_seconds: float
    max_retries: int
    auto_submit: bool
    choose_seats: str
    purpose_codes: str
    request_timeout_seconds: float
    login_qr_timeout_seconds: float
    login_qr_poll_seconds: float
    time_sync_samples: int
    time_sync_max_rtt_seconds: float
    order_wait_attempts: int
    order_wait_interval_seconds: float
    station_cache_days: int
    session_file: Path
    station_cache_file: Path
    qr_code_file: Path
    log_level: str
    perf_log: bool
    config_path: Path

    @classmethod
    def from_module(cls, module: Any, config_path: Path) -> "AppConfig":
        base_dir = config_path.resolve().parent

        def value(name: str, default: Any) -> Any:
            return getattr(module, name, default)

        cfg = cls(
            from_station=str(value("FROM_STATION", "")).strip(),
            to_station=str(value("TO_STATION", "")).strip(),
            train_date=str(value("TRAIN_DATE", "")).strip(),
            passenger_names=_as_list(value("PASSENGER_NAMES", [])),
            seat_types=_as_list(value("SEAT_TYPES", [])),
            preferred_trains=[item.upper() for item in _as_list(value("PREFERRED_TRAINS", []))],
            only_preferred_trains=bool(value("ONLY_PREFERRED_TRAINS", True)),
            start_at=str(value("START_AT", "")).strip(),
            stop_at=str(value("STOP_AT", "")).strip(),
            pre_query_seconds=float(value("PRE_QUERY_SECONDS", 3.0)),
            hot_query_interval_seconds=float(value("HOT_QUERY_INTERVAL_SECONDS", 0.25)),
            hot_window_seconds=float(value("HOT_WINDOW_SECONDS", 10.0)),
            query_interval_seconds=float(value("QUERY_INTERVAL_SECONDS", 1.0)),
            max_retries=int(value("MAX_RETRIES", 1000)),
            auto_submit=bool(value("AUTO_SUBMIT", True)),
            choose_seats=str(value("CHOOSE_SEATS", "")).strip(),
            purpose_codes=str(value("PURPOSE_CODES", "ADULT")).strip() or "ADULT",
            request_timeout_seconds=float(value("REQUEST_TIMEOUT_SECONDS", 10)),
            login_qr_timeout_seconds=float(value("LOGIN_QR_TIMEOUT_SECONDS", 180)),
            login_qr_poll_seconds=float(value("LOGIN_QR_POLL_SECONDS", 1.0)),
            time_sync_samples=int(value("TIME_SYNC_SAMPLES", 7)),
            time_sync_max_rtt_seconds=float(value("TIME_SYNC_MAX_RTT_SECONDS", 1.0)),
            order_wait_attempts=int(value("ORDER_WAIT_ATTEMPTS", 20)),
            order_wait_interval_seconds=float(value("ORDER_WAIT_INTERVAL_SECONDS", 2.0)),
            station_cache_days=int(value("STATION_CACHE_DAYS", 7)),
            session_file=_resolve_path(value("SESSION_FILE", ".runtime/session.cookies"), base_dir),
            station_cache_file=_resolve_path(value("STATION_CACHE_FILE", ".runtime/stations.json"), base_dir),
            qr_code_file=_resolve_path(value("QR_CODE_FILE", ".runtime/login_qr.png"), base_dir),
            log_level=str(value("LOG_LEVEL", "INFO")).upper(),
            perf_log=bool(value("PERF_LOG", True)),
            config_path=config_path.resolve(),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if not self.from_station:
            raise AppError("FROM_STATION 不能为空")
        if not self.to_station:
            raise AppError("TO_STATION 不能为空")
        if self.from_station == self.to_station:
            raise AppError("FROM_STATION 和 TO_STATION 不能相同")
        if not self.train_date:
            raise AppError("TRAIN_DATE 不能为空，格式为 YYYY-MM-DD")
        try:
            parsed_date = datetime.strptime(self.train_date, "%Y-%m-%d").date()
        except ValueError as exc:
            raise AppError("TRAIN_DATE 格式错误，应为 YYYY-MM-DD") from exc
        if parsed_date < date.today():
            raise AppError("TRAIN_DATE 不能早于今天")
        if self.auto_submit and not self.passenger_names:
            raise AppError("AUTO_SUBMIT=True 时必须填写 PASSENGER_NAMES")
        if not self.seat_types:
            raise AppError("SEAT_TYPES 至少需要填写一种座席")
        unsupported = [seat for seat in self.seat_types if seat not in SEAT_SPECS]
        if unsupported:
            supported = "、".join(SEAT_SPECS.keys())
            raise AppError(f"不支持的座席: {unsupported}；支持: {supported}")
        if self.query_interval_seconds <= 0:
            raise AppError("QUERY_INTERVAL_SECONDS 必须大于 0")
        if self.pre_query_seconds < 0:
            raise AppError("PRE_QUERY_SECONDS 不能小于 0")
        if self.hot_query_interval_seconds <= 0:
            raise AppError("HOT_QUERY_INTERVAL_SECONDS 必须大于 0")
        if self.hot_window_seconds < 0:
            raise AppError("HOT_WINDOW_SECONDS 不能小于 0")
        if self.max_retries <= 0:
            raise AppError("MAX_RETRIES 必须大于 0")
        if self.request_timeout_seconds <= 0:
            raise AppError("REQUEST_TIMEOUT_SECONDS 必须大于 0")
        if self.login_qr_timeout_seconds <= 0:
            raise AppError("LOGIN_QR_TIMEOUT_SECONDS 必须大于 0")
        if self.login_qr_poll_seconds <= 0:
            raise AppError("LOGIN_QR_POLL_SECONDS 必须大于 0")
        if self.time_sync_samples <= 0:
            raise AppError("TIME_SYNC_SAMPLES 必须大于 0")
        if self.time_sync_max_rtt_seconds <= 0:
            raise AppError("TIME_SYNC_MAX_RTT_SECONDS 必须大于 0")
        if self.order_wait_attempts <= 0:
            raise AppError("ORDER_WAIT_ATTEMPTS 必须大于 0")
        if self.order_wait_interval_seconds <= 0:
            raise AppError("ORDER_WAIT_INTERVAL_SECONDS 必须大于 0")
        _parse_schedule_time(self.start_at, allow_empty=True)
        _parse_schedule_time(self.stop_at, allow_empty=True)
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if self.log_level not in valid_levels:
            raise AppError(f"LOG_LEVEL 必须是 {sorted(valid_levels)} 之一")


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, Iterable):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _resolve_path(value: Any, base_dir: Path) -> Path:
    path = Path(str(value))
    return path if path.is_absolute() else base_dir / path


def _parse_schedule_time(value: str, allow_empty: bool) -> Optional[datetime]:
    if not value:
        if allow_empty:
            return None
        raise AppError("时间不能为空")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%H:%M:%S"):
        try:
            parsed = datetime.strptime(value, fmt)
            if fmt == "%H:%M:%S":
                return datetime.combine(date.today(), parsed.time())
            return parsed
        except ValueError:
            continue
    raise AppError(f"时间格式错误: {value}，应为 HH:MM:SS 或 YYYY-MM-DD HH:MM:SS")


def _resolve_schedule_time(value: str, now: datetime) -> Optional[datetime]:
    parsed = _parse_schedule_time(value, allow_empty=True)
    if not parsed:
        return None
    if len(value) == 8:
        return datetime.combine(now.date(), parsed.time())
    return parsed


def _elapsed_ms(start_perf: float) -> float:
    return (time.perf_counter() - start_perf) * 1000


def _perf_log(cfg: AppConfig, message: str, *args: Any) -> None:
    if cfg.perf_log:
        logging.info("[PERF] " + message, *args)


def load_config(config_path: Path) -> AppConfig:
    if not config_path.exists():
        raise AppError(f"配置文件不存在: {config_path}")
    spec = importlib.util.spec_from_file_location("ticket_config", str(config_path))
    if spec is None or spec.loader is None:
        raise AppError(f"无法加载配置文件: {config_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return AppConfig.from_module(module, config_path)
