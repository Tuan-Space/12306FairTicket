"""Offline cart settings and validation.

Only user choices belong in a cart item. Query tokens, returned tickets and
account details are deliberately absent from this module's serialization.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .train_policy import TRAIN_CODE_PATTERN


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


def reject_unresolved_cart_migration(value: Any) -> None:
    """Reject restricted preview drafts rather than silently widening a trip.

    Version 5 preview files may carry migration metadata. Resolved notes do not
    affect current cart semantics and are deliberately not persisted again.
    """
    if value is None:
        return
    if not isinstance(value, Mapping) or set(value) - {"notes", "issues"}:
        raise ValueError("购物车历史范围信息无效，请重新建立购物车")
    for key in ("notes", "issues"):
        codes = value.get(key, [])
        if not isinstance(codes, (list, tuple)) or any(not isinstance(code, str) for code in codes):
            raise ValueError("购物车历史范围信息无效，请重新建立购物车")
    if value.get("issues"):
        raise ValueError("此配置仍有未解决的历史车次范围限制，请重新建立购物车")
