"""Sorting mail that is not job mail: the control, and what it says.

The complaint this answers: job mail could be ticked and filed in one click,
and non-job mail sat in the table doing nothing, with no menu to change it and
no explanation of why it would not tick.
"""

from __future__ import annotations

import pytest

import profiles
from config import InMemoryCredentialStore, Settings
from gui import MainWindow
from models import (Category, Classification, EmailMessage, FolderPlan,
                    NonJobRouting, OtherCategory, TriageItem)


def other(topic=OtherCategory.RECEIPT, confidence=0.97,
          routing=NonJobRouting.LEAVE, **kwargs) -> TriageItem:
    return TriageItem(
        email=EmailMessage(uid="1", subject="Your order",
                           sender_email="orders@shop.example"),
        classification=Classification(
            summary="A receipt.", is_job_related=False,
            category=Category.UNCLASSIFIED_OTHER, other_category=topic,
            confidence_score=confidence, reasoning="r", model="t"),
        folders=FolderPlan(), non_job_routing=routing, **kwargs)


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
    win = MainWindow(Settings(icloud_email="you@icloud.example"),
                     InMemoryCredentialStore())
    win._load_demo_data()
    yield win
    win.close()
    # close() only hides it. Without deleteLater the window and every
    # widget under it stay alive for the rest of the session, and
    # setStyleSheet restyles all of them on every theme change - which
    # is what made this file take minutes instead of seconds.
    win.deleteLater()


class TestTheRowExplainsItself:
    def test_left_alone_says_so_rather_than_leave_in_place(self):
        item = other()
        assert item.is_actionable is False
        assert item.status_display == "Not job mail - not sorted"
        assert "Sorting" in item.why_not_actionable

    def test_it_is_a_setting_not_a_property_of_the_message(self):
        assert other(routing=NonJobRouting.LEAVE).left_because_not_job is True
        assert other(routing=NonJobRouting.FILE).left_because_not_job is False

    def test_not_sure_enough_is_a_different_answer(self):
        item = other(confidence=0.72, routing=NonJobRouting.FILE)
        assert item.status_display == "Not sure enough to file"
        assert item.held_back_by_confidence is True
        why = item.why_not_actionable
        assert "72%" in why and "95%" in why

    def test_a_row_that_can_be_ticked_explains_nothing(self):
        item = other(routing=NonJobRouting.FILE)
        assert item.is_actionable is True
        assert item.why_not_actionable == ""

    def test_a_failed_analysis_is_not_an_inert_row(self):
        """It routes to Needs Review, which is a folder, so it can be ticked."""
        item = other()
        item.classification = Classification(error="the provider timed out")
        assert item.is_actionable is True
        assert item.why_not_actionable == ""

    def test_job_mail_is_unaffected(self):
        item = TriageItem(
            email=EmailMessage(uid="1"),
            classification=Classification(
                summary="s", is_job_related=True, category=Category.INTERVIEW,
                other_category=OtherCategory.NOT_APPLICABLE,
                confidence_score=0.98, reasoning="r", model="t"),
            folders=FolderPlan())
        assert item.left_because_not_job is False
        assert item.why_not_actionable == ""


class TestTheSortingMenu:
    def labels(self, window):
        return [a.text() for a in window.sorting_menu.actions()
                if not a.isSeparator()]

    def test_it_offers_every_profile(self, window):
        labels = self.labels(window)
        for _name, label, _blurb in profiles.choices():
            assert any(label in text for text in labels), label

    def test_it_offers_every_answer_for_non_job_mail(self, window):
        labels = self.labels(window)
        for member in NonJobRouting:
            assert any(member.label in text for text in labels), member

    def test_the_current_choices_are_ticked(self, window):
        checked = [a.text() for a in window.sorting_menu.actions()
                   if a.isCheckable() and a.isChecked()]
        assert any("Job search" in text for text in checked)
        assert any("Leave in place" in text for text in checked)

    def test_the_button_says_what_is_happening(self, window):
        assert "Sorting" in window.sorting_button.text()
        assert "left where it is" in window.sorting_button.toolTip()

    def test_it_is_no_longer_hidden_in_the_model_menu(self, window):
        """It used to be a submenu of the menu about which AI to use."""
        window._rebuild_model_menu()
        labels = [a.text() for a in window.model_menu.actions()]
        assert not any("What to sort" in text for text in labels)


class TestOneClickTurnsItOn:
    def test_the_rows_become_actionable(self, window):
        before = [i for i in window.model.items
                  if not i.classification.is_job_related]
        assert before and not any(i.is_actionable for i in before)

        window._switch_routing(NonJobRouting.FILE)
        after = [i for i in window.model.items
                 if not i.classification.is_job_related]
        assert any(i.is_actionable for i in after)
        assert all(i.target_folder for i in after if i.is_actionable)

    def test_nothing_is_re_analyzed(self, window):
        """Routing is a decision about verdicts, not a new verdict."""
        before = {i.email.uid: i.classification.confidence_score
                  for i in window.model.items}
        window._switch_routing(NonJobRouting.FILE)
        after = {i.email.uid: i.classification.confidence_score
                 for i in window.model.items}
        assert before == after

    def test_the_uncertain_ones_are_still_held_back(self, window):
        window._switch_routing(NonJobRouting.FILE)
        unsure = [i for i in window.model.items
                  if not i.classification.is_job_related
                  and i.classification.confidence_score < i.threshold]
        assert all(not i.is_actionable for i in unsure)
        assert all(i.held_back_by_confidence for i in unsure)

    def test_the_menu_follows(self, window):
        window._switch_routing(NonJobRouting.FILE)
        checked = [a.text() for a in window.sorting_menu.actions()
                   if a.isCheckable() and a.isChecked()]
        assert any("File by topic" in text for text in checked)

    def test_the_topic_list_appears_only_when_it_means_something(self, window):
        assert not any(a.menu() for a in window.sorting_menu.actions())
        window._switch_routing(NonJobRouting.FILE)
        assert any(a.menu() for a in window.sorting_menu.actions())

    def test_choosing_the_same_thing_twice_is_harmless(self, window):
        window._switch_routing(NonJobRouting.LEAVE)
        assert window.settings.routing is NonJobRouting.LEAVE

    def test_it_is_written_down(self, window, tmp_path):
        window._switch_routing(NonJobRouting.FILE)
        assert Settings.load(tmp_path / "settings.json").routing \
            is NonJobRouting.FILE


class TestChoosingTopics:
    def test_every_topic_can_be_turned_off(self, window):
        window._switch_routing(NonJobRouting.FILE)
        window._set_topics([OtherCategory.RECEIPT])
        assert window.settings.chosen_topics == (OtherCategory.RECEIPT,)

    def test_unticked_topics_go_to_other(self, window):
        window._switch_routing(NonJobRouting.FILE)
        window._set_topics([OtherCategory.RECEIPT])
        plan = window.folder_plan
        assert plan.for_other_category(OtherCategory.RECEIPT).endswith("Receipts")
        assert plan.for_other_category(OtherCategory.TRAVEL).endswith("Other")

    def test_toggling_one_leaves_the_rest(self, window):
        window._switch_routing(NonJobRouting.FILE)
        window._set_topics(profiles.ALL_TOPICS)
        window._toggle_topic(OtherCategory.SPAM, False)
        chosen = window.settings.chosen_topics
        assert OtherCategory.SPAM not in chosen
        assert len(chosen) == len(profiles.ALL_TOPICS) - 1

    def test_the_shortcuts_work(self, window):
        window._switch_routing(NonJobRouting.FILE)
        window._set_topics(profiles.ESSENTIAL_TOPICS)
        assert len(window.settings.chosen_topics) == len(profiles.ESSENTIAL_TOPICS)
        window._set_topics(profiles.ALL_TOPICS)
        assert len(window.settings.chosen_topics) == len(profiles.ALL_TOPICS)

    def test_the_order_is_always_the_canonical_one(self, window):
        window._switch_routing(NonJobRouting.FILE)
        window._set_topics(list(reversed(profiles.ALL_TOPICS)))
        assert window.settings.chosen_topics == profiles.ALL_TOPICS


class TestThePreviewOffersTheFix:
    def test_it_explains_an_inert_row(self, window):
        item = next(i for i in window.model.items
                    if not i.classification.is_job_related)
        row = window.model.items.index(item)
        window.preview.show_item(row, item)
        assert window.preview.inert_row.isVisibleTo(window.preview)
        assert "Sorting" in window.preview.inert_note.text()

    def test_the_button_is_offered_for_the_cause_it_fixes(self, window):
        item = next(i for i in window.model.items
                    if not i.classification.is_job_related)
        window.preview.show_item(0, item)
        assert window.preview.sort_these_button.isVisibleTo(window.preview)

    def test_pressing_it_turns_sorting_on(self, window):
        item = next(i for i in window.model.items
                    if not i.classification.is_job_related)
        window.preview.show_item(0, item)
        window.preview.sort_these_button.click()
        assert window.settings.routing is NonJobRouting.FILE

    def test_an_ordinary_row_shows_no_banner(self, window):
        window._switch_routing(NonJobRouting.FILE)
        item = next(i for i in window.model.items if i.is_actionable)
        window.preview.show_item(0, item)
        assert not window.preview.inert_row.isVisibleTo(window.preview)
