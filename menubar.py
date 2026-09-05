"""The macOS menu bar item.

Gives the app a presence outside its window: a quick scan over a chosen period,
the background schedule, and the result of the last unattended run, without
bringing the window forward. Toggled from View, or from Settings.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from PySide6.QtCore import QObject, QRectF, Qt, Signal
from PySide6.QtGui import (
    QAction, QActionGroup, QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap,
)
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


#: Menu bar artwork, in pixels at 2x. macOS gives a status item a 24 point bar
#: and expects the artwork to sit inside roughly 18 to 22 points of it, so this
#: draws at 2x and lets Qt hand a correctly sized NSImage to the status bar.
_BAR_CANVAS = (46, 33)
_BAR_MARGIN = 2.0
_BAR_STROKE = 3.0


def menu_bar_icon() -> QIcon:
    """A monochrome template icon, so macOS tints it for light and dark bars."""
    width, height = _BAR_CANVAS
    pixmap = QPixmap(width, height)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    pen = QPen(QColor(0, 0, 0))
    pen.setWidthF(_BAR_STROKE)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    # The stroke straddles the path, so the path is inset by half of it. That
    # puts the same _BAR_MARGIN of clear space on all four sides.
    inset = _BAR_MARGIN + _BAR_STROKE / 2.0
    body = QRectF(inset, inset, width - 2 * inset, height - 2 * inset)
    painter.drawRoundedRect(body, 4.0, 4.0)

    # The flap starts on the body outline rather than inside it, so the two
    # shapes meet cleanly instead of leaving a hairline gap.
    flap = QPainterPath()
    flap.moveTo(body.left(), body.top() + body.height() * 0.16)
    flap.lineTo(body.center().x(), body.top() + body.height() * 0.60)
    flap.lineTo(body.right(), body.top() + body.height() * 0.16)
    painter.drawPath(flap)
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
