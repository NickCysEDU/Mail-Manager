"""The routing rules that decide where a message goes."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from models import (
    Category,
    Classification,
    Disposition,
    EmailMessage,
    FolderPlan,
    NonJobRouting,
    OtherCategory,
    TimeWindow,
    TriageItem,
    TriageSummary,
    clock,
    imap_since_date,
    resolve_window,
    sanitize_folder_component,
)


# ==========================================================================
# Category / OtherCategory
# ==========================================================================
class TestCategoryParsing:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("INTERVIEW", Category.INTERVIEW),
            ("interview", Category.INTERVIEW),
            ("  Next_Steps  ", Category.NEXT_STEPS),
            ("next steps", Category.NEXT_STEPS),
            ("next-steps", Category.NEXT_STEPS),
            ("APPLICATION_RECEIVED", Category.APPLICATION_RECEIVED),
            (Category.NOT_INTERESTED, Category.NOT_INTERESTED),
        ],
    )
    def test_accepts_reasonable_spellings(self, raw, expected):
        assert Category.parse(raw) is expected

    @pytest.mark.parametrize("raw", ["REJECTED", "", None, 7, [], "INTERVIEWS"])
    def test_rejects_anything_else(self, raw):
        assert Category.parse(raw) is None

    def test_every_category_has_a_folder_and_label(self):
        from models import CATEGORY_LEAF

        for category in Category:
            assert CATEGORY_LEAF[category]
            assert category.label

    def test_application_received_is_a_first_class_category(self):
        assert Category.APPLICATION_RECEIVED.label == "Application Received"
        assert FolderPlan().for_category(Category.APPLICATION_RECEIVED) == (
            "Job Search/Received"
        )

    @pytest.mark.parametrize(
        "category,folder",
        [
            (Category.OFFER, "Job Search/Offers"),
            (Category.NETWORKING, "Job Search/Networking"),
            (Category.UNSOLICITED, "Job Search/Unsolicited"),
        ],
    )
    def test_the_newer_categories_have_their_own_folders(self, category, folder):
        assert FolderPlan().for_category(category) == folder

    def test_every_category_has_a_distinct_colour(self):
        from models import CATEGORY_COLORS

        colours = [CATEGORY_COLORS[c] for c in Category]
        assert len(set(colours)) == len(colours)
        assert all(c.startswith("#") and len(c) == 7 for c in colours)


class TestOtherCategory:
    def test_every_member_has_a_label_and_leaf(self):
        for category in OtherCategory:
            assert category.label
            assert category.leaf

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("finance", OtherCategory.FINANCE),
            ("NEWSLETTER", OtherCategory.NEWSLETTER),
            ("not applicable", OtherCategory.NOT_APPLICABLE),
            ("security", OtherCategory.SECURITY),
        ],
    )
    def test_parse(self, raw, expected):
        assert OtherCategory.parse(raw) is expected

    def test_parse_rejects_unknown(self):
        assert OtherCategory.parse("CRYPTO") is None


# ==========================================================================
# FolderPlan
# ==========================================================================
class TestFolderPlan:
    def test_default_tree(self):
        plan = FolderPlan()
        assert plan.all_folders == (
            "Job Search",
            "Job Search/Interview",
            "Job Search/Next Steps",
            "Job Search/Offers",
            "Job Search/Received",
            "Job Search/Networking",
            "Job Search/Not Interested",
            "Job Search/Unsolicited",
            "Job Search/Needs Review",
        )

    def test_parent_precedes_children(self):
        plan = FolderPlan()
        folders = plan.all_folders
        assert folders[0] == plan.root
        assert all(f.startswith(plan.root) for f in folders[1:])

    def test_honours_server_delimiter(self):
        plan = FolderPlan(root="Job Search", delimiter=".")
        assert plan.for_category(Category.INTERVIEW) == "Job Search.Interview"
        assert plan.review_folder == "Job Search.Needs Review"

    def test_sanitises_dangerous_names(self):
        plan = FolderPlan(root='Job "Search"/Bad\\Name')
        assert '"' not in plan.root
        assert "\\" not in plan.root
        assert plan.root == "Job Search Bad Name"

    def test_empty_root_falls_back(self):
        assert FolderPlan(root="   ").root == "Job Search"

    def test_other_folders_only_returns_what_is_used(self):
        plan = FolderPlan()
        assert plan.other_folders([]) == ()
        assert plan.other_folders([OtherCategory.FINANCE]) == (
            "Sorted Mail",
            "Sorted Mail/Finance",
        )

    def test_other_folders_deduplicates(self):
        plan = FolderPlan()
        folders = plan.other_folders(
            [OtherCategory.FINANCE, OtherCategory.FINANCE, OtherCategory.SOCIAL]
        )
        assert folders == ("Sorted Mail", "Sorted Mail/Finance", "Sorted Mail/Social")

    def test_not_applicable_maps_to_other(self):
        plan = FolderPlan()
        assert plan.for_other_category(OtherCategory.NOT_APPLICABLE) == "Sorted Mail/Other"


def test_sanitize_folder_component_collapses_whitespace():
    assert sanitize_folder_component("  Job   Search \n ") == "Job Search"


# ==========================================================================
# Classification validation (protocol layer 2)
# ==========================================================================
class TestClassificationValidation:
    def base(self, **overrides):
        payload = {
            "summary": "A summary.",
            "is_job_related": True,
            "category": "INTERVIEW",
            "other_category": "NOT_APPLICABLE",
            "confidence_score": 0.97,
            "reasoning": "Because.",
        }
        payload.update(overrides)
        return payload

    def test_clean_payload_passes_through_untouched(self):
        result = Classification.from_payload(self.base(), model="claude-opus-5")
        assert result.category is Category.INTERVIEW
        assert result.other_category is OtherCategory.NOT_APPLICABLE
        assert result.confidence_score == pytest.approx(0.97)
        assert result.adjustments == ()
        assert result.model == "claude-opus-5"
        assert result.ok

    def test_unknown_category_is_demoted_not_trusted(self):
        result = Classification.from_payload(self.base(category="MAYBE_INTERVIEW"))
        assert result.category is Category.UNCLASSIFIED_OTHER
        assert any("not in the allowed enum" in a for a in result.adjustments)

    def test_non_job_mail_cannot_keep_a_job_category(self):
        result = Classification.from_payload(
            self.base(is_job_related=False, category="INTERVIEW", other_category="FINANCE")
        )
        assert result.category is Category.UNCLASSIFIED_OTHER
        assert result.other_category is OtherCategory.FINANCE
        assert any("contradicts" in a for a in result.adjustments)

    def test_job_mail_cannot_keep_a_topic_bucket(self):
        result = Classification.from_payload(self.base(other_category="NEWSLETTER"))
        assert result.other_category is OtherCategory.NOT_APPLICABLE
        assert any("cleared to NOT_APPLICABLE" in a for a in result.adjustments)

    def test_non_job_mail_always_gets_a_topic(self):
        result = Classification.from_payload(
            self.base(is_job_related=False, category="UNCLASSIFIED_OTHER")
        )
        assert result.other_category is OtherCategory.OTHER

    def test_confident_unclassified_is_capped_below_threshold(self):
        result = Classification.from_payload(
            self.base(category="UNCLASSIFIED_OTHER", confidence_score=0.99)
        )
        assert result.confidence_score < 0.95
        assert any("cannot be a high-confidence" in a for a in result.adjustments)

    @pytest.mark.parametrize(
        "raw,expected",
        [
            (-3, 0.0),
            ("0.82", 0.82),
            ("87%", 0.87),
            (87, 0.87),
            (95.5, 0.955),
            (100, 1.0),
            (0, 0.0),
            (1, 1.0),
        ],
    )
    def test_confidence_is_coerced_and_clamped(self, raw, expected):
        result = Classification.from_payload(self.base(confidence_score=raw))
        assert result.confidence_score == pytest.approx(expected)

    @pytest.mark.parametrize("raw", [1.4, 101, 1.0001, 250])
    def test_ambiguous_overshoot_is_never_rounded_up_to_certainty(self, raw):
        """1.4 could be a sloppy 1.0 or a percentage. Guessing "certain" is the
        one direction this app must never guess in, so it becomes 0.0."""
        result = Classification.from_payload(self.base(confidence_score=raw))
        assert result.confidence_score == 0.0
        assert any("confidence_score" in a for a in result.adjustments)

    def test_percentage_rescale_is_reported(self):
        result = Classification.from_payload(self.base(confidence_score=87))
        assert any("looked like a percentage" in a for a in result.adjustments)

    @pytest.mark.parametrize("raw", [None, "high", [], {}, float("nan"), float("inf"), True])
    def test_unusable_confidence_becomes_zero(self, raw):
        result = Classification.from_payload(self.base(confidence_score=raw))
        assert result.confidence_score == 0.0
        assert any("confidence_score" in a for a in result.adjustments)

    @pytest.mark.parametrize(
        "raw,expected", [("true", True), ("no", False), (1, True), (0, False), (None, False)]
    )
    def test_non_boolean_job_flag_is_coerced_and_flagged(self, raw, expected):
        result = Classification.from_payload(self.base(is_job_related=raw))
        assert result.is_job_related is expected
        assert any("is_job_related" in a for a in result.adjustments)

    def test_empty_text_fields_are_reported(self):
        result = Classification.from_payload(self.base(summary="  ", reasoning=""))
        assert "no summary" in result.summary
        assert "no reasoning" in result.reasoning
        assert len(result.adjustments) == 2

    def test_non_mapping_payload_is_a_failure(self):
        result = Classification.from_payload(["not", "an", "object"])
        assert not result.ok
        assert result.category is Category.UNCLASSIFIED_OTHER

    def test_usage_is_captured(self):
        payload = self.base()
        payload["_usage"] = {"input_tokens": 900, "output_tokens": 120}
        result = Classification.from_payload(payload)
        assert (result.input_tokens, result.output_tokens) == (900, 120)

    def test_failure_is_never_confident(self):
        result = Classification.failure("boom", model="m")
        assert result.confidence_score == 0.0
        assert result.category is Category.UNCLASSIFIED_OTHER
        assert result.error == "boom"
        assert not result.ok

    def test_category_label_reflects_both_levels(self):
        job = Classification.from_payload(self.base())
        assert job.category_label == "Interview"
        other = Classification.from_payload(
            self.base(is_job_related=False, category="UNCLASSIFIED_OTHER", other_category="FINANCE")
        )
        assert other.category_label == "Other · Finance & Bills"


# ==========================================================================
# Routing (protocol layer 3) - the core safety table
# ==========================================================================
class TestRouting:
    def item(self, **kwargs):
        classification_kwargs = kwargs.pop("classification", {})
        classification = Classification(
            summary="s",
            reasoning="r",
            **{
                "is_job_related": True,
                "category": Category.INTERVIEW,
                "other_category": OtherCategory.NOT_APPLICABLE,
                "confidence_score": 0.98,
                **classification_kwargs,
            },
        )
        return TriageItem(
            email=EmailMessage(uid="1"),
            classification=classification,
            folders=FolderPlan(),
            **kwargs,
        )

    # -- the happy path ---------------------------------------------------
    @pytest.mark.parametrize(
        "category,folder",
        [
            (Category.INTERVIEW, "Job Search/Interview"),
            (Category.NEXT_STEPS, "Job Search/Next Steps"),
            (Category.APPLICATION_RECEIVED, "Job Search/Received"),
            (Category.NOT_INTERESTED, "Job Search/Not Interested"),
        ],
    )
    def test_confident_job_mail_is_filed_and_pre_checked(self, category, folder):
        item = self.item(classification={"category": category})
        assert item.disposition is Disposition.MOVE
        assert item.target_folder == folder
        assert item.approved is True

    # -- the safety net ---------------------------------------------------
    @pytest.mark.parametrize("confidence", [0.0, 0.5, 0.899, 0.9499])
    def test_below_threshold_goes_to_needs_review_unchecked(self, confidence):
        item = self.item(classification={"confidence_score": confidence})
        assert item.disposition is Disposition.REVIEW
        assert item.target_folder == "Job Search/Needs Review"
        assert item.approved is False

    def test_threshold_is_inclusive(self):
        assert self.item(classification={"confidence_score": 0.95}).disposition is Disposition.MOVE
        assert self.item(classification={"confidence_score": 0.9499}).disposition is Disposition.REVIEW

    def test_custom_threshold_is_respected(self):
        item = self.item(classification={"confidence_score": 0.90}, threshold=0.85)
        assert item.disposition is Disposition.MOVE

    def test_unclassified_other_always_needs_review(self):
        item = self.item(
            classification={"category": Category.UNCLASSIFIED_OTHER, "confidence_score": 1.0}
        )
        assert item.disposition is Disposition.REVIEW
        assert item.approved is False

    def test_a_failed_analysis_never_moves_anything(self):
        item = TriageItem(
            email=EmailMessage(uid="1"),
            classification=Classification.failure("API exploded"),
            folders=FolderPlan(),
        )
        assert item.disposition is Disposition.REVIEW
        assert item.approved is False
        assert item.status_display == "Analysis failed"

    def test_error_with_high_confidence_still_reviews(self):
        classification = Classification(
            summary="s", reasoning="r", is_job_related=True,
            category=Category.INTERVIEW, confidence_score=1.0, error="transport blew up",
        )
        item = TriageItem(EmailMessage(uid="1"), classification, FolderPlan())
        assert item.disposition is Disposition.REVIEW

    # -- non-job mail -----------------------------------------------------
    def test_non_job_mail_is_left_alone_by_default(self):
        item = self.item(
            classification={
                "is_job_related": False,
                "category": Category.UNCLASSIFIED_OTHER,
                "other_category": OtherCategory.FINANCE,
                "confidence_score": 0.99,
            }
        )
        assert item.disposition is Disposition.LEAVE
        assert item.target_folder is None
        assert item.approved is False
        assert item.folder_display == "INBOX"
        assert item.is_actionable is False

    def test_non_job_mail_can_be_routed_to_review(self):
        item = self.item(
            classification={
                "is_job_related": False,
                "category": Category.UNCLASSIFIED_OTHER,
                "other_category": OtherCategory.SPAM,
                "confidence_score": 0.99,
            },
            non_job_routing=NonJobRouting.REVIEW,
        )
        assert item.disposition is Disposition.REVIEW
        assert item.target_folder == "Job Search/Needs Review"
        assert item.approved is False

    @pytest.mark.parametrize(
        "other,folder",
        [
            (OtherCategory.FINANCE, "Sorted Mail/Finance"),
            (OtherCategory.NEWSLETTER, "Sorted Mail/Newsletters"),
            (OtherCategory.SPAM, "Sorted Mail/Junk"),
            (OtherCategory.PERSONAL, "Sorted Mail/Personal"),
            (OtherCategory.TRAVEL, "Sorted Mail/Travel"),
        ],
    )
    def test_non_job_mail_can_be_filed_by_topic(self, other, folder):
        item = self.item(
            classification={
                "is_job_related": False,
                "category": Category.UNCLASSIFIED_OTHER,
                "other_category": other,
                "confidence_score": 0.99,
            },
            non_job_routing=NonJobRouting.FILE,
        )
        assert item.disposition is Disposition.MOVE
        assert item.target_folder == folder

    def test_topic_filing_is_not_pre_checked_unless_opted_in(self):
        kwargs = dict(
            classification={
                "is_job_related": False,
                "category": Category.UNCLASSIFIED_OTHER,
                "other_category": OtherCategory.FINANCE,
                "confidence_score": 0.99,
            },
            non_job_routing=NonJobRouting.FILE,
        )
        assert self.item(**kwargs).approved is False
        assert self.item(auto_approve_non_job=True, **kwargs).approved is True

    def test_uncertain_non_job_mail_is_left_where_it_is(self):
        """Needs Review is a job-search folder, and says so on the tin.

        A promotion the sorter is only 60% sure about is not "job mail I
        cannot place", it is not job mail at all. Sending it to Needs Review
        put ordinary post inside the job-search tree and was the single most
        confusing thing about the folder layout.
        """
        item = self.item(
            classification={
                "is_job_related": False,
                "category": Category.UNCLASSIFIED_OTHER,
                "other_category": OtherCategory.PROMOTION,
                "confidence_score": 0.60,
            },
            non_job_routing=NonJobRouting.LEAVE,
        )
        assert item.disposition is Disposition.LEAVE
        assert item.folder_short == "INBOX"

    def test_uncertain_non_job_mail_is_not_filed_by_topic_either(self):
        """Filing by topic needs the topic to be right, which it is not here."""
        item = self.item(
            classification={
                "is_job_related": False,
                "category": Category.UNCLASSIFIED_OTHER,
                "other_category": OtherCategory.PROMOTION,
                "confidence_score": 0.60,
            },
            non_job_routing=NonJobRouting.FILE,
        )
        assert item.disposition is Disposition.LEAVE

    def test_uncertain_job_mail_does_go_to_review(self):
        """Which is what the folder is actually for."""
        item = self.item(
            classification={
                "is_job_related": True,
                "category": Category.INTERVIEW,
                "confidence_score": 0.60,
            },
        )
        assert item.disposition is Disposition.REVIEW
        assert item.target_folder == "Job Search/Needs Review"

    def test_asking_for_non_job_mail_to_be_reviewed_still_works(self):
        """The one case where non-job mail belongs in a review folder is when
        the user has asked for exactly that."""
        item = self.item(
            classification={
                "is_job_related": False,
                "category": Category.UNCLASSIFIED_OTHER,
                "other_category": OtherCategory.PROMOTION,
                "confidence_score": 0.99,
            },
            non_job_routing=NonJobRouting.REVIEW,
        )
        assert item.disposition is Disposition.REVIEW

    # -- overrides --------------------------------------------------------
    def test_manual_override_wins_and_selects_the_row(self):
        item = self.item(classification={"confidence_score": 0.2})
        assert item.approved is False
        item.override_folder = "Job Search/Interview"
        assert item.target_folder == "Job Search/Interview"
        assert "manual" in item.folder_display

    def test_override_survives_a_leave_disposition(self):
        item = self.item(
            classification={
                "is_job_related": False,
                "category": Category.UNCLASSIFIED_OTHER,
                "other_category": OtherCategory.SOCIAL,
                "confidence_score": 0.99,
            }
        )
        assert item.target_folder is None
        item.override_folder = "Sorted Mail/Social"
        assert item.target_folder == "Sorted Mail/Social"
        assert item.is_actionable is True

    def test_moved_rows_are_not_actionable_again(self):
        item = self.item()
        item.moved = True
        assert item.is_actionable is False
        assert item.status_display == "Moved"

    def test_move_error_is_surfaced(self):
        item = self.item()
        item.move_error = "COPY failed"
        assert item.status_display.startswith("Failed:")


# ==========================================================================
# Summary
# ==========================================================================
class TestTriageSummary:
    def test_counts(self, item_factory):
        items = [
            item_factory(),  # interview, move, approved
            item_factory(classification_kwargs={"confidence_score": 0.3}),  # review
            item_factory(
                classification_kwargs={
                    "is_job_related": False,
                    "category": Category.UNCLASSIFIED_OTHER,
                    "other_category": OtherCategory.NEWSLETTER,
                    "confidence_score": 0.99,
                }
            ),  # leave
        ]
        summary = TriageSummary.build(items)
        assert (summary.total, summary.to_move, summary.needs_review, summary.leave_in_place) == (3, 1, 1, 1)
        assert summary.job_related == 2
        assert summary.approved == 1
        assert "3 messages" in summary.describe()

    def test_empty(self):
        summary = TriageSummary.build([])
        assert summary.total == 0
        assert "0 messages" in summary.describe()

    def test_token_totals_are_summed(self, item_factory):
        items = [
            item_factory(classification_kwargs={"input_tokens": 100, "output_tokens": 10}),
            item_factory(classification_kwargs={"input_tokens": 250, "output_tokens": 25}),
        ]
        summary = TriageSummary.build(items)
        assert summary.input_tokens == 350
        assert summary.output_tokens == 35


# ==========================================================================
# Time windows
# ==========================================================================
class TestTimeWindows:
    NOW = datetime(2026, 9, 4, 15, 30, tzinfo=timezone.utc)

    @pytest.mark.parametrize(
        "window,days",
        [
            (TimeWindow.LAST_24_HOURS, 1),
            (TimeWindow.LAST_3_DAYS, 3),
            (TimeWindow.LAST_7_DAYS, 7),
        ],
    )
    def test_presets(self, window, days):
        start, end = resolve_window(window, now=self.NOW)
        assert end == self.NOW
        assert start == self.NOW - timedelta(days=days)

    def test_custom_range(self):
        start_in = datetime(2026, 8, 1, tzinfo=timezone.utc)
        end_in = datetime(2026, 8, 15, tzinfo=timezone.utc)
        start, end = resolve_window(TimeWindow.CUSTOM, custom_start=start_in, custom_end=end_in)
        assert (start, end) == (start_in, end_in)

    def test_custom_range_swaps_reversed_dates(self):
        start, end = resolve_window(
            TimeWindow.CUSTOM,
            custom_start=datetime(2026, 8, 15, tzinfo=timezone.utc),
            custom_end=datetime(2026, 8, 1, tzinfo=timezone.utc),
        )
        assert start < end

    def test_custom_range_requires_a_start(self):
        with pytest.raises(ValueError):
            resolve_window(TimeWindow.CUSTOM)

    def test_naive_datetimes_are_treated_as_utc(self):
        start, _ = resolve_window(TimeWindow.CUSTOM, custom_start=datetime(2026, 8, 1))
        assert start.tzinfo is timezone.utc

    def test_from_name_falls_back(self):
        assert TimeWindow.from_name("NOPE") is TimeWindow.LAST_24_HOURS
        assert TimeWindow.from_name("LAST_7_DAYS") is TimeWindow.LAST_7_DAYS


class TestImapSinceDate:
    def test_format(self):
        assert imap_since_date(datetime(2026, 9, 4, tzinfo=timezone.utc)) == "04-Sep-2026"

    def test_is_locale_independent(self, monkeypatch):
        """strftime('%b') breaks IMAP search under a non-English locale."""
        import locale

        try:
            locale.setlocale(locale.LC_TIME, "de_DE.UTF-8")
        except locale.Error:
            pytest.skip("de_DE locale unavailable")
        try:
            assert imap_since_date(datetime(2026, 3, 1, tzinfo=timezone.utc)) == "01-Mar-2026"
        finally:
            locale.setlocale(locale.LC_TIME, "C")

    @pytest.mark.parametrize("month,abbr", list(enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1
    )))
    def test_all_months(self, month, abbr):
        assert imap_since_date(datetime(2026, month, 9)) == f"09-{abbr}-2026"


class TestEmailMessage:
    def test_display_helpers(self):
        message = EmailMessage(uid="1", sender_name="Dana", sender_email="d@x.com", subject="")
        assert message.sender_display == "Dana <d@x.com>"
        assert message.sender_short == "Dana"
        assert message.subject_display == "(no subject)"
        assert message.date_display() == "(no date)"

    def test_unknown_sender(self):
        assert EmailMessage(uid="1").sender_display == "(unknown sender)"

    def test_date_is_rendered_in_local_time(self):
        message = EmailMessage(uid="1", date=datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc))
        assert message.local_date().utcoffset() == datetime.now().astimezone().utcoffset()


# --------------------------------------------------------------------------
# Reading the clock
# --------------------------------------------------------------------------
class TestClock:
    """12-hour time, because that is how the times in this app get read aloud."""

    @pytest.mark.parametrize("hour, minute, expected", [
        (0, 5, "12:05 AM"),          # midnight is 12 AM, not 0 AM
        (7, 12, "7:12 AM"),          # no leading zero on the hour
        (11, 59, "11:59 AM"),
        (12, 0, "12:00 PM"),         # noon is 12 PM, the other easy one to get wrong
        (12, 30, "12:30 PM"),
        (13, 30, "1:30 PM"),
        (23, 45, "11:45 PM"),
    ])
    def test_reads_the_way_a_person_says_it(self, hour, minute, expected):
        assert clock(datetime(2026, 9, 5, hour, minute)) == expected

    def test_minutes_keep_their_leading_zero(self):
        assert clock(datetime(2026, 9, 5, 9, 5)) == "9:05 AM"


class TestMessageDates:
    def _message(self, moment):
        return EmailMessage(uid="1", subject="s", sender_name="Alex",
                            sender_email="you@icloud.example",
                            date=moment.astimezone())

    def test_recent_mail_shows_the_time_with_a_meridiem(self):
        now = datetime.now().astimezone().replace(hour=15, minute=0, second=0, microsecond=0)
        today = self._message(now - timedelta(hours=2))
        assert today.date_human(now) == f"Today  {clock((now - timedelta(hours=2)))}"
        assert today.date_human(now).endswith(("AM", "PM"))

    def test_older_mail_within_the_year_keeps_the_meridiem(self):
        now = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
        older = self._message(now - timedelta(days=40))
        rendered = older.date_human(now)
        assert rendered.endswith(("AM", "PM"))

    def test_a_year_old_message_drops_the_clock_entirely(self):
        now = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
        ancient = self._message(now - timedelta(days=400))
        assert ":" not in ancient.date_human(now)

    def test_the_tooltip_form_is_also_12_hour(self):
        moment = datetime(2026, 9, 5, 19, 20).astimezone()
        assert "7:20 PM" in self._message(moment).date_full()
