"""Shared, offline train code parsing and classification.

Train prefixes only describe the entered services. They
never imply a seat/berth capability; those depend on the actual order response.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from .input_parsing import split_multi_value_text


TRAIN_CODE_PATTERN = re.compile(r"^(?:[A-Z][0-9]{1,5}|[0-9]{1,5})$")
HIGH_SPEED_PREFIXES = ("G", "D", "C")


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
