"""Which backend the app uses, and when that changes.

The app starts on the offline rules engine. It needs no account, costs
nothing, and sends no message text anywhere, which is the right default
precisely because it asks nothing of anybody. It is not sticky: somebody who
has gone and fetched an API key has said what they want.
"""

from __future__ import annotations

import pytest

import config
import providers
from config import InMemoryCredentialStore, Settings


class TestTheDefault:
    def test_a_fresh_install_runs_on_this_mac(self):
        settings = Settings()
        assert settings.provider == "rules"
        assert settings.needs_api_key is False

    def test_the_default_needs_no_key_and_no_network(self):
        spec = providers.provider_class(providers.DEFAULT_PROVIDER)
        assert spec.needs_api_key is False
        assert spec.on_device is True

    def test_nothing_in_the_defaults_contradicts_that(self):
        """The docs say no message text leaves your Mac by default."""
        settings = Settings()
        spec = providers.provider_class(settings.provider)
        assert spec.on_device, (
            "the README and SECURITY.md both promise the default sends "
            "nothing anywhere")


class TestAKeyChangesIt:
    def test_a_key_adopts_its_backend(self):
        assert config.backend_for_key("rules", "anthropic", "sk-ant-x") == "anthropic"
        assert config.backend_for_key("rules", "gemini", "AIza-x") == "gemini"

    def test_no_key_stays_on_the_rules_engine(self):
        assert config.backend_for_key("rules", "anthropic", "") == "rules"
        assert config.backend_for_key("rules", "anthropic", "   ") == "rules"

    def test_clearing_the_key_hands_the_work_back(self):
        """Rather than leaving a backend that can no longer authenticate."""
        assert config.backend_for_key("anthropic", "anthropic", "") == "rules"

    def test_a_choice_between_two_cloud_backends_is_left_alone(self):
        """That is a preference, not a default."""
        assert config.backend_for_key("anthropic", "gemini", "") == "anthropic"

    def test_a_second_key_switches_to_it(self):
        assert config.backend_for_key("anthropic", "gemini", "AIza-x") == "gemini"


class TestTheSettingsDialog:
    @pytest.fixture
    def dialog(self, qapp, tmp_path, monkeypatch):
        from settings_dialog import SettingsDialog
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        widget = SettingsDialog(Settings(icloud_email="you@icloud.example"),
                                InMemoryCredentialStore())
        yield widget
        widget.deleteLater()

    def test_choosing_a_cloud_backend_without_a_key_stays_offline(self, dialog):
        index = dialog.provider_combo.findData("anthropic")
        dialog.provider_combo.setCurrentIndex(index)
        dialog.api_key_edit.clear()
        assert dialog.collect().provider == "rules"

    def test_choosing_one_and_typing_a_key_uses_it(self, dialog):
        index = dialog.provider_combo.findData("anthropic")
        dialog.provider_combo.setCurrentIndex(index)
        dialog.api_key_edit.setText("sk-ant-not-a-real-key")
        assert dialog.collect().provider == "anthropic"

    def test_an_already_stored_key_counts(self, qapp, tmp_path, monkeypatch):
        from settings_dialog import SettingsDialog
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        store = InMemoryCredentialStore()
        store.set_provider_key("gemini", "AIza-stored")
        widget = SettingsDialog(Settings(icloud_email="you@icloud.example"), store)
        try:
            widget.provider_combo.setCurrentIndex(
                widget.provider_combo.findData("gemini"))
            widget.api_key_edit.clear()
            assert widget.collect().provider == "gemini"
        finally:
            widget.deleteLater()

    def test_the_offline_backends_never_need_one(self, dialog):
        for name in ("rules", "ollama"):
            dialog.provider_combo.setCurrentIndex(
                dialog.provider_combo.findData(name))
            assert dialog.collect().provider == name


class TestTheWizard:
    @pytest.fixture
    def wizard(self, qapp, tmp_path, monkeypatch):
        from welcome import SetupWizard
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        widget = SetupWizard(Settings(), InMemoryCredentialStore())
        yield widget
        widget.deleteLater()

    def test_finishing_without_a_key_leaves_it_offline(self, wizard):
        wizard.classifier.backend.setCurrentIndex(
            wizard.classifier.backend.findData("anthropic"))
        wizard.classifier.key.clear()
        assert wizard.save().provider == "rules"

    def test_finishing_with_a_key_uses_that_backend(self, wizard):
        wizard.classifier.backend.setCurrentIndex(
            wizard.classifier.backend.findData("anthropic"))
        wizard.classifier.key.setText("sk-ant-not-a-real-key")
        saved = wizard.save()
        assert saved.provider == "anthropic"
        assert saved.model == providers.default_model_for("anthropic")

    def test_the_default_path_through_the_wizard_is_offline(self, wizard):
        assert wizard.save().provider == "rules"
