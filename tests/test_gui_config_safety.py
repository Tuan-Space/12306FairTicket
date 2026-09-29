"""Privacy, atomic saves and strict current-schema GUI configuration tests."""

import json
from pathlib import Path

import pytest

from ticket_app.configuration import AppError
from ticket_app.gui.settings import (
    DEFAULT_VALUES, EDITABLE_SETTINGS_KEYS, build_app_config,
    canonical_mapping, load_gui_settings, save_gui_settings,
)


def cart_settings():
    return {**DEFAULT_VALUES, "cart_items": [{
        "from_station": "上海虹桥", "to_station": "杭州东", "train_scope": "specific",
        "train_code": "G123", "seat_type": "二等座",
    }], "passenger_names": ["张三", "Mary Ann"], "seat_position_preferences": ["1A", "1F"]}


def test_current_settings_roundtrip_preserves_order_without_private_or_removed_fields(tmp_path):
    values = {**cart_settings(), "cart_migration": {"notes": ["train_first_any_multi_seat"], "issues": []},
              "cookie": "secret-cookie", "token": "secret-token", "passenger_id_no": "secret-identity",
              "session_file": "secret-path", "persist_session": True,
              "from_station": "旧单站", "preferred_trains": ["K1"], "priority_strategy": "seat_first",
              "CHOOSE_SEATS": "1B", "config_path": "secret-config"}
    path = tmp_path / "journey.json"
    save_gui_settings(path, values)
    text = path.read_text(encoding="utf-8")
    settings = json.loads(text)["settings"]
    assert set(settings) == EDITABLE_SETTINGS_KEYS
    assert "secret" not in text
    assert "旧单站" not in text
    restored = load_gui_settings(path)
    assert restored["cart_items"] == values["cart_items"]
    assert restored["passenger_names"] == ["张三", "Mary Ann"]
    assert restored["seat_position_preferences"] == ["1A", "1F"]
    assert restored["persist_session"] is False
    assert "cart_migration" not in restored
    assert "choose_seats" not in restored


@pytest.mark.parametrize("version", [1, 2, 3, 4, 6, True, "5", None])
def test_only_v5_json_is_accepted_without_executing_python(tmp_path, version):
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"version": version, "settings": cart_settings(), "values": cart_settings()}), encoding="utf-8")
    with pytest.raises(AppError, match="version|版本"):
        load_gui_settings(path)
    marker = tmp_path / "executed"
    executable = tmp_path / "config.py"
    executable.write_text(f"__import__('pathlib').Path({str(marker)!r}).touch()", encoding="utf-8")
    with pytest.raises(AppError, match="仅支持 JSON"):
        load_gui_settings(executable)
    assert not marker.exists()


@pytest.mark.parametrize("metadata", [
    {"issues": ["empty_scope_high_speed"], "notes": []},
    {"issues": "bad"}, {"issues": None}, {"notes": "bad"}, {"pending": True}, [], "bad", True,
])
def test_pending_or_malformed_migration_cannot_be_silently_discarded(tmp_path, metadata):
    values = {**cart_settings(), "cart_migration": metadata}
    path = tmp_path / "pending.json"
    path.write_text(json.dumps({"version": 5, "settings": values}), encoding="utf-8")
    with pytest.raises(AppError, match="购物车|限制"):
        load_gui_settings(path)
    with pytest.raises(AppError, match="购物车|限制"):
        save_gui_settings(tmp_path / "output.json", values)


@pytest.mark.parametrize("cart", [None, "bad", {}, False])
def test_v5_requires_cart_list(tmp_path, cart):
    path = tmp_path / "cart.json"
    path.write_text(json.dumps({"version": 5, "settings": {"cart_items": cart}}), encoding="utf-8")
    with pytest.raises(AppError):
        load_gui_settings(path)
    path.write_text(json.dumps({"version": 5, "settings": {}}), encoding="utf-8")
    with pytest.raises(AppError, match="cart_items"):
        load_gui_settings(path)


def test_empty_cart_draft_saves_but_cannot_start(tmp_path):
    path = tmp_path / "draft.json"
    save_gui_settings(path, {"cart_items": []})
    restored = load_gui_settings(path)
    assert restored["cart_items"] == []
    with pytest.raises(AppError, match="购物车"):
        build_app_config(restored)


def test_failed_atomic_replace_preserves_previous_file(tmp_path, monkeypatch):
    from ticket_app.gui import settings
    path = tmp_path / "journey.json"
    original = '{"previous": true}\n'
    path.write_text(original, encoding="utf-8")
    def fail_replace(*_args):
        raise OSError("disk unavailable")
    monkeypatch.setattr(settings.os, "replace", fail_replace)
    with pytest.raises(OSError, match="disk unavailable"):
        save_gui_settings(path, cart_settings())
    assert path.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob(".journey.json.*.tmp"))


def test_choose_seats_is_not_interpreted():
    result = canonical_mapping({"cart_items": [], "CHOOSE_SEATS": "1A1F"})
    assert result["seat_position_preferences"] == []
    assert "choose_seats" not in result
