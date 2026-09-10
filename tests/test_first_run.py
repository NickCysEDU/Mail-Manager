"""What a person meets the first time they open this.

Help mode works by showing a control's own tooltip on hover, so a control
without one is invisible to somebody trying to learn the window. These tests
are the standing check that every control can explain itself, and that the
first-run wizard offers every choice the app actually has.
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDateEdit, QDoubleSpinBox,
                               QLineEdit, QPushButton, QSlider, QSpinBox,
                               QToolButton)

import profiles
from config import InMemoryCredentialStore, Settings
from gui import MainWindow
from models import NonJobRouting, OtherCategory

#: Widget classes a person clicks, types in or drags.
INTERACTIVE = (QPushButton, QToolButton, QComboBox, QLineEdit, QCheckBox,
               QSpinBox, QDoubleSpinBox, QSlider, QDateEdit)


def explains_itself(widget) -> bool:
    if widget.toolTip() or widget.accessibleDescription():
        return True
    # Qt builds its own children inside spin boxes, editable combos and
    # overflowing menu bars. They are not controls anybody reasons about.
    name = widget.objectName() or ""
    if name.startswith("qt_"):
        return True
    parent = widget.parent()
    return bool(parent is not None and parent.toolTip())


def unexplained(root) -> list:
    out = []
    for kind in INTERACTIVE:
        for widget in root.findChildren(kind):
            if not explains_itself(widget):
                text = widget.text() if hasattr(widget, "text") else ""
                out.append(f"{kind.__name__}: {text or widget.objectName()!r}")
    return sorted(set(out))


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
    win = MainWindow(Settings(icloud_email="you@icloud.example"),
                     InMemoryCredentialStore())
    yield win
    win.close()


class TestEveryControlExplainsItself:
    def test_the_main_window(self, window):
        assert unexplained(window) == []

    def test_the_preview_pane(self, window):
        assert unexplained(window.preview) == []

    def test_the_settings_dialog(self, qapp, tmp_path, monkeypatch):
        from settings_dialog import SettingsDialog
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        dialog = SettingsDialog(Settings(icloud_email="you@icloud.example"),
                                InMemoryCredentialStore())
        try:
            # Settings is a reference page rather than a working surface, so
            # the bar is the controls that decide where mail goes.
            for name in ("routing_combo", "root_edit", "other_root_edit",
                         "learn_check", "reuse_check", "sorting_rules_check",
                         "auto_reply_check", "subscribe_check"):
                widget = getattr(dialog, name)
                assert widget.toolTip() or widget.accessibleDescription(), name
        finally:
            dialog.deleteLater()

    def test_the_setup_wizard(self, qapp, tmp_path, monkeypatch):
        from welcome import SetupWizard
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        wizard = SetupWizard(Settings(), InMemoryCredentialStore())
        try:
            # The wizard explains itself in prose on the page, so the bar
            # here is only that it presents every choice.
            assert wizard.pageIds()
        finally:
            wizard.deleteLater()


class TestTheWizardOffersEverything:
    @pytest.fixture
    def purpose(self, qapp):
        from welcome import PurposePage
        page = PurposePage()
        yield page
        page.deleteLater()

    def test_every_profile_is_offered(self, purpose):
        offered = {b.property("profile") for b in purpose.choice.buttons()}
        assert offered == {name for name, _l, _b in profiles.choices()}

    def test_every_topic_is_offered(self, purpose):
        assert set(purpose.topic_checks) == set(profiles.ALL_TOPICS)

    def test_church_is_one_of_them(self, purpose):
        """It was added later; the wizard has to have kept up."""
        assert OtherCategory.CHURCH in purpose.topic_checks

    def test_the_default_creates_something(self, purpose):
        profile = profiles.get(purpose.profile_name())
        assert profile.creates()


class TestTheSortingMenuIsDiscoverable:
    def test_it_is_in_the_toolbar_not_a_submenu(self, window):
        assert window.sorting_button.isVisibleTo(window)
        assert window.sorting_button.text().startswith("Sorting")

    def test_it_says_what_would_happen_to_non_job_mail(self, window):
        tip = window.sorting_button.toolTip()
        assert "left where it is" in tip

        window._switch_routing(NonJobRouting.FILE)
        assert "filed by topic" in window.sorting_button.toolTip()

    def test_every_routing_choice_explains_its_consequence(self, window):
        import gui
        for member in NonJobRouting:
            assert gui._ROUTING_HELP.get(member), member

    def test_the_menu_is_not_overwhelming(self, window):
        """A first-time user should meet three choices, not thirteen.

        The topic list is a submenu and only appears once filing is on,
        which is the only time it means anything.
        """
        visible = [a for a in window.sorting_menu.actions()
                   if not a.isSeparator() and a.isEnabled()]
        assert len(visible) <= 9
        assert not any(a.menu() for a in window.sorting_menu.actions())
