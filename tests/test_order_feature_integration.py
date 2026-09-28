"""Exercise the new choices through the real runner and HTTP form builders."""

import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from ticket_app.configuration import AppConfig
from ticket_app.runner import TicketRunner


class OfflineOrderSession:
    def __init__(self, quiet_script):
        self.headers = {}
        self.posts = []
        self.quiet_script = quiet_script

    def post(self, url, *, data, timeout):
        endpoint = url.rsplit("/", 1)[-1]
        self.posts.append((endpoint, dict(data)))
        if endpoint == "getPassengerDTOs":
            passengers = [
                {"passenger_name": name, "passenger_type": "3",
                 "passenger_id_type_code": "1", "passenger_id_no": "fixture-id",
                 "mobile_no": "fixture-mobile", "allEncStr": "fixture-enc"}
                for name in ("学生甲", "学生乙")
            ]
            payload = {"data": {"normal_passengers": passengers}}
        elif endpoint == "submitOrderRequest":
            payload = {"status": True}
        elif endpoint == "initDc":
            info = {"leftTicketStr": "fixture-left", "train_location": "Q1",
                    "key_check_isChange": "fixture-key"}
            html = ("<script>var globalRepeatSubmitToken = 'fixture-token';\n"
                    "var ticketInfoForPassengerForm = " + json.dumps(info) + ";\n"
                    + self.quiet_script + "\n</script>")
            return SimpleNamespace(text=html, status_code=200)
        elif endpoint == "checkOrderInfo":
            payload = {"status": True, "data": {"submitStatus": True,
                       "canChooseSeats": "Y", "choose_Seats": "O"}}
        elif endpoint == "getQueueCount":
            payload = {"status": True, "data": {"ticket": "fixture-left", "count": 0}}
        elif endpoint == "confirmSingleForQueue":
            payload = {"status": True, "data": {"submitStatus": True}}
        else:
            raise AssertionError(f"Unexpected order endpoint: {endpoint}")
        return SimpleNamespace(json=lambda: payload, status_code=200)

    def get(self, url, *, params, timeout):
        assert url.endswith("/queryOrderWaitTime")
        return SimpleNamespace(json=lambda: {"status": True, "data": {"orderId": "fixture-order"}},
                               status_code=200)


@pytest.mark.parametrize("label,code", [("二等座", "O"), ("无座", "O"), ("一等卧", "I"), ("硬座", "1")])
@pytest.mark.parametrize("quiet_script,available", [
    ("var is_jy = 'Y';", True), ("var is_jy = 'N';", False),
    ("", False), ("var is_jy = 'Y';\nis_jy = 'X';", False),
])
def test_ticket_types_and_quiet_preference_reach_exactly_one_final_form(label, code, quiet_script, available):
    cfg = AppConfig.from_mapping({
        "from_station": "北京南", "to_station": "上海虹桥",
        "train_date": (date.today() + timedelta(days=1)).isoformat(),
        "passenger_names": ["学生甲", "学生乙"],
        "passenger_ticket_types": {"学生甲": "adult", "学生乙": "student"},
        "seat_types": [label], "only_preferred_trains": False,
        "quiet_carriage_preference": True, "persist_session": False, "perf_log": False,
    })
    session = OfflineOrderSession(quiet_script)
    runner = TicketRunner(cfg, session=session)
    selected = runner._select_passengers()
    prepared = runner._prepare_passengers_by_seat_code(selected)
    candidate = {"seat_label": label, "seat_type": code, "stock": "有", "ticket": {
        "station_train_code": "D1", "train_no": "fixture-train", "date": cfg.train_date,
        "from_station": cfg.from_station, "to_station": cfg.to_station,
        "secret_str": "fixture-secret", "left_ticket": "fixture-left",
    }}
    assert runner._book_ticket(candidate, prepared)
    confirmations = [data for endpoint, data in session.posts if endpoint == "confirmSingleForQueue"]
    assert len(confirmations) == 1
    form = confirmations[0]
    assert form["is_jy"] == ("Y" if available and label == "二等座" else "N")
    ticket_rows = [row.split(",") for row in form["passengerTicketStr"].split("_")]
    assert [row[0] for row in ticket_rows] == [code, code]
    assert [row[2] for row in ticket_rows] == ["1", "3"]
    identity_rows = [row.split(",") for row in form["oldPassengerStr"].rstrip("_").split("_")]
    assert [row[3] for row in identity_rows] == ["3", "3"]
    assert all(item["passenger_type"] == "3" for item in selected)
    assert [endpoint for endpoint, _ in session.posts] == [
        "getPassengerDTOs", "submitOrderRequest", "initDc", "checkOrderInfo",
        "getQueueCount", "confirmSingleForQueue",
    ]
