"""Recognising a message that contains no word saying what it is.

This is the gap between a phrase list and a reader. Taken from a held-out set
the sorter scored 16.7% on:

    "It's here"      Collection point 4, Stockport. Bring the QR code or the
                     order number. We'll hold it for seven days.
    "Seat 14C"       FR7712 STN to DUB, Tuesday. Bags close 40 minutes before.
    "that thing on   Can we push it to half four? School run has moved.
     Thursday"

A parcel, a flight and a friend. Not one of them contains "delivery",
"flight" or any other word a list could hold, and no list can ever be long
enough, because there is no phrase to list. What a person reads instead is
the shape of the thing: a flight number next to an airport pair, a mailbox
called bookings@, two people arranging something.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from models import OtherCategory
from rules_engine import (RuleClassifier, entity_scores, looks_like_a_person,
                          personal_register, sender_purpose)


ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def sorter():
    return RuleClassifier()


class TestWhoTheSenderIs:
    """The part before the @ says what the mailbox is for, and was unused."""

    @pytest.mark.parametrize("address, topic", [
        ("offers@boots.example", OtherCategory.PROMOTION),
        ("deals@shop.example", OtherCategory.PROMOTION),
        ("newsletter@paper.example", OtherCategory.NEWSLETTER),
        ("billing@vodafone.example", OtherCategory.FINANCE),
        ("accounts@utility.example", OtherCategory.FINANCE),
        ("shipping@argos.example", OtherCategory.SHIPPING),
        ("tracking@courier.example", OtherCategory.SHIPPING),
        ("security@bank.example", OtherCategory.SECURITY),
        ("bookings@barsesta.example", OtherCategory.EVENT),
        ("tickets@venue.example", OtherCategory.EVENT),
    ])
    def test_the_mailbox_name_points_at_a_topic(self, address, topic):
        found, why = sender_purpose(address)
        assert topic in found and why

    @pytest.mark.parametrize("address", [
        "r.mccarthy@fastmail.example", "jane.doe@example.com", "sam@example.com",
    ])
    def test_a_person_is_recognised(self, address):
        assert looks_like_a_person(address) is True

    @pytest.mark.parametrize("address", [
        "noreply@argos.example", "no-reply@bank.example", "info@company.example",
        "support@vendor.example", "notifications@app.example",
        "mailer-daemon@host.example",
    ])
    def test_a_department_is_not_a_person(self, address):
        assert looks_like_a_person(address) is False

    def test_an_unremarkable_mailbox_claims_nothing(self):
        assert sender_purpose("careers@company.example") == ({}, [])

    @pytest.mark.parametrize("junk", ["", "   ", "@", "no-at-sign", "a@@b"])
    def test_junk_does_not_raise(self, junk):
        sender_purpose(junk)
        looks_like_a_person(junk)


class TestShapesRatherThanWords:
    def test_a_flight(self):
        found, why = entity_scores(
            "", "", "Seat 14C",
            "FR7712 STN to DUB, Tuesday. Bags close 40 minutes before. "
            "Your reference is J4KP2W.")
        assert OtherCategory.TRAVEL in found
        assert any("STN" in reason for reason in why), why

    def test_a_parcel(self, sorter):
        """"Collection point" is a phrase, so it lives with the words now,
        what matters is that the message still reads as a parcel."""
        got = sorter.classify(
            subject="It's here",
            body="Collection point 4, Stockport. Bring the QR code or the "
                 "order number. We'll hold it for seven days.",
            sender="noreply@argos.example")
        assert got.other_category is OtherCategory.SHIPPING

    def test_a_direct_debit(self):
        found, _why = entity_scores(
            "", "", "Sorry we missed you",
            "Your Direct Debit of 28.00 could not be collected on the 3rd. "
            "We'll try again on the 17th.")
        assert OtherCategory.FINANCE in found

    def test_a_meter_reading(self):
        found, _why = entity_scores(
            "", "", "Meter reading needed",
            "We haven't had a reading since June so the last three bills were "
            "estimates. Send one this week and we'll true it up.")
        assert OtherCategory.FINANCE in found

    def test_a_restaurant_table(self):
        found, _why = entity_scores(
            "", "", "Table for 4, Friday 8pm",
            "Confirmed under Hale. We hold tables for fifteen minutes.")
        assert OtherCategory.EVENT in found

    def test_case_matters_and_is_not_folded_away(self):
        """Normalising lowercases, and case is half of what makes a flight
        number look like a flight number."""
        upper = entity_scores("", "", "", "FR7712 STN to DUB")[0]
        lower = entity_scores("", "", "", "fr7712 stn to dub")[0]
        assert OtherCategory.TRAVEL in upper
        assert lower.get(OtherCategory.TRAVEL, 0) < upper[OtherCategory.TRAVEL]

    def test_no_single_shape_can_decide_on_its_own(self):
        """One amount of money is not a bank statement."""
        found, _why = entity_scores("", "", "Lunch", "It came to 12.40 each.")
        assert found.get(OtherCategory.FINANCE, 0.0) < 1.6

    def test_shapes_never_exceed_their_ceiling(self):
        found, _why = entity_scores(
            "", "", "Everything at once",
            "FR7712 STN to DUB seat 14C gate B12 boarding reference J4KP2W "
            "bags close")
        assert all(value <= 3.4 for value in found.values())

    @pytest.mark.parametrize("junk", ["", "\x00", "%" * 200, "A" * 5000])
    def test_junk_does_not_raise(self, junk):
        entity_scores("", "", junk, junk)


class TestOnePersonWritingToAnother:
    def test_a_note_from_a_friend(self):
        score, why = personal_register(
            "that thing on Thursday",
            "Can we push it to half four? School run has moved. Sorry, I know "
            "you rearranged once already.",
            "Rob <r.mccarthy@fastmail.example>")
        assert score >= 2.0 and why

    def test_a_mailing_list_is_never_a_person(self):
        assert personal_register(
            "hello", "Can we meet? Thanks!", "friend@example.com",
            list_unsubscribe="<https://x.example/unsub>") == (0.0, [])

    def test_warmth_from_a_seller_is_not_warmth(self):
        """"Re: our conversation" is the oldest trick in unsolicited mail."""
        score, _why = personal_register(
            "Re: our conversation",
            "Hi! Sorry for the delay. Click here to claim your $500 reward "
            "now - limited time only, act now!",
            "Dave <dave@promo.example>")
        assert score == 0.0

    def test_a_page_of_links_is_not_a_note(self):
        score, _why = personal_register(
            "Hello", "Can we meet on Thursday? Thanks!",
            "sam.hale@example.com",
            links=["https://a.example", "https://b.example",
                   "https://c.example", "https://d.example"])
        plain = personal_register(
            "Hello", "Can we meet on Thursday? Thanks!", "sam.hale@example.com")[0]
        assert score < plain

    def test_a_transactional_notice_does_not_read_as_a_person(self):
        score, _why = personal_register(
            "Your order has shipped",
            "Order 4471 dispatched. Tracking number GB889201773.",
            "noreply@shop.example")
        assert score < 2.0


class TestItStillRefusesToBeCertain:
    """Shape decides which topic wins. It never decides how sure we are."""

    def test_a_reading_built_on_shape_alone_is_not_filed(self, sorter):
        got = sorter.classify(
            subject="Seat 14C",
            body="FR7712 STN to DUB, Tuesday. Bags close 40 minutes before.",
            sender="noreply@ryanair.example")
        assert got.other_category is OtherCategory.TRAVEL
        assert got.confidence < 0.95, "shape alone must not authorise a move"

    def test_words_can_still_earn_certainty(self, sorter):
        got = sorter.classify(
            subject="Your security code",
            body="Enter 023844 to approve this sign in. Do not share this "
                 "code with anyone. If you did not request it, ignore this.",
            sender="no-reply@bank.example")
        assert got.other_category is OtherCategory.SECURITY
        assert got.confidence >= 0.90


class TestTheLiteralPrefilter:
    """The anchor test must only ever say "definitely not"."""

    def test_it_agrees_with_the_regex_on_every_signal(self):
        """Exhaustive: for every signal in every table, on realistic text.

        The prefilter is a claim about the patterns - that a matching phrase
        always leaves its longest word intact in the tightened text. This
        checks that claim against every signal the app ships rather than
        trusting the argument.
        """
        from rules_engine import _Matcher, normalize, tighten
        from rules_engine import (_CATEGORY_TABLES, JOB_CONTEXT_SIGNALS,
                                  NON_JOB_SIGNALS, TOPIC_SIGNALS)

        tables = [table for _, table in _CATEGORY_TABLES]
        tables += list(TOPIC_SIGNALS.values())
        tables += [JOB_CONTEXT_SIGNALS, NON_JOB_SIGNALS]

        disagreements = []
        for table in tables:
            for signal in table:
                matcher = _Matcher(signal)
                if not matcher.anchor:
                    continue
                for text in (signal.phrase,
                             f"Hello, {signal.phrase} - regards",
                             signal.phrase.upper(),
                             signal.phrase.replace(" ", "  "),
                             signal.phrase.replace(" ", ".")):
                    normalized, tightened = normalize(text), tighten(text)
                    filtered = matcher.hit(normalized, tightened)
                    unfiltered = matcher._hit(normalized, tightened)
                    if filtered != unfiltered:
                        disagreements.append((signal.phrase, text))
        assert disagreements == [], disagreements[:5]

    def test_it_rejects_text_that_cannot_match(self):
        from rules_engine import Signal, _Matcher, normalize, tighten
        matcher = _Matcher(Signal("invite you to interview", 3.0))
        text = "your parcel is out for delivery today"
        assert matcher.hit(normalize(text), tighten(text)) == 0.0

    def test_a_sender_signal_skips_the_prefilter(self):
        """A sender arrives untightened, so the anchor test would misfire."""
        from rules_engine import Signal, _Matcher
        matcher = _Matcher(Signal("royalmail", 2.0, field="sender"))
        assert matcher.sender_hit("no-reply@royalmail.com") == 1.0

    def test_short_phrases_get_no_anchor(self):
        from rules_engine import Signal, _Matcher
        assert _Matcher(Signal("we are", 1.0)).anchor == ""


class TestAHiringMailbox:
    """Who sent it is context a phrase table cannot read."""

    @pytest.mark.parametrize("sender,word", [
        ("Careers <no-reply@brightpath.example>", "careers"),
        ("Talent <hiring@vellum.example>", "talent"),
        ("Recruitment <careers@stanfield.example>", "recruitment"),
        ("recruiter@acme.example", "recruiter"),
        ("Talent Acquisition <ta@acme.example>", "talent acquisition"),
        ("People Team <people.team@acme.example>", "people team"),
        ("jobs@acme.example", "jobs"),
    ])
    def test_it_is_recognised(self, sender, word):
        from rules_engine import hiring_mailbox
        assert hiring_mailbox(sender) == word

    @pytest.mark.parametrize("sender", [
        "Imogen Blake <i.blake@harlow-tech.example>",
        "no-reply@amazon.example",
        "billing@utility.example",
        "Hazel Croft <hazel@croftandco.example>",
        "",
        "not an address at all",
    ])
    def test_an_ordinary_sender_is_not(self, sender):
        from rules_engine import hiring_mailbox
        assert hiring_mailbox(sender) == ""

    def test_it_stops_a_rejection_reading_as_a_note_from_a_friend(self):
        """The warmth in a rejection was what made personal_register fire."""
        from rules_engine import personal_register
        body = ("Thank you for the time you put into this. On this occasion "
                "the panel has chosen someone whose background sits closer "
                "to the brief. We'd be glad to hear from you again.")
        warm, _ = personal_register("An update", body,
                                    "Imogen <i.blake@harlow.example>")
        assert warm > 0, "the same words from a person do read as personal"

        from_careers, why = personal_register(
            "An update", body, "Careers <no-reply@brightpath.example>")
        assert from_careers == 0.0
        assert why == []


class TestConditionalSignals:
    """Weak words the context has licensed."""

    def test_they_do_nothing_without_a_hiring_sender(self):
        from rules_engine import RuleClassifier
        engine = RuleClassifier()
        verdict = engine.classify(
            subject="Terms attached",
            body="The paperwork is attached. As we discussed, starting the 6th.",
            sender="Hazel Croft <hazel@croftandco.example>")
        assert not verdict.is_job_related, "a letting agent is not an offer"

    def test_they_fire_from_a_careers_mailbox(self):
        from rules_engine import RuleClassifier
        from models import Category
        engine = RuleClassifier()
        verdict = engine.classify(
            subject="Got it",
            body=("This is just to say your details are with us and the team "
                  "will look at them over the next fortnight. No need to do "
                  "anything."),
            sender="Recruitment <careers@stanfield.example>")
        assert verdict.is_job_related
        assert verdict.category is Category.APPLICATION_RECEIVED

    def test_a_paraphrased_rejection_is_still_a_rejection(self):
        from rules_engine import RuleClassifier
        from models import Category
        engine = RuleClassifier()
        verdict = engine.classify(
            subject="An update",
            body=("Thank you for the time you put into this. On this occasion "
                  "the panel has chosen someone whose background sits closer "
                  "to the brief. We'd be glad to hear from you again."),
            sender="Careers <no-reply@brightpath.example>")
        assert verdict.is_job_related
        assert verdict.category is Category.NOT_INTERESTED


class TestTieBreaking:
    """Two topics on the same score must not be settled by table order."""

    def test_the_order_of_the_tables_does_not_decide(self, monkeypatch):
        """Reverse every table's order; the verdicts must not move."""
        import json
        import rules_engine
        from tools import evaluate

        fixture = ROOT / "tests" / "fixtures" / "labelled.json"
        rows = json.loads(fixture.read_text())
        before = evaluate.score(rows)[1]

        original = dict(rules_engine.TOPIC_SIGNALS)
        try:
            for topic, table in original.items():
                rules_engine.TOPIC_SIGNALS[topic] = tuple(reversed(table))
            after = evaluate.score(rows)[1]
        finally:
            rules_engine.TOPIC_SIGNALS.clear()
            rules_engine.TOPIC_SIGNALS.update(original)
        assert after == before

    def test_the_more_specific_topic_wins_a_tie(self):
        from models import OtherCategory
        from rules_engine import _topic_rank
        assert _topic_rank(OtherCategory.SECURITY) < _topic_rank(OtherCategory.OTHER)
        assert _topic_rank(OtherCategory.TRAVEL) < _topic_rank(OtherCategory.PERSONAL)
        assert _topic_rank(OtherCategory.PERSONAL) < _topic_rank(OtherCategory.OTHER)

    def test_every_topic_has_a_place_in_the_order(self):
        from models import OtherCategory
        from rules_engine import _TOPIC_PRECEDENCE
        placed = set(_TOPIC_PRECEDENCE)
        for topic in OtherCategory:
            if topic is OtherCategory.NOT_APPLICABLE:
                continue
            assert topic in placed, f"{topic} would sort last by accident"

    def test_a_repeat_run_gives_the_same_answer(self):
        from rules_engine import RuleClassifier
        engine = RuleClassifier()
        args = dict(subject="Your statement is ready",
                    body="Your balance is £412.30 and the code is 448193.",
                    sender="no-reply@bank.example")
        first = engine.classify(**args)
        for _ in range(5):
            again = engine.classify(**args)
            assert again.other_category is first.other_category
            assert again.confidence == first.confidence


class TestTheRunnersUpLine:
    """What nearly won, and why it did not."""

    def item(self, classification):
        from models import EmailMessage, FolderPlan, TriageItem
        return TriageItem(email=EmailMessage(uid="1", subject="x"),
                          classification=classification, folders=FolderPlan())

    def verdict(self, **overrides):
        from models import Category, Classification, OtherCategory
        defaults = dict(summary="s", is_job_related=True,
                        category=Category.INTERVIEW,
                        other_category=OtherCategory.NOT_APPLICABLE,
                        confidence_score=0.9, reasoning="r", model="local rules")
        defaults.update(overrides)
        return Classification(**defaults)

    def test_the_winner_is_never_its_own_runner_up(self):
        """It is not always the top score - precedence can overrule one."""
        from triage_table import _runners_up
        line = _runners_up(self.verdict(scores={
            "INTERVIEW": 3.0, "APPLICATION_RECEIVED": 6.5}))
        assert "Interview" not in line
        assert "Application Received" in line

    def test_a_rival_that_outscored_the_winner_says_so(self):
        from triage_table import _runners_up
        line = _runners_up(self.verdict(scores={
            "INTERVIEW": 3.0, "APPLICATION_RECEIVED": 6.5}))
        assert "outranked" in line

    def test_an_ordinary_runner_up_is_given_as_a_share(self):
        from triage_table import _runners_up
        line = _runners_up(self.verdict(scores={
            "INTERVIEW": 10.0, "OFFER": 5.0}))
        assert "50% of the winner" in line
        assert "outranked" not in line

    def test_nothing_to_say_when_there_was_no_contest(self):
        from triage_table import _runners_up
        assert _runners_up(self.verdict(scores={"INTERVIEW": 4.0})) == ""
        assert _runners_up(self.verdict(scores={})) == ""

    def test_the_non_job_side_uses_its_own_winner(self):
        from models import Category, OtherCategory
        from triage_table import _runners_up
        line = _runners_up(self.verdict(
            is_job_related=False, category=Category.UNCLASSIFIED_OTHER,
            other_category=OtherCategory.SECURITY,
            scores={"SECURITY": 4.0, "FINANCE": 2.0}))
        assert "Security" not in line
        assert "Finance" in line

    def test_at_most_three_are_listed(self):
        from triage_table import _runners_up
        line = _runners_up(self.verdict(scores={
            "INTERVIEW": 9.0, "OFFER": 5.0, "NEXT_STEPS": 4.0,
            "NETWORKING": 3.0, "APPLICATION_RECEIVED": 2.0}))
        assert line.count("<br>") == 2

    def test_a_zero_score_is_not_a_runner_up(self):
        from triage_table import _runners_up
        assert _runners_up(self.verdict(scores={
            "INTERVIEW": 4.0, "OFFER": 0.0})) == ""
