"""What the sorter knows about the world outside the message.

A phrase list can tell you a message says "your flight". It cannot tell you
ryanair.com is an airline, STN is an airport, or argos.co.uk sells things,
and those are what a person uses to read a message that never says what it
is. Two public datasets, 330 KB, no network at run time.
"""

from __future__ import annotations

import gzip
import json

import pytest

import lexicon
from models import OtherCategory
from rules_engine import RuleClassifier, entity_scores, sender_sector


class TestTheFileItself:
    def test_it_is_bundled_and_readable(self):
        assert lexicon.available() is True
        assert "brands" in lexicon.describe()

    def test_it_is_small_enough_to_ship(self):
        from pathlib import Path

        path = Path(lexicon.__file__).parent / "data" / "lexicon.json.gz"
        assert path.stat().st_size < 2 * 1024 * 1024, "too big to bundle"

    def test_it_holds_enough_to_be_worth_carrying(self):
        with gzip.open(lexicon._root() / "data" / "lexicon.json.gz",
                       "rt", encoding="utf-8") as handle:
            data = json.load(handle)
        assert len(data["brands"]) > 20_000
        assert len(data["airports"]) > 3_000

    def test_it_is_read_once(self):
        lexicon.reset()
        first = lexicon._data()
        assert lexicon._data() is first


class TestKnowingWhoSentIt:
    @pytest.mark.parametrize("sender, sector", [
        ("noreply@ryanair.example", "airline"),
        ("no-reply@halifax.example", "bank"),
        ("account@vodafone.example", "telecom"),
        ("noreply@argos.example", "retail"),
        ("offers@boots.example", "retail"),
    ])
    def test_a_known_company_is_recognised(self, sender, sector):
        assert lexicon.sector_of(sender)[0] == sector

    def test_a_subdomain_still_finds_the_company(self):
        """Real mail comes from email.argos.co.uk, not argos.co.uk."""
        assert lexicon.sector_of("x@email.argos.co.uk")[0] == "retail"
        assert lexicon.sector_of("x@mail.ryanair.com")[0] == "airline"

    def test_an_unknown_sender_claims_nothing(self):
        assert lexicon.sector_of("careers@some-startup-nobody.example") == ("", "")

    def test_an_ordinary_word_is_not_a_brand(self):
        """"next" and "post" are somebody's brand and also English."""
        for sender in ("news@some-blog.example", "post@a-forum.example",
                       "team@a-group.example"):
            assert lexicon.sector_of(sender)[0] in ("", "news")

    @pytest.mark.parametrize("junk", [
        "", "   ", "@", "not-an-address", "a@", "@b", "x@.", "x@..",
        "x@" + "a" * 300, "<>", "a@b@c.example",
    ])
    def test_junk_does_not_raise(self, junk):
        lexicon.sector_of(junk)

    def test_the_sector_becomes_a_topic(self):
        found, why = sender_sector("noreply@ryanair.example")
        assert found == {OtherCategory.TRAVEL: pytest.approx(1.6)}
        assert "airline" in why[0]


class TestKnowingWhereAirportsAre:
    def test_real_codes_are_known(self):
        for code in ("STN", "DUB", "LHR", "JFK", "SFO"):
            assert lexicon.is_airport(code) is True

    def test_invented_codes_are_not(self):
        for code in ("ZZZ", "QQQ", "AAA1", "", "PDF"):
            assert lexicon.is_airport(code) is False

    def test_a_flight_is_recognised(self):
        assert lexicon.airport_pair("FR7712 STN to DUB, Tuesday") == ("STN", "DUB")

    def test_a_file_conversion_is_not_a_flight(self):
        """Three capitals either side of "to" is also "PDF to DOC".

        This is the whole reason for carrying four and a half thousand
        airport codes rather than a regular expression.
        """
        assert lexicon.airport_pair("Convert PDF to DOC quickly") is None
        assert lexicon.airport_pair("Export CSV to XML") is None

    def test_the_same_airport_twice_is_not_a_journey(self):
        assert lexicon.airport_pair("LHR to LHR") is None

    def test_the_entity_layer_uses_the_real_list(self):
        real = entity_scores("", "", "Seat 14C", "FR7712 STN to DUB Tuesday")
        assert OtherCategory.TRAVEL in real[0]
        fake = entity_scores("", "", "Convert", "Convert PDF to DOC quickly")
        assert OtherCategory.TRAVEL not in fake[0]


class TestWordsBeatShape:
    """Shape decides when there are no words. It never overrules them."""

    def test_a_code_from_a_bank_is_a_security_notice(self, ):
        """"halifax is a bank" plus an amount of money outscored the code."""
        got = RuleClassifier().classify(
            subject="023844",
            body="Enter 023844 to approve a payment of 240.00 to J WATSON. "
                 "If you did not start this, call us on the number on your card.",
            sender="Halifax <no-reply@halifax.example>")
        assert got.other_category is OtherCategory.SECURITY

    def test_knowing_the_sender_does_not_authorise_a_move(self):
        got = RuleClassifier().classify(
            subject="Seat 14C",
            body="FR7712 STN to DUB, Tuesday. Bags close 40 minutes before.",
            sender="noreply@ryanair.example")
        assert got.other_category is OtherCategory.TRAVEL
        assert got.confidence < 0.95


class TestItDegradesRatherThanBreaking:
    def test_a_missing_file_leaves_the_sorter_working(self, monkeypatch, tmp_path):
        lexicon.reset()
        monkeypatch.setattr(lexicon, "_root", lambda: tmp_path)
        monkeypatch.setattr(lexicon, "__file__", str(tmp_path / "lexicon.py"))
        try:
            assert lexicon.available() is False
            assert lexicon.sector_of("noreply@ryanair.com") == ("", "")
            assert lexicon.airport_pair("STN to DUB") is None
            assert lexicon.is_airport("LHR") is False
            got = RuleClassifier().classify(
                subject="Your security code",
                body="Enter 1234 to sign in. Do not share this code.",
                sender="no-reply@bank.example")
            assert got.other_category is OtherCategory.SECURITY
        finally:
            lexicon.reset()

    def test_a_damaged_file_is_ignored(self, monkeypatch, tmp_path):
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "lexicon.json.gz").write_bytes(b"not a gzip file")
        lexicon.reset()
        monkeypatch.setattr(lexicon, "_root", lambda: tmp_path)
        monkeypatch.setattr(lexicon, "__file__", str(tmp_path / "lexicon.py"))
        try:
            assert lexicon.available() is False
        finally:
            lexicon.reset()
