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
from typing import Dict, List, Optional, Sequence

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


class ReplyWorker(_BaseWorker):
    """Draft replies for messages the rules matched, and save them to Drafts.

    Kept apart from the scan on purpose. Drafting talks to the model again and
    writes to the mailbox, and neither of those should happen as a side effect
    of looking at what arrived.
    """

    finished_ok = Signal(object)
    task_name = "reply drafting"

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

        rules = [r for r in self.settings.rules if r.enabled and r.action != "none"]
        drafts: List[autoreply.Draft] = []
        if not rules:
            self._log("No reply rules are switched on.")
            self.finished_ok.emit(drafts)
            return

        wants_model = any(r.action == "draft_ai" for r in rules)
        if wants_model:
            self._classifier = LLMEngine(
                provider=self.settings.provider, api_key=self.api_key,
                model=self.settings.model, base_url=self.settings.base_url,
                effort=self.settings.effort,
                max_body_chars=self.settings.max_body_chars,
                fallback_to_rules=False, ruleset=self.settings.ruleset,
            )

        matched = []
        for item in self.items:
            rule, _why = autoreply.choose_rule(rules, item.email, item.classification)
            if rule is not None:
                matched.append((item, rule))
        if not matched:
            self._log("No message matched a reply rule.")
            self.finished_ok.emit(drafts)
            return

        signature = self.settings.reply_signature
        total = len(matched)
        for index, (item, rule) in enumerate(matched, start=1):
            if self.cancel_event.is_set():
                break
            self._emit_progress(index - 1, total,
                                f"Drafting {index} of {total}: {item.email.subject_display[:40]}")
            drafts.append(autoreply.draft_for(
                rule, item.email, item.classification, signature,
                self._classifier if rule.action == "draft_ai" else None,
            ))

        usable = [d for d in drafts if d.ok]
        saved = 0
        by_account: Dict[str, List] = {}
        for draft in usable:
            by_account.setdefault(draft.account_id, []).append(draft)

        passwords = self._passwords()
        for account_id, group in by_account.items():
            account = self.settings.account_by_id(account_id) or self.settings.primary_account
            engine = IMAPEngine(host=account.host, port=account.port)
            try:
                engine.connect(account.address, passwords.get(account.id, ""))
                target = engine.drafts_mailbox()
                for draft in group:
                    raw = autoreply.build_mime(
                        draft, account.address, account.label)
                    try:
                        engine.save_draft(raw, target)
                        saved += 1
                    except IMAPError as exc:
                        draft.error = str(exc)
            except Exception as exc:  # noqa: BLE001
                for draft in group:
                    draft.error = draft.error or str(exc)
                self._log(f"{account.label}: could not save drafts - {exc}")
            finally:
                try:
                    engine.logout()
                except Exception:  # pragma: no cover
                    pass

        failed = [d for d in drafts if not d.ok]
        self._emit_progress(total, total, "Drafting finished.")
        self._log(
            f"Drafted {len(usable)} repl{'y' if len(usable) == 1 else 'ies'}, "
            f"saved {saved} to Drafts"
            + (f", {len(failed)} could not be written" if failed else "")
            + ". Nothing has been sent."
        )
        self.finished_ok.emit(drafts)


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
