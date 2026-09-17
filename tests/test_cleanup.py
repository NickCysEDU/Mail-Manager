"""Clearing out a mailbox: what gets asked of the server, and what never is.

These are the tests for a feature that deletes mail, so most of them are
about the negative space - the criteria that refuse to run, the senders that
are never offered, the command that is not sent.
"""

from __future__ import annotations

from datetime import datetime

import pytest

import cleanup
from cleanup import Criteria, imap_date, quote_search, suggest
from models import (Category, Classification, EmailMessage, FolderPlan,
                    OtherCategory, TriageItem)


def item(uid="1", sender="news@shop.example", job=False,
         other=OtherCategory.NEWSLETTER, bulk=True, when=None) -> TriageItem:
    return TriageItem(
        email=EmailMessage(uid=uid, sender_email=sender, subject=f"Offer {uid}",
                           sender_name="A Shop", date=when,
                           list_unsubscribe="<https://x.example/u>" if bulk else ""),
        classification=Classification(
            summary="", is_job_related=job,
            category=Category.INTERVIEW if job else Category.UNCLASSIFIED_OTHER,
            other_category=other, confidence_score=0.96, model="test"),
        folders=FolderPlan())


class TestTheDateIsNotTheLocale:
    """``strftime("%b")`` speaks French on a French Mac. IMAP does not."""

    def test_it_is_the_imap_spelling(self):
        assert imap_date(datetime(2026, 9, 17)) == "17-Sep-2026"

    def test_every_month_is_three_ascii_letters(self):
        for month in range(1, 13):
            said = imap_date(datetime(2026, month, 1))
            abbreviation = said.split("-")[1]
            assert len(abbreviation) == 3 and abbreviation.isascii()

    def test_single_digit_days_are_padded(self):
        assert imap_date(datetime(2026, 1, 3)) == "03-Jan-2026"


class TestQuotingCannotShiftTheCommand:
    """A stray quote does not fail: it re-reads the rest as search keys."""

    def test_a_quote_is_escaped(self):
        assert quote_search('say "hi"') == '"say \\"hi\\""'

    def test_a_backslash_is_escaped(self):
        assert quote_search("a\\b") == '"a\\\\b"'

    def test_an_ordinary_address_is_just_quoted(self):
        assert quote_search("news@shop.example") == '"news@shop.example"'


class TestWhatTheServerIsAsked:
    def test_one_sender_is_a_plain_from(self):
        assert Criteria(senders=("news@shop.example",),
                        only_seen=False).search_tokens() == [
            "FROM", '"news@shop.example"']

    def test_two_senders_need_one_or(self):
        tokens = Criteria(senders=("a@x.example", "b@x.example"),
                          only_seen=False).search_tokens()
        assert tokens == ["OR", "FROM", '"a@x.example"',
                          "FROM", '"b@x.example"']

    def test_three_senders_need_two_ors(self):
        """OR is binary. Three keys with one OR is a valid command that
        searches for the wrong thing, which is the failure worth pinning."""
        tokens = Criteria(senders=("a@x.example", "b@x.example", "c@x.example"),
                          only_seen=False).search_tokens()
        assert tokens.count("OR") == 2
        assert tokens == ["OR", "FROM", '"a@x.example"',
                          "OR", "FROM", '"b@x.example"',
                          "FROM", '"c@x.example"']

    def test_every_or_has_two_keys_after_it(self):
        for count in range(1, 9):
            senders = tuple(f"s{n}@x.example" for n in range(count))
            tokens = Criteria(senders=senders, only_seen=False).search_tokens()
            assert tokens.count("OR") == count - 1
            assert tokens.count("FROM") == count

    def test_bulk_asks_for_the_header_by_name(self):
        tokens = Criteria(only_bulk=True, only_seen=False).search_tokens()
        assert tokens == ["HEADER", "List-Unsubscribe", '""']

    def test_age_becomes_a_before_date(self):
        tokens = Criteria(older_than_days=30, only_seen=False).search_tokens(
            today=datetime(2026, 9, 17))
        assert tokens == ["BEFORE", "18-Aug-2026"]

    def test_unread_mail_is_spared_by_default(self):
        assert "SEEN" in Criteria(older_than_days=30).search_tokens()

    def test_filters_combine_with_and(self):
        tokens = Criteria(senders=("news@shop.example",), older_than_days=90,
                          only_bulk=True).search_tokens(
            today=datetime(2026, 9, 17))
        assert tokens == ["FROM", '"news@shop.example"',
                          "HEADER", "List-Unsubscribe", '""',
                          "BEFORE", "19-Jun-2026", "SEEN"]

    def test_blank_entries_are_dropped_not_searched_for(self):
        """An empty box would become FROM "" - which matches everything."""
        assert Criteria(senders=("", "  ", "a@x.example"),
                        only_seen=False).search_tokens() == [
            "FROM", '"a@x.example"']

    def test_duplicates_are_not_asked_for_twice(self):
        tokens = Criteria(senders=("a@x.example", "a@x.example"),
                          only_seen=False).search_tokens()
        assert "OR" not in tokens


class TestNothingChosenDeletesNothing:
    def test_empty_criteria_is_not_armed(self):
        assert Criteria().is_armed is False

    def test_already_read_alone_is_not_armed(self):
        """Otherwise clearing the boxes means "delete everything I have read"."""
        assert Criteria(only_seen=True).is_armed is False

    def test_any_real_filter_arms_it(self):
        assert Criteria(senders=("a@x.example",)).is_armed
        assert Criteria(subjects=("sale",)).is_armed
        assert Criteria(older_than_days=1).is_armed
        assert Criteria(only_bulk=True).is_armed

    def test_the_engine_refuses_an_unarmed_one(self):
        import imap_engine
        engine = imap_engine.IMAPEngine.__new__(imap_engine.IMAPEngine)
        called = []
        engine.search_criteria = lambda *a, **k: called.append(a)
        with pytest.raises(imap_engine.IMAPError):
            imap_engine.IMAPEngine.delete_matching(engine, Criteria())
        assert not called, "it searched before checking"


class TestWhatItOffersToDelete:
    def test_a_pile_of_newsletters_is_offered(self):
        found = suggest([item(uid=str(n)) for n in range(6)])
        assert [s.value for s in found if s.kind == "sender"] == [
            "news@shop.example"]
        assert found[0].count == 6

    def test_two_of_something_is_not_a_pile(self):
        assert suggest([item(uid="1"), item(uid="2")]) == []

    def test_job_mail_is_never_offered(self):
        found = suggest([item(uid=str(n), job=True,
                              other=OtherCategory.NOT_APPLICABLE)
                         for n in range(20)])
        assert found == []

    def test_one_job_message_disqualifies_the_whole_sender(self):
        """The cost of missing a pile is scrolling. The cost of a wrong
        suggestion is a deleted interview invitation."""
        rows = [item(uid=str(n)) for n in range(20)]
        rows.append(item(uid="job", job=True,
                         other=OtherCategory.NOT_APPLICABLE))
        assert suggest(rows) == []

    def test_money_and_security_mail_is_never_offered(self):
        for kind in (OtherCategory.FINANCE, OtherCategory.RECEIPT,
                     OtherCategory.SECURITY, OtherCategory.PERSONAL,
                     OtherCategory.WORK):
            rows = [item(uid=str(n), other=kind) for n in range(30)]
            assert suggest(rows) == [], kind

    def test_a_taught_sender_is_left_alone(self):
        rows = [item(uid=str(n)) for n in range(9)]
        assert suggest(rows, protected={"news@shop.example"}) == []

    def test_the_protected_list_ignores_case(self):
        rows = [item(uid=str(n), sender="News@Shop.Example") for n in range(9)]
        assert suggest(rows, protected={"news@shop.example"}) == []

    def test_the_biggest_pile_comes_first(self):
        rows = ([item(uid=f"a{n}", sender="a@x.example") for n in range(4)]
                + [item(uid=f"b{n}", sender="b@x.example") for n in range(9)])
        found = [s for s in suggest(rows) if s.kind == "sender"]
        assert [s.value for s in found] == ["b@x.example", "a@x.example"]

    def test_the_list_does_not_run_away(self):
        rows = [item(uid=f"{n}-{i}", sender=f"s{n}@x.example")
                for n in range(40) for i in range(4)]
        assert len(suggest(rows)) <= cleanup.MAX_SUGGESTIONS

    def test_a_category_is_only_offered_when_several_senders_share_it(self):
        one = [item(uid=str(n)) for n in range(9)]
        assert [s.kind for s in suggest(one)] == ["sender"]
        many = one + [item(uid=f"o{n}", sender="promo@other.example")
                      for n in range(5)]
        assert "kind" in {s.kind for s in suggest(many)}

    def test_a_suggestion_becomes_criteria_for_the_chosen_folder(self):
        found = suggest([item(uid=str(n)) for n in range(6)])[0]
        criteria = found.criteria(folder="Archive", older_than_days=30)
        assert criteria.folder == "Archive"
        assert criteria.senders == ("news@shop.example",)
        assert criteria.older_than_days == 30
        assert criteria.is_armed

    def test_rows_without_a_verdict_are_skipped_not_crashed_on(self):
        assert suggest([object(), None, item(uid="1")]) == []

    def test_the_category_is_read_by_value_not_by_repr(self):
        """``str()`` on a str-mixin enum differs between Pythons, and a
        membership test against the wrong one silently offers nothing."""
        assert cleanup._kind_of(item(other=OtherCategory.NEWSLETTER)) == "NEWSLETTER"
        assert cleanup._kind_of(item(other=OtherCategory.NEWSLETTER)) in cleanup.DISPOSABLE


class TestItSaysWhatItWillDo:
    def test_the_sentence_names_the_folder_and_the_filters(self):
        said = Criteria(folder="Archive", senders=("news@shop.example",),
                        older_than_days=90).describe()
        assert "Archive" in said
        assert "news@shop.example" in said
        assert "90 days" in said

    def test_one_day_is_not_plural(self):
        said = Criteria(older_than_days=1).describe()
        assert "older than 1 day" in said
        assert "1 days" not in said

    def test_two_filters_read_as_a_sentence_not_a_list(self):
        assert Criteria(senders=("a@x.example",)).describe() == (
            "messages in INBOX from \u201ca@x.example\u201d and already read")

    def test_an_unarmed_one_admits_it_means_everything(self):
        assert Criteria(folder="Archive").describe() == (
            "every message in Archive")


class TestItSurvivesARoundTrip:
    def test_settings_can_keep_a_criteria(self):
        original = Criteria(folder="Archive", senders=("a@x.example",),
                            subjects=("sale",), older_than_days=30,
                            only_seen=False, only_bulk=True)
        assert Criteria.from_dict(original.to_dict()) == original

    def test_a_missing_file_reads_as_the_default(self):
        assert Criteria.from_dict({}) == Criteria()
        assert Criteria.from_dict(None) == Criteria()


class TestTheSearchLeavesTheMachine:
    """imaplib encodes str arguments as ASCII and raises on anything else."""

    def test_an_accented_subject_is_sent_as_utf8_bytes(self, monkeypatch):
        import imap_engine
        sent = {}

        class FakeConn:
            def uid(self, *args):
                sent["args"] = args
                return "OK", [b""]

        engine = imap_engine.IMAPEngine.__new__(imap_engine.IMAPEngine)
        engine.conn = FakeConn()
        engine.select = lambda *a, **k: 0
        engine._cmd = lambda _d, func, *args: func(*args)[1]
        imap_engine.IMAPEngine.search_criteria(
            engine, Criteria(subjects=("café",), only_seen=False))
        args = sent["args"]
        assert "CHARSET" in args and "UTF-8" in args
        assert b'"caf\xc3\xa9"' in args

    def test_plain_ascii_does_not_ask_for_a_charset(self, monkeypatch):
        import imap_engine
        sent = {}

        class FakeConn:
            def uid(self, *args):
                sent["args"] = args
                return "OK", [b"1 2 3"]

        engine = imap_engine.IMAPEngine.__new__(imap_engine.IMAPEngine)
        engine.conn = FakeConn()
        engine.select = lambda *a, **k: 0
        engine._cmd = lambda _d, func, *args: func(*args)[1]
        found = imap_engine.IMAPEngine.search_criteria(
            engine, Criteria(subjects=("sale",), only_seen=False))
        assert "CHARSET" not in sent["args"]
        assert found == ["1", "2", "3"]


class TestItIsStillFewCommands:
    """The whole reason this exists: round trips, not messages."""

    def test_five_thousand_messages_is_about_a_hundred_commands(self):
        import imap_engine
        commands = []

        class FakeConn:
            def uid(self, *args):
                commands.append(args[0])
                return "OK", [b""]

        engine = imap_engine.IMAPEngine.__new__(imap_engine.IMAPEngine)
        engine.conn = FakeConn()
        engine.capabilities = ("UIDPLUS",)
        engine._cmd = lambda _d, func, *args: func(*args)[1]
        removed = imap_engine.IMAPEngine.delete_uids(
            engine, [str(n) for n in range(1, 5001)])
        assert removed == 5000
        assert len(commands) < 120, f"{len(commands)} round trips for 5000"

    def test_cancelling_stops_partway_and_says_so(self):
        import imap_engine
        import threading
        stop = threading.Event()

        class FakeConn:
            def uid(self, *args):
                stop.set()
                return "OK", [b""]

        engine = imap_engine.IMAPEngine.__new__(imap_engine.IMAPEngine)
        engine.conn = FakeConn()
        engine.capabilities = ("UIDPLUS",)
        engine._cmd = lambda _d, func, *args: func(*args)[1]
        removed = imap_engine.IMAPEngine.delete_uids(
            engine, [str(n) for n in range(1, 5001)], cancel=stop)
        assert removed == imap_engine.COMMAND_BATCH

    def test_nothing_to_delete_sends_nothing(self):
        import imap_engine
        engine = imap_engine.IMAPEngine.__new__(imap_engine.IMAPEngine)
        engine._require_conn = lambda: (_ for _ in ()).throw(
            AssertionError("it opened a connection for an empty list"))
        assert imap_engine.IMAPEngine.delete_uids(engine, []) == 0
