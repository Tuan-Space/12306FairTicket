"""Offline cart settings, validation and explicit legacy migration.

Only user choices belong in a cart item. Query tokens, returned tickets and
account details are deliberately absent from this module's serialization.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .input_parsing import split_multi_value_text
from .train_policy import TRAIN_CODE_PATTERN, normalize_train_codes


@dataclass(frozen=True)
class CartItem:
    from_station: str
    to_station: str
    train_scope: str
    train_code: str
    seat_type: str

    def to_mapping(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in CART_ITEM_FIELDS}


CART_ITEM_FIELDS = ("from_station", "to_station", "train_scope", "train_code", "seat_type")
MIGRATION_NOTES = {
    "train_first_any_multi_seat": (
        "旧配置按车次优先尝试多个席别；购物车先保留指定车次顺序，再按原席别顺序添加不限车次备选。"
        "未指定车次的尝试顺序已变化，请检查购物车顺序。"
    ),
}
MIGRATION_ISSUES = {
    "legacy_scope_high_speed": "旧配置仅允许高铁/动车。请改为明确车次，或明确确认接受不限车次后再开始。",
    "legacy_scope_conventional": "旧配置仅允许普通列车。请改为明确车次，或明确确认接受不限车次后再开始。",
    "legacy_scope_unset": "旧配置未选择有效车次范围，请编辑购物车并明确选择车次范围。",
    "legacy_only_empty": "旧配置要求只尝试指定车次，但车次清单为空。请为购物车项目指定车次或明确选择不限车次。",
    "legacy_invalid_strategy": "旧配置的尝试策略无效，请检查并确认购物车顺序。",
    "legacy_invalid_restriction": "旧配置的指定车次开关无效，请检查并明确选择购物车车次范围。",
}


def normalize_cart_items(value: Any) -> list[CartItem]:
    """Normalize shape without dropping invalid drafts or unknown seat labels.

    Validation is separate so unfinished forms can be saved and corrected.
    Non-string field values are rejected instead of stringifying credentials
    or accidentally interpreting booleans as a choice.
    """

    if not isinstance(value, (list, tuple)):
        raise ValueError("购物车必须是项目列表")
    items: list[CartItem] = []
    for number, raw in enumerate(value, 1):
        if isinstance(raw, CartItem):
            raw = raw.to_mapping()
        if not isinstance(raw, Mapping):
            raise ValueError(f"购物车第 {number} 项必须是对象")
        fields: dict[str, str] = {}
        for field in CART_ITEM_FIELDS:
            item = raw.get(field, "")
            if not isinstance(item, str):
                raise ValueError(f"购物车第 {number} 项的 {field} 必须是文字")
            fields[field] = item.strip()
        fields["train_code"] = fields["train_code"].upper()
        items.append(CartItem(**fields))
    return items


def serialize_cart_items(items: Any) -> list[dict[str, str]]:
    return [item.to_mapping() for item in normalize_cart_items(items)]


def cart_seat_types(items: Iterable[CartItem]) -> list[str]:
    return list(dict.fromkeys(item.seat_type for item in items))


def validate_cart_items(
    items: Iterable[CartItem], supported_seats: Iterable[str],
    station_names: Iterable[str] | None = None,
) -> list[str]:
    items = list(items)
    if not items:
        return ["请至少加入一个购物车备选"]
    seats = set(supported_seats)
    stations = set(station_names) if station_names is not None else None
    errors: list[str] = []
    seen: set[CartItem] = set()
    for number, item in enumerate(items, 1):
        problems: list[str] = []
        for label, station in (("出发站", item.from_station), ("到达站", item.to_station)):
            if not station:
                problems.append(f"请输入{label}")
            elif stations is not None and station not in stations:
                problems.append(f"{label}不在当前站点列表中，请选择标准站名")
        if item.from_station and item.from_station == item.to_station:
            problems.append("到达站不能与出发站相同")
        if item.train_scope == "specific":
            if not TRAIN_CODE_PATTERN.fullmatch(item.train_code):
                problems.append("请填写一个合法车次，例如 G123")
        elif item.train_scope == "all":
            if item.train_code:
                problems.append("不限车次项目不能同时指定车次")
        else:
            problems.append("请明确选择指定车次或不限车次")
        if item.seat_type not in seats:
            problems.append(f"不支持的席别：{item.seat_type or '尚未选择'}")
        if item in seen:
            problems.append("与前面的项目完全重复，请删除重复项")
        seen.add(item)
        if problems:
            errors.append(f"购物车第 {number} 项：" + "；".join(problems))
    return errors


def normalize_cart_migration(value: Any) -> dict[str, list[str]]:
    if value is None:
        return {"notes": [], "issues": []}
    if not isinstance(value, Mapping):
        raise ValueError("购物车转换说明必须是对象")
    result: dict[str, list[str]] = {}
    for key, allowed in (("notes", MIGRATION_NOTES), ("issues", MIGRATION_ISSUES)):
        raw = value.get(key, [])
        if not isinstance(raw, (list, tuple)) or any(not isinstance(code, str) or code not in allowed for code in raw):
            raise ValueError("购物车转换说明含未知类型，请检查原配置")
        result[key] = list(dict.fromkeys(raw))
    return result


def cart_migration_messages(value: Any) -> tuple[list[str], list[str]]:
    metadata = normalize_cart_migration(value)
    return ([MIGRATION_NOTES[code] for code in metadata["notes"]],
            [MIGRATION_ISSUES[code] for code in metadata["issues"]])


def migrate_legacy_cart(values: Mapping[str, Any]) -> dict[str, Any]:
    """Expand old train/seat priorities without silently broadening a scope.

    Blocking metadata is part of the draft and must only be removed after the
    user explicitly corrects the restriction or accepts an unrestricted cart.
    """

    def value(key: str, default: Any) -> Any:
        return values.get(key, values.get(key.upper(), default))

    existing = value("cart_items", None)
    if existing is not None:
        return {"cart_items": serialize_cart_items(existing),
                "cart_migration": normalize_cart_migration(value("cart_migration", None))}
    trains = list(dict.fromkeys(normalize_train_codes(value("preferred_trains", []))))
    raw_seats = value("seat_types", [])
    if isinstance(raw_seats, str):
        raw_seats = split_multi_value_text(raw_seats)
    if not isinstance(raw_seats, (list, tuple)) or any(not isinstance(seat, str) for seat in raw_seats):
        raise ValueError("旧配置席别必须是文字列表")
    seats = list(dict.fromkeys(seat.strip() for seat in raw_seats))
    origin, destination = value("from_station", ""), value("to_station", "")
    if not isinstance(origin, str) or not isinstance(destination, str):
        raise ValueError("旧配置站名必须是文字")
    strategy = value("priority_strategy", "train_first")
    only = value("only_preferred_trains", True)
    scope = value("empty_train_scope", "all")
    metadata: dict[str, list[str]] = {"notes": [], "issues": []}
    if strategy not in ("train_first", "seat_first"):
        metadata["issues"].append("legacy_invalid_strategy")
        strategy = "train_first"
    if not isinstance(only, bool):
        metadata["issues"].append("legacy_invalid_restriction")
        only = True
    if not trains:
        if only:
            metadata["issues"].append("legacy_only_empty")
        if scope in ("high_speed", "conventional"):
            metadata["issues"].append(f"legacy_scope_{scope}")
        elif scope != "all":
            metadata["issues"].append("legacy_scope_unset")
    any_trains = not only
    if strategy == "train_first" and any_trains and len(seats) > 1:
        metadata["notes"].append("train_first_any_multi_seat")
    rows: list[CartItem] = []

    def add(train: str, seat: str) -> None:
        rows.append(CartItem(origin.strip(), destination.strip(),
                             "specific" if train else "all", train, seat))

    if strategy == "seat_first":
        for seat in seats:
            for train in trains:
                add(train, seat)
            if any_trains or not trains:
                add("", seat)
    else:
        for train in trains:
            for seat in seats:
                add(train, seat)
        if any_trains or not trains:
            for seat in seats:
                add("", seat)
    return {"cart_items": serialize_cart_items(rows), "cart_migration": metadata}
