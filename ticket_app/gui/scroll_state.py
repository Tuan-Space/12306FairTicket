"""Keep background UI updates from moving the reader's current viewport."""

from functools import wraps

from PySide6.QtCore import QTimer


def preserve_reading_position(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        if getattr(self, "_preserving_scroll", False) or not hasattr(self, "steps"):
            return method(self, *args, **kwargs)
        step = self.current_step
        page = self.steps.currentWidget()
        if page is None or not hasattr(page, "verticalScrollBar"):
            return method(self, *args, **kwargs)
        bar = page.verticalScrollBar()
        position = bar.value()
        self._scroll_update = getattr(self, "_scroll_update", 0) + 1
        update = self._scroll_update
        self._preserving_scroll = True
        try:
            return method(self, *args, **kwargs)
        finally:
            self._preserving_scroll = False
            if self.current_step == step:
                bar.setValue(position)
                # Layout changes can be delivered after the slot. Do not undo
                # later navigation, another update, or the user's own scroll.
                settled = bar.value()

                def restore():
                    if (not getattr(self, "_session_closed", False)
                            and self.current_step == step and self._scroll_update == update
                            and bar.value() == settled):
                        bar.setValue(position)

                QTimer.singleShot(0, self, restore)
    return wrapped
