"""Pure, field-oriented validation for the desktop configuration form.

Unlike :meth:`AppConfig.validate`, this module deliberately returns every
actionable error in one pass so the UI can mark each corresponding widget.  It
does not create clients, load station data, or perform any network operation.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Iterable, Mapping

from ticket_app.configuration import SEAT_SPECS
from ticket_app.preferences import (
    BERTH_SEAT_TYPES,
    BerthPreference,
    SeatRelationPreference,
    seat_layout_positions,
)

from .compat import DEFAULT_VALUES


# Passenger train identifiers are normally one prefix letter plus digits, but
# conventional services may be shown as digits only (for example 1461).
TRAIN_CODE_PATTERN = re.compile(r"^(?:[A-Z][0-9]{1,5}|[0-9]{1,5})$")
VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


def _field_value(values: Mapping[str, Any], key: str) -> Any:
    """Read canonical or legacy uppercase names without accepting unknowns."""

    if key in values:
        return values[key]
    legacy_key = key.upper()
    return values.get(legacy_key, DEFAULT_VALUES[key])


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in re.split(r"[,，;；\n]+", value) if item.strip()]
    if isinstance(value, Iterable) and not isinstance(value, (Mapping, bytes, bytearray)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and result not in (float("inf"), float("-inf")) else None


def _validate_number(
    errors: dict[str, str],
    values: Mapping[str, Any],
    key: str,
    label: str,
    *,
    minimum: float,
    maximum: float,
    inclusive: bool,
    integer: bool = False,
) -> None:
    number = _number(_field_value(values, key))
    if number is None:
        errors[key] = f"{label}必须是数字"
    elif integer and not number.is_integer():
        errors[key] = f"{label}必须是整数"
    elif (number < minimum) if inclusive else (number <= minimum):
        operator = "不小于" if inclusive else "大于"
        errors[key] = f"{label}必须{operator} {minimum:g}"
    elif number > maximum:
        errors[key] = f"{label}不能大于 {maximum:g}"


def validate_gui_mapping(
    values: Mapping[str, Any], station_names: Iterable[str], *, today: date | None = None
) -> dict[str, str]:
    """Return a ``{field_key: Chinese_error}`` mapping for GUI form values.

    ``station_names`` is supplied by the offline snapshot/cache loader.  The
    optional ``today`` exists solely for deterministic tests; callers normally
    omit it and use the local current date.
    """

    errors: dict[str, str] = {}
    today = today or date.today()
    known_stations = {str(name).strip() for name in station_names if str(name).strip()}

    from_station = str(_field_value(values, "from_station") or "").strip()
    to_station = str(_field_value(values, "to_station") or "").strip()
    if not from_station:
        errors["from_station"] = "请输入出发站"
    elif from_station not in known_stations:
        errors["from_station"] = "出发站不在当前站点列表中，请选择标准站名"
    if not to_station:
        errors["to_station"] = "请输入到达站"
    elif to_station not in known_stations:
        errors["to_station"] = "到达站不在当前站点列表中，请选择标准站名"
    if from_station and to_station and from_station == to_station:
        errors["to_station"] = "到达站不能与出发站相同"

    raw_date = str(_field_value(values, "train_date") or "").strip()
    try:
        journey_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
    except ValueError:
        errors["train_date"] = "请选择乘车日期，格式为 YYYY-MM-DD"
    else:
        if journey_date < today:
            errors["train_date"] = "乘车日期不能早于今天"

    for key, label in (("start_at", "开始时间"), ("stop_at", "停止时间")):
        raw_time = str(_field_value(values, key) or "").strip()
        if not raw_time:
            continue
        try:
            # GUI intentionally accepts daily time only; CLI retains absolute
            # datetime support in AppConfig.
            if not re.fullmatch(r"\d{2}:\d{2}:\d{2}", raw_time):
                raise ValueError
            datetime.strptime(raw_time, "%H:%M:%S")
        except ValueError:
            errors[key] = f"{label}应为 HH:MM:SS，或留空"

    passengers = _string_list(_field_value(values, "passenger_names"))
    passenger_count = len(passengers)
    auto_submit = bool(_field_value(values, "auto_submit"))
    if auto_submit and passenger_count == 0:
        errors["passenger_names"] = "自动提交时请填写 1 到 5 位乘车人"
    elif passenger_count > 5:
        errors["passenger_names"] = "请填写 1 到 5 位乘车人"
    elif len(set(passengers)) != passenger_count:
        errors["passenger_names"] = "乘车人不能重复"

    preferred = _string_list(_field_value(values, "preferred_trains"))
    invalid_train = next((train for train in preferred if not TRAIN_CODE_PATTERN.fullmatch(train.upper())), None)
    if invalid_train:
        errors["preferred_trains"] = f"车次“{invalid_train}”格式不正确，例如 G123"
    elif bool(_field_value(values, "only_preferred_trains")) and not preferred:
        errors["preferred_trains"] = "已选择“只尝试上述车次”，请至少填写一个车次"

    seat_types = _string_list(_field_value(values, "seat_types"))
    invalid_seats = [seat for seat in seat_types if seat not in SEAT_SPECS]
    if not seat_types:
        errors["seat_types"] = "请至少选择一种席别"
    elif invalid_seats:
        errors["seat_types"] = f"不支持的席别：{'、'.join(invalid_seats)}"

    if "seat_position_preferences" in values:
        raw_positions = values["seat_position_preferences"]
    elif "SEAT_POSITION_PREFERENCES" in values:
        raw_positions = values["SEAT_POSITION_PREFERENCES"]
    else:
        raw_positions = _field_value(values, "choose_seats")
    try:
        positions = SeatRelationPreference.from_value(raw_positions)
    except ValueError as exc:
        errors["seat_position_preferences"] = str(exc)
    else:
        if positions.enabled:
            if not 1 <= passenger_count <= 5:
                errors["seat_position_preferences"] = "设置座位关系前，请填写 1 到 5 位乘车人"
            else:
                try:
                    positions.validate(passenger_count)
                except ValueError as exc:
                    errors["seat_position_preferences"] = str(exc)
                else:
                    valid_codes = [SEAT_SPECS[label].submit_code for label in seat_types if label in SEAT_SPECS]
                    if not any(
                        set(positions.positions).issubset(seat_layout_positions(code, None))
                        for code in valid_codes
                        if seat_layout_positions(code, None)
                    ):
                        errors["seat_position_preferences"] = "座位关系与所选席别的 ABCDF 布局不兼容"

    raw_berths = _field_value(values, "berth_preference")
    try:
        berths = BerthPreference.from_value(raw_berths)
    except ValueError as exc:
        errors["berth_preference"] = str(exc)
    else:
        if berths.enabled:
            if not 1 <= passenger_count <= 5:
                errors["berth_preference"] = "设置铺位偏好前，请填写 1 到 5 位乘车人"
            else:
                try:
                    berths.validate(passenger_count)
                except ValueError as exc:
                    errors["berth_preference"] = str(exc)
                else:
                    configured_codes = {SEAT_SPECS[label].submit_code for label in seat_types if label in SEAT_SPECS}
                    if not configured_codes.intersection(BERTH_SEAT_TYPES):
                        errors["berth_preference"] = "铺位偏好需要至少选择一种卧铺席别"

    for key, label, minimum, maximum, inclusive, integer in (
        ("query_interval_seconds", "查询间隔", 0, 60, False, False),
        ("pre_query_seconds", "预查询提前量", 0, 60, True, False),
        ("hot_query_interval_seconds", "热点查询间隔", 0, 10, False, False),
        ("hot_window_seconds", "热点窗口", 0, 120, True, False),
        ("max_retries", "最大重试次数", 0, 100000, False, True),
        ("request_timeout_seconds", "请求超时", 0, 120, False, False),
        ("login_qr_timeout_seconds", "扫码超时", 0, 900, False, False),
        ("login_qr_poll_seconds", "扫码轮询间隔", 0, 10, False, False),
        ("time_sync_samples", "对时采样次数", 0, 30, False, True),
        ("time_sync_max_rtt_seconds", "最大对时往返延迟", 0, 10, False, False),
        ("order_wait_attempts", "排队查询次数", 0, 1000, False, True),
        ("order_wait_interval_seconds", "排队查询间隔", 0, 60, False, False),
        ("station_cache_days", "站点缓存天数", 1, 365, True, True),
    ):
        _validate_number(
            errors,
            values,
            key,
            label,
            minimum=minimum,
            maximum=maximum,
            inclusive=inclusive,
            integer=integer,
        )

    log_level = str(_field_value(values, "log_level") or "").upper()
    if log_level not in VALID_LOG_LEVELS:
        errors["log_level"] = "日志级别必须是 DEBUG、INFO、WARNING、ERROR 或 CRITICAL"
    return errors
