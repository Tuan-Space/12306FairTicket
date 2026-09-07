import logging
from types import SimpleNamespace

import pytest

from ticket_app.configuration import AppError, PreparedPassengerSet
from ticket_app.preferences import (
    BerthPreference,
    OrderCapabilities,
    OrderCheckResult,
    SeatRelationPreference,
)
from ticket_app.runner import TicketRunner
from ticket_app.runtime import CancellationToken, RunCancelled


class FakeBookingClient:
    def __init__(self, capabilities: OrderCapabilities, wait_results=None, queue_data=None):
        self.capabilities = capabilities
        self.wait_results = list(wait_results or [(True, {"orderId": "ORDER-1"})])
        self.queue_data = queue_data or {"ticket": "left"}
        self.preference_payload = None
        self.after_queue = None
        self.after_confirm = None

    def submit_order_request(self, _ticket):
        return True, "OK"

    def init_dc(self):
        return "token", {"leftTicketStr": "left", "dw_flag": "a,b,c,S"}

    def check_order_info(self, _passengers, _token):
        return OrderCheckResult(True, "OK", self.capabilities)

    def get_queue_count(self, _ticket, _ticket_info, _seat_type, _token):
        if self.after_queue:
            self.after_queue()
        return True, self.queue_data

    def confirm_single_for_queue(
        self, _passengers, _ticket_info, _left_ticket, _token, preference_payload
    ):
        self.preference_payload = preference_payload
        if self.after_confirm:
            self.after_confirm()
        return True, "OK"

    def query_order_wait_time(self, _token):
        if len(self.wait_results) > 1:
            return self.wait_results.pop(0)
        return self.wait_results[0]


def make_runner(client, *, seat=None, berth=None, attempts=2):
    runner = object.__new__(TicketRunner)
    runner.cfg = SimpleNamespace(
        seat_relation_preference=seat or SeatRelationPreference(),
        berth_preference=berth or BerthPreference(),
        order_wait_attempts=attempts,
        order_wait_interval_seconds=0.001,
        perf_log=False,
    )
    runner.client = client
    runner.cancel_token = CancellationToken()
    runner.events = []
    runner.event_sink = runner.events.append
    return runner


def candidate(label="二等座", code="O"):
    return {
        "ticket": {
            "station_train_code": "G1",
            "left_ticket": "left",
        },
        "seat_label": label,
        "seat_type": code,
        "found_perf": None,
    }


def passengers(count=2):
    return PreparedPassengerSet([{} for _ in range(count)], "ticket", "old")


def test_runner_sends_one_capability_checked_seat_payload():
    capabilities = OrderCapabilities.from_mapping(
        {"canChooseSeats": "Y", "choose_Seats": "O", "canChooseBeds": "N"}
    )
    client = FakeBookingClient(capabilities)
    runner = make_runner(
        client,
        seat=SeatRelationPreference.from_value(["1A", "1F"]),
    )

    assert runner._book_ticket(candidate(), {"O": passengers()}) is True
    assert client.preference_payload.choose_seats == "1A1F"
    assert client.preference_payload.seat_detail_type == "000"
    assert any(event.kind == "preference_applied" for event in runner.events)


def test_queue_log_does_not_expose_left_ticket(caplog):
    secret = "VERY_SECRET_LEFT_TICKET"
    client = FakeBookingClient(
        OrderCapabilities(),
        queue_data={"ticket": secret, "count": "2"},
    )
    runner = make_runner(client)

    with caplog.at_level(logging.INFO):
        assert runner._book_ticket(candidate(), {"O": passengers()}) is True

    assert secret not in caplog.text
    assert "当前队列人数: 2" in caplog.text


def test_no_seat_never_reuses_the_o_code_as_a_physical_seat_preference():
    capabilities = OrderCapabilities.from_mapping(
        {"canChooseSeats": "Y", "choose_Seats": "O", "canChooseBeds": "N"}
    )
    client = FakeBookingClient(capabilities)
    runner = make_runner(
        client,
        seat=SeatRelationPreference.from_value(["1A", "1F"]),
    )

    assert runner._book_ticket(candidate("无座", "O"), {"O": passengers()}) is True
    assert client.preference_payload.choose_seats == ""
    assert any(event.kind == "preference_fallback" for event in runner.events)


def test_unknown_queue_result_stops_instead_of_trying_another_order():
    client = FakeBookingClient(
        OrderCapabilities(),
        wait_results=[(True, {"waitTime": -100, "msg": ""})],
    )
    runner = make_runner(client, attempts=1)

    with pytest.raises(AppError, match="避免重复下单"):
        runner._book_ticket(candidate(), {"O": passengers()})


def test_cancel_before_irreversible_confirm_never_sends_confirm_request():
    client = FakeBookingClient(OrderCapabilities())
    runner = make_runner(client)
    client.after_queue = runner.cancel_token.cancel

    with pytest.raises(RunCancelled, match="任务已取消"):
        runner._book_ticket(candidate(), {"O": passengers()})
    assert client.preference_payload is None


def test_cancel_after_confirm_reports_an_unknown_submitted_order():
    client = FakeBookingClient(OrderCapabilities())
    runner = make_runner(client)
    client.after_confirm = runner.cancel_token.cancel

    with pytest.raises(AppError, match="订单已提交排队"):
        runner._book_ticket(candidate(), {"O": passengers()})
    assert client.preference_payload is not None
