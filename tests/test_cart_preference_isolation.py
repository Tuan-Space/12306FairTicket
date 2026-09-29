"""Exercise mixed cart orders through real request builders, with no network."""

import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
import requests

from ticket_app.cart import CartItem
from ticket_app.configuration import AppConfig, ResponseFormatError
from ticket_app.runner import TicketRunner
from ticket_app.runtime import CancellationToken


STATIONS = {"北京南": "VNP", "上海虹桥": "AOH", "北京": "BJP", "上海": "SHH"}
SEGMENTS = {
    "G103": ("北京南", "上海虹桥", "O", "二等座"),
    "D17": ("北京", "上海", "J", "二等卧"),
}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def reject(*_args, **_kwargs):
        pytest.fail("A simulated booking must never access the network")
    monkeypatch.setattr(requests.Session, "request", reject)


class SimulatedClock:
    elapsed = 0.0

    def now(self):
        return datetime.combine(date.today(), datetime.min.time()) + timedelta(hours=7, seconds=self.elapsed)

    def sync(self, *_args):
        return True


class SimulatedToken(CancellationToken):
    def __init__(self, clock):
        super().__init__()
        self.clock = clock

    def wait(self, seconds):
        self.checkpoint()
        self.clock.elapsed += seconds
        self.checkpoint()


def response(payload=None, *, text=""):
    return SimpleNamespace(status_code=200, json=lambda: payload, text=text)


class BookingSession:
    """Two trains offer both seat families; only the cart's exact binding is valid."""

    def __init__(self, capabilities, *, first_refused=True, dto_update=None):
        self.headers = {}
        self.calls = []
        self.active = None
        self.capabilities = capabilities
        self.first_refused = first_refused
        self.confirmations = 0
        self.dto_update = dto_update

    def get(self, url, *, params, timeout):
        endpoint = url.rsplit("/", 1)[-1]
        self.calls.append((endpoint, self.active, params.copy()))
        if endpoint == "queryOrderWaitTime":
            return response({"status": True, "data": {"orderId": "fixture-order"}})
        assert endpoint == "query"
        pair = (params["leftTicketDTO.from_station"], params["leftTicketDTO.to_station"])
        train = next(name for name, (origin, dest, *_rest) in SEGMENTS.items()
                     if (STATIONS[origin], STATIONS[dest]) == pair)
        raw = [""] * 36
        raw[0:4] = [f"fresh-{train}", "预订", f"number-{train}", train]
        raw[6:14] = [*pair, "08:00", "12:00", "04:00", "Y", f"left-{train}", "20300102"]
        raw[28], raw[30], raw[35] = "有", "有", "OJ"
        return response({"status": True, "data": {"result": ["|".join(raw)],
                        "map": {value: key for key, value in STATIONS.items()}}})

    def post(self, url, *, data, timeout):
        endpoint = url.rsplit("/", 1)[-1]
        if endpoint == "submitOrderRequest":
            self.active = data["secretStr"].removeprefix("fresh-")
        self.calls.append((endpoint, self.active, data.copy()))
        if endpoint == "getPassengerDTOs":
            return response({"status": True, "data": {"normal_passengers": [{
                "passenger_name": "测试旅客", "passenger_type": "1",
                "passenger_id_type_code": "1", "passenger_id_no": "fixture-id",
            }]}})
        if endpoint == "submitOrderRequest":
            assert self.active in SEGMENTS
            return response({"status": True})
        if endpoint == "initDc":
            origin, destination, *_rest = SEGMENTS[self.active]
            dto = {"train_no": f"number-{self.active}", "station_train_code": self.active,
                   "from_station_telecode": STATIONS[origin], "to_station_telecode": STATIONS[destination]}
            if self.dto_update is not None:
                dto.update(self.dto_update)
            info = {"queryLeftTicketRequestDTO": dto, "leftTicketStr": f"dc-{self.active}",
                    "train_location": f"location-{self.active}"}
            return response(text="var globalRepeatSubmitToken = 'fixture-token';"
                            f"var ticketInfoForPassengerForm = {json.dumps(info)};"
                            "var is_jy = 'Y';")
        if endpoint == "checkOrderInfo":
            return response({"status": True, "data": {"submitStatus": True, **self.capabilities}})
        if endpoint == "getQueueCount":
            return response({"status": True, "data": {"count": 0, "ticket": f"queue-{self.active}"}})
        if endpoint == "confirmSingleForQueue":
            self.confirmations += 1
            return response({"status": True, "data": {
                "submitStatus": not (self.first_refused and self.confirmations == 1)}})
        pytest.fail(f"Unexpected booking endpoint: {endpoint}")

    def forms(self, endpoint):
        return [(train, data) for called, train, data in self.calls if called == endpoint]


def make_runner(monkeypatch, capabilities, *, reverse=False, first_refused=True, dto_update=None):
    trains = ["D17", "G103"] if reverse else ["G103", "D17"]
    cart = [CartItem(SEGMENTS[train][0], SEGMENTS[train][1], "specific", train, SEGMENTS[train][3])
            for train in trains]
    cfg = AppConfig.from_mapping({
        "cart_items": cart, "train_date": (date.today() + timedelta(days=1)).isoformat(),
        "passenger_names": ["测试旅客"], "auto_submit": True, "persist_session": False,
        "seat_position_preferences": ["1A"],
        "berth_preference": {"lower": 0, "middle": 0, "upper": 1},
        "quiet_carriage_preference": True, "max_retries": 1, "perf_log": False,
    })
    clock = SimulatedClock()
    monkeypatch.setattr("ticket_app.runner.time.monotonic", lambda: clock.elapsed)
    session = BookingSession(capabilities, first_refused=first_refused, dto_update=dto_update)
    events = []
    runner = TicketRunner(cfg, session=session, clock=clock, cancel_token=SimulatedToken(clock),
                          event_sink=events.append)
    runner.stations = SimpleNamespace(code=STATIONS.__getitem__, load=lambda *_args: None)
    runner.client.ensure_login = lambda: None
    return runner, session, events


@pytest.mark.parametrize("reverse", [False, True], ids=["seat-then-berth", "berth-then-seat"])
@pytest.mark.parametrize("supported", [True, False], ids=["position-supported", "system-allocation"])
def test_mixed_cart_keeps_exact_ticket_and_isolates_preferences(monkeypatch, reverse, supported):
    capabilities = {"canChooseSeats": "Y" if supported else "N", "choose_Seats": "OM",
                    "canChooseBeds": "Y" if supported else "N", "isCanChooseMid": "Y"}
    runner, session, events = make_runner(monkeypatch, capabilities, reverse=reverse)
    assert runner.run() == 0
    expected_trains = ["D17", "G103"] if reverse else ["G103", "D17"]
    assert [train for train, _ in session.forms("confirmSingleForQueue")] == expected_trains
    for endpoint in ("checkOrderInfo", "confirmSingleForQueue"):
        for train, data in session.forms(endpoint):
            expected_seat = SEGMENTS[train][2]
            assert data["passengerTicketStr"].split(",")[:4] == [expected_seat, "0", "1", "测试旅客"]
            assert data["oldPassengerStr"] == "测试旅客,1,fixture-id,1_"
    for train, data in session.forms("submitOrderRequest"):
        origin, destination, *_rest = SEGMENTS[train]
        assert (data["query_from_station_name"], data["query_to_station_name"]) == (origin, destination)
        assert data["secretStr"] == f"fresh-{train}"
        assert data["train_date"] == runner.cfg.train_date
    for train, data in session.forms("getQueueCount"):
        origin, destination, seat, _label = SEGMENTS[train]
        assert (data["stationTrainCode"], data["train_no"], data["seatType"]) == (train, f"number-{train}", seat)
        assert (data["fromStationTelecode"], data["toStationTelecode"]) == (STATIONS[origin], STATIONS[destination])
    for train, data in session.forms("confirmSingleForQueue"):
        assert data["choose_seats"] == ("1A" if train == "G103" and supported else "")
        assert data["seatDetailType"] == ("001" if train == "D17" and supported else "000")
        assert data["is_jy"] == ("Y" if train == "G103" else "N")
        assert data["leftTicketStr"] == f"queue-{train}"
        assert data["train_location"] == f"location-{train}"
    success = next(event for event in events if event.kind == "order_success")
    assert success.data["train_code"] == expected_trains[-1]
    assert success.data["seat_label"] == SEGMENTS[expected_trains[-1]][3]
    if not supported:
        assert any("选座" in event.message for event in events if event.kind == "preference_fallback")
        assert any("选铺" in event.message for event in events if event.kind == "preference_fallback")


def test_first_cart_success_never_submits_second_route(monkeypatch):
    runner, session, _events = make_runner(monkeypatch, {}, first_refused=False)
    assert runner.run() == 0
    assert [train for train, _data in session.forms("submitOrderRequest")] == ["G103"]
    assert len(session.forms("confirmSingleForQueue")) == 1


@pytest.mark.parametrize("dto_update", [
    {"train_no": "stale-number"}, {"station_train_code": "D17"},
    {"from_station_telecode": "BJP"}, {"to_station_telecode": "SHH"},
])
def test_stale_confirmation_page_cannot_replace_cart_train_or_stations(monkeypatch, dto_update):
    runner, session, _events = make_runner(monkeypatch, {}, dto_update=dto_update)
    with pytest.raises(ResponseFormatError, match="本次候选"):
        runner.run()
    assert [train for train, _data in session.forms("submitOrderRequest")] == ["G103"]
    assert session.forms("getQueueCount") == []
    assert session.forms("confirmSingleForQueue") == []


@pytest.mark.parametrize("blank", [None, ""])
def test_missing_confirmation_identity_uses_current_candidate(monkeypatch, blank):
    dto_update = {key: blank for key in ("train_no", "station_train_code", "from_station_telecode", "to_station_telecode")}
    runner, session, _events = make_runner(monkeypatch, {}, dto_update=dto_update, first_refused=False)
    assert runner.run() == 0
    train, form = session.forms("getQueueCount")[0]
    assert (train, form["train_no"], form["stationTrainCode"], form["seatType"]) == ("G103", "number-G103", "G103", "O")
    assert (form["fromStationTelecode"], form["toStationTelecode"]) == ("VNP", "AOH")
