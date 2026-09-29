"""Cart schema, privacy and strict current-version configuration, offline."""

from datetime import date, timedelta
import json

import pytest

from ticket_app.cart import (
    CartItem, reject_unresolved_cart_migration, cart_seat_types,
    normalize_cart_items, serialize_cart_items, validate_cart_items,
)
from ticket_app.configuration import AppConfig, AppError, SEAT_SPECS
from ticket_app.gui.settings import DEFAULT_VALUES, build_app_config, load_gui_settings, save_gui_settings
from ticket_app.gui.validation import validate_gui_mapping


def item(train="G1", seat="二等座", origin="北京南", destination="上海虹桥"):
    return CartItem(origin, destination, "specific" if train else "all", train, seat)


def values(**changes):
    return {
        **DEFAULT_VALUES, "train_date": str(date.today() + timedelta(days=1)),
        "passenger_names": ["甲"], "cart_items": [item().to_mapping()], **changes,
    }


def test_cart_config_uses_only_cart_route_and_seat_union():
    rows = [item(), item("G2", "一等卧", "北京", "上海"), item("", "无座")]
    cfg = AppConfig.from_mapping(values(cart_items=rows))
    assert cfg.cart_items == rows
    assert cfg.seat_types == ["二等座", "一等卧", "无座"]
    assert not hasattr(cfg, "from_station")
    assert AppConfig.from_mapping(cfg.to_mapping()).cart_items == rows


@pytest.mark.parametrize("missing", [None, []])
def test_cli_requires_nonempty_cart(missing):
    with pytest.raises(AppError, match="CART_ITEMS|至少加入"):
        AppConfig.from_mapping(values(cart_items=missing))
    assert AppConfig.from_mapping(values(CART_ITEMS=[item().to_mapping()])).cart_items == [item()]


@pytest.mark.parametrize("key", ["FROM_STATION", "TO_STATION", "SEAT_TYPES", "PREFERRED_TRAINS",
                                "ONLY_PREFERRED_TRAINS", "PRIORITY_STRATEGY", "EMPTY_TRAIN_SCOPE",
                                "CHOOSE_SEATS", "SEAT_RELATION_PREFERENCE"])
def test_removed_cli_fields_are_rejected_even_with_a_cart(key):
    with pytest.raises(AppError, match="已移除旧配置项"):
        AppConfig.from_mapping(values(**{key: "unused"}))


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


@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_old_configuration_versions_are_rejected(tmp_path, version):
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"version": version, "settings": values()}), encoding="utf-8")
    with pytest.raises(AppError):
        load_gui_settings(path)


@pytest.mark.parametrize("metadata", [{"issues": ["legacy_scope_high_speed"]},
                                     {"issues": ["future_restriction"]},
                                     {"issues": "broken"}, ["broken"]])
def test_v5_unresolved_restrictions_are_rejected_by_cli_and_import(tmp_path, metadata):
    with pytest.raises(ValueError, match="重新建立购物车"):
        reject_unresolved_cart_migration(metadata)
    with pytest.raises(AppError, match="重新建立购物车"):
        AppConfig.from_mapping(values(cart_migration=metadata))
    path = tmp_path / "restricted.json"
    path.write_text(json.dumps({"version": 5, "settings": values(cart_migration=metadata)}), encoding="utf-8")
    with pytest.raises(AppError, match="重新建立购物车"):
        load_gui_settings(path)


def test_resolved_preview_notes_are_ignored_and_not_saved(tmp_path):
    path = tmp_path / "notes.json"
    raw = values(cart_migration={"notes": ["train_first_any_multi_seat"], "issues": []})
    path.write_text(json.dumps({"version": 5, "settings": raw}), encoding="utf-8")
    loaded = load_gui_settings(path)
    assert build_app_config(loaded).cart_items == [item()]
    save_gui_settings(path, loaded)
    assert "cart_migration" not in json.loads(path.read_text(encoding="utf-8"))["settings"]


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
