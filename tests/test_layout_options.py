"""Where things sit: the preview pane, and folders per mailbox."""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt

from accounts import Account
from config import InMemoryCredentialStore, Settings
from gui import MainWindow


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
    win = MainWindow(Settings(icloud_email="you@icloud.example"),
                     InMemoryCredentialStore())
    win.resize(1400, 900)
    yield win
    win.close()


class TestWhereThePreviewSits:
    def test_it_starts_below(self, window):
        assert window.splitter.orientation() is Qt.Orientation.Vertical

    def test_it_can_go_beside(self, window):
        window.set_preview_position("right")
        assert window.splitter.orientation() is Qt.Orientation.Horizontal
        assert window.settings.preview_position == "right"

    def test_it_comes_back(self, window):
        window.set_preview_position("right")
        window.set_preview_position("below")
        assert window.splitter.orientation() is Qt.Orientation.Vertical

    def test_the_menu_follows(self, window):
        window.set_preview_position("right")
        assert window.preview_actions["right"].isChecked()
        assert not window.preview_actions["below"].isChecked()

    def test_nonsense_is_ignored(self, window):
        window.set_preview_position("sideways-ish")
        assert window.settings.preview_position == "below"

    def test_the_log_stays_along_the_bottom(self, window):
        """It is the outer splitter, so moving the preview never moves it."""
        window.set_preview_position("right")
        assert window.outer_splitter.orientation() is Qt.Orientation.Vertical
        assert window.outer_splitter.indexOf(window.log_view) == 1

    def test_the_log_still_opens(self, window):
        window._toggle_log(True)
        assert window.log_view.isVisibleTo(window.outer_splitter)
        assert window.outer_splitter.sizes()[-1] >= 60

    def test_both_splitters_are_remembered(self, window):
        window.set_preview_position("right")
        window._toggle_log(True)
        window.close()   # closing is what writes the layout down
        assert window.settings.splitter_state
        assert window.settings.log_splitter_state

    def test_a_stale_three_pane_state_does_not_break_anything(self, tmp_path,
                                                              qapp, monkeypatch):
        """Settings written before the split had three sections, not two."""
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        settings = Settings(icloud_email="you@icloud.example",
                            splitter_state="bm90IGEgcmVhbCBzdGF0ZQ==")
        win = MainWindow(settings, InMemoryCredentialStore())
        assert win.splitter.count() == 2
        win.close()


class TestFoldersPerMailbox:
    def test_a_mailbox_with_no_opinion_uses_the_shared_roots(self):
        account = Account(address="you@icloud.example")
        assert account.roots("Job Search", "Sorted Mail") == \
            ("Job Search", "Sorted Mail")

    def test_a_mailbox_can_have_its_own(self):
        account = Account(address="work@acme.example", folder_root="Career",
                          other_folder_root="Filed")
        assert account.roots("Job Search", "Sorted Mail") == ("Career", "Filed")

    def test_one_of_each_is_allowed(self):
        account = Account(address="work@acme.example", folder_root="Career")
        assert account.roots("Job Search", "Sorted Mail") == \
            ("Career", "Sorted Mail")

    def test_whitespace_is_not_an_opinion(self):
        account = Account(address="a@b.example", folder_root="   ")
        assert account.roots("Job Search", "Sorted Mail")[0] == "Job Search"

    def test_it_survives_a_round_trip_through_settings(self, tmp_path):
        path = tmp_path / "settings.json"
        settings = Settings(mailboxes=[
            Account(address="work@acme.example", folder_root="Career")])
        settings.save(path)
        again = Settings.load(path)
        assert again.mailboxes[0].folder_root == "Career"

    def test_the_dialog_shows_and_captures_it(self, qapp, tmp_path, monkeypatch):
        from settings_dialog import SettingsDialog
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        settings = Settings(mailboxes=[
            Account(address="work@acme.example", folder_root="Career")])
        dialog = SettingsDialog(settings, InMemoryCredentialStore())
        try:
            assert dialog.account_root_edit.text() == "Career"
            dialog.account_root_edit.setText("Somewhere Else")
            assert dialog.collect().mailboxes[0].folder_root == "Somewhere Else"
        finally:
            dialog.deleteLater()

    def test_the_placeholder_shows_what_it_would_inherit(self, qapp, tmp_path,
                                                        monkeypatch):
        from settings_dialog import SettingsDialog
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        dialog = SettingsDialog(
            Settings(mailboxes=[Account(address="a@b.example")],
                     folder_root="Hunting"),
            InMemoryCredentialStore())
        try:
            assert dialog.account_root_edit.placeholderText() == "Hunting"
            dialog.root_edit.setText("Something New")
            assert dialog.account_root_edit.placeholderText() == "Something New"
        finally:
            dialog.deleteLater()
