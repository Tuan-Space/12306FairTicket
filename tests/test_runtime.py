import threading
import unittest

from ticket_app.runtime import CancellationToken, RunCancelled, RuntimeEvent, emit_event


class RuntimeTests(unittest.TestCase):
    def test_emit_event_supports_callable_sink(self):
        events = []
        emit_event(events.append, "phase", "准备", phase="preparing")
        self.assertEqual(len(events), 1)
        self.assertIsInstance(events[0], RuntimeEvent)
        self.assertEqual(events[0].data["phase"], "preparing")

    def test_cancellation_interrupts_wait(self):
        token = CancellationToken()
        token.cancel()
        with self.assertRaises(RunCancelled):
            token.wait(60)

    def test_cancellation_wakes_an_active_wait(self):
        token = CancellationToken()
        stopped = threading.Event()

        def worker():
            try:
                token.wait(60)
            except RunCancelled:
                stopped.set()

        thread = threading.Thread(target=worker)
        thread.start()
        token.cancel()
        thread.join(timeout=1)
        self.assertTrue(stopped.is_set())


if __name__ == "__main__":
    unittest.main()
