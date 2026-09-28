"""Confirmation acknowledgements must prove acceptance or refusal explicitly."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from ticket_app.client import RailwayClient
from ticket_app.configuration import AppError, PreparedPassengerSet, ResponseFormatError
from ticket_app.preferences import OrderPreferencePayload


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(requests.Session, "request", lambda *_args, **_kwargs: pytest.fail("Unexpected network request"))


class Response:
    def __init__(self, payload, *, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.headers = {"Content-Type": "application/json"}
        self.url = "https://kyfw.12306.cn/otn/confirmPassenger/confirmSingleForQueue"
        self.history = []
        self.content = b"PRIVATE_RESPONSE_MUST_NOT_BE_LOGGED"

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def client_for(payload, *, status_code=200):
    client = object.__new__(RailwayClient)
    client.cfg = SimpleNamespace(request_timeout_seconds=5)
    client.session = Mock()
    client.session.post.return_value = Response(payload, status_code=status_code)
    client.session.get.return_value = Response(payload, status_code=status_code)
    return client


def confirm(client):
    return client.confirm_single_for_queue(
        PreparedPassengerSet([], "mock-passengers", "mock-old"),
        {"key_check_isChange": "mock-key", "train_location": "P2"},
        "mock-left", "mock-token", OrderPreferencePayload("1A", "100", is_jy="Y"),
    )


@pytest.mark.parametrize("payload,message", [
    ({"status": False, "messages": ["余票不足"]}, "余票不足"),
    ({"status": False, "data": None, "message": "明确拒绝"}, "明确拒绝"),
    ({"status": False, "data": ["unrelated"], "message": "明确拒绝"}, "明确拒绝"),
    ({"status": True, "data": {"submitStatus": False, "errMsg": "学生优惠次数已用完"}}, "学生优惠次数已用完"),
])
def test_only_explicit_boolean_rejections_return_false(payload, message):
    client = client_for(payload)
    assert confirm(client) == (False, message)
    assert client.session.post.call_count == 1


def test_explicit_success_preserves_order_preference_submission():
    client = client_for({"status": True, "data": {"submitStatus": True}})
    assert confirm(client) == (True, "OK")
    sent = client.session.post.call_args.kwargs["data"]
    assert sent["choose_seats"] == "1A"
    assert sent["seatDetailType"] == "100"
    assert sent["is_jy"] == "Y"
    assert sent["REPEAT_SUBMIT_TOKEN"] == "mock-token"


@pytest.mark.parametrize("payload", [
    {}, {"data": {"submitStatus": False}},
    *({"status": value, "data": {"submitStatus": False}}
      for value in (None, 0, 1, "false", "true", [], {})),
    {"status": True},
    *({"status": True, "data": value} for value in (None, [], "", "data", 0, False)),
    {"status": True, "data": {}},
    *({"status": True, "data": {"submitStatus": value}}
      for value in (None, 0, 1, "false", "true", [], {})),
])
def test_missing_or_wrongly_typed_acknowledgement_never_means_rejection(payload, caplog):
    client = client_for({**payload, "private": "PRIVATE_UNKNOWN_CONFIRMATION"})
    with pytest.raises(ResponseFormatError, match="confirmSingleForQueue") as error:
        confirm(client)
    assert "PRIVATE_UNKNOWN_CONFIRMATION" not in str(error.value)
    assert "PRIVATE_UNKNOWN_CONFIRMATION" not in caplog.text
    assert "PRIVATE_RESPONSE" not in caplog.text
    assert client.session.post.call_count == 1


@pytest.mark.parametrize("payload", [None, [], "false", False, ValueError("PRIVATE_JSON_BODY")])
def test_invalid_confirmation_response_container_raises_safe_error(payload, caplog):
    with pytest.raises(ResponseFormatError) as error:
        confirm(client_for(payload))
    assert "PRIVATE_JSON_BODY" not in str(error.value)
    assert "PRIVATE_JSON_BODY" not in caplog.text
    assert "PRIVATE_RESPONSE" not in caplog.text


@pytest.mark.parametrize("status_code", [302, 403, 500])
def test_http_error_cannot_be_treated_as_a_rejection_even_with_false_json(status_code):
    client = client_for({"status": False}, status_code=status_code)
    with pytest.raises(ResponseFormatError, match="HTTP"):
        confirm(client)


@pytest.mark.parametrize("payload", [
    {}, {"status": 0}, {"status": True}, {"status": True, "data": []},
    {"status": True, "data": {"submitStatus": "false"}},
])
def test_malformed_real_client_acknowledgement_keeps_runner_restart_blocked(payload):
    from test_runner_restart_safety import book, booking_runner

    runner = booking_runner()
    client = client_for(payload)
    runner.client.confirm_single_for_queue = client.confirm_single_for_queue
    with pytest.raises(ResponseFormatError):
        book(runner)
    assert runner.order_state == "unknown"
    assert not runner.safe_to_restart
    assert "wait" not in runner.client.calls
    with pytest.raises(AppError, match="不能重复下单"):
        book(runner)
    assert client.session.post.call_count == 1


INVALID_QUEUE_RESPONSES = [
    {}, {"messages": ["出票失败"]},
    *({"status": value, "data": {"msg": "出票失败"}}
      for value in (None, "false", "true", 0, 1, [], {})),
    {"status": True},
    *({"status": True, "data": value} for value in (None, [], "出票失败", False)),
    *({"status": True, "data": {"msg": value}}
      for value in ({"nested": "出票失败"}, ["出票失败"], False, 1)),
    *({"status": True, "data": {"orderId": value, "msg": "出票失败"}}
      for value in (False, 0, ["order"], {"id": "order"})),
    *({"status": True, "data": {"waitTime": value, "msg": "出票失败"}}
      for value in (True, "-1", "false", 1.5, [], {})),
    {"status": False, "messages": [{"nested": "出票失败"}]},
    {"status": False, "message": {"nested": "出票失败"}},
    {"status": False, "data": {"msg": ["出票失败"]}},
]


@pytest.mark.parametrize("payload", INVALID_QUEUE_RESPONSES)
def test_queue_response_requires_typed_status_data_and_consumed_fields(payload, caplog):
    client = client_for({**payload, "private": "PRIVATE_UNKNOWN_QUEUE"})
    with pytest.raises(ResponseFormatError, match="queryOrderWaitTime") as error:
        client.query_order_wait_time("mock-token")
    assert "PRIVATE_UNKNOWN_QUEUE" not in str(error.value)
    assert "PRIVATE_UNKNOWN_QUEUE" not in caplog.text
    assert "PRIVATE_RESPONSE" not in caplog.text


@pytest.mark.parametrize("payload", INVALID_QUEUE_RESPONSES)
def test_malformed_queue_acknowledgement_cannot_release_runner_for_another_order(payload):
    from test_runner_restart_safety import book, booking_runner

    client = client_for(payload)
    runner = booking_runner()
    runner.client.query_order_wait_time = client.query_order_wait_time
    with pytest.raises(AppError, match="避免重复下单"):
        book(runner)
    assert runner.order_state == "unknown"
    assert not runner.safe_to_restart
    with pytest.raises(AppError, match="不能重复下单"):
        book(runner)
    assert client.session.get.call_count == 1


@pytest.mark.parametrize("payload", [
    {"status": True, "data": {"msg": "出票失败，余票不足", "waitTime": -1}},
    {"status": False, "messages": ["出票失败，余票不足"]},
    {"status": False, "data": {"errMsg": "出票失败，余票不足"}},
])
def test_explicit_typed_queue_failure_still_allows_next_candidate(payload):
    from test_runner_restart_safety import book, booking_runner

    client = client_for(payload)
    runner = booking_runner()
    runner.client.query_order_wait_time = client.query_order_wait_time
    assert book(runner) is False
    assert runner.order_state == "safe"
    assert runner.safe_to_restart


@pytest.mark.parametrize("field,value", [
    ("resultStatus", False), ("resultStatus", "false"), ("resultStatus", 0),
    ("queryOrderWaitTimeStatus", False), ("queryOrderWaitTimeStatus", "false"),
])
def test_uninterpreted_queue_status_fields_do_not_establish_rejection(field, value):
    from test_runner_restart_safety import book, booking_runner

    client = client_for({"status": True, "data": {field: value, "waitTime": -100}})
    runner = booking_runner()
    runner.client.query_order_wait_time = client.query_order_wait_time
    with pytest.raises(AppError, match="避免重复下单"):
        book(runner)
    assert runner.order_state == "unknown"
    assert not runner.safe_to_restart


def test_arbitrary_false_response_is_not_stringified_into_a_terminal_order_failure(caplog):
    from test_runner_restart_safety import book, booking_runner

    client = client_for({"status": False, "private": {"result": "出票失败 PRIVATE_CREDENTIAL"}})
    runner = booking_runner()
    runner.client.query_order_wait_time = client.query_order_wait_time
    with pytest.raises(AppError, match="避免重复下单"):
        book(runner)
    assert runner.order_state == "unknown"
    assert not runner.safe_to_restart
    assert "PRIVATE_CREDENTIAL" not in caplog.text


def test_typed_queue_success_retains_the_actual_order():
    from test_runner_restart_safety import book, booking_runner

    client = client_for({"status": True, "data": {"orderId": "MOCK-CONFIRMED", "waitTime": -1}})
    runner = booking_runner()
    runner.client.query_order_wait_time = client.query_order_wait_time
    assert book(runner)
    assert runner.order_state == "success"
    assert not runner.safe_to_restart
