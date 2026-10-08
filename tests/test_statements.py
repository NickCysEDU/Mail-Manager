"""What a message states, read a sentence at a time (statements.py).

Every example is invented, and each is a way the real thing is written: the
reader is judged on the sentence, and on the context that undoes it.
"""

from __future__ import annotations

import pytest

import statements


def moves(subject, body, kind):
    return statements.read(subject, body).moves(kind)


class TestDeclines:
    @pytest.mark.parametrize("body", [
        "We have decided to move forward with other candidates.",
        "After reading every application, we xxxx xxx xxxxxxxx xx xxx xxxx xxx xxxxxxx "
        "this time.",
        "Unfortunately we will not be moving forward with your application.",
        "I'm sorry to say we won't be moving forward with further rounds at this time.",
        "There were many strong applicants, and xx xxx xxxxxx xx xxxx xxx xxxxxxx xx xxx "
        "xxxx round.",
        "We have now filled the open places for this role.",
        "We have since filled the junior platform engineer position.",
        "Timing was against us: xx xxxx xxxxxx xxx xxxxxxxx xxx xxxxxxx xxx.",
        "A quick note to say xxxx xxxxxxxx xx xx xxxxxx xxxx.",
        "The role has been filled.",
        "We have chosen to focus on other candidates for this one.",
        "Xx xxxx xx xxxxxx xxxxxxx xxxx xxxxxxxxxx xxxxx xxxxxxxxxxxxxx xxxx xxxxxxx "
        "xxxxx xxx xxxxx.",
        "You have xxx xxxx xxxxxxxx xxx xxxx xxxxxxxx.",
        "The panel did not select you for the shortlist.",
        "Your profile does not meet the minimum qualifications we set for this post.",
        "Unfortunately the role is not the right match for you.",
        "Thanks for telling us of your decision to withdraw your candidacy.",
        "Xxx xxx xx xxxxxx xxxxx xxxxxxxxxx xxx the analyst post.",
        "We xxx xxxxxx xx xxxxx xxx xxxx xxxxxxxx xx present, but we hope to stay in "
        "touch.",
    ])
    def test_a_decision_in_the_ways_it_is_written(self, body):
        assert moves("Your application", body, "declines"), body

    def test_a_hard_wrapped_sentence_is_still_one_sentence(self):
        body = ("In the end, we've decided to move forward with\n"
                "other candidates whose experience is a closer fit.")
        assert moves("Update", body, "declines")

    @pytest.mark.parametrize("body", [
        "If we decide not to move forward, we will let you know either way.",
        "Should you not be selected, we will keep your details on file.",
        "Positions are filled on a rolling basis as strong candidates apply.",
        "The position will remain open until filled.",
        "To apply again for this posting you would first need to withdraw your "
        "application.",
        "Your answers may not fully match the preferred qualifications, so we may take a "
        "second look later.",
        "Xx xxx xxx xx xxxx xx xxxxxxx xx xxxxx xxxxxxxxx personally.",
        # Found on a newsletter archive, where nobody applied for anything.
        "The difficulties were not considered a deterrent by the settlers.",
        "When the program is closed, so is the emulator.",
    ])
    def test_what_only_might_happen_is_not_a_decision(self, body):
        assert not moves("Your application", body, "declines"), body

    def test_a_quoted_reply_is_the_last_message_not_this_one(self):
        body = ("Thanks, see you on Thursday at ten.\n\n"
                "On Mon, 3 Aug 2026 at 09:12, Recruiting <jobs@acme.example> wrote:\n"
                "> We have decided to move forward with other candidates.")
        assert not moves("Re: Next steps", body, "declines")


class TestAcknowledgements:
    @pytest.mark.parametrize("subject,body", [
        ("Thank you for applying", "We have xxxxxxxx xxxx xxxxxxxxxxx xxx xxxx xxxxxx xx "
         "xxxxxxx."),
        ("Application received", "Hello, your application has been successfully submitted."),
        ("Your application", "This email confirms that xxx xxxx xxxxxxxxx xx xxxxxxxxxxx "
         "xxx the following role."),
        ("Thanks for applying to Northwind", "Thank you for applying to Northwind. Xx xxxx "
         "xxxxxxxxxx xx x xxxxx, xxxxxxx xxxx xxx xxxx xxxx xx xx xxxxx."),
    ])
    def test_an_application_acknowledged(self, subject, body):
        assert statements.read(subject, body).acknowledged

    def test_thanks_alone_is_how_a_rejection_opens(self):
        found = statements.read("Update", "Thank you for applying to the analyst role.")
        assert found.thanked
        assert not found.acknowledged

    def test_thanks_for_your_interest_is_any_letter_at_all(self):
        found = statements.read("Hello", "Thanks for your interest in the role. We will be "
                                          "in touch.")
        assert not found.acknowledged

    def test_a_promise_is_read_though_it_is_conditional(self):
        found = statements.read(
            "Xxxxx xxx xxx xxxxxxxx", "Xxxxx xxx xxx applying for the analyst role. If your "
            "skills are a strong match, xxx xxxx xxxx xxxx xxx xxxxxx team.")
        assert found.acknowledged

    def test_a_friend_thanking_you_for_your_cv_is_not_an_application(self):
        found = statements.read("Re: CV advice", "Thanks for sending me your resume - I'll "
                                                 "read it over the weekend.")
        assert not found.names_an_application


class TestRequests:
    @pytest.mark.parametrize("body", [
        "Please complete the assessment linked below within five days.",
        "Before we go further, you'll need to complete two short steps: a questionnaire "
        "and an assessment.",
        "We ask that you spend a moment telling us about your goals by completing a short "
        "survey.",
        "The next step in our hiring process is to complete the form below.",
        "Northwind xxxxxxx xxx xx xxxx xx xx xxxxx xxxxxxxxxxx xxxx xxxxxxxxx xxxx "
        "application.",
        "Following up on my note asking about your availability to complete the online "
        "tests.",
        "Sending the coding exercise over now; shout if it does not arrive.",
        "Your online assessment is now available.",
        "Meanwhile, please also complete the employment application form.",
    ])
    def test_a_step_asked_for(self, body):
        assert moves("Your application", body, "requests"), body

    @pytest.mark.parametrize("body", [
        "Depending on the team, you may be asked later to complete an assessment.",
        "Applicants who reach the final round will be asked to provide two references.",
        "Applicants who reach the final round are invited to complete an assessment.",
        "If selected, you will be asked to complete a background check.",
        "If you forgot your password, please click here to reset it before you log in.",
        "We are not asking you to complete anything further at this stage.",
    ])
    def test_a_step_that_may_come_later_is_not_asked_for(self, body):
        assert not moves("Your application", body, "requests"), body

    def test_a_survey_about_the_experience_asks_nothing_of_the_application(self):
        body = ("We have decided to move forward with other candidates. We would value "
                "hearing about your experience as a candidate. An independent group runs "
                "the questionnaire for us. Click here to start the questionnaire.")
        found = statements.read("Your status has been updated", body)
        assert found.declines
        assert not found.requests


class TestInvitations:
    @pytest.mark.parametrize("body", [
        "Use the link below to book a time that suits you.",
        "We xxxxx xxxx xx xxxxx xxxx xxxxx your background on a short call.",
        "Could you let me know your availability for a call this week?",
        "I've scheduled your phone interview for Tuesday at 10am.",
        "I'm sending you a calendar invitation for Thursday morning.",
        "I'll call you on Friday at 2pm.",
        "You have not yet completed your one-way video interview.",
        "Sorry for the slow reply; shall I get you rescheduled for next week?",
    ])
    def test_a_meeting_arranged(self, body):
        assert moves("Interview", body, "invitations"), body

    @pytest.mark.parametrize("body", [
        "Find tips for your interview on our careers blog.",
        "If selected, xx xxxx xxxxxxx xxx xx xxxxxxxx an interview.",
        "Our process typically involves a resume review and an interview with the team.",
        "We will not be scheduling interviews for this role.",
        "You may be invited to schedule a call with a recruiter.",
        "We would xxxx xx xxxxxx xxx xx xxxx xxx xxxxxx network.",
        "This new publishing schedule means I'll have more time to get out and meet "
        "readers.",
    ])
    def test_a_meeting_described_is_not_one_arranged(self, body):
        assert not moves("Your application", body, "invitations"), body


class TestOffers:
    @pytest.mark.parametrize("body", [
        "We are delighted to offer you the position of analyst.",
        "We would like to extend you a formal offer of employment.",
        "Your offer letter is attached; please sign and return it by Friday.",
    ])
    def test_an_offer_made(self, body):
        assert moves("Good news", body, "offers"), body

    @pytest.mark.parametrize("body", [
        "Any offer of employment is contingent on a background check.",
        "We cannot offer you the position on this occasion.",
        "If you accept the offer, your start date will be confirmed separately.",
        "We are happy to offer you early access to our jobs newsletter.",
    ])
    def test_an_offer_mentioned_is_not_one_made(self, body):
        assert not moves("Your application", body, "offers"), body


class TestUnconditional:
    def test_conditions_and_hedges_are_dropped(self):
        text = statements.unconditional(
            "Update", "Xx xxx xxxxxx xx xxxx xxx xxxxxxx. If another opening comes up, "
            "we will get in touch to check your availability. You may be contacted again.")
        assert "xxxxxx xx xxxx xxx xxxxxxx" xx text
        assert "check your availability" not in text
        assert "contacted again" not in text
