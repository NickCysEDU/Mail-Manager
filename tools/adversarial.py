#!/usr/bin/env python3
"""Score the offline sorter against the held-out adversarial set.

Different in kind from tools/evaluate.py. That set is real mail, collected and
labelled. This one is written, and written specifically to be awkward: no
phrase is lifted from the signal tables, several messages are built to mislead,
and a handful are about a job without being job-search mail at all.

The number it prints is meant to be lower than the one from the real set. It is
useful as a floor and as a list of where the reasoning gives out, not as a
headline figure - and the moment a signal is added because it appears in one of
these messages, the set stops measuring anything.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rules_engine import RuleClassifier  # noqa: E402

THRESHOLD = 0.95


def label(job: bool, cat: str, other: str) -> str:
    return cat if job else f"other/{other}"


def main() -> int:
    rows = json.loads((ROOT / "tests" / "fixtures" / "adversarial.json").read_text("utf-8"))
    rules = RuleClassifier()

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
        if verdict.is_job_related == row["truth_job"]:
            job_ok += 1
        if got == want:
            exact += 1
        elif verdict.confidence >= THRESHOLD:
            filed_ok -= 0            # counted below
        if verdict.confidence >= THRESHOLD:
            filed += 1
            if got == want:
                filed_ok += 1
        if got != want:
            misses.append((want, got, verdict.confidence, row["subject"], row.get("note", "")))

    total = len(rows)
    print(f"adversarial set: {total} written messages, none using table phrasing\n")
    print(f"  job vs not-job    {job_ok:>3}/{total}   {job_ok / total:.1%}")
    print(f"  exact label       {exact:>3}/{total}   {exact / total:.1%}")
    if filed:
        print(f"  of those it filed {filed_ok:>3}/{filed}   {filed_ok / filed:.1%}"
              f"   (confidence >= {THRESHOLD})")
    print(f"  held for review   {total - filed:>3}/{total}   {(total - filed) / total:.1%}")

    if misses:
        print(f"\n  {len(misses)} wrong:")
        for want, got, conf, subject, note in sorted(misses, key=lambda m: -m[2]):
            flag = "FILED " if conf >= THRESHOLD else "review"
            print(f"    [{flag} {conf:.2f}]  {want}  ->  {got}")
            print(f"        {subject[:58]!r}" + (f"  ({note})" if note else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
