"""Five things that were confusing, and what they do now.

Menu headings that read as options, a Tick button that skipped the mail it
said it would tick, thirteen topics all the same grey, a job-search tree
nested inside the non-job one, and three copies of the version number.
"""

from __future__ import annotations

import pytest

import profiles
from config import InMemoryCredentialStore, Settings
from gui import MainWindow
from models import (CATEGORY_COLORS, OTHER_COLOR, TOPIC_COLORS, Category,
                    Classification, EmailMessage, FolderPlan, NonJobRouting,
                    OtherCategory, TriageItem)
from triage_table import category_color


def other(topic=OtherCategory.RECEIPT, confidence=0.97,
          routing=NonJobRouting.FILE, topics=()) -> TriageItem:
    return TriageItem(
        email=EmailMessage(uid="1", subject="Your order",
                           sender_email="orders@shop.example"),
        classification=Classification(
            summary="s", is_job_related=False,
            category=Category.UNCLASSIFIED_OTHER, other_category=topic,
            confidence_score=confidence, reasoning="r", model="t"),
        folders=FolderPlan(topics=topics), non_job_routing=routing)


def job(category=Category.INTERVIEW, confidence=0.98) -> TriageItem:
    return TriageItem(
        email=EmailMessage(uid="2", subject="Interview"),
        classification=Classification(
            summary="s", is_job_related=True, category=category,
            other_category=OtherCategory.NOT_APPLICABLE,
            confidence_score=confidence, reasoning="r", model="t"),
        folders=FolderPlan())


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


class TestAHeadingDoesNotLookLikeAnOption:
    def test_the_sorting_menu_uses_sections(self, window):
        sections = [a.text() for a in window.sorting_menu.actions()
                    if a.isSeparator() and a.text()]
        assert "What to sort" in sections
        assert "Everything that is not job mail" in sections

    def test_no_heading_is_a_disabled_item(self, window):
        """A disabled action is drawn exactly like a greyed-out choice."""
        window._switch_routing(NonJobRouting.FILE)
        for action in window.sorting_menu.actions():
            if action.isSeparator() or action.menu():
                continue
            assert action.isEnabled(), action.text()

    def test_every_real_item_is_selectable(self, window):
        choosable = [a for a in window.sorting_menu.actions()
                     if not a.isSeparator() and not a.menu()]
        assert len(choosable) == len(profiles.choices()) + len(NonJobRouting) + 1


class TestTickHighConfidence:
    """It says every message the analysis was confident about. It means it."""

    def test_it_ticks_confident_non_job_mail_that_is_being_filed(self):
        from triage_table import TriageTableModel
        model = TriageTableModel()
        model.set_items([other(confidence=0.99), job()])
        model.set_all_approved(True, only_high_confidence=True)
        assert [i.approved for i in model.items] == [True, True]

    def test_it_leaves_the_unsure_ones_alone(self):
        from triage_table import TriageTableModel
        model = TriageTableModel()
        model.set_items([other(confidence=0.72), job(confidence=0.60)])
        model.set_all_approved(True, only_high_confidence=True)
        assert [i.approved for i in model.items] == [False, False]

    def test_it_cannot_tick_what_is_not_being_filed(self):
        """Non-job mail left in place has nowhere to go, however confident."""
        from triage_table import TriageTableModel
        model = TriageTableModel()
        model.set_items([other(confidence=0.99, routing=NonJobRouting.LEAVE)])
        model.set_all_approved(True, only_high_confidence=True)
        assert model.items[0].approved is False

    def test_pre_ticking_is_still_conservative(self):
        """The button is explicit; the automatic default is not, and stays off."""
        assert other(confidence=0.99).default_approved is False
        assert job().default_approved is True

    def test_the_button_reaches_the_model(self, window):
        window._switch_routing(NonJobRouting.FILE)
        window.model.set_all_approved(False)
        window.select_high_button.click()
        filed = [i for i in window.model.items if i.is_actionable
                 and i.is_high_confidence]
        assert filed, "the demo inbox should have some"
        assert all(i.approved for i in filed)


class TestGreyMeansNotSorted:
    def test_a_job_category_always_has_its_colour(self):
        for category, colour in CATEGORY_COLORS.items():
            assert category_color(job(category).classification) == colour

    def test_topics_are_grey_when_non_job_mail_is_left_alone(self):
        for topic in profiles.ALL_TOPICS:
            item = other(topic, routing=NonJobRouting.LEAVE)
            assert category_color(item.classification, item) == OTHER_COLOR

    def test_topics_are_coloured_when_they_are_being_filed(self):
        for topic in profiles.ALL_TOPICS:
            item = other(topic, routing=NonJobRouting.FILE)
            assert category_color(item.classification, item) == TOPIC_COLORS[topic]

    def test_a_topic_left_out_of_the_list_stays_grey(self):
        chosen = (OtherCategory.RECEIPT,)
        kept = other(OtherCategory.RECEIPT, topics=chosen)
        dropped = other(OtherCategory.CHURCH, topics=chosen)
        assert category_color(kept.classification, kept) != OTHER_COLOR
        assert category_color(dropped.classification, dropped) == OTHER_COLOR

    def test_every_topic_has_a_colour_of_its_own(self):
        used = [TOPIC_COLORS[t] for t in profiles.ALL_TOPICS]
        assert len(set(used)) == len(used), "two topics share a colour"

    def test_no_topic_colour_collides_with_a_job_colour(self):
        assert not (set(TOPIC_COLORS[t] for t in profiles.ALL_TOPICS)
                    & set(CATEGORY_COLORS.values()) - {OTHER_COLOR})

    def test_without_an_item_the_old_answer_is_given(self):
        """Callers that have only a classification still get grey."""
        item = other()
        assert category_color(item.classification) == OTHER_COLOR

    def test_the_window_colours_its_filter_chips_the_same_way(self, window):
        window._switch_routing(NonJobRouting.FILE)
        window._refresh_category_filter()
        assert window.category_filter.count() > 1


class TestTheJobTreeStaysWhereItIs:
    """It used to move inside Sorted Mail when the folders were collapsed."""

    @pytest.mark.parametrize("name", [p.name for p in profiles.PROFILES])
    def test_no_job_folder_is_nested_under_the_other_root(self, name):
        profile = profiles.get(name)
        plan = FolderPlan(root="Job Search", other_root="Sorted Mail",
                          detailed_job_folders=profile.detailed_job_folders,
                          topics=profile.topics)
        for folder in plan.leaf_folders + (plan.review_folder,):
            assert not folder.startswith("Sorted Mail"), (name, folder)

    def test_needs_review_is_always_in_the_job_tree(self):
        for detailed in (True, False):
            plan = FolderPlan(root="Job Search", other_root="Sorted Mail",
                              detailed_job_folders=detailed)
            assert plan.review_folder == "Job Search/Needs Review"

    def test_collapsing_files_into_the_root_not_a_twin_of_it(self):
        plan = FolderPlan(root="Job Search", detailed_job_folders=False)
        assert plan.for_category(Category.INTERVIEW) == "Job Search"
        assert "Job Search/Job Search" not in plan.all_folders

    def test_collapsing_still_collapses(self):
        plan = FolderPlan(root="Job Search", detailed_job_folders=False)
        filed = {plan.for_category(c) for c in Category
                 if c is not Category.UNCLASSIFIED_OTHER}
        assert filed == {"Job Search"}

    def test_the_other_root_holds_only_topics(self):
        plan = FolderPlan(root="Job Search", other_root="Sorted Mail",
                          topics=profiles.ALL_TOPICS)
        for topic in profiles.ALL_TOPICS:
            assert plan.for_other_category(topic).startswith("Sorted Mail/")


class TestTheVersionIsStatedOnce:
    def test_every_file_agrees(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        import version
        said = version.stated()
        assert len(set(said.values())) == 1, said

    def test_the_spec_reads_it_rather_than_repeating_it(self):
        from pathlib import Path
        spec = (Path(__file__).resolve().parent.parent
                / "MailManager.spec").read_text()
        assert "from models import APP_VERSION" in spec

    @pytest.mark.parametrize("part,expected", [
        ("patch", "1.2.4"), ("minor", "1.3.0"), ("major", "2.0.0")])
    def test_bumping(self, part, expected):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        import version
        assert version.bumped("1.2.3", part) == expected

    def test_nonsense_is_refused(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        import version
        with pytest.raises(ValueError):
            version.bumped("one point two", "patch")
