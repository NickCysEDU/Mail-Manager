"""Mailboxes: presets, migration from the single-mailbox era, and selection."""

from __future__ import annotations

import json

import pytest

import accounts
import config
import profiles
from accounts import Account
from config import Settings


class TestGuessingTheProvider:
    @pytest.mark.parametrize("address, preset, host", [
        ("someone@icloud.com", "icloud", "imap.mail.me.com"),
        ("someone@me.com", "icloud", "imap.mail.me.com"),
        ("someone@gmail.com", "gmail", "imap.gmail.com"),
        ("someone@outlook.com", "outlook", "outlook.office365.com"),
        ("someone@hotmail.com", "outlook", "outlook.office365.com"),
        ("someone@yahoo.com", "yahoo", "imap.mail.yahoo.com"),
        ("someone@fastmail.com", "fastmail", "imap.fastmail.com"),
        ("someone@proton.me", "proton", "127.0.0.1"),
    ])
    def test_the_domain_settles_it(self, address, preset, host):
        account = Account.for_address(address)
        assert account.preset == preset
        assert account.host == host

    def test_an_unknown_domain_needs_a_host_typed_in(self):
        account = Account.for_address("someone@example.org")
        assert account.preset == "custom"
        assert account.host == ""
        assert account.is_configured is False

    def test_proton_uses_the_bridge_port(self):
        assert Account.for_address("someone@proton.me").port == 1143


class TestAccountIdentity:
    def test_the_handle_is_derived_from_the_address(self):
        """Settings are rebuilt on every load; a random handle would not survive."""
        assert Account.for_address("a@b.com").id == Account.for_address("a@b.com").id

    def test_an_account_with_no_address_still_gets_one(self):
        assert Account().id != Account().id

    def test_accounts_are_told_apart_in_the_table(self):
        pair = [Account.for_address("me@icloud.com"),
                Account.for_address("me@gmail.com")]
        accounts.assign_colors(pair)
        accounts.unique_labels(pair)
        assert pair[0].color != pair[1].color
        assert pair[0].label != pair[1].label


class TestMigratingFromOneMailbox:
    def test_an_old_settings_file_grows_a_mailbox(self):
        """The app shipped with three flat fields. Nobody should lose them."""
        old = {"icloud_email": "me@icloud.com", "imap_host": "imap.mail.me.com",
               "imap_port": 993, "source_mailbox": "Archive"}
        settled = Settings.from_dict(old)
        assert [a.address for a in settled.accounts] == ["me@icloud.com"]
        assert settled.accounts[0].source_mailbox == "Archive"

    def test_the_flat_fields_still_describe_the_first_mailbox(self):
        """Plenty of code still reads them, so they have to stay truthful."""
        settled = Settings.from_dict({"icloud_email": "me@icloud.com"})
        settled.mailboxes.insert(0, Account.for_address("work@gmail.com"))
        settled = settled.normalized()
        assert settled.icloud_email == "work@gmail.com"
        assert settled.imap_host == "imap.gmail.com"

    def test_a_settings_file_survives_a_round_trip(self):
        settled = Settings.from_dict({"icloud_email": "me@icloud.com"})
        settled.mailboxes.append(Account.for_address("me@gmail.com"))
        settled = settled.normalized()
        again = Settings.from_dict(json.loads(json.dumps(settled.to_dict())))
        assert [a.address for a in again.accounts] == \
            [a.address for a in settled.accounts]
        assert [a.id for a in again.accounts] == [a.id for a in settled.accounts]

    def test_settings_built_in_code_still_connect(self):
        """primary_account has to hold up when no mailbox list was ever built."""
        assert Settings().primary_account.host == config.DEFAULT_IMAP_HOST


class TestChoosingWhichMailboxToScan:
    @pytest.fixture
    def two(self):
        settled = Settings.from_dict({"icloud_email": "me@icloud.com"})
        settled.mailboxes.append(Account.for_address("me@gmail.com"))
        return settled.normalized()

    def test_all_of_them_by_default(self, two):
        assert len(two.scan_accounts) == 2
        assert two.multi_account is True

    def test_one_can_be_singled_out(self, two):
        two.active_accounts = [two.accounts[1].id]
        assert [a.address for a in two.normalized().scan_accounts] == ["me@gmail.com"]

    def test_a_stale_selection_falls_back_to_all(self, two):
        two.active_accounts = ["no-such-mailbox"]
        assert len(two.normalized().scan_accounts) == 2

    def test_a_disabled_mailbox_is_skipped(self, two):
        two.mailboxes[1].enabled = False
        settled = two.normalized()
        assert [a.address for a in settled.scan_accounts] == ["me@icloud.com"]
        assert settled.multi_account is False


class TestCredentialHints:
    def test_a_known_host_names_its_own_password_page(self):
        hint = accounts.credential_hint("imap.gmail.com")
        assert "Gmail" in hint and "apppasswords" in hint

    def test_an_unknown_host_says_something_useful_anyway(self):
        assert "app password" in accounts.credential_hint("mail.example.org")


class TestSortingProfiles:
    def test_the_default_is_what_the_app_always_did(self):
        settled = Settings()
        assert settled.sort_profile == profiles.DEFAULT_PROFILE
        assert settled.folder_plan().all_folders[0] == "Job Search"

    def test_an_unknown_profile_falls_back(self):
        assert Settings.from_dict({"sort_profile": "nonsense"}).sort_profile \
            == profiles.DEFAULT_PROFILE

    def test_everyday_files_job_mail_in_one_place(self):
        """One folder rather than seven - and still the job folder.

        It used to move the whole tree under Sorted Mail, which put a second
        "Job Search" beside the topics and left the first one stranded.
        """
        settled = Settings.from_dict({"sort_profile": "everyday"})
        folders = settled.folder_plan().all_folders
        assert folders[0] == "Job Search"
        assert "Job Search/Interview" not in folders
        assert not any(f.startswith("Sorted Mail") for f in folders)

    def test_essentials_leaves_the_rest_alone(self):
        plan = Settings.from_dict({"sort_profile": "essentials"}).folder_plan()
        chosen = profiles.get("essentials").topics
        assert len(plan.other_folders(chosen)) - 1 == len(chosen)
        from models import OtherCategory
        # A topic outside the list is filed under Other, not given a mailbox.
        assert plan.for_other_category(OtherCategory.SPAM).endswith("Other")
