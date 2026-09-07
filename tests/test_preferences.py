import base64
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from ticket_app.client import RailwayClient
from ticket_app.configuration import AppConfig, AppError, PreparedPassengerSet
from ticket_app.preferences import (
    BerthPreference,
    OrderCapabilities,
    OrderPreferencePayload,
    SeatRelationPreference,
    build_order_preference_payload,
    seat_layout_letters,
)


class FakeResponse:
    def __init__(self, payload=None, text=""):
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def base_config_mapping(**updates):
    mapping = {
        "FROM_STATION": "北京南",
        "TO_STATION": "上海虹桥",
        "TRAIN_DATE": (date.today() + timedelta(days=1)).isoformat(),
        "PASSENGER_NAMES": ["甲"],
        "SEAT_TYPES": ["二等座"],
        "AUTO_SUBMIT": True,
    }
    mapping.update(updates)
    return mapping


class PreferenceModelTests(unittest.TestCase):
    def test_current_seat_layouts_and_business_dw_flag(self):
        self.assertEqual(seat_layout_letters("O"), ("A", "B", "C", "D", "F"))
        self.assertEqual(seat_layout_letters("M"), ("A", "C", "D", "F"))
        self.assertEqual(seat_layout_letters("P"), ("A", "C", "F"))
        self.assertEqual(seat_layout_letters("9", "a,b,c,S"), ("A", "F"))
        self.assertEqual(seat_layout_letters("9", "a,b,c,X"), ("A", "C", "F"))
        self.assertEqual(seat_layout_letters("9", ""), ("A", "F"))
        self.assertEqual(seat_layout_letters("9", "a,b,c"), ("A", "F"))
        self.assertEqual(seat_layout_letters("9", "a,b,c,"), ("A", "F"))

    def test_legacy_choose_seats_parses_and_sorts(self):
        preference = SeatRelationPreference.from_value("2F 1A")
        self.assertEqual(preference.positions, ("1A", "2F"))
        self.assertEqual(preference.to_choose_seats(2, "O"), "1A2F")

    def test_duplicate_count_and_layout_validation(self):
        with self.assertRaisesRegex(ValueError, "不能重复"):
            SeatRelationPreference.from_value(["1A", "1A"])
        with self.assertRaisesRegex(ValueError, "必须等于乘车人数"):
            SeatRelationPreference.from_value(["1A"]).validate(2, "O")
        with self.assertRaisesRegex(ValueError, "不支持位置"):
            SeatRelationPreference.from_value(["1B"]).validate(1, "M")
        with self.assertRaisesRegex(ValueError, "第一排"):
            SeatRelationPreference.from_value(["2A"]).validate(1, "O")

    def test_berth_encoding_is_lower_middle_upper(self):
        self.assertEqual(BerthPreference(1, 1, 1).to_seat_detail_type(3, True), "111")
        self.assertEqual(BerthPreference(2, 0, 1).to_seat_detail_type(3, False), "201")
        self.assertEqual(BerthPreference().to_seat_detail_type(1, False), "000")
        with self.assertRaisesRegex(ValueError, "不支持中铺"):
            BerthPreference(0, 1, 0).to_seat_detail_type(1, False)

    def test_capabilities_parse_exact_y_and_fail_closed_for_preferences(self):
        capabilities = OrderCapabilities.from_mapping(
            {
                "canChooseSeats": "Y",
                "choose_Seats": "OM9",
                "canChooseBeds": "Z",
                "isCanChooseMid": "N",
            }
        )
        self.assertTrue(capabilities.can_choose_seats)
        self.assertEqual(capabilities.allowed_seat_types, frozenset({"O", "M", "9"}))
        self.assertFalse(capabilities.can_choose_beds)
        self.assertIn("人证核验", capabilities.berth_unavailable_message())

        malformed = OrderCapabilities.from_mapping(
            {"canChooseSeats": True, "choose_Seats": ["O"], "canChooseBeds": 1}
        )
        self.assertFalse(malformed.can_choose_seats)
        self.assertFalse(malformed.allowed_seat_types)
        self.assertFalse(malformed.can_choose_beds)
        self.assertFalse(
            OrderCapabilities.from_mapping(
                {"canChooseSeats": "Y", "choose_Seats": "O?"}
            ).allowed_seat_types
        )

    def test_payload_uses_runtime_capability_or_soft_fallback(self):
        seat = SeatRelationPreference.from_value(["1A", "1F"])
        berth = BerthPreference()
        supported = OrderCapabilities.from_mapping(
            {"canChooseSeats": "Y", "choose_Seats": "O", "canChooseBeds": "N"}
        )
        payload = build_order_preference_payload(seat, berth, supported, "O", 2)
        self.assertEqual(payload.choose_seats, "1A1F")
        self.assertEqual(payload.seat_detail_type, "000")

        unavailable = OrderCapabilities.from_mapping(
            {"canChooseSeats": "Y", "choose_Seats": "M", "canChooseBeds": "N"}
        )
        payload = build_order_preference_payload(seat, berth, unavailable, "O", 2)
        self.assertEqual(payload.choose_seats, "")
        self.assertTrue(payload.warnings)

    def test_bed_payload_and_middle_fallback(self):
        berth = BerthPreference(lower=1, middle=1, upper=0)
        supported = OrderCapabilities.from_mapping(
            {"canChooseBeds": "Y", "isCanChooseMid": "Y"}
        )
        payload = build_order_preference_payload(
            SeatRelationPreference(), berth, supported, "3", 2
        )
        self.assertEqual(payload.seat_detail_type, "110")

        no_middle = OrderCapabilities.from_mapping(
            {"canChooseBeds": "Y", "isCanChooseMid": "N"}
        )
        payload = build_order_preference_payload(
            SeatRelationPreference(), berth, no_middle, "3", 2
        )
        self.assertEqual(payload.seat_detail_type, "000")
        self.assertTrue(payload.warnings)

    def test_preferences_warn_when_candidate_seat_type_is_inapplicable(self):
        payload = build_order_preference_payload(
            SeatRelationPreference.from_value(["1A"]),
            BerthPreference(lower=1),
            OrderCapabilities(),
            "",
            1,
        )
        self.assertEqual(payload.choose_seats, "")
        self.assertEqual(payload.seat_detail_type, "000")
        self.assertTrue(any("座位位置偏好不适用" in item for item in payload.warnings))
        self.assertTrue(any("铺位偏好不适用" in item for item in payload.warnings))

    def test_passenger_count_is_limited_to_one_through_five(self):
        preference = SeatRelationPreference()
        for count in range(1, 6):
            with self.subTest(count=count):
                preference.validate(count)
        with self.assertRaisesRegex(ValueError, "1 到 5"):
            preference.validate(0)
        with self.assertRaisesRegex(ValueError, "1 到 5"):
            preference.validate(6)

    def test_explicit_n_missing_and_too_many_bed_status_fail_closed(self):
        denied = OrderCapabilities.from_mapping(
            {"canChooseSeats": "N", "choose_Seats": "O", "canChooseBeds": "5"}
        )
        self.assertFalse(denied.can_choose_seats)
        self.assertFalse(denied.can_choose_beds)
        self.assertIn("超过5位", denied.berth_unavailable_message())

        missing = OrderCapabilities.from_mapping({})
        self.assertFalse(missing.can_choose_seats)
        self.assertFalse(missing.can_choose_beds)
        self.assertFalse(missing.can_choose_middle)


class AppConfigPreferenceTests(unittest.TestCase):
    def test_mapping_round_trip_and_new_key_precedes_legacy_key(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.py"
            with self.assertLogs(level="WARNING") as captured:
                cfg = AppConfig.from_mapping(
                    base_config_mapping(
                        PASSENGER_NAMES=["甲", "乙"],
                        SEAT_POSITION_PREFERENCES=["1A", "1F"],
                        CHOOSE_SEATS="1B2B",
                        PERSIST_SESSION=False,
                    ),
                    path,
                )
            self.assertEqual(cfg.seat_relation_preference.positions, ("1A", "1F"))
            self.assertEqual(cfg.choose_seats, "1A1F")
            self.assertFalse(cfg.persist_session)
            self.assertIn("忽略 CHOOSE_SEATS", "\n".join(captured.output))

            round_tripped = AppConfig.from_mapping(cfg.to_mapping(), path)
            self.assertEqual(round_tripped.seat_relation_preference, cfg.seat_relation_preference)
            self.assertEqual(round_tripped.berth_preference, cfg.berth_preference)
            self.assertEqual(round_tripped.persist_session, cfg.persist_session)

    def test_legacy_choose_seats_is_supported(self):
        with self.assertLogs(level="INFO") as captured:
            cfg = AppConfig.from_mapping(base_config_mapping(CHOOSE_SEATS="1A"))
        self.assertEqual(cfg.seat_relation_preference.positions, ("1A",))
        self.assertIn("转换为结构化", "\n".join(captured.output))

    def test_invalid_layout_and_more_than_five_passengers_are_rejected(self):
        with self.assertRaisesRegex(AppError, "布局"):
            AppConfig.from_mapping(
                base_config_mapping(SEAT_TYPES=["一等座"], CHOOSE_SEATS="1B")
            )
        with self.assertRaisesRegex(AppError, "最多支持 5"):
            AppConfig.from_mapping(
                base_config_mapping(PASSENGER_NAMES=[str(index) for index in range(6)])
            )

    def test_berth_preference_requires_matching_count_and_sleeper_type(self):
        with self.assertRaisesRegex(AppError, "必须等于乘车人数"):
            AppConfig.from_mapping(
                base_config_mapping(
                    PASSENGER_NAMES=["甲", "乙"],
                    SEAT_TYPES=["硬卧"],
                    BERTH_PREFERENCE={"lower": 1, "middle": 0, "upper": 0},
                )
            )
        with self.assertRaisesRegex(AppError, "卧铺席别"):
            AppConfig.from_mapping(
                base_config_mapping(BERTH_PREFERENCE={"lower": 1})
            )


class RailwayClientPreferenceTests(unittest.TestCase):
    @staticmethod
    def make_client(response):
        client = object.__new__(RailwayClient)
        client.cfg = SimpleNamespace(request_timeout_seconds=5, choose_seats="")
        client.session = Mock()
        client.session.post.return_value = response
        return client

    def test_check_order_parses_capabilities_and_keeps_legacy_unpacking(self):
        response = FakeResponse(
            {
                "status": True,
                "data": {
                    "submitStatus": True,
                    "canChooseSeats": "Y",
                    "choose_Seats": "OM",
                    "canChooseBeds": "N",
                    "isCanChooseMid": "N",
                },
            }
        )
        client = self.make_client(response)
        passengers = PreparedPassengerSet([], "O,0,...", "甲,1,..._")

        result = client.check_order_info(passengers, "token")

        self.assertTrue(result.success)
        self.assertTrue(result.capabilities.can_choose_seats)
        self.assertIn("O", result.capabilities.allowed_seat_types)
        self.assertEqual(tuple(result), (True, "OK"))
        sent = client.session.post.call_args.kwargs["data"]
        self.assertEqual(sent["bed_level_order_num"], "0" * 30)

    def test_confirm_uses_explicit_preference_payload(self):
        client = self.make_client(
            FakeResponse({"status": True, "data": {"submitStatus": True}})
        )
        passengers = PreparedPassengerSet([], "O,0,...", "甲,1,..._")
        preference = OrderPreferencePayload("1A", "000")

        ok, message = client.confirm_single_for_queue(
            passengers,
            {"key_check_isChange": "key", "train_location": "P2"},
            "left",
            "token",
            preference,
        )

        self.assertTrue(ok)
        self.assertEqual(message, "OK")
        sent = client.session.post.call_args.kwargs["data"]
        self.assertEqual(sent["choose_seats"], "1A")
        self.assertEqual(sent["seatDetailType"], "000")

    def test_init_dc_exposes_nested_dw_flag_context(self):
        html = (
            "globalRepeatSubmitToken = 'token';"
            'var ticketInfoForPassengerForm = {"orderRequestDTO":'
            '{"dw_flag":"a,b,c,S"},"leftTicketStr":"left"};'
        )
        client = self.make_client(FakeResponse(text=html))

        token, context = client.init_dc()

        self.assertEqual(token, "token")
        self.assertEqual(context["dw_flag"], "a,b,c,S")
        self.assertEqual(context["orderRequestDTO"]["dw_flag"], "a,b,c,S")

    def test_qr_creation_returns_memory_bytes_and_uuid(self):
        image_bytes = b"fake-png"
        client = self.make_client(
            FakeResponse(
                {
                    "result_code": "0",
                    "image": base64.b64encode(image_bytes).decode("ascii"),
                    "uuid": "uuid-1",
                }
            )
        )

        actual_bytes, uuid = client._create_qr_code()

        self.assertEqual(actual_bytes, image_bytes)
        self.assertEqual(uuid, "uuid-1")

    def test_persist_session_false_skips_cookie_io(self):
        client = object.__new__(RailwayClient)
        client.cfg = SimpleNamespace(persist_session=False, session_file=Path("ignored"))
        client.session = requests.Session()
        with patch("ticket_app.client.MozillaCookieJar") as cookie_jar:
            client.load_cookies()
            client.save_cookies()
        cookie_jar.assert_not_called()

    def test_persist_session_false_does_not_write_qr_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            qr_path = Path(temp_dir) / "login_qr.png"
            session_path = Path(temp_dir) / "session.cookies"
            cfg = SimpleNamespace(
                persist_session=False,
                session_file=session_path,
                qr_code_file=qr_path,
                request_timeout_seconds=5,
                login_qr_timeout_seconds=30,
                login_qr_poll_seconds=0.01,
            )
            events = []
            client = RailwayClient(cfg, event_sink=events.append)
            client.check_session = Mock(return_value=False)
            client._prefetch_login_cookies = Mock()
            client._create_qr_code = Mock(return_value=(b"qr-bytes", "uuid"))
            client._check_qr_status = Mock(return_value=("2", "confirmed"))
            client._complete_login = Mock(return_value=(True, "OK"))

            client.ensure_login()

            self.assertFalse(qr_path.exists())
            self.assertFalse(session_path.exists())
            qr_ready = [event for event in events if event.kind == "qr_ready"]
            self.assertEqual(qr_ready[0].data["image_bytes"], b"qr-bytes")

    def test_qr_polling_emits_waiting_scanned_and_expired_states(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = SimpleNamespace(
                persist_session=False,
                session_file=Path(temp_dir) / "session.cookies",
                qr_code_file=Path(temp_dir) / "login_qr.png",
                request_timeout_seconds=5,
                login_qr_timeout_seconds=30,
                login_qr_poll_seconds=0.001,
            )
            events = []
            client = RailwayClient(cfg, event_sink=events.append)
            client.check_session = Mock(return_value=False)
            client._prefetch_login_cookies = Mock()
            client._create_qr_code = Mock(return_value=(b"qr-bytes", "uuid"))
            client._check_qr_status = Mock(
                side_effect=[("0", "waiting"), ("1", "scanned"), ("3", "expired")]
            )

            with self.assertRaisesRegex(AppError, "二维码已过期"):
                client.ensure_login()

            statuses = [
                event.data.get("status")
                for event in events
                if event.kind == "qr_status"
            ]
            self.assertEqual(statuses, ["waiting", "scanned", "expired"])


if __name__ == "__main__":
    unittest.main()
