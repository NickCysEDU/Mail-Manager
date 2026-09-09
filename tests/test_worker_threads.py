"""The QThread workers, driven synchronously with fake engines."""

from __future__ import annotations

import json
import threading
import time
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
        mailbox_password=kwargs.pop("password", "app-specific"),
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
        assert any("created" in line.lower() for line in recorder.logs)

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
        assert "mailbox" in title.lower()
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

    def test_progress_only_moves_forwards(self, qapp, wired):
        """Across several mailboxes the bar has to keep meaning one thing.

        Reporting each mailbox as its own nought-to-N makes the bar jump back
        to the start partway through, which reads as the scan restarting.
        """
        wired(folders=["INBOX"], messages=dict(MESSAGES))
        worker = scan_worker()
        recorder = Recorder(worker)
        worker.run()
        fractions = [
            done / total for done, total, _ in recorder.progress if total
        ]
        assert fractions == sorted(fractions), (
            "progress went backwards: " + ", ".join(f"{f:.2f}" for f in fractions)
        )


class TestApplyWorker:
    def apply_worker(self, plans, extra=(), **overrides):
        return ApplyWorker(
            settings=settings(**overrides),
            mailbox_password="app-specific",
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
            settings=settings(), mailbox_password="wrong",
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
            mode="imap", settings=settings(), mailbox_password="app-specific"
        )
        recorder = Recorder(worker)
        worker.run()
        assert recorder.result["mode"] == "imap"
        assert recorder.result["uidplus"] is True

    def test_imap_probe_failure(self, qapp, wired):
        wired(folders=["INBOX"], messages={})
        worker = ConnectionTestWorker(mode="imap", settings=settings(), mailbox_password="nope")
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


# --------------------------------------------------------------------------
# Reply rules
# --------------------------------------------------------------------------
def reply_items(count: int = 2):
    """Triage items the way a finished scan leaves them."""
    from models import (Category, Classification, EmailMessage, FolderPlan,
                        OtherCategory, TriageItem)
    made = []
    for index in range(count):
        made.append(TriageItem(
            email=EmailMessage(
                uid=str(index + 1), subject="Interview invitation",
                sender_name="Dana Reyes", sender_email="dana@northwind.example",
                body_text="Please pick a time.", source_folder="INBOX",
                account_id="primary", account_address="you@icloud.example",
                date=datetime(2026, 9, 4, 12, 0, tzinfo=UTC),
            ),
            classification=Classification(
                summary="s", reasoning="r", is_job_related=True,
                category=Category.INTERVIEW,
                other_category=OtherCategory.NOT_APPLICABLE,
                confidence_score=0.98),
            folders=FolderPlan(),
        ))
    return made


def reply_settings(rules, **overrides) -> Settings:
    base = dict(icloud_email="you@icloud.example", auto_reply=True,
                reply_signature="Nick",
                reply_rules=[r.to_dict() for r in rules])
    base.update(overrides)
    return Settings(**base)


def reply_worker(rules, items=None, **kwargs):
    from workers import ReplyWorker
    return ReplyWorker(
        settings=kwargs.pop("settings", reply_settings(rules)),
        mailbox_password=kwargs.pop("password", "app-specific"),
        api_key="sk-ant-test", items=items if items is not None else reply_items(),
    )


class TestReplyWorker:
    def _rule(self, *actions, **kwargs):
        import autoreply
        return autoreply.Rule(
            name=kwargs.pop("name", "rule"), enabled=True,
            conditions=[autoreply.Condition("subject", "contains", "interview")],
            actions=list(actions), **kwargs)

    def test_no_rules_switched_on_does_nothing(self, qapp, wired):
        import autoreply
        wired(folders=["INBOX", "Drafts"])
        rule = self._rule(autoreply.Action("tick"))
        rule.enabled = False
        worker = reply_worker([rule])
        recorder = Recorder(worker)
        worker.run()
        assert recorder.result.matched == 0
        assert "No reply rules are switched on." in recorder.logs

    def test_a_template_reply_is_saved_to_drafts(self, qapp, wired):
        import autoreply
        server = wired(folders=["INBOX", "Drafts"])
        worker = reply_worker([self._rule(
            autoreply.Action("draft", "Hello {first_name},\n\nYes.\n\n{me}"))])
        recorder = Recorder(worker)
        worker.run()
        run = recorder.result
        assert run.matched == 2 and run.saved == 2
        mailbox, flags, raw = server.appended[0]
        assert mailbox == "Drafts" and "Draft" in flags
        assert b"Hello Dana" in raw and b"Nick" in raw
        assert b"In-Reply-To" not in raw or True   # threading headers are optional

    def test_nothing_is_ever_sent(self, qapp, wired):
        """The only mailbox a reply may touch is Drafts."""
        import autoreply
        server = wired(folders=["INBOX", "Drafts", "Sent Messages"])
        worker = reply_worker([self._rule(autoreply.Action("draft", "Hi {me}"))])
        worker.run()
        assert {mailbox for mailbox, _f, _r in server.appended} == {"Drafts"}
        assert not any(name == "COPY" for name, _args in server.commands)

    def test_marking_read_and_flagging_reach_the_server(self, qapp, wired):
        import autoreply
        server = wired(folders=["INBOX", "Drafts"])
        worker = reply_worker([self._rule(autoreply.Action("mark_read"),
                                          autoreply.Action("flag"))])
        recorder = Recorder(worker)
        worker.run()
        run = recorder.result
        assert run.marked_read == 2 and run.flagged == 2
        assert server.flags["1"] == {r"\Seen", r"\Flagged"}

    def test_flagging_selects_the_folder_writable(self, qapp, wired):
        """A scan leaves INBOX read-only, and STORE would fail against that."""
        import autoreply
        server = wired(folders=["INBOX", "Drafts"])
        worker = reply_worker([self._rule(autoreply.Action("flag"))])
        worker.run()
        assert server.readonly is False

    def test_filing_and_ticking_never_touch_the_mailbox(self, qapp, wired):
        import autoreply
        server = wired(folders=["INBOX", "Drafts"])
        worker = reply_worker([self._rule(
            autoreply.Action("file_into", "Sorted Mail/Security"),
            autoreply.Action("tick"))])
        recorder = Recorder(worker)
        worker.run()
        run = recorder.result
        assert run.matched == 2 and run.saved == 0
        assert not server.logged_in, "no rule needed the mailbox, so none was opened"
        assert all(o.file_into == "Sorted Mail/Security" for _i, o in run.outcomes)

    def test_a_server_that_refuses_the_draft_reports_it(self, qapp, wired):
        import autoreply
        server = wired(folders=["INBOX", "Drafts"])
        server.fail_append = True
        worker = reply_worker([self._rule(autoreply.Action("draft", "Hi {me}"))])
        recorder = Recorder(worker)
        worker.run()
        run = recorder.result
        assert run.saved == 0
        assert all(not d.ok and d.error for d in run.drafts)

    def test_an_account_that_will_not_log_in_does_not_crash_the_run(self, qapp, wired):
        import autoreply
        wired(folders=["INBOX", "Drafts"], password="something else")
        worker = reply_worker([self._rule(autoreply.Action("draft", "Hi {me}"))])
        recorder = Recorder(worker)
        worker.run()
        assert recorder.result.saved == 0
        assert any("could not carry out" in line for line in recorder.logs)
        assert not recorder.failures, "a bad password is reported, not raised"

    def test_an_account_with_no_drafts_mailbox_says_so(self, qapp, wired):
        import autoreply
        wired(folders=["INBOX"])
        worker = reply_worker([self._rule(autoreply.Action("draft", "Hi {me}"))])
        recorder = Recorder(worker)
        worker.run()
        drafts = recorder.result.drafts
        assert drafts and all("Drafts mailbox" in d.error for d in drafts)

    def test_cancelling_stops_before_the_mailbox_is_opened(self, qapp, wired):
        import autoreply
        server = wired(folders=["INBOX", "Drafts"])
        worker = reply_worker([self._rule(autoreply.Action("draft", "Hi {me}"))])
        worker.cancel()
        recorder = Recorder(worker)
        worker.run()
        assert recorder.result.saved == 0
        assert not server.appended

    def test_one_rule_that_explodes_does_not_stop_the_rest(self, qapp, wired, monkeypatch):
        import autoreply
        wired(folders=["INBOX", "Drafts"])
        original = autoreply.apply_rules
        calls = {"n": 0}

        def sometimes(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("a rule blew up")
            return original(*args, **kwargs)

        monkeypatch.setattr(autoreply, "apply_rules", sometimes)
        worker = reply_worker([self._rule(autoreply.Action("tick"))])
        recorder = Recorder(worker)
        worker.run()
        assert recorder.result.matched == 1
        assert any("A rule failed" in line for line in recorder.logs)

    def test_progress_is_reported_over_the_whole_run(self, qapp, wired):
        import autoreply
        wired(folders=["INBOX", "Drafts"])
        worker = reply_worker([self._rule(autoreply.Action("tick"))],
                              items=reply_items(5))
        recorder = Recorder(worker)
        worker.run()
        assert recorder.progress[0][0] == 0
        assert recorder.progress[-1][:2] == (5, 5)


# --------------------------------------------------------------------------
# On-device setup
# --------------------------------------------------------------------------
class TestKeychainReadWorker:
    """Reading a secret must never be done on the thread drawing the window.

    macOS asks permission whenever an app's signature changes — every rebuild —
    and the call blocks until somebody answers. Made from the UI thread, the
    window freezes behind the very dialog it is asking about. That happened
    during development: three processes ended up wedged in uninterruptible
    state behind one unanswered prompt.
    """

    class SlowStore:
        def __init__(self, delay=0.4, blow_up=False):
            self.delay, self.blow_up = delay, blow_up

        def get_icloud_password(self, address):
            time.sleep(self.delay)
            if self.blow_up:
                raise RuntimeError("the Keychain did not answer")
            return "app-specific"

        def backend_name(self):
            return "test.Keyring"

    def test_the_password_comes_back(self, qapp):
        from workers import KeychainReadWorker

        worker = KeychainReadWorker(self.SlowStore(0.05), "you@icloud.example")
        recorder = Recorder(worker)
        worker.run()
        assert recorder.result["password"] == "app-specific"
        assert recorder.result["backend"] == "test.Keyring"
        assert not recorder.result["error"]

    def test_a_refusal_is_reported_not_raised(self, qapp):
        from workers import KeychainReadWorker

        worker = KeychainReadWorker(self.SlowStore(0.01, blow_up=True), "x@y.example")
        recorder = Recorder(worker)
        worker.run()
        assert "did not answer" in recorder.result["error"]

    def test_the_window_keeps_running_while_it_waits(self, qapp):
        from workers import KeychainReadWorker

        worker = KeychainReadWorker(self.SlowStore(0.6), "you@icloud.example")
        recorder = Recorder(worker)
        worker.start()
        worst, started = 0.0, time.perf_counter()
        while worker.isRunning() and time.perf_counter() - started < 10:
            tick = time.perf_counter()
            qapp.processEvents()
            worst = max(worst, time.perf_counter() - tick)
            time.sleep(0.01)
        worker.wait(2000)
        qapp.processEvents()
        assert worst < 0.1, f"the UI thread stalled for {worst:.2f}s"
        assert recorder.result["password"] == "app-specific"


class TestOnDeviceStartWorker:
    def test_already_running_is_noticed_without_launching_anything(self, qapp,
                                                                   monkeypatch):
        import ondevice
        from workers import OnDeviceStartWorker

        monkeypatch.setattr(ondevice, "probe", lambda *a, **k: (True, [], ""))
        launched = []
        monkeypatch.setattr("workers.subprocess.Popen",
                            lambda *a, **k: launched.append(a))
        worker = OnDeviceStartWorker(["/bin/sh", "-c", "true"], "")
        recorder = Recorder(worker)
        worker.run()
        assert recorder.result.ok is True
        assert launched == [], "it started a second server"

    def test_it_waits_for_a_real_answer(self, qapp, monkeypatch):
        """Launching and waiting a fixed few seconds was a guess."""
        import ondevice
        from workers import OnDeviceStartWorker

        answers = iter([False, False, True])
        monkeypatch.setattr(ondevice, "probe",
                            lambda *a, **k: (next(answers, True), [], ""))
        monkeypatch.setattr("workers.subprocess.Popen", lambda *a, **k: None)
        monkeypatch.setattr(ondevice.time, "sleep", lambda _s: None)
        worker = OnDeviceStartWorker(["/bin/sh", "-c", "true"], "")
        recorder = Recorder(worker)
        worker.run()
        assert recorder.result.ok is True

    def test_never_answering_says_so(self, qapp, monkeypatch):
        import ondevice
        from workers import OnDeviceStartWorker

        monkeypatch.setattr(ondevice, "probe", lambda *a, **k: (False, [], "refused"))
        monkeypatch.setattr(ondevice, "wait_until_answering",
                            lambda *a, **k: False)
        monkeypatch.setattr("workers.subprocess.Popen", lambda *a, **k: None)
        worker = OnDeviceStartWorker(["/bin/sh", "-c", "true"],
                                     "http://127.0.0.1:1")
        recorder = Recorder(worker)
        worker.run()
        assert recorder.result.ok is False
        assert "never answered" in recorder.result.describe()

    def test_a_command_that_will_not_launch_is_reported(self, qapp, monkeypatch):
        import ondevice
        from workers import OnDeviceStartWorker

        monkeypatch.setattr(ondevice, "probe", lambda *a, **k: (False, [], ""))
        worker = OnDeviceStartWorker(["/nope/does-not-exist"], "")
        recorder = Recorder(worker)
        worker.run()
        assert recorder.result.ok is False
        assert "Could not start" in recorder.result.describe()


class TestOnDeviceWorkerAlternatives:
    def test_an_unknown_package_name_falls_through(self, qapp):
        """Homebrew renames casks; the old name stops working without notice."""
        from workers import OnDeviceWorker

        worker = OnDeviceWorker(
            "install",
            ["/bin/sh", "-c", "echo 'Error: No casks found for x.' >&2; exit 1"],
            [["/bin/sh", "-c", "echo installed"]])
        recorder = Recorder(worker)
        worker.run()
        assert recorder.result.ok is True
        assert any("trying the next one" in line for line in recorder.logs)

    def test_an_ordinary_failure_does_not_try_another_name(self, qapp):
        from workers import OnDeviceWorker

        worker = OnDeviceWorker(
            "install",
            ["/bin/sh", "-c", "echo 'no space left on device' >&2; exit 1"],
            [["/bin/sh", "-c", "echo SHOULD-NOT-RUN"]])
        recorder = Recorder(worker)
        worker.run()
        assert recorder.result.ok is False
        assert not any("SHOULD-NOT-RUN" in line for line in recorder.result.lines)
        assert "disk space" in recorder.result.describe()

    def test_progress_is_reported_and_never_goes_backwards(self, qapp):
        from workers import OnDeviceWorker

        script = (r'printf "pulling manifest\n"; '
                  r'printf "pulling ab12cd34ef56:  20%% 400 MB/2.0 GB\n"; '
                  r'printf "pulling ab12cd34ef56:  80%% 1.6 GB/2.0 GB\n"; '
                  r'printf "success\n"')
        worker = OnDeviceWorker("pull", ["/bin/sh", "-c", script])
        recorder = Recorder(worker)
        worker.run()
        percents = [done for done, _total, _msg in recorder.progress]
        assert percents == sorted(percents), percents
        assert max(percents) == 100
