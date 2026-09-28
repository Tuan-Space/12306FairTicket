"""Cart schema, privacy, legacy conversion and CLI compatibility, offline."""

from datetime import date, timedelta
import json

import pytest

from ticket_app.cart import (
    CartItem, cart_migration_messages, cart_seat_types, migrate_legacy_cart,
    normalize_cart_items, serialize_cart_items, validate_cart_items,
)
from ticket_app.configuration import AppConfig, AppError, SEAT_SPECS
from ticket_app.gui.compat import DEFAULT_VALUES, build_app_config, load_gui_settings, save_gui_settings
from ticket_app.gui.validation import validate_gui_mapping


def item(train="G1", seat="二等座", origin="北京南", destination="上海虹桥"):
    return CartItem(origin, destination, "specific" if train else "all", train, seat)


def values(**changes):
    return {
        **DEFAULT_VALUES, "train_date": str(date.today() + timedelta(days=1)),
        "passenger_names": ["甲"], "preferred_trains": ["G1"],
        "seat_types": ["二等座"], **changes,
    }


def test_cart_config_uses_only_cart_route_and_seat_union():
    rows = [item(), item("G2", "一等卧", "北京", "上海"), item("", "无座")]
    cfg = AppConfig.from_mapping(values(
        cart_items=rows, from_station="", to_station="", preferred_trains=["unfinished"],
        seat_types=["不参与任务的草稿"], priority_strategy="unused", empty_train_scope=None,
    ))
    assert cfg.cart_items == rows
    assert cfg.seat_types == ["二等座", "一等卧", "无座"]
    assert cfg.from_station == "北京南"
    assert AppConfig.from_mapping(cfg.to_mapping()).cart_items == rows


def test_cli_omitted_cart_keeps_legacy_policy_and_explicit_empty_never_falls_back():
    cfg = AppConfig.from_mapping(values(priority_strategy="seat_first"))
    assert cfg.cart_items is None
    assert cfg.priority_strategy == "seat_first"
    assert "CART_ITEMS" not in cfg.to_mapping()
    with pytest.raises(AppError, match="至少加入"):
        AppConfig.from_mapping(values(cart_items=[]))
    assert AppConfig.from_mapping(values(CART_ITEMS=[item().to_mapping()])).cart_items == [item()]


@pytest.mark.parametrize("seat", list(SEAT_SPECS))
def test_all_existing_seats_are_cart_choices(seat):
    assert validate_cart_items([item(seat=seat)], SEAT_SPECS) == []


def test_invalid_items_are_retained_as_drafts_and_reported_with_row_number():
    rows = normalize_cart_items([item().to_mapping(), {**item().to_mapping(), "train_code": "G1，D2"}])
    assert len(rows) == 2
    assert "第 2 项" in validate_cart_items(rows, SEAT_SPECS)[0]
    assert "完全重复" in validate_cart_items([item(), item()], SEAT_SPECS)[0]
    assert "不能同时指定" in validate_cart_items([CartItem("北京", "上海", "all", "G1", "二等座")], SEAT_SPECS)[0]
    assert "标准站名" in validate_cart_items([item()], SEAT_SPECS, ["北京", "上海"])[0]


def test_gui_validation_ignores_unadded_draft_but_checks_cart_and_active_preferences():
    cfg = values(cart_items=[item(seat="硬座")], from_station="未知", to_station="", preferred_trains="?")
    cfg["seat_position_preferences"] = ["1A", "1F"]
    cfg["berth_preference"] = {"lower": 4, "middle": 0, "upper": 0}
    assert validate_gui_mapping(cfg, {"北京南", "上海虹桥"}) == {}
    cfg["cart_items"] = [item(), item(seat="二等卧")]
    errors = validate_gui_mapping(cfg, {"北京南", "上海虹桥"})
    assert set(errors) == {"seat_position_preferences", "berth_preference"}


@pytest.mark.parametrize(("strategy", "only", "expected"), [
    ("train_first", True, [("D2", "二等座"), ("D2", "一等座"), ("G1", "二等座"), ("G1", "一等座")]),
    ("seat_first", True, [("D2", "二等座"), ("G1", "二等座"), ("D2", "一等座"), ("G1", "一等座")]),
    ("seat_first", False, [("D2", "二等座"), ("G1", "二等座"), ("", "二等座"), ("D2", "一等座"), ("G1", "一等座"), ("", "一等座")]),
    ("train_first", False, [("D2", "二等座"), ("D2", "一等座"), ("G1", "二等座"), ("G1", "一等座"), ("", "二等座"), ("", "一等座")]),
])
def test_legacy_priority_expansion_is_exact_and_warns_when_order_changes(strategy, only, expected):
    migrated = migrate_legacy_cart(values(preferred_trains="d2，g1", seat_types=["二等座", "一等座"],
                                          priority_strategy=strategy, only_preferred_trains=only))
    assert [(row["train_code"], row["seat_type"]) for row in migrated["cart_items"]] == expected
    notes, issues = cart_migration_messages(migrated["cart_migration"])
    assert bool(notes) == (strategy == "train_first" and not only)
    assert not issues


@pytest.mark.parametrize("version", [1, 2, 3, 4])
@pytest.mark.parametrize("scope", ["high_speed", "conventional", None])
def test_legacy_restriction_blocks_and_survives_v5_round_trip(tmp_path, version, scope):
    path = tmp_path / "old.json"
    raw = values(preferred_trains=[], only_preferred_trains=False, empty_train_scope=scope)
    path.write_text(json.dumps({"version": version, "values" if version == 1 else "settings": raw}), encoding="utf-8")
    imported = load_gui_settings(path)
    assert cart_migration_messages(imported["cart_migration"])[1]
    with pytest.raises(AppError):
        build_app_config(imported)
    save_gui_settings(path, imported)
    again = load_gui_settings(path)
    assert again["cart_migration"] == imported["cart_migration"]
    assert "cart_items" in validate_gui_mapping(again, {"北京西", "郑州东"})
    again["cart_migration"]["issues"] = []  # Explicit UI correction acknowledgment.
    assert build_app_config(again).cart_items[0].train_scope == "all"


def test_legacy_empty_specific_and_unknown_seat_are_never_silently_accepted(tmp_path):
    converted = migrate_legacy_cart(values(preferred_trains=[], seat_types=["新未知席别"]))
    assert converted["cart_items"][0]["seat_type"] == "新未知席别"
    assert "legacy_only_empty" in converted["cart_migration"]["issues"]
    with pytest.raises(AppError):
        build_app_config({**values(), **converted})


def test_v5_save_is_private_minimal_and_preserves_atomic_order(tmp_path):
    path = tmp_path / "cart.json"
    rows = [item("G1").to_mapping(), item("", "一等卧", "北京", "上海").to_mapping()]
    rows[0].update(secret_str="DO-NOT-SAVE", passenger_id_no="DO-NOT-SAVE")
    save_gui_settings(path, values(cart_items=rows, cookie="DO-NOT-SAVE"))
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["version"] == 5
    assert doc["settings"]["cart_items"] == serialize_cart_items(rows)
    assert "DO-NOT-SAVE" not in path.read_text(encoding="utf-8")
    assert not ({"from_station", "to_station", "seat_types", "preferred_trains", "priority_strategy", "empty_train_scope", "only_preferred_trains"} & doc["settings"].keys())
    loaded = load_gui_settings(path)
    assert cart_seat_types(normalize_cart_items(loaded["cart_items"])) == ["二等座", "一等卧"]
    assert build_app_config(loaded).cart_items == normalize_cart_items(rows)


def test_empty_cart_draft_saves_but_cannot_start_and_missing_v5_cart_is_rejected(tmp_path):
    path = tmp_path / "draft.json"
    save_gui_settings(path, values(cart_items=[]))
    assert load_gui_settings(path)["cart_items"] == []
    with pytest.raises(AppError, match="至少加入"):
        build_app_config(load_gui_settings(path))
    path.write_text('{"version":5,"settings":{}}', encoding="utf-8")
    with pytest.raises(AppError, match="cart_items"):
        load_gui_settings(path)


def test_old_schema_ignores_injected_future_cart_fields_before_migration(tmp_path):
    path = tmp_path / "old.json"
    old = values(cart_items="not-a-valid-future-field", cart_migration={"issues": ["unknown"]})
    path.write_text(json.dumps({"version": 4, "settings": old}), encoding="utf-8")
    loaded = load_gui_settings(path)
    assert loaded["cart_items"] == [CartItem("北京西", "郑州东", "specific", "G1", "二等座").to_mapping()]
    assert loaded["cart_migration"] == {"notes": [], "issues": []}
