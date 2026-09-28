"""Cart scope dropdown rendering and interaction in both desktop themes."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QStyle, QStyleOptionComboBox

from ticket_app.gui.app import ASSET_DIR, _load_stylesheet
from ticket_app.gui.cart_widgets import CartDialog


@pytest.fixture(params=[False, True], ids=["light", "dark"])
def cart_scope_dropdown(qtbot, qapp, request):
    previous_style = qapp.styleSheet()
    qapp.setStyleSheet(_load_stylesheet(qapp, request.param))
    dialog = CartDialog([{
        "from_station": "北京南", "to_station": "上海虹桥",
        "train_scope": "specific", "train_code": "G101", "seat_type": "二等座",
    }])
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.list.setCurrentRow(0)
    dialog.edit_current()
    combo = dialog.train_scope
    qapp.processEvents()
    yield combo, request.param
    combo.hidePopup()
    qapp.setStyleSheet(previous_style)


def _arrow_rect(combo):
    option = QStyleOptionComboBox()
    combo.initStyleOption(option)
    return combo.style().subControlRect(
        QStyle.ComplexControl.CC_ComboBox,
        option,
        QStyle.SubControl.SC_ComboBoxArrow,
        combo,
    )


def test_cart_scope_arrow_asset_is_resolved_and_visible(cart_scope_dropdown, qapp):
    combo, dark = cart_scope_dropdown
    icon = ASSET_DIR / f"chevron-down-{'dark' if dark else 'light'}.svg"
    renderer = QSvgRenderer(str(icon))
    assert renderer.isValid()
    assert icon.as_posix() in qapp.styleSheet()
    assert "url(assets/chevron-down-" not in qapp.styleSheet()

    arrow_rect = _arrow_rect(combo)
    assert arrow_rect.width() == 24
    assert arrow_rect.right() >= combo.width() - 4
    # Inspect the rendered center of the hit area, excluding the field border.
    # The chevron must contrast with the adjacent input background in each theme.
    shot = combo.grab().toImage()
    ratio = shot.devicePixelRatio()
    center = arrow_rect.center()
    background = shot.pixelColor(round((center.x() - 9) * ratio), round(center.y() * ratio))
    contrast = []
    for y in range(center.y() - 5, center.y() + 6):
        for x in range(center.x() - 6, center.x() + 7):
            pixel = shot.pixelColor(round(x * ratio), round(y * ratio))
            contrast.append(abs(pixel.lightness() - background.lightness()))
    assert max(contrast) > 60


@pytest.mark.parametrize("target", ["arrow", "body"])
def test_cart_scope_popup_opens_from_arrow_or_body(cart_scope_dropdown, qtbot, target):
    combo, _dark = cart_scope_dropdown
    point = _arrow_rect(combo).center() if target == "arrow" else QPoint(40, combo.height() // 2)
    qtbot.mouseClick(combo, Qt.MouseButton.LeftButton, pos=point)
    qtbot.waitUntil(combo.view().isVisible)
    # Qt suppresses a mouse release immediately after showing the popup so that
    # opening the menu cannot accidentally select an entry on the same gesture.
    qtbot.wait(450)
    index = combo.model().index(1, 0)
    qtbot.mouseClick(
        combo.view().viewport(), Qt.MouseButton.LeftButton,
        pos=combo.view().visualRect(index).center(),
    )
    assert combo.currentData() == "all"
    assert not combo.view().isVisible()


def test_cart_scope_dropdown_retains_keyboard_selection(cart_scope_dropdown, qtbot):
    combo, _dark = cart_scope_dropdown
    combo.setFocus()
    qtbot.keyClick(combo, Qt.Key.Key_Down, modifier=Qt.KeyboardModifier.AltModifier)
    qtbot.waitUntil(combo.view().isVisible)
    qtbot.keyClick(combo.view(), Qt.Key.Key_Down)
    qtbot.keyClick(combo.view(), Qt.Key.Key_Return)
    assert combo.currentData() == "all"
    assert not combo.view().isVisible()
