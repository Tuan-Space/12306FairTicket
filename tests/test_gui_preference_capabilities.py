"""Retained seat/berth preferences activate only for cart seat capabilities."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt

from ticket_app.gui.widgets import PositionPreferences


@pytest.mark.parametrize("seat_types", [["硬座"], ["软座"], ["无座"], []])
def test_ordinary_seats_hide_inactive_map_and_preserve_clearable_draft(qtbot, seat_types):
    preferences = PositionPreferences()
    qtbot.addWidget(preferences)
    preferences.seats.set_positions(["1A", "1F"])
    preferences.adapt_to_seats(seat_types)
    assert preferences.seats.grid.isHidden()
    assert preferences.seats.guide.isHidden()
    assert not hasattr(preferences.seats, "help_details")
    assert not hasattr(preferences.seats, "fallback")
    assert preferences.seats.layout().alignment() & Qt.AlignmentFlag.AlignTop
    assert not preferences.seats.inactive_hint.isHidden()
    assert preferences.seats.positions() == ["1A", "1F"]
    assert "1A、1F" in preferences.seats.saved_summary.text()
    assert all(not button.isEnabled() for button in preferences.seats.buttons.values())
    assert "未启用" in preferences.tabs.tabText(0)
    preferences.seats.clear_button.click()
    assert preferences.seats.positions() == []


@pytest.mark.parametrize(
    "seat_types,letters",
    [(["二等座"], "ABCDF"), (["一等座"], "ACDF"), (["商务座"], "ACF"),
     (["特等座"], "ACF"), (["商务座", "二等座"], "ABCDF")],
)
def test_seat_map_uses_actual_seat_code_capabilities(qtbot, seat_types, letters):
    preferences = PositionPreferences()
    qtbot.addWidget(preferences)
    preferences.adapt_to_seats(seat_types)
    assert not preferences.seats.grid.isHidden()
    assert not preferences.seats.guide.isHidden()
    assert "该席别内自动分配" in preferences.seats.guide.text()
    assert not hasattr(preferences.seats, "help_details")
    assert {token for token, button in preferences.seats.buttons.items() if button.isEnabled()} == {
        f"{row}{letter}" for row in (1, 2) for letter in letters
    }
    if "商务座" in seat_types:
        assert "C 位是否提供" in preferences.seats.guide.text()


def test_inactive_berth_and_seat_drafts_restore_on_matching_selection(qtbot):
    preferences = PositionPreferences()
    qtbot.addWidget(preferences)
    preferences.seats.set_positions(["1B"])
    preferences.berths.set_values({"lower": 2, "middle": 1})
    preferences.adapt_to_seats(["硬座"])
    assert not preferences.berths.spins["lower"].isEnabled()
    assert preferences.berths.clear_button.isEnabled()
    assert "未启用" in preferences.tabs.tabText(1)
    preferences.adapt_to_seats(["硬卧", "二等座"])
    assert preferences.berths.spins["lower"].isEnabled()
    assert preferences.berths.values() == {"lower": 2, "middle": 1, "upper": 0}
    assert preferences.seats.positions() == ["1B"]
    assert preferences.seats.buttons["1B"].isEnabled()
    preferences.adapt_to_seats(["商务座"])
    assert preferences.seats.positions() == ["1B"]
    assert "不适用于当前席别" in preferences.seats.saved_summary.text()
    assert not preferences.seats.buttons["1B"].isEnabled()
    preferences.berths.clear_button.click()
    assert not any(preferences.berths.values().values())
