import logging
from types import SimpleNamespace

import pytest

from ticket_app.configuration import AppError, PreparedPassengerSet, ResponseFormatError
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
    assert not any(event.kind == "preference_fallback" for event in runner.events)


@pytest.mark.parametrize(("train", "label", "code", "seat_detail", "choose_seats"), [
    ("G1", "二等座", "O", "000", "1A1F"),
    ("D1", "硬卧", "3", "200", ""),
    ("D1", "软卧", "4", "200", ""),
    ("K1", "高级软卧", "6", "200", ""),
    ("K1", "硬座", "1", "000", ""),
    ("K1", "软座", "2", "000", ""),
    ("G1", "无座", "O", "000", ""),
])
def test_mixed_preferences_apply_only_to_each_actual_candidate(train, label, code, seat_detail, choose_seats):
    capabilities = OrderCapabilities.from_mapping(
        {"canChooseSeats": "Y", "choose_Seats": "O", "canChooseBeds": "Y", "isCanChooseMid": "N"}
    )
    client = FakeBookingClient(capabilities)
    runner = make_runner(client, seat=SeatRelationPreference.from_value(["1A", "1F"]), berth=BerthPreference(lower=2))
    selected = candidate(label, code)
    selected["ticket"]["station_train_code"] = train
    assert runner._book_ticket(selected, {code: passengers()}) is True
    assert client.preference_payload.choose_seats == choose_seats
    assert client.preference_payload.seat_detail_type == seat_detail
    assert client.preference_payload.warnings == ()
    assert not any(event.kind == "preference_fallback" for event in runner.events)


@pytest.mark.parametrize(("label", "code", "expected_warning"), [
    ("二等座", "O", "未开放本次选座"),
    ("软卧", "4", "未开放在线选铺"),
])
def test_applicable_runtime_capability_denial_keeps_fallback_warning(label, code, expected_warning):
    client = FakeBookingClient(OrderCapabilities())
    runner = make_runner(client, seat=SeatRelationPreference.from_value(["1A", "1F"]), berth=BerthPreference(lower=2))
    assert runner._book_ticket(candidate(label, code), {code: passengers()}) is True
    assert client.preference_payload.choose_seats == ""
    assert client.preference_payload.seat_detail_type == "000"
    assert len(client.preference_payload.warnings) == 1
    assert expected_warning in client.preference_payload.warnings[0]
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


def test_malformed_init_dc_terminates_instead_of_trying_queue_or_another_candidate():
    class MalformedInitClient(FakeBookingClient):
        def init_dc(self):
            raise ResponseFormatError("initDc 返回的 ticketInfoForPassengerForm 格式无效，任务已停止")

        def get_queue_count(self, *_args):
            raise AssertionError("malformed initDc must never reach the queue endpoint")

    client = MalformedInitClient(OrderCapabilities())
    runner = make_runner(client)

    with pytest.raises(ResponseFormatError, match="initDc 返回"):
        runner._book_ticket(candidate(), {"O": passengers()})


def test_run_stops_after_first_candidate_init_dc_format_error_without_trying_second():
    """The outer candidate loop must not turn this unsafe error into fallback."""

    runner = object.__new__(TicketRunner)
    runner.cfg = SimpleNamespace(
        max_retries=1,
        auto_submit=True,
        perf_log=False,
        from_station="北京西",
        to_station="郑州东",
        train_date="2026-09-09",
        seat_types=["二等座"],
    )
    runner.cancel_token = CancellationToken()
    runner.event_sink = None
    runner.clock = SimpleNamespace(sync=lambda *_args: None)
    runner.stations = SimpleNamespace(load=lambda *_args: None, code=lambda name: name)
    runner.client = SimpleNamespace(ensure_login=lambda: None, query_tickets=lambda *_args: [])
    runner._phase = lambda *_args, **_kwargs: None
    runner._select_passengers = lambda: []
    runner._prepare_passengers_by_seat_code = lambda _passengers: {}
    runner._resolve_target_start = lambda: None
    runner._wait_for_query_start = lambda _target: None
    runner._should_stop = lambda: False
    runner._current_query_interval = lambda _target: 0
    runner._sleep = lambda _seconds: None
    first = {"ticket": {"station_train_code": "G1", "start_time": "08:00", "arrive_time": "09:00", "duration": "01:00"}, "seat_label": "二等座", "stock": "1"}
    second = {"ticket": {"station_train_code": "G2", "start_time": "10:00", "arrive_time": "11:00", "duration": "01:00"}, "seat_label": "二等座", "stock": "1"}
    runner._find_candidates = lambda _tickets: [first, second]
    attempted: list[str] = []

    def fail_init_format(candidate, _prepared):
        attempted.append(candidate["ticket"]["station_train_code"])
        raise ResponseFormatError("initDc 返回格式无效；尚未进入确认排队，本次任务已安全停止")

    runner._book_ticket = fail_init_format

    with pytest.raises(ResponseFormatError, match="尚未进入确认排队"):
        runner._run()
    assert attempted == ["G1"]


def test_check_order_format_error_does_not_queue_or_try_a_second_candidate():
    """A malformed pre-queue acknowledgement is terminal after submitOrder."""

    class MalformedCheckClient(FakeBookingClient):
        def __init__(self):
            super().__init__(OrderCapabilities())
            self.check_calls = 0
            self.queue_calls = 0

        def check_order_info(self, _passengers, _token):
            self.check_calls += 1
            raise ResponseFormatError(
                "checkOrderInfo 返回缺少或包含无效的 submitStatus 布尔字段，任务已停止"
            )

        def get_queue_count(self, *_args):
            self.queue_calls += 1
            raise AssertionError("malformed checkOrderInfo must never reach the queue endpoint")

    client = MalformedCheckClient()
    client.ensure_login = lambda: None
    client.query_tickets = lambda *_args: []
    runner = object.__new__(TicketRunner)
    runner.cfg = SimpleNamespace(
        max_retries=1,
        auto_submit=True,
        perf_log=False,
        from_station="北京西",
        to_station="郑州东",
        train_date="2026-09-09",
        seat_types=["二等座"],
        seat_relation_preference=SeatRelationPreference(),
        berth_preference=BerthPreference(),
        order_wait_attempts=1,
        order_wait_interval_seconds=0.001,
    )
    runner.cancel_token = CancellationToken()
    runner.event_sink = None
    runner.clock = SimpleNamespace(sync=lambda *_args: None)
    runner.stations = SimpleNamespace(load=lambda *_args: None, code=lambda name: name)
    runner.client = client
    runner._phase = lambda *_args, **_kwargs: None
    runner._select_passengers = lambda: []
    runner._prepare_passengers_by_seat_code = lambda _passengers: {"O": passengers()}
    runner._resolve_target_start = lambda: None
    runner._wait_for_query_start = lambda _target: None
    runner._should_stop = lambda: False
    runner._current_query_interval = lambda _target: 0
    runner._sleep = lambda _seconds: None
    first = {
        "ticket": {"station_train_code": "G1", "start_time": "08:00", "arrive_time": "09:00", "duration": "01:00"},
        "seat_label": "二等座",
        "seat_type": "O",
        "stock": "1",
    }
    second = {
        "ticket": {"station_train_code": "G2", "start_time": "10:00", "arrive_time": "11:00", "duration": "01:00"},
        "seat_label": "二等座",
        "seat_type": "O",
        "stock": "1",
    }
    runner._find_candidates = lambda _tickets: [first, second]

    with pytest.raises(ResponseFormatError, match="submitStatus"):
        runner._run()

    assert client.check_calls == 1
    assert client.queue_calls == 0
