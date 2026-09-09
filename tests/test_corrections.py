"""What the app remembers about being corrected."""

from __future__ import annotations

import json

import pytest

import corrections
from corrections import Correction, Memory, domain_of, is_shared_host


def teach(memory: Memory, sender: str, folder: str, suggested: str = "") -> bool:
    return memory.remember_move(sender, folder, suggested=suggested)


class TestAddressNormalising:
    def test_case_is_folded(self):
        memory = Memory()
        teach(memory, "Jane@Acme.COM", "Job Search/Interviews")
        assert memory.lookup("jane@acme.com").folder == "Job Search/Interviews"

    def test_angle_brackets_are_stripped(self):
        memory = Memory()
        teach(memory, "<jane@acme.com>", "Interviews")
        assert memory.lookup("jane@acme.com") is not None

    @pytest.mark.parametrize("bad", [
        "", "   ", "not-an-address", "two@at@signs.com", "@acme.com",
        "jane@", "jane@localhost",
    ])
    def test_nonsense_is_not_stored(self, bad):
        memory = Memory()
        assert teach(memory, bad, "Interviews") is False
        assert len(memory) == 0

    def test_domain_keeps_the_robot_subdomain(self):
        assert domain_of("no-reply@mail.greenhouse.io") == "mail.greenhouse.io"


class TestWhenItSpeaks:
    def test_one_correction_settles_an_address(self):
        memory = Memory()
        teach(memory, "jane@acme.com", "Job Search/Interviews")
        hit = memory.lookup("jane@acme.com")
        assert hit.folder == "Job Search/Interviews"
        assert hit.scope == "address"
        assert hit.strength == 1
        assert "jane@acme.com" in hit.because

    def test_one_correction_does_not_settle_a_domain(self):
        memory = Memory()
        teach(memory, "jane@acme.com", "Interviews")
        assert memory.lookup("bob@acme.com") is None

    def test_two_people_at_a_domain_settle_it(self):
        memory = Memory()
        teach(memory, "jane@acme.com", "Interviews")
        teach(memory, "bob@acme.com", "Interviews")
        hit = memory.lookup("carol@acme.com")
        assert hit.folder == "Interviews"
        assert hit.scope == "domain"
        assert "acme.com" in hit.because

    def test_one_person_twice_is_not_a_domain_rule(self):
        """Otherwise a chatty recruiter would speak for the whole company."""
        memory = Memory()
        teach(memory, "jane@acme.com", "Interviews")
        teach(memory, "jane@acme.com", "Interviews")
        assert memory.lookup("bob@acme.com") is None

    def test_a_shared_host_never_becomes_a_domain_rule(self):
        memory = Memory()
        for name in ("jane", "bob", "carol", "dave"):
            teach(memory, f"{name}@gmail.com", "Interviews")
        assert memory.lookup("stranger@gmail.com") is None
        # But the individuals are still remembered.
        assert memory.lookup("jane@gmail.com").folder == "Interviews"

    def test_a_subdomain_of_a_shared_host_is_shared_too(self):
        assert is_shared_host("mail.gmail.com")
        assert not is_shared_host("acme.com")

    def test_agreeing_with_the_app_teaches_nothing(self):
        memory = Memory()
        assert teach(memory, "jane@acme.com", "Interviews", "Interviews") is False
        assert len(memory) == 0


class TestChangingItsMind:
    def test_the_newest_correction_wins_immediately(self):
        memory = Memory()
        for _ in range(3):
            teach(memory, "jane@acme.com", "Applications")
        assert memory.lookup("jane@acme.com").folder == "Applications"

        teach(memory, "jane@acme.com", "Interviews")
        hit = memory.lookup("jane@acme.com")
        assert hit.folder == "Interviews"
        assert hit.strength == 1

    def test_agreement_accumulates(self):
        memory = Memory()
        for _ in range(3):
            teach(memory, "jane@acme.com", "Applications")
        assert memory.lookup("jane@acme.com").strength == 3

    def test_a_reversed_domain_needs_two_people_again(self):
        memory = Memory()
        teach(memory, "jane@acme.com", "Applications")
        teach(memory, "bob@acme.com", "Applications")
        assert memory.lookup("carol@acme.com").folder == "Applications"

        teach(memory, "bob@acme.com", "Interviews")
        # One person changed their mind; that is not the company changing.
        assert memory.lookup("carol@acme.com") is None

        teach(memory, "jane@acme.com", "Interviews")
        assert memory.lookup("carol@acme.com").folder == "Interviews"

    def test_only_the_recent_ones_are_consulted(self):
        memory = Memory()
        for _ in range(corrections.RECENT_PER_KEY + 5):
            teach(memory, "jane@acme.com", "Applications")
        assert memory.lookup("jane@acme.com").strength == corrections.RECENT_PER_KEY


class TestForgetting:
    def test_forget_an_address(self):
        memory = Memory()
        teach(memory, "jane@acme.com", "Interviews")
        teach(memory, "bob@acme.com", "Interviews")
        assert memory.forget("jane@acme.com") == 1
        assert memory.lookup("jane@acme.com") is None
        assert memory.lookup("bob@acme.com") is not None

    def test_forget_a_domain_takes_everyone_at_it(self):
        memory = Memory()
        teach(memory, "jane@acme.com", "Interviews")
        teach(memory, "bob@acme.com", "Interviews")
        teach(memory, "eve@other.com", "Interviews")
        assert memory.forget("acme.com") == 2
        assert memory.lookup("jane@acme.com") is None
        assert memory.lookup("eve@other.com") is not None

    def test_clear_empties_it(self):
        memory = Memory()
        teach(memory, "jane@acme.com", "Interviews")
        memory.clear()
        assert len(memory) == 0
        assert memory.summary() == []

    def test_forgetting_nothing_is_not_an_error(self):
        assert Memory().forget("") == 0
        assert Memory().forget("nobody@nowhere.com") == 0


class TestDisk:
    def test_round_trip(self, tmp_path):
        path = tmp_path / "corrections.json"
        memory = Memory(path=path)
        teach(memory, "jane@acme.com", "Job Search/Interviews", "Job Search/Applications")
        memory.save()

        again = Memory.load(path)
        assert len(again) == 1
        hit = again.lookup("jane@acme.com")
        assert hit.folder == "Job Search/Interviews"

    def test_the_file_is_private(self, tmp_path):
        path = tmp_path / "corrections.json"
        memory = Memory(path=path)
        teach(memory, "jane@acme.com", "Interviews")
        memory.save()
        assert oct(path.stat().st_mode)[-3:] == "600"

    def test_a_missing_file_is_an_empty_memory(self, tmp_path):
        assert len(Memory.load(tmp_path / "nope.json")) == 0

    def test_a_damaged_file_does_not_raise(self, tmp_path):
        path = tmp_path / "corrections.json"
        path.write_text("{not json at all")
        assert len(Memory.load(path)) == 0

    def test_junk_rows_are_dropped_not_fatal(self, tmp_path):
        path = tmp_path / "corrections.json"
        path.write_text(json.dumps({"corrections": [
            {"sender": "jane@acme.com", "folder": "Interviews"},
            {"sender": "", "folder": "Interviews"},
            {"sender": "bob@acme.com", "folder": ""},
            "a string",
            None,
        ]}))
        memory = Memory.load(path)
        assert len(memory) == 1

    def test_a_bare_list_is_accepted(self, tmp_path):
        path = tmp_path / "corrections.json"
        path.write_text(json.dumps([{"sender": "jane@acme.com", "folder": "X"}]))
        assert len(Memory.load(path)) == 1

    def test_the_file_is_capped(self, tmp_path):
        path = tmp_path / "corrections.json"
        memory = Memory(path=path)
        for index in range(corrections.MAX_ENTRIES + 50):
            teach(memory, f"person{index}@acme.com", "Interviews")
        assert len(memory) == corrections.MAX_ENTRIES
        memory.save()
        assert len(Memory.load(path)) == corrections.MAX_ENTRIES

    def test_saving_leaves_no_temp_files_behind(self, tmp_path):
        memory = Memory(path=tmp_path / "corrections.json")
        teach(memory, "jane@acme.com", "Interviews")
        memory.save()
        assert [p.name for p in tmp_path.iterdir()] == ["corrections.json"]


class TestSummary:
    def test_lists_what_it_would_act_on(self):
        memory = Memory()
        teach(memory, "jane@acme.com", "Interviews")
        teach(memory, "bob@other.com", "Applications")
        teach(memory, "carol@other.com", "Applications")
        keys = {item.key for item in memory.summary()}
        assert "jane@acme.com" in keys
        assert "other.com" in keys

    def test_an_address_the_domain_already_covers_is_not_repeated(self):
        """Four lines saying the same thing is three lines of noise."""
        memory = Memory()
        teach(memory, "bob@other.com", "Applications")
        teach(memory, "carol@other.com", "Applications")
        keys = {item.key for item in memory.summary()}
        assert keys == {"other.com"}

    def test_one_dissenter_retires_the_domain_rule(self):
        """And everyone at it goes back to speaking for themselves.

        Filing one colleague's mail somewhere else is evidence the
        company-wide rule was wrong, so it stops applying to strangers at
        that domain - while the people who were already learned keep their
        own answers.
        """
        memory = Memory()
        teach(memory, "bob@other.com", "Applications")
        teach(memory, "carol@other.com", "Applications")
        assert memory.lookup("stranger@other.com").folder == "Applications"

        teach(memory, "dave@other.com", "Interviews")
        assert memory.lookup("stranger@other.com") is None
        assert memory.lookup("bob@other.com").folder == "Applications"
        assert memory.lookup("dave@other.com").folder == "Interviews"
        assert {item.key for item in memory.summary()} == {
            "bob@other.com", "carol@other.com", "dave@other.com"}

    def test_strongest_first(self):
        memory = Memory()
        teach(memory, "weak@acme.com", "Interviews")
        for _ in range(4):
            teach(memory, "strong@acme.com", "Applications")
        assert memory.summary()[0].key == "strong@acme.com"

    def test_describe_says_something_useful(self):
        memory = Memory()
        assert "Nothing learned" in memory.describe()
        teach(memory, "jane@acme.com", "Interviews")
        assert "1 sender" in memory.describe()


class TestApplyingIt:
    def test_a_learned_folder_replaces_the_suggestion(self, item_factory):
        item = item_factory(email_kwargs={"sender_email": "jane@acme.com"})
        item.override_folder = None
        memory = Memory()
        teach(memory, "jane@acme.com", "Job Search/Interviews")

        assert corrections.apply_to([item], memory) == 1
        assert item.target_folder == "Job Search/Interviews"
        assert "jane@acme.com" in item.learned_because
        assert item.override_note == "learned"

    def test_a_hand_made_choice_is_never_overwritten(self, item_factory):
        item = item_factory(email_kwargs={"sender_email": "jane@acme.com"})
        item.override_folder = "Somewhere I Chose"
        memory = Memory()
        teach(memory, "jane@acme.com", "Job Search/Interviews")

        assert corrections.apply_to([item], memory) == 0
        assert item.target_folder == "Somewhere I Chose"

    def test_agreeing_with_the_sorter_changes_nothing(self, item_factory):
        item = item_factory(email_kwargs={"sender_email": "jane@acme.com"})
        memory = Memory()
        teach(memory, "jane@acme.com", item.suggested_folder or "X")
        if item.suggested_folder:
            assert corrections.apply_to([item], memory) == 0
            assert item.learned_from is None

    def test_an_unknown_sender_is_left_alone(self, item_factory):
        item = item_factory(email_kwargs={"sender_email": "stranger@nowhere.com"})
        before = item.target_folder
        assert corrections.apply_to([item], Memory()) == 0
        assert item.target_folder == before


class TestTheSettingsPane:
    """Anything that moves mail has to be visible and undoable."""

    @pytest.fixture
    def dialog(self, qapp, tmp_path, monkeypatch):
        from config import InMemoryCredentialStore, Settings
        from settings_dialog import SettingsDialog
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        memory = Memory(path=tmp_path / corrections.FILENAME)
        teach(memory, "jane@acme.com", "Job Search/Interviews")
        teach(memory, "bob@other.com", "Job Search/Applications")
        teach(memory, "carol@other.com", "Job Search/Applications")
        memory.save()

        widget = SettingsDialog(Settings(icloud_email="you@icloud.example"),
                                InMemoryCredentialStore())
        yield widget
        widget.deleteLater()

    def test_it_lists_what_it_would_act_on(self, dialog):
        rows = [dialog.learned_list.item(i).text()
                for i in range(dialog.learned_list.count())]
        assert any("jane@acme.com" in row for row in rows)
        assert any("other.com" in row for row in rows)

    def test_each_row_says_why(self, dialog):
        tips = [dialog.learned_list.item(i).toolTip()
                for i in range(dialog.learned_list.count())]
        assert all(tip.endswith(".") for tip in tips)
        assert any("filed mail" in tip for tip in tips)

    def test_forget_removes_a_row_and_writes_it_down(self, dialog, tmp_path):
        before = dialog.learned_list.count()
        dialog.learned_list.setCurrentRow(0)
        dialog._forget_selected()
        assert dialog.learned_list.count() < before
        assert len(Memory.load(tmp_path / corrections.FILENAME)) < 3

    def test_the_toggle_round_trips(self, dialog):
        dialog.learn_check.setChecked(False)
        assert dialog.collect().learn_from_corrections is False
        dialog.learn_check.setChecked(True)
        assert dialog.collect().learn_from_corrections is True

    def test_forget_everything_asks_first(self, dialog, tmp_path, monkeypatch):
        from PySide6.QtWidgets import QMessageBox
        monkeypatch.setattr(QMessageBox, "question",
                            staticmethod(lambda *a, **k: QMessageBox.StandardButton.Cancel))
        dialog._forget_everything()
        assert dialog.learned_list.count() > 0

        monkeypatch.setattr(QMessageBox, "question",
                            staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
        dialog._forget_everything()
        assert dialog.learned_list.count() == 0
        assert len(Memory.load(tmp_path / corrections.FILENAME)) == 0

    def test_an_unwritable_file_warns_rather_than_crashes(self, dialog, monkeypatch):
        from PySide6.QtWidgets import QMessageBox
        warned = []
        monkeypatch.setattr(QMessageBox, "warning",
                            staticmethod(lambda *a, **k: warned.append(a)))

        def boom(*_a, **_k):
            raise OSError("read-only file system")

        monkeypatch.setattr(Memory, "save", boom)
        dialog.learned_list.setCurrentRow(0)
        dialog._forget_selected()
        assert warned
