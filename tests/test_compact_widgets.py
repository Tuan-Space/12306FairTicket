"""Behavior retained while disclosure text and log controls become compact."""

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from ticket_app.gui.app import _load_stylesheet
from ticket_app.gui.widgets import HelpDetails, LogView, OrderedSeatSelector, PositionPreferences
from ticket_app.logging_utils import redact_text


@pytest.fixture(params=[False, True], ids=["light", "dark"])
def themed_log(qtbot, qapp, request):
    old = qapp.styleSheet()
    qapp.setStyleSheet(_load_stylesheet(qapp, request.param))
    view = LogView()
    view.set_light_palette(not request.param)
    qtbot.addWidget(view)
    view.resize(590, view.sizeHint().height())
    view.show()
    yield view
    qapp.setStyleSheet(old)


def test_logs_have_one_toolbar_and_collapse_only_body(themed_log, qtbot):
    view = themed_log
    controls = [view.toggle_button, view.level, view.pause, view.clear_button, view.copy_button, view.export_button]
    for control in controls:
        assert control.parentWidget() is view.toolbar
        assert view.toolbar.rect().contains(control.geometry())
    assert view.toggle_button.isChecked()
    before = view.text.height()
    view.set_compact_rows(2)
    assert view.text.height() < before
    view.set_compact_rows(3)
    assert view.text.height() == before
    line_height = view.text.fontMetrics().lineSpacing()
    assert 3 * line_height <= view.text.height() <= 3 * line_height + 32
    qtbot.mouseClick(view.toggle_button, Qt.MouseButton.LeftButton)
    assert view.text.isHidden()
    assert view.toolbar.isVisible()
    assert all(control.isVisible() for control in controls)
    assert not view.isHidden()
    qtbot.keyClick(view.toggle_button, Qt.Key.Key_Space)
    assert view.text.isVisible()


def test_direct_buttons_pause_clear_copy_export_and_pause_retains_lines(themed_log):
    view = themed_log
    assert not hasattr(view, "search") and not hasattr(view, "more_button")
    assert [button.text() for button in (view.pause, view.clear_button, view.copy_button, view.export_button)] == [
        "暂停滚动", "清空", "复制", "导出",
    ]
    view.append_line("already shown", "INFO")
    view.pause.setChecked(True)
    view.append_line("arrived while paused", "WARNING")
    assert "arrived while paused" not in view.text.toPlainText()
    assert "arrived while paused" in view.filtered_text()
    view.pause.setChecked(False)
    assert "arrived while paused" in view.text.toPlainText()
    view.clear_button.click()
    assert view.filtered_text() == view.text.toPlainText() == ""


def test_filters_copy_and_export_keep_redacted_content_and_line_bound(themed_log, monkeypatch, tmp_path):
    view = themed_log
    view.MAX_LINES = 4
    lines = [(f"older {i}", "INFO") for i in range(4)]
    lines += [(redact_text("token=private-token passenger=张三", ["张三"]), "ERROR"), ("unrelated", "DEBUG")]
    view.append_lines(lines)
    assert len(view._lines) == 4
    assert "older 0" not in view.filtered_text()
    view.level.setCurrentText("ERROR")
    assert "private-token" not in view.filtered_text()
    assert "张三" not in view.filtered_text()
    assert "token" in view.filtered_text()
    copied = []
    monkeypatch.setattr(QApplication, "clipboard", staticmethod(lambda: SimpleNamespace(setText=copied.append)))
    view.copy_button.click()
    assert copied == [view.filtered_text()]
    target = tmp_path / "filtered.log"
    monkeypatch.setattr("ticket_app.gui.widgets.QFileDialog.getSaveFileName", lambda *_: (str(target), ""))
    view.export_button.click()
    assert target.read_text(encoding="utf-8") == view.filtered_text() + "\n"
    assert view.text.toPlainText() == view.filtered_text()


def test_help_details_is_clickable_keyboard_accessible_plain_text(qtbot):
    help_widget = HelpDetails("选座说明", "<b>前后排</b> 不表示列车行驶方向。")
    qtbot.addWidget(help_widget)
    help_widget.resize(420, 120)
    help_widget.show()
    assert help_widget.details_label.isHidden()
    qtbot.mouseClick(help_widget.button, Qt.MouseButton.LeftButton)
    assert help_widget.details_label.isVisible()
    assert help_widget.details_label.textFormat() == Qt.TextFormat.PlainText
    assert "<b>" in help_widget.details_label.text()
    qtbot.keyClick(help_widget.button, Qt.Key.Key_Space)
    assert help_widget.details_label.isHidden()
    assert "选座说明" in help_widget.button.accessibleName()


def test_preferences_keep_values_and_activation_with_direct_seat_explanation(qtbot):
    prefs = PositionPreferences()
    qtbot.addWidget(prefs)
    prefs.adapt_to_seats(["二等座", "二等卧"])
    prefs.seats.set_positions(["1A"])
    prefs.berths.set_values({"lower": 1})
    assert not prefs.seats.guide.isHidden()
    assert not hasattr(prefs.seats, "help_details")
    assert "该席别内自动分配" in prefs.seats.guide.text()
    assert prefs.berths.help_details.details_label.isHidden()
    prefs.berths.help_details.button.click()
    assert not prefs.berths.help_details.details_label.isHidden()
    prefs.adapt_to_seats(["硬座"])
    assert prefs.seats.positions() == ["1A"]
    assert prefs.berths.values()["lower"] == 1
    assert prefs.seats.clear_button.isEnabled()
    assert prefs.berths.clear_button.isEnabled()
    assert all(not button.isEnabled() for button in prefs.seats.buttons.values())
    assert all(not spin.isEnabled() for spin in prefs.berths.spins.values())
    prefs.adapt_to_seats(["二等座", "二等卧"])
    assert prefs.seats.positions() == ["1A"]
    assert prefs.berths.values()["lower"] == 1
    prefs.seats.clear_button.click()
    prefs.berths.clear_button.click()
    assert prefs.seats.positions() == []
    assert prefs.berths.values() == {"lower": 0, "middle": 0, "upper": 0}


def test_seat_click_ranks_are_the_only_selection_order_copy(qtbot):
    selector = OrderedSeatSelector()
    qtbot.addWidget(selector)
    selector.set_selected_seats(["二等卧", "二等座"])
    assert not hasattr(selector, "order_summary")
    assert selector.checkboxes["二等卧"].text() == "1 · 二等卧"
    assert selector.checkboxes["二等座"].text() == "2 · 二等座"
