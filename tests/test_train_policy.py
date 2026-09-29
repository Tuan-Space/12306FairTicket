"""Offline behavioral coverage for train scope, ordering and retained settings."""

from datetime import date, timedelta

import pytest

from ticket_app.configuration import AppConfig, AppError, SEAT_SPECS, preference_capabilities
from ticket_app.gui.settings import DEFAULT_VALUES, build_app_config
from ticket_app.gui.validation import validate_gui_mapping
from ticket_app.train_policy import (
    classify_train_codes,
    normalize_train_codes,
)


def config_values(*, seats=("二等座",), **updates):
    return {
        **DEFAULT_VALUES,
        "cart_items": [{"from_station": "北京西", "to_station": "郑州东", "train_scope": "all",
                        "train_code": "", "seat_type": seat} for seat in seats],
        "train_date": (date.today() + timedelta(days=1)).isoformat(),
        "passenger_names": ["测试甲"], **updates,
    }


@pytest.mark.parametrize(("raw", "expected"), [
    ("g1，d2、C3", "high_speed"),
    ("K123;Z99；T8\n1461", "conventional"),
    ("G9\tK20", "all"),
    (["1461"], "conventional"),
    ([], None), (";，\n\t", None), ("G", None),
    ("G1,not-a-train", None), (["G1；K2"], None),
])
def test_classification_uses_shared_list_parser_and_rejects_incomplete_inputs(raw, expected):
    assert classify_train_codes(raw) == expected


@pytest.mark.parametrize("separator", [",", "，", "、", ";", "；", "\n", "\r\n", "\t"])
def test_train_normalization_keeps_order_and_duplicates(separator):
    assert normalize_train_codes(f"{separator} d8 {separator}{separator} G9 {separator}d8") == ["D8", "G9", "D8"]


@pytest.mark.parametrize(("seats", "expected"), [
    (["二等座"], (True, False)), (["商务座"], (True, False)),
    (["硬卧", "软卧", "高级软卧"], (False, True)),
    (["硬座", "软座", "无座"], (False, False)),
    (["一等座", "软卧"], (True, True)),
])
def test_preference_capabilities_follow_seat_codes_not_train_prefix(seats, expected):
    assert preference_capabilities(seats) == expected


def test_inactive_preferences_preserve_values_and_revalidate_when_reactivated():
    values = config_values(seats=["硬座", "软座", "无座"],
                           seat_position_preferences=["1A", "1F"],
                           berth_preference={"lower": 2, "middle": 0, "upper": 0})
    assert validate_gui_mapping(values, {"北京西", "郑州东"}) == {}
    cfg = AppConfig.from_mapping(values)
    assert cfg.seat_relation_preference.positions == ("1A", "1F")
    assert cfg.berth_preference.lower == 2
    for seat, field in [("二等座", "seat_position_preferences"), ("软卧", "berth_preference")]:
        active = {**values, "cart_items": [{**values["cart_items"][0], "seat_type": seat}]}
        assert field in validate_gui_mapping(active, {"北京西", "郑州东"})
        with pytest.raises(AppError, match="必须等于乘车人数"):
            AppConfig.from_mapping(active)
    restored = {**values, "cart_items": [{**values["cart_items"][0], "seat_type": seat} for seat in ("二等座", "硬卧")], "passenger_names": ["甲", "乙"]}
    assert validate_gui_mapping(restored, {"北京西", "郑州东"}) == {}
    assert AppConfig.from_mapping(restored).berth_preference.lower == 2
