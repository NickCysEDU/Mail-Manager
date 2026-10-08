"""Hover help. With help off, tooltips are held back everywhere but in the
windows whose buttons are icons (the message, the compose window, the
viewer), where a control rested on for five seconds still says what it is.
Every window's ? switches the same help, and shows it switched."""

from __future__ import annotations

import pytest

from PySide6.QtCore import QEvent, QPoint
from PySide6.QtGui import QHelpEvent
from PySide6.QtWidgets import QApplication, QPushButton, QToolTip, QVBoxLayout, QWidget

import helpmode


@pytest.fixture
def help_filter(qapp, monkeypatch):
    """A filter of its own, with the patient delay short and the pointer
    and the tooltip watched rather than real."""
    made = helpmode.HelpFilter()
    shown = []
    monkeypatch.setattr(helpmode, "PATIENT_DELAY", 120)
    monkeypatch.setattr(helpmode, "WAKE_UP", 20)
    monkeypatch.setattr(QToolTip, "showText",
                        staticmethod(lambda *a, **k: shown.append(a[1])))
    made.shown = shown
    yield made
    made.deleteLater()


def _window(qtbot, patient=True):
    window = QWidget()
    QVBoxLayout(window)
    button = QPushButton("B", window)
    button.setToolTip("Bold (⌘B)")
    window.layout().addWidget(button)
    if patient:
        window.setProperty(helpmode.PATIENT, True)
    qtbot.addWidget(window)
    window.show()
    return window, button


def _rest(help_filter, widget):
    """What Qt sends once the pointer has stopped on ``widget``."""
    event = QHelpEvent(QEvent.Type.ToolTip, QPoint(2, 2), widget.mapToGlobal(QPoint(2, 2)))
    return help_filter.eventFilter(widget, event)


def _pointer_on(monkeypatch, widget):
    monkeypatch.setattr(QApplication, "widgetAt", staticmethod(lambda *a: widget))


class TestPatientTooltips:
    def test_a_long_rest_names_the_button(self, qtbot, help_filter, monkeypatch):
        window, button = _window(qtbot)
        _pointer_on(monkeypatch, button)
        assert _rest(help_filter, button) is True
        assert help_filter.shown == [], "it came up at once, with help off"
        qtbot.waitUntil(lambda: help_filter.shown == ["Bold (⌘B)"], timeout=2000)

    def test_not_if_the_pointer_has_moved_on(self, qtbot, help_filter, monkeypatch):
        window, button = _window(qtbot)
        _pointer_on(monkeypatch, window)
        _rest(help_filter, button)
        qtbot.wait(300)
        assert help_filter.shown == []

    def test_not_in_a_window_that_did_not_ask(self, qtbot, help_filter, monkeypatch):
        """The main window keeps its quiet: help is the way to its tips."""
        window, button = _window(qtbot, patient=False)
        _pointer_on(monkeypatch, button)
        assert _rest(help_filter, button) is True
        qtbot.wait(300)
        assert help_filter.shown == []

    def test_moving_about_on_one_button_does_not_start_the_wait_again(
            self, qtbot, help_filter, monkeypatch):
        window, button = _window(qtbot)
        _pointer_on(monkeypatch, button)
        _rest(help_filter, button)
        qtbot.wait(70)
        _rest(help_filter, button)
        qtbot.wait(70)
        _rest(help_filter, button)
        # 140 ms of moving about: the 100 ms wait from the first rest is up.
        qtbot.waitUntil(lambda: bool(help_filter.shown), timeout=150)

    def test_with_help_on_it_comes_up_at_once(self, qtbot, help_filter, monkeypatch):
        window, button = _window(qtbot)
        help_filter.set_enabled(True)
        _rest(help_filter, button)
        assert help_filter.shown == ["Bold (⌘B)"]


class TestEveryQuestionMarkIsTheSameSwitch:
    def test_pressing_one_switches_help_and_every_other_shows_it(self, qtbot):
        app = QApplication.instance()
        filter_ = helpmode.install(app, False)
        switched = []

        class Owner:
            def set_help(self, on):
                switched.append(on)
                helpmode.install(app, on)

        first = helpmode.button_for(Owner())
        second = helpmode.button_for(Owner())
        qtbot.addWidget(first)
        qtbot.addWidget(second)
        try:
            first.click()
            assert switched == [True] and filter_.enabled
            assert second.isChecked(), "the other ? did not show it"
            second.click()
            assert switched == [True, False] and not filter_.enabled
            assert not first.isChecked()
        finally:
            helpmode.install(app, False)

    def test_the_windows_carry_one_and_ask_for_patient_tips(self, qapp, qtbot):
        import outgoing
        from attachment_view import AttachmentViewer
        from mail_window import ComposeWindow, MessageWindow
        from test_mail_window import _Owner

        owner = _Owner()
        reading = MessageWindow(owner, 0)
        writing = ComposeWindow(owner, outgoing.Draft(from_address="you@icloud.example"),
                                owner.accounts, owner.accounts[0])
        viewer = AttachmentViewer([], "", library=True)
        for window in (reading, writing, viewer):
            qtbot.addWidget(window)
            assert window.property(helpmode.PATIENT), type(window).__name__
            assert isinstance(window.help_button, helpmode.HelpButton)
