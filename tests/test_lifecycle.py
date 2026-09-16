"""Process hygiene: nothing keeps running after a stop, a close, or a quit."""

from __future__ import annotations

import threading
import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QThread, Signal  # noqa: E402
from PySide6.QtGui import QAction  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from config import InMemoryCredentialStore, Settings  # noqa: E402
from conftest import FakeAnthropic, FakeResponse  # noqa: E402
from gui import SHOW_ALL, SHOW_JOB_ONLY, SHOW_SELECTED, MainWindow, SettingsDialog  # noqa: E402
from llm_engine import ClassificationCancelled, LLMEngine  # noqa: E402
from models import EmailMessage  # noqa: E402
from workers import ScanWorker, _BaseWorker  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


class BlockingWorker(_BaseWorker):
    """A worker that runs until cancelled, so stop paths can be exercised."""

    finished_ok = Signal(object)
    task_name = "blocking test"

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.started_running = threading.Event()
        self.observed_cancel = False

    def run(self) -> None:
        self.started_running.set()
        while not self.cancel_event.wait(0.01):
            pass
        self.observed_cancel = True
        self.finished_ok.emit(None)


class StuckWorker(_BaseWorker):
    """A worker that ignores cancellation, to exercise the force-terminate path."""

    finished_ok = Signal(object)
    task_name = "stuck test"

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.started_running = threading.Event()
        self.release = threading.Event()

    def run(self) -> None:
        self.started_running.set()
        self.release.wait(30)


def start_and_wait(worker: BlockingWorker) -> BlockingWorker:
    worker.start()
    assert worker.started_running.wait(5), "worker never started"
    return worker


# ==========================================================================
# Worker primitives
# ==========================================================================
class TestWorkerStop:
    def test_cancel_sets_the_event_and_requests_interruption(self, qapp):
        worker = BlockingWorker()
        worker.cancel()
        assert worker.cancelled is True

    def test_stop_returns_true_once_the_thread_has_finished(self, qapp):
        worker = start_and_wait(BlockingWorker())
        assert worker.stop(5000) is True
        assert worker.isRunning() is False
        assert worker.observed_cancel is True

    def test_stop_on_an_idle_worker_is_a_no_op(self, qapp):
        assert BlockingWorker().stop(10) is True

    def test_stop_is_idempotent(self, qapp):
        worker = start_and_wait(BlockingWorker())
        assert worker.stop(5000) is True
        assert worker.stop(5000) is True

    def test_stop_reports_failure_for_a_wedged_thread(self, qapp):
        worker = StuckWorker()
        worker.start()
        assert worker.started_running.wait(5)
        try:
            assert worker.stop(200) is False   # the caller must escalate
        finally:
            worker.release.set()
            worker.wait(5000)


# ==========================================================================
# The classifier releases its sockets
# ==========================================================================
class TestClassifierTeardown:
    def test_close_releases_the_http_client(self):
        closed = []

        class Client(FakeAnthropic):
            def close(self):
                closed.append(True)

        engine = LLMEngine(api_key="k", client=Client())
        engine.close()
        assert closed == [True]

    def test_close_is_safe_without_a_client(self):
        LLMEngine(api_key="k").close()   # must not raise

    def test_close_is_idempotent(self):
        engine = LLMEngine(api_key="k", client=FakeAnthropic())
        engine.close()
        engine.close()

    def test_a_closed_engine_refuses_further_work(self):
        engine = LLMEngine(api_key="k", client=FakeAnthropic())
        engine.close()
        with pytest.raises(ClassificationCancelled):
            engine.classify(EmailMessage(uid="1"))

    def test_a_close_during_a_batch_does_not_hang_it(self):
        """Stopping must not wait out an in-flight request's full timeout."""
        started = threading.Event()
        engine_box = {}

        def handler(kwargs, beta):
            started.set()
            time.sleep(0.05)
            return FakeResponse('{"summary":"a. b.","is_job_related":true,'
                                '"category":"INTERVIEW","other_category":"NOT_APPLICABLE",'
                                '"confidence_score":0.97,"reasoning":"r"}')

        engine = LLMEngine(api_key="k", client=FakeAnthropic(handler=handler), concurrency=2)
        engine_box["engine"] = engine
        cancel = threading.Event()

        def stopper():
            started.wait(5)
            cancel.set()
            engine.close()

        threading.Thread(target=stopper, daemon=True).start()
        began = time.monotonic()
        with pytest.raises(ClassificationCancelled):
            engine.classify_many(
                [EmailMessage(uid=str(i)) for i in range(40)], cancel=cancel
            )
        assert time.monotonic() - began < 10  # returns promptly, not after 40 calls


class TestScanWorkerTeardown:
    def test_cancelling_a_scan_closes_the_classifier(self, qapp):
        worker = ScanWorker(
            settings=Settings(icloud_email="a@b.com"),
            mailbox_password="pw", api_key="k",
            window_start=None, window_end=None,
        )
        engine = LLMEngine(api_key="k", client=FakeAnthropic())
        worker._classifier = engine
        worker.cancel()
        assert engine._closed is True
        assert worker.cancelled is True

    def test_cancelling_without_a_classifier_is_safe(self, qapp):
        worker = ScanWorker(
            settings=Settings(), mailbox_password="", api_key="",
            window_start=None, window_end=None,
        )
        worker.cancel()


# ==========================================================================
# Window-level stop / shutdown
# ==========================================================================
class TestWindowStopAll:
    @pytest.fixture
    def window(self, qapp, tmp_path, monkeypatch):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        window = MainWindow(Settings(icloud_email="you@icloud.example"), InMemoryCredentialStore())
        yield window
        window.close()

    def test_stop_all_with_nothing_running(self, window):
        assert window.stop_all() == 0
        assert "Nothing is running" in window.status_label.text()

    def test_stop_all_stops_every_registered_worker(self, window):
        workers = [start_and_wait(BlockingWorker(window)) for _ in range(3)]
        for worker in workers:
            window._register(worker)
        assert len(window.running_workers()) == 3

        assert window.stop_all() == 3
        assert window.running_workers() == []
        assert all(not w.isRunning() for w in workers)
        assert "Stopped 3 task(s)" in window.status_label.text()

    def test_stop_all_detaches_a_wedged_worker_instead_of_killing_it(self, window):
        """Terminating a thread running Python can deadlock on the GIL."""
        from gui import _ABANDONED

        worker = StuckWorker(window)
        worker.start()
        assert worker.started_running.wait(5)
        window._register(worker)
        try:
            assert window.stop_all() == 1
            assert window.running_workers() == []      # detached from the window
            assert worker in _ABANDONED                # but kept alive, not destroyed
            assert worker.isRunning() is True          # and never force-killed
            assert window.scan_button.text() in ("Scan && Analyze", "Reload Sample Data")
        finally:
            worker.release.set()
            worker.wait(5000)
            if worker in _ABANDONED:
                _ABANDONED.remove(worker)

    def test_stop_all_is_safe_to_call_twice(self, window):
        window._register(start_and_wait(BlockingWorker(window)))
        window.stop_all()
        assert window.stop_all() == 0

    def test_the_primary_button_becomes_stop_while_anything_runs(self, window):
        assert window.scan_button.text() in ("Scan && Analyze", "Reload Sample Data")
        window._register(start_and_wait(BlockingWorker(window)))
        window._update_status()
        assert window.scan_button.text() == "Stop"
        assert window.stop_action.isEnabled() is True
        window.stop_all()
        assert window.scan_button.text() in ("Scan && Analyze", "Reload Sample Data")
        assert window.stop_action.isEnabled() is False

    def test_the_stop_menu_action_has_a_shortcut(self, window):
        assert window.stop_action.shortcut().toString() in ("Ctrl+.", "⌘.")

    def test_finished_workers_are_reaped_not_accumulated(self, window):
        worker = start_and_wait(BlockingWorker(window))
        window._register(worker)
        assert len(window._workers) == 1
        worker.stop(5000)
        QApplication.processEvents()      # deliver the finished signal
        assert window._workers == []

    def test_shutdown_detaches_a_wedged_worker(self, window):
        from gui import _ABANDONED

        worker = StuckWorker(window)
        worker.start()
        assert worker.started_running.wait(5)
        window._register(worker)
        try:
            window.shutdown()
            assert window._workers == []
            assert worker in _ABANDONED
        finally:
            worker.release.set()
            worker.wait(5000)
            if worker in _ABANDONED:
                _ABANDONED.remove(worker)

    def test_shutdown_leaves_no_threads_behind(self, window):
        workers = [start_and_wait(BlockingWorker(window)) for _ in range(2)]
        for worker in workers:
            window._register(worker)
        window.shutdown()
        assert window._workers == []
        assert all(not w.isRunning() for w in workers)

    def test_shutdown_is_idempotent(self, window):
        window.shutdown()
        window.shutdown()

    def test_closing_the_window_stops_everything(self, window):
        worker = start_and_wait(BlockingWorker(window))
        window._register(worker)
        window.close()
        assert worker.isRunning() is False

    def test_starting_a_second_task_is_refused(self, window, dialog_calls):
        window._register(start_and_wait(BlockingWorker(window)))
        try:
            assert window._busy() is True
            assert any("already running" in text for _, _, text in dialog_calls)
        finally:
            window.stop_all()


class TestSettingsDialogTeardown:
    def test_closing_the_dialog_stops_its_test_worker(self, qapp):
        dialog = SettingsDialog(Settings(icloud_email="a@b.com"), InMemoryCredentialStore())
        worker = start_and_wait(BlockingWorker(dialog))
        dialog._worker = worker
        dialog.reject()
        assert worker.isRunning() is False
        assert dialog._worker is None
        dialog.deleteLater()

    def test_accepting_the_dialog_also_stops_it(self, qapp):
        dialog = SettingsDialog(Settings(icloud_email="a@b.com"), InMemoryCredentialStore())
        worker = start_and_wait(BlockingWorker(dialog))
        dialog._worker = worker
        dialog.accept()
        assert worker.isRunning() is False
        dialog.deleteLater()


# ==========================================================================
# The simplified filter control
# ==========================================================================
class TestShowFilter:
    @pytest.fixture
    def window(self, qapp, tmp_path, monkeypatch):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        window = MainWindow(Settings(icloud_email="a@b.com"), InMemoryCredentialStore())
        yield window
        window.close()

    def test_one_control_replaces_the_checkbox_row(self, window):
        modes = [window.show_combo.itemData(i) for i in range(window.show_combo.count())]
        assert modes == [SHOW_ALL, SHOW_JOB_ONLY, SHOW_SELECTED]

    def test_defaults_to_showing_everything(self, window):
        assert window.show_combo.currentData() == SHOW_ALL
        assert window.proxy._hide_non_job is False
        assert window.proxy._only_selected is False

    def test_job_only_mode(self, window):
        window.show_combo.setCurrentIndex(1)
        assert window.proxy._hide_non_job is True
        assert window.proxy._only_selected is False

    def test_ticked_only_mode(self, window):
        window.show_combo.setCurrentIndex(2)
        assert window.proxy._hide_non_job is False
        assert window.proxy._only_selected is True

    def test_the_mode_is_remembered_across_launches(self, window, tmp_path):
        window.show_combo.setCurrentIndex(1)
        window.close()
        assert Settings.load().hide_non_job is True

    def test_a_remembered_mode_is_restored(self, qapp, tmp_path, monkeypatch):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        window = MainWindow(Settings(icloud_email="a@b.com", hide_non_job=True),
                            InMemoryCredentialStore())
        assert window.show_combo.currentData() == SHOW_JOB_ONLY
        assert window.proxy._hide_non_job is True
        window.close()


class TestEmptyState:
    def test_the_empty_page_is_shown_until_there_are_results(self, qapp, tmp_path, monkeypatch):
        from tests.test_gui import make_item  # reuse the row builder

        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        window = MainWindow(Settings(icloud_email="a@b.com"), InMemoryCredentialStore())
        try:
            assert window.table_stack.currentIndex() == 0
            assert "Scan &amp; Analyze" in window.empty_label.text()
            window.model.set_items([make_item("1")])
            assert window.table_stack.currentIndex() == 1
            window.model.set_items([])
            assert window.table_stack.currentIndex() == 0
        finally:
            window.close()


# ==========================================================================
# Stop actually stops
# ==========================================================================
class TestStopHalts:
    """Closing the backend used to look like a transport error, which sent the
    scan down the local-fallback path and quietly finished it instead."""

    def messages(self, count):
        return [EmailMessage(uid=str(i), subject=f"Subject {i}") for i in range(count)]

    def broken_client(self, calls):
        class _M:
            @staticmethod
            def create(**kwargs):
                calls.append(1)
                time.sleep(0.02)
                raise RuntimeError("socket closed")

        class _Beta:
            messages = _M

        class Client:
            messages = _M
            beta = _Beta

        return Client()

    def test_stopping_halts_the_batch_instead_of_finishing_it_locally(self):
        calls = []
        engine = LLMEngine(
            api_key="k", client=self.broken_client(calls), sleep=lambda _: None,
            batch_size=1, concurrency=2, fallback_to_rules=True,
        )
        cancel = threading.Event()
        threading.Timer(0.10, lambda: (cancel.set(), engine.close())).start()
        with pytest.raises(ClassificationCancelled):
            engine.classify_many(self.messages(60), cancel=cancel)
        assert len(calls) < 20, "the scan kept going after Stop"
        assert engine.fallback_count < 20

    def test_stopping_returns_promptly(self):
        calls = []
        engine = LLMEngine(
            api_key="k", client=self.broken_client(calls), sleep=lambda _: None,
            batch_size=1, concurrency=2,
        )
        cancel = threading.Event()
        threading.Timer(0.10, lambda: (cancel.set(), engine.close())).start()
        began = time.monotonic()
        with pytest.raises(ClassificationCancelled):
            engine.classify_many(self.messages(200), cancel=cancel)
        assert time.monotonic() - began < 3.0

    def test_a_closed_engine_refuses_to_fall_back(self):
        engine = LLMEngine(api_key="k", client=self.broken_client([]), fallback_to_rules=True)
        engine.close()
        with pytest.raises(ClassificationCancelled):
            engine.classify(EmailMessage(uid="1", subject="Update"))
        assert engine.fallback_count == 0


class TestStopClearsTheUi:
    @pytest.fixture
    def window(self, qapp, tmp_path, monkeypatch):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        window = MainWindow(Settings(icloud_email="you@icloud.example"), InMemoryCredentialStore())
        yield window
        window.close()

    def test_stopping_hides_the_animated_progress_bar(self, window):
        window._set_busy(True, "Scanning…")
        assert window.progress.isVisibleTo(window)
        assert window.progress.maximum() == 0        # indeterminate = animating
        window._register(start_and_wait(BlockingWorker(window)))
        window.stop_all()
        assert window.progress.isHidden()
        assert window.progress.maximum() != 0        # no longer animating

    def test_stopping_hides_the_metrics_strip(self, window):
        window._on_metrics({"phase": "analyze", "done": 3, "total": 10})
        window._register(start_and_wait(BlockingWorker(window)))
        window._on_metrics({"phase": "analyze", "done": 3, "total": 10})
        assert window.metrics_bar.isVisibleTo(window)
        window.stop_all()
        assert window.metrics_bar.isHidden()

    def test_late_progress_signals_from_a_stopped_worker_are_ignored(self, window):
        window._register(start_and_wait(BlockingWorker(window)))
        window._set_busy(True, "Scanning…")
        window._on_progress(5, 10, "Analyzing…")
        assert window.status_label.text() == "Analyzing…"
        window.stop_all()
        window._on_progress(6, 10, "Analyzing…")     # a straggler arriving late
        assert "Analyzing" not in window.status_label.text()

    def test_a_stopped_worker_stops_emitting(self, qapp):
        worker = BlockingWorker()
        seen = []
        worker.progress.connect(lambda *a: seen.append(a))
        worker._emit_progress(1, 10, "working")
        worker.cancel()
        worker._emit_progress(2, 10, "still working")
        assert len(seen) == 1


# ==========================================================================
# Live metrics
# ==========================================================================
class TestMetrics:
    def test_the_analyze_readout_names_what_matters(self):
        from gui import _metrics_html

        html = _metrics_html({
            "phase": "analyze", "done": 42, "total": 120,
            "job_related": 18, "to_file": 11, "needs_review": 31,
            "requests": 7, "input_tokens": 20_500, "output_tokens": 3_100,
            "cost": 0.0123, "elapsed": 65.0, "rate": 0.65, "eta": 120.0,
            "model": "Gemini · gemini-flash-lite-latest",
        })
        for fragment in ("42", "120", "18", "11", "31", "$0.0123",
                         "23,600", "Analyzing", "remaining", "elapsed"):
            assert fragment in html, fragment

    def test_a_local_backend_is_reported_as_free(self):
        from gui import _metrics_html

        html = _metrics_html({"phase": "analyze", "done": 1, "total": 2,
                              "on_device": True, "cost": 0.0})
        assert "free" in html and "$" not in html

    def test_local_fallbacks_are_surfaced_while_running(self):
        from gui import _metrics_html

        html = _metrics_html({"phase": "analyze", "done": 5, "total": 9, "fallbacks": 3})
        assert "local fallback" in html

    def test_the_fetch_phase_has_its_own_heading(self):
        from gui import _metrics_html

        assert "Fetching" in _metrics_html({"phase": "fetch", "done": 3, "total": 9})
        assert "Filing" in _metrics_html({"phase": "apply", "done": 3, "total": 9})

    @pytest.mark.parametrize(
        "seconds,expected",
        [(0, "0s"), (45, "45s"), (65, "1m 05s"), (3725, "1h 02m")],
    )
    def test_durations_are_readable(self, seconds, expected):
        from gui import _format_duration

        assert _format_duration(seconds) == expected

    def test_a_slow_rate_is_shown_per_message_not_as_a_fraction(self):
        from gui import _metrics_html

        html = _metrics_html({"phase": "analyze", "done": 2, "total": 9, "rate": 0.25})
        assert "4.0 s/msg" in html


class TestQuittingWithSettingsOpen:
    """Settings is modal, so Cmd-Q goes to it and the app appears to hang.

    Reported as "Mail Manager does not quit when settings is open". It was
    two faults: the Quit action went straight to QApplication.quit, skipping
    every check, and nothing knew the dialog was there.
    """

    @pytest.fixture
    def window(self, qapp, tmp_path, monkeypatch):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        window = MainWindow(Settings(icloud_email="you@icloud.example"),
                            InMemoryCredentialStore())
        yield window
        window._settings_dialog = None
        window.close()

    def _open_settings(self, window):
        from gui import SettingsDialog
        from config import InMemoryCredentialStore

        dialog = SettingsDialog(window.settings, InMemoryCredentialStore(), window)
        dialog.show()
        window._settings_dialog = dialog
        return dialog

    def test_quit_asks_about_unsaved_settings(self, window, monkeypatch):
        asked = []
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *a, **k: (asked.append(a[1] if len(a) > 1 else ""),
                             QMessageBox.StandardButton.Cancel)[1])
        dialog = self._open_settings(window)
        try:
            window.quit_app()
            assert asked, "it tried to quit without asking"
            assert dialog.isVisible(), "Cancel should have left it open"
        finally:
            window._settings_dialog = None
            dialog.deleteLater()

    def test_cancel_keeps_the_app_running(self, window, monkeypatch):
        monkeypatch.setattr(QMessageBox, "question",
                            lambda *a, **k: QMessageBox.StandardButton.Cancel)
        quit_called = []
        monkeypatch.setattr(QApplication, "quit",
                            lambda *a: quit_called.append(True))
        dialog = self._open_settings(window)
        try:
            window.quit_app()
            assert not quit_called
        finally:
            window._settings_dialog = None
            dialog.deleteLater()

    def test_saving_accepts_the_dialog(self, window, monkeypatch):
        monkeypatch.setattr(QMessageBox, "question",
                            lambda *a, **k: QMessageBox.StandardButton.Save)
        monkeypatch.setattr(QApplication, "quit", lambda *a: None)
        dialog = self._open_settings(window)
        try:
            window.quit_app()
            assert dialog.result() == QDialog.DialogCode.Accepted
        finally:
            window._settings_dialog = None
            dialog.deleteLater()

    def test_discarding_rejects_it(self, window, monkeypatch):
        monkeypatch.setattr(QMessageBox, "question",
                            lambda *a, **k: QMessageBox.StandardButton.Discard)
        monkeypatch.setattr(QApplication, "quit", lambda *a: None)
        dialog = self._open_settings(window)
        try:
            window.quit_app()
            assert dialog.result() == QDialog.DialogCode.Rejected
        finally:
            window._settings_dialog = None
            dialog.deleteLater()

    def test_no_settings_open_means_no_extra_question(self, window, monkeypatch):
        monkeypatch.setattr(QApplication, "quit", lambda *a: None)
        window._settings_dialog = None
        assert window._close_settings_first() is True

    def test_the_quit_action_goes_through_the_checks(self, window, monkeypatch):
        """It used to be wired straight to QApplication.quit, which skipped
        the unapplied-scan warning as well as the Settings question.

        Proved by triggering the real action with Settings open: if it still
        went straight to the application, nothing would be asked.
        """
        asked = []
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *a, **k: (asked.append(True),
                             QMessageBox.StandardButton.Cancel)[1])
        quit_called = []
        monkeypatch.setattr(QApplication, "quit",
                            lambda *a: quit_called.append(True))
        dialog = self._open_settings(window)
        try:
            actions = [a for a in window.findChildren(QAction)
                       if a.text() == "&Quit"]
            assert actions, "no Quit action found"
            actions[0].trigger()
            assert asked, "Quit bypassed the window's own checks"
            assert not quit_called, "it left anyway after Cancel"
        finally:
            window._settings_dialog = None
            dialog.deleteLater()


class TestADeletedSettingsDialogDoesNotTakeTheAppWithIt:
    """Qt calls qFatal when a running QThread is destroyed.

    A dialog destroys its children, and Settings parents its workers to
    itself, so a probe still in flight when the dialog went away aborted
    the process - no exception, no traceback, just SIGABRT. done() and
    closeEvent() stopped the two workers somebody might want to be asked
    about; the quiet ones - reading the Keychain, listing models, probing
    an endpoint - were stopped nowhere, and neither hook runs at all when
    a dialog is deleted rather than closed.

    It reached CI as a worker crashing mid-file, which reads like anything
    at all.
    """

    class _Slow(_BaseWorker):
        """Runs until asked to stop, so the race is not a race.

        It reports through a plain Event rather than its own state: the
        worker is a child of the dialog, so by the time the dialog has
        gone the C++ object has too and there is nothing left to ask.
        """

        def __init__(self, finished: threading.Event, parent=None) -> None:
            super().__init__(parent)
            self._finished = finished

        def run(self) -> None:
            try:
                for _ in range(200):
                    if (self.isInterruptionRequested()
                            or self.cancel_event.is_set()):
                        return
                    time.sleep(0.02)
            finally:
                self._finished.set()

    def _dialog(self, qapp, tmp_path, monkeypatch):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        return SettingsDialog(Settings(icloud_email="you@icloud.example"),
                              InMemoryCredentialStore())

    def test_deleting_it_stops_a_worker_still_running(self, qapp, tmp_path,
                                                      monkeypatch):
        from PySide6.QtCore import QEvent

        done = threading.Event()
        dialog = self._dialog(qapp, tmp_path, monkeypatch)
        worker = self._Slow(done, parent=dialog)
        worker.start()
        for _ in range(50):
            if worker.isRunning():
                break
            time.sleep(0.01)
        assert worker.isRunning(), "the worker never started, so nothing is proven"

        dialog.deleteLater()
        qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        qapp.processEvents()
        # Reaching this line at all is most of the assertion: the abort
        # took the whole process rather than raising anything catchable.
        assert done.wait(5.0), "the worker was still running when the dialog went"

    def test_closing_it_stops_a_worker_still_running(self, qapp, tmp_path,
                                                     monkeypatch):
        done = threading.Event()
        dialog = self._dialog(qapp, tmp_path, monkeypatch)
        worker = self._Slow(done, parent=dialog)
        worker.start()
        for _ in range(50):
            if worker.isRunning():
                break
            time.sleep(0.01)
        assert worker.isRunning()

        dialog.close()
        assert done.wait(5.0), "closing Settings left a worker running"
        assert not worker.isRunning()
        dialog.deleteLater()
