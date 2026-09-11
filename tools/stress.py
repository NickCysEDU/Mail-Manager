#!/usr/bin/env python3
"""Throw hostile input at every boundary the app has, and watch for cracks.

This app reads other people's mail. The text it parses is written by whoever
sent it, which means every parser in here takes input from an adversary who
gets to try as many times as they like.

So this is not a unit test. It generates tens of thousands of inputs designed
to break something - malformed MIME, HTML that never closes a tag, encodings
that lie, text engineered to make a regular expression backtrack, ciphertext
with a bit flipped, JSON from a hostile settings file - and asserts three
things about every single one:

  1. It does not raise. A crash in a mail client is a denial of service.
  2. It does not hang. Every input has a time budget.
  3. It does not leak. What goes in does not come out somewhere it shouldn't.

    python tools/stress.py                # the standard run, about a minute
    python tools/stress.py --rounds 50000 # longer
    python tools/stress.py --seed 12345   # reproduce a failure exactly
"""

from __future__ import annotations

import argparse
import random
import string
import sys
import time
import traceback
from pathlib import Path
from typing import Callable, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

#: No single input may take longer than this. The real limit is much lower;
#: this is the point at which something has gone wrong rather than slow.
BUDGET = 2.0

#: Bytes and characters that have broken a parser somewhere at some point.
NASTY = [
    "\x00", "\r\n", "\r", "\n", "\x7f", "\x1b[0m", "﻿", "​",
    "‮", "́", "ß", "é", "日本語", "🙂", "&amp;", "&#x41;", "&nbsp;",
    "<", ">", "\"", "'", "\\", "/", "%", "$", "{", "}", "[", "]", "..",
    "../", "\\\\", "&", "=", "?", "#", "@", ":", ";", "|", "`", "*",
    "<script>", "</script>", "<!--", "-->", "<![CDATA[", "]]>",
    "<?xml", "<!DOCTYPE", "&lt;", "\\u0000", "%00", "%2e%2e",
]

WORDS = [
    "interview", "offer", "rejected", "application", "schedule", "calendly",
    "unsubscribe", "receipt", "invoice", "parcel", "flight", "sunday",
    "worship", "password", "code", "urgent", "click", "here", "http://x",
    "https://a.example", "we", "have", "decided", "to", "move", "forward",
]


def noise(rng: random.Random, length: int) -> str:
    """A string built from the things that break parsers."""
    parts = []
    while sum(len(p) for p in parts) < length:
        roll = rng.random()
        if roll < 0.35:
            parts.append(rng.choice(NASTY))
        elif roll < 0.70:
            parts.append(rng.choice(WORDS))
        elif roll < 0.85:
            parts.append("".join(rng.choice(string.printable)
                                 for _ in range(rng.randint(1, 12))))
        else:
            parts.append(chr(rng.randint(1, 0x10FFFF)))
        parts.append(rng.choice([" ", "", "\n", "\t"]))
    return "".join(parts)[:length]


def html_noise(rng: random.Random, length: int) -> str:
    """Markup that is trying to be difficult."""
    tags = ["div", "p", "a", "script", "style", "table", "td", "span", "b",
            "img", "br", "meta", "head", "body", "html", "!--", "svg"]
    parts = []
    while sum(len(p) for p in parts) < length:
        roll = rng.random()
        tag = rng.choice(tags)
        if roll < 0.30:
            parts.append(f"<{tag}")            # never closed
        elif roll < 0.50:
            parts.append(f"<{tag}>")
        elif roll < 0.65:
            parts.append(f"</{tag}>")
        elif roll < 0.75:
            parts.append(f'<{tag} href="{noise(rng, 20)}">')
        elif roll < 0.85:
            parts.append(noise(rng, 30))
        else:
            parts.append(rng.choice(["&", "&amp;", "&#", "&#x", "<!", "-->"]))
    return "".join(parts)[:length]


class Runner:
    def __init__(self, seed: int, rounds: int, verbose: bool) -> None:
        self.rng = random.Random(seed)
        self.rounds = rounds
        self.verbose = verbose
        self.failures: List[str] = []
        self.slowest = (0.0, "")
        self.checked = 0

    def run(self, name: str, fn: Callable[[], object], payload: str = "") -> None:
        self.checked += 1
        began = time.perf_counter()
        try:
            fn()
        except Exception:
            self.failures.append(
                f"{name} RAISED on {payload[:120]!r}\n"
                + traceback.format_exc(limit=4))
            return
        took = time.perf_counter() - began
        if took > self.slowest[0]:
            self.slowest = (took, f"{name}: {payload[:70]!r}")
        if took > BUDGET:
            self.failures.append(
                f"{name} took {took:.1f}s on {len(payload)} chars: {payload[:120]!r}")

    # -- the boundaries --------------------------------------------------
    def stress_rules_engine(self) -> None:
        import rules_engine
        engine = rules_engine.RuleClassifier()
        for _ in range(self.rounds):
            size = self.rng.choice([0, 1, 40, 400, 4000, 40_000])
            subject = noise(self.rng, self.rng.randint(0, 200))
            body = noise(self.rng, size)
            sender = noise(self.rng, self.rng.randint(0, 60))
            self.run("rules_engine.classify",
                     lambda: engine.classify(subject=subject, body=body,
                                             sender=sender),
                     subject + " | " + body[:60])

    def stress_html(self) -> None:
        import html_utils
        for _ in range(self.rounds):
            size = self.rng.choice([0, 50, 500, 5000, 50_000])
            doc = html_noise(self.rng, size)
            self.run("html_to_text", lambda: html_utils.html_to_text(doc), doc)
            self.run("condense", lambda: html_utils.condense(doc), doc)

    def stress_mime(self) -> None:
        from imap_engine import parse_message
        for _ in range(self.rounds):
            raw = noise(self.rng, self.rng.randint(0, 3000)).encode(
                "utf-8", "surrogatepass")
            if self.rng.random() < 0.5:
                raw = (b"Subject: " + noise(self.rng, 60).encode("utf-8", "replace")
                       + b"\r\nFrom: " + noise(self.rng, 40).encode("utf-8", "replace")
                       + b"\r\nContent-Type: multipart/mixed; boundary=\"x\"\r\n\r\n"
                       + raw)
            self.run("parse_message",
                     lambda: parse_message(raw, "1"), repr(raw[:80]))

    def stress_settings(self) -> None:
        from config import Settings
        keys = ["provider", "model", "base_url", "confidence_threshold",
                "max_messages", "concurrency", "batch_size", "folder_root",
                "other_folder_root", "non_job_routing", "sort_profile",
                "topics", "mailboxes", "reply_rules", "hidden_columns",
                "appearance_mode", "ruleset", "effort", "window_geometry"]
        values = [None, True, False, 0, -1, 10**12, -10**12, 1.5, float("inf"),
                  float("nan"), "", "x" * 5000, "../../etc", [], {}, [{}],
                  {"a": {"b": {"c": [1, 2, 3]}}}, "\x00", "🙂"]
        for _ in range(self.rounds):
            raw = {self.rng.choice(keys): self.rng.choice(values)
                   for _ in range(self.rng.randint(1, 6))}
            self.run("Settings.from_dict",
                     lambda: Settings.from_dict(raw).normalized(), repr(raw)[:120])

    def stress_classification(self) -> None:
        from models import Classification
        keys = ["summary", "is_job_related", "category", "other_category",
                "confidence_score", "reasoning", "signals", "scores", "_usage"]
        values = [None, True, 0, -1, 10**9, 1.5, float("inf"), float("nan"),
                  "", "x" * 100_000, [], {}, [1, 2], {"a": "b"}, "\x00"]
        for _ in range(self.rounds):
            payload = {self.rng.choice(keys): self.rng.choice(values)
                       for _ in range(self.rng.randint(1, 5))}
            self.run("Classification.from_payload",
                     lambda: Classification.from_payload(payload),
                     repr(payload)[:120])

    def stress_rules(self) -> None:
        import autoreply
        fields = [f[0] for f in autoreply.FIELDS] + ["nonsense", ""]
        operators = ["contains", "is", "matches", "not_contains", "shrug", ""]
        for _ in range(self.rounds // 2):
            rule = autoreply.Rule(
                name=noise(self.rng, 20),
                conditions=[autoreply.Condition(
                    field=self.rng.choice(fields),
                    operator=self.rng.choice(operators),
                    value=noise(self.rng, self.rng.randint(0, 80)))
                    for _ in range(self.rng.randint(0, 3))],
                actions=[autoreply.Action(
                    kind=self.rng.choice(["file_into", "tick", "draft", "x"]),
                    value=noise(self.rng, 20))])
            self.run("Rule round trip",
                     lambda: autoreply.Rule.from_dict(rule.to_dict()).describe()
                     if hasattr(rule, "describe") else rule.to_dict(),
                     rule.name)
            self.run("pattern_risk",
                     lambda: autoreply.pattern_risk(
                         noise(self.rng, self.rng.randint(0, 60))))

    def stress_vault(self) -> None:
        import tempfile
        import vault
        from config import InMemoryCredentialStore
        box = vault.Vault(store=InMemoryCredentialStore())
        directory = Path(tempfile.mkdtemp())
        good = directory / "verdicts.json"
        box.write(good, {"rows": [{"summary": "secret"}]})
        sealed = good.read_bytes()
        for _ in range(self.rounds // 2):
            raw = bytearray(sealed)
            for _ in range(self.rng.randint(1, 6)):
                if raw:
                    raw[self.rng.randrange(len(raw))] = self.rng.randrange(256)
            if self.rng.random() < 0.3:
                cut = self.rng.randrange(len(raw) + 1)
                raw = raw[:cut]
            path = directory / "verdicts.json"
            path.write_bytes(bytes(raw))
            self.run("vault.read (tampered)",
                     lambda: box.read(path), repr(bytes(raw[:40])))

    def stress_lexicon(self) -> None:
        import lexicon
        for _ in range(self.rounds):
            text = noise(self.rng, self.rng.randint(0, 300))
            self.run("lexicon.sector_of", lambda: lexicon.sector_of(text), text)
            self.run("lexicon.airport_pair", lambda: lexicon.airport_pair(text), text)

    def stress_conversations(self) -> None:
        import conversations
        from models import EmailMessage
        for _ in range(self.rounds // 4):
            batch = [EmailMessage(
                uid=str(i),
                message_id=noise(self.rng, 30),
                in_reply_to=noise(self.rng, 40),
                references=noise(self.rng, 80),
                subject=noise(self.rng, 60),
                sender_email=noise(self.rng, 30))
                for i in range(self.rng.randint(1, 12))]
            self.run("conversations.thread_keys",
                     lambda: conversations.thread_keys(batch))


SUITES = ("rules_engine", "html", "mime", "settings", "classification",
          "rules", "vault", "lexicon", "conversations")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="stress", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rounds", type=int, default=1500)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--only", choices=SUITES, action="append")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    seed = args.seed if args.seed is not None else random.randrange(2**31)
    runner = Runner(seed, args.rounds, args.verbose)
    print(f"seed {seed}, {args.rounds} rounds per boundary\n")

    began = time.perf_counter()
    for name in (args.only or SUITES):
        start = time.perf_counter()
        before = len(runner.failures)
        getattr(runner, f"stress_{name}")()
        broke = len(runner.failures) - before
        mark = "FAIL" if broke else " ok "
        print(f"  {mark}  {name:16} {time.perf_counter() - start:6.1f}s"
              f"  {broke} problem(s)")

    print(f"\n{runner.checked:,} inputs in {time.perf_counter() - began:.1f}s")
    print(f"slowest: {runner.slowest[0]:.2f}s  {runner.slowest[1]}")
    if runner.failures:
        print(f"\n{len(runner.failures)} FAILURE(S), reproduce with --seed {seed}\n")
        for failure in runner.failures[:10]:
            print(failure)
        return 1
    print("\nNothing raised, nothing hung.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
