#!/usr/bin/env python3
"""Tune the sorter against your own inbox, without your inbox leaving it.

The eval fixtures in this repository are hand-written stand-ins. Real mail is
better evidence than any of them, and the only real mail available is yours -
so this reads it, runs the sorter over it, and tells you where the sorter is
weakest, in terms general enough to act on.

    ./dev tune                       # what the sorter is unsure about
    ./dev tune --days 30             # a wider window
    ./dev tune --corrections         # where it disagrees with what you taught it
    ./dev tune --write my-set.json   # a labelled set, for evaluate.py

Nothing it writes goes near the repository. The output path must be outside
it, and a check refuses to write anywhere tracked by git. The summary printed
to the terminal carries no addresses, no subjects and no message text - only
counts, categories and confidence bands, which is everything you need to know
where the gaps are and nothing you would mind reading aloud.

The one exception is ``--write``, which writes real messages to a real file
because that is what a labelled set is. That file is yours. Keep it out of the
repository, and note that ``tests/test_privacy.py`` will fail the moment
anything shaped like a personal address is committed - which is the point.
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import corrections  # noqa: E402
import rules_engine  # noqa: E402
from config import CredentialStore, Settings  # noqa: E402
from imap_engine import IMAPEngine  # noqa: E402


#: Bands to report confidence in. Wide, because the useful question is "does
#: it know or is it guessing", not "is it 0.71 or 0.73".
BANDS = ((0.95, "confident"), (0.75, "fairly sure"), (0.5, "guessing"),
         (0.0, "no idea"))


def band(score: float) -> str:
    for floor, name in BANDS:
        if score >= floor:
            return name
    return "no idea"


def inside_the_repository(path: Path) -> bool:
    """Whether git would track a file written here."""
    try:
        path = path.resolve()
    except OSError:
        return False
    try:
        path.relative_to(ROOT)
    except ValueError:
        return False
    return True


def fetch(settings: Settings, store: CredentialStore, days: int, limit: int):
    """Every message in the window, from every mailbox that is switched on."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    out = []
    for account in settings.scan_accounts:
        password = store.get_mailbox_password(account.address)
        if not password:
            print(f"  {account.label}: no password stored, skipped")
            continue
        engine = IMAPEngine(host=account.host, port=account.port)
        try:
            engine.connect(account.address, password)
            scan = engine.fetch_window(
                start=start, end=end, mailbox=account.source_mailbox,
                max_messages=limit, connections=account.connections)
            out.extend(scan.messages)
            print(f"  {account.label}: {len(scan.messages)} message(s)")
        finally:
            try:
                engine.logout()
            except Exception:  # pragma: no cover - best effort
                pass
    return out


def verdicts(messages, ruleset: str):
    engine = rules_engine.RuleClassifier(ruleset=ruleset)
    for message in messages:
        yield message, engine.classify(
            subject=message.subject, body=message.body_text,
            sender=message.sender_display, links=message.links,
            list_unsubscribe=message.list_unsubscribe,
            truncated=message.truncated)


def report(pairs) -> None:
    """Counts and bands only. Nothing here identifies a message."""
    by_band = collections.Counter()
    by_verdict = collections.Counter()
    unsure = collections.Counter()
    no_evidence = 0
    for _message, verdict in pairs:
        name = band(verdict.confidence)
        by_band[name] += 1
        label = (verdict.category.value if verdict.is_job_related
                 else f"other/{verdict.other_category.value}")
        by_verdict[label] += 1
        if verdict.confidence < 0.75:
            unsure[label] += 1
        if not verdict.matched:
            no_evidence += 1

    total = sum(by_band.values()) or 1
    print(f"\n{total} message(s) read.\n")
    print("how sure it was:")
    for _floor, name in BANDS:
        count = by_band.get(name, 0)
        print(f"  {name:14} {count:5}  {count / total:6.1%}")

    print("\nwhere they landed:")
    for label, count in by_verdict.most_common():
        print(f"  {label:24} {count:5}  {count / total:6.1%}")

    print("\nweakest ground - what it lands on while unsure:")
    for label, count in unsure.most_common(8):
        share = count / max(1, by_verdict[label])
        print(f"  {label:24} {count:5}  ({share:.0%} of that category)")

    print(f"\n{no_evidence} message(s) matched no phrase at all "
          f"({no_evidence / total:.1%}). Those are the ones a new signal "
          "would help most.")


def against_corrections(pairs) -> None:
    """Where the sorter still disagrees with what you have taught it.

    Every one of these is a message you filed somewhere by hand and the
    sorter would still put elsewhere - which is the most direct evidence
    there is of a gap in the rules.
    """
    memory = corrections.Memory.load()
    if not len(memory):
        print("\nNothing has been corrected yet, so there is nothing to check "
              "against. Correct a few rows in the app and run this again.")
        return
    disagreements = collections.Counter()
    checked = 0
    for message, verdict in pairs:
        learned = memory.lookup(message.sender_email)
        if learned is None:
            continue
        checked += 1
        label = (verdict.category.value if verdict.is_job_related
                 else f"other/{verdict.other_category.value}")
        leaf = learned.folder.rsplit("/", 1)[-1]
        if leaf.lower() not in label.lower().replace("_", " "):
            disagreements[(leaf, label)] += 1

    print(f"\n{checked} message(s) came from a sender you have corrected.")
    if not disagreements:
        print("The sorter agrees with all of them.")
        return
    print("\nstill disagreeing (you said -> it says):")
    for (wanted, got), count in disagreements.most_common(15):
        print(f"  {wanted:22} -> {got:24} {count:4}")


def write_set(pairs, path: Path) -> int:
    """Write a labelled set in the shape evaluate.py reads.

    The labels are the sorter's own, so this is a starting point to correct
    rather than an answer key - which is the honest way round: a set labelled
    by the thing being measured measures nothing.
    """
    if inside_the_repository(path):
        print(f"Refusing to write {path}: it is inside the repository. "
              "Real mail does not belong in version control.", file=sys.stderr)
        return 2
    rows = []
    for message, verdict in pairs:
        rows.append({
            "subject": message.subject,
            "sender": message.sender_display,
            "body": message.body_text,
            "unsub": message.list_unsubscribe,
            "links": list(message.links),
            # Pre-filled from the sorter, for a person to correct by hand.
            "truth_job": verdict.is_job_related,
            "truth_cat": verdict.category.value,
            "truth_other": verdict.other_category.value,
            "_confidence": round(verdict.confidence, 3),
            "_check_this": verdict.confidence < 0.75,
        })
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    unsure = sum(1 for row in rows if row["_check_this"])
    print(f"\nWrote {len(rows)} message(s) to {path}.")
    print(f"{unsure} of them are marked _check_this - correct those labels "
          "first, they are where the sorter was least sure.")
    print("Then: python tools/evaluate.py --file " + str(path))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="tune", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--ruleset", default="")
    parser.add_argument("--corrections", action="store_true",
                        help="Check against what you have taught it.")
    parser.add_argument("--write", metavar="PATH",
                        help="Write a labelled set, outside the repository.")
    args = parser.parse_args(argv)

    settings = Settings.load()
    store = CredentialStore()
    print(f"Reading the last {args.days} day(s)…")
    messages = fetch(settings, store, args.days, args.limit)
    if not messages:
        print("Nothing to read.")
        return 1

    pairs = list(verdicts(messages, args.ruleset or settings.ruleset))
    report(pairs)
    if args.corrections:
        against_corrections(pairs)
    if args.write:
        return write_set(pairs, Path(args.write).expanduser())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
