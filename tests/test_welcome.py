"""First-run setup: linking mailboxes, and choosing which folders to build.

The wizard used to ask for an iCloud address and an app-specific password and
nothing else, so somebody whose mail is on Gmail could not finish it, and
nobody could link a second mailbox without going to Settings afterwards — even
though the app has supported both for a long time.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

import accounts  # noqa: E402
import profiles  # noqa: E402
from config import InMemoryCredentialStore, Settings  # noqa: E402
from welcome import SetupWizard  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def wizard(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
    store = InMemoryCredentialStore()
    subject = SetupWizard(Settings(), store)
    yield subject, store
    subject.deleteLater()


def link(page, preset: str, address: str, secret: str) -> None:
    page.preset.setCurrentIndex(page.preset.findData(preset))
    page.email.setText(address)
    page.password.setText(secret)


class TestLinkingMailboxes:
    def test_every_provider_the_app_supports_is_offered(self, wizard):
        subject, _ = wizard
        offered = {subject.mailbox.preset.itemData(i)
                   for i in range(subject.mailbox.preset.count())}
        assert offered == {name for name, _label in accounts.choices()}
        assert {"icloud", "gmail", "outlook"} <= offered

    def test_it_will_not_move_on_until_a_real_address_is_given(self, wizard):
        subject, _ = wizard
        page = subject.mailbox
        assert page.isComplete() is False
        page.email.setText("not an address")
        assert page.isComplete() is False
        page.email.setText("me@gmail.com")
        assert page.isComplete() is True

    def test_the_server_follows_the_provider(self, wizard):
        subject, _ = wizard
        link(subject.mailbox, "gmail", "me@gmail.com", "aaaa bbbb cccc dddd")
        assert subject.mailbox.ready_accounts()[0].host == "imap.gmail.com"

    def test_several_mailboxes_can_be_linked(self, wizard):
        subject, _ = wizard
        page = subject.mailbox
        link(page, "gmail", "a@gmail.com", "one")
        page._add()
        link(page, "icloud", "b@icloud.com", "two")
        page._add()
        link(page, "outlook", "c@outlook.com", "three")
        assert [a.address for a in page.ready_accounts()] == [
            "a@gmail.com", "b@icloud.com", "c@outlook.com"]
        assert [a.host for a in page.ready_accounts()] == [
            "imap.gmail.com", "imap.mail.me.com", "outlook.office365.com"]

    def test_each_password_is_kept_with_its_own_address(self, wizard):
        """A Gmail password once got filed under an iCloud address."""
        subject, _ = wizard
        page = subject.mailbox
        link(page, "gmail", "a@gmail.com", "gmail-secret")
        page._add()
        link(page, "icloud", "b@icloud.com", "icloud-secret")
        assert page.passwords() == {"a@gmail.com": "gmail-secret",
                                    "b@icloud.com": "icloud-secret"}

    def test_switching_provider_clears_an_address_from_the_old_one(self, wizard):
        """Changing provider is a different mailbox, not a different server."""
        subject, _ = wizard
        page = subject.mailbox
        link(page, "icloud", "me@icloud.com", "secret")
        page.preset.setCurrentIndex(page.preset.findData("gmail"))
        assert page.email.text() == ""
        assert page.password.text() == ""

    def test_a_custom_address_is_left_alone_when_switching(self, wizard):
        """Only an address that plainly belongs elsewhere is cleared."""
        subject, _ = wizard
        page = subject.mailbox
        link(page, "custom", "me@my-own-domain.example", "secret")
        page.preset.setCurrentIndex(page.preset.findData("fastmail"))
        assert page.email.text() == "me@my-own-domain.example"

    def test_the_last_mailbox_cannot_be_removed(self, wizard):
        subject, _ = wizard
        page = subject.mailbox
        page._remove()
        assert len(page._accounts) == 1

    def test_removing_one_of_several_works(self, wizard):
        subject, _ = wizard
        page = subject.mailbox
        link(page, "gmail", "a@gmail.com", "one")
        page._add()
        link(page, "icloud", "b@icloud.com", "two")
        page._remove()
        assert [a.address for a in page.ready_accounts()] == ["a@gmail.com"]

    def test_the_list_says_which_still_need_a_password(self, wizard):
        subject, _ = wizard
        page = subject.mailbox
        page.email.setText("a@gmail.com")
        assert "needs a password" in page.listing.item(0).text()
        page.password.setText("secret")
        assert "ready" in page.listing.item(0).text()


class TestChoosingWhichFoldersToBuild:
    def test_the_page_asks_about_folders(self, wizard):
        subject, _ = wizard
        assert "folders" in subject.purpose.title().lower()

    def test_job_only_everyday_only_and_both_are_all_offered(self, wizard):
        subject, _ = wizard
        offered = {b.property("profile") for b in subject.purpose.choice.buttons()}
        assert {"job_search", "everyday", "everything"} <= offered

    @pytest.mark.parametrize("name, job_tree, everyday_tree", [
        ("job_search", True, False),
        ("everything", True, True),
        ("everyday", False, True),
    ])
    def test_each_choice_builds_what_it_says(self, name, job_tree, everyday_tree):
        profile = profiles.get(name)
        assert profile.makes_job_folders is job_tree
        assert bool(profile.topics) is everyday_tree
        described = profile.creates()
        assert ("for each stage" in described) is job_tree
        assert ("everyday topics" in described) is everyday_tree

    def test_the_description_comes_from_the_profile(self):
        """Typed-out descriptions drift from what the code actually does."""
        for name, _label, _blurb in profiles.choices():
            profile = profiles.get(name)
            assert profile.folder_root in profile.creates()

    def test_topics_are_offered_only_when_they_get_folders(self, wizard):
        subject, _ = wizard
        page = subject.purpose
        for button in page.choice.buttons():
            if button.property("profile") == "job_search":
                button.setChecked(True)
        assert page.topics_box.isVisible() is False
        for button in page.choice.buttons():
            if button.property("profile") == "everything":
                button.setChecked(True)
        assert len(page.chosen_topics()) == 12


class TestWhatSetupWritesDown:
    def test_the_whole_flow_saves(self, wizard):
        subject, store = wizard
        page = subject.mailbox
        link(page, "gmail", "a@gmail.com", "gmail-secret")
        page._add()
        link(page, "outlook", "b@outlook.com", "outlook-secret")
        for button in subject.purpose.choice.buttons():
            if button.property("profile") == "everything":
                button.setChecked(True)

        saved = subject.save()
        assert [a.address for a in saved.mailboxes] == ["a@gmail.com",
                                                        "b@outlook.com"]
        assert saved.sort_profile == "everything"
        assert len(saved.topics) == 12
        assert store.get_icloud_password("a@gmail.com") == "gmail-secret"
        assert store.get_icloud_password("b@outlook.com") == "outlook-secret"

    def test_it_survives_being_reloaded(self, wizard, tmp_path, monkeypatch):
        subject, _ = wizard
        link(subject.mailbox, "gmail", "a@gmail.com", "secret")
        subject.save()
        reloaded = Settings.load()
        assert [a.address for a in reloaded.mailboxes] == ["a@gmail.com"]
        assert reloaded.is_configured() is True

    def test_the_first_mailbox_still_fills_the_older_fields(self, wizard):
        """A good deal of the app reads icloud_email directly."""
        subject, _ = wizard
        link(subject.mailbox, "gmail", "first@gmail.com", "secret")
        subject.mailbox._add()
        link(subject.mailbox, "icloud", "second@icloud.com", "secret")
        assert subject.result_settings().icloud_email == "first@gmail.com"

    def test_an_incomplete_mailbox_is_not_saved(self, wizard):
        subject, _ = wizard
        link(subject.mailbox, "gmail", "a@gmail.com", "secret")
        subject.mailbox._add()          # left blank
        assert len(subject.result_settings().mailboxes) == 1
