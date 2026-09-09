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


class TestTerminalOutput:
    """Ollama draws a redrawn frame, not a log. Most of its output is escapes."""

    def test_escape_codes_never_reach_the_reader(self):
        raw = "\x1b[?2026h\x1b[?25l\x1b[1Gpulling manifest ⠙ \x1b[K\x1b[?25h\x1b[?2026l"
        assert ondevice.clean_terminal_text(raw) == "pulling manifest"

    def test_a_redrawn_frame_is_split_into_its_rows(self):
        """A frame carries the heading and the bar; one line would hide one.

        This is why a two-gigabyte download reported "reading the manifest"
        from beginning to end: the heading came first in the same blob.
        """
        seen = []
        frame = ("pulling dde5aa3fc5ff:  21% 429 MB/2.0 GB\x1b[K"
                 "\x1b[A\x1b[1Gpulling manifest \x1b[K")
        ondevice._emit(frame.encode(), seen.append, final=True)
        assert seen == ["pulling dde5aa3fc5ff:  21% 429 MB/2.0 GB",
                        "pulling manifest"]

    def test_a_spinner_is_not_a_label(self):
        assert "⠙" not in ondevice.clean_terminal_text("pulling manifest ⠙")


class TestProgressReader:
    def _feed(self, lines):
        reader = ondevice.ProgressReader()
        return [reader.feed(line) for line in lines]

    def test_bytes_drive_the_bar(self):
        out = self._feed(["pulling abc123def456:  25% ▕██▏ 500 MB/2.0 GB"])
        assert out[0][0] == pytest.approx(25.0, abs=0.5)
        assert out[0][1] == "Downloading 500 MB of 2 GB"

    def test_a_redrawn_heading_does_not_drag_it_back(self):
        """The heading arrives between every update of the bar."""
        reader = ondevice.ProgressReader()
        reader.feed("pulling abc123def456:  60% 1.2 GB/2.0 GB")
        before = reader.percent
        assert reader.feed("pulling manifest") is None
        assert reader.percent == before

    def test_it_never_goes_backwards(self):
        reader = ondevice.ProgressReader()
        seen = []
        for line in ("pulling manifest",
                     "pulling aaa111bbb222:  40% 800 MB/2.0 GB",
                     "pulling aaa111bbb222:  10% 200 MB/2.0 GB",
                     "verifying sha256 digest",
                     "writing manifest", "success"):
            out = reader.feed(line)
            if out:
                seen.append(out[0])
        assert seen == sorted(seen), seen

    def test_a_small_layer_does_not_restart_the_bar(self):
        """A model is one big file and a few tiny ones."""
        reader = ondevice.ProgressReader()
        reader.feed("pulling aaa111bbb222:  50% 1.0 GB/2.0 GB")
        assert reader.feed("pulling ccc333ddd444:  10% 1 KB/12 KB") is None
        assert reader.percent == pytest.approx(50.0, abs=0.5)

    def test_finishing_reaches_a_hundred(self):
        reader = ondevice.ProgressReader()
        reader.feed("pulling aaa111bbb222:  50% 1.0 GB/2.0 GB")
        assert reader.finish() == (100.0, "Finished.")

    @pytest.mark.parametrize("junk", ["", "   ", "%%%", "\x00", "MB/GB",
                                      "999999 ZB/1 QB", "-5 MB/2 GB"])
    def test_junk_does_not_raise(self, junk):
        ondevice.ProgressReader().feed(junk)


class TestInstallCandidates:
    def test_more_than_one_name_is_offered(self):
        """The cask was renamed from "ollama" to "ollama-app" once already."""
        assert ondevice.BREW_CASKS[0] == "ollama-app"
        assert "ollama" in ondevice.BREW_CASKS

    @pytest.mark.parametrize("output", [
        "Warning: Cask 'x' is unavailable: No Cask with this name exists.",
        "Error: No casks found for x.",
        "Error: No formulae or casks found for x.",
        "Warning: No available formula with the name \"x\".",
    ])
    def test_homebrew_saying_it_never_heard_of_it(self, output):
        assert ondevice.is_unknown_package(output) is True

    @pytest.mark.parametrize("output", [
        "Error: no space left on device",
        "==> Downloading https://example.com/x.tar.gz",
        "curl: (6) Could not resolve host",
        "",
    ])
    def test_other_failures_are_not_worth_another_name(self, output):
        assert ondevice.is_unknown_package(output) is False


class TestPlainLanguageErrors:
    @pytest.mark.parametrize("output, expected", [
        ("Error: pull model manifest: file does not exist", "no model by that name"),
        ("Error: could not connect to ollama app", "server is not running"),
        ("write /blobs: no space left on device", "disk space"),
        ("Error: No casks found for ollama", "does not have a package"),
    ])
    def test_a_known_failure_is_explained(self, output, expected):
        assert expected in ondevice.explain(output)

    def test_an_unknown_failure_invents_nothing(self):
        assert ondevice.explain("something nobody has ever seen") == ""


class TestStartingTheServer:
    def test_the_app_is_preferred_over_serve(self, monkeypatch):
        """A second `ollama serve` exits with "address already in use"."""
        monkeypatch.setattr(ondevice, "find_app", lambda: "/Applications/Ollama.app")
        monkeypatch.setattr(ondevice, "find_binary", lambda: "/usr/local/bin/ollama")
        assert ondevice.start_command()[:2] == ["/usr/bin/open", "-a"]

    def test_serve_is_the_fallback(self, monkeypatch):
        monkeypatch.setattr(ondevice, "find_app", lambda: None)
        monkeypatch.setattr(ondevice, "find_binary", lambda: "/usr/local/bin/ollama")
        assert ondevice.start_command() == ["/usr/local/bin/ollama", "serve"]

    def test_nothing_installed_means_no_command(self, monkeypatch):
        monkeypatch.setattr(ondevice, "find_app", lambda: None)
        monkeypatch.setattr(ondevice, "find_binary", lambda: None)
        assert ondevice.start_command() is None

    def test_waiting_gives_up_rather_than_hanging(self):
        started = time.perf_counter()
        assert ondevice.wait_until_answering("http://127.0.0.1:1",
                                             timeout=1.5) is False
        assert time.perf_counter() - started < 5.0

    def test_waiting_can_be_stopped(self):
        cancel = threading.Event()
        threading.Timer(0.4, cancel.set).start()
        started = time.perf_counter()
        assert ondevice.wait_until_answering("http://127.0.0.1:1", timeout=30,
                                             cancel=cancel) is False
        assert time.perf_counter() - started < 3.0


class TestListingAndRemoving:
    """Seeing what is installed, and getting rid of it.

    Downloading was possible from the start; nothing showed what you had, so
    a Mac filled up several gigabytes at a time with nothing saying so.
    """

    def test_a_model_describes_itself(self):
        model = ondevice.Model(name="llama3.2:3b", size=2_019_393_189,
                               parameters="3.2B", quantisation="Q4_K_M")
        assert "2 GB" in model.size_text
        assert "3.2B" in model.describe() and "Q4_K_M" in model.describe()

    def test_loaded_and_unloaded_read_differently(self):
        assert ondevice.Model(loaded=True).status_text == "in memory, ready"
        assert ondevice.Model(loaded=False).status_text == "on disk"

    def test_an_unknown_size_does_not_print_a_zero(self):
        assert ondevice.Model(name="x").size_text == "unknown size"

    def test_listing_a_server_that_is_not_there_returns_a_reason(self):
        models, error = ondevice.installed_models("http://127.0.0.1:1", timeout=1.0)
        assert models == [] and error

    @pytest.mark.parametrize("name", ["", "   ", "-rf", "--all"])
    def test_a_name_that_is_not_a_name_gets_no_command(self, name):
        assert ondevice.remove_command(name) is None

    def test_a_hostile_name_is_one_argument_not_a_shell(self, monkeypatch):
        """`ollama rm "; rm -rf ~"` is a model that does not exist."""
        monkeypatch.setattr(ondevice, "find_binary", lambda: "/usr/local/bin/ollama")
        command = ondevice.remove_command("; rm -rf ~")
        assert command == ["/usr/local/bin/ollama", "rm", "; rm -rf ~"]
        assert len(command) == 3, "the name must stay a single argument"

    def test_nothing_installed_means_no_remove_command(self, monkeypatch):
        monkeypatch.setattr(ondevice, "find_binary", lambda: None)
        assert ondevice.remove_command("llama3.2:3b") is None
