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
from ticket_app.gui.cart_widgets import CartDialog


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
                window.from_station.setText("北京南")
                window.to_station.setText("上海虹桥")
                window.preferred_trains.setText("G103")
                window.cart_seat.setCurrentIndex(window.cart_seat.findData("二等座"))
                assert window._add_cart_items()
                window.from_station.setText("北京")
                window.to_station.setText("上海")
                window.preferred_trains.setText("1461")
                window.cart_seat.setCurrentIndex(window.cart_seat.findData("硬卧"))
                assert window._add_cart_items()
                window.from_station.setText("北京南")
                window.to_station.setText("上海虹桥")
                window.preferred_trains.setText("G103")
                window.cart_seat.setCurrentIndex(window.cart_seat.findData("一等座"))
                assert window._add_cart_items()
                window.basic_scroll.ensureWidgetVisible(window.add_cart_button, 0, 12)
                save("trip")
                dialog = CartDialog(window.cart_items, window, station_names=window.station_names)
                dialog.show()
                QTest.qWait(150)
                assert dialog.grab().save(str(output / f"{args.prefix}-list.png"))
                dialog.close()
                window._go_to_step(1)
                save("login")
                window._on_connection_completed("login", {"authenticated": True, "contacts": [
                    {"name": "示例乘车人", "passenger_type": "1"},
                ]})
                window.contact_selector.checkboxes[0].click()
                window._go_to_step(2)
                save("confirm")
                window._active_operation = SimpleNamespace(mode="task")
                window._operation_mode = "task"
                window.cancel_token = GuiCancelToken()
                window._task_state = "running"
                window._go_to_step(3)
                window._target_timestamp = time.time() + 180
                window.current_cart_item.setText("购物车 3 项 · 等待开售后按顺序尝试")
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
    print(f"Saved 5 offline GUI screenshots to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
