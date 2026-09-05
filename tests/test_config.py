"""Settings persistence and Keychain-backed credentials."""

from __future__ import annotations

import json
import os
import stat

import pytest

from config import (
    ANTHROPIC_ACCOUNT,
    CredentialError,
    CredentialStore,
    InMemoryCredentialStore,
    Settings,
    app_support_dir,
    log_dir,
    parse_iso,
    settings_path,
)
from models import NonJobRouting, TimeWindow


class TestPaths:
    def test_honours_the_home_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path / "custom"))
        assert app_support_dir() == tmp_path / "custom"
        assert settings_path() == tmp_path / "custom" / "settings.json"
        assert log_dir() == tmp_path / "custom" / "Logs"

    def test_defaults_to_application_support_on_macos(self, monkeypatch):
        monkeypatch.delenv("ICLOUD_TRIAGE_HOME", raising=False)
        monkeypatch.setattr("sys.platform", "darwin")
        assert "Application Support" in str(app_support_dir())


class TestSettingsRoundTrip:
    def test_defaults_are_sane(self):
        settings = Settings()
        assert settings.imap_host == "imap.mail.me.com"
        assert settings.imap_port == 993
        # A fresh install runs offline: no key, no cost, no setup.
        assert settings.provider == "rules"
        assert settings.model == "rules-v1"
        assert settings.needs_api_key is False
        assert settings.confidence_threshold == 0.95
        assert settings.batch_size == 6
        assert settings.fallback_to_rules is True
        assert settings.routing is NonJobRouting.LEAVE
        assert settings.auto_approve_non_job is False

    def test_save_and_load(self, tmp_path):
        original = Settings(icloud_email="you@icloud.example", model="claude-sonnet-5", concurrency=6)
        path = original.save(tmp_path / "settings.json")
        loaded = Settings.load(path)
        assert loaded.icloud_email == "you@icloud.example"
        assert loaded.model == "claude-sonnet-5"
        assert loaded.concurrency == 6

    def test_every_field_survives_a_round_trip(self, tmp_path):
        original = Settings(
            icloud_email="a@b.com", source_mailbox="Archive", effort="xhigh",
            folder_root="Hunt", other_folder_root="Buckets",
            non_job_routing=NonJobRouting.FILE.value, auto_approve_non_job=True,
            last_window=TimeWindow.LAST_7_DAYS.name, max_messages=999,
            hide_non_job=True, show_log_panel=True,
        )
        path = original.save(tmp_path / "s.json")
        assert Settings.load(path) == original.normalized()

    def test_missing_file_yields_defaults(self, tmp_path):
        assert Settings.load(tmp_path / "nope.json") == Settings()

    def test_corrupt_file_yields_defaults_without_raising(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text("{not json at all")
        assert Settings.load(path) == Settings()

    def test_non_object_json_yields_defaults(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text("[1, 2, 3]")
        assert Settings.load(path) == Settings()

    def test_unknown_keys_from_a_future_version_are_ignored(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"icloud_email": "a@b.com", "quantum_mode": True}))
        assert Settings.load(path).icloud_email == "a@b.com"

    def test_the_file_is_written_atomically_and_privately(self, tmp_path):
        path = Settings(icloud_email="a@b.com").save(tmp_path / "settings.json")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert not list(tmp_path.glob(".settings-*"))

    def test_parent_directories_are_created(self, tmp_path):
        path = Settings().save(tmp_path / "deep" / "nested" / "settings.json")
        assert path.exists()

    def test_secrets_are_never_written_to_disk(self, tmp_path):
        path = Settings(icloud_email="you@icloud.example").save(tmp_path / "s.json")
        content = path.read_text()
        assert "password" not in content.lower()
        assert "api_key" not in content.lower()


class TestSettingsValidation:
    @pytest.mark.parametrize(
        "field,value,expected",
        [
            ("imap_port", 0, 993),
            ("imap_port", 999999, 993),
            ("imap_port", "not a port", 993),
            ("imap_port", 143, 143),
            ("concurrency", 0, 1),
            ("concurrency", 500, 16),
            ("batch_size", 0, 1),
            ("batch_size", 500, 25),
            ("confidence_threshold", 2.0, 1.0),
            ("confidence_threshold", 0.1, 0.5),
            ("confidence_threshold", "bad", 0.95),
            ("max_body_chars", 5, 500),
            ("max_body_chars", 10 ** 9, 200000),
            ("max_messages", 0, 1),
            ("max_messages", 10 ** 6, 5000),
        ],
    )
    def test_out_of_range_values_are_clamped(self, field, value, expected):
        settings = Settings(**{field: value}).normalized()
        assert getattr(settings, field) == expected

    @pytest.mark.parametrize(
        "field,default",
        [("imap_host", "imap.mail.me.com"), ("source_mailbox", "INBOX"),
         ("model", "rules-v1"), ("folder_root", "Job Search"),
         ("other_folder_root", "Sorted Mail")],
    )
    def test_blank_strings_fall_back_to_defaults(self, field, default):
        assert getattr(Settings(**{field: "   "}).normalized(), field) == default

    def test_an_invalid_port_falls_back_rather_than_clamping_to_1(self):
        assert Settings(imap_port=0).normalized().imap_port == 993

    def test_unknown_effort_falls_back(self):
        assert Settings(effort="turbo").normalized().effort == "medium"

    def test_unknown_window_falls_back(self):
        assert Settings(last_window="LAST_CENTURY").normalized().window is TimeWindow.LAST_24_HOURS

    def test_unknown_routing_falls_back_to_the_safe_option(self):
        assert Settings(non_job_routing="DELETE_EVERYTHING").normalized().routing is NonJobRouting.LEAVE

    def test_email_is_trimmed(self):
        assert Settings(icloud_email="  you@icloud.example  ").normalized().icloud_email == "you@icloud.example"

    def test_is_configured(self):
        assert not Settings().is_configured()
        assert Settings(icloud_email="a@b.com").is_configured()

    def test_every_backend_offers_real_models(self):
        import providers

        for name in providers.PROVIDERS_BY_NAME:
            spec = providers.provider_class(name)
            assert spec.models, f"{name} offers no models"
            assert spec.default_model in {c.value for c in spec.models}
            for choice in spec.models:
                assert choice.value and choice.label


class TestProviderSettings:
    def test_an_unknown_backend_falls_back_to_the_offline_engine(self):
        assert Settings(provider="skynet").normalized().provider == "rules"

    @pytest.mark.parametrize("provider", ["anthropic", "gemini", "openai", "ollama"])
    def test_an_empty_model_resolves_to_that_backend_default(self, provider):
        import providers

        settings = Settings(provider=provider, model="").normalized()
        assert settings.model == providers.default_model_for(provider)

    def test_a_custom_model_is_left_alone(self):
        assert Settings(provider="ollama", model="mistral:7b").normalized().model == "mistral:7b"

    def test_local_backends_need_no_key(self):
        assert Settings(provider="ollama").normalized().needs_api_key is False
        assert Settings(provider="ollama").normalized().on_device is True
        assert Settings(provider="gemini").normalized().needs_api_key is True

    def test_provider_label(self):
        assert "Gemini" in Settings(provider="gemini").normalized().provider_label


class TestPerProviderCredentials:
    def store(self):
        from test_config import FakeKeyring

        return CredentialStore(backend=FakeKeyring())

    def test_each_backend_has_its_own_slot(self):
        store = self.store()
        store.set_provider_key("anthropic", "sk-ant-1")
        store.set_provider_key("gemini", "AIza-2")
        store.set_provider_key("openai", "sk-3")
        assert store.get_provider_key("anthropic") == "sk-ant-1"
        assert store.get_provider_key("gemini") == "AIza-2"
        assert store.get_provider_key("openai") == "sk-3"

    def test_anthropic_keeps_its_original_keychain_account(self):
        """An existing Keychain entry must keep working after this change."""
        assert CredentialStore.provider_account("anthropic") == ANTHROPIC_ACCOUNT
        assert CredentialStore.provider_account("gemini") == "apikey:gemini"

    @pytest.mark.parametrize(
        "provider,variable",
        [("anthropic", "ANTHROPIC_API_KEY"), ("gemini", "GEMINI_API_KEY"),
         ("gemini", "GOOGLE_API_KEY"), ("openai", "OPENAI_API_KEY")],
    )
    def test_environment_fallbacks(self, provider, variable, monkeypatch):
        monkeypatch.setenv(variable, "from-env")
        assert self.store().get_provider_key(provider) == "from-env"

    def test_a_stored_key_beats_the_environment(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "from-env")
        store = self.store()
        store.set_provider_key("gemini", "stored")
        assert store.get_provider_key("gemini") == "stored"

    def test_a_local_backend_has_no_key(self):
        assert self.store().get_provider_key("ollama") == ""


class TestParseIso:
    def test_valid(self):
        assert parse_iso("2026-09-04T12:00:00").year == 2026

    @pytest.mark.parametrize("raw", ["", "not a date", "2026-13-45"])
    def test_invalid_returns_none(self, raw):
        assert parse_iso(raw) is None


class FakeKeyring:
    """Stand-in for the ``keyring`` module."""

    def __init__(self, fail: bool = False) -> None:
        self.data = {}
        self.fail = fail

    def get_password(self, service, account):
        if self.fail:
            raise RuntimeError("keychain locked")
        return self.data.get((service, account))

    def set_password(self, service, account, secret):
        if self.fail:
            raise RuntimeError("keychain locked")
        self.data[(service, account)] = secret

    def delete_password(self, service, account):
        if (service, account) not in self.data:
            raise RuntimeError("no such password")
        del self.data[(service, account)]

    def get_keyring(self):
        return self


class TestCredentialStore:
    def store(self, **kwargs):
        return CredentialStore(backend=FakeKeyring(**kwargs))

    def test_icloud_password_round_trip(self):
        store = self.store()
        store.set_icloud_password("You@iCloud.Example", "abcd-efgh")
        assert store.get_icloud_password("you@icloud.example") == "abcd-efgh"

    def test_account_key_is_case_insensitive(self):
        assert CredentialStore.icloud_account("A@B.COM") == CredentialStore.icloud_account("a@b.com")

    def test_anthropic_key_round_trip(self):
        store = self.store()
        store.set_anthropic_key("sk-ant-123")
        assert store.get_anthropic_key() == "sk-ant-123"

    def test_anthropic_key_falls_back_to_the_environment(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
        assert self.store().get_anthropic_key() == "sk-ant-from-env"

    def test_a_stored_key_beats_the_environment(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
        store = self.store()
        store.set_anthropic_key("sk-ant-stored")
        assert store.get_anthropic_key() == "sk-ant-stored"

    def test_saving_an_empty_secret_deletes_it(self):
        store = self.store()
        store.set_anthropic_key("sk-ant-1")
        store.set_anthropic_key("")
        assert store.get(ANTHROPIC_ACCOUNT) == ""

    def test_deleting_a_missing_secret_is_not_an_error(self):
        self.store().delete("nothing-here")

    def test_missing_email_is_rejected(self):
        with pytest.raises(CredentialError):
            self.store().set_icloud_password("", "pw")

    def test_blank_email_reads_as_empty(self):
        assert self.store().get_icloud_password("") == ""

    def test_a_locked_keychain_produces_a_clear_error(self):
        with pytest.raises(CredentialError, match="Keychain"):
            self.store(fail=True).get("anything")

    def test_backend_name_is_reported(self):
        assert "FakeKeyring" in self.store().backend_name()

    def test_availability(self):
        assert self.store().available() is True


class TestInMemoryStore:
    def test_behaves_like_the_real_thing(self):
        store = InMemoryCredentialStore()
        store.set_icloud_password("a@b.com", "pw")
        store.set_anthropic_key("sk-ant-x")
        assert store.get_icloud_password("a@b.com") == "pw"
        assert store.get_anthropic_key() == "sk-ant-x"
        assert store.available()
        assert "in-memory" in store.backend_name()

    def test_is_not_persistent(self):
        InMemoryCredentialStore().set_anthropic_key("sk-ant-x")
        assert InMemoryCredentialStore().get_anthropic_key() == ""
