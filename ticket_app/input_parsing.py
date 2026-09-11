"""Shared text parsing for passenger names and ordered train selections."""

from __future__ import annotations

import re


# Ordinary spaces deliberately remain inside an entry (for example a name).
_MULTI_VALUE_SEPARATOR = re.compile(r"[,，、;；\r\n\t]+")


def split_multi_value_text(value: str) -> list[str]:
    """Split human-entered text without reordering or removing duplicates.

    Callers retain their own handling of non-string configuration values.
    In particular, entries in a JSON list are already individual values and
    must not be split again if an entry contains separator punctuation.
    """

    return [item for part in _MULTI_VALUE_SEPARATOR.split(value) if (item := part.strip())]
