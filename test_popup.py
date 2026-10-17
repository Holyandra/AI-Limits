import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'

import time
import unittest

from PySide6.QtCore import QPoint, QRect, QSize
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QSystemTrayIcon

from app import Monitor, TITLES, reset_markup
from providers import Quota
from tray_popup import popup_position

APPLICATION = QApplication.instance() or QApplication([])


class PopupTests(unittest.TestCase):
    def setUp(self):
        self.window = Monitor(testing=True)
        self.window.timer.stop()
        self.window.due = {'codex': float('inf'), 'antigravity': float('inf')}
        self.window.last_attempt = time.monotonic()

    def tearDown(self):
        self.window.single_click_timer.stop()
        self.window.popup.hide()
        self.window.popup.deleteLater()
        self.window.exiting = True
        self.window.close()
        self.window.deleteLater()

    def test_one_click_opens_popup_and_pointer_exit_hides_it(self):
        # A previous pinned state must not make a single click sticky.
        self.window.keep_open = True
        self.window.tray_clicked(QSystemTrayIcon.ActivationReason.Trigger)
        self.assertTrue(self.window.single_click_timer.isActive())
        self.window.single_click_timer.stop()
        self.window.show_transient_popup()
        popup = self.window.popup
        self.assertTrue(popup.isVisible())
        self.assertFalse(self.window.keep_open)
        self.assertFalse(self.window.isVisible())
        self.assertIsNone(self.window.centralWidget())
        self.assertIs(self.window.dashboard.parentWidget(), popup)
        popup.grace_until = 0
        popup.check_pointer(popup.frameGeometry().center(), now=10)
        self.assertTrue(popup.isVisible())
        outside = QPoint(-10000, -10000)
        popup.check_pointer(outside, now=11)
        popup.check_pointer(outside, now=11.2)
        self.assertFalse(popup.isVisible())

    def test_double_click_pins_popup_against_pointer_exit(self):
        self.window.tray_clicked(QSystemTrayIcon.ActivationReason.Trigger)
        self.window.tray_clicked(QSystemTrayIcon.ActivationReason.DoubleClick)
        # Some platforms also deliver a final single-click notification.
        self.window.tray_clicked(QSystemTrayIcon.ActivationReason.Trigger)
        QTest.qWait(QApplication.doubleClickInterval() + 20)
        self.assertFalse(self.window.single_click_timer.isActive())
        self.assertTrue(self.window.popup.isVisible())
        self.assertTrue(self.window.keep_open)
        popup = self.window.popup
        popup.grace_until = 0
        outside = QPoint(-10000, -10000)
        popup.check_pointer(outside, now=10)
        popup.check_pointer(outside, now=11)
        self.assertTrue(popup.isVisible())

    def test_refresh_while_popup_open_keeps_dashboard_attached(self):
        self.window.toggle_popup()
        self.window.rebuild()
        APPLICATION.processEvents()
        self.assertTrue(self.window.popup.isVisible())
        self.assertIs(self.window.dashboard.parentWidget(), self.window.popup)
        self.assertTrue(self.window.popup.screen().availableGeometry().contains(self.window.popup.frameGeometry()))
        self.window.show_window(center=True)
        APPLICATION.processEvents()
        self.assertTrue(self.window.popup.isVisible())
        self.assertTrue(self.window.keep_open)
        self.assertIs(self.window.dashboard.parentWidget(), self.window.popup)
        values = self.window.dashboard.findChildren(QLabel, 'quotaValue')
        self.assertGreater(len(values), 0)

    def test_provider_order_and_emphasized_time(self):
        self.assertEqual(list(TITLES), ['antigravity', 'codex'])
        markup = reset_markup(Quota('Gemini Flash', '5 часов', 50, time.time() + 3600))
        self.assertIn('font-weight:700', markup)
        self.assertIn('color:#f4f6ff', markup)
        self.assertIn('color:#9caac6', markup)
        self.assertIn('font-size:12px', markup)
        self.assertLess(markup.index('font-size:12px'), markup.index('font-weight:700'))

    def test_vertical_panel_fits_all_quotas(self):
        self.window.toggle_popup()
        APPLICATION.processEvents()
        popup = self.window.popup
        self.assertLess(popup.width(), popup.height())
        self.assertLessEqual(popup.width(), 480)
        values = [label for label in self.window.dashboard.findChildren(QLabel, 'quotaValue') if label.isVisible()]
        self.assertEqual(len(values), 8)
        for value in values:
            self.assertTrue(popup.rect().contains(value.mapTo(popup, value.rect().bottomRight())))

    def test_bottom_tray_panel_uses_the_screen_corner(self):
        screen = QRect(-1920, 0, 1920, 1080)
        available = QRect(-1920, 0, 1920, 1032)
        size = QSize(420, 756)
        x, y = popup_position(size, QRect(-900,1040,24,24), available, screen)
        self.assertEqual(x + size.width(), available.right() - 8 + 1)
        self.assertEqual(y + size.height(), available.bottom() - 8 + 1)

    def test_popup_stays_on_monitor_for_each_taskbar_edge(self):
        screen = QRect(-1920, 0, 1920, 1080)
        available = QRect(-1920, 0, 1920, 1032)
        size = QSize(780, 490)
        for anchor in [QRect(-60,1040,24,24), QRect(-1915,500,24,24), QRect(-30,500,24,24), QRect(-900,0,24,24)]:
            with self.subTest(anchor=anchor):
                x, y = popup_position(size, anchor, available, screen)
                self.assertTrue(available.contains(QRect(x,y,size.width(),size.height())))


if __name__ == '__main__':
    unittest.main()
