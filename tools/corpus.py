#!/usr/bin/env python3
"""Run the sorter over the SpamAssassin public corpus.

Six thousand real messages from 2002-2005, with ham and spam labelled. Not in
this repository - it is 60 MB of other people's mail - so fetch it first:

    python tools/corpus.py --fetch
    python tools/corpus.py

What it is good for, and what it is not. The vocabulary is twenty years old, so
the recall figure is a floor rather than a description of how the app does on a
modern inbox. What does carry over is everything structural: real MIME, real
encodings, headers that do not parse, bodies that are one long line, and every
shape of malformed message somebody actually sent. It found two real defects -
a message beginning with seventy underscores took forty-three seconds to
classify, and work-from-home spam was being read as an interview next step.

The number to watch is the last one. Sorting spam into Junk is a nice to have;
sorting somebody's actual mail into Junk is the failure that matters.
"""

from __future__ import annotations

import argparse
import collections
import pathlib
import random
import subprocess
import sys
import tarfile
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CACHE = pathlib.Path.home() / ".cache" / "mail-manager-corpus"
BASE = "https://spamassassin.apache.org/old/publiccorpus/"
ARCHIVES = (
    ("20030228_easy_ham.tar.bz2", "ham"),
    ("20030228_easy_ham_2.tar.bz2", "ham"),
    ("20030228_hard_ham.tar.bz2", "ham"),
    ("20030228_spam.tar.bz2", "spam"),
    ("20050311_spam_2.tar.bz2", "spam"),
)


def fetch() -> int:
    CACHE.mkdir(parents=True, exist_ok=True)
    for name, _label in ARCHIVES:
        target = CACHE / name
        if not target.exists():
            print(f"  downloading {name}")
            urllib.request.urlretrieve(BASE + name, target)
        marker = CACHE / name.replace(".tar.bz2", "")
        if not any(CACHE.glob(marker.name.split("_", 1)[-1] or "*")):
            with tarfile.open(target) as archive:
                archive.extractall(CACHE)
    print(f"  ready in {CACHE}")
    return 0


def messages():
    from imap_engine import parse_message

    for folder in sorted(p for p in CACHE.iterdir() if p.is_dir()):
        label = "spam" if "spam" in folder.name else "ham"
        for path in sorted(folder.iterdir()):
            if path.name.startswith("cmds"):
                continue
            try:
                raw = path.read_bytes()
                yield label, parse_message(uid=path.name, raw=raw, size=len(raw),
                                           flags=(), internaldate=None)
            except Exception as exc:  # noqa: BLE001 - reported below
                yield "unreadable", (path.name, exc)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch", action="store_true", help="download it first")
    parser.add_argument("--sample", type=int, default=0,
                        help="use only this many messages, chosen at random")
    args = parser.parse_args()

    if args.fetch:
        return fetch()
    if not CACHE.is_dir():
        print("The corpus is not here yet. Run: python tools/corpus.py --fetch",
              file=sys.stderr)
        return 1

    from models import Category, OtherCategory
    from rules_engine import RuleClassifier

    rules = RuleClassifier()
    junk = {OtherCategory.SPAM, OtherCategory.PROMOTION}
    everything = [m for m in messages()]
    if args.sample:
        random.seed(11)
        everything = random.sample(everything, min(args.sample, len(everything)))

    unreadable = [m for label, m in everything if label == "unreadable"]
    landed = collections.Counter()
    slowest = []
    spam_seen = spam_junked = ham_seen = ham_junked = ham_as_job = 0
    started = time.monotonic()

    for label, message in everything:
        if label == "unreadable":
            continue
        began = time.monotonic()
        verdict = rules.classify(
            subject=message.subject, body=message.body_text,
            sender=f"{message.sender_name} <{message.sender_email}>",
            links=message.links, list_unsubscribe=message.list_unsubscribe)
        slowest.append((time.monotonic() - began, message.subject[:40]))
        junked = not verdict.is_job_related and verdict.other_category in junk
        if label == "spam":
            spam_seen += 1
            spam_junked += junked
            landed[verdict.other_category.value if not verdict.is_job_related
                   else verdict.category.value] += 1
        else:
            ham_seen += 1
            ham_junked += junked and verdict.confidence >= 0.95
            ham_as_job += (verdict.is_job_related and verdict.confidence >= 0.95
                           and verdict.category is not Category.UNCLASSIFIED_OTHER)

    elapsed = time.monotonic() - started
    total = spam_seen + ham_seen
    print(f"{total} real messages in {elapsed:.0f}s ({total / max(elapsed, 1):.0f}/s)\n")
    print(f"  unreadable                {len(unreadable)}")
    print(f"  slowest single message    {max(slowest)[0]:.2f}s")
    print(f"  spam sent to junk         {spam_junked}/{spam_seen}   "
          f"{spam_junked / max(spam_seen, 1):.1%}")
    print(f"  HAM sent to junk          {ham_junked}/{ham_seen}   "
          f"{ham_junked / max(ham_seen, 1):.2%}   <- the one that matters")
    print(f"  ham filed as job mail     {ham_as_job}/{ham_seen}   "
          f"{ham_as_job / max(ham_seen, 1):.2%}")
    print("\n  where spam landed:")
    for name, count in landed.most_common(8):
        print(f"    {name:<22} {count:>5}  {count / max(spam_seen, 1):>6.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
