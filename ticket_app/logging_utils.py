"""Logging helpers that keep credentials and passenger identifiers out of logs."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from logging.handlers import RotatingFileHandler
from typing import Iterable


_COOKIE_LINE = re.compile(r"(?i)(\b(?:cookie|set-cookie)\b[\"']?\s*[:=]\s*)[^\r\n]+")
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:REPEAT_SUBMIT_TOKEN|newapptk|uamtk|tk|token|secretStr|"
    r"leftTicketStr|session(?:id)?|uuid|sig|order_?id)\b[\"']?\s*[:=]\s*)"
    r"([\"']?)[^\"'\s,;}&]+(?:\2)"
)
_MAINLAND_ID = re.compile(r"(?<!\d)(\d{6})\d{8,11}([0-9Xx]{2,4})(?!\d)")
_PHONE = re.compile(r"(?<!\d)(1\d{2})\d{4}(\d{4})(?!\d)")
_CHINESE_ORDER_ID = re.compile(r"(订单号\s*[:：]\s*)[A-Za-z0-9_-]+", re.IGNORECASE)


def redact_text(value: object, sensitive_terms: Iterable[str] = ()) -> str:
    text = str(value)
    text = _COOKIE_LINE.sub(lambda m: f"{m.group(1)}<已隐藏>", text)
    text = _SENSITIVE_ASSIGNMENT.sub(lambda m: f"{m.group(1)}<已隐藏>", text)
    text = _MAINLAND_ID.sub(r"\1********\2", text)
    text = _PHONE.sub(r"\1****\2", text)
    text = _CHINESE_ORDER_ID.sub(r"\1<已隐藏>", text)
    for term in sorted({str(item).strip() for item in sensitive_terms}, key=len, reverse=True):
        if len(term) >= 2:
            text = text.replace(term, "<姓名已隐藏>")
    return text


class RedactingFormatter(logging.Formatter):
    def __init__(self, *args: object, sensitive_terms: Iterable[str] = (), **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.sensitive_terms = tuple(sensitive_terms)

    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record), self.sensitive_terms)


def make_rotating_file_handler(
    path: Path,
    level: int = logging.INFO,
    sensitive_terms: Iterable[str] = (),
) -> RotatingFileHandler:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        path,
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setLevel(level)
    handler.setFormatter(
        RedactingFormatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            sensitive_terms=sensitive_terms,
        )
    )
    return handler
