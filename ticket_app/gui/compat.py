"""Safe configuration and local station-data helpers for the desktop UI.

GUI configuration is JSON-only.  ``safe_read_legacy_config`` remains as a
deprecated, non-executing migration helper for third-party callers, but the
desktop application must not offer or invoke it.  In particular, no GUI-owned
configuration API imports (or executes) a user supplied ``config.py``.
"""

from __future__ import annotations

import ast
import inspect
import json
import math
import os
import tempfile
from dataclasses import MISSING, fields, is_dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, MutableMapping, Optional

from ticket_app.configuration import AppConfig, AppError
from ticket_app.input_parsing import split_multi_value_text
from ticket_app.preferences import SeatRelationPreference


PROJECT_ROOT = Path(__file__).resolve().parents[2]
_LOCAL_APPDATA = os.environ.get("LOCALAPPDATA")
if _LOCAL_APPDATA:
    LOCAL_DATA_DIR = Path(_LOCAL_APPDATA) / "12306FairTicket"
else:
    LOCAL_DATA_DIR = Path.home() / ".local" / "share" / "12306FairTicket"
RUNTIME_DIR = LOCAL_DATA_DIR
PROFILE_FILE = RUNTIME_DIR / "gui_profiles.json"
LEGACY_CONFIG_FILE = PROJECT_ROOT / "config.py"
GUI_CONFIG_VERSION = 2
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


CONFIG_KEY_MAP: Dict[str, str] = {
    "FROM_STATION": "from_station",
    "TO_STATION": "to_station",
    "TRAIN_DATE": "train_date",
    "PASSENGER_NAMES": "passenger_names",
    "SEAT_TYPES": "seat_types",
    "PREFERRED_TRAINS": "preferred_trains",
    "ONLY_PREFERRED_TRAINS": "only_preferred_trains",
    "START_AT": "start_at",
    "STOP_AT": "stop_at",
    "QUERY_INTERVAL_SECONDS": "query_interval_seconds",
    "MAX_RETRIES": "max_retries",
    "PRE_QUERY_SECONDS": "pre_query_seconds",
    "HOT_QUERY_INTERVAL_SECONDS": "hot_query_interval_seconds",
    "HOT_WINDOW_SECONDS": "hot_window_seconds",
    "AUTO_SUBMIT": "auto_submit",
    "CHOOSE_SEATS": "choose_seats",
    "SEAT_POSITION_PREFERENCES": "seat_position_preferences",
    "BERTH_PREFERENCE": "berth_preference",
    "POSITION_FALLBACK": "position_fallback",
    "PERSIST_SESSION": "persist_session",
    "PURPOSE_CODES": "purpose_codes",
    "REQUEST_TIMEOUT_SECONDS": "request_timeout_seconds",
    "LOGIN_QR_TIMEOUT_SECONDS": "login_qr_timeout_seconds",
    "LOGIN_QR_POLL_SECONDS": "login_qr_poll_seconds",
    "TIME_SYNC_SAMPLES": "time_sync_samples",
    "TIME_SYNC_MAX_RTT_SECONDS": "time_sync_max_rtt_seconds",
    "ORDER_WAIT_ATTEMPTS": "order_wait_attempts",
    "ORDER_WAIT_INTERVAL_SECONDS": "order_wait_interval_seconds",
    "STATION_CACHE_DAYS": "station_cache_days",
    "SESSION_FILE": "session_file",
    "STATION_CACHE_FILE": "station_cache_file",
    "QR_CODE_FILE": "qr_code_file",
    "LOG_LEVEL": "log_level",
    "PERF_LOG": "perf_log",
}
CANONICAL_TO_CONFIG = {value: key for key, value in CONFIG_KEY_MAP.items()}


DEFAULT_VALUES: Dict[str, Any] = {
    "from_station": "北京西",
    "to_station": "郑州东",
    "train_date": "",
    "passenger_names": [],
    "seat_types": ["二等座", "无座", "一等座"],
    "preferred_trains": [],
    "only_preferred_trains": True,
    "start_at": "10:00:00",
    "stop_at": "10:05:00",
    "query_interval_seconds": 0.6,
    "max_retries": 1000,
    "pre_query_seconds": 1.5,
    "hot_query_interval_seconds": 0.25,
    "hot_window_seconds": 5.0,
    "auto_submit": True,
    "choose_seats": "",
    "seat_position_preferences": [],
    "berth_preference": {"lower": 0, "middle": 0, "upper": 0},
    "position_fallback": True,
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
    "config_path": str(LEGACY_CONFIG_FILE),
}


# These are every setting a GUI user is allowed to change.  Runtime paths,
# session persistence and identity/login fields deliberately do not appear in
# this allow-list, so unknown future keys cannot accidentally be persisted.
EDITABLE_SETTINGS_KEYS = frozenset(
    {
        "from_station", "to_station", "train_date", "passenger_names",
        "seat_types", "preferred_trains", "only_preferred_trains",
        "start_at", "stop_at", "query_interval_seconds", "max_retries",
        "pre_query_seconds", "hot_query_interval_seconds", "hot_window_seconds",
        "auto_submit", "seat_position_preferences", "berth_preference",
        "request_timeout_seconds", "login_qr_timeout_seconds",
        "login_qr_poll_seconds", "time_sync_samples", "time_sync_max_rtt_seconds",
        "order_wait_attempts", "order_wait_interval_seconds", "station_cache_days",
        "log_level", "perf_log",
    }
)
# Kept as a public compatibility name for the v1 named-profile reader.
PROFILE_KEYS = frozenset(
    {
        "from_station", "to_station", "train_date", "passenger_names",
        "seat_types", "preferred_trains", "only_preferred_trains", "start_at",
        "stop_at", "auto_submit", "seat_position_preferences", "berth_preference",
        "position_fallback",
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

    result = dict(DEFAULT_VALUES)
    # Presence matters here: an explicitly supplied empty structured preference
    # means "clear the preference" and must win over a stale CHOOSE_SEATS value.
    structured_position_supplied = any(
        CONFIG_KEY_MAP.get(str(key), str(key).lower()) == "seat_position_preferences"
        for key in values
    )
    for key, value in values.items():
        canonical = CONFIG_KEY_MAP.get(str(key), str(key).lower())
        result[canonical] = _json_value(value)

    for name in ("passenger_names", "seat_types", "preferred_trains"):
        value = result.get(name)
        if isinstance(value, str):
            result[name] = split_multi_value_text(value)
        elif isinstance(value, Iterable) and not isinstance(value, Mapping):
            result[name] = [str(item).strip() for item in value if str(item).strip()]
        else:
            result[name] = []
    result["preferred_trains"] = [item.upper() for item in result["preferred_trains"]]

    positions = result.get("seat_position_preferences", [])
    if not structured_position_supplied and result.get("choose_seats"):
        positions = result["choose_seats"]
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


def safe_read_legacy_config(path: Path) -> Dict[str, Any]:
    """Read literal top-level assignments without executing the Python file."""

    path = path.resolve()
    if not path.exists():
        return dict(DEFAULT_VALUES)
    if path.stat().st_size > 1_000_000:
        raise AppError("配置文件过大，已拒绝导入")
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    except (OSError, SyntaxError, UnicodeError) as exc:
        raise AppError(f"无法解析旧配置: {exc}") from exc

    extracted: Dict[str, Any] = {}
    for node in tree.body:
        target: Optional[ast.expr] = None
        value_node: Optional[ast.expr] = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value_node = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value_node = node.target, node.value
        if not isinstance(target, ast.Name) or value_node is None:
            continue
        if target.id not in CONFIG_KEY_MAP:
            continue
        try:
            extracted[target.id] = ast.literal_eval(value_node)
        except (ValueError, TypeError, SyntaxError):
            # Calls, comprehensions, names and other executable expressions are
            # intentionally ignored rather than evaluated.
            continue
    result = canonical_mapping(extracted)
    result["config_path"] = str(path)
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


def _with_config_keys(values: Mapping[str, Any]) -> Dict[str, Any]:
    canonical = canonical_mapping(values)
    payload = dict(canonical)
    for key, value in canonical.items():
        config_key = CANONICAL_TO_CONFIG.get(key)
        if config_key:
            payload[config_key] = _json_value(value)
    return payload


def build_app_config(values: Mapping[str, Any], config_path: Optional[Path] = None) -> AppConfig:
    """Build AppConfig through the new mapping API or the legacy dataclass."""

    canonical = canonical_mapping(values)
    # Desktop policy: every process starts with a fresh QR login.  Runtime
    # state and diagnostics live under the user's local application-data path.
    canonical["persist_session"] = False
    canonical["session_file"] = str(RUNTIME_DIR / "session.cookies")
    canonical["station_cache_file"] = str(RUNTIME_DIR / "stations.json")
    canonical["qr_code_file"] = str(RUNTIME_DIR / "login_qr.png")
    path = (config_path or LEGACY_CONFIG_FILE).resolve()
    mapping_factory = getattr(AppConfig, "from_mapping", None)
    if callable(mapping_factory):
        payload = _with_config_keys(canonical)
        signature = inspect.signature(mapping_factory)
        kwargs: Dict[str, Any] = {}
        if "config_path" in signature.parameters:
            kwargs["config_path"] = path
        if "base_dir" in signature.parameters:
            kwargs["base_dir"] = path.parent
        return mapping_factory(payload, **kwargs)

    if not is_dataclass(AppConfig):
        raise AppError("当前 AppConfig 既不是 dataclass，也未提供 from_mapping")

    field_values: MutableMapping[str, Any] = dict(canonical)
    field_values["config_path"] = path
    for name in ("session_file", "station_cache_file", "qr_code_file"):
        raw = Path(str(field_values[name]))
        field_values[name] = raw if raw.is_absolute() else path.parent / raw

    kwargs = {}
    for item in fields(AppConfig):
        if item.name in field_values:
            kwargs[item.name] = field_values[item.name]
        elif item.default is MISSING and item.default_factory is MISSING:
            raise AppError(f"GUI 无法为 AppConfig.{item.name} 提供值")
    cfg = AppConfig(**kwargs)
    validator = getattr(cfg, "validate", None)
    if callable(validator):
        validator()
    return cfg


def config_to_mapping(cfg: AppConfig) -> Dict[str, Any]:
    exporter = getattr(cfg, "to_mapping", None)
    if callable(exporter):
        return canonical_mapping(exporter())
    if is_dataclass(cfg):
        return canonical_mapping({item.name: getattr(cfg, item.name) for item in fields(cfg)})
    raise AppError("当前 AppConfig 不支持导出配置")


def profile_payload(values: Mapping[str, Any]) -> Dict[str, Any]:
    canonical = canonical_mapping(values)
    return {key: _json_value(canonical[key]) for key in sorted(PROFILE_KEYS) if key in canonical}


def editable_settings_payload(values: Mapping[str, Any]) -> Dict[str, Any]:
    """Produce the complete, privacy-safe version 2 settings mapping.

    Defaults are filled in intentionally: saving an unfinished form is valid,
    and an imported v1 document receives the advanced settings introduced by
    version 2.  Only the allow-listed keys can reach disk.
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
    boolean_keys = {"only_preferred_trains", "auto_submit", "perf_log"}
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
    """Save all editable GUI settings as a version 2 document atomically."""

    _atomic_write_json(
        path,
        {"version": GUI_CONFIG_VERSION, "settings": editable_settings_payload(values)},
    )


def load_gui_settings(path: Path) -> Dict[str, Any]:
    """Import a version 2 document or a safe subset of the legacy version 1.

    Version 1 exports used ``values`` and contain only the former profile
    fields.  Old named-profile database documents are intentionally not
    interpreted, because importing one silently would reintroduce the removed
    named-profile model.
    """

    document = _read_json_file(path)
    version = document.get("version")
    if type(version) is not int:
        raise AppError("配置 JSON 缺少整数 version")
    if version == GUI_CONFIG_VERSION:
        settings = document.get("settings")
        if not isinstance(settings, Mapping):
            raise AppError("version 2 配置的 settings 必须是对象")
        return canonical_mapping(editable_settings_payload(settings))
    if version == 1:
        settings = document.get("values")
        if not isinstance(settings, Mapping):
            raise AppError("version 1 配置必须包含 values 对象")
        return canonical_mapping(editable_settings_payload(settings))
    raise AppError(f"不支持的配置版本: {version}")


class GuiConfigStore:
    """Stateless JSON-only save/import facade used by the simplified UI."""

    def save_file(self, path: Path, values: Mapping[str, Any]) -> None:
        save_gui_settings(path, values)

    def import_file(self, path: Path) -> Dict[str, Any]:
        return load_gui_settings(path)


class ProfileStore:
    """Versioned, atomic JSON profile persistence."""

    def __init__(self, path: Path = PROFILE_FILE) -> None:
        self.path = path
        self._document: Dict[str, Any] = {"version": 1, "last_profile": "", "profiles": {}}
        self.reload()

    def reload(self) -> None:
        if not self.path.exists():
            return
        try:
            if self.path.stat().st_size > 1_000_000:
                return
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return
        if not isinstance(loaded, Mapping):
            return
        if type(loaded.get("version")) is not int or loaded.get("version") != 1:
            # Unknown formats are ignored rather than partially interpreting
            # fields with potentially different privacy or validation rules.
            return
        if isinstance(loaded.get("profiles"), Mapping):
            profiles: Dict[str, Any] = {}
            for name, value in loaded["profiles"].items():
                if not isinstance(value, Mapping):
                    continue
                try:
                    profiles[str(name)] = profile_payload(value)
                except (AppError, TypeError, ValueError):
                    # A single malformed profile must not prevent the GUI from
                    # opening or expose partially interpreted preferences.
                    continue
            last_profile = str(loaded.get("last_profile", ""))
            self._document = {
                "version": 1,
                "last_profile": last_profile if last_profile in profiles else "",
                "profiles": profiles,
            }

    @property
    def last_profile(self) -> str:
        return str(self._document.get("last_profile", ""))

    def names(self) -> list[str]:
        return sorted(self._document["profiles"], key=str.casefold)

    def get(self, name: str) -> Optional[Dict[str, Any]]:
        value = self._document["profiles"].get(name)
        return canonical_mapping(value) if isinstance(value, Mapping) else None

    def put(self, name: str, values: Mapping[str, Any]) -> None:
        name = name.strip()
        if not name:
            raise AppError("配置档案名不能为空")
        self._document["profiles"][name] = profile_payload(values)
        self._document["last_profile"] = name
        self._write()

    def delete(self, name: str) -> None:
        self._document["profiles"].pop(name, None)
        if self.last_profile == name:
            self._document["last_profile"] = ""
        self._write()

    def mark_last(self, name: str) -> None:
        if name in self._document["profiles"]:
            self._document["last_profile"] = name
            self._write()

    def import_file(self, path: Path, name: Optional[str] = None) -> str:
        try:
            if path.stat().st_size > 1_000_000:
                raise AppError("配置档案 JSON 超过 1 MB，已拒绝导入")
            loaded = json.loads(path.read_text(encoding="utf-8-sig"))
        except AppError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AppError(f"配置档案 JSON 无法读取: {exc}") from exc
        if not isinstance(loaded, Mapping):
            raise AppError("配置档案 JSON 顶层必须是对象")
        if type(loaded.get("version")) is not int or loaded.get("version") != 1:
            raise AppError("不支持的配置档案版本（当前仅支持 version=1）")
        resolved = (name or str(loaded.get("name") or path.stem)).strip()
        values = loaded.get("values", loaded)
        if not isinstance(values, Mapping):
            raise AppError("配置档案 values 必须是对象")
        self.put(resolved, values)
        return resolved

    def export_file(self, path: Path, name: str, values: Mapping[str, Any]) -> None:
        document = {"version": 1, "name": name, "values": profile_payload(values)}
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        text = json.dumps(self._document, ensure_ascii=False, indent=2) + "\n"
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, self.path)
