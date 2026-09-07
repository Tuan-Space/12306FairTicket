"""Security and compatibility regression tests for GUI-owned configuration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ticket_app.configuration import AppError
from ticket_app.gui.compat import (
    DEFAULT_VALUES,
    PROFILE_KEYS,
    ProfileStore,
    safe_read_legacy_config,
)


def test_profile_store_round_trip_only_persists_profile_safe_fields(tmp_path: Path) -> None:
    profile_path = tmp_path / "LocalAppData" / "12306FairTicket" / "gui_profiles.json"
    exported_path = tmp_path / "exported-profile.json"
    values = {
        **DEFAULT_VALUES,
        "from_station": "上海虹桥",
        "to_station": "杭州东",
        "passenger_names": ["张三", "李四"],
        "preferred_trains": ["G123"],
        "seat_position_preferences": ["1A", "1F"],
        "berth_preference": {"lower": 0, "middle": 0, "upper": 0},
        # None of these credentials, identity data, runtime paths or advanced
        # networking knobs may be written to a reusable GUI profile.
        "cookie": "COOKIE-SECRET-DO-NOT-PERSIST",
        "token": "TOKEN-SECRET-DO-NOT-PERSIST",
        "passenger_id_no": "11010519491231002X",
        "id_card": "IDENTITY-SECRET-DO-NOT-PERSIST",
        "request_timeout_seconds": 73.5,
        "login_qr_timeout_seconds": 731.0,
        "login_qr_poll_seconds": 7.3,
        "time_sync_samples": 29,
        "time_sync_max_rtt_seconds": 9.5,
        "session_file": str(tmp_path / "SECRET-session.cookies"),
        "station_cache_file": str(tmp_path / "SECRET-stations.json"),
        "qr_code_file": str(tmp_path / "SECRET-login.png"),
    }

    store = ProfileStore(profile_path)
    store.put("周末行程", values)
    store.export_file(exported_path, "周末行程", values)

    document = json.loads(profile_path.read_text(encoding="utf-8"))
    persisted = document["profiles"]["周末行程"]
    assert set(persisted) == set(PROFILE_KEYS)
    assert persisted["passenger_names"] == ["张三", "李四"]
    assert persisted["seat_position_preferences"] == ["1A", "1F"]

    serialized = profile_path.read_text(encoding="utf-8") + exported_path.read_text(encoding="utf-8")
    for secret in (
        "COOKIE-SECRET-DO-NOT-PERSIST",
        "TOKEN-SECRET-DO-NOT-PERSIST",
        "11010519491231002X",
        "IDENTITY-SECRET-DO-NOT-PERSIST",
        "SECRET-session.cookies",
        "SECRET-stations.json",
        "SECRET-login.png",
    ):
        assert secret not in serialized
    for excluded_key in (
        "cookie",
        "token",
        "passenger_id_no",
        "id_card",
        "request_timeout_seconds",
        "login_qr_timeout_seconds",
        "login_qr_poll_seconds",
        "time_sync_samples",
        "time_sync_max_rtt_seconds",
        "session_file",
        "station_cache_file",
        "qr_code_file",
        "persist_session",
    ):
        assert excluded_key not in persisted

    reloaded = ProfileStore(profile_path)
    restored = reloaded.get("周末行程")
    assert restored is not None
    assert restored["from_station"] == "上海虹桥"
    assert restored["to_station"] == "杭州东"
    assert restored["passenger_names"] == ["张三", "李四"]
    assert restored["request_timeout_seconds"] == DEFAULT_VALUES["request_timeout_seconds"]

    imported_store = ProfileStore(tmp_path / "Imported" / "gui_profiles.json")
    imported_name = imported_store.import_file(exported_path)
    imported = imported_store.get(imported_name)
    assert imported is not None
    assert imported["preferred_trains"] == ["G123"]
    assert imported["seat_position_preferences"] == ["1A", "1F"]


def test_profile_store_ignores_profiles_from_an_unsupported_document_version(tmp_path: Path) -> None:
    profile_path = tmp_path / "LocalAppData" / "12306FairTicket" / "gui_profiles.json"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        json.dumps(
            {
                "version": 2,
                "last_profile": "future-profile",
                "profiles": {
                    "future-profile": {
                        "from_station": "不应加载",
                        "to_station": "也不应加载",
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    store = ProfileStore(profile_path)

    assert store.names() == []
    assert store.last_profile == ""
    assert store.get("future-profile") is None


def test_safe_legacy_reader_never_executes_python_expressions(tmp_path: Path) -> None:
    marker = tmp_path / "expression-was-executed.txt"
    legacy = tmp_path / "config.py"
    legacy.write_text(
        "\n".join(
            (
                'FROM_STATION = "上海"',
                'PASSENGER_NAMES = ["张三"]',
                'SEAT_TYPES = ["二等座"]',
                f'TO_STATION = __import__("pathlib").Path({str(marker)!r}).write_text("owned")',
                "REQUEST_TIMEOUT_SECONDS = 1 + 72",
                f'__import__("pathlib").Path({str(marker)!r}).write_text("owned")',
            )
        ),
        encoding="utf-8",
    )

    values = safe_read_legacy_config(legacy)

    assert not marker.exists()
    assert values["from_station"] == "上海"
    assert values["passenger_names"] == ["张三"]
    assert values["to_station"] == DEFAULT_VALUES["to_station"]
    assert values["request_timeout_seconds"] == DEFAULT_VALUES["request_timeout_seconds"]
    assert values["config_path"] == str(legacy.resolve())


def test_legacy_choose_seats_is_migrated_to_structured_positions(tmp_path: Path) -> None:
    legacy = tmp_path / "config.py"
    legacy.write_text("CHOOSE_SEATS = '1A1F'\n", encoding="utf-8")

    values = safe_read_legacy_config(legacy)

    assert values["seat_position_preferences"] == ["1A", "1F"]


def test_legacy_letter_only_choose_seats_is_migrated(tmp_path: Path) -> None:
    legacy = tmp_path / "config.py"
    legacy.write_text("CHOOSE_SEATS = 'AF'\n", encoding="utf-8")

    values = safe_read_legacy_config(legacy)

    assert values["seat_position_preferences"] == ["1A", "1F"]


def test_invalid_legacy_choose_seats_is_reported(tmp_path: Path) -> None:
    legacy = tmp_path / "config.py"
    legacy.write_text("CHOOSE_SEATS = '3A'\n", encoding="utf-8")

    with pytest.raises(AppError, match="座位位置偏好无效"):
        safe_read_legacy_config(legacy)


def test_profile_import_rejects_files_larger_than_one_megabyte(tmp_path: Path) -> None:
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * 1_000_001)
    store = ProfileStore(tmp_path / "profiles.json")

    with pytest.raises(AppError, match="超过 1 MB"):
        store.import_file(oversized)


def test_explicit_empty_structured_positions_override_legacy_choose_seats(tmp_path: Path) -> None:
    legacy = tmp_path / "config.py"
    # Put CHOOSE_SEATS last so this also catches order-dependent migration.
    legacy.write_text(
        "SEAT_POSITION_PREFERENCES = []\nCHOOSE_SEATS = '1A1F'\n",
        encoding="utf-8",
    )

    values = safe_read_legacy_config(legacy)

    assert values["seat_position_preferences"] == []
