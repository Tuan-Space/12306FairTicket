"""Keyboard-accessible cart shortcut, independent of the scrolling page."""

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFontMetrics, QIcon, QPainter, QPainterPath, QPen, QRegion
from PySide6.QtWidgets import QAbstractButton

from .settings import PROJECT_ROOT


class FloatingCartButton(QAbstractButton):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("floatingCart")
        self.setFixedSize(72, 72)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._count = 0
        self._icon = QIcon(str(PROJECT_ROOT / "assets" / "cart.svg"))
        self.set_count(0)

    @property
    def count(self):
        return self._count

    def set_count(self, count):
        self._count = count
        title = f"购物车（{count} 个备选）"
        self.setAccessibleName(title)
        self.setToolTip(title + " · 查看、排序和修改")
        # A widget mask makes the unpainted corners pass mouse/wheel events
        # to the page below, instead of intercepting a 72px square.
        shape = QPainterPath()
        shape.setFillRule(Qt.FillRule.WindingFill)
        shape.addEllipse(QRectF(3, 9, 58, 58))
        shape.addRoundedRect(self._badge_rect().adjusted(-1, -1, 1, 1), 13, 13)
        self.setMask(QRegion(shape.toFillPolygon().toPolygon(), Qt.FillRule.WindingFill))
        self.update()

    def _badge_rect(self):
        font = self.font()
        font.setBold(True)
        font.setPointSize(9)
        label = str(self._count) if self._count < 100 else "99+"
        width = max(24, QFontMetrics(font).horizontalAdvance(label) + 12)
        return QRectF(70 - width, 2, width, 24)

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.click()
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor("#2367ca" if self.isDown() else "#408bf2" if self.underMouse() else "#347ee8")
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(QRectF(4, 10, 56, 56))
        self._icon.paint(painter, 18, 24, 28, 28)
        font = self.font()
        font.setBold(True)
        font.setPointSize(9)
        painter.setFont(font)
        label = str(self._count) if self._count < 100 else "99+"
        badge = self._badge_rect()
        painter.setBrush(QColor("#ffffff"))
        painter.setPen(QPen(color, 2))
        painter.drawRoundedRect(badge, 12, 12)
        painter.setPen(QColor("#174f9d"))
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, label)
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor("#ffffff"), 2, Qt.PenStyle.DashLine))
            painter.drawEllipse(QRectF(7, 13, 50, 50))
