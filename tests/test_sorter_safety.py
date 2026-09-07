"""The property the offline sorter has to keep: never confidently wrong.

Its accuracy on collected mail is good and on hand-written mail is poor, and
both of those are measurements rather than promises - tools/evaluate.py and
tools/adversarial.py print them. Neither is asserted here, because pinning an
accuracy figure in a test turns every future improvement into a failing test
and every fixture into something to tune against.

What is asserted is the property the design actually rests on. The app files
mail into folders without being watched. Getting a message wrong and holding it
for a person to look at costs that person a moment. Getting it wrong and filing
it costs them a message they will not find again. Those are not the same
mistake, and only the second one is a bug.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rules_engine import RuleClassifier

FIXTURES = Path(__file__).parent / "fixtures"
FILING_THRESHOLD = 0.95


def load(name):
    path = FIXTURES / name
    if not path.is_file():
        pytest.skip(f"{name} is not present")
    return json.loads(path.read_text("utf-8"))


def expected(row) -> str:
    return row["truth_cat"] if row["truth_job"] else f"other/{row['truth_other']}"


def actual(verdict) -> str:
    return (verdict.category.value if verdict.is_job_related
            else f"other/{verdict.other_category.value}")


@pytest.fixture(scope="module")
def rules():
    return RuleClassifier()


@pytest.mark.parametrize("fixture", ["adversarial.json", "holdout.json", "labelled.json"])
def test_nothing_is_filed_into_the_wrong_folder(rules, fixture):
    """Above the filing threshold, being wrong is not allowed to be common.

    One in fifty is the budget, and it is deliberately a budget rather than
    zero: the labelled set contains a genuine near-miss, and demanding
    perfection there would only push somebody to weaken the threshold.
    """
    rows = load(fixture)
    wrong = []
    for row in rows:
        verdict = rules.classify(
            subject=row["subject"], body=row["body"], sender=row["sender"],
            list_unsubscribe=row.get("unsub", "") or row.get("list_unsubscribe", ""),
        )
        if verdict.confidence < FILING_THRESHOLD:
            continue                      # held for review, which is the safe outcome
        if actual(verdict) != expected(row):
            wrong.append((row["subject"], expected(row), actual(verdict),
                          verdict.confidence))

    filed = sum(
        1 for row in rows
        if rules.classify(
            subject=row["subject"], body=row["body"], sender=row["sender"],
            list_unsubscribe=row.get("unsub", "") or row.get("list_unsubscribe", ""),
        ).confidence >= FILING_THRESHOLD
    )
    allowed = max(1, filed // 50)
    assert len(wrong) <= allowed, (
        f"{len(wrong)} of {filed} filed messages went to the wrong folder:\n"
        + "\n".join(f"  {s!r}: wanted {w}, got {g} at {c:.2f}" for s, w, g, c in wrong)
    )


def test_a_message_it_cannot_read_is_not_guessed_at(rules):
    """Nothing recognisable in it should mean no confidence, not a coin toss."""
    verdict = rules.classify(
        subject="fwd", body="see below", sender="a@b.example")
    assert verdict.confidence < FILING_THRESHOLD


def test_the_held_out_set_is_never_used_to_tune(rules):
    """A guard on the process rather than the code.

    If the held-out set ever starts scoring like the dev set, either the engine
    genuinely generalised or somebody quietly fitted to it. Both are worth
    stopping to look at, and the second is the likely one.
    """
    holdout = load("holdout.json")
    correct = sum(
        1 for row in holdout
        if actual(rules.classify(
            subject=row["subject"], body=row["body"], sender=row["sender"],
            list_unsubscribe=row.get("unsub", ""))) == expected(row)
    )
    assert correct / len(holdout) < 0.80, (
        "The held-out set is scoring like a training set. If the engine really "
        "did improve this much, write a fresh set and re-measure before "
        "believing it."
    )
