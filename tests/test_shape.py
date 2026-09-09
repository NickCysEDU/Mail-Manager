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

import pytest

from models import OtherCategory
from rules_engine import (RuleClassifier, entity_scores, looks_like_a_person,
                          personal_register, sender_purpose)


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
        """"Collection point" is a phrase, so it lives with the words now —
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
