"""Seat-relation and berth-preference protocol helpers.

12306 exposes these options as best-effort preferences.  The seat grid describes
relative positions (``1A``, ``2F``), not a physical coach/row/seat number.  The
server may allocate another position when the requested relationship is not
available, so this module deliberately builds one request payload and never
models retry-by-cancelling/rebooking behaviour.
"""

import re
from dataclasses import dataclass, field
from typing import Any, FrozenSet, Iterable, Mapping, Optional, Tuple


SEAT_LETTER_ORDER = "ABCDF"
SEAT_POSITION_PATTERN = re.compile(r"[12][ABCDF]")
CHOOSE_SEATS_PATTERN = re.compile(r"(?:[12][ABCDF])+")

# Current passengerInfo_js layout branches.  Q/M/D share the first-class
# template, O uses the second-class template, and P uses the special-class
# template.  Business class (9) is selected separately from ``dw_flag`` below.
SEAT_LAYOUT_LETTERS = {
    "Q": ("A", "C", "D", "F"),
    "M": ("A", "C", "D", "F"),
    "D": ("A", "C", "D", "F"),
    "O": ("A", "B", "C", "D", "F"),
    "P": ("A", "C", "F"),
}
SEATED_SEAT_TYPES = frozenset(SEAT_LAYOUT_LETTERS) | frozenset({"9"})
CHOOSABLE_SEAT_TYPES = frozenset({"Q", "M", "D", "O", "P", "9"})
BERTH_SEAT_TYPES = frozenset({"3", "4", "6", "A", "F"})


def _validate_passenger_count(passenger_count: int) -> None:
    if isinstance(passenger_count, bool) or not isinstance(passenger_count, int):
        raise ValueError("乘车人数必须是整数")
    if not 1 <= passenger_count <= 5:
        raise ValueError("12306 单次选座/选铺仅支持 1 到 5 位乘车人")


def _business_layout_letters(dw_flag: Optional[str]) -> Tuple[str, ...]:
    """Return the current business-class layout inferred from ``dw_flag``.

    The official page reads ``orderRequestDTO.dw_flag.split(',')[3]``.  ``S``
    selects its dedicated 1+1 business template (A/F); a known non-S value
    reuses the 2+1 special-class template (A/C/F).  When the context is absent
    we expose only the safe intersection A/F rather than guessing a train model.
    ``None`` is reserved for configuration-time validation and returns the
    union of layouts, because the real flag is not available until initDc.
    """

    if dw_flag is None:
        return ("A", "C", "F")
    parts = str(dw_flag).split(",") if dw_flag else []
    if len(parts) <= 3:
        return ("A", "F")
    layout_flag = parts[3].strip().upper()
    if not layout_flag or layout_flag == "S":
        return ("A", "F")
    return ("A", "C", "F")


def seat_layout_letters(seat_type: str, dw_flag: Optional[str] = "") -> Tuple[str, ...]:
    normalized = str(seat_type or "").strip().upper()
    if normalized == "9":
        return _business_layout_letters(dw_flag)
    return SEAT_LAYOUT_LETTERS.get(normalized, ())


def seat_layout_positions(seat_type: str, dw_flag: Optional[str] = "") -> Tuple[str, ...]:
    letters = seat_layout_letters(seat_type, dw_flag)
    return tuple("%s%s" % (row, letter) for row in (1, 2) for letter in letters)


def _position_sort_key(position: str) -> Tuple[int, int]:
    return int(position[0]), SEAT_LETTER_ORDER.index(position[1])


def _normalize_position(value: Any) -> str:
    text = str(value or "").strip().upper()
    if len(text) == 1 and text in SEAT_LETTER_ORDER:
        text = "1" + text
    if not SEAT_POSITION_PATTERN.fullmatch(text):
        raise ValueError("无效座位关系格子 %r；应为 1A..1F 或 2A..2F" % text)
    return text


def _parse_positions(value: Any) -> Tuple[str, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, SeatRelationPreference):
        return value.positions
    if isinstance(value, Mapping):
        for key in ("positions", "seat_positions", "selected_positions"):
            if key in value:
                return _parse_positions(value[key])
        raise ValueError("座位偏好对象必须包含 positions")
    if isinstance(value, str):
        compact = re.sub(r"[\s,;|]+", "", value).upper()
        if not compact:
            return ()
        if CHOOSE_SEATS_PATTERN.fullmatch(compact):
            return tuple(_normalize_position(item) for item in re.findall(r"[12][ABCDF]", compact))
        if re.fullmatch(r"[ABCDF]+", compact):
            return tuple("1" + letter for letter in compact)
        raise ValueError("无效 CHOOSE_SEATS/座位关系偏好: %r" % value)
    if isinstance(value, Iterable):
        return tuple(_normalize_position(item) for item in value)
    return (_normalize_position(value),)


@dataclass(frozen=True)
class SeatRelationPreference:
    """Selected cells in the two-row relative-seat grid."""

    positions: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        normalized = _parse_positions(self.positions)
        if len(set(normalized)) != len(normalized):
            raise ValueError("座位关系格子不能重复")
        object.__setattr__(self, "positions", tuple(sorted(normalized, key=_position_sort_key)))

    @classmethod
    def from_value(cls, value: Any) -> "SeatRelationPreference":
        if isinstance(value, cls):
            return value
        return cls(_parse_positions(value))

    @property
    def enabled(self) -> bool:
        return bool(self.positions)

    def validate(
        self,
        passenger_count: int,
        seat_type: Optional[str] = None,
        dw_flag: Optional[str] = "",
    ) -> None:
        _validate_passenger_count(passenger_count)
        if not self.positions:
            return
        if len(self.positions) != passenger_count:
            raise ValueError("已选座位关系格子数必须等于乘车人数")
        if passenger_count == 1 and any(position.startswith("2") for position in self.positions):
            raise ValueError("单人选座只能使用关系图第一排")
        if seat_type is None:
            return
        allowed = frozenset(seat_layout_positions(seat_type, dw_flag))
        if not allowed:
            raise ValueError("当前席别不支持 ABCDF 座位位置偏好")
        invalid = [position for position in self.positions if position not in allowed]
        if invalid:
            letters = "".join(seat_layout_letters(seat_type, dw_flag))
            raise ValueError("当前席别/车型不支持位置 %s；可用字母为 %s" % (invalid, letters))

    def to_choose_seats(self, passenger_count: int, seat_type: str, dw_flag: str = "") -> str:
        self.validate(passenger_count, seat_type, dw_flag)
        return "".join(self.positions)

    def to_mapping(self) -> Mapping[str, Any]:
        return {"positions": list(self.positions)}


def _count_value(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError("%s数量必须是整数" % label)
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("%s数量必须是整数" % label) from exc
    if result < 0 or result > 5:
        raise ValueError("%s数量必须在 0 到 5 之间" % label)
    return result


@dataclass(frozen=True)
class BerthPreference:
    """Requested passenger counts for lower, middle and upper berths."""

    lower: int = 0
    middle: int = 0
    upper: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "lower", _count_value(self.lower, "下铺"))
        object.__setattr__(self, "middle", _count_value(self.middle, "中铺"))
        object.__setattr__(self, "upper", _count_value(self.upper, "上铺"))

    @classmethod
    def from_value(cls, value: Any) -> "BerthPreference":
        if isinstance(value, cls):
            return value
        if value is None or value == "":
            return cls()
        if isinstance(value, Mapping):
            return cls(
                lower=value.get("lower", value.get("下铺", 0)),
                middle=value.get("middle", value.get("中铺", 0)),
                upper=value.get("upper", value.get("上铺", 0)),
            )
        text = str(value).strip().lower()
        names = {
            "lower": (1, 0, 0),
            "下铺": (1, 0, 0),
            "middle": (0, 1, 0),
            "中铺": (0, 1, 0),
            "upper": (0, 0, 1),
            "上铺": (0, 0, 1),
        }
        if text in names:
            return cls(*names[text])
        raise ValueError("BERTH_PREFERENCE 应为包含 lower/middle/upper 的对象")

    @property
    def total(self) -> int:
        return self.lower + self.middle + self.upper

    @property
    def enabled(self) -> bool:
        return self.total > 0

    def validate(self, passenger_count: int, can_choose_middle: Optional[bool] = None) -> None:
        _validate_passenger_count(passenger_count)
        if not self.enabled:
            return
        if self.total != passenger_count:
            raise ValueError("上/中/下铺数量之和必须等于乘车人数")
        if can_choose_middle is False and self.middle:
            raise ValueError("当前列车/席别不支持中铺偏好")

    def to_seat_detail_type(self, passenger_count: int, can_choose_middle: bool) -> str:
        self.validate(passenger_count, can_choose_middle)
        if not self.enabled:
            return "000"
        return "%d%d%d" % (self.lower, self.middle, self.upper)

    def to_mapping(self) -> Mapping[str, int]:
        return {"lower": self.lower, "middle": self.middle, "upper": self.upper}


def _allowed_seat_types(value: Any) -> FrozenSet[str]:
    if not isinstance(value, str):
        return frozenset()
    if not value or not all(char in CHOOSABLE_SEAT_TYPES for char in value):
        return frozenset()
    return frozenset(value)


@dataclass(frozen=True)
class OrderCapabilities:
    """Runtime preference capabilities returned by checkOrderInfo."""

    can_choose_seats: bool = False
    allowed_seat_types: FrozenSet[str] = field(default_factory=frozenset)
    can_choose_beds: bool = False
    can_choose_middle: bool = False
    seat_status: str = ""
    bed_status: str = ""
    middle_status: str = ""

    @classmethod
    def from_mapping(cls, data: Any) -> "OrderCapabilities":
        if not isinstance(data, Mapping):
            return cls()
        seat_status = data.get("canChooseSeats")
        bed_status = data.get("canChooseBeds")
        middle_status = data.get("isCanChooseMid")
        seat_status = seat_status if isinstance(seat_status, str) else ""
        bed_status = bed_status if isinstance(bed_status, str) else ""
        middle_status = middle_status if isinstance(middle_status, str) else ""
        return cls(
            can_choose_seats=seat_status == "Y",
            allowed_seat_types=_allowed_seat_types(data.get("choose_Seats")),
            can_choose_beds=bed_status == "Y",
            can_choose_middle=middle_status == "Y",
            seat_status=seat_status,
            bed_status=bed_status,
            middle_status=middle_status,
        )

    @property
    def choose_seats(self) -> str:
        return "".join(sorted(self.allowed_seat_types))

    @property
    def can_choose_mid(self) -> bool:
        return self.can_choose_middle

    def berth_unavailable_message(self) -> str:
        if self.bed_status == "Z":
            return "选铺服务需先在铁路12306 App完成人证核验或注册常旅客会员"
        if self.bed_status == "5":
            return "超过5位乘车人时12306不提供在线选铺"
        return "当前列车/席别未开放在线选铺"


@dataclass(frozen=True)
class OrderCheckResult:
    success: bool
    message: str
    capabilities: OrderCapabilities = field(default_factory=OrderCapabilities)

    @property
    def ok(self) -> bool:
        return self.success

    def __iter__(self):
        """Keep existing ``ok, message = check_order_info(...)`` callers working."""

        yield self.success
        yield self.message


@dataclass(frozen=True)
class OrderPreferencePayload:
    choose_seats: str = ""
    seat_detail_type: str = "000"
    warnings: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        choose_seats = str(self.choose_seats or "").strip().upper()
        seat_detail_type = str(self.seat_detail_type or "000").strip()
        if choose_seats and not CHOOSE_SEATS_PATTERN.fullmatch(choose_seats):
            raise ValueError("choose_seats 必须由 1A/2F 形式的关系格子连接组成")
        if not re.fullmatch(r"[0-5]{3}", seat_detail_type):
            raise ValueError("seatDetailType 必须是下/中/上三个 0-5 数字")
        object.__setattr__(self, "choose_seats", choose_seats)
        object.__setattr__(self, "seat_detail_type", seat_detail_type)
        object.__setattr__(self, "warnings", tuple(str(item) for item in self.warnings if str(item)))

    @property
    def seatDetailType(self) -> str:  # noqa: N802 - mirrors the upstream field
        return self.seat_detail_type

    def to_form_fields(self) -> Mapping[str, str]:
        return {"choose_seats": self.choose_seats, "seatDetailType": self.seat_detail_type}


def build_order_preference_payload(
    seat_preference: SeatRelationPreference,
    berth_preference: BerthPreference,
    capabilities: OrderCapabilities,
    seat_type: str,
    passenger_count: int,
    dw_flag: str = "",
) -> OrderPreferencePayload:
    """Resolve configured preferences against the current order capabilities.

    Unsupported or unknown runtime capabilities intentionally produce an empty
    preference payload while keeping the order eligible for normal allocation.
    Invalid counts and malformed configuration are still rejected earlier by the
    model validators instead of being sent to 12306.
    """

    _validate_passenger_count(passenger_count)
    seat_type = str(seat_type or "").strip().upper()
    choose_seats = ""
    seat_detail_type = "000"
    warnings = []

    # Retained preferences for another seat family are inactive, not a runtime
    # downgrade. Only warn if an applicable preference is actually unavailable.
    if seat_preference.enabled and seat_type in SEATED_SEAT_TYPES:
        if not capabilities.can_choose_seats:
            warnings.append("12306未开放本次选座，已改为系统分配")
        elif seat_type not in capabilities.allowed_seat_types:
            warnings.append("12306未开放当前席别选座，已改为系统分配")
        else:
            try:
                choose_seats = seat_preference.to_choose_seats(passenger_count, seat_type, dw_flag)
            except ValueError as exc:
                warnings.append("%s；已改为系统分配" % exc)

    if berth_preference.enabled and seat_type in BERTH_SEAT_TYPES:
        if not capabilities.can_choose_beds:
            warnings.append(capabilities.berth_unavailable_message() + "，已改为系统分配")
        elif berth_preference.middle and not capabilities.can_choose_middle:
            warnings.append("当前列车/席别不支持中铺偏好，已改为系统分配")
        else:
            seat_detail_type = berth_preference.to_seat_detail_type(
                passenger_count, capabilities.can_choose_middle
            )

    return OrderPreferencePayload(choose_seats, seat_detail_type, tuple(warnings))
