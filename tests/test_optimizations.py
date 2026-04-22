import email.utils
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ticket_app import configuration
from ticket_app.clock import ServerClock
from ticket_app.helpers import _is_terminal_order_failure
from ticket_app.runner import TicketRunner


class FakeResponse:
    def __init__(self, date_header):
        self.headers = {"Date": date_header}


class FakeSession:
    def __init__(self, date_headers):
        self.date_headers = list(date_headers)

    def head(self, url, timeout):
        return FakeResponse(self.date_headers.pop(0))


class OptimizationTests(unittest.TestCase):
    def test_server_clock_uses_lowest_rtt_sample(self):
        date_header = email.utils.formatdate(1000, usegmt=True)
        cfg = SimpleNamespace(
            request_timeout_seconds=5,
            time_sync_samples=3,
            time_sync_max_rtt_seconds=1.0,
        )
        clock = ServerClock(FakeSession([date_header, date_header, date_header]), cfg)

        wall_times = [1000.0, 1000.4, 1001.0, 1001.1, 1002.0, 1002.2]
        perf_times = [10.0, 10.4, 11.0, 11.1, 12.0, 12.2]
        with patch("ticket_app.clock.time.time", side_effect=wall_times), patch(
            "ticket_app.clock.time.perf_counter", side_effect=perf_times
        ), patch("ticket_app.clock.time.sleep"):
            clock.sync()

        self.assertAlmostEqual(clock.offset_seconds, -1.05, places=6)

    def test_hot_window_interval_switches_around_start_time(self):
        cfg = SimpleNamespace(
            pre_query_seconds=3.0,
            hot_query_interval_seconds=0.25,
            hot_window_seconds=10.0,
            query_interval_seconds=1.0,
        )
        runner = object.__new__(TicketRunner)
        runner.cfg = cfg
        runner.clock = SimpleNamespace(now=lambda: configuration.datetime(2026, 5, 1, 9, 59, 58))
        target = configuration.datetime(2026, 5, 1, 10, 0, 0)

        self.assertEqual(runner._current_query_interval(target), 0.25)

        runner.clock = SimpleNamespace(now=lambda: configuration.datetime(2026, 5, 1, 10, 0, 11))
        self.assertEqual(runner._current_query_interval(target), 1.0)

    def test_terminal_failure_does_not_treat_negative_wait_as_failure(self):
        self.assertFalse(_is_terminal_order_failure(""))
        self.assertFalse(_is_terminal_order_failure("排队中，预计等待 -100 秒"))
        self.assertTrue(_is_terminal_order_failure("没有足够的票!"))

    def test_candidate_order_respects_train_and_seat_priority(self):
        cfg = SimpleNamespace(
            preferred_trains=["G2", "G1"],
            only_preferred_trains=True,
        )
        runner = object.__new__(TicketRunner)
        runner.cfg = cfg
        runner.preferred_order = {"G2": 0, "G1": 1}
        runner.seat_sequence = [("二等座", configuration.SEAT_SPECS["二等座"]), ("一等座", configuration.SEAT_SPECS["一等座"])]
        tickets = [
            {
                "station_train_code": "G1",
                "can_buy": True,
                "seats": {"edz": "有", "ydz": "有"},
            },
            {
                "station_train_code": "G2",
                "can_buy": True,
                "seats": {"edz": "--", "ydz": "1"},
            },
            {
                "station_train_code": "G3",
                "can_buy": True,
                "seats": {"edz": "有", "ydz": "有"},
            },
        ]

        candidates = runner._find_candidates(tickets)

        self.assertEqual([item["ticket"]["station_train_code"] for item in candidates], ["G2", "G1", "G1"])
        self.assertEqual([item["seat_label"] for item in candidates], ["一等座", "二等座", "一等座"])


if __name__ == "__main__":
    unittest.main()
