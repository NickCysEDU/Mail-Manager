#!/usr/bin/env python3
"""Run the triage pipeline in the terminal, with no GUI.

The fastest way to see what the app would decide, and the fastest way to debug
a classification you disagree with.

    ./dev scan                     # real mailbox, past 24h, nothing is moved
    ./dev scan --hours 72          # a wider window
    ./dev scan --fake              # bundled samples, no mailbox and no API
    ./dev scan --limit 5 --json    # machine-readable
    ./dev scan --uid 1001 --prompt # show the exact prompt sent for one message

Folder moves are never performed here - this is a read-only view of the
decision. Use the app to file anything.
"""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import CredentialStore, Settings  # noqa: E402
from imap_engine import IMAPEngine  # noqa: E402
from llm_engine import LLMAuthError, LLMEngine  # noqa: E402
from models import Disposition, TriageItem  # noqa: E402

RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
GREEN, AMBER, RED, GREY, BLUE = (
    "\033[32m", "\033[33m", "\033[31m", "\033[90m", "\033[34m",
)


def colour(text: str, code: str, enabled: bool) -> str:
    return f"{code}{text}{RESET}" if enabled else text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devscan", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--hours", type=float, default=24.0,
                        help="How far back to scan (default: 24).")
    parser.add_argument("--limit", type=int, default=0,
                        help="Stop after N messages (0 = the settings cap).")
    parser.add_argument("--fake", action="store_true",
                        help="Use bundled sample mail; no mailbox, no API calls, no cost.")
    parser.add_argument("--mailbox", default=None, help="Mailbox to scan (default: INBOX).")
    parser.add_argument("--model", default=None, help="Override the configured model.")
    parser.add_argument("--provider", default=None,
                        help="Override the configured backend "
                             "(anthropic, gemini, openai, ollama).")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    parser.add_argument("--full", action="store_true",
                        help="Print the full summary and reasoning for every message.")
    parser.add_argument("--uid", action="append", default=[],
                        help="Only this UID. Repeatable.")
    parser.add_argument("--prompt", action="store_true",
                        help="Print the exact prompt sent to the model, then exit.")
    parser.add_argument("--no-colour", action="store_true", help="Disable ANSI colour.")
    return parser


def fetch_real(settings: Settings, args, log) -> list:
    store = CredentialStore()
    password = store.get_icloud_password(settings.icloud_email)
    if not settings.icloud_email or not password:
        log("No iCloud credentials stored. Run:  ./dev creds", error=True)
        raise SystemExit(2)

    start = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    engine = IMAPEngine(host=settings.imap_host, port=settings.imap_port)
    log(f"Connecting to {settings.imap_host} as {settings.icloud_email}…")
    engine.connect(settings.icloud_email, password)
    try:
        log(f"Hierarchy delimiter “{engine.delimiter}”, UIDPLUS "
            f"{'yes' if engine.has_capability('UIDPLUS') else 'no'}.")
        result = engine.fetch_window(
            start=start,
            mailbox=args.mailbox or settings.source_mailbox,
            max_messages=args.limit or settings.max_messages,
            progress=lambda done, total, message: log(f"  {message}", dim=True),
        )
        for warning in result.warnings:
            log(f"  warning: {warning}")
        return result.messages
    finally:
        engine.logout()


def classify(messages, settings: Settings, args, log):
    if args.fake:
        import demo_data
        from models import Classification

        log("Using scripted verdicts (no API calls).", dim=True)
        verdicts = []
        for message in messages:
            verdict = demo_data.verdict_for_prompt(message.subject.lower())
            verdicts.append(
                Classification.from_payload(verdict, model="scripted")
                if verdict
                else Classification.failure("No scripted verdict for this message.")
            )
        return verdicts, None

    store = CredentialStore()
    api_key = store.get_provider_key(settings.provider)
    if settings.needs_api_key and not api_key:
        log(f"No {settings.provider_label} API key stored. Run:  ./dev creds   "
            "(or use --fake)", error=True)
        raise SystemExit(2)

    engine = LLMEngine(
        provider=settings.provider,
        api_key=api_key,
        model=args.model or settings.model,
        base_url=settings.base_url,
        effort=settings.effort,
        max_body_chars=settings.max_body_chars,
        concurrency=settings.concurrency,
    )
    log(f"Analyzing {len(messages)} message(s) with {engine.provider.describe()}…")
    started = time.monotonic()
    try:
        results = engine.classify_many(
            messages, progress=lambda done, total, msg: log(f"  {msg}", dim=True)
        )
    except LLMAuthError as exc:
        log(f"\n{exc}", error=True)
        log(f"Fix it with:  ./dev creds        (backend: {settings.provider})", error=True)
        log("Or try a backend that needs no key:  ./dev scan --provider rules", error=True)
        raise SystemExit(2) from None
    finally:
        engine.close()
    log(f"Done in {time.monotonic() - started:.1f}s - {engine.usage.describe()}", dim=True)
    for note in engine.degradations:
        log(f"  note: {note}")
    return results, engine


def render_table(items, use_colour: bool, full: bool) -> None:
    marks = {
        Disposition.MOVE: colour("MOVE  ", GREEN, use_colour),
        Disposition.REVIEW: colour("REVIEW", AMBER, use_colour),
        Disposition.LEAVE: colour("LEAVE ", GREY, use_colour),
    }
    print()
    for item in items:
        classification = item.classification
        confidence = classification.confidence_percent
        bar_colour = GREEN if item.is_high_confidence else (AMBER if confidence >= 70 else RED)
        header = (
            f"{marks[item.disposition]}  "
            f"{colour(f'{confidence:5.1f}%', bar_colour, use_colour)}  "
            f"{colour(classification.category_label, BOLD, use_colour)}"
        )
        print(header)
        print(f"        {colour(item.email.sender_short, DIM, use_colour)}  ·  "
              f"{item.email.subject_display}")
        print(f"        {colour('→ ' + item.folder_display, DIM, use_colour)}")
        body = classification.summary if not full else (
            f"{classification.summary}\n\nReasoning: {classification.reasoning}"
        )
        for line in textwrap.wrap(body, width=92) if not full else body.splitlines():
            for wrapped in textwrap.wrap(line, width=92) or [""]:
                print(f"        {colour(wrapped, DIM, use_colour)}")
        if classification.adjustments:
            for adjustment in classification.adjustments:
                print(f"        {colour('! ' + adjustment, AMBER, use_colour)}")
        if classification.error:
            print(f"        {colour('! ' + classification.error, RED, use_colour)}")
        print()


def render_totals(items, use_colour: bool) -> None:
    from models import TriageSummary

    summary = TriageSummary.build(items)
    print(colour(summary.describe(), BOLD, use_colour))
    by_folder = {}
    for item in items:
        by_folder[item.folder_display] = by_folder.get(item.folder_display, 0) + 1
    for folder, count in sorted(by_folder.items(), key=lambda pair: -pair[1]):
        print(f"  {count:>3}  {folder}")


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    use_colour = sys.stdout.isatty() and not args.no_colour

    def log(message: str, dim: bool = False, error: bool = False) -> None:
        if args.json and not error:
            return
        stream = sys.stderr if error else sys.stderr
        code = RED if error else (DIM if dim else "")
        print(colour(message, code, use_colour) if code else message, file=stream)

    settings = Settings.load()
    if args.provider:
        settings.provider = args.provider
        settings = settings.normalized()
        if args.model:
            settings.model = args.model

    if args.fake:
        import demo_data
        messages = demo_data.demo_emails()
        log(f"Loaded {len(messages)} bundled sample message(s).")
    else:
        messages = fetch_real(settings, args, log)
        log(f"Fetched {len(messages)} message(s).")

    if args.uid:
        wanted = set(args.uid)
        messages = [m for m in messages if m.uid in wanted]
        if not messages:
            log(f"No message matched UID(s) {', '.join(args.uid)}.", error=True)
            return 1
    if args.limit:
        messages = messages[: args.limit]

    if args.prompt:
        engine = LLMEngine(
            api_key="preview-only",
            model=args.model or settings.model,
            max_body_chars=settings.max_body_chars,
        )
        for message in messages:
            print(f"===== UID {message.uid} - {message.subject_display} =====")
            print(engine.build_prompt(message))
            print()
        return 0

    if not messages:
        log("Nothing to analyze.")
        return 0

    classifications, _ = classify(messages, settings, args, log)
    plan = __import__("models").FolderPlan(
        root=settings.folder_root, other_root=settings.other_folder_root
    )
    items = [
        TriageItem(
            email=message, classification=classification, folders=plan,
            threshold=settings.confidence_threshold,
            non_job_routing=settings.routing,
            auto_approve_non_job=settings.auto_approve_non_job,
        )
        for message, classification in zip(messages, classifications)
    ]

    if args.json:
        from gui import _export_row  # reuse the app's export shape

        print(json.dumps([_export_row(item) for item in items], indent=2))
        return 0

    render_table(items, use_colour, args.full)
    render_totals(items, use_colour)
    print()
    print("Nothing was moved - devscan is read-only. Use the app to file messages.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
