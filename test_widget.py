import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'

import time
import unittest
from unittest.mock import Mock, call, patch

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QScrollArea

from app import (Monitor, SegmentedProgressBar, cooldown_percent, countdown,
                 set_sleep_prevention, window_duration_seconds)
from providers import Quota, Snapshot

APPLICATION = QApplication.instance() or QApplication([])


class WidgetTests(unittest.TestCase):
    def setUp(self):
        self.window = Monitor(testing=True)
        self.window.timer.stop()
        self.window.due = {'codex': float('inf'), 'antigravity': float('inf')}

    def tearDown(self):
        self.window.exiting = True
        self.window.close()
        self.window.deleteLater()

    def test_refresh_failure_keeps_previous_values_with_stale_label(self):
        snapshot = Snapshot('codex', 'Codex', [Quota('Общая', '5 часов', 12)], updated=time.time())
        self.window.received('codex', [snapshot])
        self.window.received('codex', [Snapshot('codex', 'Codex', error='Нет сети')])
        self.assertEqual(self.window.snapshots['codex'].quotas[0].remaining, 12)
        self.assertEqual(self.window.snapshots['codex'].updated, snapshot.updated)
        self.assertTrue(any('Данные устарели' in label.text() for label in self.window.findChildren(QLabel)))

    def test_compact_popup_stays_on_top(self):
        self.assertTrue(self.window.popup.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)

    def test_all_eight_live_style_quotas_fit_without_scroll(self):
        snapshots = [
            Snapshot('codex', 'Codex', [Quota('Общая квота Codex', w, 50, time.time()+3600) for w in ['5 часов', 'Неделя']]),
            Snapshot('antigravity', 'Antigravity', [Quota(g, w, 50, time.time()+3600) for g in ['Claude', 'Gemini Argon', 'Gemini Flash'] for w in ['5 часов', 'Неделя']]),
        ]
        self.window.snapshots = {s.key:s for s in snapshots}
        self.window.rebuild()
        self.window.resize(max(720,self.window.minimumWidth()), max(590,self.window.minimumHeight()))
        APPLICATION.processEvents()
        self.assertFalse(self.window.findChildren(QScrollArea))
        values = [v for v in self.window.findChildren(QLabel, 'quotaValue') if v.isVisible()]
        self.assertEqual(len(values), 8)
        current_cards = [self.window.body_layout.itemAt(i).widget()
                         for i in range(self.window.body_layout.count())
                         if self.window.body_layout.itemAt(i).widget() is not None]
        panels = [panel for card in current_cards if card.objectName() == 'card'
                  for panel in card.findChildren(QFrame, 'quotaGroup')]
        self.assertEqual(len(panels), 4)
        for panel in panels:
            self.assertEqual(panel.layout().spacing(), 16)
            windows = [panel.layout().itemAt(i).widget().findChildren(QLabel, 'quotaWindow')[0].text()
                       for i in range(2)]
            self.assertEqual(windows, ['Нед.', '5 ч.'])
        viewport = self.window.centralWidget().rect()
        for value in values:
            location = value.mapTo(self.window.centralWidget(), value.rect().bottomRight())
            self.assertTrue(viewport.contains(location))
        self.assertLessEqual(self.window.height(), 640)

    def test_expired_reset_does_not_claim_refilled_quota(self):
        self.assertIn('ждём новое окно', countdown(100, now=200))
        self.assertIn('Сброс через', countdown(1000, now=200))

    def test_cooldown_overlay_tracks_time_left_in_each_window(self):
        now = 1000
        self.assertEqual(window_duration_seconds('5 часов'), 5 * 60 * 60)
        self.assertEqual(window_duration_seconds('Неделя'), 7 * 24 * 60 * 60)
        self.assertEqual(window_duration_seconds('30 мин.'), 30 * 60)
        self.assertAlmostEqual(cooldown_percent('5 часов', now + 9000, now), 50)
        self.assertAlmostEqual(cooldown_percent('Неделя', now + 3.5 * 24 * 60 * 60, now), 50)
        self.assertEqual(cooldown_percent('5 часов', now - 1, now), 0)
        self.assertEqual(cooldown_percent('неизвестное окно', now + 100, now), 0)

    def test_quota_fill_and_translucent_cooldown_start_from_the_right(self):
        remaining_color = QColor('#73a75a')
        quota_only = SegmentedProgressBar(20, remaining_color.name())
        quota_only.resize(104, 5)
        quota_only.show()
        APPLICATION.processEvents()
        quota_image = quota_only.grab().toImage()
        self.assertEqual(quota_image.pixelColor(101, 2), remaining_color)
        self.assertEqual(quota_image.pixelColor(4, 2), QColor('#303d54'))

        cooldown = SegmentedProgressBar(20, remaining_color.name(), '5 часов', time.time() + 9000)
        cooldown.resize(104, 5)
        cooldown.show()
        APPLICATION.processEvents()
        cooldown_image = cooldown.grab().toImage()
        self.assertAlmostEqual(cooldown.cooldown, 50, delta=0.1)
        self.assertNotEqual(cooldown_image.pixelColor(101, 2), remaining_color)
        self.assertEqual(cooldown_image.pixelColor(4, 2), QColor('#303d54'))
        quota_only.close()
        cooldown.close()

    def test_sleep_prevention_sets_and_clears_only_system_idle_request(self):
        kernel32 = Mock()
        kernel32.SetThreadExecutionState.return_value = 1
        with patch('app.os.name', 'nt'), patch('app.ctypes.WinDLL', return_value=kernel32, create=True):
            self.assertTrue(set_sleep_prevention(True))
            self.assertTrue(set_sleep_prevention(False))
        self.assertEqual(kernel32.SetThreadExecutionState.call_args_list,
                         [call(0x80000001), call(0x80000000)])

    def test_sleep_prevention_toggle_is_checkable_and_persisted(self):
        self.assertTrue(self.window.sleep_prevention.isCheckable())
        with patch('app.set_sleep_prevention') as request_sleep_state, \
             patch.object(self.window, 'preferences_save') as save_preferences:
            self.window.set_keep_awake(True)
        self.assertTrue(self.window.keep_awake)
        self.assertTrue(self.window.sleep_prevention.isChecked())
        self.assertTrue(self.window.preferences['keep_awake'])
        request_sleep_state.assert_not_called()
        save_preferences.assert_called_once()


if __name__ == '__main__':
    unittest.main()

