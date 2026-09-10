"""Which messages are the same conversation."""

from __future__ import annotations

import pytest

import conversations
from conversations import group, message_ids, normalise_subject, thread_keys
from models import (Category, Classification, EmailMessage, FolderPlan,
                    OtherCategory, TriageItem)


def note(uid, *, mid="", reply_to="", refs="", subject="Hello",
         sender="jane@acme.example") -> EmailMessage:
    return EmailMessage(uid=str(uid), message_id=mid, in_reply_to=reply_to,
                        references=refs, subject=subject, sender_email=sender)


class TestNormalisingASubject:
    @pytest.mark.parametrize("subject", [
        "Interview on Thursday", "Re: Interview on Thursday",
        "RE: Interview on Thursday", "Fwd: Interview on Thursday",
        "Re: Fw: Re: Interview on Thursday", "AW: Interview on Thursday",
        "SV: Interview on Thursday", "Re : Interview on Thursday",
        "[list] Interview on Thursday",
    ])
    def test_the_markers_come_off(self, subject):
        assert normalise_subject(subject) == "interview on thursday"

    def test_whitespace_is_folded(self):
        assert normalise_subject("  Interview   on  Thursday ") == \
            "interview on thursday"

    def test_a_subject_that_is_only_markers_becomes_empty(self):
        assert normalise_subject("Re: Fwd:") == ""

    def test_it_does_not_run_forever_on_a_pathological_subject(self):
        assert normalise_subject("Re: " * 500 + "x") is not None

    def test_a_word_ending_in_re_is_left_alone(self):
        assert normalise_subject("Software here: an update") == \
            "software here: an update"


class TestReadingMessageIds:
    def test_it_takes_bracketed_ids(self):
        assert message_ids("<a@x> <b@y>") == ["a@x", "b@y"]

    def test_bare_words_are_not_ids(self):
        """A References header of loose words would join unrelated threads."""
        assert message_ids("something something") == []

    def test_case_is_folded(self):
        assert message_ids("<A@X>") == ["a@x"]

    def test_nothing_is_not_an_error(self):
        assert message_ids("") == []
        assert message_ids(None) == []


class TestThreadingByIdentity:
    def test_a_reply_joins_its_parent(self):
        keys = thread_keys([note(1, mid="<a@x>"),
                            note(2, mid="<b@x>", reply_to="<a@x>")])
        assert keys[0] == keys[1]

    def test_a_chain_of_references_is_one_thread(self):
        keys = thread_keys([
            note(1, mid="<a@x>", subject="One"),
            note(2, mid="<b@x>", reply_to="<a@x>", subject="Two"),
            note(3, mid="<c@x>", refs="<a@x> <b@x>", subject="Three"),
        ])
        assert len(set(keys)) == 1

    def test_two_branches_of_one_thread_meet(self):
        """Two people reply to the same message; all four are one thread."""
        keys = thread_keys([
            note(1, mid="<a@x>", subject="One"),
            note(2, mid="<b@x>", reply_to="<a@x>", subject="Two"),
            note(3, mid="<c@x>", reply_to="<a@x>", subject="Three"),
        ])
        assert len(set(keys)) == 1

    def test_unrelated_mail_stays_apart(self):
        keys = thread_keys([note(1, mid="<a@x>", subject="One"),
                            note(2, mid="<b@x>", subject="Two")])
        assert keys[0] != keys[1]

    def test_the_answer_does_not_depend_on_the_order(self):
        forwards = [note(1, mid="<a@x>", subject="One"),
                    note(2, mid="<b@x>", reply_to="<a@x>", subject="Two"),
                    note(3, mid="<c@x>", refs="<b@x>", subject="Three")]
        backwards = list(reversed(forwards))
        assert len(set(thread_keys(forwards))) == 1
        assert len(set(thread_keys(backwards))) == 1
        assert set(thread_keys(forwards)) == set(thread_keys(backwards))

    def test_a_message_with_no_id_is_still_its_own_thread(self):
        keys = thread_keys([note(1, subject="One"), note(2, subject="Two")])
        assert len(set(keys)) == 2

    def test_two_with_no_ids_join_on_subject_and_sender(self):
        """Which is the fallback working, not a bug."""
        keys = thread_keys([note(1, subject="One"), note(2, subject="Re: One")])
        assert keys[0] == keys[1]

    def test_two_identical_messages_are_two_messages(self):
        """A duplicate delivery is one entry each, not one entry."""
        keys = thread_keys([note(1, mid="<a@x>"), note(2, mid="<a@x>")])
        assert len(keys) == 2


class TestThreadingBySubjectAndSender:
    def test_a_stripped_reply_still_joins(self):
        keys = thread_keys([
            note(1, mid="<a@x>", subject="Interview on Thursday"),
            note(2, mid="<b@x>", subject="Re: Interview on Thursday"),
        ])
        assert keys[0] == keys[1]

    def test_the_same_subject_from_different_people_stays_apart(self):
        """Otherwise every "Thank you for applying" ever sent is one thread."""
        keys = thread_keys([
            note(1, mid="<a@x>", subject="Thank you for applying",
                 sender="one@acme.example"),
            note(2, mid="<b@x>", subject="Thank you for applying",
                 sender="two@other.example"),
        ])
        assert keys[0] != keys[1]

    def test_different_subjects_from_one_person_stay_apart(self):
        """Otherwise a recruiter's whole correspondence becomes one thread."""
        keys = thread_keys([
            note(1, mid="<a@x>", subject="Interview"),
            note(2, mid="<b@x>", subject="Offer"),
        ])
        assert keys[0] != keys[1]

    def test_an_empty_subject_joins_nothing(self):
        keys = thread_keys([note(1, mid="<a@x>", subject=""),
                            note(2, mid="<b@x>", subject="")])
        assert keys[0] != keys[1]


class TestGrouping:
    def test_it_reports_positions(self):
        found = group([note(1, mid="<a@x>", subject="One"),
                       note(2, mid="<b@x>", reply_to="<a@x>", subject="Two"),
                       note(3, mid="<c@x>", subject="Elsewhere")])
        sizes = sorted(len(v) for v in found.values())
        assert sizes == [1, 2]

    def test_nothing_is_nothing(self):
        assert group([]) == {}
        assert thread_keys([]) == []

    def test_describe(self):
        assert conversations.describe(1) == ""
        assert "3 messages" in conversations.describe(3)


class TestStampingTheItems:
    def item(self, uid, **kwargs):
        return TriageItem(
            email=note(uid, **kwargs),
            classification=Classification(
                summary="s", is_job_related=True, category=Category.INTERVIEW,
                other_category=OtherCategory.NOT_APPLICABLE,
                confidence_score=0.9, reasoning="r", model="t"),
            folders=FolderPlan())

    def test_each_item_learns_its_thread(self):
        items = [self.item(1, mid="<a@x>", subject="One"),
                 self.item(2, mid="<b@x>", reply_to="<a@x>", subject="Two"),
                 self.item(3, mid="<c@x>", subject="Alone")]
        assert conversations.apply_to(items) == 2
        assert items[0].thread_key == items[1].thread_key
        assert items[0].thread_size == 2
        assert items[2].thread_size == 1

    def test_a_lone_message_is_not_in_a_conversation(self):
        items = [self.item(1, mid="<a@x>")]
        assert conversations.apply_to(items) == 0
        assert items[0].in_a_conversation is False

    def test_nothing_to_stamp_is_not_an_error(self):
        assert conversations.apply_to([]) == 0

    def test_the_preview_says_so(self):
        from triage_table import _reasoning_html
        items = [self.item(1, mid="<a@x>", subject="One"),
                 self.item(2, mid="<b@x>", reply_to="<a@x>", subject="Two")]
        conversations.apply_to(items)
        assert "conversation" in _reasoning_html(items[0]).lower()


class TestInTheWindow:
    @pytest.fixture
    def window(self, qapp, tmp_path, monkeypatch):
        from config import InMemoryCredentialStore, Settings
        from gui import MainWindow
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        win = MainWindow(Settings(icloud_email="you@icloud.example"),
                         InMemoryCredentialStore())
        win.folder_plan = FolderPlan()
        items = [TestStampingTheItems().item(n, mid=f"<{n}@x>",
                                             reply_to="<1@x>" if n > 1 else "",
                                             subject="One" if n == 1 else "Re: One")
                 for n in (1, 2, 3)]
        items.append(TestStampingTheItems().item(9, mid="<9@x>", subject="Alone"))
        conversations.apply_to(items)
        win.model.set_items(items)
        win._refresh_folder_choices()
        yield win
        win.close()

    def test_the_model_finds_the_whole_conversation(self, window):
        assert sorted(window.model.conversation_of(0)) == [0, 1, 2]
        assert window.model.conversation_of(3) == [3]

    def test_the_menu_offers_it(self, window):
        from PySide6.QtCore import QItemSelectionModel
        window.table.selectionModel().select(
            window.proxy.index(0, 0),
            QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QItemSelectionModel.SelectionFlag.Rows)
        labels = [a.text() for a in window.build_table_menu().actions()]
        assert any("whole conversation (3 messages)" in label for label in labels)

    def test_the_menu_does_not_offer_it_for_a_lone_message(self, window):
        from PySide6.QtCore import QItemSelectionModel
        window.table.selectionModel().select(
            window.proxy.index(3, 0),
            QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QItemSelectionModel.SelectionFlag.Rows)
        labels = [a.text() for a in window.build_table_menu().actions()]
        assert not any("conversation" in label for label in labels)

    def test_selecting_it_selects_every_row(self, window):
        window._select_rows([0, 1, 2])
        assert sorted(window._selected_rows()) == [0, 1, 2]

    def test_filing_a_conversation_files_all_of_it(self, window):
        suggested = window.model.items[0].suggested_folder
        target = next(f for f in window._folder_choices() if f != suggested)
        window._refile([0, 1, 2], target)
        assert [i.target_folder for i in window.model.items[:3]] == [target] * 3
        assert window.model.items[3].target_folder != target
