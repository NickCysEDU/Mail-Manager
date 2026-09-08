"""Meetings that are about a job, postings that carry no process words.

Two shapes the sorter used to score at exactly zero, both found in real mail:

  * "Please use this link to schedule a 20-minute Google Meet call" — a call
    is being proposed, and every fixed phrase for that ("schedule a call")
    breaks the moment somebody says how long it will take.
  * A job description mailed to yourself. It contains not one word a hiring
    process uses. It is all headings.

The near-misses matter as much as the hits. A dentist, a school and a sales
team all book calls in the same words, and a job board quotes the same
headings a posting does.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rules_engine import (RuleClassifier, job_board_blast, job_posting_score,
                          meeting_request_score, normalize,
                          professional_context_score, unwrap_links)

FIXTURE = Path(__file__).parent / "fixtures" / "meetings.json"
CASES = json.loads(FIXTURE.read_text())


@pytest.fixture(scope="module")
def sorter():
    return RuleClassifier()


def verdict_for(sorter, row):
    return sorter.classify(
        subject=row["subject"], body=row["body"], sender=row["sender"],
        links=row["links"], list_unsubscribe=row["unsub"])


class TestTheWholeSet:
    @pytest.mark.parametrize("row", CASES, ids=[c["subject"][:28] for c in CASES])
    def test_job_or_not_is_right_for_every_case(self, sorter, row):
        got = verdict_for(sorter, row)
        assert got.is_job_related is row["truth_job"], (
            f"{row['subject']!r}: {got.reasoning[:160]}")

    def test_nothing_is_confidently_wrong(self, sorter):
        """Being unsure is fine. Filing the wrong thing at 0.95 is not."""
        for row in CASES:
            got = verdict_for(sorter, row)
            if got.confidence >= 0.95:
                assert got.is_job_related is row["truth_job"], row["subject"]


class TestMeetingRequests:
    @pytest.mark.parametrize("text", [
        "please use this link to schedule a 20-minute Google Meet call with Erik",
        "I'd like to set up a quick 15 min intro chat this week",
        "let's book a 30 minute Zoom conversation early next week",
        "would you be free to jump on a brief Teams call on Thursday?",
        "happy to arrange a 45-minute video conversation",
        "can we find some time to talk next week",
        "let me know your availability",
        "what times work for you?",
        "grab 20 minutes tomorrow",
    ])
    def test_a_meeting_is_recognised_however_it_is_worded(self, text):
        score, why = meeting_request_score(normalize(text), "")
        assert score > 0, f"missed: {text!r}"
        assert why

    @pytest.mark.parametrize("text", [
        "your order has shipped and will arrive on Tuesday",
        "here is the report you asked for",
        "the invoice for August is attached",
        "thanks for lunch yesterday",
    ])
    def test_ordinary_mail_is_not_a_meeting(self, text):
        assert meeting_request_score(normalize(text), "")[0] == 0.0

    def test_a_meeting_alone_says_nothing_about_a_job(self):
        """This is the whole design: the two questions are kept apart."""
        dentist = "please use this link to schedule a 30-minute appointment"
        assert meeting_request_score(normalize(dentist), "")[0] > 0
        assert professional_context_score(normalize(dentist), "")[0] == 0.0


class TestJobPostings:
    def test_a_description_is_recognised_without_process_words(self):
        body = normalize(
            "JOB SUMMARY\nContributes to the Distributed Apps team.\n"
            "JOB RESPONSIBILITIES\nCodes, tests and debugs programs.\n"
            "QUALIFICATIONS\nBachelor's degree or equivalent.")
        score, why = job_posting_score("", body)
        assert score >= 2.6 and len(why) >= 2

    def test_one_heading_is_not_a_posting(self):
        """"Requirements" appears in mail that is nothing to do with a job."""
        body = normalize("Requirements: bring your own laptop to the workshop.")
        assert job_posting_score("", body)[0] == 0.0

    def test_a_posting_on_a_mailing_list_is_discounted(self):
        body = normalize("Job summary and qualifications inside. "
                         "Responsibilities listed. Equal opportunity employer.")
        alone = job_posting_score("", body)[0]
        listed = job_posting_score("", body, "<https://x.example/unsub>")[0]
        assert 0 < listed < alone


class TestJobBoardBlasts:
    def test_a_board_writing_to_a_list_is_not_a_job_search(self):
        score, why = job_board_blast(
            normalize("companies hiring analysts now"),
            normalize("browse hundreds of openings and apply in one click"),
            "<https://board.example/unsub>")
        assert score > 0 and "job board" in why

    def test_the_same_words_from_a_person_are_not_a_blast(self):
        """No unsubscribe header means somebody wrote to you."""
        assert job_board_blast(
            normalize("companies hiring analysts now"),
            normalize("browse hundreds of openings"), "")[0] == 0.0


class TestWrappedLinks:
    def test_a_tracker_does_not_hide_the_destination(self):
        wrapped = ("https://ee1c.streak-link.example/DBv8/"
                   "https%3A%2F%2Fcalendar.app.google%2FCqAdg")
        assert "calendar.app.google" in unwrap_links([wrapped])

    def test_a_doubly_wrapped_link_is_opened_too(self):
        import urllib.parse
        inner = urllib.parse.quote("https://calendly.com/x/intro", safe="")
        outer = "https://click.example/r/" + urllib.parse.quote(inner, safe="")
        assert "calendly.com" in unwrap_links([outer])

    def test_an_ordinary_link_survives_unchanged(self):
        assert "example.com/careers" in unwrap_links(["https://example.com/careers"])

    @pytest.mark.parametrize("junk", ["", "%", "%zz", "https://%%%", "%25%25%25"])
    def test_a_malformed_link_does_not_raise(self, junk):
        unwrap_links([junk])
