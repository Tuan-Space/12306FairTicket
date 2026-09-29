"""No-network execution tests for ordered alternatives and shared query pacing."""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import requests

from ticket_app.cart import CartItem
from ticket_app.configuration import AppConfig, AppError, PreparedPassengerSet, ResponseFormatError, SEAT_SPECS
from ticket_app.helpers import _format_queue_date
from ticket_app.preferences import OrderCapabilities, OrderCheckResult
from ticket_app.runner import TicketRunner
from ticket_app.runtime import CancellationToken, RunCancelled


STATIONS = {"北京南": "VNP", "上海虹桥": "AOH", "北京": "BJP", "上海": "SHH"}
PAIR_A = ("VNP", "AOH")
PAIR_B = ("BJP", "SHH")


@pytest.fixture(autouse=True)
def reject_network(monkeypatch):
    monkeypatch.setattr(requests.Session, "request", lambda *_a, **_k: pytest.fail("Unexpected network request"))


def item(train="G1", seat="二等座", pair="A"):
    origin, destination = ("北京南", "上海虹桥") if pair == "A" else ("北京", "上海")
    return CartItem(origin, destination, "specific" if train else "all", train, seat)


def row(train="G1", pair=PAIR_A, *, seat_codes="OM", stocks=None, secret="fresh", train_no=None):
    fields = [""] * 36
    fields[0:4] = [secret, "预订", train_no or train + "-number", train]
    fields[6:14] = [*pair, "08:00", "12:00", "04:00", "Y", "fresh-left", "20300102"]
    fields[30], fields[31], fields[35] = "有", "有", seat_codes
    for key, stock in (stocks or {}).items():
        fields[{"swz": 32, "tz": 25, "ydz": 31, "edz": 30, "gr": 21, "rw": 23,
                "rz": 24, "yw": 28, "yz": 29, "wz": 26}[key]] = stock
    return "|".join(fields)


class VirtualClock:
    def __init__(self):
        self.elapsed = 0.0

    def now(self):
        return datetime(2030, 1, 2, 7) + timedelta(seconds=self.elapsed)

    def sync(self, *_args):
        return True


class VirtualToken(CancellationToken):
    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.waits = []

    def wait(self, seconds):
        self.checkpoint()
        self.waits.append(seconds)
        self.clock.elapsed += seconds
        self.checkpoint()


class QuerySession:
    def __init__(self, responses, clock):
        self.headers = {}
        self.responses = responses
        self.clock = clock
        self.calls = []

    def get(self, url, *, params, timeout):
        pair = (params["leftTicketDTO.from_station"], params["leftTicketDTO.to_station"])
        self.calls.append((pair, url.rsplit("/", 1)[-1], self.clock.elapsed))
        outcome = self.responses(pair, len(self.calls)) if callable(self.responses) else self.responses[pair]
        if isinstance(outcome, Exception):
            raise outcome
        payload = outcome if isinstance(outcome, dict) else {"status": True, "data": {"result": outcome,
                   "map": {code: name for name, code in STATIONS.items()}}}
        return SimpleNamespace(json=lambda: payload, status_code=200)


def runner_for(monkeypatch, cart, responses, *, auto_submit=False, retries=1):
    clock = VirtualClock()
    monkeypatch.setattr("ticket_app.runner.time.monotonic", lambda: clock.elapsed)
    cfg = AppConfig.from_mapping({
        "cart_items": cart, "train_date": (date.today() + timedelta(days=1)).isoformat(),
        "passenger_names": ["测试旅客"], "persist_session": False, "auto_submit": auto_submit,
        "query_interval_seconds": 2.0, "max_retries": retries, "perf_log": False,
    })
    session = QuerySession(responses, clock)
    token = VirtualToken(clock)
    events = []
    runner = TicketRunner(cfg, session=session, cancel_token=token, event_sink=events.append, clock=clock)
    runner.stations = SimpleNamespace(code=STATIONS.__getitem__, load=lambda *_args: None)
    runner.client.ensure_login = lambda: None
    runner._resolve_target_start = lambda: None
    runner._select_passengers = lambda: []
    runner._prepare_passengers_by_seat_code = lambda _passengers: {}
    runner._resolve_query_routes()
    return runner, session, clock, events


def test_interleaved_order_is_lazy_and_same_pair_reuses_one_query(monkeypatch):
    runner, session, _clock, _events = runner_for(monkeypatch,
        [item("G1"), item("G2", pair="B"), item("G3")],
        {PAIR_A: [row("G3"), row("G1")], PAIR_B: [row("G2", PAIR_B)]})
    candidates = runner._round_candidates(None)
    assert next(candidates)["ticket"]["station_train_code"] == "G1"
    assert [call[0] for call in session.calls] == [PAIR_A]
    assert next(candidates)["ticket"]["station_train_code"] == "G2"
    assert [call[0] for call in session.calls] == [PAIR_A, PAIR_B]
    assert next(candidates)["ticket"]["station_train_code"] == "G3"
    with pytest.raises(StopIteration):
        next(candidates)
    assert [call[0] for call in session.calls] == [PAIR_A, PAIR_B]


def test_overlapping_any_train_entry_deduplicates_actual_seat_not_submit_code(monkeypatch):
    runner, session, _clock, _events = runner_for(monkeypatch,
        [item("G2"), item(""), item("G2", "无座"), item("G2", "一等座")],
        {PAIR_A: [row("G2", stocks={"wz": "有"}), row("G1")]})
    result = list(runner._round_candidates(None))
    assert [(c["train_code"], c["seat_label"], c["seat_type"], c["cart_index"]) for c in result] == [
        ("G2", "二等座", "O", 1), ("G1", "二等座", "O", 2),
        ("G2", "无座", "O", 3), ("G2", "一等座", "M", 4)]
    assert len(session.calls) == 1


def test_same_actual_train_on_different_intervals_remains_two_candidates(monkeypatch):
    runner, _session, _clock, _events = runner_for(monkeypatch,
        [item("G1"), item("G1", pair="B")],
        {PAIR_A: [row("G1", train_no="same-train")],
         PAIR_B: [row("G1", PAIR_B, train_no="same-train")]})
    result = list(runner._round_candidates(None))
    assert [c["from_station"] for c in result] == ["北京南", "北京"]


@pytest.mark.parametrize("returned_pair", [PAIR_B, ("", ""), (PAIR_A[0], PAIR_B[1])])
def test_wrong_or_missing_actual_stations_never_become_candidates(monkeypatch, returned_pair):
    runner, _session, _clock, events = runner_for(monkeypatch, [item()],
                                               {PAIR_A: [row(pair=returned_pair)]})
    assert list(runner._round_candidates(None)) == []
    assert any(e.kind == "query_route_mismatch" for e in events)


@pytest.mark.parametrize("station_map", [{}, {"VNP": "北京", "AOH": "上海"}])
def test_matched_station_codes_use_validated_names_when_response_map_is_missing_or_wrong(monkeypatch, station_map):
    payload = {"status": True, "data": {"result": [row()], "map": station_map}}
    runner, session, _clock, _events = runner_for(monkeypatch, [item()], {PAIR_A: payload})
    candidate = next(runner._round_candidates(None))
    ticket = candidate["ticket"]
    assert ticket["from_station"] == "北京南"
    assert ticket["to_station"] == "上海虹桥"
    captured = []
    session.post = lambda _url, *, data, timeout: (
        captured.append(data) or SimpleNamespace(status_code=200, json=lambda: {"status": True}))
    assert runner.client.submit_order_request(ticket)[0]
    assert captured[0]["query_from_station_name"] == "北京南"
    assert captured[0]["query_to_station_name"] == "上海虹桥"
    assert captured[0]["secretStr"] == "fresh"


def test_failed_route_is_cached_for_round_and_does_not_block_later_route(monkeypatch):
    runner, session, _clock, events = runner_for(monkeypatch,
        [item("G1"), item("G2", pair="B"), item("G3")],
        {PAIR_A: requests.Timeout("offline"), PAIR_B: [row("G2", PAIR_B)]})
    result = list(runner._round_candidates(None))
    assert [c["train_code"] for c in result] == ["G2"]
    assert [call[0] for call in session.calls] == [PAIR_A] * 3 + [PAIR_B]
    assert [call[2] for call in session.calls] == [0, 2, 4, 6]
    assert len([e for e in events if e.kind == "query_failed"]) == 1
    assert not any(e.kind == "query_empty" for e in events)


def test_full_round_budget_retries_failed_pair_and_never_reuses_previous_tickets(monkeypatch):
    def response(pair, count):
        if count <= 3:
            return requests.Timeout("offline")
        return [row("G1", secret=f"round-{count}")] if pair == PAIR_A else []
    runner, session, _clock, events = runner_for(monkeypatch,
        [item("G1"), item("G2", pair="B")], response, retries=3)
    assert runner.run() == 1
    assert [e.data["attempt"] for e in events if e.kind == "query"] == [1, 2, 3]
    assert [call[0] for call in session.calls] == [PAIR_A] * 3 + [PAIR_B, PAIR_A, PAIR_B, PAIR_A, PAIR_B]
    assert len([e for e in events if e.kind == "candidate"]) == 2
    assert [call[2] for call in session.calls] == list(range(0, 16, 2))


def test_all_failed_rounds_still_exhaust_the_full_round_budget(monkeypatch):
    runner, session, _clock, events = runner_for(monkeypatch,
        [item(), item("G2", pair="B")], lambda *_args: requests.Timeout(), retries=2)
    assert runner.run() == 1
    assert len(session.calls) == 12
    assert len([e for e in events if e.kind == "query"]) == 2
    assert len([e for e in events if e.kind == "query_failed"]) == 4


@pytest.mark.parametrize("payload", [
    {"status": False}, {"status": True}, {"status": True, "data": {}},
    {"status": True, "data": {"result": "bad"}},
])
def test_malformed_or_rejected_query_is_failure_not_empty(monkeypatch, payload):
    runner, session, _clock, _events = runner_for(monkeypatch, [item()], {PAIR_A: payload})
    result = runner.client.query_tickets_result(*PAIR_A)
    assert not result.success and not result.tickets
    assert len(session.calls) == 3


def test_successful_empty_query_does_not_try_fallback_endpoints(monkeypatch):
    runner, session, _clock, events = runner_for(monkeypatch, [item()], {PAIR_A: []})
    assert list(runner._round_candidates(None)) == []
    assert len(session.calls) == 1
    assert [e.kind for e in events] == ["cart_item", "query_empty"]


@pytest.mark.parametrize("label", list(SEAT_SPECS))
def test_each_existing_seat_uses_its_actual_order_code(monkeypatch, label):
    spec = SEAT_SPECS[label]
    code = spec.submit_code or "O"
    runner, _session, _clock, _events = runner_for(monkeypatch, [item("D1", label)],
        {PAIR_A: [row("D1", seat_codes=code, stocks={spec.stock_key: "有"})]})
    candidates = list(runner._round_candidates(None))
    assert [(c["seat_label"], c["seat_type"]) for c in candidates] == [(label, code)]


def test_ambiguous_shared_berth_stock_still_skips_both_cart_entries(monkeypatch):
    runner, _session, _clock, events = runner_for(monkeypatch,
        [item("D1", "软卧"), item("D1", "一等卧")],
        {PAIR_A: [row("D1", seat_codes="4I", stocks={"rw": "有"})]})
    assert list(runner._round_candidates(None)) == []
    assert len([e for e in events if e.kind == "query_empty"]) == 2


def test_first_success_stops_before_querying_later_route(monkeypatch):
    runner, session, _clock, events = runner_for(monkeypatch,
        [item(), item("G2", pair="B")], {PAIR_A: [row()]}, auto_submit=True)
    attempted = []
    runner._book_ticket = lambda candidate, _prepared: attempted.append(candidate) or True
    assert runner.run() == 0
    assert len(attempted) == len(session.calls) == 1
    candidate_event = next(e for e in events if e.kind == "candidate")
    assert {key: candidate_event.data[key] for key in ("cart_index", "cart_total", "from_station", "to_station", "train_code")} == {
        "cart_index": 1, "cart_total": 2, "from_station": "北京南", "to_station": "上海虹桥", "train_code": "G1"}


def test_unknown_order_stops_whole_cart_and_blocks_restart(monkeypatch):
    runner, session, _clock, _events = runner_for(monkeypatch,
        [item(), item("G2", pair="B")], {PAIR_A: [row()]}, auto_submit=True)
    def uncertain(_candidate, _prepared):
        runner._set_order_state("unknown")
        return False
    runner._book_ticket = uncertain
    with pytest.raises(AppError, match="整车任务已停止"):
        runner.run()
    assert len(session.calls) == 1
    assert not runner.safe_to_restart
    with pytest.raises(AppError, match="不能再次启动"):
        runner.run()
    assert len(session.calls) == 1


def test_cancellation_between_entries_does_not_query_next_route(monkeypatch):
    runner, session, _clock, _events = runner_for(monkeypatch,
        [item(), item("G2", pair="B")], {PAIR_A: [row()]})
    iterator = runner._round_candidates(None)
    next(iterator)
    runner.cancel_token.cancel()
    with pytest.raises(RunCancelled):
        next(iterator)
    assert len(session.calls) == 1


def test_new_round_uses_fresh_response_tokens_even_after_safe_failure(monkeypatch):
    runner, session, _clock, _events = runner_for(monkeypatch, [item()],
        lambda _pair, count: [row(secret=f"fresh-round-{count}")], auto_submit=True, retries=2)
    submitted_tokens = []
    runner._book_ticket = lambda candidate, _prepared: submitted_tokens.append(candidate["ticket"]["secret_str"]) or False
    assert runner.run() == 1
    assert submitted_tokens == ["fresh-round-1", "fresh-round-2"]
    assert len(session.calls) == 2


def test_query_pacing_wait_respects_fixed_stop_deadline(monkeypatch):
    runner, session, clock, _events = runner_for(monkeypatch,
        [item(), item("G2", pair="B")], {PAIR_A: [], PAIR_B: []})
    runner.cfg.stop_at = "07:00:01"
    assert runner.run() == 1
    assert [call[0] for call in session.calls] == [PAIR_A]
    assert clock.elapsed == 1


def test_shared_pacing_uses_existing_hot_window_interval(monkeypatch):
    runner, _session, clock, _events = runner_for(monkeypatch, [item()], {PAIR_A: []})
    target = clock.now() + timedelta(seconds=3)
    runner.cfg.hot_query_interval_seconds = 0.5
    runner.cfg.pre_query_seconds = 2
    runner.cfg.hot_window_seconds = 5
    runner._pace_query_request(target)
    clock.elapsed = 1.5
    runner._pace_query_request(target)
    runner._pace_query_request(target)
    assert clock.elapsed == 2.0
    clock.elapsed = 9
    runner._pace_query_request(target)
    runner._pace_query_request(target)
    assert clock.elapsed == 11


def test_full_order_path_submits_fresh_second_route_and_publishes_actual_segment(monkeypatch):
    runner, session, _clock, events = runner_for(monkeypatch,
        [item(), item("G2", "一等卧", pair="B")],
        {PAIR_A: [], PAIR_B: [row("G2", PAIR_B, seat_codes="I", stocks={"rw": "有"}, secret="second-route-secret")]},
        auto_submit=True)
    submitted = []
    runner._prepare_passengers_by_seat_code = lambda _p: {"I": PreparedPassengerSet([{}], "I,fixture", "fixture")}
    runner.client.submit_order_request = lambda ticket: (submitted.append(ticket) or True, "OK")
    runner.client.init_dc = lambda: ("fixture-token", {"leftTicketStr": "fresh-left"})
    runner.client.check_order_info = lambda *_args: OrderCheckResult(True, "OK", OrderCapabilities())
    runner.client.get_queue_count = lambda *_args: (True, {"ticket": "fresh-queue", "count": 0})
    runner.client.confirm_single_for_queue = lambda *_args: (True, "OK")
    runner.client.query_order_wait_time = lambda *_args: (True, {"orderId": "fixture-order"})
    assert runner.run() == 0
    assert len(submitted) == 1
    assert submitted[0]["secret_str"] == "second-route-secret"
    assert (submitted[0]["from_station_telecode"], submitted[0]["to_station_telecode"]) == PAIR_B
    assert runner.order_state == "success"
    for event in events:
        if event.kind == "order_success" or (event.kind == "phase" and event.data["phase"] in {"submitting", "queueing"}):
            assert event.data["cart_index"] == 2
            assert event.data["from_station"] == "北京"
            assert event.data["to_station"] == "上海"
            assert event.data["train_code"] == "G2"
            assert event.data["seat_label"] == "一等卧"


def test_overnight_intermediate_stop_submits_query_day_and_preserves_origin_day(monkeypatch):
    boarding_day = date.today() + timedelta(days=1)
    originating_day = date.today().strftime("%Y%m%d")
    raw = row("D1").split("|")
    raw[13] = originating_day
    runner, session, _clock, events = runner_for(monkeypatch, [item("D1")], {PAIR_A: ["|".join(raw)]})
    candidate = next(runner._round_candidates(None))
    ticket = candidate["ticket"]
    assert ticket["query_date"] == boarding_day.isoformat()
    assert ticket["start_train_date"] == date.today().isoformat()
    assert "date" not in ticket
    captured = []
    def post(url, *, data, timeout):
        captured.append((url.rsplit("/", 1)[-1], data))
        return SimpleNamespace(status_code=200, json=lambda: {"status": True, "data": {"count": 0}})
    session.post = post
    assert runner.client.submit_order_request(ticket)[0]
    millis = datetime.combine(boarding_day, datetime.min.time(), timezone(timedelta(hours=8))).timestamp() * 1000
    info = {"orderRequestDTO": {"train_date": {"time": millis}}}
    assert runner.client.get_queue_count(ticket, info, "O", "fixture-token")[0]
    submit, queue = [data for _endpoint, data in captured]
    assert submit["train_date"] == submit["back_train_date"] == boarding_day.isoformat()
    assert queue["train_date"] == _format_queue_date(boarding_day.isoformat())
    assert runner._candidate_context(candidate)["train_date"] == boarding_day.isoformat()


def test_queue_without_server_date_uses_current_query_date(monkeypatch):
    runner, session, _clock, _events = runner_for(monkeypatch, [item()], {PAIR_A: [row()]})
    ticket = next(runner._round_candidates(None))["ticket"]
    captured = []
    session.post = lambda _url, *, data, timeout: (
        captured.append(data) or SimpleNamespace(status_code=200, json=lambda: {"status": True, "data": {}}))
    assert runner.client.get_queue_count(ticket, {}, "O", "fixture-token")[0]
    assert captured[-1]["train_date"] == _format_queue_date(runner.cfg.train_date)


@pytest.mark.parametrize("query_date", [None, "", "2030-02-30", "20300102", True, 123])
def test_missing_or_invalid_query_date_never_uses_origin_day_or_sends_an_order(monkeypatch, query_date):
    runner, session, _clock, _events = runner_for(monkeypatch, [item()], {PAIR_A: [row()]})
    candidate = next(runner._round_candidates(None))
    ticket = candidate["ticket"]
    if query_date is None:
        ticket.pop("query_date")
    else:
        ticket["query_date"] = query_date
    ticket["date"] = "2030-01-01"  # A removed alias must never restore the origin-date fallback.
    session.post = lambda *_args, **_kwargs: pytest.fail("Invalid query date must stop before HTTP")
    for action in (lambda: runner.client.submit_order_request(ticket),
                   lambda: runner.client.get_queue_count(ticket, {}, "O", "fixture-token"),
                   lambda: runner._candidate_context(candidate)):
        with pytest.raises(ResponseFormatError, match="乘车日期"):
            action()


@pytest.mark.parametrize("server_date", [None, {}, {"time": True}, {"time": "123"},
                                         {"time": float("nan")}, {"time": 0}])
def test_malformed_or_mismatched_order_date_stops_before_queue_request(monkeypatch, server_date):
    runner, session, _clock, _events = runner_for(monkeypatch, [item()], {PAIR_A: [row()]})
    ticket = next(runner._round_candidates(None))["ticket"]
    session.post = lambda *_args, **_kwargs: pytest.fail("Mismatched date must not query the queue")
    with pytest.raises(ResponseFormatError, match="乘车日期"):
        runner.client.get_queue_count(ticket, {"orderRequestDTO": {"train_date": server_date}}, "O", "fixture-token")
