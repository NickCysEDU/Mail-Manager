"""The About window: what the app says about itself.

It is the one place a person can go to find out what this is, where their
mail goes, and who to tell when something is wrong. All three have to be
true, and the security route has to work.
"""

from __future__ import annotations

import pytest

import about
from config import InMemoryCredentialStore, Settings


@pytest.fixture
def dialog(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
    widget = about.AboutDialog(Settings(icloud_email="you@icloud.example"),
                               InMemoryCredentialStore())
    yield widget
    widget.deleteLater()


def text_of(widget) -> str:
    from PySide6.QtWidgets import QLabel
    return "\n".join(label.text() for label in widget.findChildren(QLabel))


class TestWhatItSays:
    def test_it_names_the_app_and_the_build(self, dialog):
        import buildinfo
        from models import APP_DISPLAY_NAME
        body = text_of(dialog)
        assert APP_DISPLAY_NAME in body
        assert buildinfo.short() in body

    def test_it_says_nothing_moves_without_you(self, dialog):
        assert "Nothing moves until you press Apply" in text_of(dialog)

    def test_it_says_where_the_mail_goes(self, dialog):
        """On the default backend, that is nowhere."""
        body = text_of(dialog)
        assert "No message text leaves the machine" in body
        assert "Keychain" in body

    def test_a_cloud_backend_says_so_instead(self, qapp, tmp_path, monkeypatch):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        settings = Settings(icloud_email="you@icloud.example",
                            provider="anthropic", model="claude-haiku-4-5")
        widget = about.AboutDialog(settings, InMemoryCredentialStore())
        try:
            body = text_of(widget)
            assert "is sent to" in body
            assert "Attachments are never uploaded" in body
        finally:
            widget.deleteLater()

    def test_it_reports_the_settings_that_matter(self, dialog):
        body = text_of(dialog)
        assert "95% confidence" in body
        assert "Local rules" in body


class TestTheDisclosure:
    def test_it_is_shown_in_full(self, dialog):
        assert about.AI_DISCLOSURE in text_of(dialog).replace("<i>", "").replace("</i>", "")

    def test_it_says_what_it_is_meant_to_say(self):
        for phrase in ("AI-assisted tools", "limited supporting role",
                       "coding assistance", "copy editing",
                       "sample display content"):
            assert phrase in about.AI_DISCLOSURE, phrase

    def test_the_docs_carry_the_same_words(self):
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        for name in ("README.md", "docs/HANDBOOK.md"):
            text = (root / name).read_text(encoding="utf-8")
            # The docs wrap it; compare on collapsed whitespace.
            assert " ".join(about.AI_DISCLOSURE.split()) in \
                " ".join(text.split()), name


class TestTheLinks:
    def test_every_link_is_under_the_repository(self):
        for url in (about.SECURITY_URL, about.ISSUES_URL,
                    about.LICENCE_URL, about.HANDBOOK_URL):
            assert url.startswith(about.REPOSITORY), url

    def test_the_security_route_is_a_private_advisory(self):
        """Not a public issue: a vulnerability should not be filed in the open."""
        assert about.SECURITY_URL.endswith("/security/advisories/new")

    def test_the_buttons_are_wired(self, dialog, monkeypatch):
        from PySide6.QtWidgets import QPushButton
        opened = []
        monkeypatch.setattr(dialog, "_open", opened.append)
        labels = {b.text(): b for b in dialog.findChildren(QPushButton)}
        for label in ("Security concern…", "Report a bug…", "Source code…"):
            assert label in labels, label
            labels[label].click()
        assert opened == [about.SECURITY_URL, about.ISSUES_URL, about.REPOSITORY]

    def test_every_button_explains_itself(self, dialog):
        from PySide6.QtWidgets import QPushButton
        for button in dialog.findChildren(QPushButton):
            if button.text() == "Close":
                continue
            assert button.toolTip(), button.text()

    def test_copying_the_build_puts_it_on_the_clipboard(self, dialog):
        from PySide6.QtGui import QGuiApplication
        import buildinfo
        dialog._copy_build()
        assert QGuiApplication.clipboard().text() == buildinfo.full()


class TestItOpensFromTheWindow:
    def test_the_help_menu_has_it(self, qapp, tmp_path, monkeypatch):
        from PySide6.QtWidgets import QMenu
        from gui import MainWindow
        from models import APP_DISPLAY_NAME
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        window = MainWindow(Settings(icloud_email="you@icloud.example"),
                            InMemoryCredentialStore())
        try:
            titles = []
            for menu in window.menuBar().findChildren(QMenu):
                titles += [a.text() for a in menu.actions()]
            assert f"About {APP_DISPLAY_NAME}" in titles
        finally:
            window.close()

    def test_it_builds_without_raising(self, qapp, tmp_path, monkeypatch):
        from gui import MainWindow
        from PySide6.QtWidgets import QDialog
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        window = MainWindow(Settings(icloud_email="you@icloud.example"),
                            InMemoryCredentialStore())
        monkeypatch.setattr(QDialog, "exec", lambda self: 0)
        try:
            window._about()      # must not raise
        finally:
            window.close()


class TestItSaysWhatIsKeptAndHow:
    """verdicts.json writes "see About for why" when it cannot seal.

    That is a promise about this window, so the window has to keep it - in
    both states, and without mangling the one proper noun in the sentence.
    """

    def test_the_sealed_case_names_the_keychain_properly(self, qtbot, monkeypatch):
        import about
        import vault

        class Sealed:
            def describe(self):
                return "Encrypted with a key in your Keychain."

        monkeypatch.setattr(vault, "shared", lambda: Sealed())
        said = about.AboutDialog._at_rest(object())
        assert "Keychain" in said, "lowercased a product name"
        assert "keychain" not in said.replace("Keychain", "")

    def test_the_degraded_case_explains_itself(self, qtbot, monkeypatch):
        import about
        import vault

        class Unsealed:
            def describe(self):
                return ("Not encrypted: the Keychain is unavailable, so "
                        "summaries are not written to disk at all.")

        monkeypatch.setattr(vault, "shared", lambda: Unsealed())
        said = about.AboutDialog._at_rest(object())
        assert "not encrypted" in said
        assert "not written to disk" in said
        assert "Keychain" in said

    def test_the_placeholder_points_somewhere_real(self):
        """The string the cache writes has to match what About offers."""
        import verdict_cache

        entry = verdict_cache.Entry(key="k", recipe="r",
                                    payload={"summary": "s", "reasoning": "r"},
                                    model="m", when="2026-01-01T00:00:00")
        blanked = entry.to_dict(with_text=False)
        assert blanked["payload"]["summary"] == ""
        assert "About" in blanked["payload"]["reasoning"]
