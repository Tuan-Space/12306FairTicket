"""Offline behavioral coverage for train scope, ordering and retained settings."""

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from ticket_app.configuration import AppConfig, AppError, SEAT_SPECS, preference_capabilities
from ticket_app.gui.compat import DEFAULT_VALUES, build_app_config
from ticket_app.gui.validation import validate_gui_mapping
from ticket_app.runner import TicketRunner
from ticket_app.train_policy import (
    classify_train_codes,
    effective_train_scope,
    normalize_train_codes,
    priority_preview,
    scope_summary,
    train_in_scope,
    validate_train_policy,
)


def config_values(**updates):
    values = {
        **DEFAULT_VALUES,
        "from_station": "北京西",
        "to_station": "郑州东",
        "train_date": (date.today() + timedelta(days=1)).isoformat(),
        "passenger_names": ["测试甲"],
        "only_preferred_trains": False,
        "seat_types": ["二等座"],
    }
    values.update(updates)
    return values


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


@pytest.mark.parametrize(("preferred", "only", "scope", "expected"), [
    (["G9"], True, "conventional", "exact"),
    (["G9"], False, "high_speed", "all"),
    (["K9"], False, None, "all"),
    ([], False, "high_speed", "high_speed"),
    ([], False, "conventional", "conventional"),
    ([], False, "all", "all"),
    ([], False, None, None),
])
def test_effective_scope_distinguishes_guidance_from_filter(preferred, only, scope, expected):
    assert effective_train_scope(preferred, only, scope) == expected
    if preferred and not only:
        assert train_in_scope("G8", preferred, only, scope)
        assert train_in_scope("K8", preferred, only, scope)
        assert "其他类型列车" in scope_summary(preferred, only, scope)


@pytest.mark.parametrize(("scope", "accepted"), [
    ("high_speed", ["G1", "D2", "C3"]),
    ("conventional", ["K4", "Z5", "1461"]),
    ("all", ["G1", "D2", "C3", "K4", "Z5", "1461"]),
    (None, []),
])
def test_family_filter_handles_emu_and_numeric_services(scope, accepted):
    trains = ["G1", "D2", "C3", "K4", "Z5", "1461"]
    assert [train for train in trains if train_in_scope(train, [], False, scope)] == accepted


def test_invalid_policy_is_field_oriented_for_gui_and_rejected_by_cli():
    assert "preferred_trains" in validate_train_policy([], True)
    assert "empty_train_scope" in validate_train_policy([], False, None)
    assert validate_train_policy(["D9"], False, None) == {}
    for strategy in [[], {}, 1, None, "invalid"]:
        assert "priority_strategy" in validate_train_policy(["D9"], False, "all", strategy)
        assert "完善" in priority_preview(["D9"], ["二等座"], strategy)
    for scope in [[], {}, 1, "invalid"]:
        assert "empty_train_scope" in validate_train_policy([], False, scope)
    for updates in [
        {"only_preferred_trains": True},
        {"priority_strategy": "other"},
        {"preferred_trains": "G1；bad-code"},
    ]:
        values = config_values(**updates)
        assert validate_gui_mapping(values, {"北京西", "郑州东"})
        with pytest.raises(AppError):
            AppConfig.from_mapping(values)
        with pytest.raises(AppError):
            build_app_config(values)


@pytest.mark.parametrize("scope", [None, "high_speed", "conventional", "all"])
def test_gui_scope_is_always_all_while_cli_retains_its_range(scope):
    values = config_values(empty_train_scope=scope)
    assert validate_gui_mapping(values, {"北京西", "郑州东"}) == {}
    assert build_app_config(values).empty_train_scope == "all"
    if scope is None:
        with pytest.raises(AppError):
            AppConfig.from_mapping(values)
    else:
        assert AppConfig.from_mapping(values).empty_train_scope == scope


def make_candidates(strategy, preferred=("G2", "G1"), only=False, scope="all"):
    runner = object.__new__(TicketRunner)
    runner.cfg = SimpleNamespace(
        preferred_trains=list(preferred), only_preferred_trains=only,
        priority_strategy=strategy, empty_train_scope=scope,
    )
    runner.seat_sequence = [(label, SEAT_SPECS[label]) for label in ["二等座", "硬卧", "无座"]]
    tickets = [
        {"station_train_code": "K3", "can_buy": True, "seats": {"yw": "有", "wz": "2"}},
        {"station_train_code": "G1", "can_buy": True, "seats": {"edz": "有", "wz": "1"}},
        {"station_train_code": "G2", "can_buy": True, "seats": {"edz": "无", "yw": "1", "wz": "有"}},
        {"station_train_code": "1461", "can_buy": True, "seats": {"yw": "1", "wz": "0"}},
        {"station_train_code": "D8", "can_buy": False, "seats": {"yw": "有"}},
    ]
    return runner._find_candidates(tickets)


@pytest.mark.parametrize(("strategy", "expected"), [
    ("train_first", [
        ("G2", "硬卧"), ("G2", "无座"), ("G1", "二等座"), ("G1", "无座"),
        ("1461", "硬卧"), ("K3", "硬卧"), ("K3", "无座"),
    ]),
    ("seat_first", [
        ("G1", "二等座"), ("G2", "硬卧"), ("1461", "硬卧"), ("K3", "硬卧"),
        ("G2", "无座"), ("G1", "无座"), ("K3", "无座"),
    ]),
])
def test_complete_candidate_order_uses_mock_stock_and_allows_emu_berths(strategy, expected):
    candidates = make_candidates(strategy)
    assert [(item["ticket"]["station_train_code"], item["seat_label"]) for item in candidates] == expected
    standing = {item["ticket"]["station_train_code"]: item["seat_type"] for item in candidates if item["seat_label"] == "无座"}
    assert standing == {"G1": "O", "G2": "O", "K3": "1"}


def test_candidate_filter_applies_exact_list_or_empty_scope():
    exact = make_candidates("seat_first", only=True)
    assert {item["ticket"]["station_train_code"] for item in exact} == {"G1", "G2"}
    ordinary = make_candidates("seat_first", preferred=[], scope="conventional")
    assert [(item["ticket"]["station_train_code"], item["seat_label"]) for item in ordinary] == [
        ("1461", "硬卧"), ("K3", "硬卧"), ("K3", "无座"),
    ]


def test_preview_uses_the_same_order_and_distinguishes_examples():
    seats = ["二等座", "硬卧"]
    first = priority_preview(["G2", "G1"], seats, "train_first", True)
    seat = priority_preview(["G2", "G1"], seats, "seat_first", True)
    assert "G2 二等座 → G2 硬卧 → G1 二等座 → G1 硬卧" in first
    assert "G2 二等座 → G1 二等座 → G2 硬卧 → G1 硬卧" in seat
    assert "其他车次" in priority_preview(["G2"], seats, "seat_first", False)
    assert "（示例）" in priority_preview([], seats, empty_train_scope="high_speed")
    assert "跳过无票候选" in first


@pytest.mark.parametrize(("seats", "expected"), [
    (["二等座"], (True, False)), (["商务座"], (True, False)),
    (["硬卧", "软卧", "高级软卧"], (False, True)),
    (["硬座", "软座", "无座"], (False, False)),
    (["一等座", "软卧"], (True, True)),
])
def test_preference_capabilities_follow_seat_codes_not_train_prefix(seats, expected):
    assert preference_capabilities(seats) == expected


def test_inactive_preferences_preserve_values_and_revalidate_when_reactivated():
    values = config_values(seat_types=["硬座", "软座", "无座"],
                           seat_position_preferences=["1A", "1F"],
                           berth_preference={"lower": 2, "middle": 0, "upper": 0})
    assert validate_gui_mapping(values, {"北京西", "郑州东"}) == {}
    cfg = AppConfig.from_mapping(values)
    assert cfg.seat_relation_preference.positions == ("1A", "1F")
    assert cfg.berth_preference.lower == 2
    for seat, field in [("二等座", "seat_position_preferences"), ("软卧", "berth_preference")]:
        active = {**values, "seat_types": [seat]}
        assert field in validate_gui_mapping(active, {"北京西", "郑州东"})
        with pytest.raises(AppError, match="必须等于乘车人数"):
            AppConfig.from_mapping(active)
    restored = {**values, "seat_types": ["二等座", "硬卧"], "passenger_names": ["甲", "乙"]}
    assert validate_gui_mapping(restored, {"北京西", "郑州东"}) == {}
    assert AppConfig.from_mapping(restored).berth_preference.lower == 2
