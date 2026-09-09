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
        teach(memory, "Jane@Acme.EXAMPLE", "Job Search/Interviews")
        assert memory.lookup("jane@acme.example").folder == "Job Search/Interviews"

    def test_angle_brackets_are_stripped(self):
        memory = Memory()
        teach(memory, "<jane@acme.example>", "Interviews")
        assert memory.lookup("jane@acme.example") is not None

    @pytest.mark.parametrize("bad", [
        "", "   ", "not-an-address", "two@at@signs.com", "@acme.example",
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
        teach(memory, "jane@acme.example", "Job Search/Interviews")
        hit = memory.lookup("jane@acme.example")
        assert hit.folder == "Job Search/Interviews"
        assert hit.scope == "address"
        assert hit.strength == 1
        assert "jane@acme.example" in hit.because

    def test_one_correction_does_not_settle_a_domain(self):
        memory = Memory()
        teach(memory, "jane@acme.example", "Interviews")
        assert memory.lookup("bob@acme.example") is None

    def test_two_people_at_a_domain_settle_it(self):
        memory = Memory()
        teach(memory, "jane@acme.example", "Interviews")
        teach(memory, "bob@acme.example", "Interviews")
        hit = memory.lookup("carol@acme.example")
        assert hit.folder == "Interviews"
        assert hit.scope == "domain"
        assert "acme.example" in hit.because

    def test_one_person_twice_is_not_a_domain_rule(self):
        """Otherwise a chatty recruiter would speak for the whole company."""
        memory = Memory()
        teach(memory, "jane@acme.example", "Interviews")
        teach(memory, "jane@acme.example", "Interviews")
        assert memory.lookup("bob@acme.example") is None

    def test_a_shared_host_never_becomes_a_domain_rule(self):
        memory = Memory()
        for name in ("alice", "bob", "carol", "somebody"):
            teach(memory, f"{name}@gmail.com", "Interviews")
        assert memory.lookup("nobody@gmail.com") is None
        # But the individuals are still remembered.
        assert memory.lookup("alice@gmail.com").folder == "Interviews"

    def test_a_subdomain_of_a_shared_host_is_shared_too(self):
        assert is_shared_host("mail.gmail.com")
        assert not is_shared_host("acme.example")

    def test_agreeing_with_the_app_teaches_nothing(self):
        memory = Memory()
        assert teach(memory, "jane@acme.example", "Interviews", "Interviews") is False
        assert len(memory) == 0


class TestChangingItsMind:
    def test_the_newest_correction_wins_immediately(self):
        memory = Memory()
        for _ in range(3):
            teach(memory, "jane@acme.example", "Applications")
        assert memory.lookup("jane@acme.example").folder == "Applications"

        teach(memory, "jane@acme.example", "Interviews")
        hit = memory.lookup("jane@acme.example")
        assert hit.folder == "Interviews"
        assert hit.strength == 1

    def test_agreement_accumulates(self):
        memory = Memory()
        for _ in range(3):
            teach(memory, "jane@acme.example", "Applications")
        assert memory.lookup("jane@acme.example").strength == 3

    def test_a_reversed_domain_needs_two_people_again(self):
        memory = Memory()
        teach(memory, "jane@acme.example", "Applications")
        teach(memory, "bob@acme.example", "Applications")
        assert memory.lookup("carol@acme.example").folder == "Applications"

        teach(memory, "bob@acme.example", "Interviews")
        # One person changed their mind; that is not the company changing.
        assert memory.lookup("carol@acme.example") is None

        teach(memory, "jane@acme.example", "Interviews")
        assert memory.lookup("carol@acme.example").folder == "Interviews"

    def test_only_the_recent_ones_are_consulted(self):
        memory = Memory()
        for _ in range(corrections.RECENT_PER_KEY + 5):
            teach(memory, "jane@acme.example", "Applications")
        assert memory.lookup("jane@acme.example").strength == corrections.RECENT_PER_KEY


class TestForgetting:
    def test_forget_an_address(self):
        memory = Memory()
        teach(memory, "jane@acme.example", "Interviews")
        teach(memory, "bob@acme.example", "Interviews")
        assert memory.forget("jane@acme.example") == 1
        assert memory.lookup("jane@acme.example") is None
        assert memory.lookup("bob@acme.example") is not None

    def test_forget_a_domain_takes_everyone_at_it(self):
        memory = Memory()
        teach(memory, "jane@acme.example", "Interviews")
        teach(memory, "bob@acme.example", "Interviews")
        teach(memory, "eve@other.example", "Interviews")
        assert memory.forget("acme.example") == 2
        assert memory.lookup("jane@acme.example") is None
        assert memory.lookup("eve@other.example") is not None

    def test_clear_empties_it(self):
        memory = Memory()
        teach(memory, "jane@acme.example", "Interviews")
        memory.clear()
        assert len(memory) == 0
        assert memory.summary() == []

    def test_forgetting_nothing_is_not_an_error(self):
        assert Memory().forget("") == 0
        assert Memory().forget("nobody@nowhere.example") == 0


class TestDisk:
    def test_round_trip(self, tmp_path):
        path = tmp_path / "corrections.json"
        memory = Memory(path=path)
        teach(memory, "jane@acme.example", "Job Search/Interviews", "Job Search/Applications")
        memory.save()

        again = Memory.load(path)
        assert len(again) == 1
        hit = again.lookup("jane@acme.example")
        assert hit.folder == "Job Search/Interviews"

    def test_the_file_is_private(self, tmp_path):
        path = tmp_path / "corrections.json"
        memory = Memory(path=path)
        teach(memory, "jane@acme.example", "Interviews")
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
            {"sender": "jane@acme.example", "folder": "Interviews"},
            {"sender": "", "folder": "Interviews"},
            {"sender": "bob@acme.example", "folder": ""},
            "a string",
            None,
        ]}))
        memory = Memory.load(path)
        assert len(memory) == 1

    def test_a_bare_list_is_accepted(self, tmp_path):
        path = tmp_path / "corrections.json"
        path.write_text(json.dumps([{"sender": "jane@acme.example", "folder": "X"}]))
        assert len(Memory.load(path)) == 1

    def test_the_file_is_capped(self, tmp_path):
        path = tmp_path / "corrections.json"
        memory = Memory(path=path)
        for index in range(corrections.MAX_ENTRIES + 50):
            teach(memory, f"person{index}@acme.example", "Interviews")
        assert len(memory) == corrections.MAX_ENTRIES
        memory.save()
        assert len(Memory.load(path)) == corrections.MAX_ENTRIES

    def test_saving_leaves_no_temp_files_behind(self, tmp_path):
        memory = Memory(path=tmp_path / "corrections.json")
        teach(memory, "jane@acme.example", "Interviews")
        memory.save()
        assert [p.name for p in tmp_path.iterdir()] == ["corrections.json"]


class TestSummary:
    def test_lists_what_it_would_act_on(self):
        memory = Memory()
        teach(memory, "jane@acme.example", "Interviews")
        teach(memory, "bob@other.example", "Applications")
        teach(memory, "carol@other.example", "Applications")
        keys = {item.key for item in memory.summary()}
        assert "jane@acme.example" in keys
        assert "other.example" in keys

    def test_an_address_the_domain_already_covers_is_not_repeated(self):
        """Four lines saying the same thing is three lines of noise."""
        memory = Memory()
        teach(memory, "bob@other.example", "Applications")
        teach(memory, "carol@other.example", "Applications")
        keys = {item.key for item in memory.summary()}
        assert keys == {"other.example"}

    def test_one_dissenter_retires_the_domain_rule(self):
        """And everyone at it goes back to speaking for themselves.

        Filing one colleague's mail somewhere else is evidence the
        company-wide rule was wrong, so it stops applying to strangers at
        that domain - while the people who were already learned keep their
        own answers.
        """
        memory = Memory()
        teach(memory, "bob@other.example", "Applications")
        teach(memory, "carol@other.example", "Applications")
        assert memory.lookup("stranger@other.example").folder == "Applications"

        teach(memory, "dave@other.example", "Interviews")
        assert memory.lookup("stranger@other.example") is None
        assert memory.lookup("bob@other.example").folder == "Applications"
        assert memory.lookup("dave@other.example").folder == "Interviews"
        assert {item.key for item in memory.summary()} == {
            "bob@other.example", "carol@other.example", "dave@other.example"}

    def test_strongest_first(self):
        memory = Memory()
        teach(memory, "weak@acme.example", "Interviews")
        for _ in range(4):
            teach(memory, "strong@acme.example", "Applications")
        assert memory.summary()[0].key == "strong@acme.example"

    def test_describe_says_something_useful(self):
        memory = Memory()
        assert "Nothing learned" in memory.describe()
        teach(memory, "jane@acme.example", "Interviews")
        assert "1 sender" in memory.describe()


class TestApplyingIt:
    def test_a_learned_folder_replaces_the_suggestion(self, item_factory):
        item = item_factory(email_kwargs={"sender_email": "jane@acme.example"})
        item.override_folder = None
        memory = Memory()
        teach(memory, "jane@acme.example", "Job Search/Interviews")

        assert corrections.apply_to([item], memory) == 1
        assert item.target_folder == "Job Search/Interviews"
        assert "jane@acme.example" in item.learned_because
        assert item.override_note == "learned"

    def test_a_hand_made_choice_is_never_overwritten(self, item_factory):
        item = item_factory(email_kwargs={"sender_email": "jane@acme.example"})
        item.override_folder = "Somewhere I Chose"
        memory = Memory()
        teach(memory, "jane@acme.example", "Job Search/Interviews")

        assert corrections.apply_to([item], memory) == 0
        assert item.target_folder == "Somewhere I Chose"

    def test_agreeing_with_the_sorter_changes_nothing(self, item_factory):
        item = item_factory(email_kwargs={"sender_email": "jane@acme.example"})
        memory = Memory()
        teach(memory, "jane@acme.example", item.suggested_folder or "X")
        if item.suggested_folder:
            assert corrections.apply_to([item], memory) == 0
            assert item.learned_from is None

    def test_an_unknown_sender_is_left_alone(self, item_factory):
        item = item_factory(email_kwargs={"sender_email": "stranger@nowhere.example"})
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
        teach(memory, "jane@acme.example", "Job Search/Interviews")
        teach(memory, "bob@other.example", "Job Search/Applications")
        teach(memory, "carol@other.example", "Job Search/Applications")
        memory.save()

        widget = SettingsDialog(Settings(icloud_email="you@icloud.example"),
                                InMemoryCredentialStore())
        yield widget
        widget.deleteLater()

    def test_it_lists_what_it_would_act_on(self, dialog):
        rows = [dialog.learned_list.item(i).text()
                for i in range(dialog.learned_list.count())]
        assert any("jane@acme.example" in row for row in rows)
        assert any("other.example" in row for row in rows)

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
