"""The briefing: what it puts first, and what it must never claim.

Most of these are about ranking and about honesty. A briefing is read once
and believed, so the two ways it can fail are burying the message that
mattered and saying a mailbox was quiet when nobody looked at it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import briefing
from briefing import Urgency
from models import (Category, Classification, EmailMessage, FolderPlan,
                    NonJobRouting, OtherCategory, TriageItem)


def item(subject="Subject", sender="someone@example.test", job=True,
         category=Category.APPLICATION_RECEIVED,
         other=OtherCategory.NOT_APPLICABLE, confidence=0.97, when=None,
         account="", error=None, routing=NonJobRouting.FILE) -> TriageItem:
    return TriageItem(
        email=EmailMessage(uid=subject, subject=subject, sender_email=sender,
                           sender_name=sender.split("@")[0], date=when,
                           account_label=account),
        classification=Classification(
            summary="", is_job_related=job, category=category,
            other_category=other, confidence_score=confidence,
            model="test", error=error),
        folders=FolderPlan(), non_job_routing=routing)


def when(hours_ago: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=hours_ago)


class TestTheHeadline:
    def test_it_counts_what_arrived_and_splits_it(self):
        rows = [item(subject=str(n)) for n in range(4)]
        rows += [item(subject=f"o{n}", job=False,
                      other=OtherCategory.NEWSLETTER) for n in range(6)]
        said = briefing.build(rows).headline.sentence()
        assert "10 message" in said
        assert "4 about your job search" in said
        assert "6 everything else" in said

    def test_one_message_is_not_plural(self):
        assert "1 message:" in briefing.build([item()]).headline.sentence()

    def test_it_says_how_long_a_window_it_read(self):
        end = datetime.now(timezone.utc)
        for hours, wording in ((1, "in the last hour"),
                               (6, "in the last 6 hours"),
                               (72, "in the last 3 days")):
            said = briefing.build(
                [item()], window_start=end - timedelta(hours=hours),
                window_end=end).headline.sentence()
            assert wording in said, f"{hours}h came out as: {said}"

    def test_it_says_how_much_is_ticked(self):
        rows = [item(subject=str(n)) for n in range(3)]
        rows[0].approved = False
        said = briefing.build(rows).headline.filing()
        assert "2 of 3" in said

    def test_nothing_at_all_is_said_plainly(self):
        report = briefing.build([])
        assert report.is_empty
        assert "Nothing arrived" in report.headline.sentence()


class TestWhatItPutsFirst:
    def test_an_offer_outranks_a_rejection_however_old(self):
        rows = [
            item(subject="Rejected", category=Category.NOT_INTERESTED,
                 when=when(0.1)),
            item(subject="Offer", category=Category.OFFER, when=when(48)),
        ]
        attention = briefing.build(rows).attention
        assert attention, "nothing was flagged as needing attention"
        assert attention[0].label == "Offer"

    def test_a_rejection_is_not_flagged_at_all(self):
        rows = [item(subject="Rejected", category=Category.NOT_INTERESTED)]
        assert briefing.build(rows).attention == []

    def test_receipts_and_newsletters_are_counted_not_flagged(self):
        rows = [item(subject=str(n), job=False,
                     other=OtherCategory.NEWSLETTER) for n in range(20)]
        report = briefing.build(rows)
        assert report.attention == []
        assert any(line.count == 20 for line in report.arrivals)

    def test_a_security_notice_is_flagged(self):
        """Non-job mail is not automatically unimportant."""
        rows = [item(subject="Sign-in code", job=False,
                     other=OtherCategory.SECURITY)]
        assert briefing.build(rows).attention[0].label == "Sign-in code"

    def test_what_the_sorter_could_not_read_is_flagged(self):
        rows = [item(subject="Odd one", error="the model timed out")]
        line = briefing.build(rows).attention[0]
        assert line.label == "Odd one"
        assert line.urgency == Urgency.UNSURE

    def test_confidence_breaks_a_tie_before_the_date_does(self):
        """Two interview invitations are not equally interesting if the
        sorter is guessing at one of them."""
        rows = [
            item(subject="Unsure", category=Category.INTERVIEW,
                 confidence=0.96, when=when(0.1)),
            item(subject="Certain", category=Category.INTERVIEW,
                 confidence=0.99, when=when(20)),
        ]
        assert briefing.build(rows).attention[0].label == "Certain"

    def test_the_list_does_not_run_away(self):
        rows = [item(subject=str(n), category=Category.OFFER)
                for n in range(60)]
        assert len(briefing.build(rows).attention) <= briefing.MOST

    def test_a_flagged_line_knows_which_row_it_came_from(self):
        rows = [item(subject="Dull", category=Category.NOT_INTERESTED),
                item(subject="Offer", category=Category.OFFER)]
        assert briefing.build(rows).attention[0].row == 1


class TestTheCounts:
    def test_arrivals_are_grouped_by_kind_with_their_folder(self):
        rows = [item(subject=str(n)) for n in range(5)]
        line = next(l for l in briefing.build(rows).arrivals
                    if l.label == "Application received")
        assert line.count == 5
        assert line.folder.endswith("Received")

    def test_folders_answer_what_apply_would_do(self):
        rows = [item(subject=str(n), category=Category.INTERVIEW)
                for n in range(3)]
        rows += [item(subject=f"o{n}", category=Category.OFFER)
                 for n in range(2)]
        folders = {l.label: l.count for l in briefing.build(rows).folders}
        assert folders["Job Search/Interview"] == 3
        assert folders["Job Search/Offers"] == 2

    def test_senders_only_lists_anyone_who_wrote_twice(self):
        rows = [item(subject=str(n), sender="lots@example.test")
                for n in range(4)]
        rows.append(item(subject="one", sender="once@example.test"))
        senders = {l.label for l in briefing.build(rows).senders}
        assert "lots@example.test" in senders
        assert "once@example.test" not in senders

    def test_unticked_rows_are_a_loose_end(self):
        rows = [item(subject=str(n)) for n in range(4)]
        rows[0].approved = False
        rows[1].approved = False
        waiting = " ".join(l.label for l in briefing.build(rows).waiting)
        assert "2 ready to file, not ticked" in waiting

    def test_a_loose_end_counts_itself_once(self):
        """The label says the number, so the count column must not say it
        again - the line used to read "5 x 5 ready to file"."""
        rows = [item(subject=str(n)) for n in range(5)]
        for row in rows[:3]:
            row.approved = False
        line = briefing.build(rows).waiting[0]
        assert "3 ready to file" in line.label
        assert line.count == 1, (
            "the count is in the words, so the count column has to stay at "
            "one or the line reads '3 x 3 ready to file'")


class TestItNeverClaimsMoreThanItKnows:
    def test_a_mailbox_is_only_called_quiet_when_rows_name_mailboxes(self):
        """On one account nothing carries a label, so every name passed in
        would look like a mailbox that produced nothing. "Checked, and
        there was nothing there" is the one line that must never be wrong.
        """
        rows = [item(subject=str(n)) for n in range(3)]
        report = briefing.build(rows, mailboxes=["a@x.test", "b@x.test"])
        assert report.quiet == []

    def test_a_mailbox_that_really_sent_nothing_is_named(self):
        rows = [item(subject=str(n), account="a@x.test") for n in range(3)]
        report = briefing.build(rows, mailboxes=["a@x.test", "b@x.test"])
        assert report.quiet == ["b@x.test"]

    def test_with_no_rows_every_mailbox_is_quiet(self):
        report = briefing.build([], mailboxes=["a@x.test", "b@x.test"])
        assert report.quiet == ["a@x.test", "b@x.test"]

    def test_it_reads_the_same_list_the_same_way_twice(self):
        """It is a report, not a process: nothing here may depend on when
        it ran or on what it did last time."""
        rows = [item(subject=str(n), category=Category.OFFER)
                for n in range(6)]
        first = briefing.build(rows).as_text()
        second = briefing.build(rows).as_text()
        assert first == second

    def test_it_changes_nothing_it_was_given(self):
        rows = [item(subject=str(n)) for n in range(4)]
        before = [(r.approved, r.override_folder) for r in rows]
        briefing.build(rows)
        assert [(r.approved, r.override_folder) for r in rows] == before


class TestTheTextVersion:
    def test_every_section_reaches_the_clipboard_text(self):
        rows = [item(subject="Offer", category=Category.OFFER),
                item(subject="Ack", sender="ats@example.test"),
                item(subject="Ack2", sender="ats@example.test")]
        rows[0].approved = False
        text = briefing.build(rows).as_text()
        for heading in ("Needs you", "What came in", "Where it is going",
                        "Who wrote", "Still waiting"):
            assert heading in text, f"{heading} is missing from the text"

    def test_an_empty_briefing_still_says_something(self):
        assert briefing.build([]).as_text().strip()


class TestTheDialog:
    def test_it_builds_and_clicking_a_line_asks_for_that_row(self, qtbot):
        from briefing_dialog import BriefingDialog

        rows = [item(subject="Dull", category=Category.NOT_INTERESTED),
                item(subject="Offer", category=Category.OFFER)]
        dialog = BriefingDialog(briefing.build(rows))
        qtbot.addWidget(dialog)
        asked = []
        dialog.show_row.connect(asked.append)
        from PySide6.QtWidgets import QPushButton
        links = [b for b in dialog.findChildren(QPushButton)
                 if b.text() == "Offer"]
        assert links, "the flagged message is not clickable"
        links[0].click()
        assert asked == [1]

    def test_copy_puts_the_whole_thing_on_the_clipboard(self, qtbot):
        from PySide6.QtGui import QGuiApplication

        from briefing_dialog import BriefingDialog

        rows = [item(subject="Offer", category=Category.OFFER)]
        report = briefing.build(rows)
        dialog = BriefingDialog(report)
        qtbot.addWidget(dialog)
        dialog._copy()
        clipboard = QGuiApplication.clipboard()
        assert clipboard.text() == report.as_text()

    def test_an_empty_briefing_shows_a_window_rather_than_nothing(self, qtbot):
        from briefing_dialog import BriefingDialog

        dialog = BriefingDialog(briefing.build([]))
        qtbot.addWidget(dialog)
        assert dialog.findChildren(type(dialog)) is not None
