from copy import deepcopy
from datetime import date, timedelta

import pytest

from ticket_app.configuration import AppConfig, AppError
from ticket_app.helpers import _build_passenger_strings
from ticket_app.passengers import is_student_ticket_rejection, normalize_passenger_ticket_types, select_passengers


def contact(name, identity_type="1"):
    return {
        "passenger_name": name, "passenger_type": identity_type,
        "passenger_id_type_code": "1", "passenger_id_no": "fixture-id-" + name,
        "mobile_no": "fixture-phone", "allEncStr": "fixture-encrypted-" + name,
    }


def config_values(**updates):
    values = {
        "from_station": "北京南", "to_station": "上海虹桥",
        "train_date": (date.today() + timedelta(days=1)).isoformat(),
        "passenger_names": ["学生甲", "学生乙", "成人"], "seat_types": ["二等座"],
        "only_preferred_trains": False,
    }
    values.update(updates)
    return values


def test_mixed_ticket_choices_preserve_contact_identity_and_order():
    contacts = [contact("学生甲", "3"), contact("学生乙", "3"), contact("成人")]
    original = deepcopy(contacts)
    selected = select_passengers(contacts, ["学生乙", "成人", "学生甲"], {"学生甲": "adult", "学生乙": "student"})
    for passenger in selected:
        passenger["seat_type"] = "O"
    tickets, old = _build_passenger_strings(selected)
    assert [part.split(",")[2] for part in tickets.split("_")] == ["3", "1", "1"]
    assert [part.split(",")[3] for part in old.rstrip("_").split("_")] == ["3", "1", "3"]
    assert [item["passenger_name"] for item in selected] == ["学生乙", "成人", "学生甲"]
    assert "fixture-encrypted-学生甲" in tickets
    assert contacts == original


def test_legacy_selection_follows_all_contact_types_without_overriding_identity():
    contacts = [contact(str(code), str(code)) for code in range(1, 5)]
    selected = select_passengers(contacts, ["1", "2", "3", "4"])
    assert [item["ticket_type"] for item in selected] == ["1", "2", "3", "4"]
    assert select_passengers([{"passenger_name": "无类型"}], ["无类型"])[0]["ticket_type"] == "1"


@pytest.mark.parametrize("identity_type", ["1", "2", "4", None])
def test_student_ticket_requires_student_contact(identity_type):
    with pytest.raises(AppError, match="不是学生"):
        select_passengers([contact("甲", identity_type)], ["甲"], {"甲": "student"})


def test_selected_duplicate_name_is_rejected_but_unselected_duplicate_does_not_block():
    contacts = [contact("重名"), contact("重名", "3"), contact("唯一")]
    with pytest.raises(AppError, match="多个同名"):
        select_passengers(contacts, ["重名"])
    assert select_passengers(contacts, ["唯一"])[0]["passenger_name"] == "唯一"


def test_missing_duplicate_and_unselected_override_are_errors():
    with pytest.raises(AppError, match="不存在"):
        select_passengers([], ["甲"])
    with pytest.raises(AppError, match="不能重复"):
        select_passengers([contact("甲")], ["甲", "甲"])
    with pytest.raises(AppError, match="未选择"):
        select_passengers([contact("甲")], ["甲"], {"乙": "adult"})


@pytest.mark.parametrize("value", [None, [], "adult", {"甲": "auto"}, {"甲": "3"}, {"甲": True}, {1: "adult"}, {" ": "adult"}, {"甲": "adult", " 甲 ": "student"}])
def test_invalid_ticket_type_mapping_is_not_silently_discarded(value):
    with pytest.raises(ValueError):
        normalize_passenger_ticket_types(value)


def test_name_normalization_preserves_internal_spaces():
    assert normalize_passenger_ticket_types({" Mary Ann ": "adult"}) == {"Mary Ann": "adult"}


def test_config_new_fields_round_trip_and_defaults_preserve_legacy_behavior():
    default = AppConfig.from_mapping(config_values())
    assert default.passenger_ticket_types == {}
    assert default.quiet_carriage_preference is False
    cfg = AppConfig.from_mapping(config_values(passenger_ticket_types={"学生甲": "adult", "学生乙": "student"}, quiet_carriage_preference=True))
    restored = AppConfig.from_mapping(cfg.to_mapping())
    assert restored.passenger_ticket_types == cfg.passenger_ticket_types
    assert restored.quiet_carriage_preference is True
    default.passenger_ticket_types["学生甲"] = "adult"
    assert AppConfig.from_mapping(config_values()).passenger_ticket_types == {}


def test_config_rejects_unselected_names_and_invalid_types():
    with pytest.raises(AppError, match="未选择"):
        AppConfig.from_mapping(config_values(passenger_ticket_types={"别人": "adult"}))
    with pytest.raises(AppError, match="仅支持"):
        AppConfig.from_mapping(config_values(passenger_ticket_types={"学生甲": "invalid"}))


@pytest.mark.parametrize("message", [
    "学生优惠次数已用完，请购买成人票", "优惠乘车次数不足", "学生票优惠次数为0",
    "学生优惠资质未通过核验", "请先完成学生资质核验，当前学生资质未认证",
    "学生优惠资格已过期", "学生优惠区间不一致", "乘车人不是学生，不能购买学生票",
])
def test_explicit_student_eligibility_rejections_are_recognized(message):
    assert is_student_ticket_rejection(message)


@pytest.mark.parametrize("message", [
    "31012", 31012, None, {}, "学生票出票状态：31012", "学生票订单正在排队",
    "学生资质核验成功", "学生票剩余优惠次数为1", "余票不足", "系统繁忙，请稍后重试",
    "学生票订单尚未完成支付", "学生票剩余优惠次数为1，余票不足",
])
def test_unknown_codes_progress_and_ordinary_stock_failures_are_not_reclassified(message):
    assert not is_student_ticket_rejection(message)
