from ticket_app.cart import CartItem
"""Offline cancellation, order disposition, and fixed deadline regressions."""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import requests

from ticket_app.configuration import AppError, PreparedPassengerSet, ResponseFormatError
from ticket_app.preferences import BerthPreference, OrderCapabilities, OrderCheckResult, SeatRelationPreference
from ticket_app.runner import TicketRunner
from ticket_app.runtime import CancellationToken, RunCancelled


NOW = datetime(2030, 1, 2, 12, 0, 0)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(requests.Session, "request", lambda *_args, **_kwargs: pytest.fail("Unexpected network request"))


class Clock:
    def __init__(self, now=NOW):
        self.current = now
        self.sync_calls = []

    def now(self):
        return self.current

    def sync(self, token, sink, **kwargs):
        self.sync_calls.append(kwargs)
        token.checkpoint()
        return True


class VirtualToken(CancellationToken):
    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.supports_actions = True
        self.waits = []

    def wait(self, seconds):
        self.checkpoint()
        self.waits.append(seconds)
        self.clock.current += timedelta(seconds=seconds)
        self.checkpoint()

    def next_action(self, timeout=0.0):
        action = super().next_action(0)
        if action is None:
            self.wait(timeout)
        return action


class BookingClient:
    def __init__(self):
        self.calls = []
        self.hook = lambda _stage: None
        self.confirm_result = (True, "OK")
        self.wait_result = (True, {"orderId": "MOCK-ORDER"})

    def _visit(self, stage):
        self.calls.append(stage)
        self.hook(stage)

    def submit_order_request(self, ticket):
        self._visit("submit")
        return True, "OK"

    def init_dc(self):
        self._visit("init")
        return "mock-token", {"leftTicketStr": "mock-left"}

    def check_order_info(self, passengers, token):
        self._visit("check")
        return OrderCheckResult(True, "OK", OrderCapabilities())

    def get_queue_count(self, *args):
        self._visit("count")
        return True, {"ticket": "mock-left", "count": 0}

    def confirm_single_for_queue(self, *args):
        self._visit("confirm")
        return self.confirm_result

    def query_order_wait_time(self, token):
        self._visit("wait")
        return self.wait_result


def booking_runner():
    runner = object.__new__(TicketRunner)
    runner.cfg = SimpleNamespace(
        seat_relation_preference=SeatRelationPreference(), berth_preference=BerthPreference(),
        order_wait_attempts=1, order_wait_interval_seconds=0.01, perf_log=False, stop_at="",
    )
    runner.clock = Clock()
    runner.cancel_token = VirtualToken(runner.clock)
    runner.client = BookingClient()
    runner.events = []
    runner.event_sink = runner.events.append
    return runner


def book(runner):
    return runner._book_ticket({
        "ticket": {"station_train_code": "G1", "query_date": "2030-01-02", "left_ticket": "mock-left"},
        "seat_label": "二等座", "seat_type": "O", "found_perf": None,
    }, {"O": PreparedPassengerSet([{}], "mock-passenger", "mock-old")})


@pytest.mark.parametrize("stage", ["submit", "init", "check", "count"])
def test_cancellation_between_preparation_requests_never_sends_later_request(stage):
    runner = booking_runner()
    runner.client.hook = lambda current: runner.cancel_token.cancel() if current == stage else None
    with pytest.raises(RunCancelled):
        book(runner)
    sequence = ["submit", "init", "check", "count"]
    assert runner.client.calls == sequence[:sequence.index(stage) + 1]
    assert runner.order_state == "safe"
    assert runner.safe_to_restart


def test_confirm_and_queue_states_are_set_before_their_network_calls():
    runner = booking_runner()
    states = []
    runner.client.hook = lambda stage: states.append((stage, runner.order_state, runner.safe_to_restart))
    assert book(runner)
    assert ("confirm", "confirm_sent", False) in states
    assert ("wait", "queued", False) in states
    assert runner.order_state == "success"
    assert not runner.safe_to_restart


@pytest.mark.parametrize("stage", ["confirm", "wait"])
@pytest.mark.parametrize("failure", [requests.Timeout("mock timeout"), ResponseFormatError("mock malformed response")])
def test_uncertain_confirmation_or_queue_response_blocks_restart(stage, failure):
    runner = booking_runner()

    def fail(current):
        if current == stage:
            raise failure

    runner.client.hook = fail
    with pytest.raises((AppError, requests.Timeout)):
        book(runner)
    assert runner.order_state == "unknown"
    assert not runner.safe_to_restart
    count = len(runner.client.calls)
    with pytest.raises(AppError, match="不能重复下单"):
        book(runner)
    assert len(runner.client.calls) == count


def test_cancel_during_confirm_accounts_for_submitted_result_and_blocks_restart():
    runner = booking_runner()
    runner.client.hook = lambda stage: runner.cancel_token.cancel() if stage == "confirm" else None
    with pytest.raises(AppError, match="订单已提交排队"):
        book(runner)
    assert runner.client.calls[-1] == "confirm"
    assert runner.order_state == "unknown"
    assert not runner.safe_to_restart


def test_cancellation_exception_after_confirm_started_remains_unsafe():
    runner = booking_runner()

    def interrupt(stage):
        if stage == "confirm":
            raise RunCancelled("mock cancellation after send")

    runner.client.hook = interrupt
    with pytest.raises(RunCancelled):
        book(runner)
    assert runner.order_state == "unknown"
    assert not runner.safe_to_restart


def test_cancelled_pending_queue_reply_blocks_restart():
    runner = booking_runner()
    runner.client.wait_result = (True, {"waitTime": 10})
    runner.client.hook = lambda stage: runner.cancel_token.cancel() if stage == "wait" else None
    with pytest.raises(AppError, match="订单已提交排队"):
        book(runner)
    assert runner.order_state == "unknown"
    assert not runner.safe_to_restart


def test_cancel_while_queue_reply_contains_success_preserves_success():
    runner = booking_runner()
    runner.client.hook = lambda stage: runner.cancel_token.cancel() if stage == "wait" else None
    assert book(runner)
    assert runner.cancel_token.is_cancelled
    assert runner.order_state == "success"
    assert not runner.safe_to_restart
    assert any(event.kind == "order_success" for event in runner.events)


def test_success_survives_a_late_event_sink_failure():
    runner = booking_runner()

    def sink(event):
        if event.kind == "order_success":
            raise RuntimeError("mock UI callback failed")

    runner.event_sink = sink
    with pytest.raises(RuntimeError, match="mock UI"):
        book(runner)
    assert runner.order_state == "success"
    assert not runner.safe_to_restart
    runner._set_order_state("unknown")
    assert runner.order_state == "success"


def test_explicit_confirmation_rejection_is_safe_even_if_cancel_arrived_with_reply():
    runner = booking_runner()
    runner.client.confirm_result = (False, "余票不足")
    runner.client.hook = lambda stage: runner.cancel_token.cancel() if stage == "confirm" else None
    assert book(runner) is False
    assert runner.order_state == "safe"
    assert runner.safe_to_restart


@pytest.mark.parametrize("ok", [True, False])
def test_explicit_terminal_queue_failure_is_safe(ok):
    runner = booking_runner()
    runner.client.wait_result = (ok, {"msg": "出票失败，余票不足"})
    assert book(runner) is False
    assert runner.order_state == "safe"
    assert runner.safe_to_restart


@pytest.mark.parametrize("ok", [True, False])
def test_explicit_student_rejection_after_queue_can_be_corrected_safely(ok):
    runner = booking_runner()
    runner.client.wait_result = (ok, {"msg": "学生优惠次数已用完"})
    with pytest.raises(AppError, match="修改相应乘车人票种"):
        book(runner)
    assert runner.order_state == "safe"
    assert runner.safe_to_restart


@pytest.mark.parametrize("result", [(True, {"waitTime": -100}), (False, {"msg": "系统繁忙"})])
def test_exhausted_unknown_queue_result_does_not_become_safe(result):
    runner = booking_runner()
    runner.client.wait_result = result
    with pytest.raises(AppError, match="避免重复下单"):
        book(runner)
    assert runner.order_state == "unknown"
    assert not runner.safe_to_restart


def full_runner(*, now=NOW, stop_at="", start_at=""):
    runner = booking_runner()
    runner.clock = Clock(now)
    runner.cancel_token = VirtualToken(runner.clock)
    runner.cfg = SimpleNamespace(
        stop_at=stop_at, start_at=start_at, auto_submit=False, pre_query_seconds=2,
        query_interval_seconds=1, hot_query_interval_seconds=1, hot_window_seconds=5,
        max_retries=2, cart_items=[CartItem("北京西", "郑州东", "all", "", "二等座")],
        train_date="2030-01-03", perf_log=False,
    )
    runner.client.ensure_login = lambda **_kwargs: None
    runner.client.check_session = lambda: True
    runner.client.query_calls = 0

    def query(*_args, before_request):
        before_request()
        runner.client.query_calls += 1
        return SimpleNamespace(success=True, tickets=[])

    runner.client.query_tickets_result = query
    runner.stations = SimpleNamespace(load=lambda *_args: None, code=lambda name: name)
    return runner


@pytest.mark.parametrize("stop_at", ["11:59:59", "12:00:00", "2030-01-02 11:59:59"])
def test_past_or_current_stop_time_finishes_before_initial_network_work(stop_at):
    runner = full_runner(stop_at=stop_at)
    runner.client.ensure_login = lambda: pytest.fail("Expired deadline must stop before login")
    assert runner.run() == 1
    assert runner.clock.sync_calls == []
    assert runner.client.query_calls == 0
    assert runner.safe_to_restart
    assert any(event.kind == "phase" and event.data["phase"] == "stopped" for event in runner.events)


def test_daily_stop_time_stays_expired_just_after_deadline_and_after_midnight():
    runner = full_runner(stop_at="12:00:01")
    assert not runner._should_stop()
    runner.clock.current += timedelta(seconds=2)
    assert runner._should_stop()
    runner.clock.current += timedelta(days=1)
    assert runner._should_stop()
    assert runner._stop_deadline == NOW + timedelta(seconds=1)


def test_waiting_for_future_sale_stops_at_deadline_without_querying():
    runner = full_runner(stop_at="12:00:05", start_at="13:00:00")
    assert runner.run() == 1
    assert runner.clock.current == NOW + timedelta(seconds=5)
    assert runner.client.query_calls == 0
    assert runner.safe_to_restart


def test_query_interval_wait_is_cut_short_at_deadline():
    runner = full_runner(stop_at="12:00:05")
    runner.cfg.query_interval_seconds = 60
    assert runner.run() == 1
    assert runner.clock.current == NOW + timedelta(seconds=5)
    assert runner.client.query_calls == 1
    assert runner.safe_to_restart


def test_long_login_wait_is_bounded_by_the_run_deadline():
    runner = full_runner(stop_at="12:00:05", start_at="13:00:00")

    def login(**_kwargs):
        runner.client.cancel_token.wait(60)
        pytest.fail("Long login wait must not outlive STOP_AT")

    runner.client.ensure_login = login
    assert runner.run() == 1
    assert runner.clock.current == NOW + timedelta(seconds=5)
    assert runner.client.query_calls == 0
    assert runner.safe_to_restart


def test_maintenance_login_cannot_outlive_deadline_or_reload_contacts():
    runner = full_runner(stop_at="12:00:05", start_at="13:00:00")
    token = runner.cancel_token
    initial_login = True

    def login(**kwargs):
        nonlocal initial_login
        if initial_login:
            initial_login = False
            token.set_actions_enabled(True)
            assert token.request_action("login")
            return
        assert kwargs == {"check_first": False}
        runner.client.cancel_token.wait(300)
        pytest.fail("Maintenance login should stop at the task deadline")

    runner.client.ensure_login = login
    assert runner.run() == 1
    assert runner.clock.current == NOW + timedelta(seconds=5)
    assert runner.client.query_calls == 0
    assert not token.request_action("login")


def test_maintenance_sync_finishing_after_deadline_never_starts_querying():
    runner = full_runner(stop_at="12:00:05", start_at="13:00:00")
    sync_calls = []

    def sync(token, sink, **kwargs):
        sync_calls.append(kwargs)
        if len(sync_calls) == 1:
            token.set_actions_enabled(True)
            assert token.request_action("sync_clock")
            return True
        assert kwargs["budget_seconds"] == 5
        runner.clock.current += timedelta(seconds=6)
        return True

    runner.clock.sync = sync
    assert runner.run() == 1
    assert len(sync_calls) == 2
    assert runner.client.query_calls == 0
    assert runner.safe_to_restart


def test_deadline_passed_during_preparation_prevents_irreversible_confirm():
    runner = booking_runner()
    runner.cfg.stop_at = "12:00:01"

    def slow_count(stage):
        if stage == "count":
            runner.clock.current += timedelta(seconds=2)

    runner.client.hook = slow_count
    with pytest.raises(RunCancelled, match="停止时间"):
        book(runner)
    assert runner.client.calls == ["submit", "init", "check", "count"]
    assert runner.order_state == "safe"
    assert runner.safe_to_restart


def test_stop_time_does_not_discard_a_confirmed_order_result():
    runner = full_runner(stop_at="12:00:01")
    # Install the same bounded token used by the public run without initiating
    # an order through the query loop, then exercise the real booking chain.
    runner._resolve_stop_deadline()
    from ticket_app.runner import _DeadlineToken
    runner.cancel_token = _DeadlineToken(runner.cancel_token, runner._remaining_until_stop)
    runner.client.cancel_token = runner.cancel_token
    runner.cfg.seat_relation_preference = SeatRelationPreference()
    runner.cfg.berth_preference = BerthPreference()
    runner.cfg.order_wait_attempts = 1
    runner.cfg.order_wait_interval_seconds = 1

    def slow_confirm(stage):
        if stage == "confirm":
            runner.clock.current += timedelta(seconds=3)

    runner.client.hook = slow_confirm
    assert book(runner)
    assert runner.order_state == "success"
    assert not runner.safe_to_restart


def test_expired_query_wait_returns_deadline_code_instead_of_user_cancel_code():
    runner = full_runner(stop_at="12:00:00")
    assert runner.run() == 1
    runner = full_runner(stop_at="12:00:05")
    runner.cancel_token.cancel()
    assert runner.run() == 130
