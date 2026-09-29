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
        window.cart_seat.set_selected_seats([seat])
        assert window._add_cart_items()
    window._on_connection_completed("login", {"authenticated": True, "contacts": [
        {"name": "示例乘客甲", "passenger_type": "1"},
        {"name": "示例乘客乙", "passenger_type": "3"},
        {"name": "示例乘客丙", "passenger_type": "1"},
        {"name": "示例乘客丁", "passenger_type": "1"},
    ]})
    for checkbox in window.contact_selector.checkboxes[:3]:
        checkbox.click()


def inspect_layout(window, output: Path | None = None) -> list[str]:
    """Assert useful geometry and save pixels for visual inspection."""
    from PySide6.QtCore import QPoint
    from PySide6.QtTest import QTest
    from ticket_app.gui.cart_widgets import CartDialog
    from ticket_app.gui.worker import GuiCancelToken, OperationOutcome

    checked = []

    def visible_in(widget, owner):
        assert widget.isVisible(), widget.objectName() or type(widget).__name__
        rect = widget.rect().translated(widget.mapTo(owner, QPoint()))
        assert owner.rect().contains(rect), (widget.objectName(), rect.getRect(), owner.rect().getRect())

    def in_window(widget):
        return widget.rect().translated(widget.mapTo(window, QPoint()))

    def check_three_columns(widgets):
        row = list(widgets)[:3]
        assert len(row) == 3
        assert max(in_window(widget).center().y() for widget in row) - min(
            in_window(widget).center().y() for widget in row) <= 4
        assert all(in_window(left).right() < in_window(right).left() for left, right in zip(row, row[1:]))

    def check_floating_cart(scroll, primary_button):
        """Only the cart shape overlays the viewport; the footer stays clear."""
        QTest.qWait(20)
        cart_position = window.cart_button.mapTo(window, QPoint())
        footer_position = primary_button.mapTo(window, QPoint())
        assert window.cart_button.count == len(window.cart_items)
        maximum = scroll.verticalScrollBar().maximum()
        for value in (0, maximum // 2, maximum):
            scroll.verticalScrollBar().setValue(value)
            QTest.qWait(10)
            visible_in(window.cart_button, window)
            visible_in(window.cart_button, window.steps)
            visible_in(primary_button, window)
            assert window.cart_button.mapTo(window, QPoint()) == cart_position
            assert primary_button.mapTo(window, QPoint()) == footer_position
            assert in_window(scroll.viewport()).bottom() >= in_window(window.cart_button).bottom()
            assert not window.cart_button.mask().contains(QPoint(0, 0))
            assert not window.cart_button.mask().contains(QPoint(70, 70))
            assert not in_window(window.cart_button).intersects(in_window(primary_button))
            assert scroll.horizontalScrollBar().maximum() == 0
        scroll.verticalScrollBar().setValue(0)

    def save(widget, name):
        if output is not None:
            widget.hide()
            widget.show()
            QTest.qWait(80)
            widget.repaint()
            assert widget.grab().save(str(output / f"{name}.png"))

    window._go_to_step(0)
    QTest.qWait(30)
    check_floating_cart(window.basic_scroll, window.next_button)
    assert not hasattr(window, "prep_sync_clock_button")
    for left, right in ((window.from_station, window.to_station),
                        (window.from_station, window.update_stations_button),
                        (window.train_scope, window.preferred_trains),
                        (window.start_at, window.stop_at)):
        assert abs(in_window(left).center().y() - in_window(right).center().y()) <= 4, (
            left.objectName(), right.objectName(), in_window(left).getRect(), in_window(right).getRect())
    checked.append("第一步悬浮购物车固定且不挡下一步，站点、时间及车次按行排列")
    save(window, "trip")
    seat_top = window.cart_seat.mapTo(window.basic_scroll.widget(), QPoint()).y()
    window.basic_scroll.verticalScrollBar().setValue(max(0, seat_top - 28))
    QTest.qWait(20)
    save(window, "seats")

    window.advanced_toggle.setChecked(True)
    QTest.qWait(20)
    check_floating_cart(window.basic_scroll, window.next_button)
    check_three_columns(window.advanced[key] for key in ("query_interval_seconds", "max_retries", "pre_query_seconds"))
    advanced_top = window.advanced_content.mapTo(window.basic_scroll.widget(), QPoint()).y()
    window.basic_scroll.verticalScrollBar().setValue(max(0, advanced_top - 8))
    QTest.qWait(20)
    save(window, "advanced")
    window.advanced_toggle.setChecked(False)

    window._go_to_step(1)
    check_floating_cart(window.passenger_scroll, window.next_button)
    check_three_columns(window.contact_selector.checkboxes)
    check_three_columns(window.passenger_ticket_types.rows.values())
    assert in_window(window.contact_selector.checkboxes[3]).top() > in_window(window.contact_selector.checkboxes[0]).bottom()
    assert len(window.contact_selector.selected_names()) == 3
    assert not window.field_blocks["passenger_names"].isHidden()
    assert in_window(window.passenger_name_label).right() < in_window(window.passengers).left()
    checked.append("第二步姓名框常驻，联系人和票种每行三列，悬浮购物车不遮挡正文和下一步")
    save(window, "passengers")

    dialog = CartDialog(window.cart_items, window, station_names=window.station_names)
    try:
        dialog.resize(min(dialog.width(), window.width()), min(dialog.height(), window.height()))
        dialog.show()
        QTest.qWait(30)
        assert dialog.cart_help.isVisible()
        visible_in(dialog.done_button, dialog)
        for index in range(dialog.list.count()):
            item = dialog.list.item(index)
            dialog.list.scrollToItem(item)
            QTest.qWait(5)
            rect = dialog.list.visualItemRect(item)
            assert rect.width() > 200
            assert rect.height() == 44
            assert "\n" not in item.text()
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
        checked.append("购物车44px单行完整绑定站对车次席别，编辑可滚动，保存修改保持可见")
        save(dialog, "cart-edit")
    finally:
        dialog.reject()
        dialog.deleteLater()

    window._go_to_step(2)
    QTest.qWait(30)
    check_floating_cart(window.confirm_scroll, window.start_button)
    assert window.confirm_cart.items() == window.cart_items
    assert window.confirm_cart.verticalScrollBar().maximum() == 0
    for item in window.cart_items:
        assert item["from_station"] in window.confirm_cart.text()
        assert item["train_code"] in window.confirm_cart.text()
        assert item["seat_type"] in window.confirm_cart.text()
    assert not hasattr(window, "preference_help")
    assert not hasattr(window, "quiet_help")
    for label in (window.confirm_summary, window.preference_note, window.position_preferences.seats.guide):
        assert label.height() >= label.heightForWidth(label.width())
        assert label.mapTo(window.confirm_scroll.widget(), label.rect().bottomRight()).x() < window.confirm_scroll.widget().width()
    tabs = window.position_preferences.tabs
    for index in (0, 1, 0):
        tabs.setCurrentIndex(index)
        QTest.qWait(30)
        page = tabs.currentWidget()
        assert page.height() >= page.layout().totalHeightForWidth(page.width())
        gap = in_window(window.quiet_carriage).top() - in_window(tabs).bottom()
        assert 0 <= gap <= 20, ("偏好页底部留白", index, gap)
    window.confirm_scroll.verticalScrollBar().setValue(window.confirm_scroll.verticalScrollBar().maximum())
    visible_in(window.start_button, window)
    visible_in(window.back_button, window)
    assert window.confirm_scroll.horizontalScrollBar().maximum() == 0
    checked.append("第三步摘要完整，悬浮购物车不挡开始任务，偏好和底部操作可访问")
    window.confirm_scroll.verticalScrollBar().setValue(0)
    save(window, "confirm")
    window.confirm_scroll.verticalScrollBar().setValue(window.confirm_scroll.verticalScrollBar().maximum())
    save(window, "preferences")

    try:
        window._active_operation = SimpleNamespace(mode="task")
        window._operation_mode = "task"
        window.cancel_token = GuiCancelToken()
        window._task_state = "running"
        window._target_timestamp = time.time() + 180
        window._go_to_step(3)
        assert not window.cart_button.isVisible()
        window._set_phase("waiting", "离线演示：等待开售")
        assert window.sale_countdown.isVisible()
        assert not hasattr(window, "timeline")
        cards = (window.query_metric, window.rtt_metric, window.offset_metric)
        assert all(card.isVisible() for card in cards)
        assert max(in_window(card).center().y() for card in cards) - min(in_window(card).center().y() for card in cards) <= 1
        assert window.query_count.isVisible()
        window.current_cart_item.setText("备选 1 / 4\n北京南 → 上海虹桥 · G103 · 二等座")
        window.logs_toggle.setChecked(True)
        for number in range(4):
            window._on_log_message(f"离线界面验证 {number + 1}：未连接 12306，未创建订单。", "INFO")
        QTest.qWait(30)
        assert window.logs_toggle is window.log_view.toggle_button
        assert window.log_view.text.verticalScrollBar().maximum() > 0
        controls = (window.logs_toggle, window.log_view.level, window.log_view.pause,
                    window.log_view.clear_button, window.log_view.copy_button, window.log_view.export_button)
        for control in controls:
            visible_in(control, window.log_view.toolbar)
        assert max(in_window(control).center().y() for control in controls) - min(
            in_window(control).center().y() for control in controls) <= 4
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
        checked.append("第四步隐藏悬浮购物车，日志固定且独立滚动，上一步和停止保持可见")
        window.run_scroll.verticalScrollBar().setValue(0)
        save(window, "waiting")
        expanded_height = window.log_view.height()
        window.logs_toggle.setChecked(False)
        QTest.qWait(20)
        assert not window.log_view.text.isVisible()
        assert window.log_view.toolbar.isVisible()
        assert window.log_view.height() < expanded_height
        for control in controls:
            visible_in(control, window)
        window.logs_toggle.setChecked(True)
        # Each outcome belongs to its own mock run. Serial phase changes would
        # retain success/unknown safety state and leave a false active task.
        for phase, outcome in (
            ("success", OperationOutcome("task", result=0, order_state="success")),
            ("failed", OperationOutcome("task", error="离线演示：连接失败")),
            ("cancelled", OperationOutcome("task", result=130)),
            ("unknown", OperationOutcome("task", result=0, order_state="unknown")),
            ("no_ticket", OperationOutcome("task", result=1)),
        ):
            window._active_operation = SimpleNamespace(mode="task")
            window._operation_mode = "task"
            window.cancel_token = GuiCancelToken()
            window._task_state = "running"
            window._order_state = "safe"
            window._order_succeeded = False
            window._restart_allowed = False
            window._completion_prompt_shown = True
            window._last_candidate_context = {}
            window.order_button.setEnabled(False)
            window._set_phase("waiting", "离线演示：等待开售")
            if phase == "cancelled":
                window.cancel_token.cancel()
            with patch("ticket_app.gui.app.QMessageBox.critical"), patch("ticket_app.gui.app.QMessageBox.information"):
                window._receive_operation_outcome(window._operation_generation, outcome)
            QTest.qWait(10)
            assert window._active_operation is None, phase
            assert window._task_state == phase, (phase, window._task_state)
            assert not window.sale_countdown.isVisible(), phase
            if phase == "cancelled":
                assert window.stop_button.isVisible()
                assert window.stop_button.text() == "继续任务"
            else:
                assert not window.stop_button.isVisible(), phase
            if phase == "no_ticket":
                assert not window.order_button.isVisible()
                window.log_view.clear()
                window._on_log_message("离线演示：已达到停止条件，本次未出票；没有提交真实订单。", "INFO")
        save(window, "finished")
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
