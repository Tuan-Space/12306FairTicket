"""Offline regression for shared query columns and distinct sleeper order codes."""

from datetime import date, timedelta
import json
import logging
from types import SimpleNamespace

import pytest
import requests

from ticket_app.client import RailwayClient
from ticket_app.configuration import AppConfig, preference_capabilities
from ticket_app.gui.compat import DEFAULT_VALUES, build_app_config, load_gui_settings, save_gui_settings
from ticket_app.runner import TicketRunner


@pytest.fixture(autouse=True)
def reject_network(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Sleeper regression must not access the network")
    monkeypatch.setattr(requests.Session, "request", reject)


def settings(**updates):
    values = {**DEFAULT_VALUES, "from_station": "北京西", "to_station": "郑州东",
              "train_date": (date.today() + timedelta(days=1)).isoformat(),
              "passenger_names": ["测试甲"], "seat_types": ["软卧", "一等卧", "硬卧", "二等卧"],
              "preferred_trains": [], "only_preferred_trains": False, "persist_session": False}
    values.update(updates)
    return values


def ticket(codes, train="D1", rw="有", yw="2", edz="无"):
    parts = [""] * 36
    parts[0], parts[1], parts[2], parts[3], parts[11] = "test-secret", "预订", train + "-no", train, "Y"
    parts[13] = date.today().strftime("%Y%m%d")
    parts[23], parts[28], parts[30], parts[35] = rw, yw, edz, codes
    return RailwayClient._parse_tickets(["|".join(parts)], {})[0]


@pytest.mark.parametrize("codes,expected", [
    ("4", [("软卧", "4")]), ("I", [("一等卧", "I")]),
    ("3", [("硬卧", "3")]), ("J", [("二等卧", "J")]),
    ("4J", [("软卧", "4"), ("二等卧", "J")]),
    ("I3", [("一等卧", "I"), ("硬卧", "3")]),
    ("IJ", [("一等卧", "I"), ("二等卧", "J")]),
    ("43", [("软卧", "4"), ("硬卧", "3")]), ("6O", []),
])
@pytest.mark.parametrize("train", ["D1", "G1", "K1", "1234"])
def test_actual_codes_distinguish_shared_stock_without_prefix_guessing(codes, expected, train):
    runner = TicketRunner(AppConfig.from_mapping(settings()))
    parsed = ticket(codes, train)
    assert parsed["seat_types"] == codes
    candidates = runner._find_candidates([parsed])
    assert [(item["seat_label"], item["seat_type"]) for item in candidates] == expected


@pytest.mark.parametrize("codes,warnings", [("", 2), ("4I3J", 2), ("4I", 1), ("3J", 1)])
def test_ambiguous_shared_columns_are_skipped_with_deduplicated_logs(codes, warnings, caplog):
    runner = TicketRunner(AppConfig.from_mapping(settings(seat_types=["软卧", "一等卧", "硬卧", "二等卧", "二等座"])))
    parsed = ticket(codes, edz="有")
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            candidates = runner._find_candidates([parsed])
            assert [item["seat_label"] for item in candidates] == ["二等座"]
    assert len(caplog.records) == warnings
    assert "无法区分实际席别" in caplog.text
    assert "test-secret" not in caplog.text


def test_short_query_row_and_empty_stock_do_not_create_sleeper_candidates(caplog):
    parsed = RailwayClient._parse_tickets(["|".join([""] * 35)], {})[0]
    assert parsed["seat_types"] == ""
    runner = TicketRunner(AppConfig.from_mapping(settings()))
    assert runner._find_candidates([ticket("IJ", rw="0", yw="无")]) == []
    assert not caplog.records


@pytest.mark.parametrize("strategy,expected", [
    ("train_first", [("D2", "J"), ("D2", "I"), ("D1", "J"), ("D1", "I")]),
    ("seat_first", [("D2", "J"), ("D1", "J"), ("D2", "I"), ("D1", "I")]),
])
def test_new_sleeper_candidates_preserve_both_priority_strategies(strategy, expected):
    cfg = AppConfig.from_mapping(settings(seat_types=["二等卧", "一等卧"], preferred_trains=["D2", "D1"], priority_strategy=strategy))
    candidates = TicketRunner(cfg)._find_candidates([ticket("IJ", "D1"), ticket("IJ", "D2")])
    assert [(item["ticket"]["station_train_code"], item["seat_type"]) for item in candidates] == expected


@pytest.mark.parametrize("label,code", [("一等卧", "I"), ("二等卧", "J")])
@pytest.mark.parametrize("beds,middle,requested_middle,detail", [
    ("Y", "Y", 0, "100"), ("Y", "Y", 1, "010"),
    ("Y", "N", 1, "000"), ("N", "N", 0, "000"),
])
def test_parse_to_order_sends_exact_code_and_capability_checked_berths(
    monkeypatch, label, code, beds, middle, requested_middle, detail
):
    cfg = AppConfig.from_mapping(settings(seat_types=[label], berth_preference={"lower": 1-requested_middle, "middle": requested_middle}))
    runner = TicketRunner(cfg)
    calls = {}
    def post(url, data, **kwargs):
        endpoint = url.rsplit("/", 1)[-1]
        calls[endpoint] = data
        responses = {
            "checkOrderInfo": {"submitStatus": True, "canChooseBeds": beds, "isCanChooseMid": middle},
            "getQueueCount": {"ticket": "test-left", "count": "0"},
            "confirmSingleForQueue": {"submitStatus": True},
        }
        return SimpleNamespace(json=lambda: {"status": True, "data": responses[endpoint]})
    monkeypatch.setattr(runner.client.session, "post", post)
    monkeypatch.setattr(runner.client, "submit_order_request", lambda _ticket: (True, "OK"))
    monkeypatch.setattr(runner.client, "init_dc", lambda: ("test-token", {}))
    monkeypatch.setattr(runner.client, "query_order_wait_time", lambda _token: (True, {"orderId": "test-order"}))
    passenger = {"passenger_name": "测试甲", "passenger_id_no": "test-id", "passenger_id_type_code": "1"}
    prepared = runner._prepare_passengers_by_seat_code([passenger])
    selected = runner._find_candidates([ticket(code)])[0]
    assert runner._book_ticket(selected, prepared)
    assert calls["getQueueCount"]["seatType"] == code
    for endpoint in ("checkOrderInfo", "confirmSingleForQueue"):
        assert calls[endpoint]["passengerTicketStr"].startswith(code + ",")
    assert calls["confirmSingleForQueue"]["choose_seats"] == ""
    assert calls["confirmSingleForQueue"]["seatDetailType"] == detail
    assert "seat_type" not in passenger


@pytest.mark.parametrize("seats", [["一等卧"], ["二等卧"], ["二等卧", "软卧", "一等卧", "硬卧"]])
def test_gui_json_and_cli_config_accept_exact_new_labels(tmp_path, seats):
    values = settings(seat_types=seats, berth_preference={"lower": 1})
    path = tmp_path / "settings.json"
    save_gui_settings(path, values)
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 3
    loaded = load_gui_settings(path)
    assert loaded["seat_types"] == seats
    assert build_app_config(loaded).seat_types == seats
    assert AppConfig.from_mapping({**values, "SEAT_TYPES": seats}).seat_types == seats
    assert preference_capabilities(seats) == (False, True)
    assert build_app_config({**loaded, "seat_types": ["硬座"], "passenger_names": ["甲", "乙"]})
