"""A tray-anchored window that dismisses when the pointer leaves it."""
import time

from PySide6.QtCore import QRect, QTimer, Qt
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtWidgets import QApplication, QFrame, QVBoxLayout


def popup_position(size, anchor, available, screen):
    """Choose the taskbar edge and keep the entire panel on its monitor."""
    margin = 8
    center = anchor.center()
    edge = min(('left', 'right', 'top', 'bottom'), key=lambda side: {
        'left': center.x() - screen.left(), 'right': screen.right() - center.x(),
        'top': center.y() - screen.top(), 'bottom': screen.bottom() - center.y(),
    }[side])
    # Open from the tray corner, rather than from the middle of its icon row.
    x = available.right() - size.width() - margin + 1
    y = available.bottom() - size.height() - margin + 1
    if edge == 'top':
        y = available.top() + margin
    elif edge == 'left':
        x = available.left() + margin
    x = max(available.left() + margin, min(x, available.right() - size.width() - margin + 1))
    y = max(available.top() + margin, min(y, available.bottom() - size.height() - margin + 1))
    return x, y


class TrayPopup(QFrame):
    def __init__(self, owner):
        flags = (Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint |
                 Qt.WindowType.WindowStaysOnTopHint)
        super().__init__(None, flags)
        self.owner = owner
        self.setObjectName('trayPopup')
        self.setWindowTitle('AI Limits')
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setStyleSheet(owner.styleSheet() + '\nQFrame#trayPopup {background: #101624; border: 1px solid #35425d; border-radius: 12px;}')
        self.content_layout = QVBoxLayout(self)
        self.content_layout.setContentsMargins(2, 2, 2, 2)
        self.anchor = QRect()
        self.outside_since = None
        self.grace_until = 0
        self.dismiss_timer = QTimer(self)
        self.dismiss_timer.setInterval(80)
        self.dismiss_timer.timeout.connect(self.check_pointer)

    def position_at_tray(self):
        self.anchor = self.owner.tray.geometry()
        if self.anchor.isEmpty():
            self.anchor = QRect(QCursor.pos(), QCursor.pos())
        screen = QGuiApplication.screenAt(self.anchor.center()) or QApplication.primaryScreen()
        self.content_layout.activate()
        minimum = self.owner.dashboard.minimumSizeHint()
        # A separate vertical layout of the same data; keep all quotas visible.
        width = max(420, minimum.width() + 4)
        height = max(minimum.height() + 4, self.owner.dashboard.sizeHint().height() + 4)
        self.resize(width, height)
        self.move(*popup_position(self.size(), self.anchor, screen.availableGeometry(), screen.geometry()))

    def present(self):
        self.position_at_tray()
        self.outside_since = None
        self.grace_until = time.monotonic() + .4
        self.show()
        self.raise_()
        if self.owner.keep_open:
            self.dismiss_timer.stop()
        else:
            self.dismiss_timer.start()

    def set_keep_open(self, enabled):
        self.outside_since = None
        if not self.isVisible():
            return
        if enabled:
            self.dismiss_timer.stop()
        else:
            self.grace_until = time.monotonic() + .4
            self.dismiss_timer.start()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # A layout can apply its minimum height after a data refresh. Re-anchor
        # using the resulting size, so the panel never grows below the taskbar.
        if self.isVisible() and not self.anchor.isEmpty():
            screen = QGuiApplication.screenAt(self.anchor.center()) or QApplication.primaryScreen()
            self.move(*popup_position(self.size(), self.anchor, screen.availableGeometry(), screen.geometry()))

    def check_pointer(self, point=None, now=None):
        if not self.isVisible() or self.owner.keep_open:
            return
        if QApplication.activePopupWidget():
            self.outside_since = None
            return
        point = QCursor.pos() if point is None else point
        now = time.monotonic() if now is None else now
        if now < self.grace_until:
            return
        inside = self.frameGeometry().adjusted(-3, -3, 3, 3).contains(point)
        over_icon = self.anchor.adjusted(-4, -4, 4, 4).contains(point)
        if inside or over_icon:
            self.outside_since = None
        elif self.outside_since is None:
            self.outside_since = now
        elif now - self.outside_since >= .18:
            self.hide()

    def hideEvent(self, event):
        self.dismiss_timer.stop()
        super().hideEvent(event)

    def closeEvent(self, event):
        event.ignore()
        self.hide()
