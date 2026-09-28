"""Offline layout regressions; native DPI screenshots have a separate runner."""

import pytest

from test_gui_app import main_window  # noqa: F401
from scripts.check_cart_layout import inspect_layout, prepare_demo
from ticket_app.gui.app import _load_stylesheet
from ticket_app.cart import MIGRATION_ISSUES, MIGRATION_NOTES
from ticket_app.gui.cart_widgets import CartDialog


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
@pytest.mark.parametrize("size", [(750, 880), (640, 480)])
def test_cart_steps_and_dialog_keep_controls_visible(main_window, qtbot, qapp, dark, size):
    previous = qapp.styleSheet()
    try:
        qapp.setStyleSheet(_load_stylesheet(qapp, dark))
        main_window.resize(*size)
        main_window.show()
        qtbot.wait(20)
        prepare_demo(main_window)
        assert len(inspect_layout(main_window)) == 4
    finally:
        qapp.setStyleSheet(previous)


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_long_migration_notes_do_not_push_editor_beyond_small_screen(qtbot, qapp, dark):
    previous = qapp.styleSheet()
    try:
        qapp.setStyleSheet(_load_stylesheet(qapp, dark))
        dialog = CartDialog([
            {"from_station": "北京南", "to_station": "上海虹桥", "train_scope": "all",
             "train_code": "", "seat_type": "二等座"},
        ], migration_warnings=list(MIGRATION_ISSUES.values()) + list(MIGRATION_NOTES.values()))
        qtbot.addWidget(dialog)
        dialog.show()
        dialog.edit_current()
        dialog.resize(640, 405)
        qtbot.wait(20)
        assert dialog.height() <= 405
        assert dialog.migration_scroll.verticalScrollBar().maximum() > 0
        assert dialog.edit_title.isVisible()
        assert dialog.done_button.mapTo(dialog, dialog.done_button.rect().bottomRight()).y() < 405
    finally:
        qapp.setStyleSheet(previous)
