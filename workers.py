"""Background workers.

Every network call runs on a QThread so the UI never blocks. Workers own their
own :class:`~imap_engine.IMAPEngine`; the IMAP connection is deliberately closed
before classification starts, because iCloud drops idle IMAP sessions and a scan
of 200 messages can spend minutes in the model backend.
"""

from __future__ import annotations

import logging
import threading
import traceback
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import QThread, Signal

from config import Settings
import time

from imap_engine import IMAPEngine, IMAPError, MovePlan, MoveReport, ScanCancelled
from llm_engine import ClassificationCancelled, LLMEngine, LLMError
from models import Category, EmailMessage, FolderPlan, OtherCategory, TriageItem

log = logging.getLogger(__name__)


@dataclass
class ScanOutcome:
    """Everything a completed scan hands back to the window."""

    items: List[TriageItem] = field(default_factory=list)
    folder_plan: Optional[FolderPlan] = None
    created_folders: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    usage_text: str = ""
    scanned_count: int = 0
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None


class _BaseWorker(QThread):
    """Shared progress/logging plumbing and cancellation."""

    progress = Signal(int, int, str)
    #: Live counters while work is in flight: see :meth:`_emit_metrics`.
    metrics = Signal(dict)
    log_message = Signal(str)
    failed = Signal(str, str)

    #: Human-readable name used in the activity log when stopping.
    task_name = "task"

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.cancel_event = threading.Event()

    def cancel(self) -> None:
        """Ask the worker to stop at the next checkpoint."""
        self.cancel_event.set()
        self.requestInterruption()

    def stop(self, wait_ms: int = 4000) -> bool:
        """Cancel and wait. Returns ``True`` once the thread has finished.

        Every long operation polls ``cancel_event`` between network round
        trips, so this normally returns well inside ``wait_ms``.
        """
        if not self.isRunning():
            return True
        self.cancel()
        return bool(self.wait(wait_ms))

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def _emit_progress(self, done: int, total: int, message: str) -> None:
        if self.cancel_event.is_set():
            return          # a stopped worker must not keep driving the UI
        self.progress.emit(int(done), int(total), message)

    def _emit_metrics(self, **values) -> None:
        if not self.cancel_event.is_set():
            self.metrics.emit(dict(values))

    def _log(self, message: str) -> None:
        log.info(message)
        self.log_message.emit(message)

    #: Failures that are worth explaining rather than just reporting, keyed by
    #: something that appears in the message. The app knows what to do about
    #: each of these, and saying so is the difference between an error the user
    #: can act on and one they can only screenshot.
    ADVICE = (
        ("AUTHENTICATIONFAILED", "The server rejected the password. Most providers "
         "need an app password rather than the one you sign in with; Settings → "
         "Mailboxes has a link to generate one."),
        ("rejected those credentials", "Check the address, and that the password is "
         "an app password rather than your ordinary one."),
        ("Name or service not known", "That host name did not resolve. Check the "
         "IMAP host in Settings → Mailboxes."),
        ("nodename nor servname", "That host name did not resolve. Check the IMAP "
         "host in Settings → Mailboxes."),
        ("Connection refused", "Nothing is listening on that host and port. If this "
         "is Proton, its Bridge app has to be running."),
        ("timed out", "The server did not answer in time. This is usually the "
         "network rather than the app; try again, and lower Parallel connections "
         "in Settings → Mailboxes if it keeps happening."),
        ("certificate", "The TLS certificate did not verify. The app will not "
         "connect without a valid one, which is deliberate."),
        ("no Drafts mailbox", "Create a folder called Drafts in this account, then "
         "run the reply again."),
        ("API key", "Settings → Analysis holds the key, and has a link to the page "
         "that issues one."),
        ("quota", "The provider is rate limiting or out of credit. The offline "
         "sorter needs no key and no quota, and is one click away in the ⚙︎ menu."),
        ("Ollama", "Settings → Analysis can install and start Ollama for you when "
         "On this Mac is selected."),
    )

    def _advice_for(self, detail: str) -> str:
        lowered = detail.lower()
        for marker, advice in self.ADVICE:
            if marker.lower() in lowered:
                return advice
        return ""

    def _report_exception(self, title: str, exc: BaseException) -> None:
        log.error("%s: %s", title, exc, exc_info=True)
        detail = str(exc) or type(exc).__name__
        if not isinstance(exc, (IMAPError, LLMError, ValueError)):
            detail = f"{type(exc).__name__}: {detail}\n\n{traceback.format_exc(limit=4)}"
        advice = self._advice_for(detail)
        if advice:
            detail = f"{detail}\n\n{advice}"
        self.failed.emit(title, detail)


class ScanWorker(_BaseWorker):
    """Fetch a time window from iCloud, then classify it with the chosen backend."""

    finished_ok = Signal(object)
    task_name = "scan"

    def __init__(
        self,
        settings: Settings,
        mailbox_password,
        api_key: str,
        window_start: datetime,
        window_end: Optional[datetime],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        #: A password per account id, or one string for a single mailbox.
        self.mailbox_password = mailbox_password
        self.api_key = api_key
        self.window_start = window_start
        self.window_end = window_end
        self._classifier: Optional[LLMEngine] = None

    def cancel(self) -> None:
        """Stop, and drop the HTTP connections so in-flight calls fail fast."""
        super().cancel()
        classifier = self._classifier
        if classifier is not None:
            classifier.close()

    def switch_model(self, provider: str, model: str, api_key: str = "",
                     base_url: str = "", ruleset: str = "") -> bool:
        """Point a scan that is already running at a different backend.

        A scan spends its first seconds in IMAP, before the classifier exists.
        Updating the settings covers that window; swapping covers the rest.
        """
        self.settings = replace(
            self.settings, provider=provider, model=model,
            base_url=base_url or self.settings.base_url,
            ruleset=ruleset or self.settings.ruleset,
        )
        self.api_key = api_key
        classifier = self._classifier
        if classifier is None:
            self._log(f"Backend set to {provider} · {model}; this scan will use it.")
            return True
        try:
            classifier.swap_provider(
                provider=provider, api_key=api_key, model=model,
                base_url=base_url, ruleset=ruleset,
            )
        except Exception as exc:  # noqa: BLE001 - never take the scan down
            self._log(f"Could not switch backend mid-scan: {exc}")
            return False
        self._log(f"Backend switched to {provider} · {model} for the remaining batches.")
        return True

    def switch_ruleset(self, name: str) -> bool:
        self.settings = replace(self.settings, ruleset=name)
        classifier = self._classifier
        if classifier is not None:
            classifier.set_ruleset(name)
        return True

    def _passwords(self) -> Dict[str, str]:
        """Password per account id, accepting a bare string for one mailbox."""
        if isinstance(self.mailbox_password, dict):
            return dict(self.mailbox_password)
        primary = self.settings.scan_accounts
        return {primary[0].id: self.mailbox_password} if primary else {}

    def run(self) -> None:  # noqa: C901 - a linear pipeline reads better whole
        outcome = ScanOutcome(window_start=self.window_start, window_end=self.window_end)
        targets = self.settings.scan_accounts
        passwords = self._passwords()
        messages: List[EmailMessage] = []
        plan = None
        warnings_seen: List[str] = []
        # Progress runs across every mailbox rather than restarting at each
        # one, so the bar means the same thing whether one is selected or six.
        fetched_before = 0
        expected_total = 0

        try:
            for index, account in enumerate(targets, start=1):
                if self.cancel_event.is_set():
                    raise ScanCancelled("Cancelled.")
                where = f" ({index} of {len(targets)})" if len(targets) > 1 else ""
                engine = IMAPEngine(host=account.host, port=account.port)
                try:
                    # ---- 1. Mail ----------------------------------------
                    self._emit_progress(0, 100, f"Connecting to {account.label}…{where}")
                    engine.connect(account.address, passwords.get(account.id, ""))
                    self._log(
                        f"Connected to {account.host} as {account.address} "
                        f"(hierarchy delimiter \u201c{engine.delimiter}\u201d)."
                    )

                    account_plan = engine.folder_plan(
                        self.settings.folder_root, self.settings.other_folder_root
                    )
                    account_plan = replace(
                        account_plan,
                        detailed_job_folders=self.settings.profile.detailed_job_folders,
                        topics=self.settings.chosen_topics,
                    )
                    if plan is None:
                        plan = account_plan
                        outcome.folder_plan = account_plan

                    created = engine.ensure_folders(
                        account_plan, subscribe=self.settings.subscribe_new_folders)
                    outcome.created_folders.extend(created)
                    if created:
                        self._log(f"{account.label}: created " + ", ".join(created))
                    else:
                        self._log(
                            f"{account.label}: all \u201c{account_plan.root}\u201d "
                            "folders already exist."
                        )

                    if not engine.has_capability("UIDPLUS"):
                        warnings_seen.append(
                            f"{account.label} does not advertise UIDPLUS, so applying "
                            "moves will use a full EXPUNGE of the source mailbox."
                        )

                    self._emit_progress(fetched_before, max(fetched_before * 2, 1),
                                        f"Searching {account.label}…")
                    fetch_started = time.monotonic()
                    offset = fetched_before
                    remaining = len(targets) - index

                    def fetch_report(done: int, total: int, text: str) -> None:
                        # Mailboxes still to come are unknown until they are
                        # opened, so they are assumed to hold about as much as
                        # this one. The bar creeps rather than jumping back.
                        # Fetching is the first half of the scan and sorting the
                        # second, so this reports against twice the message
                        # count and stops at the midpoint.
                        overall = offset + total + int(remaining * total * 0.9)
                        seen = offset + done
                        self._emit_progress(seen, max(overall * 2, seen * 2, 1), text)
                        elapsed = max(1e-6, time.monotonic() - fetch_started)
                        self._emit_metrics(
                            phase="fetch", done=seen, total=max(overall, seen, 1),
                            elapsed=elapsed,
                            rate=seen / elapsed,
                            eta=(overall - seen) / (seen / elapsed) if seen else 0.0,
                        )

                    scan = engine.fetch_window(
                        start=self.window_start,
                        end=self.window_end,
                        mailbox=account.source_mailbox,
                        max_messages=self.settings.max_messages,
                        progress=fetch_report,
                        cancel=self.cancel_event,
                        connections=account.connections,
                        max_bytes=self.settings.fetch_bytes,
                    )
                    warnings_seen.extend(scan.warnings)
                    # Tag every message so the table can say where it came from
                    # and the move phase knows which server to talk to.
                    for message in scan.messages:
                        message.account_id = account.id
                        message.account_label = account.label
                        message.account_address = account.address
                    messages.extend(scan.messages)
                    fetched_before += len(scan.messages)
                    expected_total = fetched_before
                    self._log(
                        f"{account.label}: fetched {len(scan.messages)} message(s) from "
                        f"{account.source_mailbox} ({len(scan.candidate_uids)} matched "
                        "the server-side date search)."
                    )
                finally:
                    try:
                        engine.logout()
                    except Exception:  # pragma: no cover
                        pass

            if self.cancel_event.is_set():
                raise ScanCancelled("Cancelled.")
        except ScanCancelled:
            self._log("Scan cancelled.")
            self.finished_ok.emit(outcome)
            return
        except Exception as exc:  # noqa: BLE001
            self._report_exception("Could not read your mailbox", exc)
            return

        outcome.warnings.extend(warnings_seen)
        outcome.scanned_count = len(messages)
        if plan is not None:
            outcome.folder_plan = plan

        if not messages:
            self._emit_progress(1, 1, "No messages found in this window.")
            self.finished_ok.emit(outcome)
            return

        # ---- 2. Classification ------------------------------------------
        classifier = LLMEngine(
            provider=self.settings.provider,
            api_key=self.api_key,
            model=self.settings.model,
            base_url=self.settings.base_url,
            effort=self.settings.effort,
            max_body_chars=self.settings.max_body_chars,
            concurrency=self.settings.concurrency,
            fallback_to_rules=self.settings.fallback_to_rules,
            batch_size=self.settings.batch_size,
            ruleset=self.settings.ruleset,
        )
        self._classifier = classifier
        try:
            if self.cancel_event.is_set():
                raise ClassificationCancelled("Cancelled.")
            self._emit_progress(
                len(messages), len(messages) * 2,
                f"Analyzing with {self.settings.provider_label}…",
            )
            started = time.monotonic()
            tally = {"job": 0, "file": 0, "review": 0, "failed": 0}

            def observe(batch) -> None:
                for classification in batch:
                    if classification.error:
                        tally["failed"] += 1
                    if classification.is_job_related:
                        tally["job"] += 1
                    if (classification.confidence_score >= self.settings.confidence_threshold
                            and classification.is_job_related
                            and classification.category is not Category.UNCLASSIFIED_OTHER
                            and not classification.error):
                        tally["file"] += 1
                    else:
                        tally["review"] += 1

            fetched = len(messages)

            def report(done: int, total: int, text: str) -> None:
                # Continuing the fetch phase's scale rather than starting a new
                # one: a bar that fills, empties and fills again reads as the
                # scan having restarted.
                self._emit_progress(fetched + done, fetched + max(total, done), text)
                elapsed = max(1e-6, time.monotonic() - started)
                rate = done / elapsed
                self._emit_metrics(
                    phase="analyze",
                    done=done, total=total,
                    job_related=tally["job"], to_file=tally["file"],
                    needs_review=tally["review"], failed=tally["failed"],
                    requests=classifier.usage.requests,
                    batched=classifier.batched_requests,
                    input_tokens=classifier.usage.input_tokens,
                    output_tokens=classifier.usage.output_tokens,
                    cost=classifier.usage.estimated_cost_usd,
                    on_device=classifier.usage.on_device,
                    fallbacks=classifier.fallback_count,
                    elapsed=elapsed,
                    rate=rate,
                    eta=(total - done) / rate if rate > 0 and done else 0.0,
                    model=f"{self.settings.provider_label} · {classifier.model}",
                )

            classifications = classifier.classify_many(
                messages,
                progress=report,
                cancel=self.cancel_event,
                observer=observe,
            )
            outcome.usage_text = classifier.usage.describe()
            for note in classifier.degradations:
                outcome.warnings.append(f"{self.settings.provider_label} request adjusted: {note}")
            if classifier.fallback_count:
                outcome.warnings.append(
                    f"{classifier.fallback_count} message(s) could not reach "
                    f"{self.settings.provider_label} and were classified by the local "
                    "rules engine instead. Those rows say so in their reasoning."
                )
        except ClassificationCancelled:
            self._log("Analysis cancelled.")
            self.finished_ok.emit(outcome)
            return
        except Exception as exc:  # noqa: BLE001
            self._report_exception("Could not analyze your messages", exc)
            return
        finally:
            # Always release the connection pool; a scan can be minutes apart.
            classifier.close()
            self._classifier = None

        # ---- 3. Routing --------------------------------------------------
        routing = self.settings.routing
        outcome.items = [
            TriageItem(
                email=message,
                classification=classification,
                folders=plan,
                threshold=self.settings.confidence_threshold,
                non_job_routing=routing,
                auto_approve_non_job=self.settings.auto_approve_non_job,
            )
            for message, classification in zip(messages, classifications)
        ]
        self._log(f"Analysis complete. {outcome.usage_text}")
        self.finished_ok.emit(outcome)


class ApplyWorker(_BaseWorker):
    """Create any missing target folders, then move the approved messages."""

    finished_ok = Signal(object)
    task_name = "folder move"

    def __init__(
        self,
        settings: Settings,
        mailbox_password,
        plans: Sequence[MovePlan],
        extra_folders: Sequence[str] = (),
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.mailbox_password = mailbox_password
        self.plans = list(plans)
        self.extra_folders = list(extra_folders)

    def _passwords(self) -> Dict[str, str]:
        if isinstance(self.mailbox_password, dict):
            return dict(self.mailbox_password)
        targets = self.settings.scan_accounts
        return {targets[0].id: self.mailbox_password} if targets else {}

    def _grouped(self) -> List[tuple]:
        """Moves bundled by mailbox and by the folder they start in.

        Two levels because each mailbox is a separate server, and because a
        move can only name one source folder at a time. Filing starts from the
        inbox for everything; undoing starts from wherever each message was
        filed to.
        """
        default = self.settings.primary_account
        buckets: Dict[tuple, List[MovePlan]] = {}
        for plan in self.plans:
            account = self.settings.account_by_id(plan.account_id) or default
            source = plan.source_folder or account.source_mailbox
            buckets.setdefault((account.id, source), []).append(plan)
        out = []
        for (account_id, source), plans in buckets.items():
            account = self.settings.account_by_id(account_id) or default
            out.append((account, source, plans))
        return out

    def run(self) -> None:
        passwords = self._passwords()
        groups = self._grouped()
        combined = MoveReport()
        done_so_far = 0
        total = len(self.plans) or 1

        for account, source, plans in groups:
            engine = IMAPEngine(host=account.host, port=account.port)
            try:
                self._emit_progress(done_so_far, total, f"Connecting to {account.label}…")
                engine.connect(account.address, passwords.get(account.id, ""))

                plan = engine.folder_plan(
                    self.settings.folder_root, self.settings.other_folder_root)
                plan = replace(
                    plan,
                    detailed_job_folders=self.settings.profile.detailed_job_folders,
                    topics=self.settings.chosen_topics,
                )
                wanted = list(plan.all_folders) + list(self.extra_folders)
                created = engine.ensure_folder_paths(
                    wanted, subscribe=self.settings.subscribe_new_folders
                )
                if created:
                    self._log(f"{account.label}: created " + ", ".join(created))

                apply_started = time.monotonic()
                offset = done_so_far

                def apply_report(done: int, _total: int, text: str) -> None:
                    self._emit_progress(offset + done, total, text)
                    elapsed = max(1e-6, time.monotonic() - apply_started)
                    self._emit_metrics(
                        phase="apply", done=offset + done, total=total, elapsed=elapsed,
                        rate=(offset + done) / elapsed,
                        eta=((total - offset - done) / ((offset + done) / elapsed)
                             if offset + done else 0.0),
                    )

                report = engine.move_messages(
                    plans,
                    mailbox=source,
                    progress=apply_report,
                    cancel=self.cancel_event,
                )
                combined.moved.update(report.moved)
                combined.failed.update(report.failed)
                combined.warnings.extend(report.warnings)
                combined.created_folders = list(combined.created_folders) + list(created)
                combined.expunged = combined.expunged or report.expunged
                done_so_far += len(plans)
            except ScanCancelled:
                self._log("Apply cancelled before completion.")
                combined.warnings.append("Cancelled before all moves completed.")
                self.finished_ok.emit(combined)
                return
            except Exception as exc:  # noqa: BLE001
                self._report_exception(f"Could not file your messages in {account.label}", exc)
                return
            finally:
                try:
                    engine.logout()
                except Exception:  # pragma: no cover
                    pass

        self._log(f"Apply finished: {combined.describe()}")
        self.finished_ok.emit(combined)


@dataclass
class ReplyRun:
    """What one pass of the reply rules did.

    Kept as a record rather than a list of drafts because a rule can now file,
    tick and flag as well as draft, and the window needs to know about all of
    it to show what changed.
    """

    outcomes: List[Tuple[TriageItem, object]] = field(default_factory=list)
    saved: int = 0
    marked_read: int = 0
    flagged: int = 0

    def add(self, item: TriageItem, outcome) -> None:
        self.outcomes.append((item, outcome))

    @property
    def drafts(self) -> List:
        return [o.draft for _i, o in self.outcomes if o.draft is not None]

    @property
    def matched(self) -> int:
        return len(self.outcomes)

    def describe(self) -> str:
        drafts = self.drafts
        failed = [d for d in drafts if not d.ok]
        parts = [f"{self.matched} message{'' if self.matched == 1 else 's'} "
                 f"matched a reply rule"]
        if drafts:
            written = len(drafts) - len(failed)
            parts.append(f"drafted {written} repl{'y' if written == 1 else 'ies'}"
                         f", saved {self.saved} to Drafts")
        if self.marked_read:
            parts.append(f"marked {self.marked_read} as read")
        if self.flagged:
            parts.append(f"flagged {self.flagged}")
        filed = sum(1 for _i, o in self.outcomes if o.file_into)
        if filed:
            parts.append(f"pointed {filed} at a different folder")
        if failed:
            parts.append(f"{len(failed)} could not be written")
        return ", ".join(parts) + ". Nothing has been sent."


class ReplyWorker(_BaseWorker):
    """Run the reply rules over the scanned messages and carry out what they say.

    Kept apart from the scan on purpose. A rule can talk to the model again and
    write to the mailbox, and neither of those should happen as a side effect of
    looking at what arrived.

    Nothing is ever sent. A reply lands in Drafts, and a person presses send.
    """

    finished_ok = Signal(object)
    task_name = "reply rules"

    def __init__(self, settings: Settings, mailbox_password, api_key: str,
                 items: Sequence[TriageItem], parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.mailbox_password = mailbox_password
        self.api_key = api_key
        self.items = list(items)
        self._classifier: Optional[LLMEngine] = None

    def cancel(self) -> None:
        super().cancel()
        if self._classifier is not None:
            self._classifier.close()

    def _passwords(self) -> Dict[str, str]:
        if isinstance(self.mailbox_password, dict):
            return dict(self.mailbox_password)
        targets = self.settings.scan_accounts
        return {targets[0].id: self.mailbox_password} if targets else {}

    def run(self) -> None:
        import autoreply

        rules = [r for r in self.settings.rules if r.enabled and r.actions]
        result = ReplyRun()
        if not rules:
            self._log("No reply rules are switched on.")
            self.finished_ok.emit(result)
            return

        if any(r.uses_the_model for r in rules):
            self._classifier = LLMEngine(
                provider=self.settings.provider, api_key=self.api_key,
                model=self.settings.model, base_url=self.settings.base_url,
                effort=self.settings.effort,
                max_body_chars=self.settings.max_body_chars,
                fallback_to_rules=False, ruleset=self.settings.ruleset,
            )

        signature = self.settings.reply_signature
        total = len(self.items)
        for index, item in enumerate(self.items, start=1):
            if self.cancel_event.is_set():
                break
            self._emit_progress(
                index - 1, total,
                f"Checking {index} of {total}: {item.email.subject_display[:40]}")
            try:
                outcome = autoreply.apply_rules(
                    rules, item.email, item.classification, signature,
                    self._classifier)
            except Exception as exc:  # noqa: BLE001 - one bad rule, not a crash
                self._log(f"A rule failed on “{item.email.subject_display[:40]}”: {exc}")
                continue
            if outcome is not None:
                result.add(item, outcome)

        self._emit_progress(total, total, "Carrying out what the rules said.")
        if not result.outcomes:
            self._log("No message matched a reply rule.")
            self.finished_ok.emit(result)
            return

        self._touch_mailboxes(result)

        self._emit_progress(total, total, "Reply rules finished.")
        self._log(result.describe())
        self.finished_ok.emit(result)

    # -- the part that talks to the mailbox --------------------------------
    def _touch_mailboxes(self, result: "ReplyRun") -> None:
        """Save drafts and set flags, one connection per account."""
        by_account: Dict[str, List[Tuple[TriageItem, object]]] = {}
        for item, outcome in result.outcomes:
            if outcome.changes_the_mailbox:
                by_account.setdefault(item.email.account_id, []).append((item, outcome))
        if not by_account:
            return

        passwords = self._passwords()
        for account_id, group in by_account.items():
            if self.cancel_event.is_set():
                break
            account = (self.settings.account_by_id(account_id)
                       or self.settings.primary_account)
            engine = IMAPEngine(host=account.host, port=account.port)
            try:
                engine.connect(account.address, passwords.get(account.id, ""))
                self._save_drafts(engine, account, group, result)
                self._set_flags(engine, group, result)
            except Exception as exc:  # noqa: BLE001
                for _item, outcome in group:
                    if outcome.draft is not None:
                        outcome.draft.error = outcome.draft.error or str(exc)
                self._log(f"{account.label}: could not carry out the rules - {exc}")
            finally:
                try:
                    engine.logout()
                except Exception:  # pragma: no cover
                    pass

    def _save_drafts(self, engine, account, group, result: "ReplyRun") -> None:
        import autoreply

        wanted = [o for _i, o in group if o.draft is not None and o.draft.ok]
        if not wanted:
            return
        target = engine.drafts_mailbox()
        for outcome in wanted:
            if self.cancel_event.is_set():
                return
            raw = autoreply.build_mime(outcome.draft, account.address, account.label)
            try:
                engine.save_draft(raw, target)
                result.saved += 1
            except IMAPError as exc:
                outcome.draft.error = str(exc)

    def _set_flags(self, engine, group, result: "ReplyRun") -> None:
        """One STORE per flag per source folder, rather than one per message."""
        wanted: Dict[Tuple[str, str], List[str]] = {}
        for item, outcome in group:
            folder = item.email.source_folder or "INBOX"
            if outcome.mark_read:
                wanted.setdefault((folder, "seen"), []).append(item.email.uid)
            if outcome.flag:
                wanted.setdefault((folder, "flagged"), []).append(item.email.uid)
        for (folder, flag), uids in wanted.items():
            if self.cancel_event.is_set():
                return
            try:
                touched = engine.set_flags(uids, [flag], add=True, mailbox=folder)
            except IMAPError as exc:
                self._log(f"Could not set {flag} on {len(uids)} message"
                          f"{'' if len(uids) == 1 else 's'}: {exc}")
                continue
            if flag == "seen":
                result.marked_read += touched
            else:
                result.flagged += touched


@dataclass
class OnDeviceResult:
    """What one install, start or download step did."""

    step: str = ""
    ok: bool = False
    cancelled: bool = False
    lines: List[str] = field(default_factory=list)

    @property
    def tail(self) -> str:
        """The last line that says anything, for a one-line summary."""
        for line in reversed(self.lines):
            if line.strip():
                return line.strip()
        return ""

    def describe(self) -> str:
        if self.cancelled:
            return "Stopped. Nothing was left half-installed on purpose, but " \
                   "check with “Test” before relying on it."
        if self.ok:
            return {"install": "Ollama is installed.",
                    "pull": "The model is downloaded and ready.",
                    "start": "Ollama is running."}.get(self.step, "Done.")
        return f"That did not work. {self.tail[:160]}"


class OnDeviceProbeWorker(_BaseWorker):
    """Asks the local model server what it has, off the UI thread.

    Two seconds does not sound like a freeze until it happens every time
    somebody opens a menu. An endpoint that drops packets rather than refusing
    them costs the full timeout, and the endpoint is a field the user can type
    anything into.
    """

    finished_ok = Signal(object)
    task_name = "on-device probe"

    def __init__(self, endpoint: str = "", parent=None) -> None:
        super().__init__(parent)
        self.endpoint = endpoint

    def run(self) -> None:
        import ondevice

        try:
            state = ondevice.status(self.endpoint or ondevice.DEFAULT_ENDPOINT)
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            state = ondevice.Status(error=str(exc))
        self.finished_ok.emit(state)


class OnDeviceWorker(_BaseWorker):
    """Installs Ollama, or downloads a model, without freezing the window.

    This used to be a blocking subprocess call on the UI thread. A Homebrew
    install takes minutes, so the app beachballed for the whole of it with no
    output and no way to stop - indistinguishable, from the outside, from a
    crash.
    """

    finished_ok = Signal(object)
    task_name = "on-device setup"

    def __init__(self, step: str, command: Sequence[str], parent=None) -> None:
        super().__init__(parent)
        self.step = step
        self.command = list(command)

    def run(self) -> None:
        import ondevice

        result = OnDeviceResult(step=self.step)
        seen_percent = 0.0
        started = time.monotonic()

        def line(text: str) -> None:
            nonlocal seen_percent
            result.lines.append(text)
            # Only the last few hundred, so a chatty install cannot grow
            # without bound on a long download.
            if len(result.lines) > 500:
                del result.lines[:200]
            percent, label = ondevice.parse_progress(text)
            if percent is None:
                # Nothing to go on, so keep the bar where it is and say what
                # is happening. A bar that jumps back to zero on every log
                # line is worse than one that waits.
                self._emit_progress(int(seen_percent), 100, label or text[:80])
                return
            # Never let the bar go backwards: a pull reports each layer from
            # zero, and a bar that restarts four times reads as four failures.
            seen_percent = max(seen_percent, percent)
            self._emit_progress(int(seen_percent), 100, label or "Working")

        self._log(f"Running: {' '.join(self.command)}")
        self._emit_progress(0, 100, "Starting…")
        ok = ondevice.stream(self.command, line, cancel=self.cancel_event)
        result.cancelled = self.cancel_event.is_set()
        result.ok = ok and not result.cancelled
        if result.ok:
            self._emit_progress(100, 100, "Finished.")
        took = time.monotonic() - started
        self._log(f"{result.describe()} ({took:.0f}s)")
        for text in result.lines[-4:]:
            self._log(f"    {text}")
        self.finished_ok.emit(result)


class ConnectionTestWorker(_BaseWorker):
    """Runs the Settings dialog's two “Test” buttons off the UI thread."""

    finished_ok = Signal(object)
    task_name = "connection test"

    def __init__(
        self,
        mode: str,
        settings: Settings,
        mailbox_password: str = "",
        api_key: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.mode = mode  # "imap" | "claude"
        self.settings = settings
        #: A password per account id, or one string for a single mailbox.
        self.mailbox_password = mailbox_password
        self.api_key = api_key

    def run(self) -> None:
        try:
            if self.mode == "imap":
                engine = IMAPEngine(host=self.settings.imap_host, port=self.settings.imap_port)
                result = engine.probe(self.settings.icloud_email, self.mailbox_password)
                result["mode"] = "imap"
            else:
                classifier = LLMEngine(
                    provider=self.settings.provider,
                    api_key=self.api_key,
                    model=self.settings.model,
                    base_url=self.settings.base_url,
                    effort=self.settings.effort,
                )
                result = classifier.test_connection()
                result["mode"] = "claude"
            self.finished_ok.emit(result)
        except Exception as exc:  # noqa: BLE001
            title = ("iCloud connection failed" if self.mode == "imap"
                     else f"{self.settings.provider_label} connection failed")
            self._report_exception(title, exc)


def build_move_plans(items: Sequence[TriageItem]) -> List[MovePlan]:
    """Approved, movable rows -> IMAP move plans."""
    return [
        MovePlan(uid=item.email.uid, target_folder=item.target_folder,
                 subject=item.email.subject_display, account_id=item.email.account_id)
        for item in items
        if item.approved and item.is_actionable and item.target_folder
    ]


def required_folders(items: Sequence[TriageItem], plan: Optional[FolderPlan]) -> List[str]:
    """Folders the approved set needs beyond the standard Job Search tree.

    Only the mailboxes actually used are returned, so enabling topic filing does
    not scatter a dozen empty folders through the account.
    """
    if plan is None:
        return []
    standard = {folder.lower() for folder in plan.all_folders}
    others: List[OtherCategory] = []
    manual: List[str] = []
    for item in items:
        if not (item.approved and item.is_actionable):
            continue
        target = item.target_folder
        if not target or target.lower() in standard:
            continue
        if item.override_folder:
            manual.append(target)
        elif not item.classification.is_job_related:
            others.append(item.classification.other_category)
    needed = list(plan.other_folders(dict.fromkeys(others)))
    for folder in manual:
        if folder not in needed:
            needed.append(folder)
    return needed
