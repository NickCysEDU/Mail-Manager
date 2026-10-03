"""Meetings that are about a job, postings that carry no process words.

Two shapes a phrase list scores at zero:

  * A call being proposed with its length in it ("schedule a 25-minute video
    call"): every fixed phrase for that breaks the moment somebody says how
    long it will take.
  * A job description mailed to yourself, which contains not one word a
    hiring process uses. It is all headings.

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

import private_fixtures

FIXTURE = private_fixtures.path("meetings.json")
pytestmark = pytest.mark.skipif(
    FIXTURE is None, reason="meetings.json is " + private_fixtures.WHY)
CASES = json.loads(FIXTURE.read_text()) if FIXTURE else []


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
        "xxx xxx xxxx xxxxx xx xxxxxxxx x 00-xxxxxx xxxxx xxxx xxxx Xxxx",
        "could we set up a short 10 min catch-up chat on Monday",
        "shall we book a 40 minute Zoom chat later this week",
        "are you free to hop on a quick Teams call on Wednesday?",
        "glad to arrange a 50-minute phone conversation",
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
        dentist = "use the link to book a 30-minute check-up appointment"
        assert meeting_request_score(normalize(dentist), "")[0] > 0
        assert professional_context_score(normalize(dentist), "")[0] == 0.0


class TestJobPostings:
    def test_a_description_is_recognised_without_process_words(self):
        body = normalize(
            "JOB SUMMARY\nJoins the data platform team.\n"
            "JOB RESPONSIBILITIES\nBuilds and looks after pipelines.\n"
            "QUALIFICATIONS\nA degree or equivalent experience.")
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
            normalize("see dozens of new roles and apply with one tap"),
            "<https://board.example/unsub>")
        assert score > 0 and "job board" in why

    def test_the_same_words_from_a_person_are_not_a_blast(self):
        """No unsubscribe header means somebody wrote to you."""
        assert job_board_blast(
            normalize("companies hiring analysts now"),
            normalize("see dozens of new roles"), "")[0] == 0.0


class TestWrappedLinks:
    def test_a_tracker_does_not_hide_the_destination(self):
        wrapped = ("https://t1.tracker.example/x9/"
                   "https%3A%2F%2Fcalendar.app.google%2Fabc123")
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
