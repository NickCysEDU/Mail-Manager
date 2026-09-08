#!/usr/bin/env python3
"""Score a real model against a labelled set, with no way to fool yourself.

    ./dev eval-llm --provider gemini --model gemini-flash-latest
    ./dev eval-llm --file /tmp/mine.json --misses

The engine falls back to the offline rules when a request fails, which is the
right behaviour for a scan and the wrong behaviour for a measurement: a run
that quietly answers half its questions locally reports the rule set's
accuracy under the model's name. This refuses to count such a row at all.

Every request costs money and free tiers are small, so it goes one message at
a time, pauses between them, and says what it spent.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import CredentialStore  # noqa: E402
from llm_engine import LLMEngine  # noqa: E402
from models import EmailMessage  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "labelled.json"


def as_message(row: dict, index: int) -> EmailMessage:
    sender = row.get("sender", "")
    return EmailMessage(
        uid=str(index),
        subject=row.get("subject", ""),
        sender_name=sender.split("<")[0].strip(),
        sender_email=sender.split("<")[-1].strip(">").strip(),
        body_text=row.get("body", ""),
        links=list(row.get("links") or ()),
        list_unsubscribe=row.get("unsub", ""),
    )


def ask(engine: LLMEngine, message: EmailMessage, patience: int):
    """One verdict from the model, or a reason there isn't one."""
    for attempt in range(patience):
        try:
            verdict = engine.classify(message)
        except Exception as exc:  # noqa: BLE001
            text = str(exc)
            if "429" not in text and "quota" not in text.lower():
                return None, text[:120]
            wait = 20 * (attempt + 1)
            print(f"    over quota; waiting {wait}s", file=sys.stderr, flush=True)
            time.sleep(wait)
            continue
        if verdict.error:
            return None, verdict.error[:120]
        # The tell that a fallback happened, set by the engine itself.
        if "local rules" in (verdict.model or ""):
            return None, "fell back to the local rules"
        return verdict, ""
    return None, "gave up waiting for quota"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, default=FIXTURES)
    parser.add_argument("--provider", default="gemini")
    parser.add_argument("--model", default="")
    parser.add_argument("--pause", type=float, default=4.0,
                        help="seconds between requests")
    parser.add_argument("--patience", type=int, default=6,
                        help="how many times to wait out a quota error")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--misses", action="store_true")
    args = parser.parse_args(argv)

    rows = json.loads(args.file.read_text())
    if args.limit:
        rows = rows[:args.limit]

    import providers
    model = args.model or providers.provider_class(args.provider).default_model
    engine = LLMEngine(
        provider=args.provider,
        api_key=CredentialStore().get_provider_key(args.provider),
        model=model, concurrency=1, batch_size=1,
        # The whole point: a fallback must not be mistaken for an answer.
        fallback_to_rules=False,
    )

    print(f"{args.file.name}: {len(rows)} messages   {args.provider} · {model}\n")
    scored = job_ok = cat_ok = 0
    skipped: list = []
    misses: list = []
    for index, row in enumerate(rows):
        verdict, trouble = ask(engine, as_message(row, index), args.patience)
        if verdict is None:
            skipped.append((row.get("subject", ""), trouble))
            continue
        scored += 1
        got_job = verdict.is_job_related
        got_cat = (verdict.category.value if got_job
                   else f"other/{verdict.other_category.value}")
        want_cat = (row["truth_cat"] if row["truth_job"]
                    else f"other/{row['truth_other']}")
        job_ok += got_job == row["truth_job"]
        cat_ok += got_cat == want_cat
        if got_cat != want_cat:
            misses.append((verdict.confidence_score, want_cat, got_cat,
                           row.get("subject", "")))
        time.sleep(args.pause)

    if not scored:
        print("Nothing was scored. Every request failed or fell back.",
              file=sys.stderr)
        for subject, why in skipped[:5]:
            print(f"    {subject[:44]:46} {why}", file=sys.stderr)
        return 1

    print(f"  scored              {scored}/{len(rows)}")
    print(f"  job vs not-job    {job_ok:4}/{scored}  {job_ok / scored * 100:6.1f}%")
    print(f"  exact category    {cat_ok:4}/{scored}  {cat_ok / scored * 100:6.1f}%")
    usage = engine.usage
    print(f"  tokens             {usage.input_tokens:,} in, "
          f"{usage.output_tokens:,} out")
    if skipped:
        print(f"\n  {len(skipped)} not scored, and not counted either:")
        for subject, why in skipped[:8]:
            print(f"    {subject[:44]:46} {why}")
    if args.misses and misses:
        print("\nevery disagreement:")
        for confidence, want, got, subject in sorted(misses):
            print(f"  {confidence:4.2f}  {want:22} -> {got:22} {subject[:44]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
