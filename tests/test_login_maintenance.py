"""Offline scheduling, relogin and serialized maintenance regression tests."""

from collections import deque
from datetime import datetime
from types import SimpleNamespace

import pytest
import requests

from ticket_app.clock import ServerClock
from ticket_app.configuration import AppError
from ticket_app.runner import TicketRunner
from ticket_app.runtime import CancellationToken, RunCancelled


TARGET = 1_800_000_000.0


class VirtualClock:
    def __init__(self, current):
        self.current = current
        self.syncs = []

    def now(self):
        return datetime.fromtimestamp(self.current)

    def sync(self, _cancel, _events, **kwargs):
        self.syncs.append((self.current, kwargs))
        return True


class VirtualToken(CancellationToken):
    def __init__(self, clock, actions=(), interactive=True):
        super().__init__()
        self.supports_actions = interactive
        self.clock = clock
        self.scheduled_actions = deque(actions)
        self.waits = []

    def next_action(self, timeout=0):
        self.checkpoint()
        self.waits.append((self.clock.current, timeout))
        end = datetime.fromtimestamp(self.clock.current + timeout).timestamp()
        if self.scheduled_actions and self.scheduled_actions[0][0] <= end:
            when, action = self.scheduled_actions.popleft()
            self.clock.current = max(self.clock.current, when)
            if action == "cancel":
                self.cancel()
                self.checkpoint()
            return action
        self.clock.current = end
        return None


def make_runner(monkeypatch, *, start=TARGET - 400, pre_query=2, checks=(True,),
                logins=(True,), actions=(), interactive=True, initial_checked=True):
    monkeypatch.setattr("ticket_app.runner.time.monotonic", lambda: 100.0)
    runner = object.__new__(TicketRunner)
    runner.cfg = SimpleNamespace(pre_query_seconds=pre_query, auto_submit=True)
    runner.clock = VirtualClock(start)
    runner.cancel_token = VirtualToken(runner.clock, actions, interactive)
    runner.events = []
    runner.event_sink = runner.events.append
    if initial_checked:
        runner._last_login_check_monotonic = 100.0
    check_values = deque(checks)
    login_values = deque(logins)
    runner.check_calls = []
    runner.login_calls = []
    runner.reloads = []

    def check_session():
        runner.check_calls.append(runner.clock.current)
        value = check_values.popleft()
        if isinstance(value, Exception):
            raise value
        return value

    def ensure_login(*, check_first):
        assert check_first is False
        runner.login_calls.append(runner.clock.current)
        value = login_values.popleft()
        if isinstance(value, Exception):
            raise value
        return value

    def select():
        runner.reloads.append(runner.clock.current)
        return [{"new_account": True}]

    runner.client = SimpleNamespace(check_session=check_session, ensure_login=ensure_login)
    runner._select_passengers = select
    runner._prepare_passengers_by_seat_code = lambda passengers: {"O": passengers}
    runner._prepared_passengers = {"O": [{"old_account": True}]}
    return runner


def wait(runner):
    runner._wait_for_query_start(datetime.fromtimestamp(TARGET))


def test_regular_schedule_checks_once_syncs_once_and_preserves_fine_warmup(monkeypatch):
    runner = make_runner(monkeypatch)
    wait(runner)
    assert runner.check_calls == pytest.approx([TARGET - 180], abs=0.004)
    assert runner.clock.syncs[0][0] == pytest.approx(TARGET - 60, abs=0.004)
    assert len(runner.clock.syncs) == 1
    assert runner.clock.current == pytest.approx(TARGET - 2, abs=0.004)
    assert any(timeout == 0.003 for _, timeout in runner.cancel_token.waits)
    assert any(timeout == 0.02 for _, timeout in runner.cancel_token.waits)
    assert not runner.cancel_token.request_action("check_login")


@pytest.mark.parametrize("pre_query,sync_before,check_before", [(60, 70, 190), (200, 210, 330)])
def test_long_warmup_moves_checks_before_query_start(monkeypatch, pre_query, sync_before, check_before):
    runner = make_runner(monkeypatch, pre_query=pre_query)
    wait(runner)
    assert runner.check_calls == pytest.approx([TARGET - check_before], abs=0.004)
    assert runner.clock.syncs[0][0] == pytest.approx(TARGET - sync_before, abs=0.004)
    assert runner.clock.current == pytest.approx(TARGET - pre_query, abs=0.004)


@pytest.mark.parametrize("seconds_before,budget", [(100, 10), (20, 10), (5, 3), (1, 10)])
def test_late_initial_login_satisfies_recheck_and_sync_has_remaining_budget(monkeypatch, seconds_before, budget):
    runner = make_runner(monkeypatch, start=TARGET - seconds_before, checks=())
    wait(runner)
    assert runner.check_calls == []
    assert len(runner.clock.syncs) == 1
    assert runner.clock.syncs[0][1]["budget_seconds"] == pytest.approx(budget)


def test_automatic_expiry_relogs_and_rebuilds_passenger_credentials(monkeypatch):
    runner = make_runner(monkeypatch, checks=(False,))
    wait(runner)
    assert len(runner.check_calls) == len(runner.login_calls) == len(runner.reloads) == 1
    assert runner._prepared_passengers == {"O": [{"new_account": True}]}


def test_qr_expiry_stays_paused_until_manual_login_without_losing_target(monkeypatch):
    runner = make_runner(monkeypatch, checks=(False,), logins=(AppError("二维码已过期"), True),
                         actions=[(TARGET - 30, "login")])
    wait(runner)
    assert len(runner.check_calls) == 1
    assert len(runner.login_calls) == 2
    assert runner.reloads == [TARGET - 30]
    assert runner.clock.syncs[0][0] == TARGET - 30
    assert runner.clock.current == pytest.approx(TARGET - 2, abs=0.004)


def test_cli_qr_expiry_stops_instead_of_waiting_for_nonexistent_buttons(monkeypatch):
    runner = make_runner(monkeypatch, checks=(False,), logins=(AppError("二维码已过期"),), interactive=False)
    with pytest.raises(AppError, match="重新运行"):
        wait(runner)


def test_automatic_unknown_check_stops_without_creating_qr(monkeypatch):
    runner = make_runner(monkeypatch, checks=(AppError("network"),))
    with pytest.raises(AppError, match="复查失败"):
        wait(runner)
    assert runner.login_calls == []
    assert not runner.cancel_token.request_action("login")


def test_early_manual_failed_check_keeps_state_and_does_not_cancel_scheduled_check(monkeypatch):
    runner = make_runner(monkeypatch, checks=(AppError("network"), True), actions=[(TARGET - 300, "check_login")])
    wait(runner)
    assert runner.check_calls == pytest.approx([TARGET - 300, TARGET - 180], abs=0.004)
    assert runner.login_calls == []


def test_manual_failed_check_after_scheduled_success_does_not_stop(monkeypatch):
    runner = make_runner(monkeypatch, checks=(True, AppError("network")), actions=[(TARGET - 100, "check_login")])
    wait(runner)
    assert len(runner.check_calls) == 2
    assert not runner._login_required


def test_manual_check_at_due_time_is_not_duplicated(monkeypatch):
    runner = make_runner(monkeypatch, checks=(True,), actions=[(TARGET - 180, "check_login")])
    wait(runner)
    assert runner.check_calls == [TARGET - 180]


def test_check_started_before_deadline_but_completed_after_serves_scheduled_check(monkeypatch):
    runner = make_runner(monkeypatch, checks=(True,), actions=[(TARGET - 181, "check_login")])
    original = runner.client.check_session

    def slow_check():
        value = original()
        runner.clock.current += 2
        return value

    runner.client.check_session = slow_check
    wait(runner)
    assert runner.check_calls == [TARGET - 181]


def test_manual_expiry_waits_for_explicit_login_then_recalibrates(monkeypatch):
    runner = make_runner(monkeypatch, checks=(True, False),
                         actions=[(TARGET - 30, "check_login"), (TARGET - 10, "login")])
    wait(runner)
    assert runner.login_calls == [TARGET - 10]
    assert runner.reloads == [TARGET - 10]
    assert len(runner.clock.syncs) == 2
    assert runner.clock.syncs[1][0] == TARGET - 10


def test_manual_sync_at_due_time_coalesces_but_early_sync_does_not(monkeypatch):
    runner = make_runner(monkeypatch, actions=[(TARGET - 250, "sync_clock"), (TARGET - 60, "sync_clock")])
    wait(runner)
    assert [item[0] for item in runner.clock.syncs] == [TARGET - 250, TARGET - 60]


def test_manual_sync_completing_after_deadline_is_not_repeated(monkeypatch):
    runner = make_runner(monkeypatch, actions=[(TARGET - 61, "sync_clock")])
    original = runner.clock.sync

    def slow_sync(*args, **kwargs):
        value = original(*args, **kwargs)
        runner.clock.current += 2
        return value

    runner.clock.sync = slow_sync
    wait(runner)
    assert len(runner.clock.syncs) == 1


def test_stop_while_paused_never_queries_or_consumes_future_login(monkeypatch):
    runner = make_runner(monkeypatch, checks=(False,), logins=(AppError("二维码已过期"),),
                         actions=[(TARGET - 100, "cancel"), (TARGET - 20, "login")])
    with pytest.raises(RunCancelled):
        wait(runner)
    assert len(runner.login_calls) == 1
    assert runner.reloads == []
    assert not runner.cancel_token.request_action("sync_clock")


def test_stale_queued_action_at_warmup_is_discarded(monkeypatch):
    runner = make_runner(monkeypatch, actions=[(TARGET - 2, "sync_clock")])
    wait(runner)
    assert len(runner.clock.syncs) == 1


def test_each_run_has_its_own_timed_checks(monkeypatch):
    first = make_runner(monkeypatch)
    second = make_runner(monkeypatch)
    wait(first)
    wait(second)
    assert len(first.check_calls) == len(second.check_calls) == 1
    assert len(first.clock.syncs) == len(second.clock.syncs) == 1


def test_new_runner_preserves_shared_successful_clock_but_uses_edited_settings():
    old_cfg = SimpleNamespace(request_timeout_seconds=3, time_sync_samples=1, time_sync_max_rtt_seconds=1)
    with requests.Session() as session:
        clock = ServerClock(session, old_cfg)
        clock.has_synchronized = True
        clock.offset_seconds = 12
        clock._base_server_timestamp = 1000
        clock._base_perf_counter = 500
        new_cfg = SimpleNamespace(request_timeout_seconds=7, time_sync_samples=2, time_sync_max_rtt_seconds=1,
                                  persist_session=False, cart_items=[])
        runner = TicketRunner(new_cfg, session=session, clock=clock)
        assert runner.clock is clock
        assert clock.cfg is new_cfg
        assert clock.timeout == 7
        assert clock.session is session
        assert clock.sync(budget_seconds=0) is False
        assert clock.has_synchronized
        assert clock.offset_seconds == 12
        assert clock._base_server_timestamp == 1000
        assert clock._base_perf_counter == 500
