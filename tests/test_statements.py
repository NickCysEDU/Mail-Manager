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
        "Having gone through every application, we took the decision to not progress "
        "you this time.",
        "Unfortunately we will not be moving forward with your application.",
        "I'm afraid we won't be progressing to further interviews at this stage.",
        "With so much strong interest, we are not able to take you on to the final "
        "stage.",
        "We have now filled the open places for this role.",
        "We have since filled the junior platform engineer position.",
        "Bad timing, sadly: we have just filled the vacancy you went for.",
        "Just letting you know that this opening has now closed.",
        "The role has been filled.",
        "We have chosen to focus on other candidates for this one.",
        "We are proceeding with applicants whose experience lines up more closely with "
        "what the team needs.",
        "You have not been chosen for this opening.",
        "The panel did not select you for the shortlist.",
        "Your profile does not meet the minimum qualifications we set for this post.",
        "Unfortunately the role is not the right match for you.",
        "Thanks for telling us of your decision to withdraw your candidacy.",
        "Your candidacy for the analyst post is no longer under consideration.",
        "We cannot offer you the job just now, though we would like to keep in touch.",
    ])
    def test_a_decision_in_the_ways_it_is_written(self, body):
        assert moves("Your application", body, "declines"), body

    @pytest.mark.parametrize("body,move", [
        ("We have wrapped up our search for the platform role and offered it to another "
         "candidate.", "the search ended with a hire"),
        ("After a lot of thought we have made a hire for the support desk post.",
         "the search ended with a hire"),
        ("Another applicant was selected for the analyst post.",
         "the search ended with a hire"),
        ("We won't be making you an offer for this role.", "an offer not made"),
        ("We regret that your application on this occasion was unsuccessful.",
         "an application unsuccessful"),
        ("Your submission has been deemed unsuccessful.", "an application unsuccessful"),
        ("The team has decided to go in a different direction with this hire.",
         "another direction taken"),
        ("We no longer have a position available on this team.", "no opening left"),
        # Said at length: the decision and the reader further apart.
        ("We have decided not to proceed any further in the recruitment process with you.",
         "not going on with you"),
        ("Your application has not on this occasion been selected.", "not selected"),
        ("As you requested, your candidacy for the Records Clerk opening has been "
         "withdrawn.", "the application withdrawn"),
        ("Your withdrawal request has been processed.", "the application withdrawn"),
        ("We have withdrawn your application from the Lab Technician search, as you asked.",
         "the application withdrawn"),
        ("We've decided to go with someone who has more networking experience.",
         "someone else chosen"),
        ("They went with somebody else, I'm afraid.", "someone else chosen"),
        ("The committee selected an internal candidate.", "someone else chosen"),
        ("Sadly your result did not meet the minimum standard for the next round.",
         "a bar not met"),
    ])
    def test_the_door_closed_in_other_words(self, body, move):
        assert move in moves("Your application", body, "declines"), body

    @pytest.mark.parametrize("body", [
        "I went with someone else to the gig last night.",
        "They went with someone from work to the concert.",
        "If we go with another candidate, we will let you know.",
        "We picked a different venue for the party.",
    ])
    def test_somebody_chosen_for_something_else(self, body):
        assert "someone else chosen" not in moves("Your application", body, "declines"), body

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
        "It may not be possible to reply to each applicant in person.",
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
        ("Thank you for applying", "Your application has reached us and our team will "
         "review it soon."),
        ("Application received", "Hello, your application has been successfully submitted."),
        ("Your application", "This is to confirm that your application for the role below "
         "has been submitted."),
        ("Thanks for applying to Northwind", "Thanks for applying to Northwind. Should your "
         "background fit, we will contact you."),
    ])
    def test_an_application_acknowledged(self, subject, body):
        assert statements.read(subject, body).acknowledged

    @pytest.mark.parametrize("body,move", [
        ("Thank you for creating a candidate profile with us.", "a candidate account opened"),
        ("Welcome to the Marlhaven careers portal!", "a candidate account opened"),
        ("We are still reviewing applications for the desk role.",
         "applications still being read"),
        ("The hiring team is working through candidates now.",
         "applications still being read"),
        ("Just a note to say your resume came through and is with the team.",
         "the application landed"),
        ("We haven't made a final decision on the role yet.", "no decision yet"),
        ("Your application remains under consideration.", "still in the running"),
        ("You've successfully applied for Records Clerk.", "the application arrived"),
        ("Successfully applied for Records Clerk at Fernmoor County.", "the application arrived"),
        ("You've applied to Records Clerk at Fernmoor County.", "the application arrived"),
        ("Got your CV for the weekend support spot - thanks!", "the application arrived"),
        ("The hiring team has been notified of your application.", "the application arrived"),
        ("Yours is on the shortlist for the hiring manager.", "still in the running"),
    ])
    def test_news_that_nothing_is_decided_is_an_acknowledgement(self, body, move):
        found = statements.read("Update", body)
        assert move in found.moves("acknowledgements"), body
        assert found.acknowledged

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
            "Thank you for applying", "Thanks for applying for the analyst role. If your "
            "skills fit what we need, our talent team will get back to you.")
        assert found.acknowledged

    def test_a_cv_got_for_a_spot_names_the_application(self):
        found = statements.read("Re: CV", "Got your CV for the weekend support spot - thanks!")
        assert found.names_an_application

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
        "Northwind asks you to complete a short diversity form about your application.",
        "Following up on my note asking about your availability to complete the online "
        "tests.",
        "Sending the coding exercise over now; shout if it does not arrive.",
        "Your online assessment is now available.",
        "Meanwhile, please also complete the employment application form.",
    ])
    def test_a_step_asked_for(self, body):
        assert moves("Your application", body, "requests"), body

    @pytest.mark.parametrize("body,move", [
        ("Could you send two short writing samples by Friday?", "a sample asked for"),
        ("Please share your portfolio before the panel meets.", "a sample asked for"),
        ("Your skills test expires tomorrow at noon.", "a test about to expire"),
        ("A reminder that the assessment closes on Sunday.", "a test about to expire"),
        ("Please upload copies of your diploma and your nursing licence to the portal.",
         "documents asked for"),
        ("Kindly bring a photo ID and proof of eligibility to work on Monday.",
         "documents asked for"),
        ("Before I can put you forward, I need you to formally apply on our careers site.",
         "a formal application asked for"),
        ("We'd like you to apply through the link below so I can submit you.",
         "a formal application asked for"),
        ("Please apply via our careers site and I'll flag it to the team.",
         "a formal application asked for"),
        ("Once you formally apply through the portal, I can move things along.",
         "a formal application asked for"),
    ])
    def test_a_step_asked_for_by_name(self, body, move):
        assert move in moves("Your application", body, "requests"), body

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
        "We'd love to talk more about your experience on a short call.",
        "Could you let me know your availability for a call this week?",
        "I've scheduled your phone interview for Tuesday at 10am.",
        "I'm sending you a calendar invitation for Thursday morning.",
        "I'll call you on Friday at 2pm.",
        "You have not yet completed your one-way video interview.",
        "Sorry for the slow reply; shall I get you rescheduled for next week?",
        "As one of our shortlisted candidates, we would like to schedule a phone screen "
        "with you.",
    ])
    def test_a_meeting_arranged(self, body):
        assert moves("Interview", body, "invitations"), body

    @pytest.mark.parametrize("body,move", [
        ("Something came up, so I need to move our interview to Thursday.",
         "a time being moved"),
        ("Could we push tomorrow's phone screen back an hour?", "a time being moved"),
        ("Great, let's do Wednesday at 4.", "a time agreed"),
        ("Tuesday at 11 works for me.", "a time agreed"),
        ("A few notes ahead of Friday's panel interview.", "a meeting ahead"),
        ("Some reading for before your interview on Monday.", "a meeting ahead"),
        ("Really looking forward to seeing you on Tuesday at 2.", "a time agreed"),
        ("Can't wait to have you in on Wednesday at 10.", "a time agreed"),
    ])
    def test_a_meeting_moved_agreed_or_ahead(self, body, move):
        assert move in moves("Interview", body, "invitations"), body

    @pytest.mark.parametrize("body", [
        "Find tips for your interview on our careers blog.",
        "Before your interview, read our guide to how we hire.",
        "We only write back to people we would like to interview.",
        "Shortlisted candidates will be contacted to schedule a phone screen.",
        "Candidates selected for interview may be asked to meet the team.",
        "We can't have you in on Wednesday at 10, sorry.",
        "If you are shortlisted, we will get in touch to arrange an interview.",
        "Our process typically involves a resume review and an interview with the team.",
        "We will not be scheduling interviews for this role.",
        "You may be invited to schedule a call with a recruiter.",
        "You're warmly invited to sign up to our talent network.",
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

    @pytest.mark.parametrize("body,move", [
        ("Your offer letter with everything we agreed last week is attached.",
         "the offer sent"),
        ("Could you let us know your decision on our offer by Monday?",
         "the offer to answer"),
        ("We're happy to hold the offer open until the end of the month.",
         "the offer to answer"),
        ("The bonus in your offer is paid each spring.", "a detail of the offer"),
        ("We'd love to have you join the Marlhaven team as our next support technician.",
         "a job offered in other words"),
        ("Your onboarding checklist is ready in the portal.", "a start being prepared"),
        ("Thank you for accepting our offer - we can't wait to have you.",
         "the offer accepted"),
        ("Please find attached the revised offer reflecting the new title.", "the offer sent"),
        ("We'd be thrilled to have you join us as our weekend IT tech.",
         "a job offered in other words"),
    ])
    def test_an_offer_and_what_follows_it(self, body, move):
        assert move in moves("Good news", body, "offers"), body

    @pytest.mark.parametrize("body", [
        "His first day is Monday, so do say hello.",
        "We'd love you to join our mailing list as a subscriber.",
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
            "Update", "We cannot take your application further. If a similar role opens, "
            "we will get in touch to check your availability. You may be contacted again.")
        assert "cannot take your application further" in text
        assert "check your availability" not in text
        assert "contacted again" not in text
