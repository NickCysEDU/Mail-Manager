#!/usr/bin/env python3
"""Score the offline sorter against mail nobody wrote to a template.

Two sets, and the difference between them is the point.

``adversarial.json`` was written first and then used to guide the structural
features, so it is a dev set: it flatters the thing it shaped.

``holdout.json`` was written after that work was finished and is scored once.
Nothing has been added to the engine because of anything in it. If that ever
changes it stops measuring anything and a third set is needed.

Neither is a headline figure. ``tools/evaluate.py`` runs against real collected
mail, and that is the number that describes ordinary use, because ordinary
transactional mail comes out of templates and these deliberately do not. What
these are for is the floor, and above all the last line of each block: how much
gets filed wrongly when the sorter is out of its depth.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rules_engine import RuleClassifier  # noqa: E402

THRESHOLD = 0.95
SETS = (
    ("adversarial.json", "dev set", "guided the structural work, so it flatters it"),
    ("holdout.json", "held out", "written afterwards, never tuned against"),
)


def label(job: bool, category: str, other: str) -> str:
    return category if job else f"other/{other}"


def score(rows, rules: RuleClassifier):
    job_ok = exact = filed = filed_ok = 0
    misses = []
    for row in rows:
        verdict = rules.classify(
            subject=row["subject"], body=row["body"], sender=row["sender"],
            list_unsubscribe=row.get("unsub", ""),
        )
        want = label(row["truth_job"], row["truth_cat"], row["truth_other"])
        got = label(verdict.is_job_related, verdict.category.value,
                    verdict.other_category.value)
        job_ok += verdict.is_job_related == row["truth_job"]
        exact += got == want
        if verdict.confidence >= THRESHOLD:
            filed += 1
            filed_ok += got == want
        if got != want:
            misses.append((verdict.confidence, want, got, row["subject"],
                           row.get("note", "")))
    return job_ok, exact, filed, filed_ok, misses


def main() -> int:
    rules = RuleClassifier()
    verbose = "-v" in sys.argv or "--verbose" in sys.argv

    for filename, title, caveat in SETS:
        path = ROOT / "tests" / "fixtures" / filename
        if not path.is_file():
            continue
        rows = json.loads(path.read_text("utf-8"))
        job_ok, exact, filed, filed_ok, misses = score(rows, rules)
        total = len(rows)
        wrongly_filed = filed - filed_ok

        print(f"\n{title} - {total} written messages ({caveat})")
        print(f"  job vs not-job     {job_ok:>3}/{total}   {job_ok / total:.1%}")
        print(f"  exact label        {exact:>3}/{total}   {exact / total:.1%}")
        print(f"  held for review    {total - filed:>3}/{total}   {(total - filed) / total:.1%}")
        print(f"  FILED WRONGLY      {wrongly_filed:>3}/{total}   "
              f"{wrongly_filed / total:.1%}   <- the one that matters")

        if verbose and misses:
            for conf, want, got, subject, note in sorted(misses, reverse=True):
                tag = "FILED " if conf >= THRESHOLD else "review"
                print(f"    [{tag} {conf:.2f}]  {want} -> {got}   {subject[:44]!r}"
                      + (f"  ({note})" if note else ""))
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
