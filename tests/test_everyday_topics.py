"""Everyday sorting: the topics that have nothing to do with a job search.

The labelled evaluation set has few examples of these topics, so these cases
are written rather than collected: a specification of intended behaviour and a
guard against topics bleeding into one another, not a measure of accuracy. The
pairs at the bottom are the point: receipts against bank statements, a payment
made against one still to make, real travel against travel used as a metaphor.
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
        OtherCategory.TRANSACTIONS,
    ),
    (
        "Order BR-40192 is on its way",
        "Good news, your package left our warehouse. Estimated delivery is "
        "Thursday. Tracking number 1Z999AA10123456784.",
        "auto-notify@ups.com", "",
        OtherCategory.TRANSACTIONS,
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
        "This Sunday at St Brendan's",
        "Morning worship is at 10, with Holy Communion. The sermon series "
        "continues, and the coffee rota for October is on the noticeboard. "
        "Please keep the youth group in your prayers.",
        "office@stbrendans-parish.example", "",
        OtherCategory.CHURCH,
    ),
    (
        # A newsletter in form, and church mail to the person reading it.
        "Bulletin from the parish - 11 October",
        "Grace and peace to you all. In this week's bulletin: Sunday school "
        "begins again, the choir is looking for two more voices, and the "
        "Methodist church nearby has invited us to its harvest supper.",
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
        assert receipt.other_category is OtherCategory.TRANSACTIONS
        assert statement.other_category is OtherCategory.FINANCE

    def test_a_shipping_update_is_a_transaction(self, rules):
        shipped = sort(rules, "Your parcel is out for delivery",
                       "Your shipment is out for delivery and should arrive "
                       "today. Track your package for live updates.",
                       "track@fedex.com")
        assert shipped.other_category is OtherCategory.TRANSACTIONS

    def test_travel_words_in_marketing_are_not_filed_as_travel(self, rules):
        """A course sold as a holiday.

        Travel vocabulary with nothing behind it should not be confident
        enough to file. It is allowed to guess travel, and it does; what it
        must not do is act on that guess, so this asserts the confidence
        rather than the label.
        """
        metaphor = sort(
            rules, "Your trip with Sunpeak Academy starts now",
            "Pack your bags: our new course departs today, and there is a seat for you "
            "on the leaderboard.",
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


# -- a payment that has happened against one still to come. The card, the
# account and the bank say whose money it is; which way it went decides.
class TestMoneyThatHasMovedIsATransaction:
    def test_a_payment_made_is_not_a_bill_to_pay(self, rules):
        paid = sort(rules, "We got your payment",
                    "Thanks - we received your payment of $120.00 for the card "
                    "ending in 5531 this morning.", "alerts@lindenbank.example")
        due = sort(rules, "Your payment is coming up",
                   "A payment of $120.00 on the card ending in 5531 is due on "
                   "the 14th.", "alerts@lindenbank.example")
        assert paid.other_category is OtherCategory.TRANSACTIONS
        assert due.other_category is OtherCategory.FINANCE

    def test_a_deposit_that_has_landed(self, rules):
        found = sort(rules, "Money in",
                     "A deposit of $640.25 was posted to your savings account "
                     "ending in 4826.", "notices@lindenbank.example")
        assert found.other_category is OtherCategory.TRANSACTIONS

    def test_a_transfer_that_has_gone_through(self, rules):
        found = sort(rules, "Transfer update",
                     "Your transfer of $75.00 to Tamsin has been completed.",
                     "notices@lindenbank.example")
        assert found.other_category is OtherCategory.TRANSACTIONS

    def test_a_card_purchase_reported_by_the_bank(self, rules):
        found = sort(rules, "Card alert",
                     "A purchase of $31.60 was made with your card ending in 5531 "
                     "at Harbour Books.", "alerts@lindenbank.example")
        assert found.other_category is OtherCategory.TRANSACTIONS

    def test_money_you_sent_a_friend(self, rules):
        found = sort(rules, "Done",
                     "You sent $18 to Priya for the concert tickets.",
                     "cash@wallet.example")
        assert found.other_category is OtherCategory.TRANSACTIONS

    def test_a_plan_renewed_for_an_amount(self, rules):
        found = sort(rules, "Thanks for staying with us",
                     "Your Studio plan was renewed for $14.99 this month.",
                     "billing@sketchpad.example")
        assert found.other_category is OtherCategory.TRANSACTIONS

    def test_a_gift_thanked_with_its_amount(self, rules):
        found = sort(rules, "From all of us at the shelter",
                     "Thank you for your gift of $40 to Hillside Animal Rescue.",
                     "giving@hillsiderescue.example")
        assert found.other_category is OtherCategory.TRANSACTIONS

    def test_a_payment_acknowledged_without_its_amount(self, rules):
        found = sort(rules, "All set",
                     "We've received your payment. There is nothing more to do.",
                     "billing@waterworks.example")
        assert found.other_category is OtherCategory.TRANSACTIONS

    def test_thanks_for_a_payment(self, rules):
        found = sort(rules, "From the front desk",
                     "Thank you for your payment. See you at the next session!",
                     "desk@pinegrovedental.example")
        assert found.other_category is OtherCategory.TRANSACTIONS

    def test_a_deposit_accepted(self, rules):
        found = sort(rules, "Mobile deposit",
                     "Your check deposit of $310.00 has been accepted.",
                     "notices@lindenbank.example")
        assert found.other_category is OtherCategory.TRANSACTIONS

    def test_an_automatic_payment_that_went_through(self, rules):
        found = sort(rules, "Your automatic payment went through",
                     "Your automatic payment of $61.75 was processed today.",
                     "billing@gridpower.example")
        assert found.other_category is OtherCategory.TRANSACTIONS

    def test_a_subscription_called_off(self, rules):
        found = sort(rules, "Plan closed",
                     "Your subscription has been canceled and you won't be billed "
                     "again.", "billing@sketchpad.example")
        assert found.other_category is OtherCategory.TRANSACTIONS

    def test_a_membership_called_off(self, rules):
        found = sort(rules, "Membership update",
                     "As you asked, your membership has been cancelled.",
                     "members@climbhall.example")
        assert found.other_category is OtherCategory.TRANSACTIONS

    def test_a_ride_receipt_with_only_its_total(self, rules):
        found = sort(rules, "Your evening ride",
                     "Trip total $16.40. Have a good night!",
                     "receipts@hopcab.example")
        assert found.other_category is OtherCategory.TRANSACTIONS

    def test_a_receipts_mailbox_is_not_a_person(self, rules):
        found = sort(rules, "Your evening ride",
                     "Trip total $16.40. Thanks for riding with us, see you soon!",
                     "receipts@hopcab.example")
        assert found.other_category is OtherCategory.TRANSACTIONS

    def test_a_receipt_named_in_the_subject(self, rules):
        found = sort(rules, "Receipt for your class pass",
                     "Ten yoga classes, valid until June.",
                     "studio@stillwateryoga.example")
        assert found.other_category is OtherCategory.TRANSACTIONS

    def test_a_payment_confirmation_named_as_one(self, rules):
        found = sort(rules, "Payment confirmation",
                     "Reference 50-2231. Keep this email for your records.",
                     "billing@waterworks.example")
        assert found.other_category is OtherCategory.TRANSACTIONS

    def test_a_shops_click_and_collect(self, rules):
        found = sort(rules, "Come and get it",
                     "Your click & collect items are waiting at the Bridge Street "
                     "shop.", "noreply@shopfront.example")
        assert found.other_category is OtherCategory.TRANSACTIONS

    def test_a_shops_collection_counter(self, rules):
        found = sort(rules, "Good news",
                     "Everything is ready for collection at our Quay Road store.",
                     "noreply@shopfront.example")
        assert found.other_category is OtherCategory.TRANSACTIONS

    def test_tracking_a_delivery(self, rules):
        found = sort(rules, "Where it is now",
                     "Track your delivery from the link below.",
                     "noreply@shopfront.example")
        assert found.other_category is OtherCategory.TRANSACTIONS

    def test_a_deposit_named_in_the_subject(self, rules):
        found = sort(rules, "Deposit posted",
                     "Amount: $512.00. Account ending in 4826.",
                     "notices@lindenbank.example")
        assert found.other_category is OtherCategory.TRANSACTIONS

    def test_a_couriers_own_domain(self, rules):
        found = sort(rules, "An update from us",
                     "Here is the latest on item 33QX7701.",
                     "no-reply@auspost.com.au")
        assert found.other_category is OtherCategory.TRANSACTIONS


class TestMoneyStillToComeIsFinance:
    def test_a_payment_that_has_not_arrived(self, rules):
        found = sort(rules, "Your account is past due",
                     "We haven't received your payment of $64.10, which was due "
                     "on the 2nd.", "billing@waterworks.example")
        assert found.other_category is OtherCategory.FINANCE

    def test_a_payment_not_yet_in_is_not_acknowledged(self, rules):
        found = sort(rules, "Checking in",
                     "We haven't received your payment yet.",
                     "office@stillwateryoga.example")
        assert found.other_category is not OtherCategory.TRANSACTIONS

    def test_a_payment_that_was_never_made(self, rules):
        found = sort(rules, "About your plan",
                     "No payment was made on your $14.99 plan in October.",
                     "hello@sketchpad.example")
        assert found.other_category is not OtherCategory.TRANSACTIONS

    def test_a_payment_that_was_declined(self, rules):
        found = sort(rules, "Action needed",
                     "Your payment of $14.99 has been declined. Update your card "
                     "to keep your plan.", "billing@sketchpad.example")
        assert found.other_category is not OtherCategory.TRANSACTIONS

    def test_a_payment_that_is_only_scheduled(self, rules):
        found = sort(rules, "Coming up",
                     "Your payment of $88.00 has been scheduled for March 3 from "
                     "the account ending in 2290.", "billing@gridpower.example")
        assert found.other_category is OtherCategory.FINANCE

    def test_a_card_mentioned_in_account_news(self, rules):
        found = sort(rules, "About your card",
                     "We've raised the credit limit on your card ending in 5531 "
                     "to $4,000.", "alerts@lindenbank.example")
        assert found.other_category is OtherCategory.FINANCE

    def test_a_payment_that_has_not_arrived_said_in_full(self, rules):
        found = sort(rules, "Checking in",
                     "We have not received your payment for March.",
                     "office@stillwateryoga.example")
        assert found.other_category is not OtherCategory.TRANSACTIONS

    def test_a_payment_held_until_later(self, rules):
        found = sort(rules, "On hold",
                     "Your payment of $40.00 will be held until your card has been "
                     "approved.", "billing@sketchpad.example")
        assert found.other_category is not OtherCategory.TRANSACTIONS

    def test_a_new_card_in_the_post_is_the_accounts_news(self, rules):
        """A parcel's words beside the card's: the card follows a payment,
        and there is none."""
        found = sort(rules, "Your new card",
                     "Your replacement card ending in 5531 is on its way.",
                     "alerts@lindenbank.example")
        assert found.other_category is OtherCategory.FINANCE

    def test_a_payment_noted_on_a_bill_still_owing(self, rules):
        """As much said for a payment made as for one owed: the account's own
        words stay with Finance."""
        found = sort(rules, "Account update",
                     "Your payment of $35.00 was received. Your balance of $120.00 "
                     "is due on the 3rd.", "notices@lindenbank.example")
        assert found.other_category is OtherCategory.FINANCE

    def test_a_payment_request_is_said_with_some_confidence(self, rules):
        found = sort(rules, "Lunch",
                     "Idris requested $15.00 for lunch.", "notify@paylane.example")
        assert found.other_category is OtherCategory.FINANCE
        assert found.confidence >= 0.80

    def test_a_fee_the_bank_took(self, rules):
        found = sort(rules, "Overdraft fee",
                     "An overdraft fee of $35.00 was charged to your account "
                     "ending in 2290.", "alerts@lindenbank.example")
        assert found.other_category is OtherCategory.FINANCE


# -- words that look like a transaction and are not one.
class TestNotEveryMoneyWordIsATransaction:
    def test_a_paid_subscription_is_not_a_payment(self, rules):
        found = sort(rules, "Read the full story",
                     "This article needs a paid subscription; plans start at $4.99 "
                     "a month.", "stories@dailyledger.example")
        assert found.other_category is not OtherCategory.TRANSACTIONS

    def test_tracking_a_habit_is_not_tracking_a_parcel(self, rules):
        found = sort(rules, "Track your progress",
                     "Track your reading streak in the app: 12 days so far!",
                     "hello@pagecount.example")
        assert found.other_category is not OtherCategory.TRANSACTIONS

    def test_a_hard_sell_wearing_order_words(self, rules):
        found = sort(rules, "LAST CHANCE: 70% OFF EVERYTHING",
                     "ORDER NOW!!! Your order ships free today only. Act now, "
                     "limited stock, buy two get one free, save 70% at checkout!",
                     "promo@megadeals.example")
        assert found.other_category is not OtherCategory.TRANSACTIONS

    def test_purchases_made_possible_by_gifts(self, rules):
        found = sort(rules, "What you made happen",
                     "Last year 1,150 new books were bought for the branch: the "
                     "purchases were made possible by $18,000 in member gifts.",
                     "friends@clearwaterlib.example")
        assert found.other_category is not OtherCategory.TRANSACTIONS


# -- a business's own record of money that moved, whatever trade it is in.
class TestATradesOwnReceipt:
    def test_a_sportsbooks_deposit_with_its_casino_in_the_footer(self, rules):
        found = sort(rules, "Your deposit confirmation",
                     "Hi Sam, your deposit is confirmed. Amount: $25.00. Good luck "
                     "out there! Sportsbook | Casino | Lottery | Help",
                     "account@northpeakbets.example")
        assert found.other_category is OtherCategory.TRANSACTIONS

    def test_a_banks_wire_sent(self, rules):
        found = sort(rules, "Wire transfer completed",
                     "Your outgoing wire of $1,500.00 was sent on May 2 from the "
                     "account ending in 4826.", "alerts@lindenbank.example")
        assert found.other_category is OtherCategory.TRANSACTIONS

    def test_a_wire_with_no_other_word_for_it(self, rules):
        found = sort(rules, "Outgoing wire",
                     "Your wire of $1,500.00 was sent on May 2.",
                     "alerts@lindenbank.example")
        assert found.other_category is OtherCategory.TRANSACTIONS

    def test_a_deposit_confirmed_by_the_book(self, rules):
        found = sort(rules, "Account update",
                     "A deposit of $25.00 has been confirmed on your account. "
                     "Sportsbook | Casino | Lottery | Help",
                     "account@northpeakbets.example")
        assert found.other_category is OtherCategory.TRANSACTIONS

    def test_a_deposit_confirmation_named_as_one(self, rules):
        found = sort(rules, "Deposit confirmation",
                     "Amount: $25.00 to checking.", "notices@lindenbank.example")
        assert found.other_category is OtherCategory.TRANSACTIONS

    def test_a_withdrawal_confirmation_named_as_one(self, rules):
        found = sort(rules, "Withdrawal confirmation",
                     "Amount: $60.00 from checking.", "notices@lindenbank.example")
        assert found.other_category is OtherCategory.TRANSACTIONS

    def test_a_transfer_named_in_its_headline(self, rules):
        found = sort(rules, "Transfer received",
                     "Amount: $300.00 into savings.", "notices@lindenbank.example")
        assert found.other_category is OtherCategory.TRANSACTIONS

    def test_a_payment_confirmed_in_so_many_words(self, rules):
        found = sort(rules, "Thanks",
                     "Your payment is confirmed and your plan carries on.",
                     "hello@sketchpad.example")
        assert found.other_category is OtherCategory.TRANSACTIONS

    def test_the_same_words_from_a_free_mail_account_are_not_vouched_for(
            self, rules):
        found = sort(rules, "Your deposit confirmation",
                     "Your deposit is confirmed. Amount: $25.00. Play the casino "
                     "and the lottery now, bonus inside.",
                     "northpeak.bets.payouts@gmail.example")
        assert found.other_category is not OtherCategory.TRANSACTIONS

    def test_a_lottery_win_that_calls_itself_confirmed(self, rules):
        found = sort(rules, "Lottery winnings confirmed",
                     "Dear customer, your deposit of $950,000.00 is confirmed. "
                     "Kindly pay $9.99 here to release the funds: "
                     "http://win.example/x", "claims@lotto-office.example")
        assert found.other_category is OtherCategory.SPAM

    def test_a_count_of_orders_is_not_money_received(self, rules):
        found = sort(rules, "Make $38,500 in 90 days",
                     "Send $5 to each name on the list. Once you have received 100 "
                     "or more orders, the reports sell themselves.",
                     "profits@cashflow-reports.example")
        assert found.other_category is not OtherCategory.TRANSACTIONS


# -- the scams that wear a receipt or a courier's notice.
class TestAScamDressedAsATransaction:
    def test_a_parcel_held_for_a_small_fee(self, rules):
        found = sort(rules, "Delivery failed",
                     "Dear customer, your parcel could not be delivered. Kindly "
                     "confirm your details and pay $1.49 here: http://bit.example/x",
                     "info@parcel-notice.example")
        assert found.other_category is OtherCategory.SPAM

    def test_a_balance_asked_for_with_dear_customer(self, rules):
        found = sort(rules, "Account update",
                     "Dear Customer, your balance is £54.20. Please pay £54.20 today "
                     "using the link below.", "accounts@gridpower.example")
        assert found.other_category is not OtherCategory.SPAM

    def test_a_real_bill_that_says_dear_customer(self, rules):
        found = sort(rules, "Your bill is ready",
                     "Dear customer, your bill for March is ready. Pay $54.20 now "
                     "online or by phone.", "billing@gridpower.example")
        assert found.other_category is not OtherCategory.SPAM

    def test_a_charge_to_ring_about_from_a_free_mail_account(self, rules):
        found = sort(rules, "Your renewal",
                     "Your account has been charged $399.99 for the annual "
                     "protection plan. If you did not authorize this, call "
                     "1-888-555-0142 to cancel.", "renewals.team8812@gmail.example")
        assert found.other_category is OtherCategory.SPAM

    def test_the_same_alert_from_the_bank_itself(self, rules):
        found = sort(rules, "Card alert",
                     "Your card was charged $399.99 at Gadget Barn. If you did not "
                     "authorize this, call 1-888-555-0142.", "alerts@lindenbank.example")
        assert found.other_category is not OtherCategory.SPAM

    def test_a_charge_from_a_free_mail_account_with_no_number(self, rules):
        found = sort(rules, "Lesson booked",
                     "Your lesson is booked and $40.00 was charged. If you didn't "
                     "place this booking, call me and I'll sort it out.",
                     "tutor.rhea@gmail.example")
        assert found.other_category is not OtherCategory.SPAM

    def test_a_free_mail_account_that_asks_nothing_be_disputed(self, rules):
        found = sort(rules, "Lesson booked",
                     "Your lesson is booked and $40.00 was charged. Questions? Call "
                     "me on 555-201-7788.", "tutor.rhea@gmail.example")
        assert found.other_category is not OtherCategory.SPAM

    def test_a_call_about_nothing_charged(self, rules):
        found = sort(rules, "Your booking",
                     "If you didn't place this booking, call 555-201-7788 and "
                     "I'll cancel it.", "tutor.rhea@gmail.example")
        assert found.other_category is not OtherCategory.SPAM
