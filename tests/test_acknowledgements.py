"""The commonest mail in a job search, and the least interesting.

Every applicant-tracking vendor writes "we got it, we'll read it, we'll be in
touch" differently and no two share a phrase, so a list of phrases catches
whichever vendor happened to be in the corpus. What they all do is the same
three moves, and counting moves catches the family.

Three real failures this fixes, all from one mailbox:

  * iCIMS mail, "Thank you very much for your recent application to the X
    position", matched nothing and sat in Needs Review at 0.55.
  * "We have received your application. If your experience aligns, we will
    reach out to discuss next steps" was read as an action item at 0.70,
    because every acknowledgement ends that way.
  * "We have filled the position with another candidate" was not a rejection,
    because the phrase list only had the passive voice.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rules_engine import (RuleClassifier, acknowledgement_score, normalize,
                          steps_are_only_promised)

import private_fixtures

FIXTURE = private_fixtures.path("acknowledgements.json")
pytestmark = pytest.mark.skipif(
    FIXTURE is None,
    reason="acknowledgements.json is " + private_fixtures.WHY)
CASES = json.loads(FIXTURE.read_text()) if FIXTURE else []


@pytest.fixture(scope="module")
def sorter():
    return RuleClassifier()


def verdict_for(sorter, row):
    return sorter.classify(subject=row["subject"], body=row["body"],
                           sender=row["sender"], links=row["links"],
                           list_unsubscribe=row["unsub"])


class TestTheWholeSet:
    @pytest.mark.parametrize("row", CASES, ids=[c["subject"][:30] for c in CASES])
    def test_every_category_is_right(self, sorter, row):
        got = verdict_for(sorter, row)
        want = row["truth_cat"]
        assert got.is_job_related is row["truth_job"]
        assert got.category.value == want, got.reasoning[:200]

    def test_most_plain_acknowledgements_file_without_review(self, sorter):
        """These need no reading, and reading them is the whole cost.

        Not all of them: pushing the weight far enough to file every one also
        filed a wrong message on the held-out set, and one wrong filing costs
        more than several unnecessary reviews. Five of six is where that line
        sits.
        """
        acks = [r for r in CASES if r["truth_cat"] == "APPLICATION_RECEIVED"]
        filed = [r for r in acks if verdict_for(sorter, r).confidence >= 0.95]
        assert len(filed) >= len(acks) - 1, (
            f"only {len(filed)} of {len(acks)} were confident enough to file")

    def test_nothing_is_confidently_wrong(self, sorter):
        for row in CASES:
            got = verdict_for(sorter, row)
            if got.confidence >= 0.95:
                assert got.category.value == row["truth_cat"], row["subject"]


class TestAcknowledgementScore:
    def test_two_moves_are_needed(self):
        """"Thank you for applying" opens a rejection too."""
        one = normalize("Thank you for applying to the Analyst position.")
        assert acknowledgement_score("", one)[0] == 0.0

    def test_the_icims_wording_is_recognised(self):
        body = normalize(
            "Thank you very much for your recent application to the "
            "Vulnerability Analyst position. Your resume will be reviewed by "
            "our recruiting staff, and we will contact you soon.")
        score, why = acknowledgement_score("", body)
        assert score >= 2.4 and len(why) >= 2

    @pytest.mark.parametrize("body", [
        "We have received your application and our team will review it. "
        "We will be in touch if your qualifications match.",
        "Your application has been successfully submitted. We will contact "
        "you if there is a suitable match.",
        "Thanks for applying! Our recruiting team is reviewing applications "
        "and someone will reach out if you are selected to progress.",
        "This message verifies that you have submitted an employment "
        "application. Your application will be reviewed by the department.",
    ])
    def test_the_family_is_covered_however_it_is_worded(self, body):
        assert acknowledgement_score("", normalize(body))[0] >= 2.4

    @pytest.mark.parametrize("body", [
        "Your order has shipped and will be reviewed for quality.",
        "We received your support ticket and will be in touch shortly.",
        "Thank you for your interest in our newsletter.",
    ])
    def test_it_does_not_fire_on_mail_that_is_not_about_a_job(self, body):
        """Firing here would be harmless on its own, job-relatedness is a
        separate question, but it should not be fabricating evidence."""
        sorter = RuleClassifier()
        got = sorter.classify(subject="", body=body, sender="x@y.example")
        assert got.is_job_related is False


class TestPromisedNextSteps:
    @pytest.mark.parametrize("text", [
        "we will reach out to discuss next steps",
        "should it be a good fit, we will contact you about next steps",
        "if selected to progress in the process, we will be in touch with next steps",
        "you will receive an email with next steps",
        "someone will contact you regarding next steps",
    ])
    def test_a_promise_is_not_a_request(self, text):
        assert steps_are_only_promised(normalize(text)) is True

    @pytest.mark.parametrize("text", [
        "Next steps: please complete the assessment below",
        "Here are your next steps. Upload your documents by Friday.",
        "Please review the next steps and confirm your availability",
        "we will contact you about next steps. Separately, next steps "
        "require you to sign the form.",
    ])
    def test_a_request_still_reads_as_one(self, text):
        assert steps_are_only_promised(normalize(text)) is False

    def test_no_mention_at_all_changes_nothing(self):
        assert steps_are_only_promised(normalize("hello there")) is False

    def test_an_acknowledgement_is_not_turned_into_an_action(self, sorter):
        got = sorter.classify(
            subject="We have received your application",
            body="Thank you for your interest in the Analytics Engineer "
                 "position. We have received your application. If your "
                 "experience and skills align with our needs, we will reach "
                 "out to discuss next steps.",
            sender="careers@northgate.example")
        assert got.category.value == "APPLICATION_RECEIVED"
        assert got.confidence >= 0.95


class TestRejectionsInTheActiveVoice:
    @pytest.mark.parametrize("body", [
        "We have filled the position with another candidate.",
        "We filled the role internally.",
        "Another candidate was selected for this position.",
        "We have decided to move forward with other candidates.",
        "We are unable to offer you a position at this time.",
        "We extended the offer to another candidate.",
    ])
    def test_someone_else_got_it(self, sorter, body):
        got = sorter.classify(
            subject="Update on your application",
            body="Thank you for your interest in the Engineer role. " + body,
            sender="no-reply@vireo.example")
        assert got.category.value == "NOT_INTERESTED", got.reasoning[:160]
