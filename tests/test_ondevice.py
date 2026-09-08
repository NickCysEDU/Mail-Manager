"""Installing a model must never freeze the window.

This used to be `subprocess.run` on the UI thread with a ten-minute timeout,
so `brew install ollama` beachballed the app for the whole install, said
nothing while it did, and could not be stopped. From the outside that is
indistinguishable from a crash.
"""

from __future__ import annotations

import subprocess
import threading
import time

import pytest

import ondevice


class TestStreaming:
    def test_output_arrives_line_by_line(self):
        seen = []
        assert ondevice.stream(
            ["/bin/sh", "-c", "echo one; echo two; echo three"], seen.append)
        assert seen == ["one", "two", "three"]

    def test_a_carriage_return_ends_a_line_too(self):
        """Ollama draws its progress bar with \\r and no newline.

        Waiting for a newline means a two-gigabyte download is one line that
        arrives once it has finished.
        """
        seen = []
        started = time.perf_counter()
        ondevice.stream(
            ["/bin/sh", "-c",
             r'printf "10%%\r"; sleep 0.3; printf "90%%\r"; sleep 0.3; printf "done\n"'],
            lambda line: seen.append((time.perf_counter() - started, line)))
        assert [line for _at, line in seen] == ["10%", "90%", "done"]
        # The first one has to arrive long before the command ends.
        assert seen[0][0] < 0.25, "output was buffered until the end"

    def test_a_final_line_without_a_newline_is_not_lost(self):
        seen = []
        ondevice.stream(["/usr/bin/printf", "no trailing newline"], seen.append)
        assert seen == ["no trailing newline"]

    def test_a_failing_command_reports_false(self):
        seen = []
        assert ondevice.stream(["/bin/sh", "-c", "echo oops >&2; exit 3"],
                               seen.append) is False
        assert seen == ["oops"]

    def test_a_missing_binary_is_said_in_words(self):
        seen = []
        assert ondevice.stream(["/nope/nothing"], seen.append) is False
        assert "not on this Mac" in seen[0]

    def test_an_empty_command_does_nothing(self):
        seen = []
        assert ondevice.stream([], seen.append) is False
        assert seen == ["Nothing to run."]


class TestStopping:
    def test_a_silent_command_still_stops(self):
        """The one case somebody most wants to cancel: a stalled download.

        Iterating the pipe blocks until a line arrives, so a command that has
        gone quiet could not be cancelled at all.
        """
        cancel = threading.Event()
        threading.Timer(0.4, cancel.set).start()
        started = time.perf_counter()
        ok = ondevice.stream(["/bin/sh", "-c", "echo starting; sleep 60"],
                             lambda _line: None, cancel=cancel)
        assert ok is False
        assert time.perf_counter() - started < 4.0

    def test_stopping_takes_the_children_with_it(self):
        """brew spawns curl and git; killing only brew leaves them running."""
        seen = []
        cancel = threading.Event()
        threading.Timer(0.6, cancel.set).start()
        ondevice.stream(
            ["/bin/sh", "-c", 'sleep 60 & echo "child $!"; sleep 60'],
            seen.append, cancel=cancel)
        kids = [line.split()[-1] for line in seen if line.startswith("child")]
        assert kids, "the test command did not report its child"
        time.sleep(0.4)
        alive = subprocess.run(["ps", "-p", kids[0]],
                               capture_output=True).returncode == 0
        assert not alive, f"child {kids[0]} outlived the cancel"

    def test_a_command_that_never_ends_hits_the_timeout(self):
        seen = []
        started = time.perf_counter()
        assert ondevice.stream(["/bin/sh", "-c", "sleep 60"], seen.append,
                               timeout=1.0) is False
        assert time.perf_counter() - started < 4.0
        assert any("Giving up" in line for line in seen)

    def test_cancelling_before_it_starts_is_harmless(self):
        cancel = threading.Event()
        cancel.set()
        assert ondevice.stream(["/bin/sh", "-c", "sleep 30"],
                               lambda _l: None, cancel=cancel) is False


class TestProgressParsing:
    @pytest.mark.parametrize("line, percent", [
        ("pulling manifest", 2.0),
        ("pulling dde5aa3fc5ff... 100% |####| 2.0 GB", 100.0),
        ("pulling dde5aa3fc5ff...  47% |##  | 1.2 GB/2.7 GB  12 MB/s", 47.0),
        ("verifying sha256 digest", 70.0),
        ("writing manifest", 95.0),
        ("success", 100.0),
        ("==> Downloading https://formulae.brew.sh/api/cask.jws.json", 5.0),
        ("==> Installing Cask ollama", 75.0),
    ])
    def test_a_percentage_is_found(self, line, percent):
        assert ondevice.parse_progress(line)[0] == percent

    @pytest.mark.parametrize("line", [
        "", "   ", "Warning: something odd happened",
        "150%", "-5%", "🍺  ollama was successfully installed!",
    ])
    def test_nothing_is_invented_when_it_cannot_tell(self, line):
        """None means a busy bar. A bar stuck at nought reads as broken."""
        assert ondevice.parse_progress(line)[0] is None

    def test_the_number_is_not_repeated_in_the_label(self):
        percent, label = ondevice.parse_progress(
            "pulling dde5aa3fc5ff...  47% |##  | 1.2 GB")
        assert percent == 47.0
        assert "47" not in label

    @pytest.mark.parametrize("junk", [
        "\x00\x01", "%" * 400, "a" * 5000, "99999999%", "% % % %",
    ])
    def test_junk_does_not_raise(self, junk):
        ondevice.parse_progress(junk)


class TestProbing:
    def test_a_refused_port_answers_at_once(self):
        started = time.perf_counter()
        running, _models, error = ondevice.probe("http://127.0.0.1:1", timeout=2.0)
        assert running is False and error
        assert time.perf_counter() - started < 1.0

    def test_a_bad_endpoint_never_raises(self):
        for endpoint in ("", "not a url", "http://", "ftp://x", "http://x.invalid"):
            running, models, _error = ondevice.probe(endpoint, timeout=0.5)
            assert running is False and models == []
