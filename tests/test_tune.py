"""The tuning tool, and the promise it makes about where things are written."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import tune  # noqa: E402

from models import Category, EmailMessage, OtherCategory  # noqa: E402
from rules_engine import RuleVerdict  # noqa: E402


def verdict(confidence=0.9, job=True, matched=("a phrase",)) -> RuleVerdict:
    return RuleVerdict(
        is_job_related=job, category=Category.INTERVIEW,
        other_category=(OtherCategory.NOT_APPLICABLE if job
                        else OtherCategory.OTHER),
        confidence=confidence, summary="s", reasoning="r", matched=matched)


def pair(subject="Interview", sender="jane@acme.example", **kwargs):
    return (EmailMessage(uid="1", subject=subject, sender_email=sender,
                         sender_name="Jane", body_text="Some words."),
            verdict(**kwargs))


class TestWhereItWillWrite:
    def test_it_refuses_the_repository(self, capsys):
        assert tune.write_set([pair()], ROOT / "tests" / "mine.json") == 2
        assert "Refusing" in capsys.readouterr().err

    def test_it_refuses_a_subdirectory_of_the_repository(self):
        assert tune.write_set([pair()], ROOT / "data" / "deep" / "mine.json") == 2

    def test_it_writes_outside_it(self, tmp_path):
        target = tmp_path / "mine.json"
        assert tune.write_set([pair()], target) == 0
        assert target.exists()

    def test_inside_the_repository(self, tmp_path):
        assert tune.inside_the_repository(ROOT / "anything.json")
        assert tune.inside_the_repository(ROOT / "a" / "b" / "c.json")
        assert not tune.inside_the_repository(tmp_path / "anything.json")


class TestWhatItWrites:
    def test_the_shape_evaluate_reads(self, tmp_path):
        target = tmp_path / "mine.json"
        tune.write_set([pair(), pair(confidence=0.4)], target)
        rows = json.loads(target.read_text())
        assert len(rows) == 2
        for row in rows:
            assert set(row) >= {"subject", "sender", "body", "truth_job",
                                "truth_cat", "truth_other"}

    def test_the_unsure_ones_are_marked_for_a_person_to_check(self, tmp_path):
        target = tmp_path / "mine.json"
        tune.write_set([pair(confidence=0.99), pair(confidence=0.4)], target)
        rows = json.loads(target.read_text())
        assert [row["_check_this"] for row in rows] == [False, True]

    def test_evaluate_can_read_it_back(self, tmp_path):
        """The two tools have to agree on the file format, or this is useless."""
        sys.path.insert(0, str(ROOT / "tools"))
        import evaluate

        target = tmp_path / "mine.json"
        tune.write_set([pair(), pair(subject="Offer")], target)
        rows = json.loads(target.read_text())
        job, cat, _confident, _total, _misses = evaluate.score(rows)
        assert job + cat >= 0          # it read every row without a KeyError
        assert job <= len(rows) and cat <= len(rows)


class TestTheSummaryGivesNothingAway:
    """The point of printing counts is that they can be shared."""

    def test_no_subject_sender_or_body_reaches_the_terminal(self, capsys):
        pairs = [pair(subject="Interview with Northwind about the staff role",
                      sender="imogen.blake@northwind-tech.example"),
                 pair(confidence=0.4, subject="Your parcel is late")]
        tune.report(pairs)
        printed = capsys.readouterr().out
        for secret in ("Northwind", "imogen", "blake", "parcel",
                       "Some words", "northwind-tech"):
            assert secret.lower() not in printed.lower(), secret

    def test_it_says_how_sure_it_was(self, capsys):
        tune.report([pair(confidence=0.99), pair(confidence=0.8),
                     pair(confidence=0.6), pair(confidence=0.1)])
        printed = capsys.readouterr().out
        for name in ("confident", "fairly sure", "guessing", "no idea"):
            assert name in printed

    def test_it_names_the_weakest_ground(self, capsys):
        tune.report([pair(confidence=0.4) for _ in range(5)])
        printed = capsys.readouterr().out
        assert "weakest ground" in printed
        assert "INTERVIEW" in printed

    def test_it_counts_what_matched_nothing(self, capsys):
        tune.report([pair(matched=()), pair(matched=("a phrase",))])
        assert "1 message(s) matched no phrase at all" in capsys.readouterr().out

    def test_bands(self):
        assert tune.band(1.0) == "confident"
        assert tune.band(0.8) == "fairly sure"
        assert tune.band(0.6) == "guessing"
        assert tune.band(0.0) == "no idea"


class TestAgainstWhatYouTaughtIt:
    def test_nothing_learned_says_so(self, capsys, tmp_path, monkeypatch):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        tune.against_corrections([pair()])
        assert "Nothing has been corrected yet" in capsys.readouterr().out

    def test_it_finds_a_disagreement(self, capsys, tmp_path, monkeypatch):
        import corrections
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        memory = corrections.Memory.load()
        memory.remember_move("jane@acme.example", "Job Search/Offers")
        memory.save()

        tune.against_corrections([pair()])       # the sorter says INTERVIEW
        printed = capsys.readouterr().out
        assert "still disagreeing" in printed
        assert "Offers" in printed

    def test_agreement_is_reported_as_agreement(self, capsys, tmp_path,
                                                monkeypatch):
        import corrections
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        memory = corrections.Memory.load()
        memory.remember_move("jane@acme.example", "Job Search/Interview")
        memory.save()

        tune.against_corrections([pair()])
        assert "agrees with all of them" in capsys.readouterr().out
