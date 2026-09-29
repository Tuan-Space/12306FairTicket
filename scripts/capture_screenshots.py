"""Capture real GUI widgets with offline demo data for README (no orders)."""

from pathlib import Path
import argparse
import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "windows" if os.name == "nt" else "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from ticket_app.gui.app import MainWindow, _load_stylesheet
from ticket_app.gui.worker import GuiCancelToken


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "docs" / "images")
    parser.add_argument("--prefix", default="cart")
    parser.add_argument("--scale", default="1")
    parser.add_argument("--light", action="store_true")
    parser.add_argument("--width", type=int, default=750)
    parser.add_argument("--height", type=int, default=880)
    args = parser.parse_args()
    os.environ["QT_SCALE_FACTOR"] = args.scale
    app = QApplication([])
    app.setProperty("darkTheme", not args.light)
    app.setStyleSheet(_load_stylesheet(app, not args.light))
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    errors = []
    with patch("requests.sessions.Session.request", side_effect=AssertionError("Screenshots must stay offline")):
        window = MainWindow()
        window.resize(min(args.width, window.width()), min(args.height, window.height()))
        window.show()

        def save(name):
            # Re-show after layout changes so native Windows backing stores
            # paint every footer control before the widget capture.
            window.hide()
            window.show()
            QTest.qWait(120)
            window.repaint()
            if not window.grab().save(str(output / f"{args.prefix}-{name}.png")):
                raise RuntimeError(f"Cannot save {name}")

        def capture():
            try:
                window.station_names.update({"北京南", "上海虹桥", "北京", "上海"})
                window.from_station.setText("北京南")
                window.to_station.setText("上海虹桥")
                window.preferred_trains.setText("G103，G105")
                window.cart_seat.set_selected_seats(["二等座", "一等座"])
                assert [(item["train_code"], item["seat_type"]) for item in window._draft_cart_items()] == [
                    ("G103", "二等座"), ("G105", "二等座"),
                    ("G103", "一等座"), ("G105", "一等座"),
                ]
                # Capture the actionable state: the add button shows the four
                # pending alternatives; explanatory previews start folded.
                window.basic_scroll.verticalScrollBar().setValue(0)
                save("trip")
                assert window._add_cart_items()

                # Add sleeper alternatives separately. These are explicit cart
                # items, not a train-prefix guess or a global seat replacement.
                window.preferred_trains.setText("D17")
                window.cart_seat.set_selected_seats(["二等卧"])
                assert window._add_cart_items()
                window.from_station.setText("北京")
                window.to_station.setText("上海")
                window.preferred_trains.setText("1461")
                window.cart_seat.set_selected_seats(["硬卧"])
                assert window._add_cart_items()
                window._go_to_step(1)
                window._on_connection_completed("login", {"authenticated": True, "contacts": [
                    {"name": "示例乘客甲", "passenger_type": "1"},
                    {"name": "示例乘客乙", "passenger_type": "3"},
                    {"name": "示例乘客丙", "passenger_type": "1"},
                    {"name": "示例乘客丁", "passenger_type": "1"},
                ]})
                for checkbox in window.contact_selector.checkboxes[:3]:
                    checkbox.click()
                window.passenger_scroll.verticalScrollBar().setValue(0)
                save("login")
                window._go_to_step(2)
                assert window.confirm_cart.items() == window.cart_items
                window.confirm_scroll.verticalScrollBar().setValue(0)
                save("confirm")
                window._active_operation = SimpleNamespace(mode="task")
                window._operation_mode = "task"
                window.cancel_token = GuiCancelToken()
                window._task_state = "running"
                window._go_to_step(3)
                window._on_runtime_event("clock_sync", {
                    "source": "manual", "success": True, "rtt_ms": 42, "offset_seconds": 0.128,
                    "server_timestamp": time.time() + 0.128, "monotonic_timestamp": time.monotonic(),
                    "checked_at": time.time(),
                })
                window._on_runtime_event("session_checked", {"state": "valid", "checked_at": time.time()})
                window._target_timestamp = time.time() + 180
                window.current_cart_item.setText(f"购物车 {len(window.cart_items)} 项 · 等待开售后按顺序尝试")
                window._set_phase("waiting", "等待开售，届时自动开始查询")
                window._on_runtime_event("maintenance_availability", {"enabled": True, "busy": False})
                assert window.sale_countdown.isVisible()
                window.logs_toggle.setChecked(True)
                window.log_view.clear()
                window._on_log_message("离线演示：正在等待开售，未连接 12306。", "INFO")
                window._on_log_message("离线演示：购物车按排列顺序尝试，任一成功即停止。", "INFO")
                window._on_log_message("离线演示：只有确认页的开始任务才会启动订票。", "INFO")
                save("waiting")
                assert window.back_button.isVisible(), "Step 4 back button must be visible"
            except Exception as exc:
                errors.append(exc)
            finally:
                window._active_operation = None
                window.cancel_token = None
                window._operation_mode = ""
                window.close()
                for _ in range(150):
                    if window._background_thread is None:
                        break
                    QTest.qWait(20)
                app.quit()

        QTimer.singleShot(250, capture)
        app.exec()
    if errors:
        raise errors[0]
    print(f"Saved 4 offline GUI screenshots to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
