"""The read-only contact picker must never stringify identity-bearing payloads."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from ticket_app.client import RailwayClient
from ticket_app.configuration import AppError, ConnectionConfig
from ticket_app.runtime import RunCancelled


def client_for(payload, status=200):
    client = RailwayClient(ConnectionConfig())
    response = SimpleNamespace(status_code=status, json=Mock(return_value=payload))
    client.session.post = Mock(return_value=response)
    return client, response


@pytest.mark.parametrize("payload", [
    [], None, {"status": False, "messages": ["private-person-id"]},
    {"data": None}, {"data": {}}, {"data": {"normal_passengers": {"private-person-id": "secret"}}},
    {"data": {"normal_passengers": [None]}},
    {"data": {"normal_passengers": [{"passenger_name": None, "passenger_id_no": "private-person-id"}]}},
    {"data": {"normal_passengers": [{"passenger_name": " ", "passenger_id_no": "private-person-id"}]}},
    {"data": {"normal_passengers": [{"passenger_name": 123, "passenger_id_no": "private-person-id"}]}},
])
def test_invalid_contact_payload_is_friendly_and_does_not_expose_identity(payload, caplog):
    client, _ = client_for(payload)
    with pytest.raises(AppError, match="乘车人") as error:
        client.get_passengers()
    assert "private-person-id" not in str(error.value)
    assert "private-person-id" not in caplog.text


def test_no_contacts_is_a_valid_empty_list():
    client, _ = client_for({"data": {"normal_passengers": []}})
    assert client.get_passengers() == []


def test_contacts_return_copies_and_preserve_identity_for_ordering():
    original = {"passenger_name": "甲", "passenger_id_no": "sensitive", "passenger_type": "3"}
    client, _ = client_for({"data": {"normal_passengers": [original]}})
    contacts = client.get_passengers()
    assert contacts == [original]
    assert contacts[0] is not original


@pytest.mark.parametrize("failure", ["http", "json", "network"])
def test_transport_failures_do_not_expose_raw_exception_or_response(failure, caplog):
    client, response = client_for({"data": {"normal_passengers": []}})
    if failure == "http":
        response.status_code = 403
    elif failure == "json":
        response.json.side_effect = ValueError("private-person-id")
    else:
        client.session.post.side_effect = requests.Timeout("private-person-id")
    with pytest.raises(AppError, match="检查登录") as error:
        client.get_passengers()
    assert "private-person-id" not in str(error.value)
    assert "private-person-id" not in caplog.text
    assert error.value.__suppress_context__ is True


def test_cancellation_after_response_does_not_return_contacts():
    client, response = client_for({"data": {"normal_passengers": []}})
    def finish_after_cancel(*_args, **_kwargs):
        client.cancel_token.cancel()
        return response
    client.session.post.side_effect = finish_after_cancel
    with pytest.raises(RunCancelled):
        client.get_passengers()
