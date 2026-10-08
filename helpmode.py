"""A help mode that puts tooltips back where they belong.

Qt shows a tooltip after a fixed delay whether or not anybody wanted one, and
on a dense window that is mostly noise. This makes it deliberate: a circled
question mark in the corner, filled when it is on, and while it is on every
control explains itself after a short hover.

The toggle also lengthens the delay and the time on screen, because the point
of turning it on is that you are reading rather than working.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEvent, QObject, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QPainter, QPen
from PySide6.QtWidgets import QApplication, QToolButton, QToolTip, QWidget

#: How long a hover has to last before an explanation appears, in milliseconds.
HOVER_DELAY = 600
#: How long it stays there. Long enough to finish a sentence twice.
VISIBLE_FOR = 20000

#: With help off, a window that asks for it (see PATIENT) still explains a
#: control that has been rested on this long, in milliseconds: its buttons
#: are icons, and their names have to be somewhere. Counted from when the
#: pointer came to rest, which Qt reports WAKE_UP after it stops.
PATIENT_DELAY = 5000
WAKE_UP = 700
#: The property a window sets to ask for that.
PATIENT = "patientTips"


def circle_in(rect: QRect) -> QRect:
    """The largest sensible circle inside a rect, centred.

    Its own function because the button is not always the size it asks for:
    it requests 26 by 26, the shared stylesheet gives every control a minimum
    height, and it arrives 26 by 28. Painting into the whole rect drew an
    oval. Taking a square off the shorter side means no stylesheet can
    squash it, and it can be checked without a widget that refuses to resize.
    """
    side = max(8, min(rect.width(), rect.height()) - 6)
    box = QRect(0, 0, side, side)
    box.moveCenter(rect.center())
    return box


class HelpButton(QToolButton):
    """A question mark in a circle: outlined when off, filled when on."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setAutoRaise(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName("helpButton")   # excluded from the shared control height
        self.setFixedSize(26, 26)
        # Laid out at the size it is drawn. The Mac style takes its own
        # margins off a tool button's box, which left this one four pixels
        # tall to a layout short of room: it was squeezed to a sliver.
        self.setAttribute(Qt.WidgetAttribute.WA_LayoutUsesWidgetRect, True)
        self.setText("")
        self._sync_text()
        self.toggled.connect(lambda _on: self._sync_text())

    def follow(self, on: bool) -> None:
        """Show the app's help as switched elsewhere, without switching it."""
        if self.isChecked() != bool(on):
            self.blockSignals(True)
            self.setChecked(bool(on))
            self.blockSignals(False)
            self._sync_text()

    def _sync_text(self) -> None:
        on = self.isChecked()
        self.setToolTip(
            "Help is on. Hover anything to see what it does."
            if on else
            "Turn on help."
        )
        self.setAccessibleName("Help" + (" (on)" if on else ""))
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        colour = self.palette().color(
            self.palette().ColorRole.Highlight if self.isChecked()
            else self.palette().ColorRole.WindowText
        )
        box = circle_in(self.rect())

        if self.isChecked():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(colour)
            painter.drawEllipse(box)
            ink = self.palette().color(self.palette().ColorRole.HighlightedText)
        else:
            pen = QPen(colour, 1.6)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(box)
            ink = colour

        font = self.font()
        font.setPointSizeF(max(9.0, font.pointSizeF()))
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(ink)
        painter.drawText(box, int(Qt.AlignmentFlag.AlignCenter), "?")
        painter.end()


class HelpFilter(QObject):
    """While help is on, show a widget's tooltip after a deliberate hover;
    with it off, only in a window that asks (see PATIENT), and only after a
    long one."""

    #: Help switched on or off, for every ? to show it.
    switched = Signal(bool)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.enabled = False
        #: The control rested on in a patient window, waiting out the delay.
        self._waiting: Optional[QWidget] = None
        self._patience = QTimer(self)
        self._patience.setSingleShot(True)
        self._patience.timeout.connect(self._explain_now)

    def set_enabled(self, on: bool) -> None:
        was = self.enabled
        self.enabled = bool(on)
        if not self.enabled:
            QToolTip.hideText()
        if was != self.enabled:
            self.switched.emit(self.enabled)

    @staticmethod
    def _wording(widget: QWidget) -> str:
        text = widget.toolTip()
        if not text:
            parent = widget.parentWidget()
            text = parent.toolTip() if parent is not None else ""
        return text

    def _wait_on(self, widget: QWidget) -> None:
        """Explain ``widget`` if the pointer is still on it at the end of the
        delay. A pointer moving about on the same control does not start
        the wait again, or a hand that never quite stops would never see
        it."""
        if widget is self._waiting and self._patience.isActive():
            return
        self._waiting = widget
        self._patience.start(max(0, PATIENT_DELAY - WAKE_UP))

    def _explain_now(self) -> None:
        widget, self._waiting = self._waiting, None
        try:
            import shiboken6

            alive = widget is not None and shiboken6.isValid(widget)
        except Exception:  # noqa: BLE001 - assume it has gone
            alive = False
        if not alive or not widget.isVisible() or self.enabled:
            return
        where = QCursor.pos()
        under = QApplication.widgetAt(where)
        if under is None or not (under is widget or widget.isAncestorOf(under)):
            return
        text = self._wording(widget)
        if text:
            QToolTip.showText(where, text, widget, widget.rect(), VISIBLE_FOR)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        """Tooltips appear only while help is on.

        Qt shows any tooltip a widget happens to carry, which on a window this
        dense means explanations arriving unasked while somebody is working.
        With help off this swallows them; with help on it also lends a widget
        its parent's wording when it has none of its own, and leaves it up long
        enough to finish reading.
        """
        if event.type() != QEvent.Type.ToolTip:
            return False
        widget = watched if isinstance(watched, QWidget) else None
        if not self.enabled:
            QToolTip.hideText()
            if (widget is not None and widget.window().property(PATIENT)
                    and self._wording(widget)):
                self._wait_on(widget)
            return True                     # eaten: nothing asked for it yet
        if widget is None:
            return False
        text = self._wording(widget)
        if not text:
            return False
        QToolTip.showText(event.globalPos(), text, widget,
                          widget.rect(), VISIBLE_FOR)
        return True


def install(app, on: bool) -> HelpFilter:
    """Attach the filter to an application and set the hover delay."""
    existing = getattr(app, "_help_filter", None)
    if existing is None:
        existing = HelpFilter(app)
        app.installEventFilter(existing)
        app._help_filter = existing
    existing.set_enabled(on)
    return existing


def button_for(owner=None) -> HelpButton:
    """A ? for a window of its own, in step with every other: pressing it
    switches help for the whole app, through ``owner`` (the main window,
    which keeps the setting) where there is one."""
    app = QApplication.instance()
    found = getattr(app, "_help_filter", None)
    if found is None:
        found = install(app, False)
    button = HelpButton()
    button.setChecked(found.enabled)
    found.switched.connect(button.follow)

    def pressed(on: bool) -> None:
        setter = getattr(owner, "set_help", None)
        if callable(setter):
            setter(on)
        else:
            install(app, on)

    button.toggled.connect(pressed)
    return button
