"""Everyday sorting: the topics that have nothing to do with a job search.

The labelled set in tests/fixtures is drawn from a real inbox during a job
hunt, so it barely covers these - six of the twelve topics have no example in
it at all. These cases are written rather than collected, which makes them a
specification of intended behaviour and a guard against the topics bleeding
into one another, not a measurement of accuracy. The pairs at the bottom are
the point: receipts against bank statements, couriers against shops, real
travel against travel used as a metaphor.
"""

from __future__ import annotations

import pytest

from models import OtherCategory
from rules_engine import RuleClassifier


@pytest.fixture(scope="module")
def rules():
    return RuleClassifier()


def sort(rules, subject, body, sender="someone@example.com", unsub=""):
    verdict = rules.classify(subject=subject, body=body, sender=sender,
                             list_unsubscribe=unsub)
    return verdict


# (subject, body, sender, unsubscribe header, expected topic)
CASES = [
    (
        "Your Blue Ridge Outfitters order",
        "Thanks for your purchase. Order #40192 comes to $84.20, charged to the "
        "card ending in 4417. We will email again when it ships.",
        "orders@blueridge.example", "",
        OtherCategory.RECEIPT,
    ),
    (
        "Order BR-40192 is on its way",
        "Good news, your package left our warehouse. Estimated delivery is "
        "Thursday. Tracking number 1Z999AA10123456784.",
        "auto-notify@ups.com", "",
        OtherCategory.SHIPPING,
    ),
    (
        "Your September statement is ready",
        "Your account statement is available to view. The minimum payment of "
        "$35.00 is due on 28 September.",
        "alerts@chase.com", "",
        OtherCategory.FINANCE,
    ),
    (
        "Your sign-in code",
        "Your code is 448192. It expires in 10 minutes. Do not share this code "
        "with anyone; we will never ask you for it.",
        "no-reply@accounts.example", "",
        OtherCategory.SECURITY,
    ),
    (
        "Trip to Lisbon: check-in is now open",
        "Online check in has opened for your flight on 12 October. Your booking "
        "reference is QK4T2M. Download your boarding pass before you travel.",
        "noreply@united.com", "",
        OtherCategory.TRAVEL,
    ),
    (
        "You're registered for the October workshop",
        "Registration confirmed. Doors open at 6pm and the venue is the Foundry "
        "on Mill Street. Add to calendar below.",
        "hello@eventbrite.com", "",
        OtherCategory.EVENT,
    ),
    (
        "Dad's 70th",
        "Hi love, are you free the weekend of the 14th? Nan is coming down and "
        "we thought we would do lunch. Give me a call when you get a minute. "
        "Love Mom",
        "mum@example.com", "",
        OtherCategory.PERSONAL,
    ),
    (
        "Timesheets close Friday",
        "A reminder that timesheets for this pay period close at 5pm Friday. "
        "Any PTO request for December should go in before the holiday schedule "
        "is published.",
        "people@company.example", "",
        OtherCategory.WORK,
    ),
    (
        "Sam Whitfield commented on your post",
        "Sam replied to your post and two other people liked it this week.",
        "notify@linkedin.com", "unsubscribe",
        OtherCategory.SOCIAL,
    ),
    (
        "The Tuesday Dispatch, issue 214",
        "In this week's issue: what the new rules mean, three charts worth your "
        "time, and the usual roundup. View this email in your browser.",
        "hello@substack.com", "unsubscribe",
        OtherCategory.NEWSLETTER,
    ),
    (
        "48 hours only: 30% off everything",
        "Our flash sale ends tonight. Use discount code AUTUMN30 at checkout for "
        "30 percent off, and shipping is free over $50.",
        "deals@shop.example", "unsubscribe",
        OtherCategory.PROMOTION,
    ),
    (
        "URGENT: your funds await",
        "Dear beloved, I am a barrister writing regarding an inheritance of which "
        "you are the beneficiary. Kindly reply to claim your prize.",
        "barrister@free.example", "",
        OtherCategory.SPAM,
    ),
    (
        "This Sunday at St Alban's",
        "Morning worship is at 10, with Holy Communion. The sermon series on "
        "Romans continues, and the coffee rota for October is on the "
        "noticeboard. Please pray for the Hendersons.",
        "office@stalbans-parish.example", "",
        OtherCategory.CHURCH,
    ),
    (
        # The one that made the category worth having: in form this is a
        # newsletter, and to the person reading it, it is not.
        "eNews from the parish - 4 September",
        "Greetings in the name of our Lord. Inside this week's bulletin: "
        "Sunday school restarts, the choir needs two more singers, and our "
        "Lutheran neighbours have invited us to their harvest festival.",
        "office@parish.example", "",
        OtherCategory.CHURCH,
    ),
]


@pytest.mark.parametrize(
    "subject, body, sender, unsub, expected",
    CASES, ids=[c[4].value.lower() for c in CASES])
def test_everyday_mail_lands_in_the_right_topic(
        rules, subject, body, sender, unsub, expected):
    verdict = sort(rules, subject, body, sender, unsub)
    assert verdict.is_job_related is False
    assert verdict.other_category is expected


def test_every_topic_with_a_folder_is_reachable(rules):
    """A topic nothing can ever be sorted into is a folder that stays empty."""
    reached = {expected for *_rest, expected in CASES}
    import profiles
    assert set(profiles.ALL_TOPICS) <= reached


# -- the confusable pairs, which is where a signal table actually earns its keep
class TestTellingSimilarMailApart:
    def test_a_receipt_is_not_a_bank_statement(self, rules):
        receipt = sort(rules, "Your receipt from Ferndale Coffee",
                       "Payment successful. You were charged $6.40. "
                       "Here is your receipt.", "receipts@square.example")
        statement = sort(rules, "Your statement is ready",
                         "Your monthly account statement is available. "
                         "Payment is due on the 3rd.", "alerts@bank.example")
        assert receipt.other_category is OtherCategory.RECEIPT
        assert statement.other_category is OtherCategory.FINANCE

    def test_a_shipping_update_is_not_a_receipt(self, rules):
        shipped = sort(rules, "Your parcel is out for delivery",
                       "Your shipment is out for delivery and should arrive "
                       "today. Track your package for live updates.",
                       "track@fedex.com")
        assert shipped.other_category is OtherCategory.SHIPPING

    def test_travel_words_in_marketing_are_not_filed_as_travel(self, rules):
        """The case that caught this: a course sold as a holiday.

        Travel vocabulary with nothing behind it should not be confident
        enough to file. It is allowed to guess travel, and it does; what it
        must not do is act on that guess, so this asserts the confidence
        rather than the label.
        """
        metaphor = sort(
            rules, "Welcome to Byte Lotus. Check-in is now open.",
            "Your stay begins today. Check in is now open for our new course, "
            "and there is a room booked for you on the leaderboard.",
            "hello@learning.example", unsub="unsubscribe")
        assert metaphor.confidence < 0.95      # held for review, not filed

    def test_a_real_booking_still_reads_as_travel_despite_the_header(self, rules):
        """The discount for bulk mail is a discount, not a veto."""
        booking = sort(
            rules, "Your itinerary for Lisbon",
            "Booking confirmation. Your flight departs at 07:15 and your "
            "boarding pass is attached. Booking reference QK4T2M. Hotel "
            "reservation confirmed for three nights.",
            "noreply@united.com", unsub="unsubscribe")
        assert booking.other_category is OtherCategory.TRAVEL
        assert booking.confidence >= 0.95      # and confident enough to file

    def test_a_security_code_is_not_phishing_boilerplate(self, rules):
        code = sort(rules, "Your verification code",
                    "Your one time code is 992814. Do not share this code.",
                    "security@service.example")
        assert code.other_category is OtherCategory.SECURITY

    def test_a_newsletter_is_not_a_promotion(self, rules):
        letter = sort(rules, "The Tuesday Dispatch, issue 88",
                      "In this week's issue: three charts worth your time. "
                      "View this email in your browser.",
                      "editor@dispatch.example", unsub="unsubscribe")
        assert letter.other_category is OtherCategory.NEWSLETTER
