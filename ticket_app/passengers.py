"""Passenger identity matching and per-order ticket choices.

A contact's passenger_type describes their stored identity.  The independently
chosen ticket_type describes the ticket being purchased, so buying an adult
ticket must never overwrite a student's stored identity.
"""

from collections import defaultdict
import re
from typing import Any, Iterable, Mapping


TICKET_TYPE_CODES = {"adult": "1", "student": "3"}


def is_student_ticket_rejection(message: Any) -> bool:
    """Recognize explicit eligibility refusals, not undocumented numeric codes.

    This only classifies the server's message.  It must not be used to infer
    that an unknown queue result is safe to retry or to switch the ticket type.
    """

    if not isinstance(message, str):
        return False
    text = re.sub(r"\s+", "", message)
    quota = r"(?:优惠(?:乘车)?次数|学生票次数)"
    if re.search(
        quota + r"(?:余额)?(?:已经全部用完|已经用完|已全部用完|已用完|已耗尽|不足|为[0零]|已达上限|超过上限)"
        r"|(?:没有剩余|无剩余)(?:的)?" + quota,
        text,
    ):
        return True
    if "学生" not in text:
        return False
    if any(term in text for term in ("不是学生", "非学生", "学生优惠区间不符", "学生优惠区间不一致")):
        return True
    eligibility = r"(?:学生(?:优惠)?(?:资质|资格|身份|认证|核验)|学生票购买资格)"
    refusal = r"(?:未核验|未认证|未完成|核验失败|认证失败|未通过|不具备|不符合|无效|已过期|不存在)"
    return bool(
        re.search(eligibility + r"[^，。；;]{0,16}" + refusal, text)
        or re.search(r"(?:未通过|未完成|未办理|未认证|不具备|没有)[^，。；;]{0,8}" + eligibility, text)
    )


def normalize_passenger_ticket_types(value: Any) -> dict[str, str]:
    """Validate explicit choices; an absent name means follow the contact."""

    if not isinstance(value, Mapping):
        raise ValueError("乘车人票种必须是姓名到 adult/student 的映射")
    result: dict[str, str] = {}
    for raw_name, ticket_type in value.items():
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ValueError("乘车人票种的姓名不能为空")
        name = raw_name.strip()
        if name in result:
            raise ValueError("乘车人票种包含重复姓名")
        if not isinstance(ticket_type, str) or ticket_type not in TICKET_TYPE_CODES:
            raise ValueError("乘车人票种仅支持 adult（成人票）或 student（学生票）")
        result[name] = ticket_type
    return result


def select_passengers(
    passengers: Iterable[Mapping[str, Any]],
    names: Iterable[str],
    ticket_types: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Match names unambiguously and return identity-preserving order copies."""

    # Keep configuration's normalization import independent of this runtime
    # error type so both GUI and CLI can use the pure normalizer.
    from .configuration import AppError

    try:
        choices = normalize_passenger_ticket_types({} if ticket_types is None else ticket_types)
    except ValueError as exc:
        raise AppError(str(exc)) from exc
    selected_names = list(names)
    if len(set(selected_names)) != len(selected_names):
        raise AppError("乘车人不能重复")
    if any(name not in selected_names for name in choices):
        raise AppError("乘车人票种中包含未选择的姓名，请重新检查配置")
    by_name: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for passenger in passengers:
        if not isinstance(passenger, Mapping):
            raise AppError("12306 返回的乘车人资料格式无效")
        name = passenger.get("passenger_name")
        if isinstance(name, str):
            by_name[name].append(passenger)

    selected: list[dict[str, Any]] = []
    for name in selected_names:
        matches = by_name.get(name, [])
        if not matches:
            raise AppError(f"配置中的乘车人不存在: {name}。请在 12306 常用乘车人中核对姓名")
        if len(matches) > 1:
            raise AppError(f"乘车人 {name} 对应多个同名联系人，无法仅按姓名区分；请到 12306 官方渠道核对")
        passenger = dict(matches[0])
        identity_type = str(passenger.get("passenger_type") or "1")
        choice = choices.get(name)
        if choice == "student" and identity_type != "3":
            raise AppError(f"乘车人 {name} 的联系人类型不是学生，不能选择学生票；请先在 12306 核对学生身份")
        passenger["ticket_type"] = TICKET_TYPE_CODES[choice] if choice else identity_type
        selected.append(passenger)
    return selected
