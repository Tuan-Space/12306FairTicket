"""Privacy-safe JSON v5 settings and offline station-data helpers."""

from __future__ import annotations

from copy import deepcopy
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

from ticket_app.cart import (
    reject_unresolved_cart_migration, serialize_cart_items,
)
from ticket_app.configuration import AppConfig, AppError, DEFAULT_CONFIG_FILE
from ticket_app.input_parsing import split_multi_value_text
from ticket_app.passengers import normalize_passenger_ticket_types
from ticket_app.preferences import SeatRelationPreference


PROJECT_ROOT = Path(__file__).resolve().parents[2]
_LOCAL_APPDATA = os.environ.get("LOCALAPPDATA")
if _LOCAL_APPDATA:
    LOCAL_DATA_DIR = Path(_LOCAL_APPDATA) / "12306FairTicket"
else:
    LOCAL_DATA_DIR = Path.home() / ".local" / "share" / "12306FairTicket"
RUNTIME_DIR = LOCAL_DATA_DIR
GUI_CONFIG_VERSION = 5
GUI_CONFIG_MAX_BYTES = 1_000_000
STATION_CACHE_FILE = LOCAL_DATA_DIR / "stations.json"
STATION_SNAPSHOT_FILE = PROJECT_ROOT / "assets" / "stations_snapshot.json"

COMMON_STATIONS = (
    "北京",
    "北京西",
    "北京南",
    "北京朝阳",
    "上海",
    "上海虹桥",
    "广州",
    "广州南",
    "深圳北",
    "杭州东",
    "南京南",
    "天津",
    "石家庄",
    "郑州东",
    "武汉",
    "长沙南",
    "西安北",
    "成都东",
    "重庆北",
    "昆明南",
    "南宁东",
    "贵阳北",
    "福州南",
    "厦门北",
    "济南西",
    "青岛北",
    "沈阳北",
    "长春西",
    "哈尔滨西",
)


DEFAULT_VALUES: Dict[str, Any] = {
    "cart_items": [],
    "train_date": "",
    "passenger_names": [],
    "passenger_ticket_types": {},
    "quiet_carriage_preference": False,
    "start_at": "10:00:00",
    "stop_at": "10:05:00",
    "query_interval_seconds": 0.6,
    "max_retries": 1000,
    "pre_query_seconds": 1.5,
    "hot_query_interval_seconds": 0.25,
    "hot_window_seconds": 5.0,
    "auto_submit": True,
    "seat_position_preferences": [],
    "berth_preference": {"lower": 0, "middle": 0, "upper": 0},
    "persist_session": False,
    "purpose_codes": "ADULT",
    "request_timeout_seconds": 10.0,
    "login_qr_timeout_seconds": 180.0,
    "login_qr_poll_seconds": 1.0,
    "time_sync_samples": 7,
    "time_sync_max_rtt_seconds": 1.0,
    "order_wait_attempts": 300,
    "order_wait_interval_seconds": 2.0,
    "station_cache_days": 7,
    "session_file": str(RUNTIME_DIR / "session.cookies"),
    "station_cache_file": str(RUNTIME_DIR / "stations.json"),
    "qr_code_file": str(RUNTIME_DIR / "login_qr.png"),
    "log_level": "INFO",
    "perf_log": True,
}


# These are every setting a GUI user is allowed to change.  Runtime paths,
# session persistence and identity/login fields deliberately do not appear in
# this allow-list, so unknown future keys cannot accidentally be persisted.
EDITABLE_SETTINGS_KEYS = frozenset(
    {
        "train_date", "passenger_names", "cart_items",
        "passenger_ticket_types", "quiet_carriage_preference",
        "start_at", "stop_at", "query_interval_seconds", "max_retries",
        "pre_query_seconds", "hot_query_interval_seconds", "hot_window_seconds",
        "auto_submit", "seat_position_preferences", "berth_preference",
        "request_timeout_seconds", "login_qr_timeout_seconds",
        "login_qr_poll_seconds", "time_sync_samples", "time_sync_max_rtt_seconds",
        "order_wait_attempts", "order_wait_interval_seconds", "station_cache_days",
        "log_level", "perf_log",
    }
)


def _json_value(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "positions"):
        return [_json_value(item) for item in getattr(value, "positions")]
    if all(hasattr(value, name) for name in ("lower", "middle", "upper")):
        return {name: int(getattr(value, name)) for name in ("lower", "middle", "upper")}
    return str(value)


def canonical_mapping(values: Mapping[str, Any]) -> Dict[str, Any]:
    """Return lowercase GUI keys from either Python-style or config keys."""

    result = deepcopy(DEFAULT_VALUES)
    try:
        reject_unresolved_cart_migration(values.get("cart_migration", values.get("CART_MIGRATION")))
    except ValueError as exc:
        raise AppError(str(exc)) from exc
    for key, value in values.items():
        canonical = str(key).lower()
        if canonical not in DEFAULT_VALUES:
            continue
        if canonical == "cart_items" and value is not None:
            try:
                result[canonical] = serialize_cart_items(value)
            except ValueError as exc:
                raise AppError(str(exc)) from exc
        else:
            result[canonical] = _json_value(value)

    if result["cart_items"] is None:
        raise AppError("配置必须包含 cart_items 项目列表（空草稿可使用 []）")
    for name in ("passenger_names",):
        value = result.get(name)
        if isinstance(value, str):
            result[name] = split_multi_value_text(value)
        elif isinstance(value, Iterable) and not isinstance(value, Mapping):
            result[name] = [str(item).strip() for item in value if str(item).strip()]
        else:
            result[name] = []
    try:
        result["passenger_ticket_types"] = normalize_passenger_ticket_types(result.get("passenger_ticket_types"))
    except ValueError as exc:
        raise AppError(f"乘车人票种无效: {exc}") from exc
    if not isinstance(result.get("quiet_carriage_preference"), bool):
        raise AppError("静音车厢偏好必须是布尔值")

    positions = result.get("seat_position_preferences", [])
    try:
        position_preference = SeatRelationPreference.from_value(positions)
    except ValueError as exc:
        raise AppError(f"座位位置偏好无效: {exc}") from exc
    result["seat_position_preferences"] = list(position_preference.positions)

    berth = result.get("berth_preference")
    if not isinstance(berth, Mapping):
        berth = {}
    result["berth_preference"] = {
        "lower": max(0, int(berth.get("lower", 0) or 0)),
        "middle": max(0, int(berth.get("middle", 0) or 0)),
        "upper": max(0, int(berth.get("upper", 0) or 0)),
    }
    return result


def _station_names_from_document(document: Any) -> set[str]:
    """Extract station names from a StationStore cache or a plain mapping."""

    stations = document.get("stations", document) if isinstance(document, Mapping) else {}
    if not isinstance(stations, Mapping):
        return set()
    return {str(name).strip() for name in stations if str(name).strip()}


def bundled_station_names(snapshot_path: Path = STATION_SNAPSHOT_FILE) -> list[str]:
    """Return packaged station names without ever contacting the network."""

    names = set(COMMON_STATIONS)
    try:
        if snapshot_path.exists():
            names.update(_station_names_from_document(json.loads(snapshot_path.read_text(encoding="utf-8"))))
    except (OSError, UnicodeError, json.JSONDecodeError):
        # The compact built-in list keeps autocomplete usable if a damaged
        # installation omitted its optional snapshot.
        pass
    return sorted(names)


def cached_station_names(cache_path: Optional[Path] = None) -> list[str]:
    """Load packaged and current-user station names; this is strictly offline."""

    names = set(bundled_station_names())
    candidates = (cache_path or STATION_CACHE_FILE,)
    for path in candidates:
        if not path.exists():
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        names.update(_station_names_from_document(document))
    return sorted(names)


def build_app_config(values: Mapping[str, Any], config_path: Optional[Path] = None) -> AppConfig:
    """Build the current cart configuration without loading a Python file."""

    canonical = canonical_mapping(values)
    canonical["persist_session"] = False
    canonical["session_file"] = str(RUNTIME_DIR / "session.cookies")
    canonical["station_cache_file"] = str(RUNTIME_DIR / "stations.json")
    canonical["qr_code_file"] = str(RUNTIME_DIR / "login_qr.png")
    return AppConfig.from_mapping(canonical, config_path=(config_path or DEFAULT_CONFIG_FILE).resolve())


def editable_settings_payload(values: Mapping[str, Any]) -> Dict[str, Any]:
    """Produce the complete, privacy-safe version 5 settings mapping.

    Unfinished forms remain saveable; only allow-listed user settings reach disk.
    """

    canonical = canonical_mapping(values)
    payload = {
        key: _json_value(canonical[key])
        for key in sorted(EDITABLE_SETTINGS_KEYS)
        if key in canonical
    }
    float_keys = {
        "query_interval_seconds", "pre_query_seconds", "hot_query_interval_seconds",
        "hot_window_seconds", "request_timeout_seconds", "login_qr_timeout_seconds",
        "login_qr_poll_seconds", "time_sync_max_rtt_seconds", "order_wait_interval_seconds",
    }
    integer_keys = {"max_retries", "time_sync_samples", "order_wait_attempts", "station_cache_days"}
    boolean_keys = {"auto_submit", "perf_log", "quiet_carriage_preference"}
    try:
        for key in float_keys:
            raw = payload[key]
            if isinstance(raw, bool):
                raise ValueError("布尔值不是数值")
            number = float(raw)
            if not math.isfinite(number):
                raise ValueError("必须是有限数值")
            payload[key] = number
        for key in integer_keys:
            raw = payload[key]
            if isinstance(raw, bool):
                raise ValueError("布尔值不是整数")
            number = int(raw)
            if float(raw) != number:
                raise ValueError("必须是整数")
            payload[key] = number
        for key in boolean_keys:
            if not isinstance(payload[key], bool):
                raise ValueError("必须是布尔值")
    except (TypeError, ValueError, OverflowError) as exc:
        raise AppError(f"配置字段类型无效: {exc}") from exc
    return payload


def _read_json_file(path: Path) -> Mapping[str, Any]:
    """Read one bounded JSON object and give UI callers a friendly error."""

    try:
        if path.suffix.lower() != ".json":
            raise AppError("GUI 配置仅支持 JSON 文件")
        if path.stat().st_size > GUI_CONFIG_MAX_BYTES:
            raise AppError("配置 JSON 超过 1 MB，已拒绝导入")
        loaded = json.loads(path.read_text(encoding="utf-8-sig"))
    except AppError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AppError(f"配置 JSON 无法读取: {exc}") from exc
    if not isinstance(loaded, Mapping):
        raise AppError("配置 JSON 顶层必须是对象")
    return loaded


def _atomic_write_json(path: Path, document: Mapping[str, Any]) -> None:
    """Atomically replace a JSON file, leaving the old file intact on error."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(document, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        # ``os.replace`` removes it on success; this cleanup only covers a
        # serialization/write failure before replacement.
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def save_gui_settings(path: Path, values: Mapping[str, Any]) -> None:
    """Save all editable GUI settings as a version 5 document atomically."""

    _atomic_write_json(
        path,
        {"version": GUI_CONFIG_VERSION, "settings": editable_settings_payload(values)},
    )


def load_gui_settings(path: Path) -> Dict[str, Any]:
    """Read only JSON v5; obsolete or unresolved restrictions are rejected."""

    document = _read_json_file(path)
    version = document.get("version")
    if type(version) is not int:
        raise AppError("配置 JSON 缺少整数 version")
    if version != GUI_CONFIG_VERSION:
        raise AppError(f"不支持的配置版本: {version}；仅支持 version 5，请重新建立购物车")
    settings = document.get("settings")
    if not isinstance(settings, Mapping):
        raise AppError("version 5 配置的 settings 必须是对象")
    if "cart_items" not in settings and "CART_ITEMS" not in settings:
        raise AppError("version 5 配置必须包含 cart_items 项目列表（空草稿可使用 []）")
    return canonical_mapping(editable_settings_payload(settings))
