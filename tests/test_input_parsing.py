"""Shared multi-value parsing across form validation and config persistence."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from ticket_app.configuration import AppConfig, AppError, _as_list
from ticket_app.gui.compat import (
    DEFAULT_VALUES,
    EDITABLE_SETTINGS_KEYS,
    build_app_config,
    canonical_mapping,
    load_gui_settings,
    save_gui_settings,
)
from ticket_app.gui.validation import _string_list, validate_gui_mapping
from ticket_app.input_parsing import split_multi_value_text


SEPARATORS = [",", "，", "、", ";", "；", "\r", "\n", "\r\n", "\t"]
STATIONS = {"北京西", "郑州东"}
TODAY = date(2026, 9, 11)


def valid_values(**updates: object) -> dict[str, object]:
    return {
        **DEFAULT_VALUES,
        "train_date": "2099-01-01",
        "passenger_names": ["张三"],
        "seat_types": ["二等座"],
        "preferred_trains": ["G79"],
        **updates,
    }


def validate(values: dict[str, object]) -> dict[str, str]:
    return validate_gui_mapping(values, STATIONS, today=TODAY)


@pytest.mark.parametrize("separator", SEPARATORS)
def test_supported_separators_are_shared_by_config_validation_and_json(
    separator: str, tmp_path: Path
) -> None:
    passengers = f"{separator} 张三 {separator}{separator} Mary Ann {separator}李四{separator}"
    trains = f"{separator} d123 {separator}{separator} g79 {separator}1461{separator}"
    values = valid_values(passenger_names=passengers, preferred_trains=trains)

    assert split_multi_value_text(passengers) == ["张三", "Mary Ann", "李四"]
    assert validate(values) == {}
    canonical = canonical_mapping(values)
    assert canonical["passenger_names"] == ["张三", "Mary Ann", "李四"]
    assert canonical["preferred_trains"] == ["D123", "G79", "1461"]

    for config in (AppConfig.from_mapping(values), build_app_config(values)):
        assert config.passenger_names == canonical["passenger_names"]
        assert config.preferred_trains == canonical["preferred_trains"]

    # Exercise legacy import of raw text fields, then save the normalized v3 schema.
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"version": 2, "settings": values}), encoding="utf-8")
    imported = load_gui_settings(path)
    assert validate(imported) == {}
    save_gui_settings(path, imported)
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["version"] == 3
    assert set(document["settings"]) == EDITABLE_SETTINGS_KEYS
    assert document["settings"]["passenger_names"] == canonical["passenger_names"]
    assert document["settings"]["preferred_trains"] == canonical["preferred_trains"]
    restored = load_gui_settings(path)
    assert restored == imported
    assert build_app_config(restored).preferred_trains == ["D123", "G79", "1461"]


def test_mixed_separators_keep_internal_spaces_order_and_duplicates() -> None:
    text = " ,，、;；\r\n\t 张三 , Mary  Ann；\t李四、 张三 \r\n; "
    assert split_multi_value_text(text) == ["张三", "Mary  Ann", "李四", "张三"]
    values = canonical_mapping({"PREFERRED_TRAINS": "d123、g79；D123\t1461"})
    assert values["preferred_trains"] == ["D123", "G79", "D123", "1461"]


@pytest.mark.parametrize("text", ["", "  ", ",，、;；\r\n\t", " , ，\t； "])
def test_empty_fields_and_separator_only_fields_remain_empty(text: str) -> None:
    assert split_multi_value_text(text) == []
    values = valid_values(passenger_names=text, preferred_trains=text)
    errors = validate(values)
    assert "1 到 5" in errors["passenger_names"]
    assert "至少填写" in errors["preferred_trains"]
    with pytest.raises(AppError, match="必须填写 PASSENGER_NAMES"):
        AppConfig.from_mapping(values)


@pytest.mark.parametrize("container", [list, tuple])
def test_structured_sequence_entries_are_atomic(container: type) -> None:
    passengers = container([" 张三，李四 ", " Mary Ann ", ""])
    expected = ["张三，李四", "Mary Ann"]
    assert _as_list(passengers) == expected
    assert _string_list(passengers) == expected
    assert canonical_mapping({"passenger_names": passengers})["passenger_names"] == expected
    assert AppConfig.from_mapping(valid_values(passenger_names=passengers)).passenger_names == expected

    # A separator inside an existing train array entry is still an invalid
    # identifier, rather than silently turning one configured entry into two.
    values = valid_values(preferred_trains=container(["g79；D123"]))
    assert canonical_mapping(values)["preferred_trains"] == ["G79；D123"]
    assert "格式不正确" in validate(values)["preferred_trains"]


@pytest.mark.parametrize(
    ("passengers", "gui_message", "config_message"),
    [
        ("张三，李四；张三", "乘车人不能重复", "不能包含重复乘车人"),
        ("甲、乙，丙;丁；戊\t己", "1 到 5", "最多支持 5 位乘车人"),
    ],
)
def test_passenger_errors_survive_normalization_and_save_import(
    passengers: str, gui_message: str, config_message: str, tmp_path: Path
) -> None:
    values = valid_values(passenger_names=passengers)
    path = tmp_path / "unfinished.json"
    save_gui_settings(path, values)
    for candidate in (values, canonical_mapping(values), load_gui_settings(path)):
        assert gui_message in validate(candidate)["passenger_names"]
        with pytest.raises(AppError, match=config_message):
            build_app_config(candidate)


@pytest.mark.parametrize("trains", ["g79、not-a-train；D123", "g79 G95", "G79，D123/5"])
def test_invalid_train_is_still_reported_after_save_import(trains: str, tmp_path: Path) -> None:
    values = valid_values(preferred_trains=trains)
    path = tmp_path / "invalid-train.json"
    save_gui_settings(path, values)
    for candidate in (values, canonical_mapping(values), load_gui_settings(path)):
        assert "格式不正确" in validate(candidate)["preferred_trains"]


def test_legacy_v1_uppercase_text_fields_import_with_shared_rules(tmp_path: Path) -> None:
    path = tmp_path / "legacy.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "values": {
                    "PASSENGER_NAMES": "张三、李四；Mary Ann",
                    "PREFERRED_TRAINS": "d123\tg79，1461",
                },
            }
        ),
        encoding="utf-8",
    )
    values = load_gui_settings(path)
    assert values["passenger_names"] == ["张三", "李四", "Mary Ann"]
    assert values["preferred_trains"] == ["D123", "G79", "1461"]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("auto_submit", "true"),
        ("max_retries", True),
        ("max_retries", 1.5),
        ("query_interval_seconds", True),
        ("query_interval_seconds", float("inf")),
    ],
)
def test_shared_text_parser_does_not_relax_existing_config_type_checks(
    key: str, value: object, tmp_path: Path
) -> None:
    values = valid_values(passenger_names="张三、李四", preferred_trains="g79；D123", **{key: value})
    with pytest.raises(AppError, match="配置字段类型无效"):
        save_gui_settings(tmp_path / "invalid-type.json", values)
