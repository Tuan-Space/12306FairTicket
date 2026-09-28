"""Independent, offline edge review of connection and maintenance safety."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from ticket_app.client import RailwayClient
from ticket_app.clock import ServerClock
from ticket_app.configuration import AppError, ConnectionConfig, ResponseFormatError
from ticket_app.quiet_capability import extract_quiet_carriage_available
from ticket_app.runtime import CancellationToken, RunCancelled


def client_with_html(html):
    client = RailwayClient(ConnectionConfig())
    client.session.post = Mock(return_value=SimpleNamespace(text=html, status_code=200))
    return client


def html_with_script(script):
    return (
        "<html><script>var globalRepeatSubmitToken='fixture';"
        "var ticketInfoForPassengerForm={};\n" + script + "</script></html>"
    )


@pytest.mark.parametrize("snippet", [
    'var message = ";var is_jy=\'Y\';";',
    "function renderer(){\nvar is_jy='Y';\n}",
    "var example = `\nvar is_jy='Y';\n`;",
    "if (false) {\nvar is_jy='Y';\n}",
    "var is_jy='Y';\nif (someRuntimeValue) { is_jy='N'; }",
    "var is_jy='Y';\nwindow.is_jy='N';",
])
def test_quiet_capability_ignores_non_global_literals_and_ambiguous_assignments(snippet):
    _, info = client_with_html(html_with_script(snippet)).init_dc()
    assert info["quiet_carriage_available"] is False


def test_quiet_capability_is_not_read_from_html_text():
    html = html_with_script("") + "<div>\nvar is_jy='Y';\n</div>"
    _, info = client_with_html(html).init_dc()
    assert info["quiet_carriage_available"] is False


def test_quiet_capability_accepts_one_literal_top_level_declaration():
    _, info = client_with_html(html_with_script("var is_jy = 'Y';")).init_dc()
    assert info["quiet_carriage_available"] is True


@pytest.mark.parametrize("html", [
    "<script type='application/json'>var is_jy='Y';</script>",
    "<script type='module'>var is_jy='Y';</script>",
    "<script src='example.js'>var is_jy='Y';</script>",
    "<template><script>var is_jy='Y';</script></template>",
    "<noscript><script>var is_jy='Y';</script></noscript>",
    "<textarea><script>var is_jy='Y';</script></textarea>",
    "<script>var pattern=/;var is_jy='Y';/;</script>",
    "<script>var is_jy='Y';window['is_jy']='N';</script>",
    "<script>var is_jy='Y';</script><script>is_jy='N';</script>",
])
def test_capability_parser_ignores_inert_sources_and_ambiguous_cross_script_writes(html):
    assert extract_quiet_carriage_available(html) is False


@pytest.mark.parametrize("source", [
    "var is_jy='Y';", "/* comment */ var is_jy='Y';",
    "// is_jy='N';\nvar is_jy='Y';",
    "var url='https://example.invalid/';var is_jy='Y';",
    "var pattern=/is_jy/;var is_jy='Y';",
    "<script>var url='https://example.invalid/';var is_jy='Y';</script>",
])
def test_capability_parser_accepts_literal_global_without_comment_or_url_confusion(source):
    assert extract_quiet_carriage_available(source) is True


def test_response_diagnostics_never_log_body_credentials_or_redirect_url(caplog):
    response = requests.Response()
    response.status_code = 502
    response.url = "https://kyfw.12306.cn/login/private-token?cookie=secret-query"
    response.headers["Content-Type"] = "text/html; private=header-secret"
    response._content = b"<html>secret-body and passenger-id</html>"
    response.history = [requests.Response()]
    with pytest.raises(ResponseFormatError):
        RailwayClient._response_json_object(response, "submitOrderRequest")
    assert "HTTP=502" in caplog.text
    assert "body_kind=html" in caplog.text
    assert "redirects=1" in caplog.text
    for secret in ("private-token", "secret-query", "header-secret", "secret-body", "passenger-id"):
        assert secret not in caplog.text


@pytest.mark.parametrize("payload,status", [
    ({"status": False, "data": {"flag": False}}, 200),
    ({"data": {"flag": "false"}}, 200),
    ({"data": {}}, 200),
    ({"data": {"flag": False}}, 429),
    ([], 200),
])
def test_ambiguous_session_check_does_not_trigger_qr_or_claim_expiry(payload, status):
    events = []
    client = RailwayClient(ConnectionConfig(), event_sink=events.append)
    client.session.post = Mock(return_value=SimpleNamespace(status_code=status, json=lambda: payload))
    client._create_qr_code = Mock()
    with pytest.raises(AppError, match="无法确认"):
        client.ensure_login()
    assert [event.data.get("state") for event in events if event.kind == "session_checked"] == ["failed"]
    client._create_qr_code.assert_not_called()


@pytest.mark.parametrize("valid,state", [(True, "valid"), (False, "expired")])
def test_confirmed_session_states_are_distinct(valid, state):
    events = []
    client = RailwayClient(ConnectionConfig(), event_sink=events.append)
    client.session.post = Mock(return_value=SimpleNamespace(status_code=200, json=lambda: {"data": {"flag": valid}}))
    assert client.check_session() is valid
    assert events[-1].data["state"] == state


def existing_clock(session):
    clock = ServerClock(session, ConnectionConfig(time_sync_samples=1))
    clock.has_synchronized = True
    clock.offset_seconds = 17.5
    clock._base_server_timestamp = 1000
    clock._base_perf_counter = 900
    return clock


def assert_existing_anchor(clock):
    assert clock.offset_seconds == 17.5
    assert clock._base_server_timestamp == 1000
    assert clock._base_perf_counter == 900
    assert clock.has_synchronized is True


def test_clock_failure_preserves_anchor_and_hides_network_exception_values(caplog):
    session = SimpleNamespace(head=Mock(side_effect=requests.Timeout("private-token")))
    clock = existing_clock(session)
    events = []
    assert clock.sync(event_sink=events.append) is False
    assert_existing_anchor(clock)
    assert events[-1].data["success"] is False
    assert "private-token" not in caplog.text


def test_exhausted_clock_budget_never_starts_network_request():
    session = SimpleNamespace(head=Mock())
    clock = existing_clock(session)
    assert clock.sync(budget_seconds=0) is False
    session.head.assert_not_called()
    assert_existing_anchor(clock)


def test_cancel_during_clock_response_preserves_existing_anchor():
    token = CancellationToken()
    def head(*args, **kwargs):
        token.cancel()
        return SimpleNamespace(status_code=200, headers={"Date": "Mon, 28 Sep 2026 00:00:00 GMT"})
    clock = existing_clock(SimpleNamespace(head=head))
    with pytest.raises(RunCancelled):
        clock.sync(token)
    assert_existing_anchor(clock)


def test_budget_constrains_clock_timeout_and_disables_redirects():
    session = SimpleNamespace(head=Mock(return_value=SimpleNamespace(status_code=200, headers={"Date": "Mon, 28 Sep 2026 00:00:00 GMT"})))
    clock = existing_clock(session)
    assert clock.sync(budget_seconds=0.1) is True
    kwargs = session.head.call_args.kwargs
    assert 0 < kwargs["timeout"] <= 0.050001
    assert kwargs["allow_redirects"] is False


def test_command_queue_is_disabled_by_default_deduplicates_and_clears_on_transition():
    token = CancellationToken()
    assert token.request_action("check_login") is False
    token.set_actions_enabled(True)
    assert token.request_action("check_login") is True
    assert token.request_action("check_login") is False
    assert token.request_action("sync_clock") is True
    token.set_actions_enabled(False, clear_pending=True)
    assert token.next_action() is None
    assert token.request_action("login") is False


def test_cancellation_wakes_waiting_command_worker_and_discards_pending_work():
    token = CancellationToken()
    token.set_actions_enabled(True)
    started = Event()
    def wait_for_action():
        started.set()
        return token.next_action(timeout=30)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(wait_for_action)
        assert started.wait(1)
        token.cancel()
        with pytest.raises(RunCancelled):
            future.result(timeout=1)
    assert token.request_action("sync_clock") is False
