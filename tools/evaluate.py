#!/usr/bin/env python3
"""Score the offline classifier against a labelled set.

    ./dev eval                     # score the bundled fixtures
    ./dev eval --file /tmp/x.json  # score your own labelled export
    ./dev eval --misses            # list what it got wrong

The labelled file is a JSON list of objects with subject, sender, body, links,
unsub, truth_job, truth_cat and truth_other. `./dev scan --json` output can be
reshaped into it, which is how the bundled set was produced.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rules_engine import RuleClassifier  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "labelled.json"


def load(path: Path) -> list:
    if not path.exists():
        print(f"No labelled set at {path}", file=sys.stderr)
        raise SystemExit(2)
    return json.loads(path.read_text())


def verdict_of(row: dict) -> str:
    return row["truth_cat"] if row["truth_job"] else f"other/{row['truth_other']}"


def score(rows: list, ruleset: str = "general") -> tuple:
    rules = RuleClassifier(ruleset=ruleset)
    job_hits = cat_hits = 0
    confident_hits = confident_total = 0
    misses = []
    for row in rows:
        v = rules.classify(
            subject=row.get("subject", ""), body=row.get("body", ""),
            sender=row.get("sender", ""), links=row.get("links", ()),
            list_unsubscribe=row.get("unsub", ""),
        )
        got = v.category.value if v.is_job_related else f"other/{v.other_category.value}"
        want = verdict_of(row)
        if v.is_job_related == row["truth_job"]:
            job_hits += 1
        if got == want:
            cat_hits += 1
        else:
            misses.append((want, got, round(v.confidence, 2), row.get("subject", "")[:66]))
        # Of the ones it was confident enough to file, how many were right?
        if v.confidence >= 0.95:
            confident_total += 1
            confident_hits += int(got == want)
    return job_hits, cat_hits, confident_hits, confident_total, misses


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="evaluate", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", default=str(FIXTURES))
    parser.add_argument("--ruleset", default="general")
    parser.add_argument("--misses", action="store_true", help="List every disagreement.")
    args = parser.parse_args(argv)

    rows = load(Path(args.file))
    job, cat, cok, ctot, misses = score(rows, args.ruleset)
    total = len(rows)

    print(f"labelled set: {total} messages   rule set: {args.ruleset}")
    print(f"  job vs not-job     {job:>4}/{total}   {job / total * 100:5.1f}%")
    print(f"  exact category     {cat:>4}/{total}   {cat / total * 100:5.1f}%")
    if ctot:
        print(f"  of those it filed  {cok:>4}/{ctot}   {cok / ctot * 100:5.1f}%   "
              f"(confidence >= 0.95)")
    print(f"  sent for review    {total - ctot:>4}/{total}   "
          f"{(total - ctot) / total * 100:5.1f}%")

    if misses:
        print("\nmiss pattern (expected -> got):")
        for (want, got), n in Counter((w, g) for w, g, _, _ in misses).most_common(12):
            print(f"  {n:>3}  {want:22} -> {got}")
    if args.misses:
        print("\nevery disagreement:")
        for want, got, conf, subject in misses:
            print(f"  {conf:4.2f}  {want:22} -> {got:22} {subject}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
