"""Move planning and the folders an approved set actually needs."""

from __future__ import annotations


from models import Category, Classification, EmailMessage, FolderPlan, NonJobRouting, OtherCategory, TriageItem
from workers import build_move_plans, required_folders


def item(uid="1", **overrides):
    classification_kwargs = overrides.pop("classification", {})
    classification = Classification(
        summary="s", reasoning="r",
        **{
            "is_job_related": True,
            "category": Category.INTERVIEW,
            "other_category": OtherCategory.NOT_APPLICABLE,
            "confidence_score": 0.98,
            **classification_kwargs,
        },
    )
    return TriageItem(
        email=EmailMessage(uid=uid, subject=f"Subject {uid}"),
        classification=classification,
        folders=FolderPlan(),
        **overrides,
    )


class TestBuildMovePlans:
    def test_only_approved_rows_are_planned(self):
        approved = item("1")
        unapproved = item("2", classification={"confidence_score": 0.3})
        plans = build_move_plans([approved, unapproved])
        assert [p.uid for p in plans] == ["1"]

    def test_leave_in_place_rows_produce_nothing(self):
        left = item("1", classification={
            "is_job_related": False,
            "category": Category.UNCLASSIFIED_OTHER,
            "other_category": OtherCategory.FINANCE,
            "confidence_score": 0.99,
        })
        left.approved = True  # even if forced on, there is no target
        assert build_move_plans([left]) == []

    def test_already_moved_rows_are_not_replanned(self):
        moved = item("1")
        moved.moved = True
        assert build_move_plans([moved]) == []

    def test_manual_overrides_are_honoured(self):
        overridden = item("1", classification={"confidence_score": 0.2})
        overridden.override_folder = "Job Search/Interview"
        overridden.approved = True
        plans = build_move_plans([overridden])
        assert plans[0].target_folder == "Job Search/Interview"

    def test_subject_is_carried_for_the_log(self):
        assert build_move_plans([item("7")])[0].subject == "Subject 7"

    def test_empty_input(self):
        assert build_move_plans([]) == []


class TestRequiredFolders:
    def test_standard_tree_needs_no_extras(self):
        assert required_folders([item("1")], FolderPlan()) == []

    def test_topic_folders_are_requested_only_when_used(self):
        rows = [
            item("1", non_job_routing=NonJobRouting.FILE, auto_approve_non_job=True,
                 classification={"is_job_related": False, "category": Category.UNCLASSIFIED_OTHER,
                                 "other_category": OtherCategory.FINANCE, "confidence_score": 0.99}),
            item("2", non_job_routing=NonJobRouting.FILE, auto_approve_non_job=True,
                 classification={"is_job_related": False, "category": Category.UNCLASSIFIED_OTHER,
                                 "other_category": OtherCategory.NEWSLETTER, "confidence_score": 0.99}),
        ]
        assert required_folders(rows, FolderPlan()) == [
            "Sorted Mail", "Sorted Mail/Finance", "Sorted Mail/Newsletters"
        ]

    def test_unapproved_topics_are_not_created(self):
        """Nobody wants a dozen empty mailboxes appearing in their account."""
        rows = [
            item("1", non_job_routing=NonJobRouting.FILE,
                 classification={"is_job_related": False, "category": Category.UNCLASSIFIED_OTHER,
                                 "other_category": OtherCategory.FINANCE, "confidence_score": 0.99}),
        ]
        assert rows[0].approved is False
        assert required_folders(rows, FolderPlan()) == []

    def test_duplicate_topics_collapse(self):
        rows = [
            item(str(i), non_job_routing=NonJobRouting.FILE, auto_approve_non_job=True,
                 classification={"is_job_related": False, "category": Category.UNCLASSIFIED_OTHER,
                                 "other_category": OtherCategory.SOCIAL, "confidence_score": 0.99})
            for i in range(5)
        ]
        assert required_folders(rows, FolderPlan()) == ["Sorted Mail", "Sorted Mail/Social"]

    def test_a_manual_override_to_a_new_folder_is_requested(self):
        row = item("1")
        row.override_folder = "Archive/2026"
        assert required_folders([row], FolderPlan()) == ["Archive/2026"]

    def test_no_plan_means_no_folders(self):
        assert required_folders([item("1")], None) == []


class TestReadingWithTheThread:
    """Within a conversation the sorter is sure about, its weak readings of
    the other messages take the conversation's category."""

    def _item(self, uid, subject, job=True, category=Category.INTERVIEW, confidence=0.9,
              other=OtherCategory.NOT_APPLICABLE, error=None, thread="t1"):
        from models import EmailMessage, TriageItem

        found = TriageItem(
            email=EmailMessage(uid=uid, subject=subject, sender_email="j@acme.example"),
            classification=Classification(
                summary="s", is_job_related=job, category=category, other_category=other,
                confidence_score=confidence, reasoning="r", model="rules", error=error),
            folders=FolderPlan())
        found.thread_key = thread
        return found

    def test_weak_readings_take_the_conversations_category(self):
        from workers import read_with_the_thread

        lead = self._item("1", "Teams call", confidence=0.96)
        unsure = self._item("2", "Re: Teams call", category=Category.UNCLASSIFIED_OTHER,
                            confidence=0.38)
        elsewhere = self._item("3", "Teams call", job=False, category=Category.UNCLASSIFIED_OTHER,
                               other=OtherCategory.EVENT, confidence=0.89)
        assert read_with_the_thread([lead, unsure, elsewhere]) == 2
        for item in (unsure, elsewhere):
            assert item.classification.is_job_related
            assert item.classification.category is Category.INTERVIEW
            assert item.classification.confidence_score == 0.78
            assert "conversation" in item.classification.adjustments
            assert "Read with the rest of its conversation" in item.classification.reasoning
        assert lead.classification.confidence_score == 0.96

    def test_sure_readings_and_lone_messages_are_left_alone(self):
        from workers import read_with_the_thread

        lead = self._item("1", "Offer", category=Category.OFFER, confidence=0.95)
        sure_other = self._item("2", "Re: Offer", category=Category.NEXT_STEPS, confidence=0.9)
        sure_not = self._item("3", "Re: Offer", job=False,
                              category=Category.UNCLASSIFIED_OTHER,
                              other=OtherCategory.PERSONAL, confidence=0.95)
        broken = self._item("4", "Re: Offer", category=Category.UNCLASSIFIED_OTHER,
                            confidence=0.0, error="no model")
        alone = self._item("5", "Hello", category=Category.UNCLASSIFIED_OTHER,
                           confidence=0.3, thread="t2")
        assert read_with_the_thread([lead, sure_other, sure_not, broken, alone]) == 0
        assert sure_other.classification.category is Category.NEXT_STEPS
        assert not sure_not.classification.is_job_related
        assert alone.classification.category is Category.UNCLASSIFIED_OTHER

    def test_a_conversation_nobody_is_sure_about_carries_nothing(self):
        from workers import read_with_the_thread

        items = [self._item("1", "x", category=Category.UNCLASSIFIED_OTHER, confidence=0.5),
                 self._item("2", "x", category=Category.INTERVIEW, confidence=0.7)]
        assert read_with_the_thread(items) == 0
