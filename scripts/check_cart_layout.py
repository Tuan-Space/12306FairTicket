"""Repeat offline cart layout checks and capture real Qt widgets at each DPI.

Run with the project's Python 3.12 environment. The driver uses a separate Qt
process for each scale, since changing QT_SCALE_FACTOR after QApplication is
created would not test the requested DPI. No 12306 login or query is performed.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def prepare_demo(window) -> None:
    """Use public cart add controls and account UI events, all offline."""
    window.station_names.update({"北京南", "上海虹桥", "北京", "上海"})
    for origin, destination, train, seat in (
        ("北京南", "上海虹桥", "G103", "二等座"),
        ("北京", "上海", "1461", "硬卧"),
        ("北京南", "上海虹桥", "G103", "一等座"),
        ("北京南", "上海虹桥", "D17", "一等卧"),
    ):
        window.from_station.setText(origin)
        window.to_station.setText(destination)
        window.preferred_trains.setText(train)
        window.cart_seat.setCurrentIndex(window.cart_seat.findData(seat))
        assert window._add_cart_items()
    window._on_connection_completed("login", {"authenticated": True, "contacts": [
        {"name": "示例乘车人", "passenger_type": "1"},
    ]})
    window.contact_selector.checkboxes[0].click()


def inspect_layout(window, output: Path | None = None) -> list[str]:
    """Assert useful geometry and save pixels for visual inspection."""
    from PySide6.QtCore import QPoint
    from PySide6.QtTest import QTest
    from ticket_app.gui.cart_widgets import CartDialog
    from ticket_app.gui.worker import GuiCancelToken

    checked = []

    def visible_in(widget, owner):
        assert widget.isVisible(), widget.objectName() or type(widget).__name__
        rect = widget.rect().translated(widget.mapTo(owner, QPoint()))
        assert owner.rect().contains(rect), (widget.objectName(), rect.toTuple(), owner.rect().toTuple())

    def save(widget, name):
        if output is not None:
            widget.hide()
            widget.show()
            QTest.qWait(80)
            widget.repaint()
            assert widget.grab().save(str(output / f"{name}.png"))

    window._go_to_step(0)
    QTest.qWait(30)
    cart_position = window.cart_bar.mapTo(window, QPoint())
    footer_position = window.next_button.mapTo(window, QPoint())
    for value in (0, window.basic_scroll.verticalScrollBar().maximum()):
        window.basic_scroll.verticalScrollBar().setValue(value)
        QTest.qWait(10)
        visible_in(window.cart_bar, window)
        assert window.cart_summary.height() >= window.cart_summary.heightForWidth(window.cart_summary.width())
        visible_in(window.next_button, window)
        assert window.cart_bar.mapTo(window, QPoint()) == cart_position
        assert window.next_button.mapTo(window, QPoint()) == footer_position
        assert window.basic_scroll.horizontalScrollBar().maximum() == 0
    checked.append("第一步购物车栏和下一步固定，正文无横向裁切")
    save(window, "trip")

    dialog = CartDialog(window.cart_items, window, station_names=window.station_names)
    try:
        dialog.resize(min(690, window.width()), min(650, window.height()))
        dialog.show()
        QTest.qWait(30)
        visible_in(dialog.done_button, dialog)
        for index in range(dialog.list.count()):
            item = dialog.list.item(index)
            dialog.list.scrollToItem(item)
            QTest.qWait(5)
            rect = dialog.list.visualItemRect(item)
            assert rect.width() > 200
            assert rect.height() >= dialog.list.fontMetrics().lineSpacing() * 2
            assert str(index + 1) + "." == item.text().split(" ", 1)[0]
        save(dialog, "cart")
        dialog.list.setCurrentRow(0)
        dialog.edit_current()
        QTest.qWait(20)
        visible_in(dialog.done_button, dialog)
        assert dialog.editor_scroll.horizontalScrollBar().maximum() == 0
        dialog.editor_scroll.ensureWidgetVisible(dialog.save_edit_button)
        QTest.qWait(10)
        visible_in(dialog.save_edit_button, dialog)
        visible_in(dialog.save_edit_button, dialog.editor_scroll.viewport())
        visible_in(dialog.edit_title, dialog)
        assert dialog.list.visualItemRect(dialog.list.currentItem()).intersects(dialog.list.viewport().rect())
        checked.append("购物车每行可选择，编辑区可滚动，完成按钮保持可见")
        save(dialog, "cart-edit")
    finally:
        dialog.reject()
        dialog.deleteLater()

    window._go_to_step(2)
    QTest.qWait(30)
    for item in window.cart_items:
        assert item["from_station"] in window.confirm_summary.text()
        assert item["train_code"] in window.confirm_summary.text()
        assert item["seat_type"] in window.confirm_summary.text()
    assert window.confirm_summary.height() >= window.confirm_summary.heightForWidth(window.confirm_summary.width())
    window.confirm_scroll.verticalScrollBar().setValue(window.confirm_scroll.verticalScrollBar().maximum())
    visible_in(window.start_button, window)
    visible_in(window.back_button, window)
    assert window.confirm_scroll.horizontalScrollBar().maximum() == 0
    checked.append("第三步购物车摘要完整，偏好和底部操作可访问")
    window.confirm_scroll.verticalScrollBar().setValue(0)
    save(window, "confirm")

    try:
        window._active_operation = SimpleNamespace(mode="task")
        window._operation_mode = "task"
        window.cancel_token = GuiCancelToken()
        window._task_state = "running"
        window._target_timestamp = time.time() + 180
        window._go_to_step(3)
        window._set_phase("waiting", "离线演示：等待开售")
        window.current_cart_item.setText("备选 1 / 4\n北京南 → 上海虹桥 · G103 · 二等座")
        window.logs_toggle.setChecked(True)
        window._on_log_message("离线界面验证：未连接 12306，未创建订单。", "INFO")
        QTest.qWait(30)
        log_position = window.logs_panel.mapTo(window, QPoint())
        footer_position = window.stop_button.mapTo(window, QPoint())
        for value in (0, window.run_scroll.verticalScrollBar().maximum()):
            window.run_scroll.verticalScrollBar().setValue(value)
            QTest.qWait(10)
            for widget in (window.logs_panel, window.stop_button, window.back_button):
                visible_in(widget, window)
            assert window.logs_panel.mapTo(window, QPoint()) == log_position
            assert window.stop_button.mapTo(window, QPoint()) == footer_position
            assert window.run_scroll.horizontalScrollBar().maximum() == 0
        checked.append("第四步日志固定且可独立滚动，上一步和停止按钮保持可见")
        window.run_scroll.verticalScrollBar().setValue(0)
        save(window, "waiting")
    finally:
        window._active_operation = None
        window.cancel_token = None
        window._operation_mode = ""
        window._task_state = "idle"
    return checked


def run_case(args) -> int:
    os.environ["QT_QPA_PLATFORM"] = args.platform
    os.environ["QT_SCALE_FACTOR"] = str(args.scale)
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from ticket_app.gui import app as gui_app

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication([])
    app.setStyleSheet(gui_app._load_stylesheet(app, args.theme == "dark"))
    app.setProperty("darkTheme", args.theme == "dark")
    args.output.mkdir(parents=True, exist_ok=True)
    result = {"theme": args.theme, "scale": args.scale, "requested_size": args.size}
    with ExitStack() as stack:
        stack.enter_context(patch("requests.sessions.Session.request", side_effect=AssertionError("Layout checks must stay offline")))
        stack.enter_context(patch.object(gui_app, "LOCAL_DATA_DIR", args.output / ".runtime"))
        stack.enter_context(patch.object(gui_app.MainWindow, "_setup_tray", lambda window: setattr(window, "tray", None)))
        window = gui_app.MainWindow()
        try:
            width, height = map(int, args.size.split("x"))
            available = window.screen().availableGeometry()
            window.resize(min(width, available.width() - 40), min(height, available.height() - 60))
            window.show()
            QTest.qWait(100)
            prepare_demo(window)
            result["actual_size"] = [window.width(), window.height()]
            result["checks"] = inspect_layout(window, args.output)
            result["passed"] = True
        except Exception as exc:
            result["passed"] = False
            result["error"] = repr(exc)
            window.grab().save(str(args.output / "failure.png"))
        finally:
            window.close()
            for _ in range(150):
                if window._background_thread is None:
                    break
                QTest.qWait(20)
    (args.output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["passed"] else 1


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts" / "cart-ui-check")
    parser.add_argument("--platform", default="windows" if os.name == "nt" else "offscreen")
    parser.add_argument("--case", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--scale", default="1")
    parser.add_argument("--theme", choices=("dark", "light"), default="dark")
    parser.add_argument("--size", default="750x880")
    args = parser.parse_args()
    if args.case:
        return run_case(args)
    failures = 0
    cases = []
    for scale in ("1", "1.5", "2"):
        for theme in ("dark", "light"):
            for size in ("750x880", "640x480"):
                folder = args.output / f"{theme}-{scale}-{size}"
                command = [sys.executable, str(Path(__file__).resolve()), "--case", "--platform", args.platform,
                           "--scale", scale, "--theme", theme, "--size", size, "--output", str(folder)]
                environment = {**os.environ, "PYTHONIOENCODING": "utf-8"}
                result = subprocess.run(command, cwd=ROOT, env=environment, check=False,
                                        timeout=30, capture_output=True, text=True, encoding="utf-8")
                print(result.stdout.strip() or result.stderr.strip())
                failures += result.returncode != 0
                report = folder / "result.json"
                if report.exists():
                    cases.append(json.loads(report.read_text(encoding="utf-8")))
                else:
                    cases.append({"theme": theme, "scale": scale, "requested_size": size,
                                  "passed": False, "error": result.stderr.strip()})
    (args.output / "matrix.json").write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Layout cases: {12 - failures} / 12 passed. Screenshots: {args.output}")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
