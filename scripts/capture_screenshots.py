"""Capture real GUI widgets with offline demo data for README (no orders)."""

from pathlib import Path
import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "windows" if os.name == "nt" else "offscreen")
os.environ["QT_SCALE_FACTOR"] = "1"

from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from ticket_app.gui.app import MainWindow, _load_stylesheet
from ticket_app.gui.worker import GuiCancelToken


def main() -> int:
    app = QApplication([])
    app.setProperty("darkTheme", True)
    app.setStyleSheet(_load_stylesheet(app, True))
    output = ROOT / "docs" / "images"
    output.mkdir(parents=True, exist_ok=True)
    errors = []
    with patch("requests.sessions.Session.request", side_effect=AssertionError("Screenshots must stay offline")):
        window = MainWindow()
        window.show()

        def save(name):
            # Re-show after layout changes so native Windows backing stores
            # paint every footer control before the widget capture.
            window.hide()
            window.show()
            QTest.qWait(120)
            window.repaint()
            if not window.grab().save(str(output / f"v2-{name}.png")):
                raise RuntimeError(f"Cannot save {name}")

        def capture():
            try:
                save("trip")
                window._go_to_step(1)
                save("login")
                window._on_connection_completed("login", {"authenticated": True, "contacts": [
                    {"name": "示例乘车人", "passenger_type": "1"},
                ]})
                window.contact_selector.checkboxes[0].click()
                window.seat_types.set_values(["二等座", "一等座"])
                window._go_to_step(2)
                save("confirm")
                window._active_operation = SimpleNamespace(mode="task")
                window._operation_mode = "task"
                window.cancel_token = GuiCancelToken()
                window._task_state = "running"
                window._go_to_step(3)
                window._target_timestamp = time.time() + 180
                window._set_phase("waiting", "等待开售，届时自动开始查询")
                window._on_log_message("离线演示：正在等待开售，未连接 12306。", "INFO")
                save("waiting")
                assert window.back_button.isVisible(), "Step 4 back button must be visible"
            except Exception as exc:
                errors.append(exc)
            finally:
                window._active_operation = None
                window.cancel_token = None
                window._operation_mode = ""
                window.close()
                app.quit()

        QTimer.singleShot(250, capture)
        app.exec()
    if errors:
        raise errors[0]
    print(f"Saved 4 offline GUI screenshots to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
