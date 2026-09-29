"""Offline field-validation coverage for the desktop form data layer."""

from __future__ import annotations

from datetime import date

from ticket_app.gui.settings import DEFAULT_VALUES
from ticket_app.gui.validation import validate_gui_mapping


STATIONS = {"北京西", "郑州东", "上海虹桥", "杭州东"}
TODAY = date(2026, 9, 8)


def valid_values() -> dict[str, object]:
    return {
        **DEFAULT_VALUES,
        "cart_items": [{"from_station": "北京西", "to_station": "郑州东",
                        "train_scope": "specific", "train_code": "G123", "seat_type": "二等座"}],
        "train_date": "2026-09-08",
        "passenger_names": ["张三", "李四"],
        "start_at": "10:00:00",
        "stop_at": "10:05:00",
    }


def validate(values: dict[str, object]) -> dict[str, str]:
    return validate_gui_mapping(values, STATIONS, today=TODAY)


def test_valid_gui_mapping_has_no_errors() -> None:
    assert validate(valid_values()) == {}


def test_station_date_and_gui_only_time_errors_are_field_oriented() -> None:
    values = valid_values()
    values.update(
        {
            "cart_items": [{**values["cart_items"][0], "from_station": "不存在站", "to_station": "不存在站"}],
            "train_date": "2026-09-07",
            "start_at": "2026-09-08 10:00:00",
            "stop_at": "25:00:00",
        }
    )
    errors = validate(values)
    assert "站点列表" in errors["cart_items"]
    assert "不能与出发站相同" in errors["cart_items"]
    assert "不能早于今天" in errors["train_date"]
    assert "HH:MM:SS" in errors["start_at"]
    assert "HH:MM:SS" in errors["stop_at"]


def test_passenger_train_and_seat_errors_are_reported_together() -> None:
    values = valid_values()
    values.update(
        {
            "passenger_names": ["张三", "张三"],
            "cart_items": [{**values["cart_items"][0], "train_code": "not-a-train", "seat_type": "不存在席别"}],
        }
    )
    errors = validate(values)
    assert errors["passenger_names"] == "乘车人不能重复"
    assert "合法车次" in errors["cart_items"]
    assert "不支持的席别" in errors["cart_items"]


def test_specific_cart_item_requires_train_and_people_are_limited_to_five() -> None:
    values = valid_values()
    values["cart_items"][0]["train_code"] = ""
    values["passenger_names"] = [str(number) for number in range(6)]
    errors = validate(values)
    assert "合法车次" in errors["cart_items"]
    assert "1 到 5" in errors["passenger_names"]


def test_seat_relation_and_berth_preferences_reuse_protocol_validators() -> None:
    values = valid_values()
    values.update(
        {
            "cart_items": [{**values["cart_items"][0], "seat_type": "商务座"}],
            "seat_position_preferences": ["1A", "1B"],
            "berth_preference": {"lower": 1, "middle": 0, "upper": 1},
        }
    )
    errors = validate(values)
    assert "ABCDF 布局不兼容" in errors["seat_position_preferences"]
    assert "berth_preference" not in errors  # Retained, inactive quantities do not block this seat order.

    values["seat_position_preferences"] = ["2A"]
    errors = validate(values)
    assert "格子数必须等于乘车人数" in errors["seat_position_preferences"]


def test_advanced_ranges_are_checked_offline() -> None:
    values = valid_values()
    values.update(
        {
            "query_interval_seconds": 0,
            "pre_query_seconds": -1,
            "hot_query_interval_seconds": "bad",
            "station_cache_days": 0,
            "log_level": "verbose",
        }
    )
    errors = validate(values)
    assert "必须大于" in errors["query_interval_seconds"]
    assert "不小于" in errors["pre_query_seconds"]
    assert "必须是数字" in errors["hot_query_interval_seconds"]
    assert "不小于 1" in errors["station_cache_days"]
    assert "日志级别" in errors["log_level"]
