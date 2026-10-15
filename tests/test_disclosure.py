"""What a model elsewhere is sent, said once before it is sent anything."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QDialog, QMessageBox  # noqa: E402

import disclosure  # noqa: E402
import providers  # noqa: E402
from config import InMemoryCredentialStore, Settings  # noqa: E402


@pytest.fixture
def answered(monkeypatch):
    """Press the dialog's own "Use ..." button, and keep what it said."""
    said = []

    def use(self):
        said.append(self.text() + "\n" + self.informativeText())
        for button in self.buttons():
            if self.buttonRole(button) == QMessageBox.ButtonRole.AcceptRole:
                button.click()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", use)
    return said


class TestWhatLeavesThisMac:
    @pytest.mark.parametrize("name, url, away", [
        ("rules", "", False),
        ("ollama", "", False),
        ("ollama", "http://192.168.1.5:11434", False),
        ("anthropic", "", True),
        ("gemini", "", True),
        ("openai", "", True),
        ("openai", "http://localhost:1234/v1", False),
        ("openai", "https://api.groq.example/openai/v1", True),
        ("openai", "http://127.0.0.1.evil.example/v1", True),
    ])
    def test_which_backends_send_mail_away(self, name, url, away):
        assert providers.leaves_this_mac(name, url) is away


class TestItIsSaidOnce:
    def test_a_backend_here_is_not_asked_about(self, qapp, dialog_calls):
        settings = Settings()
        assert disclosure.ask(None, settings, "rules")
        assert disclosure.ask(None, settings, "openai", "http://localhost:1234/v1")
        assert dialog_calls == [] and settings.told_what_is_sent == []

    def test_cancelled_it_is_not_chosen_or_remembered(self, qapp, dialog_calls):
        settings = Settings()
        assert not disclosure.ask(None, settings, "gemini")
        assert settings.told_what_is_sent == []
        assert [kind for kind, _title, _text in dialog_calls] == ["exec"]

    def test_agreed_it_is_remembered_and_not_asked_again(self, qapp, answered):
        settings = Settings()
        assert disclosure.ask(None, settings, "anthropic")
        assert settings.told_what_is_sent == ["anthropic"]
        assert disclosure.ask(None, settings, "anthropic")
        assert len(answered) == 1
        assert Settings.from_dict(settings.to_dict()).told_what_is_sent == ["anthropic"]

    def test_it_says_what_is_sent(self, qapp, answered):
        disclosure.ask(None, Settings(), "anthropic")
        said = answered[0]
        for part in ("who sent it and to whom", "subject and date",
                     "names of its attachments", "where its links go",
                     "Codes, passwords, keys and the secret parts of links are taken out",
                     "Attachments are never sent", "in the background"):
            assert part in said, part
        assert "free tier" not in said

    def test_geminis_free_tier_is_named(self, qapp, answered):
        disclosure.ask(None, Settings(), "gemini")
        assert "free tier" in answered[0]
        assert providers.GeminiProvider.privacy_url in answered[0]


class TestSettings:
    @pytest.fixture
    def dialog(self, qapp, qtbot):
        from settings_dialog import SettingsDialog

        made = SettingsDialog(Settings(icloud_email="you@icloud.example").normalized(),
                              InMemoryCredentialStore())
        qtbot.addWidget(made)
        return made

    def _choose(self, dialog, name):
        dialog.provider_combo.setCurrentIndex(dialog.provider_combo.findData(name))

    def test_geminis_terms_are_warned_of_where_it_is_chosen(self, dialog):
        self._choose(dialog, "gemini")
        assert not dialog.provider_warning.isHidden()
        assert "free tier" in dialog.provider_warning.text()
        for name in ("anthropic", "rules"):
            self._choose(dialog, name)
            assert dialog.provider_warning.isHidden()

    def test_ok_waits_until_it_has_been_said(self, dialog, dialog_calls):
        self._choose(dialog, "gemini")
        dialog.done(QDialog.DialogCode.Accepted)
        assert dialog.result() != QDialog.DialogCode.Accepted
        assert dialog.collect().told_what_is_sent == []

    def test_agreed_ok_goes_ahead_and_it_is_kept(self, dialog, answered):
        self._choose(dialog, "gemini")
        dialog.done(QDialog.DialogCode.Accepted)
        assert dialog.result() == QDialog.DialogCode.Accepted
        assert dialog.collect().told_what_is_sent == ["gemini"]

    def test_cancel_asks_nothing(self, dialog, dialog_calls):
        self._choose(dialog, "gemini")
        dialog.done(QDialog.DialogCode.Rejected)
        assert dialog_calls == []


class TestTheModelMenu:
    @pytest.fixture
    def window(self, qapp, tmp_path, monkeypatch):
        from gui import MainWindow

        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        made = MainWindow(Settings(icloud_email="you@icloud.example").normalized(),
                          InMemoryCredentialStore())
        yield made
        made.close()
        made.deleteLater()

    def _ticked(self, window):
        return [(section.menu().title(), action.text())
                for section in window.model_menu.actions() if section.menu()
                for action in section.menu().actions() if action.isChecked()]

    def _gemini(self, window):
        return next(section.menu().actions()[0]
                    for section in window.model_menu.actions()
                    if section.menu() and "Gemini" in section.menu().title())

    def test_cancelled_the_backend_stays(self, window, dialog_calls):
        """Chosen from the menu, which ticks the entry as it is clicked."""
        before = self._ticked(window)
        self._gemini(window).trigger()
        assert window.settings.provider == "rules"
        assert Settings.load().told_what_is_sent == []
        assert self._ticked(window) == before

    def test_agreed_it_switches_and_is_kept(self, window, answered):
        self._gemini(window).trigger()
        assert window.settings.provider == "gemini"
        assert Settings.load().told_what_is_sent == ["gemini"]


class TestSettingUp:
    def test_the_backend_page_asks_before_going_on(self, qapp, qtbot, dialog_calls,
                                                   tmp_path, monkeypatch):
        from welcome import SetupWizard

        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        wizard = SetupWizard(Settings(), InMemoryCredentialStore())
        qtbot.addWidget(wizard)
        page = wizard.classifier
        page.backend.setCurrentIndex(page.backend.findData("rules"))
        assert page.validatePage() and dialog_calls == []
        page.backend.setCurrentIndex(page.backend.findData("gemini"))
        assert "free tier" in page.note.text()
        assert not page.validatePage()
        assert wizard.result_settings().told_what_is_sent == []
