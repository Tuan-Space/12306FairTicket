"""Offline tests for GUI JSON settings and explicit station refreshes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ticket_app.configuration import AppError, STATION_URL
from ticket_app.gui.compat import (
    DEFAULT_VALUES,
    EDITABLE_SETTINGS_KEYS,
    GUI_CONFIG_VERSION,
    bundled_station_names,
    cached_station_names,
    load_gui_settings,
    save_gui_settings,
)
from ticket_app.gui.station_worker import StationRefreshWorker, refresh_station_cache


def test_v3_round_trip_contains_all_editable_settings_and_no_sensitive_values(tmp_path: Path) -> None:
    target = tmp_path / "travel.json"
    values = {
        **DEFAULT_VALUES,
        "from_station": "上海虹桥",
        "to_station": "杭州东",
        "request_timeout_seconds": 13.5,
        "cookie": "do-not-save-cookie",
        "token": "do-not-save-token",
        "passenger_id_no": "do-not-save-identity",
        "session_file": "do-not-save-session-path",
        "station_cache_file": "do-not-save-station-path",
        "qr_code_file": "do-not-save-qr-path",
        "persist_session": True,
        "not_a_gui_setting": "ignored",
    }

    save_gui_settings(target, values)

    document = json.loads(target.read_text(encoding="utf-8"))
    assert not list(tmp_path.glob(".travel.json.*.tmp"))
    assert document["version"] == GUI_CONFIG_VERSION
    assert set(document["settings"]) == set(EDITABLE_SETTINGS_KEYS)
    serialized = target.read_text(encoding="utf-8")
    for secret in ("do-not-save-cookie", "do-not-save-token", "do-not-save-identity", "do-not-save-session-path"):
        assert secret not in serialized

    restored = load_gui_settings(target)
    assert restored["from_station"] == "上海虹桥"
    assert restored["request_timeout_seconds"] == 13.5
    assert restored["persist_session"] is False
    assert restored["session_file"] == DEFAULT_VALUES["session_file"]


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_migration_preserves_order_preferences_and_compatibility_defaults(tmp_path: Path, version: int) -> None:
    legacy = tmp_path / "legacy.json"
    settings = {
        "passenger_names": ["测试甲", "Mary Ann"],
        "preferred_trains": ["D9", "K1"],
        "seat_types": ["软卧", "二等座", "硬座"],
        "only_preferred_trains": False,
        "seat_position_preferences": ["1A", "1F"],
        "berth_preference": {"lower": 2, "middle": 0, "upper": 0},
    }
    legacy.write_text(json.dumps({"version": version, "values" if version == 1 else "settings": settings}), encoding="utf-8")
    loaded = load_gui_settings(legacy)
    for key, value in settings.items():
        assert loaded[key] == value
    assert loaded["empty_train_scope"] == "all"
    assert loaded["priority_strategy"] == "train_first"
    new_file = tmp_path / "migrated.json"
    save_gui_settings(new_file, loaded)
    assert json.loads(new_file.read_text(encoding="utf-8"))["version"] == 3
    assert load_gui_settings(new_file) == loaded


@pytest.mark.parametrize("scope", [None, "high_speed", "conventional", "all"])
def test_v3_save_normalizes_removed_scope_and_keeps_priority_strategy(tmp_path: Path, scope: str | None) -> None:
    path = tmp_path / "draft.json"
    save_gui_settings(path, {**DEFAULT_VALUES, "empty_train_scope": scope, "priority_strategy": "seat_first"})
    restored = load_gui_settings(path)
    assert restored["empty_train_scope"] == "all"
    assert restored["priority_strategy"] == "seat_first"


@pytest.mark.parametrize(("key", "value"), [
    ("empty_train_scope", []), ("empty_train_scope", {}), ("empty_train_scope", True),
    ("priority_strategy", []), ("priority_strategy", {}), ("priority_strategy", None),
])
def test_v3_policy_rejects_malformed_types_without_partial_import(tmp_path: Path, key: str, value) -> None:
    path = tmp_path / "malformed-policy.json"
    path.write_text(json.dumps({"version": 3, "settings": {key: value}}), encoding="utf-8")
    with pytest.raises(AppError, match="字段类型无效"):
        load_gui_settings(path)


def test_v1_export_imports_and_unknown_v2_keys_are_ignored(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.json"
    legacy.write_text(
        json.dumps({"version": 1, "name": "old", "values": {"from_station": "上海", "secret": "x"}}),
        encoding="utf-8",
    )
    assert load_gui_settings(legacy)["from_station"] == "上海"

    current = tmp_path / "current.json"
    current.write_text(
        json.dumps({"version": 2, "settings": {"to_station": "北京", "unknown": "x", "token": "secret"}}),
        encoding="utf-8",
    )
    restored = load_gui_settings(current)
    assert restored["to_station"] == "北京"
    assert "unknown" not in restored
    assert "token" not in restored


def test_gui_json_import_rejects_non_json_and_oversized_files(tmp_path: Path) -> None:
    py_file = tmp_path / "config.py"
    py_file.write_text("FROM_STATION = '上海'", encoding="utf-8")
    with pytest.raises(AppError, match="仅支持 JSON"):
        load_gui_settings(py_file)

    oversized = tmp_path / "large.json"
    oversized.write_bytes(b" " * 1_000_001)
    with pytest.raises(AppError, match="超过 1 MB"):
        load_gui_settings(oversized)


def test_gui_json_rejects_invalid_numeric_structure_but_not_value_ranges(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed.json"
    malformed.write_text(
        json.dumps({"version": 2, "settings": {"query_interval_seconds": "not-a-number"}}),
        encoding="utf-8",
    )
    with pytest.raises(AppError, match="字段类型无效"):
        load_gui_settings(malformed)

    # A negative interval is structurally valid JSON and stays importable as a
    # draft; field-level UI validation is responsible for marking its range.
    draft = tmp_path / "draft.json"
    save_gui_settings(draft, {**DEFAULT_VALUES, "query_interval_seconds": -1})
    assert load_gui_settings(draft)["query_interval_seconds"] == -1.0


def test_station_names_use_bundled_snapshot_and_latest_cache_without_network(tmp_path: Path) -> None:
    cache = tmp_path / "stations.json"
    cache.write_text(json.dumps({"stations": {"缓存测试站": "ABC"}}), encoding="utf-8")

    assert "北京西" in bundled_station_names()
    names = cached_station_names(cache)
    assert "缓存测试站" in names
    assert "北京西" in names


def test_explicit_station_refresh_uses_injected_fetcher_and_atomic_cache(tmp_path: Path) -> None:
    cache = tmp_path / "stations.json"
    calls: list[tuple[str, float]] = []

    def fetcher(url: str, timeout: float) -> str:
        calls.append((url, timeout))
        return "var station_names ='@bjb|北京|BJP|beijing|bj|0@shh|上海|SHH|shanghai|sh|0';"

    stations = refresh_station_cache(cache_path=cache, timeout_seconds=3.0, fetcher=fetcher)

    assert calls == [(STATION_URL, 3.0)]
    assert stations == {"北京": "BJP", "上海": "SHH"}
    assert json.loads(cache.read_text(encoding="utf-8"))["stations"] == stations
    assert not list(tmp_path.glob(".stations.json.*.tmp"))


def test_station_refresh_failure_leaves_cache_untouched_and_worker_reports_error(tmp_path: Path) -> None:
    cache = tmp_path / "stations.json"
    original = '{"stations":{"旧站":"OLD"}}\n'
    cache.write_text(original, encoding="utf-8")

    def failing_fetcher(_url: str, _timeout: float) -> str:
        raise RuntimeError("offline")

    with pytest.raises(AppError, match="更新站点失败"):
        refresh_station_cache(cache_path=cache, fetcher=failing_fetcher)
    assert cache.read_text(encoding="utf-8") == original

    results: list[tuple[object, object]] = []
    worker = StationRefreshWorker(cache_path=cache, fetcher=failing_fetcher, callback=lambda stations, error: results.append((stations, error)))
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert worker.stations is None
    assert isinstance(worker.error, AppError)
    assert len(results) == 1
