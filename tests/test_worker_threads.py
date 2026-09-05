"""The QThread workers, driven synchronously with fake engines."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

import workers as workers_module  # noqa: E402
from conftest import FakeAnthropic, FakeResponse, build_mime  # noqa: E402
from config import Settings  # noqa: E402
from imap_engine import IMAPAuthError, IMAPEngine, MovePlan  # noqa: E402
from llm_engine import LLMEngine  # noqa: E402
from models import Category, NonJobRouting  # noqa: E402
from workers import ApplyWorker, ConnectionTestWorker, ScanWorker  # noqa: E402

UTC = timezone.utc

VERDICT = json.dumps({
    "summary": "A recruiter invited you to interview. Book a slot this week.",
    "is_job_related": True,
    "category": "INTERVIEW",
    "other_category": "NOT_APPLICABLE",
    "confidence_score": 0.98,
    "reasoning": "Explicit invitation plus a scheduling link.",
})


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


class Recorder:
    """Collects everything a worker emits."""

    def __init__(self, worker):
        self.progress = []
        self.logs = []
        self.failures = []
        self.results = []
        worker.progress.connect(lambda d, t, m: self.progress.append((d, t, m)))
        worker.log_message.connect(self.logs.append)
        worker.failed.connect(lambda title, detail: self.failures.append((title, detail)))
        worker.finished_ok.connect(self.results.append)

    @property
    def result(self):
        assert self.results, f"worker emitted no result; failures={self.failures}"
        return self.results[0]


@pytest.fixture
def wired(monkeypatch, fake_imap_factory):
    """Patch the engines the workers construct internally."""
    state = {}

    def install(server=None, llm_handler=None, **server_kwargs):
        server = server or fake_imap_factory(**server_kwargs)
        state["server"] = server
        monkeypatch.setattr(
            workers_module, "IMAPEngine",
            lambda host=None, port=None: IMAPEngine(connection_factory=lambda h, p: server),
        )
        handler = llm_handler or (lambda kwargs, beta: FakeResponse(VERDICT))
        monkeypatch.setattr(
            workers_module, "LLMEngine",
            lambda **kw: LLMEngine(
                api_key=kw.get("api_key", "k"),
                model=kw.get("model", "claude-haiku-4-5"),
                concurrency=kw.get("concurrency", 2),
                client=FakeAnthropic(handler=handler),   # forces the Claude backend
                sleep=lambda _: None,
            ),
        )
        return server

    return install


def settings(**overrides) -> Settings:
    base = dict(icloud_email="you@icloud.example", max_messages=100)
    base.update(overrides)
    return Settings(**base)


def scan_worker(**kwargs) -> ScanWorker:
    return ScanWorker(
        settings=kwargs.pop("settings", settings()),
        icloud_password=kwargs.pop("password", "app-specific"),
        api_key=kwargs.pop("api_key", "sk-ant-test"),
        window_start=kwargs.pop("start", datetime(2026, 9, 1, tzinfo=UTC)),
        window_end=kwargs.pop("end", datetime(2026, 9, 30, tzinfo=UTC)),
    )


MESSAGES = {
    "1": build_mime(subject="Interview invitation", plain="Please pick a time for your interview."),
    "2": build_mime(subject="Second message", plain="Another message body long enough to keep."),
}


class TestScanWorker:
    def test_happy_path(self, qapp, wired):
        wired(folders=["INBOX"], messages=dict(MESSAGES))
        worker = scan_worker()
        recorder = Recorder(worker)
        worker.run()

        outcome = recorder.result
        assert len(outcome.items) == 2
        assert all(i.classification.category is Category.INTERVIEW for i in outcome.items)
        assert outcome.folder_plan.root == "Job Search"
        assert outcome.usage_text
        assert recorder.failures == []

    def test_creates_the_folder_tree_and_reports_it(self, qapp, wired):
        server = wired(folders=["INBOX"], messages=dict(MESSAGES))
        worker = scan_worker()
        recorder = Recorder(worker)
        worker.run()

        assert "Job Search/Received" in server.folders
        assert recorder.result.created_folders == list(recorder.result.folder_plan.all_folders)
        assert any("Created folders" in line for line in recorder.logs)

    def test_the_imap_session_is_closed_before_the_slow_part(self, qapp, wired):
        """iCloud drops idle IMAP sessions; classification can take minutes."""
        server = wired(folders=["INBOX"], messages=dict(MESSAGES))
        logged_out_when_classifying = {}

        def handler(kwargs, beta):
            logged_out_when_classifying["value"] = server.logged_out
            return FakeResponse(VERDICT)

        wired(server=server, llm_handler=handler)
        worker = scan_worker()
        Recorder(worker)
        worker.run()
        assert logged_out_when_classifying["value"] is True

    def test_routing_settings_are_applied_to_the_items(self, qapp, wired):
        wired(folders=["INBOX"], messages=dict(MESSAGES))
        worker = scan_worker(
            settings=settings(
                confidence_threshold=0.99,
                non_job_routing=NonJobRouting.FILE.value,
            )
        )
        recorder = Recorder(worker)
        worker.run()
        item = recorder.result.items[0]
        assert item.threshold == 0.99
        assert item.non_job_routing is NonJobRouting.FILE
        assert item.approved is False  # 0.98 is now below the threshold

    def test_an_empty_window_finishes_without_calling_the_model(self, qapp, wired):
        wired(folders=["INBOX"], messages={})
        worker = scan_worker()
        recorder = Recorder(worker)
        worker.run()
        assert recorder.result.items == []
        assert recorder.failures == []

    def test_bad_credentials_surface_as_a_failure(self, qapp, wired):
        wired(folders=["INBOX"], messages=dict(MESSAGES))
        worker = scan_worker(password="wrong")
        recorder = Recorder(worker)
        worker.run()
        assert recorder.results == []
        assert recorder.failures
        title, detail = recorder.failures[0]
        assert "iCloud" in title
        assert "app-specific password" in detail

    def test_a_model_auth_failure_surfaces_as_a_failure(self, qapp, wired):
        from conftest import ApiStatusError

        wired(
            folders=["INBOX"], messages=dict(MESSAGES),
            llm_handler=lambda k, b: (_ for _ in ()).throw(ApiStatusError("invalid x-api-key", 401)),
        )
        worker = scan_worker()
        recorder = Recorder(worker)
        worker.run()
        assert recorder.results == []
        assert "analyze" in recorder.failures[0][0]

    def test_cancellation_returns_an_empty_outcome_not_an_error(self, qapp, wired):
        wired(folders=["INBOX"], messages=dict(MESSAGES))
        worker = scan_worker()
        recorder = Recorder(worker)
        worker.cancel()
        worker.run()
        assert recorder.result.items == []
        assert recorder.failures == []

    def test_a_missing_uidplus_capability_is_warned_about(self, qapp, wired, fake_imap_factory):
        wired(server=fake_imap_factory(
            folders=["INBOX"], messages=dict(MESSAGES), capabilities=("IMAP4REV1",)
        ))
        worker = scan_worker()
        recorder = Recorder(worker)
        worker.run()
        assert any("UIDPLUS" in w for w in recorder.result.warnings)

    def test_a_truncated_window_is_warned_about(self, qapp, wired):
        wired(folders=["INBOX"], messages={str(i): build_mime() for i in range(1, 11)})
        worker = scan_worker(settings=settings(max_messages=3))
        recorder = Recorder(worker)
        worker.run()
        assert any("most recent" in w for w in recorder.result.warnings)
        assert len(recorder.result.items) == 3

    def test_progress_is_reported_through_both_phases(self, qapp, wired):
        wired(folders=["INBOX"], messages=dict(MESSAGES))
        worker = scan_worker()
        recorder = Recorder(worker)
        worker.run()
        text = " ".join(m for _, _, m in recorder.progress)
        assert "Connecting" in text
        assert "Analyz" in text


class TestApplyWorker:
    def apply_worker(self, plans, extra=(), **overrides):
        return ApplyWorker(
            settings=settings(**overrides),
            icloud_password="app-specific",
            plans=plans,
            extra_folders=list(extra),
        )

    def test_files_the_approved_messages(self, qapp, wired):
        server = wired(folders=["INBOX"], messages=dict(MESSAGES))
        worker = self.apply_worker([MovePlan("1", "Job Search/Interview")])
        recorder = Recorder(worker)
        worker.run()

        report = recorder.result
        assert report.moved == {"1": "Job Search/Interview"}
        assert server.expunged == ["1"]
        assert "2" in server.messages

    def test_missing_folders_are_created_first(self, qapp, wired):
        server = wired(folders=["INBOX"], messages=dict(MESSAGES))
        worker = self.apply_worker(
            [MovePlan("1", "Sorted Mail/Finance")],
            extra=["Sorted Mail", "Sorted Mail/Finance"],
        )
        recorder = Recorder(worker)
        worker.run()
        assert "Sorted Mail/Finance" in server.folders
        assert recorder.result.moved == {"1": "Sorted Mail/Finance"}
        assert "Sorted Mail/Finance" in recorder.result.created_folders

    def test_a_connection_failure_surfaces_as_a_failure(self, qapp, wired):
        wired(folders=["INBOX"], messages=dict(MESSAGES))
        worker = ApplyWorker(
            settings=settings(), icloud_password="wrong",
            plans=[MovePlan("1", "Job Search/Interview")],
        )
        recorder = Recorder(worker)
        worker.run()
        assert recorder.results == []
        assert "file your messages" in recorder.failures[0][0]

    def test_cancellation_reports_a_partial_result(self, qapp, wired):
        wired(folders=["INBOX"], messages=dict(MESSAGES))
        worker = self.apply_worker([MovePlan("1", "Job Search/Interview")])
        recorder = Recorder(worker)
        worker.cancel()
        worker.run()
        assert recorder.result.moved == {}
        assert recorder.result.warnings


class TestConnectionTestWorker:
    def test_imap_probe(self, qapp, wired):
        wired(folders=["INBOX", "Job Search"], messages=dict(MESSAGES))
        worker = ConnectionTestWorker(
            mode="imap", settings=settings(), icloud_password="app-specific"
        )
        recorder = Recorder(worker)
        worker.run()
        assert recorder.result["mode"] == "imap"
        assert recorder.result["uidplus"] is True

    def test_imap_probe_failure(self, qapp, wired):
        wired(folders=["INBOX"], messages={})
        worker = ConnectionTestWorker(mode="imap", settings=settings(), icloud_password="nope")
        recorder = Recorder(worker)
        worker.run()
        assert recorder.results == []
        assert "iCloud connection failed" == recorder.failures[0][0]

    def test_claude_probe(self, qapp, wired):
        wired(folders=["INBOX"], messages={})
        worker = ConnectionTestWorker(
            mode="claude", settings=settings(), api_key="sk-ant-test"
        )
        recorder = Recorder(worker)
        worker.run()
        assert recorder.result["mode"] == "claude"
        assert recorder.result["category"] == "INTERVIEW"

    def test_claude_probe_failure(self, qapp, wired):
        wired(folders=["INBOX"], messages={}, llm_handler=lambda k, b: FakeResponse("not json"))
        worker = ConnectionTestWorker(
            mode="claude", settings=settings(), api_key="sk-ant-test"
        )
        recorder = Recorder(worker)
        worker.run()
        assert recorder.results == []
        assert "connection failed" in recorder.failures[0][0]
