import json
import re
from datetime import datetime
from typing import Any, Dict, List, Tuple

from .configuration import MONTH_NAMES, SEAT_SPECS, WEEKDAY_NAMES


def _display_stock(value: str) -> str:
    return value if value else "--"


def _stock_available(value: str) -> bool:
    return value not in {"", "--", "无", "*"}


def _is_terminal_order_failure(message: Any) -> bool:
    text = str(message or "")
    terminal_markers = (
        "没有足够的票",
        "余票不足",
        "出票失败",
        "占座失败",
        "排队失败",
        "订单提交失败",
        "取消排队",
    )
    if any(marker in text for marker in terminal_markers):
        return True
    return False


def _resolve_submit_seat_code(seat_label: str, ticket: Dict[str, Any]) -> str:
    if seat_label != "无座":
        return SEAT_SPECS[seat_label].submit_code
    train_code = ticket["station_train_code"].upper()
    return "O" if train_code.startswith(("G", "D", "C")) else "1"


def _format_queue_date(train_date: str) -> str:
    parsed = datetime.strptime(train_date, "%Y-%m-%d")
    return (
        f"{WEEKDAY_NAMES[parsed.weekday()]} {MONTH_NAMES[parsed.month]} {parsed.day:02d} "
        f"{parsed.year} 00:00:00 GMT+0800 (中国标准时间)"
    )


def _build_passenger_strings(passengers: List[Dict[str, Any]]) -> Tuple[str, str]:
    passenger_ticket_list = []
    old_passenger_list = []
    for passenger in passengers:
        seat_type = passenger.get("seat_type")
        ticket_type = passenger.get("passenger_type") or "1"
        name = passenger.get("passenger_name") or ""
        id_type = passenger.get("passenger_id_type_code") or ""
        id_no = passenger.get("passenger_id_no") or ""
        mobile = passenger.get("mobile_no") or ""
        all_enc = passenger.get("allEncStr") or passenger.get("allEncstr") or ""
        fields = [seat_type, "0", ticket_type, name, id_type, id_no, mobile, "N"]
        if all_enc:
            fields.append(all_enc)
        passenger_ticket_list.append(",".join(str(item) for item in fields))
        old_passenger_list.append(f"{name},{id_type},{id_no},{ticket_type}_")
    return "_".join(passenger_ticket_list), "".join(old_passenger_list)


def _message_from_payload(payload: Dict[str, Any]) -> str:
    messages = payload.get("messages")
    if isinstance(messages, list) and messages:
        return str(messages[0])
    if isinstance(messages, str) and messages:
        return messages
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("errMsg", "msg", "message"):
            if data.get(key):
                return str(data[key])
    for key in ("message", "result_message"):
        if payload.get(key):
            return str(payload[key])
    return str(payload)


def _extract_js_object(text: str, variable_name: str) -> str:
    start = text.find(variable_name)
    if start == -1:
        return ""
    brace_start = text.find("{", start)
    if brace_start == -1:
        return ""
    depth = 0
    quote = ""
    escaped = False
    for index in range(brace_start, len(text)):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[brace_start : index + 1]
    return ""


def _parse_js_object(text: str) -> Dict[str, Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        cleaned = re.sub(r"([,{]\s*)([A-Za-z_]\w*)(\s*:)", r'\1"\2"\3', text)
        cleaned = cleaned.replace("'", '"').replace("undefined", "null")
        return json.loads(cleaned)
