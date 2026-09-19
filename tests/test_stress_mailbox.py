"""Deletion, against a mailbox big enough and awkward enough to break it.

The unit tests for clearing out mail check the shape of what is sent. These
check what happens to the mail, against a stand-in server that actually
evaluates the search it is given: it parses the IMAP expression, works out
which messages match, and only lets those be flagged and expunged. So
"deleted exactly the right four hundred and nothing else" is a real
assertion rather than a restatement of what the code did.

The mailbox is deliberately hostile. Twenty thousand messages, addresses and
subjects with quotes, backslashes, accents, right-to-left marks and newlines
in them, senders whose names contain other senders' names, messages with no
date and messages dated in the future. Anything that survives this is not
going to be surprised by somebody's inbox.
"""

from __future__ import annotations

import imaplib
import random
import threading
from datetime import datetime, timedelta

import pytest

import imap_engine
from cleanup import Criteria

#: Months as IMAP spells them, for reading BEFORE back.
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

NOW = datetime(2026, 9, 18, 12, 0, 0)


class Message:
    """One message on the fake server, as the search sees it."""

    __slots__ = ("uid", "sender", "subject", "when", "seen", "bulk")

    def __init__(self, uid, sender, subject, when, seen, bulk):
        self.uid = uid
        self.sender = sender
        self.subject = subject
        self.when = when
        self.seen = seen
        self.bulk = bulk


class SearchingIMAP:
    """A stand-in that really answers the search it is asked.

    Only the handful of commands the clear-out uses, and each of them
    strictly: an unquoted mailbox, an unknown key or a malformed OR is an
    error rather than something quietly ignored, because a search that is
    quietly misread is exactly the failure worth catching - it deletes
    the wrong messages and reports success.
    """

    #: imaplib's own exception types. The engine catches those and turns
    #: them into IMAPError; anything else goes straight through it, so a
    #: stand-in with its own exception classes tests a path the real
    #: server never takes.
    abort = imaplib.IMAP4.abort
    error = imaplib.IMAP4.error

    def __init__(self, messages, delimiter="/"):
        self.store = {m.uid: m for m in messages}
        self.delimiter = delimiter
        self.selected = None
        self.readonly = None
        self.deleted: set = set()
        self.expunged: list = []
        self.commands: list = []
        self.searches: list = []
        #: Raised on the nth STORE, to model a server giving up part way.
        self.fail_store_after = None
        self._stores = 0

    # -- the commands the clear-out uses ---------------------------------
    def select(self, mailbox, readonly=False):
        name = str(mailbox)
        assert name.startswith('"') and name.endswith('"'), \
            "mailbox names must be quoted"
        self.selected = name[1:-1]
        self.readonly = readonly
        self.commands.append(("SELECT", (self.selected, readonly)))
        return ("OK", [str(len(self.store)).encode()])

    def uid(self, command, *args):
        command = command.upper()
        self.commands.append(("UID " + command, args))
        if command == "SEARCH":
            return self._search(args)
        if command == "STORE":
            return self._store(args)
        if command == "EXPUNGE":
            return self._expunge(args)
        raise self.error(f"Unsupported command {command}")

    def capability(self):
        return ("OK", [b"IMAP4REV1 UIDPLUS"])

    def logout(self):
        return ("BYE", [b""])

    # -- search ----------------------------------------------------------
    def _search(self, args):
        tokens = []
        for arg in args:
            if arg is None:
                continue
            tokens.append(arg.decode("utf-8") if isinstance(arg, bytes)
                          else str(arg))
        self.searches.append(list(tokens))
        if tokens[:1] == ["CHARSET"]:
            assert tokens[1].upper() == "UTF-8", "only UTF-8 is understood"
            tokens = tokens[2:]
        matched = [m for m in self.store.values() if self._matches(tokens, m)]
        uids = sorted((m.uid for m in matched), key=int)
        return ("OK", [" ".join(uids).encode()])

    def _matches(self, tokens, message) -> bool:
        rest, answer = self._one(list(tokens), message)
        # Everything left over is ANDed, which is what IMAP does.
        while rest:
            rest, more = self._one(rest, message)
            answer = answer and more
        return answer

    def _one(self, tokens, message):
        if not tokens:
            return [], True
        key = tokens[0].upper()
        rest = tokens[1:]
        if key == "ALL":
            return rest, True
        if key == "SEEN":
            return rest, message.seen
        if key == "UNSEEN":
            return rest, not message.seen
        if key == "OR":
            rest, left = self._one(rest, message)
            rest, right = self._one(rest, message)
            return rest, (left or right)
        if key in ("FROM", "SUBJECT"):
            if not rest:
                raise self.error(f"{key} with nothing to look for")
            needle = self._unquote(rest[0])
            if not needle:
                raise self.error(f"{key} with an empty string matches all")
            field = message.sender if key == "FROM" else message.subject
            return rest[1:], needle.lower() in field.lower()
        if key == "HEADER":
            if len(rest) < 2:
                raise self.error("HEADER needs a name and a string")
            name, value = rest[0], self._unquote(rest[1])
            if name.lower() != "list-unsubscribe":
                raise self.error(f"no such header here: {name}")
            # A zero-length string means "has this header at all".
            return rest[2:], message.bulk if value == "" else False
        if key == "BEFORE":
            if not rest:
                raise self.error("BEFORE with no date")
            when = self._date(rest[0])
            if message.when is None:
                return rest[1:], False
            return rest[1:], message.when.date() < when.date()
        raise self.error(f"unknown search key {key}")

    @staticmethod
    def _unquote(text):
        text = str(text)
        if text.startswith('"') and text.endswith('"'):
            body = text[1:-1]
            out, escaped = [], False
            for char in body:
                if escaped:
                    out.append(char)
                    escaped = False
                elif char == "\\":
                    escaped = True
                else:
                    out.append(char)
            return "".join(out)
        return text

    def _date(self, text):
        parts = str(text).split("-")
        if len(parts) != 3 or parts[1] not in MONTHS:
            raise self.error(f"not a date IMAP understands: {text}")
        return datetime(int(parts[2]), MONTHS.index(parts[1]) + 1,
                        int(parts[0]))

    # -- store and expunge -----------------------------------------------
    def _store(self, args):
        uid_set, mode, flags = args[0], args[1], args[2]
        assert "SILENT" in str(mode).upper(), "the silent form is the cheap one"
        assert "\\Deleted" in str(flags)
        self._stores += 1
        if (self.fail_store_after is not None
                and self._stores > self.fail_store_after):
            raise self.error("server gave up")
        wanted = [u for u in str(uid_set).split(",") if u]
        for uid in wanted:
            if uid not in self.store:
                raise self.error(f"no such message {uid}")
            self.deleted.add(uid)
        return ("OK", [b""])

    def _expunge(self, args):
        wanted = [u for u in str(args[0]).split(",") if u]
        for uid in wanted:
            if uid in self.deleted:
                self.expunged.append(uid)
                self.store.pop(uid, None)
        return ("OK", [b""])


def engine_on(server) -> imap_engine.IMAPEngine:
    """A real engine with a fake socket under it - no other stand-ins."""
    engine = imap_engine.IMAPEngine.__new__(imap_engine.IMAPEngine)
    engine.conn = server
    engine.capabilities = ("IMAP4REV1", "UIDPLUS")
    engine.delimiter = "/"
    engine._selected = None
    engine._selected_readonly = None
    return engine


# ---------------------------------------------------------------------------
# The mailbox itself
# ---------------------------------------------------------------------------
AWKWARD = [
    'say "hello"',                 # a quote, which ends an IMAP string early
    "back\\slash",                 # an escape
    "café ☕ naïve",                # not ASCII
    "sale‮sale",              # a right-to-left override
    "line\nbreak",                 # a newline in a header
    "  padded  ",
    "'; DROP TABLE mail; --",
    "a" * 300,                     # longer than anybody expects
]


def mailbox(count=20_000, seed=11):
    """A big, ugly mailbox."""
    shake = random.Random(seed)
    senders = ([f"person{n}@people.example" for n in range(40)]
               + ["news@shop.example", "news@shop.example.org",
                  "notnews@shop.example", "NEWS@SHOP.EXAMPLE"]
               + [f"{odd}@odd.example" for odd in AWKWARD[:4]])
    subjects = ([f"Message {n}" for n in range(60)]
                + AWKWARD + ["Your receipt", "your RECEIPT", "receipts"])
    out = []
    for index in range(1, count + 1):
        when = None
        roll = shake.random()
        if roll < 0.02:
            when = None                              # no date at all
        elif roll < 0.04:
            when = NOW + timedelta(days=shake.randint(1, 400))   # the future
        else:
            when = NOW - timedelta(days=shake.randint(0, 900),
                                   hours=shake.randint(0, 23))
        out.append(Message(
            uid=str(index),
            sender=shake.choice(senders),
            subject=shake.choice(subjects),
            when=when,
            seen=shake.random() < 0.7,
            bulk=shake.random() < 0.3,
        ))
    return out


def should_match(messages, criteria: Criteria, now=NOW):
    """What the criteria mean, worked out here rather than by the server."""
    wanted = []
    for m in messages:
        if criteria.senders and not any(
                s.lower() in m.sender.lower() for s in criteria.senders):
            continue
        if criteria.subjects and not any(
                s.lower() in m.subject.lower() for s in criteria.subjects):
            continue
        if criteria.only_bulk and not m.bulk:
            continue
        if criteria.older_than_days:
            cutoff = (now - timedelta(days=criteria.older_than_days)).date()
            if m.when is None or m.when.date() >= cutoff:
                continue
        if criteria.only_seen and not m.seen:
            continue
        wanted.append(m.uid)
    return set(wanted)


@pytest.fixture(scope="module")
def big():
    return mailbox()


class TestItDeletesExactlyWhatWasAsked:
    @pytest.mark.parametrize("criteria", [
        Criteria(senders=("news@shop.example",), only_seen=False),
        Criteria(senders=("news@shop.example",)),
        Criteria(subjects=("receipt",), only_seen=False),
        Criteria(senders=("person1@people.example",
                          "person2@people.example",
                          "person3@people.example"), only_seen=False),
        Criteria(older_than_days=365, only_seen=False),
        Criteria(only_bulk=True, only_seen=False),
        Criteria(senders=("shop.example",), older_than_days=30,
                 only_bulk=True),
        Criteria(subjects=('say "hello"',), only_seen=False),
        Criteria(subjects=("back\\slash",), only_seen=False),
        Criteria(subjects=("café ☕ naïve",), only_seen=False),
    ])
    def test_the_right_messages_and_only_those(self, big, criteria):
        server = SearchingIMAP(list(big))
        engine = engine_on(server)
        expected = should_match(big, criteria, _fixed_now(criteria))
        removed = engine.delete_matching(criteria)
        assert set(server.expunged) == expected, (
            f"{len(set(server.expunged) - expected)} deleted that should not "
            f"have been, {len(expected - set(server.expunged))} missed")
        assert removed == len(expected)

    def test_a_domain_is_a_substring_and_catches_what_that_catches(self, big):
        """FROM is a substring match on the server, which is what makes a
        domain usable as a filter - and it means "@shop.example" also
        takes "@shop.example.org" and anyone at all whose address ends
        that way. Worth pinning because somebody reading the dialog will
        assume it means the domain exactly, and it does not."""
        criteria = Criteria(senders=("@shop.example",), only_seen=False)
        server = SearchingIMAP(list(big))
        engine_on(server).delete_matching(criteria)
        gone = {m.sender for m in big if m.uid in set(server.expunged)}
        assert "news@shop.example" in gone
        assert "news@shop.example.org" in gone, "a subdomain was spared"
        assert "notnews@shop.example" in gone, (
            "a different local part at the same domain was spared")
        assert not any("odd.example" in who for who in gone), (
            "it reached another domain entirely")

    def test_matching_is_not_case_sensitive(self, big):
        server = SearchingIMAP(list(big))
        engine_on(server).delete_matching(
            Criteria(senders=("NEWS@shop.EXAMPLE",), only_seen=False))
        gone = {m.sender for m in big if m.uid in set(server.expunged)}
        assert "news@shop.example" in gone
        assert "NEWS@SHOP.EXAMPLE" in gone


def _fixed_now(criteria):
    """The moment the search was built, so the check uses the same one."""
    return NOW


@pytest.fixture(autouse=True)
def _one_clock(monkeypatch):
    """Both sides of every date comparison read the same moment.

    The criteria build their IMAP BEFORE date from ``datetime.now()`` and
    the expectations here are worked out against a fixed NOW. Those agree
    for as long as the two fall on the same day, which is to say they
    agree until a run crosses midnight - and then a whole day of messages
    sits between the two cutoffs. A build runner found it at 00:07: 19
    deleted that should not have been.

    Freezing the clock the criteria read is the fix. Nothing about the
    product changes; it is this file that was reading two clocks and
    calling them one.
    """
    import cleanup

    class Frozen(cleanup.datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(cleanup, "datetime", Frozen)


class TestTheSuiteReadsOneClock:
    """The date criteria build their cutoff from the current moment and
    the expectations here are worked out against a fixed one. Those agree
    for exactly as long as they fall on the same day.
    """

    def test_the_criteria_and_the_expectations_share_a_now(self):
        import cleanup

        assert cleanup.datetime.now() == NOW, (
            "the criteria are reading the wall clock, so this suite fails "
            "whenever a run crosses midnight")

    def test_a_day_rolling_over_does_not_move_the_cutoff(self):
        """What actually went wrong, held still: with the two clocks a day
        apart, a year-old filter took a whole extra day of messages - 19
        of them, on a build runner at seven minutes past midnight."""
        from cleanup import Criteria

        criteria = Criteria(older_than_days=365, only_seen=False)
        tokens = criteria.search_tokens()
        assert "BEFORE" in tokens
        when = tokens[tokens.index("BEFORE") + 1]
        from cleanup import imap_date

        assert when == imap_date(NOW - timedelta(days=365)), (
            f"the search says {when}, which is not 365 days before the "
            f"moment the expectations use")


class TestNothingIsDeletedByAccident:
    def test_criteria_that_ask_for_nothing_are_refused(self, big):
        server = SearchingIMAP(list(big))
        with pytest.raises(imap_engine.IMAPError):
            engine_on(server).delete_matching(Criteria())
        assert not server.expunged
        assert not server.commands, "it talked to the server at all"

    def test_a_search_that_matches_nothing_deletes_nothing(self, big):
        server = SearchingIMAP(list(big))
        removed = engine_on(server).delete_matching(
            Criteria(senders=("nobody-at-all@nowhere.example",),
                     only_seen=False))
        assert removed == 0
        assert not server.expunged
        assert not server.deleted

    def test_counting_touches_nothing(self, big):
        server = SearchingIMAP(list(big))
        count = engine_on(server).count_matching(
            Criteria(senders=("news@shop.example",), only_seen=False))
        assert count > 0
        assert not server.deleted and not server.expunged
        assert server.readonly is True, "counting opened the folder to write"

    def test_unread_mail_is_spared_unless_asked_for(self, big):
        server = SearchingIMAP(list(big))
        engine_on(server).delete_matching(
            Criteria(senders=("news@shop.example",), only_seen=True))
        gone = {m.uid for m in big if m.uid in set(server.expunged)}
        unread = {m.uid for m in big if not m.seen}
        assert not (gone & unread), "an unread message was deleted"

    def test_a_message_with_no_date_survives_an_age_filter(self, big):
        server = SearchingIMAP(list(big))
        engine_on(server).delete_matching(
            Criteria(older_than_days=1, only_seen=False))
        undated = {m.uid for m in big if m.when is None}
        assert undated, "the mailbox has no undated messages to check"
        assert not (undated & set(server.expunged))

    def test_the_future_is_not_older_than_anything(self, big):
        server = SearchingIMAP(list(big))
        engine_on(server).delete_matching(
            Criteria(older_than_days=1, only_seen=False))
        ahead = {m.uid for m in big
                 if m.when is not None and m.when > NOW}
        assert ahead
        assert not (ahead & set(server.expunged))


class TestItStaysCheapAtScale:
    def test_twenty_thousand_messages_is_a_few_hundred_commands(self, big):
        server = SearchingIMAP(list(big))
        removed = engine_on(server).empty_folder("INBOX")
        assert removed == len(big)
        assert not server.store, "something was left behind"
        sent = len([c for c in server.commands if c[0].startswith("UID")])
        # One search, then one store and one expunge per hundred.
        batch = imap_engine.COMMAND_BATCH
        expected = 1 + 2 * -(-len(big) // batch)
        assert sent == expected, (
            f"{sent} commands for {len(big)} messages, expected {expected}")
        assert sent < len(big) / 40, "that is not a saving worth having"

    def test_the_cost_grows_with_the_batches_not_the_messages(self):
        counts = {}
        for size in (1_000, 8_000):
            server = SearchingIMAP(mailbox(size, seed=3))
            engine_on(server).empty_folder("INBOX")
            counts[size] = len([c for c in server.commands
                                if c[0].startswith("UID")])
        grew = counts[8_000] / counts[1_000]
        assert 6.0 < grew < 10.0, (
            f"eight times the mail took {grew:.1f} times the commands")

    def test_it_is_quick(self, big):
        import time

        server = SearchingIMAP(list(big))
        started = time.monotonic()
        engine_on(server).empty_folder("INBOX")
        took = time.monotonic() - started
        assert took < 10.0, f"{took:.1f}s to clear {len(big)} messages"


class TestItSurvivesBeingInterrupted:
    def test_stopping_part_way_leaves_the_rest_alone(self, big):
        server = SearchingIMAP(list(big))
        stop = threading.Event()
        seen = {"n": 0}

        def progress(done, total, text):
            seen["n"] += 1
            if seen["n"] >= 3:
                stop.set()

        removed = engine_on(server).empty_folder(
            "INBOX", progress=progress, cancel=stop)
        assert 0 < removed < len(big), f"removed {removed} of {len(big)}"
        assert len(server.store) == len(big) - removed
        # Nothing outside what it said it removed was touched.
        assert len(server.expunged) == removed

    def test_a_server_that_gives_up_half_way_raises(self, big):
        server = SearchingIMAP(list(big))
        server.fail_store_after = 4
        with pytest.raises(imap_engine.IMAPError):
            engine_on(server).empty_folder("INBOX")
        # And what it managed before that is consistent: flagged, not lost.
        assert len(server.store) == len(big)

    def test_cancelling_before_it_starts_removes_nothing(self, big):
        server = SearchingIMAP(list(big))
        stop = threading.Event()
        stop.set()
        removed = engine_on(server).empty_folder("INBOX", cancel=stop)
        assert removed == 0
        assert not server.expunged


class TestTheSearchItselfSurvivesTheMailbox:
    @pytest.mark.parametrize("text", AWKWARD)
    def test_any_subject_can_be_searched_for(self, text):
        """Including the ones that end an IMAP string early."""
        server = SearchingIMAP([
            Message("1", "a@x.example", text, NOW, True, False),
            Message("2", "a@x.example", "something else", NOW, True, False),
        ])
        engine = engine_on(server)
        removed = engine.delete_matching(
            Criteria(subjects=(text,), only_seen=False))
        assert removed == 1
        assert server.expunged == ["1"]

    @pytest.mark.parametrize("count", [1, 2, 3, 7, 25])
    def test_any_number_of_senders_folds_correctly(self, count):
        """OR is binary, so n senders need n-1 of them, correctly nested."""
        messages = [Message(str(n), f"s{n}@x.example", "hi", NOW, True, False)
                    for n in range(40)]
        wanted = tuple(f"s{n}@x.example" for n in range(count))
        server = SearchingIMAP(messages)
        removed = engine_on(server).delete_matching(
            Criteria(senders=wanted, only_seen=False))
        assert removed == count
        assert set(server.expunged) == {str(n) for n in range(count)}

    def test_a_non_ascii_search_says_which_charset(self):
        server = SearchingIMAP([
            Message("1", "a@x.example", "café", NOW, True, False)])
        engine_on(server).delete_matching(
            Criteria(subjects=("café",), only_seen=False))
        assert server.searches[0][:2] == ["CHARSET", "UTF-8"]

    def test_the_folder_is_opened_for_writing_before_deleting(self, big):
        server = SearchingIMAP(list(big))
        engine_on(server).delete_matching(
            Criteria(senders=("news@shop.example",), only_seen=False))
        assert server.readonly is False


class TestOneFolderOnly:
    def test_emptying_names_the_folder_it_was_given(self, big):
        server = SearchingIMAP(list(big))
        engine_on(server).empty_folder("Archive/2019")
        assert server.selected == "Archive/2019"

    def test_clearing_out_names_the_folder_in_the_criteria(self, big):
        server = SearchingIMAP(list(big))
        engine_on(server).delete_matching(
            Criteria(folder="Sorted Mail/To Delete",
                     senders=("news@shop.example",), only_seen=False))
        assert server.selected == "Sorted Mail/To Delete"

    def test_every_message_touched_came_from_that_search(self, big):
        """The UIDs come from a SEARCH of one folder and the STORE and
        EXPUNGE are addressed to those UIDs, so nothing else can be
        reached however long it runs."""
        server = SearchingIMAP(list(big))
        criteria = Criteria(senders=("news@shop.example",), only_seen=False)
        engine_on(server).delete_matching(criteria)
        found = set()
        for name, args in server.commands:
            if name == "UID SEARCH":
                continue
            if name in ("UID STORE", "UID EXPUNGE"):
                found.update(str(args[0]).split(","))
        assert found == set(server.expunged)
