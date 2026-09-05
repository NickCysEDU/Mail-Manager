"""Unattended scanning on a timer.

Two ways to run it:

* **In the app.** A Qt timer fires a scan on the chosen interval while the
  window is open. Nothing is filed without permission unless auto-file is on.
* **As a background agent.** ``mailmanager-agent`` runs headless under
  ``launchd``, so scans continue when the app is closed and survive a reboot.
  The agent writes a small JSON status file the app reads on launch.

The agent only ever files what the routing rules would have pre-ticked: high
confidence, job related, and never anything the rules send to Needs Review.
That is the same bar the checkbox column uses, so unattended filing can never
do something the app would not have offered to do.
"""

from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

LAUNCH_AGENT_LABEL = "com.mailmanager.icloudjobtriage.agent"

#: Intervals offered in the menu, in minutes.
INTERVALS: Tuple[Tuple[int, str], ...] = (
    (0, "Off"),
    (15, "Every 15 minutes"),
    (30, "Every 30 minutes"),
    (60, "Every hour"),
    (180, "Every 3 hours"),
    (360, "Every 6 hours"),
    (720, "Every 12 hours"),
    (1440, "Once a day"),
)


def interval_label(minutes: int) -> str:
    for value, label in INTERVALS:
        if value == minutes:
            return label
    return f"Every {minutes} minutes"


# --------------------------------------------------------------------------
# Status file, shared between the app and the agent
# --------------------------------------------------------------------------
@dataclass
class RunRecord:
    """What the last unattended scan did."""

    started: str = ""
    finished: str = ""
    scanned: int = 0
    filed: int = 0
    review: int = 0
    error: str = ""
    backend: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "started": self.started, "finished": self.finished,
            "scanned": self.scanned, "filed": self.filed, "review": self.review,
            "error": self.error, "backend": self.backend,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "RunRecord":
        return cls(
            started=str(data.get("started", "")), finished=str(data.get("finished", "")),
            scanned=int(data.get("scanned", 0) or 0), filed=int(data.get("filed", 0) or 0),
            review=int(data.get("review", 0) or 0), error=str(data.get("error", "")),
            backend=str(data.get("backend", "")),
        )

    def describe(self) -> str:
        if self.error:
            return f"Last background run failed: {self.error}"
        if not self.finished:
            return "No background run yet."
        when = self.finished.replace("T", " ")[:16]
        return (
            f"Last background run {when}: {self.scanned} scanned, "
            f"{self.filed} filed, {self.review} left for review."
        )


def status_path() -> Path:
    from config import app_support_dir

    return app_support_dir() / "agent-status.json"


def read_status() -> RunRecord:
    try:
        return RunRecord.from_dict(json.loads(status_path().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return RunRecord()


def write_status(record: RunRecord) -> None:
    path = status_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record.to_dict(), indent=2), encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError:
        pass


# --------------------------------------------------------------------------
# launchd agent
# --------------------------------------------------------------------------
def agent_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def agent_command() -> List[str]:
    """How to invoke the headless scan, whether bundled or run from source."""
    if getattr(sys, "frozen", False):  # pragma: no cover - only inside the .app
        return [str(Path(sys.executable).resolve()), "--scan-once"]
    root = Path(__file__).resolve().parent
    return [sys.executable, str(root / "main.py"), "--scan-once"]


def agent_installed() -> bool:
    return agent_plist_path().exists()


def agent_running() -> bool:
    if not agent_installed():
        return False
    try:
        result = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, timeout=8
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return LAUNCH_AGENT_LABEL in result.stdout


def install_agent(interval_minutes: int) -> Tuple[bool, str]:
    """Register the background agent with launchd."""
    if interval_minutes <= 0:
        return remove_agent()

    from config import log_dir

    logs = log_dir()
    try:
        logs.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    plist = {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": agent_command(),
        "StartInterval": int(interval_minutes) * 60,
        "RunAtLoad": False,
        "StandardOutPath": str(logs / "agent.out.log"),
        "StandardErrorPath": str(logs / "agent.err.log"),
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 5,
    }
    path = agent_plist_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as handle:
            plistlib.dump(plist, handle)
        os.chmod(path, 0o644)
    except OSError as exc:
        return False, f"Could not write {path}: {exc}"

    _launchctl("bootout")          # replace any previous registration
    ok, message = _launchctl("bootstrap")
    if not ok:
        return False, message
    return True, f"Background scanning {interval_label(interval_minutes).lower()}."


def remove_agent() -> Tuple[bool, str]:
    _launchctl("bootout")
    path = agent_plist_path()
    try:
        path.unlink()
    except FileNotFoundError:
        return True, "Background scanning was already off."
    except OSError as exc:
        return False, f"Could not remove {path}: {exc}"
    return True, "Background scanning is off."


def _launchctl(action: str) -> Tuple[bool, str]:
    domain = f"gui/{os.getuid()}"
    path = agent_plist_path()
    command = (
        ["launchctl", "bootstrap", domain, str(path)] if action == "bootstrap"
        else ["launchctl", "bootout", f"{domain}/{LAUNCH_AGENT_LABEL}"]
    )
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"launchctl failed: {exc}"
    if result.returncode != 0 and action == "bootstrap":
        return False, (result.stderr or result.stdout or "launchctl refused the job").strip()
    return True, ""


# --------------------------------------------------------------------------
# The headless run itself
# --------------------------------------------------------------------------
def run_once(settings=None, store=None, apply_moves: Optional[bool] = None) -> RunRecord:
    """One unattended scan. Used by the agent and by the in-app timer.

    Returns a :class:`RunRecord`; never raises, because the caller is either a
    launchd job with nowhere to report or a timer inside a running window.
    """
    from config import CredentialStore, Settings
    from imap_engine import IMAPEngine, MovePlan
    from llm_engine import LLMEngine
    from models import FolderPlan, TriageItem, resolve_window

    settings = settings or Settings.load()
    store = store or CredentialStore()
    should_file = settings.auto_file_background if apply_moves is None else apply_moves
    record = RunRecord(
        started=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        backend=f"{settings.provider_label} - {settings.model}",
    )

    engine = IMAPEngine(host=settings.imap_host, port=settings.imap_port)
    classifier = None
    try:
        password = store.get_icloud_password(settings.icloud_email)
        if not settings.icloud_email or not password:
            record.error = "No iCloud credentials are stored."
            write_status(record)
            return record

        start = datetime.now(timezone.utc) - timedelta(
            minutes=max(15, settings.background_window_minutes)
        )
        engine.connect(settings.icloud_email, password)
        plan = engine.folder_plan(settings.folder_root, settings.other_folder_root)
        engine.ensure_folders(plan, subscribe=settings.subscribe_new_folders)
        scan = engine.fetch_window(
            start=start, mailbox=settings.source_mailbox,
            max_messages=settings.max_messages,
            connections=settings.imap_connections, max_bytes=settings.fetch_bytes,
        )
        engine.logout()
        record.scanned = len(scan.messages)
        if not scan.messages:
            record.finished = datetime.now(timezone.utc).isoformat(timespec="seconds")
            write_status(record)
            return record

        classifier = LLMEngine(
            provider=settings.provider,
            api_key=store.get_provider_key(settings.provider),
            model=settings.model, base_url=settings.base_url, effort=settings.effort,
            max_body_chars=settings.max_body_chars, concurrency=settings.concurrency,
            fallback_to_rules=settings.fallback_to_rules,
            batch_size=settings.batch_size, ruleset=settings.ruleset,
        )
        results = classifier.classify_many(scan.messages)
        items = [
            TriageItem(
                email=message, classification=classification, folders=plan,
                threshold=settings.confidence_threshold,
                non_job_routing=settings.routing,
                auto_approve_non_job=settings.auto_approve_non_job,
            )
            for message, classification in zip(scan.messages, results)
        ]
        # Only what the app would have pre-ticked. Nothing else is touched.
        approved = [item for item in items if item.approved and item.is_actionable]
        record.review = sum(1 for item in items if not item.approved)

        if should_file and approved:
            engine.connect(settings.icloud_email, password)
            report = engine.move_messages(
                [MovePlan(item.email.uid, item.target_folder) for item in approved],
                mailbox=settings.source_mailbox,
            )
            record.filed = report.moved_count
            if report.failed:
                record.error = f"{len(report.failed)} message(s) could not be filed."
    except Exception as exc:  # noqa: BLE001 - a background job reports, never crashes
        record.error = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            engine.logout()
        except Exception:
            pass
        if classifier is not None:
            classifier.close()

    record.finished = datetime.now(timezone.utc).isoformat(timespec="seconds")
    write_status(record)
    return record
