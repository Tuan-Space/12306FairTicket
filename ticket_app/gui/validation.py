"""Pure, field-oriented validation for the desktop configuration form.

Unlike :meth:`AppConfig.validate`, this module deliberately returns every
actionable error in one pass so the UI can mark each corresponding widget.  It
does not create clients, load station data, or perform any network operation.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Iterable, Mapping

from ticket_app.cart import cart_seat_types, normalize_cart_items, validate_cart_items
from ticket_app.configuration import SEAT_SPECS, preference_capabilities
from ticket_app.input_parsing import split_multi_value_text
from ticket_app.passengers import normalize_passenger_ticket_types
from ticket_app.preferences import (
    BerthPreference,
    SeatRelationPreference,
    seat_layout_positions,
)

from .settings import DEFAULT_VALUES


VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


def _field_value(values: Mapping[str, Any], key: str) -> Any:
    """Read current snake-case or uppercase setting names."""

    if key in values:
        return values[key]
    return values.get(key.upper(), DEFAULT_VALUES[key])


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return split_multi_value_text(value)
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

    cart_items = []
    try:
        cart_items = normalize_cart_items(_field_value(values, "cart_items"))
        cart_errors = validate_cart_items(cart_items, SEAT_SPECS, known_stations)
        if cart_errors:
            errors["cart_items"] = "\n".join(cart_errors)
    except ValueError as exc:
        errors["cart_items"] = str(exc)

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
    try:
        ticket_types = normalize_passenger_ticket_types(_field_value(values, "passenger_ticket_types"))
    except ValueError as exc:
        errors["passenger_ticket_types"] = str(exc)
    else:
        if set(ticket_types) - set(passengers):
            errors["passenger_ticket_types"] = "票种设置包含未选择的乘车人，请重新设置"
    if not isinstance(_field_value(values, "quiet_carriage_preference"), bool):
        errors["quiet_carriage_preference"] = "静音车厢偏好必须是开关值"

    seat_types = cart_seat_types(cart_items)
    has_seats, has_berths = preference_capabilities(seat_types)
    raw_positions = _field_value(values, "seat_position_preferences")
    try:
        positions = SeatRelationPreference.from_value(raw_positions)
    except ValueError as exc:
        errors["seat_position_preferences"] = str(exc)
    else:
        if positions.enabled and has_seats:
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
        if berths.enabled and has_berths:
            if not 1 <= passenger_count <= 5:
                errors["berth_preference"] = "设置铺位偏好前，请填写 1 到 5 位乘车人"
            else:
                try:
                    berths.validate(passenger_count)
                except ValueError as exc:
                    errors["berth_preference"] = str(exc)

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
