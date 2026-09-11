"""Shared, offline train classification, filtering and attempt ordering.

Train prefixes only guide the form and optional train-family filtering. They
never imply a seat/berth capability; those depend on the actual order response.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence

from .input_parsing import split_multi_value_text


TRAIN_CODE_PATTERN = re.compile(r"^(?:[A-Z][0-9]{1,5}|[0-9]{1,5})$")
HIGH_SPEED_PREFIXES = ("G", "D", "C")
TRAIN_SCOPES = frozenset({"high_speed", "conventional", "all"})
PRIORITY_STRATEGIES = frozenset({"train_first", "seat_first"})
SCOPE_LABELS = {"high_speed": "高铁/动车", "conventional": "普通列车", "all": "不限类型"}
STRATEGY_LABELS = {"train_first": "车次优先", "seat_first": "席别优先"}


def normalize_train_codes(value: Any) -> list[str]:
    """Normalize free text while preserving order and existing list semantics."""

    if value is None:
        return []
    if isinstance(value, str):
        return [item.upper() for item in split_multi_value_text(value)]
    if isinstance(value, Iterable) and not isinstance(value, (Mapping, bytes, bytearray)):
        return [str(item).strip().upper() for item in value if str(item).strip()]
    text = str(value).strip().upper()
    return [text] if text else []


def classify_train_codes(preferred: Any) -> str | None:
    trains = normalize_train_codes(preferred)
    if not trains or any(not TRAIN_CODE_PATTERN.fullmatch(code) for code in trains):
        return None
    families = {"high_speed" if code.startswith(HIGH_SPEED_PREFIXES) else "conventional" for code in trains}
    return next(iter(families)) if len(families) == 1 else "all"


def effective_train_scope(
    preferred: Any, only_preferred: bool, empty_train_scope: str | None = "all"
) -> str | None:
    if normalize_train_codes(preferred):
        return "exact" if only_preferred else "all"
    return empty_train_scope if isinstance(empty_train_scope, str) and empty_train_scope in TRAIN_SCOPES else None


def validate_train_policy(
    preferred: Any,
    only_preferred: bool,
    empty_train_scope: str | None = "all",
    priority_strategy: str = "train_first",
) -> dict[str, str]:
    errors: dict[str, str] = {}
    trains = normalize_train_codes(preferred)
    invalid = next((code for code in trains if not TRAIN_CODE_PATTERN.fullmatch(code)), None)
    if invalid:
        errors["preferred_trains"] = f"车次“{invalid}”格式不正确，例如 G123"
    elif not trains and only_preferred:
        errors["preferred_trains"] = "已选择“只尝试上述车次”，请至少填写一个车次"
    if empty_train_scope is not None and (
        not isinstance(empty_train_scope, str) or empty_train_scope not in TRAIN_SCOPES
    ):
        errors["empty_train_scope"] = "车次范围必须是高铁/动车、普通列车或不限类型"
    elif not trains and empty_train_scope is None:
        errors["empty_train_scope"] = "优先车次留空时，请选择高铁/动车、普通列车或不限类型"
    if not isinstance(priority_strategy, str) or priority_strategy not in PRIORITY_STRATEGIES:
        errors["priority_strategy"] = "请选择车次优先或席别优先"
    return errors


def train_in_scope(
    train_code: str, preferred: Any, only_preferred: bool, empty_train_scope: str | None = "all"
) -> bool:
    train = str(train_code).strip().upper()
    scope = effective_train_scope(preferred, only_preferred, empty_train_scope)
    if scope == "exact":
        return train in normalize_train_codes(preferred)
    if scope == "all":
        return True
    return scope is not None and classify_train_codes([train]) == scope


def priority_sort_key(
    train_code: str,
    seat_label: str,
    preferred: Sequence[str],
    seat_types: Sequence[str],
    priority_strategy: str = "train_first",
) -> tuple:
    """One ordering key for both real candidates and the form's illustration."""

    train = str(train_code).strip().upper()
    trains = normalize_train_codes(preferred)
    train_index = trains.index(train) if train in trains else len(trains)
    train_key = (train_index, train)
    seat_index = seat_types.index(seat_label)
    if priority_strategy == "seat_first":
        return (seat_index, *train_key)
    return (*train_key, seat_index)


def scope_summary(
    preferred: Any, only_preferred: bool, empty_train_scope: str | None = "all"
) -> str:
    trains = normalize_train_codes(preferred)
    if trains and classify_train_codes(trains) is None:
        return "实际车次范围：请先修正车次格式"
    if trains and only_preferred:
        return "实际车次范围：仅 " + "、".join(trains)
    if trains:
        return "实际车次范围：优先上述车次，其余车次均可备选（含其他类型列车）"
    if only_preferred:
        return "实际车次范围：仅指定清单，请先填写车次或取消限制"
    scope = effective_train_scope(trains, only_preferred, empty_train_scope)
    if scope is None:
        return "实际车次范围：尚未选择"
    return "实际车次范围：" + SCOPE_LABELS[scope]


def priority_preview(
    preferred: Any,
    seat_types: Sequence[str],
    priority_strategy: str = "train_first",
    only_preferred: bool = False,
    empty_train_scope: str | None = "all",
    limit: int = 6,
) -> str:
    suffix = "实际执行会跳过无票候选。"
    trains = normalize_train_codes(preferred)
    if not seat_types or validate_train_policy(trains, only_preferred, empty_train_scope, priority_strategy):
        return "请先完善车次范围和席别；" + suffix
    labels: dict[str, str] = {}
    if not trains:
        trains = ["G1", "D2"] if empty_train_scope == "high_speed" else ["K1", "1461"]
        if empty_train_scope == "all":
            trains = ["G1", "K1"]
        labels = {train: train + "（示例）" for train in trains}
    elif not only_preferred:
        # A symbolic backup remains after every explicitly preferred train.
        # It is an illustration of an eligible result, not an invented service.
        backup = "其他车次"
        trains = list(dict.fromkeys(trains)) + [backup]
    pairs = [(train, seat) for train in dict.fromkeys(trains) for seat in seat_types]
    pairs.sort(key=lambda pair: priority_sort_key(pair[0], pair[1], normalize_train_codes(preferred), seat_types, priority_strategy))
    shown = " → ".join(f"{labels.get(train, train)} {seat}" for train, seat in pairs[:limit])
    if len(pairs) > limit:
        shown += " → …"
    return f"{STRATEGY_LABELS[priority_strategy]}：{shown}。{suffix}"
