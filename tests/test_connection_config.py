from dataclasses import replace

import pytest

from ticket_app.configuration import AppError, ConnectionConfig


def test_connection_config_does_not_require_or_validate_booking_fields(tmp_path):
    cfg = ConnectionConfig.from_mapping({"from_station": "", "seat_types": [], "passenger_names": [], "train_date": "invalid"}, tmp_path)
    assert cfg.persist_session is False
    assert cfg.session_file == tmp_path / ".runtime/session.cookies"
    assert cfg.qr_code_file == tmp_path / ".runtime/login_qr.png"
    assert not hasattr(cfg, "from_station")


def test_connection_config_accepts_both_config_key_styles(tmp_path):
    cfg = ConnectionConfig.from_mapping({"REQUEST_TIMEOUT_SECONDS": "5.5", "time_sync_samples": "3", "LOGIN_QR_POLL_SECONDS": 0.5, "session_file": "cookies", "qr_code_file": "qr"}, base_dir=tmp_path)
    assert cfg.request_timeout_seconds == 5.5
    assert cfg.time_sync_samples == 3
    assert cfg.login_qr_poll_seconds == 0.5
    assert cfg.session_file == tmp_path / "cookies"


@pytest.mark.parametrize("name", ["request_timeout_seconds", "login_qr_timeout_seconds", "login_qr_poll_seconds", "time_sync_max_rtt_seconds", "time_sync_samples"])
@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True, None, "bad"])
def test_connection_config_rejects_invalid_numeric_parameters(name, value):
    with pytest.raises(AppError):
        ConnectionConfig.from_mapping({name: value})


def test_fractional_samples_and_invalid_direct_construction_are_rejected():
    with pytest.raises(AppError, match="正整数"):
        ConnectionConfig.from_mapping({"time_sync_samples": 1.5})
    with pytest.raises(AppError):
        replace(ConnectionConfig(), time_sync_samples=True).validate()
    with pytest.raises(AppError):
        replace(ConnectionConfig(), request_timeout_seconds=float("nan")).validate()
