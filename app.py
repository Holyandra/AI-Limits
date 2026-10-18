from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from html import escape
from datetime import datetime, timedelta, timezone
from pathlib import Path

from functools import lru_cache

from PySide6.QtCore import QObject, QPointF, QRectF, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QColor, QCursor, QFont, QFontDatabase, QIcon, QPainter, QPen, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout,
                              QLabel, QMainWindow, QMenu, QPushButton,
                              QSizePolicy, QSystemTrayIcon, QVBoxLayout, QWidget)

from providers import AntigravityProvider, CodexProvider, Quota, Snapshot
from tray_popup import TrayPopup

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'QuotaWidget'
ZONE = timezone(timedelta(hours=7), 'Новосибирск')
MONTHS_RU = ('янв', 'фев', 'мар', 'апр', 'май', 'июн',
             'июл', 'авг', 'сен', 'окт', 'ноя', 'дек')
TITLES = {'antigravity': 'Antigravity', 'codex': 'Codex'}
UPDATE_INTERVAL_SECONDS = 30
BRAND_MARKS = {
    'antigravity': 'antigravity-product.png',
    'claude': 'claude.svg',
    'gemini': 'gemini.png',
    'argon': 'gemini.png',
    'codex': 'codex-color.svg',
    'chatgpt': 'openai-knot-white.png',
    'gpt': 'openai-knot-white.png',
}


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def save_json(path, data):
    DATA.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def set_sleep_prevention(enabled):
    """Keep Windows from entering idle sleep without keeping the display on."""
    if os.name != 'nt':
        return False
    try:
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        set_execution_state = kernel32.SetThreadExecutionState
        set_execution_state.argtypes = [ctypes.c_uint32]
        set_execution_state.restype = ctypes.c_uint32
        flags = 0x80000000 | (0x00000001 if enabled else 0)  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        return bool(set_execution_state(flags))
    except (AttributeError, OSError):
        return False


def number(value):
    return str(math.floor(value))


def reset_date(reset):
    value = datetime.fromtimestamp(reset, ZONE)
    return f'{value.day:02d} {MONTHS_RU[value.month - 1]} {value:%H:%M}'


def countdown(reset, now=None):
    if reset is None:
        return 'Срок сброса не указан'
    now = time.time() if now is None else now
    seconds = int(reset - now)
    date = reset_date(reset)
    if seconds <= 0:
        return f'Срок {date} прошёл · ждём новое окно'
    minutes = (seconds + 59) // 60
    days, hours, mins = minutes // 1440, (minutes % 1440) // 60, minutes % 60
    duration = f'{days} д. {hours} ч.' if days else (f'{hours} ч. {mins} мин.' if hours else f'{mins} мин.')
    return f'Сброс через {duration} · {date}'


def icon():
    """Transparent, bright chart-only mark for the tray and panel header."""
    pix = QPixmap(64, 64)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor('#f3fdff'))
    p.drawRoundedRect(9, 35, 10, 17, 3, 3)
    p.setBrush(QColor('#7deaff'))
    p.drawRoundedRect(25, 25, 10, 27, 3, 3)
    p.setBrush(QColor('#b8e9ff'))
    p.drawRoundedRect(41, 15, 10, 37, 3, 3)
    p.setPen(QPen(QColor('#eef9ff'), 3.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.drawLine(7, 55, 55, 55)
    p.end()
    return QIcon(pix)


@lru_cache(maxsize=32)
def glyph_pixmap(kind, size=16):
    """Render the original provider/model mark into a compact transparent icon."""
    asset = BRAND_MARKS.get(kind)
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    if not asset:
        return pix
    path = ROOT / 'assets' / 'brands' / asset
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    if path.suffix.lower() == '.svg':
        renderer = QSvgRenderer(str(path))
        if renderer.isValid():
            renderer.render(p, QRectF(0, 0, size, size))
    else:
        source = QPixmap(str(path))
        if not source.isNull():
            scaled = source.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation)
            p.drawPixmap((size - scaled.width()) // 2, (size - scaled.height()) // 2, scaled)
    p.end()
    return pix


def short_reset(reset, now=None):
    if reset is None:
        return 'Сброс не указан'
    now = time.time() if now is None else now
    if reset <= now:
        return 'Ждём новое окно'
    minutes = max(1, int((reset - now + 59) // 60))
    days, hours, mins = minutes // 1440, minutes % 1440 // 60, minutes % 60
    duration = f'{days}д {hours}ч' if days else f'{hours}ч {mins}м' if hours else f'{mins}м'
    return duration + ' · ' + reset_date(reset)


def reset_markup(quota, now=None):
    reset = quota.reset
    if reset is None:
        return '<span style="color:#9caac6;font-size:11px">.</span>'
    duration, separator, date = short_reset(reset, now).partition(' · ')
    result = ''
    if separator:
        if quota.window == '5 часов':
            date = datetime.fromtimestamp(reset, ZONE).strftime('%H:%M')
        result = '<span style="color:#9caac6;font-size:12px">' + escape(date) + '</span>&nbsp; '
    return result + '<span style="color:#f4f6ff;font-weight:700">' + escape(duration) + '</span>'


def window_duration_seconds(window):
    named = {'5 часов': 5 * 60 * 60, 'Неделя': 7 * 24 * 60 * 60}
    if window in named:
        return named[window]
    match = re.fullmatch(r'(\d+)\s+(дн\.|ч\.|мин\.)', window.strip())
    if not match:
        return 0
    amount = int(match.group(1))
    unit_seconds = {'дн.': 24 * 60 * 60, 'ч.': 60 * 60, 'мин.': 60}[match.group(2)]
    return amount * unit_seconds


def cooldown_percent(window, reset, now=None):
    duration = window_duration_seconds(window)
    if reset is None or duration <= 0:
        return 0.0
    now = time.time() if now is None else now
    seconds_left = max(0.0, min(float(duration), float(reset) - now))
    return seconds_left * 100 / duration


class SegmentedProgressBar(QWidget):
    """Draw reversed quota fill with a translucent, segmented reset countdown."""
    def __init__(self, remaining, color, window=None, reset=None, parent=None):
        super().__init__(parent)
        self.remaining = max(0.0, min(100.0, float(remaining or 0)))
        self.fill_color = QColor(color)
        self.window = window
        self.reset = reset
        self.cooldown = cooldown_percent(window or '', reset)
        self.setFixedHeight(5)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def update_cooldown(self, now=None):
        self.cooldown = cooldown_percent(self.window or '', self.reset, now)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        track_color = QColor('#303d54')
        gap = 2.0
        segment_width = (self.width() - gap * 4) / 5
        height = float(self.height())
        radius = 2.0
        for part in range(5):
            x = (4 - part) * (segment_width + gap)
            track = QRectF(x, 0, segment_width, height)
            painter.setBrush(track_color)
            painter.drawRoundedRect(track, radius, radius)
        def draw_fill(percent, color):
            for part in range(5):
                x = (4 - part) * (segment_width + gap)
                portion = max(0.0, min(1.0, percent / 20 - part))
                if portion:
                    fill_width = segment_width * portion
                    filled = QRectF(x + segment_width - fill_width, 0, fill_width, height)
                    painter.setBrush(color)
                    painter.drawRoundedRect(filled, min(radius, fill_width / 2), radius)

        draw_fill(self.remaining, self.fill_color)
        draw_fill(self.cooldown, QColor(233, 245, 255, 100))
        painter.end()


class RefreshIndicator(QWidget):
    """A small rotating pair of circular arrows shown while fetching quotas."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.angle = 0
        self.spinning = False
        self.setFixedSize(12, 12)
        self.setVisible(False)
        self.timer = QTimer(self)
        self.timer.setInterval(60)
        self.timer.timeout.connect(self.advance)

    def set_spinning(self, spinning):
        spinning = bool(spinning)
        if self.spinning == spinning:
            return
        self.spinning = spinning
        self.setVisible(spinning)
        if spinning:
            self.timer.start()
        else:
            self.timer.stop()

    def advance(self):
        self.angle = (self.angle + 18) % 360
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.translate(self.width() / 2, self.height() / 2)
        painter.rotate(self.angle)
        painter.translate(-self.width() / 2, -self.height() / 2)
        color = QColor('#aebbd1')
        pen = QPen(color, 1.1, Qt.PenStyle.SolidLine,
                   Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        rect = QRectF(2, 2, 8, 8)
        arrows = ((30, 130), (210, 130))
        radius = 4
        center_x = center_y = 6
        for start, span in arrows:
            end = start + span
            painter.drawArc(rect, start * 16, span * 16)
            radians = math.radians(end)
            tip_x = center_x + radius * math.cos(radians)
            tip_y = center_y - radius * math.sin(radians)
            tangent_x = -math.sin(radians)
            tangent_y = -math.cos(radians)
            base_x = tip_x - tangent_x * 1.7
            base_y = tip_y - tangent_y * 1.7
            normal_x, normal_y = -tangent_y, tangent_x
            for side in (-1, 1):
                point = QPointF(base_x + normal_x * side * 1.05,
                                base_y + normal_y * side * 1.05)
                painter.drawLine(QPointF(tip_x, tip_y), point)
        painter.end()


class Bus(QObject):
    ready = Signal(str, object)


class Monitor(QMainWindow):
    def __init__(self, start_tray=False, testing=False):
        super().__init__()
        self.testing = testing
        self.exiting = False
        self.preferences = load_json(DATA / 'settings.json', {})
        self.interval = UPDATE_INTERVAL_SECONDS
        self.keep_open = bool(self.preferences.get('keep_open', False))
        self.keep_awake = bool(self.preferences.get('keep_awake', False))
        self.snapshots = {}
        self.session_success = set()
        for key, entry in load_json(DATA / 'cache.json', {}).items():
            if key not in TITLES:
                continue
            try:
                snapshot = Snapshot.from_dict(entry)
                snapshot.title = TITLES[key]
                snapshot.error = 'Сохранённые данные. Проверяем актуальность…'
                self.snapshots[key] = snapshot
            except (TypeError, ValueError, KeyError):
                pass
        self.codex = CodexProvider()
        self.antigravity = AntigravityProvider()
        self.bus = Bus()
        self.bus.ready.connect(self.received)
        self.running = set()
        self.failures = {'codex': 0, 'antigravity': 0}
        self.due = {'codex': 0., 'antigravity': 0.}
        self.last_attempt = 0.
        self.reset_labels = []
        self.cooldown_bars = []
        self.age_labels = []
        self.popup_mode = False
        self.setWindowTitle('AI Limits')
        self.setWindowIcon(icon())
        self.resize(980, 640)
        self.setMinimumSize(950, 620)
        self.setStyleSheet('''
            QMainWindow, QWidget#root, QWidget#scrollContent { background: #101624; color: #e8edf9; }
            QWidget { font-family: 'Segoe UI'; font-size: 14px; color: #e8edf9; }
            QLabel { background: transparent; }
            QLabel#heading { font-size: 25px; font-weight: 700; }
            QLabel#muted { color: #9caac6; font-size: 13px; }
            QLabel#error { color: #f3b67d; font-size: 13px; }
            QLabel#section { font-size: 17px; font-weight: 700; }
            QLabel#providerPlan { color: #9caac6; font-size: 12px; font-weight: 600; }
            QLabel#groupTitle { color: #dce7f7; font-size: 13px; font-weight: 400; }
            QLabel#footer { color: #9caac6; font-size: 10px; }
            QFrame#card { background: #1a2336; border: 1px solid #2b3650; border-radius: 12px; }
            QFrame#quotaGroup { background: #26334a; border: 1px solid #3b4b67; border-radius: 9px; }
            QPushButton { background: #25314b; border: 1px solid #34415e; border-radius: 6px; padding: 6px 10px; font-size: 13px; }
            QPushButton:hover { background: #354466; }
            QPushButton:checked { background: #425694; }
            QPushButton:disabled { color: #8591a7; }
            QProgressBar { background: #303d54; border: 0; border-radius: 3px; min-height: 6px; max-height: 6px; }
            QProgressBar::chunk { background: #69d8b7; border-radius: 3px; }
            QMenu { background: #1a2336; border: 1px solid #34415e; padding: 5px; }
            QMenu::item { padding: 7px 16px; }
            QMenu::item:selected { background: #354466; }
        ''')
        root = QWidget()
        root.setObjectName('root')
        outer = QVBoxLayout(root)
        outer.setContentsMargins(12, 10, 12, 8)
        outer.setSpacing(6)
        self.popup_header = QWidget()
        popup_header = QVBoxLayout(self.popup_header)
        popup_header.setContentsMargins(0, 0, 0, 0)
        popup_header.setSpacing(5)
        brand_row = QHBoxLayout()
        brand_row.setContentsMargins(0, 0, 0, 0)
        brand = QLabel()
        brand.setPixmap(icon().pixmap(34, 34))
        brand.setFixedSize(34, 34)
        brand_row.addWidget(brand)
        popup_title = QLabel('AI Limits')
        popup_title.setObjectName('heading')
        brand_row.addWidget(popup_title)
        brand_row.addStretch()
        controls_column = QVBoxLayout()
        controls_column.setContentsMargins(0, 0, 0, 0)
        controls_column.setSpacing(2)
        self.refresh = QPushButton('Обновить')
        self.refresh.setFixedWidth(132)
        self.refresh.clicked.connect(self.refresh_now)
        controls_column.addWidget(self.refresh)
        brand_row.addLayout(controls_column)
        popup_header.addLayout(brand_row)
        outer.addWidget(self.popup_header)
        self.subtitle = QLabel('Оставшийся лимит · время до сброса и дата над каждой шкалой')
        self.subtitle.setObjectName('muted')
        outer.addWidget(self.subtitle)
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(10)
        outer.addWidget(self.body, 1)
        self.footer_bar = QWidget()
        footer_layout = QHBoxLayout(self.footer_bar)
        footer_layout.setContentsMargins(0, 0, 0, 0)
        footer_layout.setSpacing(4)
        self.interval_footer = QLabel('Автообновление · 30 сек.')
        self.interval_footer.setObjectName('footer')
        self.interval_footer.setToolTip('Лимиты обновляются автоматически раз в 30 секунд')
        footer_layout.addWidget(self.interval_footer)
        self.refresh_indicator = RefreshIndicator()
        footer_layout.addWidget(self.refresh_indicator)
        footer_layout.addStretch(1)
        self.footer = QLabel('Новосибирск UTC+7')
        self.footer.setObjectName('footer')
        self.footer.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        footer_layout.addWidget(self.footer)
        outer.addWidget(self.footer_bar)
        self.setCentralWidget(root)
        self.dashboard = root
        self.dashboard.setStyleSheet(self.styleSheet())
        self.popup = TrayPopup(self)
        self.destroyed.connect(self.popup.deleteLater)
        self.single_click_timer = QTimer(self)
        self.single_click_timer.setSingleShot(True)
        self.single_click_timer.timeout.connect(self.show_transient_popup)
        self.ignore_tray_until = 0
        self.tray = QSystemTrayIcon(icon(), self)
        self.tray.setToolTip('AI Limits')
        menu = QMenu(self)
        menu.addAction('Показать панель', self.show_window)
        menu.addAction('Обновить сейчас', self.refresh_now)
        self.startup = QAction('Запускать вместе с Windows', self)
        self.startup.setCheckable(True)
        self.startup.setChecked(self.startup_path().exists() or self.legacy_startup_path().exists())
        self.startup.toggled.connect(self.set_startup)
        menu.addAction(self.startup)
        self.sleep_prevention = QAction('Не переводить компьютер в спящий режим', self)
        self.sleep_prevention.setCheckable(True)
        self.sleep_prevention.setChecked(self.keep_awake)
        self.sleep_prevention.toggled.connect(self.set_keep_awake)
        menu.addAction(self.sleep_prevention)
        menu.addSeparator()
        menu.addAction('Открыть папку программы', lambda: os.startfile(str(ROOT)))
        menu.addAction('Выход', self.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.tray_clicked)
        if not testing:
            self.tray.show()
            if self.keep_awake and not set_sleep_prevention(True):
                self.keep_awake = False
                self.sleep_prevention.blockSignals(True)
                self.sleep_prevention.setChecked(False)
                self.sleep_prevention.blockSignals(False)
                self.preferences['keep_awake'] = False
                self.preferences_save()
                self.tray.showMessage('AI Limits', 'Не удалось включить запрет автоматического сна.')
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.rebuild()
        geometry = self.preferences.get('geometry')
        if isinstance(geometry, list) and len(geometry) == 4 and all(isinstance(x, int) for x in geometry):
            x, y, width, height = geometry
            screen = QApplication.primaryScreen().availableGeometry()
            if self.preferences.get('layout_version') == 4:
                self.resize(max(self.minimumWidth(), min(width, screen.width())), max(self.minimumHeight(), min(height, screen.height())))
            self.move(max(screen.left(), min(x, screen.right() - self.width())),
                      max(screen.top(), min(y, screen.bottom() - self.height())))
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(5000)
        QApplication.instance().aboutToQuit.connect(self.codex.close)
        QApplication.instance().aboutToQuit.connect(lambda: set_sleep_prevention(False))
        if not start_tray or not QSystemTrayIcon.isSystemTrayAvailable():
            self.show()
        QTimer.singleShot(0, self.tick)

    def preferences_save(self):
        if self.testing:
            return
        self.preferences.pop('popup_on_top', None)
        self.preferences.update(interval=UPDATE_INTERVAL_SECONDS, keep_open=self.keep_open,
                                 keep_awake=self.keep_awake, layout_version=6)
        save_json(DATA / 'settings.json', self.preferences)

    def set_keep_open(self, enabled):
        self.keep_open = bool(enabled)
        if hasattr(self, 'popup'):
            self.popup.set_keep_open(enabled)
        self.preferences_save()

    def set_keep_awake(self, enabled):
        enabled = bool(enabled)
        if not self.testing and not set_sleep_prevention(enabled):
            self.sleep_prevention.blockSignals(True)
            self.sleep_prevention.setChecked(self.keep_awake)
            self.sleep_prevention.blockSignals(False)
            self.tray.showMessage('AI Limits', 'Не удалось изменить запрет автоматического сна.')
            return
        self.keep_awake = enabled
        self.preferences['keep_awake'] = enabled
        self.sleep_prevention.blockSignals(True)
        self.sleep_prevention.setChecked(enabled)
        self.sleep_prevention.blockSignals(False)
        self.preferences_save()

    def hide_dashboard(self):
        self.single_click_timer.stop()
        self.hide()
        self.popup.hide()

    def attach_dashboard(self, to_popup):
        if to_popup and self.centralWidget() is self.dashboard:
            self.dashboard.hide()
            self.takeCentralWidget()
            self.popup.content_layout.addWidget(self.dashboard)
            self.dashboard.show()
        elif not to_popup and self.centralWidget() is not self.dashboard:
            self.dashboard.hide()
            self.popup.content_layout.removeWidget(self.dashboard)
            self.setCentralWidget(self.dashboard)
            self.dashboard.show()
        if self.popup_mode != to_popup:
            self.popup_mode = to_popup
            self.popup_header.setVisible(to_popup)
            self.subtitle.setVisible(not to_popup)
            self.footer.setText('Новосибирск UTC+7')
            self.rebuild()

    def toggle_popup(self):
        if self.popup.isVisible():
            self.popup.hide()
            return
        self.open_popup()

    def show_transient_popup(self):
        """Show the tray panel in hover-dismiss mode after a single click."""
        self.set_keep_open(False)
        if self.popup.isVisible():
            self.popup.raise_()
            return
        self.open_popup()

    def open_popup(self):
        self.hide()
        self.attach_dashboard(True)
        self.update_clocks()
        self.popup.present()
        if time.monotonic() - self.last_attempt > 15:
            self.refresh_now()

    def show_window(self, center=False):
        """Compatibility entry point: show the compact panel and keep it open."""
        self.single_click_timer.stop()
        self.set_keep_open(True)
        if self.popup.isVisible():
            self.popup.raise_()
            return
        self.open_popup()

    def tray_clicked(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            if time.monotonic() >= self.ignore_tray_until:
                self.single_click_timer.start(QApplication.doubleClickInterval())
        elif reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.single_click_timer.stop()
            self.ignore_tray_until = time.monotonic() + QApplication.doubleClickInterval() / 1000
            self.set_keep_open(not self.keep_open if self.popup.isVisible() else True)
            if not self.popup.isVisible():
                self.open_popup()
        elif reason == QSystemTrayIcon.ActivationReason.Context:
            self.single_click_timer.stop()
            self.popup.hide()

    def closeEvent(self, event):
        self.preferences_save()
        if self.exiting or not QSystemTrayIcon.isSystemTrayAvailable():
            self.codex.close()
            event.accept()
        else:
            event.ignore()
            self.hide()

    def quit(self):
        self.exiting = True
        self.preferences_save()
        self.timer.stop()
        self.single_click_timer.stop()
        self.refresh_indicator.set_spinning(False)
        self.popup.hide()
        self.codex.close()
        self.tray.hide()
        QApplication.instance().quit()

    @staticmethod
    def startup_path():
        return Path(os.environ.get('APPDATA', str(Path.home()))) / 'Microsoft/Windows/Start Menu/Programs/Startup/AI Limits.lnk'

    @staticmethod
    def legacy_startup_path():
        return Path(os.environ.get('APPDATA', str(Path.home()))) / 'Microsoft/Windows/Start Menu/Programs/Startup/Quota Widget.lnk'

    def set_startup(self, enabled):
        path = self.startup_path()
        legacy = self.legacy_startup_path()
        try:
            if enabled:
                create_shortcut(path, tray=True)
                if legacy.exists():
                    legacy.unlink()
            else:
                for shortcut in (path, legacy):
                    if shortcut.exists():
                        shortcut.unlink()
        except (OSError, subprocess.SubprocessError):
            self.startup.blockSignals(True)
            self.startup.setChecked(path.exists() or legacy.exists())
            self.startup.blockSignals(False)
            self.tray.showMessage('AI Limits', 'Не удалось изменить автозапуск Windows.')

    def refresh_now(self):
        if time.monotonic() - self.last_attempt < 3:
            return
        for key in self.due:
            self.due[key] = 0
        self.tick()

    def tick(self):
        if self.exiting:
            return
        now = time.monotonic()
        for key in self.due:
            if key not in self.running and now >= self.due[key]:
                self.running.add(key)
                self.last_attempt = now
                threading.Thread(target=self.fetch, args=(key,), daemon=True).start()
        self.refresh_indicator.set_spinning(bool(self.running))
        if self.isVisible() or self.popup.isVisible():
            self.update_clocks()

    def fetch(self, key):
        try:
            results = [self.codex.fetch()] if key == 'codex' else self.antigravity.fetch()
        except Exception as error:
            message = str(error) if isinstance(error, RuntimeError) else 'Ошибка подключения. Проверьте сеть и вход в приложение.'
            results = [Snapshot(key, TITLES[key], error=message)]
        if not self.exiting:
            self.bus.ready.emit(key, results)

    def received(self, key, results):
        self.running.discard(key)
        self.refresh_indicator.set_spinning(bool(self.running))
        failed = all(snapshot.error for snapshot in results)
        self.failures[key] = self.failures[key] + 1 if failed else 0
        delay = max(self.interval, min(300, 30 * 2 ** min(self.failures[key] - 1, 4))) if failed else self.interval
        self.due[key] = time.monotonic() + delay
        for snapshot in results:
            if snapshot.error and snapshot.key in self.snapshots:
                self.snapshots[snapshot.key].error = snapshot.error
            else:
                self.snapshots[snapshot.key] = snapshot
            if not snapshot.error:
                self.session_success.add(snapshot.key)
        if not self.testing:
            save_json(DATA / 'cache.json', {key: value.to_dict() for key, value in self.snapshots.items()})
        self.rebuild()

    @staticmethod
    def quota_color(quota):
        remaining = 0 if quota.disabled else quota.remaining
        if remaining is None:
            return '#8c9ab2'
        if remaining >= 30:
            return '#73a75a'
        if remaining > 10:
            return '#f4d35e'
        return '#ff7a82'

    @staticmethod
    def group_heading(parts, provider=None, status=None):
        heading = QWidget()
        layout = QHBoxLayout(heading)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        if status is not None:
            layout.addWidget(status, 0, Qt.AlignmentFlag.AlignVCenter)
        for kind, text in parts:
            if kind:
                mark = QLabel()
                mark.setPixmap(glyph_pixmap(kind, 14))
                mark.setFixedSize(14, 14)
                layout.addWidget(mark, 0, Qt.AlignmentFlag.AlignVCenter)
            label = QLabel(text)
            label.setObjectName('groupTitle')
            layout.addWidget(label, 0, Qt.AlignmentFlag.AlignVCenter)
        if provider is not None:
            provider_key, provider_name, plan, detail = provider
            layout.addStretch(1)
            mark = QLabel()
            mark.setPixmap(glyph_pixmap(provider_key, 16))
            mark.setFixedSize(16, 16)
            layout.addWidget(mark, 0, Qt.AlignmentFlag.AlignVCenter)
            name = QLabel(provider_name)
            name.setObjectName('section')
            name.setToolTip(detail)
            layout.addWidget(name, 0, Qt.AlignmentFlag.AlignVCenter)
            if plan:
                plan_label = QLabel(plan.upper())
                plan_label.setObjectName('providerPlan')
                plan_label.setToolTip(plan or 'Тариф')
                layout.addWidget(plan_label, 0, Qt.AlignmentFlag.AlignVCenter)
        heading.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        return heading

    def quota_cell(self, quota, with_window=False):
        cell = QWidget()
        cell_layout = QVBoxLayout(cell)
        cell_layout.setContentsMargins(0, 0, 0, 0)
        cell_layout.setSpacing(7 if not self.popup_mode else 6)
        metrics = QHBoxLayout()
        metrics.setSpacing(8)
        remaining = 0 if quota.disabled else quota.remaining
        text = '.' if remaining is None else f'{number(remaining)}%'
        value = QLabel(text)
        value.setObjectName('quotaValue')
        value.setProperty('quotaGroup', quota.group)
        value.setProperty('quotaWindow', quota.window)
        color = self.quota_color(quota)
        value.setStyleSheet('font-size: 13px; font-weight: 600; color: #f4f6ff;')
        explanation = quota.description
        if quota.disabled:
            explanation = 'Пятичасовое окно не действует; показываем остаток 0%.\n' + explanation
        elif remaining is not None:
            explanation = f'Осталось {number(remaining)}%.\n' + explanation
        value.setToolTip(explanation)
        value.setFixedWidth(128)
        metrics.addWidget(value)
        if with_window:
            metrics.addStretch()
            window_label = QLabel({'5 часов': '5 ч.', 'Неделя': 'Нед.'}.get(quota.window, quota.window))
            window_label.setObjectName('quotaWindow')
            window_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            window_label.setFixedWidth(52)
            window_label.setStyleSheet('font-size: 10px; font-weight: 600; color: #aebbd1;')
            metrics.addWidget(window_label)
            metrics.addStretch()
        else:
            metrics.addStretch()
        reset = QLabel()
        reset.setObjectName('quotaReset')
        reset.setStyleSheet('font-size: 12px;')
        reset.setTextFormat(Qt.TextFormat.RichText)
        reset.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        reset.setFixedWidth(128)
        reset.setToolTip(countdown(quota.reset) if quota.reset is not None else '')
        self.reset_labels.append((reset, quota))
        metrics.addWidget(reset)
        cell_layout.addLayout(metrics)
        bar = SegmentedProgressBar(remaining, color, quota.window, quota.reset)
        bar.setToolTip('Цветная заливка — остаток лимита. Полупрозрачная — время до сброса.')
        self.cooldown_bars.append(bar)
        cell_layout.addWidget(bar)
        cell.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        return cell

    def rebuild(self):
        self.reset_labels, self.cooldown_bars, self.age_labels = [], [], []
        while self.body_layout.count():
            item = self.body_layout.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        for key in TITLES:
            snapshot = self.snapshots.get(key, Snapshot(key, TITLES[key]))
            card = QFrame()
            card.setObjectName('card')
            card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
            inner = QVBoxLayout(card)
            inner.setContentsMargins(12, 9 if self.popup_mode else 12, 12, 9 if self.popup_mode else 12)
            inner.setSpacing(7 if self.popup_mode else 10)
            age = QLabel()
            age.setObjectName('error' if snapshot.error else 'muted')
            age.setToolTip(snapshot.error or 'Время последнего успешного обновления')
            self.age_labels.append((age, snapshot))
            detail = '\n'.join(snapshot.models) or 'Каталог моделей ещё не получен'
            identity = (key, snapshot.title, snapshot.plan, detail + '\n\n' + snapshot.note)
            if not snapshot.quotas:
                inner.addWidget(self.group_heading([], provider=identity, status=age))
                message = QLabel('Нет связи · наведите для подробностей' if snapshot.error else 'Подключаемся…')
                message.setObjectName('error' if snapshot.error else 'muted')
                message.setToolTip(snapshot.error)
                inner.addWidget(message)
            else:
                groups = list(dict.fromkeys(q.group for q in snapshot.quotas))
                if key == 'antigravity':
                    def group_order(group):
                        lower = group.lower()
                        if 'claude' in lower or 'gpt' in lower:
                            return 0
                        if 'argon' in lower:
                            return 1
                        return 2 if 'gemini' in lower else 3
                    groups.sort(key=group_order)
                names = {'Общая квота Codex': [('chatgpt', 'ChatGPT')],
                         'Gemini Models': [('gemini', 'Gemini Flash')],
                         'Claude and GPT models': [('claude', 'Claude')],
                         'Gemini 4 Argon': [('gemini', 'Gemini Argon')]}
                for row, group in enumerate(groups, 1):
                    parts = names.get(group, [('gemini', group)])
                    first = row == 1
                    label = self.group_heading(parts, provider=identity if first else None,
                                               status=age if first else None)
                    label.setToolTip(group + '\n' + snapshot.note)
                    if row > 1:
                        inner.addSpacing(10)
                    inner.addWidget(label)
                    group_panel = QFrame()
                    group_panel.setObjectName('quotaGroup')
                    group_panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
                    panel_layout = QVBoxLayout(group_panel)
                    panel_layout.setContentsMargins(9, 5, 9, 5)
                    panel_layout.setSpacing(16)
                    matches = [q for q in snapshot.quotas if q.group == group]
                    for window in ('Неделя', '5 часов'):
                        if not any(quota.window == window for quota in matches):
                            matches.append(Quota(group, window, None,
                                                 description='Сервис не возвращает отдельный лимит этого окна для группы.'))
                    matches.sort(key=lambda q: (0 if q.window == 'Неделя' else 1 if q.window == '5 часов' else 2, q.window))
                    for quota in matches:
                        panel_layout.addWidget(self.quota_cell(quota, with_window=True))
                    inner.addWidget(group_panel)
            self.body_layout.addWidget(card)
            card.show()
        self.body_layout.addStretch()
        self.update_clocks()
        self.update_tray()
        self.body_layout.activate()
        self.dashboard.layout().activate()
        if not self.popup_mode:
            minimum = self.dashboard.minimumSizeHint()
            self.setMinimumSize(max(950, minimum.width()), max(620, minimum.height()))
        if self.popup.isVisible():
            self.popup.position_at_tray()


    def update_clocks(self):
        for label, quota in self.reset_labels:
            label.setText(reset_markup(quota))
            label.setToolTip(countdown(quota.reset))
        for bar in self.cooldown_bars:
            bar.update_cooldown()
        for label, snapshot in self.age_labels:
            text = ('Данные устарели' if snapshot.updated else 'Нет связи') if snapshot.error else ''
            label.setText(text)
            label.setVisible(bool(text))

    def update_tray(self):
        lines = ['AI Limits', '']
        for key in TITLES:
            snapshot = self.snapshots.get(key)
            if key == 'codex':
                lines.append('')
            lines.append('Antigravity' if key == 'antigravity' else 'Codex')
            if not snapshot or not snapshot.quotas:
                lines.append('Нет данных')
                continue
            groups = list(dict.fromkeys(q.group for q in snapshot.quotas))
            if key == 'antigravity':
                groups.sort(key=lambda group: (0 if 'claude' in group.lower() or 'gpt' in group.lower()
                                               else 1 if 'argon' in group.lower() else 2))
            names = {'Общая квота Codex': 'ChatGPT', 'Gemini Models': 'Gemini Flash',
                     'Claude and GPT models': 'Claude', 'Gemini 4 Argon': 'Gemini Argon'}
            for group in groups:
                by_window = {q.window: q for q in snapshot.quotas if q.group == group}
                def percent_for(window):
                    quota = by_window.get(window)
                    if not quota:
                        return '.'
                    if quota.disabled:
                        return '0%'
                    if quota.remaining is None:
                        return '.'
                    return f'{number(quota.remaining)}%'
                lines.append(f"{names.get(group, group)} {percent_for('5 часов')} / {percent_for('Неделя')}")
        self.tray.setIcon(icon())
        # Keep the hover summary within the native Windows tray-text limit.
        self.tray.setToolTip('\n'.join(lines)[:127])


def create_shortcut(path, tray=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Paths are passed as environment data, never interpolated into PowerShell code.
    env = os.environ.copy()
    env.update(QUOTA_LINK=str(path), QUOTA_PYTHON=str(Path(sys.executable).resolve().with_name('pythonw.exe')),
               QUOTA_ROOT=str(ROOT), QUOTA_ARGS='"' + str(ROOT / 'app.py') + '"' + (' --tray' if tray else ''))
    script = '''$w=New-Object -ComObject WScript.Shell
$s=$w.CreateShortcut($env:QUOTA_LINK)
$s.TargetPath=$env:QUOTA_PYTHON
$s.Arguments=$env:QUOTA_ARGS
$s.WorkingDirectory=$env:QUOTA_ROOT
$s.Description='AI Limits'
$s.IconLocation=Join-Path $env:QUOTA_ROOT 'ai-limits-start.ico'
$s.Save()'''
    subprocess.run(['powershell', '-NoProfile', '-Command', script], check=True, env=env,
                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--tray', action='store_true')
    parser.add_argument('--install-shortcut', action='store_true')
    parser.add_argument('--preview', action='store_true')
    parser.add_argument('--peek', action='store_true', help='Open the tray panel')
    parser.add_argument('--large', action='store_true', help='Open and keep the compact tray panel visible')
    parser.add_argument('--status', action='store_true', help='Print local window status as JSON')
    parser.add_argument('--quit', action='store_true', help='Close the running widget')
    args = parser.parse_args()
    if args.preview:
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    app = QApplication(sys.argv[:1])
    # The offscreen Windows Qt backend does not discover system fonts itself.
    fonts = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts'
    for filename in ('segoeui.ttf', 'segoeuib.ttf', 'seguisb.ttf'):
        font_path = fonts / filename
        if font_path.exists():
            QFontDatabase.addApplicationFont(str(font_path))
    app.setFont(QFont('Segoe UI', 11))
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName('AI Limits')
    if args.install_shortcut:
        # Use a distinct icon path so the Windows Start menu does not reuse a stale cached icon.
        if not icon().pixmap(64, 64).save(str(ROOT / 'ai-limits-start.ico')):
            raise RuntimeError('Could not create the AI Limits shortcut icon')
        base = Path(os.environ['APPDATA']) / 'Microsoft/Windows/Start Menu/Programs'
        create_shortcut(base / 'AI Limits.lnk')
        create_shortcut(ROOT / 'AI Limits.lnk')
        for old_shortcut in (base / 'Квоты Codex и Antigravity.lnk', ROOT / 'Квоты.lnk'):
            if old_shortcut.exists():
                old_shortcut.unlink()
        print('Start menu shortcut installed')
        return
    if not args.preview:
        client = QLocalSocket()
        client.connectToServer('QuotaWidget-' + os.environ.get('USERNAME', 'user'))
        if client.waitForConnected(500):
            command = 'status' if args.status else 'quit' if args.quit else 'peek' if args.peek else 'large' if args.large else 'show'
            client.write(command.encode('ascii'))
            client.waitForBytesWritten(500)
            if args.status and client.waitForReadyRead(1500):
                print(bytes(client.readAll()).decode('utf-8'))
            return
        if args.status or args.quit:
            if args.status:
                print('{"running":false}')
            return
    window = Monitor(True, testing=args.preview)
    server = QLocalServer(window)
    if not args.preview:
        def incoming():
            connection = server.nextPendingConnection()
            if not connection:
                return
            def dispatch():
                command = bytes(connection.readAll()).decode('ascii', errors='ignore').strip()
                if not command:
                    return
                if command == 'status':
                    popup = window.popup
                    payload = {'running': True, 'main_visible': window.isVisible(), 'popup_visible': popup.isVisible(),
                               'popup_geometry': popup.geometry().getRect(), 'tray_geometry': window.tray.geometry().getRect(),
                               'main_geometry': window.geometry().getRect(),
                               'keep_open': window.keep_open,
                               'always_on_top': bool(popup.windowFlags() & Qt.WindowType.WindowStaysOnTopHint),
                               'interval_seconds': window.interval,
                               'pointer_inside_popup': popup.frameGeometry().contains(QCursor.pos()),
                               'dashboard_host': 'main' if window.centralWidget() is window.dashboard else 'popup',
                               'visible_quotas': sum(v.isVisible() for v in window.dashboard.findChildren(QLabel, 'quotaValue'))}
                    connection.write(json.dumps(payload).encode('utf-8'))
                elif command == 'peek':
                    window.tray_clicked(QSystemTrayIcon.ActivationReason.Trigger)
                elif command == 'large':
                    window.tray_clicked(QSystemTrayIcon.ActivationReason.DoubleClick)
                elif command == 'quit':
                    window.quit()
                else:
                    window.show_window()
                connection.disconnectFromServer()
            connection.readyRead.connect(dispatch)
            connection.disconnected.connect(connection.deleteLater)
            if connection.bytesAvailable():
                dispatch()
        server.newConnection.connect(incoming)
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        if not server.listen('QuotaWidget-' + os.environ.get('USERNAME', 'user')):
            window.quit()
            return
        if args.peek:
            QTimer.singleShot(0, lambda: window.tray_clicked(QSystemTrayIcon.ActivationReason.Trigger))
        elif args.large:
            QTimer.singleShot(0, window.show_window)
        elif not args.tray:
            QTimer.singleShot(0, window.toggle_popup)
    if args.preview:
        def capture():
            if window.running and not getattr(window, 'preview_timeout', False):
                QTimer.singleShot(500, capture)
                return
            window.resize(max(980, window.minimumWidth()), max(640, window.minimumHeight()))
            app.processEvents()
            window.grab().save(str(ROOT / 'preview.png'))
            window.toggle_popup()
            window.popup.dismiss_timer.stop()
            app.processEvents()
            window.popup.grab().save(str(ROOT / 'preview-popup.png'))
            print(json.dumps({key: {'quotas': len(s.quotas), 'models': len(s.models), 'error': s.error} for key, s in window.snapshots.items()}, ensure_ascii=True))
            window.quit()
        QTimer.singleShot(1500, capture)
        QTimer.singleShot(45000, lambda: setattr(window, 'preview_timeout', True))
    sys.exit(app.exec())


if __name__ == '__main__':
    main()


