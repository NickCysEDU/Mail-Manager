"""The macOS menu bar item.

Gives the app a presence outside its window: a quick scan over a chosen period,
the background schedule, and the result of the last unattended run, without
bringing the window forward. Toggled from View, or from Settings.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from PySide6.QtCore import QObject, QPoint, Qt, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

import scheduler
from models import APP_DISPLAY_NAME, TimeWindow

log = logging.getLogger(__name__)

#: Quick-scan choices offered from the menu bar.
QUICK_SCANS = (
    (TimeWindow.LAST_24_HOURS, "Scan the past 24 hours"),
    (TimeWindow.LAST_3_DAYS, "Scan the past 3 days"),
    (TimeWindow.LAST_7_DAYS, "Scan the past 7 days"),
)


def menu_bar_icon() -> QIcon:
    """A monochrome template icon, so macOS tints it for light and dark bars."""
    size = 36
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = painter.pen()
    pen.setColor(QColor(0, 0, 0))
    pen.setWidthF(2.6)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    # An envelope: readable at 16 points, which is all a menu bar gives you.
    painter.drawRoundedRect(6, 10, 24, 17, 3, 3)
    painter.drawPolyline([QPoint(7, 12), QPoint(18, 21), QPoint(29, 12)])
    painter.end()
    icon = QIcon(pixmap)
    icon.setIsMask(True)          # let macOS handle light and dark
    return icon


class MenuBarController(QObject):
    """Owns the tray item and keeps its menu in step with the app."""

    quickScanRequested = Signal(object)      # a TimeWindow
    openRequested = Signal()
    settingsRequested = Signal()
    scheduleChanged = Signal(int)            # minutes, 0 for off
    quitRequested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.tray: Optional[QSystemTrayIcon] = None
        self._menu: Optional[QMenu] = None
        self._status_action: Optional[QAction] = None
        self._interval_actions: dict = {}

    # -- lifecycle -------------------------------------------------------
    def available(self) -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable()

    def show(self, schedule_minutes: int = 0) -> bool:
        if not self.available():
            log.info("No system tray on this system; menu bar item not shown.")
            return False
        if self.tray is None:
            self.tray = QSystemTrayIcon(menu_bar_icon(), self)
            self.tray.setToolTip(APP_DISPLAY_NAME)
            self.tray.activated.connect(self._activated)
            self._menu = QMenu()
            self.tray.setContextMenu(self._menu)
        self.rebuild(schedule_minutes)
        self.tray.show()
        return True

    def hide(self) -> None:
        if self.tray is not None:
            self.tray.hide()

    def visible(self) -> bool:
        return self.tray is not None and self.tray.isVisible()

    # -- menu ------------------------------------------------------------
    def rebuild(self, schedule_minutes: int = 0) -> None:
        if self._menu is None:
            return
        menu = self._menu
        menu.clear()

        open_action = QAction(f"Open {APP_DISPLAY_NAME}", self)
        open_action.triggered.connect(self.openRequested.emit)
        menu.addAction(open_action)
        menu.addSeparator()

        for window, label in QUICK_SCANS:
            action = QAction(label, self)
            action.triggered.connect(
                lambda checked=False, w=window: self.quickScanRequested.emit(w)
            )
            menu.addAction(action)
        menu.addSeparator()

        schedule_menu = menu.addMenu("Scan automatically")
        group = QActionGroup(self)
        group.setExclusive(True)
        self._interval_actions = {}
        for minutes, label in scheduler.INTERVALS:
            action = QAction(label, self)
            action.setCheckable(True)
            action.setChecked(minutes == schedule_minutes)
            action.triggered.connect(
                lambda checked=False, m=minutes: self.scheduleChanged.emit(m)
            )
            group.addAction(action)
            schedule_menu.addAction(action)
            self._interval_actions[minutes] = action

        self._status_action = QAction(scheduler.read_status().describe(), self)
        self._status_action.setEnabled(False)
        menu.addAction(self._status_action)
        menu.addSeparator()

        settings_action = QAction("Settings...", self)
        settings_action.triggered.connect(self.settingsRequested.emit)
        menu.addAction(settings_action)

        quit_action = QAction(f"Quit {APP_DISPLAY_NAME}", self)
        quit_action.triggered.connect(self.quitRequested.emit)
        menu.addAction(quit_action)

    def set_schedule(self, minutes: int) -> None:
        for value, action in self._interval_actions.items():
            action.setChecked(value == minutes)

    def refresh_status(self, text: str = "") -> None:
        if self._status_action is not None:
            self._status_action.setText(text or scheduler.read_status().describe())

    def notify(self, title: str, message: str) -> None:
        if self.tray is not None and self.tray.isVisible():
            self.tray.showMessage(title, message, menu_bar_icon(), 5000)

    def _activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.openRequested.emit()
