import importlib.util
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .preferences import (
    BERTH_SEAT_TYPES,
    BerthPreference,
    SeatRelationPreference,
    seat_layout_positions,
)


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
    seat_relation_preference: SeatRelationPreference
    berth_preference: BerthPreference
    persist_session: bool
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

    @property
    def seat_position_preferences(self) -> SeatRelationPreference:
        """Compatibility-friendly alias used by the GUI layer."""

        return self.seat_relation_preference

    @property
    def choose_seats(self) -> str:
        """Legacy raw value, retained for older callers during migration."""

        return "".join(self.seat_relation_preference.positions)

    @classmethod
    def from_module(cls, module: Any, config_path: Path) -> "AppConfig":
        return cls.from_mapping(vars(module), config_path)

    @classmethod
    def from_mapping(
        cls,
        mapping: Mapping[str, Any],
        config_path: Path = DEFAULT_CONFIG_FILE,
    ) -> "AppConfig":
        """Create a validated config from Python-style or snake-case keys."""

        config_path = Path(config_path).resolve()
        base_dir = config_path.parent

        def has_value(*names: str) -> bool:
            return any(name in mapping or name.lower() in mapping for name in names)

        def value(name: str, default: Any, *aliases: str) -> Any:
            for candidate in (name,) + aliases:
                if candidate in mapping:
                    return mapping[candidate]
                snake_name = candidate.lower()
                if snake_name in mapping:
                    return mapping[snake_name]
            return default

        has_structured_seat_preference = has_value(
            "SEAT_POSITION_PREFERENCES", "SEAT_RELATION_PREFERENCE"
        )
        has_legacy_seat_preference = has_value("CHOOSE_SEATS")
        legacy_seat_preference = value("CHOOSE_SEATS", "")
        if has_structured_seat_preference:
            raw_seat_preference = value(
                "SEAT_POSITION_PREFERENCES", [], "SEAT_RELATION_PREFERENCE"
            )
            if has_legacy_seat_preference and str(legacy_seat_preference or "").strip():
                logging.warning(
                    "同时配置了结构化座位偏好和旧 CHOOSE_SEATS；已忽略 CHOOSE_SEATS"
                )
        else:
            raw_seat_preference = legacy_seat_preference
            if str(raw_seat_preference or "").strip():
                logging.info("已将旧 CHOOSE_SEATS 转换为结构化座位位置偏好")

        try:
            seat_preference = SeatRelationPreference.from_value(raw_seat_preference)
            berth_preference = BerthPreference.from_value(value("BERTH_PREFERENCE", {}))
        except ValueError as exc:
            raise AppError(str(exc)) from exc

        cfg = cls(
            from_station=str(value("FROM_STATION", "")).strip(),
            to_station=str(value("TO_STATION", "")).strip(),
            train_date=str(value("TRAIN_DATE", "")).strip(),
            passenger_names=_as_list(value("PASSENGER_NAMES", [])),
            seat_types=_as_list(value("SEAT_TYPES", [])),
            preferred_trains=[item.upper() for item in _as_list(value("PREFERRED_TRAINS", []))],
            only_preferred_trains=_as_bool(value("ONLY_PREFERRED_TRAINS", True), "ONLY_PREFERRED_TRAINS"),
            start_at=str(value("START_AT", "")).strip(),
            stop_at=str(value("STOP_AT", "")).strip(),
            pre_query_seconds=float(value("PRE_QUERY_SECONDS", 3.0)),
            hot_query_interval_seconds=float(value("HOT_QUERY_INTERVAL_SECONDS", 0.25)),
            hot_window_seconds=float(value("HOT_WINDOW_SECONDS", 10.0)),
            query_interval_seconds=float(value("QUERY_INTERVAL_SECONDS", 1.0)),
            max_retries=int(value("MAX_RETRIES", 1000)),
            auto_submit=_as_bool(value("AUTO_SUBMIT", True), "AUTO_SUBMIT"),
            seat_relation_preference=seat_preference,
            berth_preference=berth_preference,
            persist_session=_as_bool(value("PERSIST_SESSION", True), "PERSIST_SESSION"),
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
            perf_log=_as_bool(value("PERF_LOG", True), "PERF_LOG"),
            config_path=config_path,
        )
        cfg.validate()
        return cfg

    def to_mapping(self) -> Dict[str, Any]:
        """Return a serializable mapping suitable for GUI persistence."""

        return {
            "FROM_STATION": self.from_station,
            "TO_STATION": self.to_station,
            "TRAIN_DATE": self.train_date,
            "PASSENGER_NAMES": list(self.passenger_names),
            "SEAT_TYPES": list(self.seat_types),
            "PREFERRED_TRAINS": list(self.preferred_trains),
            "ONLY_PREFERRED_TRAINS": self.only_preferred_trains,
            "START_AT": self.start_at,
            "STOP_AT": self.stop_at,
            "PRE_QUERY_SECONDS": self.pre_query_seconds,
            "HOT_QUERY_INTERVAL_SECONDS": self.hot_query_interval_seconds,
            "HOT_WINDOW_SECONDS": self.hot_window_seconds,
            "QUERY_INTERVAL_SECONDS": self.query_interval_seconds,
            "MAX_RETRIES": self.max_retries,
            "AUTO_SUBMIT": self.auto_submit,
            "SEAT_POSITION_PREFERENCES": list(self.seat_relation_preference.positions),
            "BERTH_PREFERENCE": dict(self.berth_preference.to_mapping()),
            "PERSIST_SESSION": self.persist_session,
            "PURPOSE_CODES": self.purpose_codes,
            "REQUEST_TIMEOUT_SECONDS": self.request_timeout_seconds,
            "LOGIN_QR_TIMEOUT_SECONDS": self.login_qr_timeout_seconds,
            "LOGIN_QR_POLL_SECONDS": self.login_qr_poll_seconds,
            "TIME_SYNC_SAMPLES": self.time_sync_samples,
            "TIME_SYNC_MAX_RTT_SECONDS": self.time_sync_max_rtt_seconds,
            "ORDER_WAIT_ATTEMPTS": self.order_wait_attempts,
            "ORDER_WAIT_INTERVAL_SECONDS": self.order_wait_interval_seconds,
            "STATION_CACHE_DAYS": self.station_cache_days,
            "LOG_LEVEL": self.log_level,
            "PERF_LOG": self.perf_log,
            "QR_CODE_FILE": str(self.qr_code_file),
            "SESSION_FILE": str(self.session_file),
            "STATION_CACHE_FILE": str(self.station_cache_file),
        }

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
        passenger_count = len(self.passenger_names)
        if self.auto_submit and not self.passenger_names:
            raise AppError("AUTO_SUBMIT=True 时必须填写 PASSENGER_NAMES")
        if passenger_count > 5:
            raise AppError("PASSENGER_NAMES 最多支持 5 位乘车人")
        if len(set(self.passenger_names)) != passenger_count:
            raise AppError("PASSENGER_NAMES 不能包含重复乘车人")
        if not self.seat_types:
            raise AppError("SEAT_TYPES 至少需要填写一种座席")
        unsupported = [seat for seat in self.seat_types if seat not in SEAT_SPECS]
        if unsupported:
            supported = "、".join(SEAT_SPECS.keys())
            raise AppError(f"不支持的座席: {unsupported}；支持: {supported}")
        if self.seat_relation_preference.enabled:
            if not passenger_count:
                raise AppError("设置座位位置偏好时必须填写 PASSENGER_NAMES")
            try:
                self.seat_relation_preference.validate(passenger_count)
            except ValueError as exc:
                raise AppError(str(exc)) from exc
            seated_codes = [SEAT_SPECS[label].submit_code for label in self.seat_types]
            if not any(
                set(self.seat_relation_preference.positions).issubset(
                    seat_layout_positions(code, None)
                )
                for code in seated_codes
                if seat_layout_positions(code, None)
            ):
                raise AppError("座位位置偏好与所选 SEAT_TYPES 的 ABCDF 布局均不兼容")
        if self.berth_preference.enabled:
            if not passenger_count:
                raise AppError("设置铺位偏好时必须填写 PASSENGER_NAMES")
            try:
                self.berth_preference.validate(passenger_count)
            except ValueError as exc:
                raise AppError(str(exc)) from exc
            configured_codes = {SEAT_SPECS[label].submit_code for label in self.seat_types}
            if not configured_codes.intersection(BERTH_SEAT_TYPES):
                raise AppError("BERTH_PREFERENCE 需要至少选择一种卧铺席别")
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


def _as_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "y", "1", "on"}:
            return True
        if normalized in {"false", "no", "n", "0", "off", ""}:
            return False
    raise AppError(f"{name} 必须是布尔值")


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
