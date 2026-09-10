"""Finding, installing and starting Ollama.

Picking "On this Mac" and getting a connection error is a dead end for anybody
who has not already set Ollama up, and it is the one backend where the fix is
entirely local. This works out what is missing - the app, the server, the model
- and does the part it can do.

Nothing here installs anything without being asked. The functions report, and
the caller decides.
"""

from __future__ import annotations

import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

DEFAULT_ENDPOINT = "http://127.0.0.1:11434"
DOWNLOAD_URL = "https://ollama.com/download"
#: What Homebrew calls it, canonical name first. It was renamed from "ollama"
#: to "ollama-app" and the old name survives only as an alias, which is the
#: sort of thing that stops working without warning. Each is tried in turn.
BREW_CASKS: Tuple[str, ...] = ("ollama-app", "ollama")

#: Kept for anything still importing it.
BREW_FORMULA = BREW_CASKS[-1]

#: Failures worth trying the next candidate for, rather than giving up.
#: Homebrew's exact wording, taken from running it rather than remembered:
#:   Warning: Cask 'x' is unavailable: No Cask with this name exists.
#:   Error: No casks found for x.
#:   Error: No formulae or casks found for x.
_UNKNOWN_CASK = re.compile(
    r"(?:no (?:cask|formula) with this name exists|"
    r"no casks? found for|no formulae or casks found|"
    r"no available (?:cask|formula)|is unavailable:|"
    r"unknown (?:command|cask|formula))", re.I)


def is_unknown_package(output: str) -> bool:
    """Whether Homebrew simply does not know that name."""
    return bool(_UNKNOWN_CASK.search(output or ""))

#: Where the app puts its binary, plus the usual package-manager locations.
_BINARY_CANDIDATES = (
    "/usr/local/bin/ollama",
    "/opt/homebrew/bin/ollama",
    "/Applications/Ollama.app/Contents/Resources/ollama",
)
_APP_CANDIDATES = (
    "/Applications/Ollama.app",
    str(Path.home() / "Applications" / "Ollama.app"),
)


@dataclass
class Status:
    """What is present, and therefore what is missing."""

    binary: Optional[str] = None
    app: Optional[str] = None
    running: bool = False
    models: List[str] = field(default_factory=list)
    error: str = ""

    @property
    def installed(self) -> bool:
        return bool(self.binary or self.app)

    @property
    def ready(self) -> bool:
        return self.running and bool(self.models)

    def describe(self) -> str:
        if self.ready:
            return f"Ollama is running with {len(self.models)} model(s) installed."
        if self.running:
            return "Ollama is running but has no models yet."
        if self.installed:
            return "Ollama is installed but not running."
        return "Ollama is not installed on this Mac."

    def next_step(self) -> str:
        """The single thing that would move this forward."""
        if self.ready:
            return ""
        if not self.installed:
            return "install"
        if not self.running:
            return "start"
        return "pull"


def find_binary() -> Optional[str]:
    found = shutil.which("ollama")
    if found:
        return found
    for candidate in _BINARY_CANDIDATES:
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def find_app() -> Optional[str]:
    return next((path for path in _APP_CANDIDATES if Path(path).is_dir()), None)


def probe(endpoint: str = DEFAULT_ENDPOINT, timeout: float = 2.0) -> Tuple[bool, List[str], str]:
    """Ask the local server what it has. Never raises."""
    url = endpoint.rstrip("/") + "/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.URLError as exc:
        return False, [], str(getattr(exc, "reason", exc))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return False, [], str(exc)
    models = [
        str(entry.get("name", "")).strip()
        for entry in (payload.get("models") or [])
        if entry.get("name")
    ]
    return True, models, ""


@dataclass
class Model:
    """One model the local server holds."""

    name: str = ""
    size: int = 0
    family: str = ""
    parameters: str = ""
    quantisation: str = ""
    modified: str = ""
    loaded: bool = False

    @property
    def size_text(self) -> str:
        return _human(float(self.size)) if self.size else "unknown size"

    def describe(self) -> str:
        bits = [self.size_text]
        if self.parameters:
            bits.append(self.parameters)
        if self.quantisation:
            bits.append(self.quantisation)
        return " · ".join(bits)

    @property
    def status_text(self) -> str:
        return "in memory, ready" if self.loaded else "on disk"


def installed_models(endpoint: str = DEFAULT_ENDPOINT,
                     timeout: float = 4.0) -> Tuple[List[Model], str]:
    """Every model on this machine, with what is known about each.

    Returns (models, error). Never raises: the panel that shows this must
    not be able to take the window down.
    """
    payload, error = _ask(endpoint, "/api/tags", timeout)
    if error:
        return [], error
    running = {name for name in _loaded(endpoint, timeout)}
    found: List[Model] = []
    for entry in (payload.get("models") or []):
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or entry.get("model") or "").strip()
        if not name:
            continue
        details = entry.get("details") or {}
        found.append(Model(
            name=name,
            size=int(entry.get("size") or 0),
            family=str(details.get("family") or ""),
            parameters=str(details.get("parameter_size") or ""),
            quantisation=str(details.get("quantization_level") or ""),
            modified=str(entry.get("modified_at") or "")[:10],
            loaded=name in running,
        ))
    found.sort(key=lambda m: m.name)
    return found, ""


def _loaded(endpoint: str, timeout: float) -> List[str]:
    """Which models are in memory right now. Empty when it cannot tell."""
    payload, error = _ask(endpoint, "/api/ps", timeout)
    if error:
        return []
    return [str(e.get("name") or "") for e in (payload.get("models") or [])
            if isinstance(e, dict)]


def _ask(endpoint: str, path: str, timeout: float) -> Tuple[dict, str]:
    """One GET against the local server. Never raises."""
    url = (endpoint or DEFAULT_ENDPOINT).rstrip("/") + path
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace")), ""
    except urllib.error.URLError as exc:
        return {}, str(getattr(exc, "reason", exc))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {}, str(exc)


def remove_command(name: str) -> Optional[List[str]]:
    """The command that deletes a model, if Ollama is installed.

    The name is passed as its own argument and never through a shell, so a
    model called ``; rm -rf ~`` is just a name that does not exist.
    """
    binary = find_binary()
    cleaned = (name or "").strip()
    if not binary or not cleaned or cleaned.startswith("-"):
        return None
    return [binary, "rm", cleaned]


def status(endpoint: str = DEFAULT_ENDPOINT) -> Status:
    running, models, error = probe(endpoint)
    return Status(binary=find_binary(), app=find_app(), running=running,
                  models=models, error=error)


def homebrew() -> Optional[str]:
    """Homebrew's path, if it is installed. Used to offer a one-click install."""
    for candidate in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew"):
        if os.access(candidate, os.X_OK):
            return candidate
    return shutil.which("brew")


def install_command() -> Optional[List[str]]:
    """The command that would install Ollama, if one is available."""
    candidates = install_commands()
    return candidates[0] if candidates else None


def install_commands() -> List[List[str]]:
    """Every command that might install Ollama, best first.

    More than one because the cask has been renamed once already. Trying the
    next name when Homebrew says it has never heard of the first costs a
    second and removes a whole class of "it just did not work".
    """
    brew = homebrew()
    if not brew:
        return []
    return [[brew, "install", "--cask", cask] for cask in BREW_CASKS]


def start_command() -> Optional[List[str]]:
    """The command that would start the server, if Ollama is installed.

    The app is preferred over ``ollama serve`` when it is there. Opening it
    twice is harmless, whereas a second ``serve`` exits with "address already
    in use" - and it manages the server the way somebody who installed the app
    expects, including starting it again after a reboot.
    """
    app = find_app()
    if app:
        return ["/usr/bin/open", "-a", app]
    binary = find_binary()
    return [binary, "serve"] if binary else None


def wait_until_answering(endpoint: str = DEFAULT_ENDPOINT, timeout: float = 25.0,
                         cancel: Optional[threading.Event] = None,
                         on_wait: Optional[Callable[[float], None]] = None) -> bool:
    """Poll until the server answers, or give up. Never raises.

    Starting it and waiting a fixed few seconds is a guess, and it was wrong
    in both directions: too short on a cold start, and it reported success
    when nothing had come up at all.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cancel is not None and cancel.is_set():
            return False
        running, _models, _error = probe(endpoint, timeout=1.5)
        if running:
            return True
        if on_wait is not None:
            on_wait(max(0.0, deadline - time.monotonic()))
        time.sleep(0.5)
    return probe(endpoint, timeout=2.0)[0]


def pull_command(model: str) -> Optional[List[str]]:
    binary = find_binary()
    return [binary, "pull", model] if binary else None


def run(command: List[str], timeout: float = 600.0) -> Tuple[bool, str]:
    """Run one of the commands above and wait. Returns (ok, output).

    Blocks for as long as the command takes, which for ``brew install`` is
    minutes. Never call this from a thread that is drawing a window - use
    :func:`stream`, which is what the settings panel does.
    """
    lines: List[str] = []
    ok = stream(command, lines.append, timeout=timeout)
    return ok, "\n".join(lines).strip()


def stream(command: List[str], on_line: Callable[[str], None],
           cancel: Optional[threading.Event] = None,
           timeout: float = 900.0, poll: float = 0.2) -> bool:
    """Run a command, handing back each line as it arrives. Never raises.

    Downloading a model is a progress bar on stdout and installing one is a
    Homebrew log; both take minutes, and a window that says nothing for four
    minutes is indistinguishable from a window that has crashed. So output is
    reported as it happens, and the whole thing can be stopped.

    The pipe is polled rather than iterated. Iterating blocks until a line
    arrives, which means a download that stalls cannot be cancelled - the one
    case where somebody most wants to cancel it. Polling checks the stop flag
    every fifth of a second whether or not anything was written.

    Ollama writes its progress bar with carriage returns and no newline, so
    ``\r`` ends a line here too; otherwise a two-gigabyte download is a single
    line that arrives when it has finished.
    """
    if not command:
        on_line("Nothing to run.")
        return False
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=0,
            # Its own group, so stopping it stops anything it started.
            start_new_session=True,
        )
    except FileNotFoundError:
        on_line(f"{command[0]} is not on this Mac.")
        return False
    except OSError as exc:
        on_line(str(exc))
        return False

    deadline = time.monotonic() + timeout
    stopped = False
    pending = b""
    selector = selectors.DefaultSelector()
    try:
        assert process.stdout is not None
        os.set_blocking(process.stdout.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ)
        while True:
            if cancel is not None and cancel.is_set():
                stopped = True
                break
            if time.monotonic() > deadline:
                on_line("Giving up: it has been "
                        + (f"{timeout / 60:.0f} minutes." if timeout >= 60
                           else f"{timeout:.0f} seconds."))
                stopped = True
                break
            for _key, _events in selector.select(timeout=poll):
                try:
                    chunk = process.stdout.read(65536)
                except (BlockingIOError, InterruptedError):
                    chunk = b""
                except (OSError, ValueError):
                    chunk = b""
                if chunk:
                    pending += chunk
                    pending = _emit(pending, on_line)
            if process.poll() is not None:
                # Drain whatever is left now the writer has gone.
                try:
                    while True:
                        chunk = process.stdout.read(65536)
                        if not chunk:
                            break
                        pending += chunk
                except (OSError, ValueError, BlockingIOError):
                    pass
                pending = _emit(pending, on_line, final=True)
                break
    finally:
        try:
            selector.close()
        except OSError:                       # pragma: no cover
            pass
        if stopped:
            _stop(process)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:     # pragma: no cover - very rare
            _stop(process)
        try:
            if process.stdout is not None:
                process.stdout.close()
        except OSError:                       # pragma: no cover
            pass
    if stopped:
        return False
    return process.returncode == 0


#: A percentage anywhere in a line. Ollama's pull writes "… 47% ▕███ ▏ 1.2 GB".
_PERCENT = re.compile(r"(?<![-\d.])(\d{1,3}(?:\.\d+)?)\s*%")

#: How far along a phase is, for the tools that report phases and not numbers.
#: Homebrew says what it is doing and never how far through it is, so a bar
#: driven only by percentages would sit at zero for the whole install. These
#: are honest about being coarse: each is the *start* of that phase.
_PHASES: Tuple[Tuple["re.Pattern", float, str], ...] = (
    (re.compile(r"==> *downloading", re.I), 5.0, "Downloading"),
    (re.compile(r"==> *fetching", re.I), 5.0, "Fetching"),
    (re.compile(r"==> *verifying|verifying sha256", re.I), 70.0, "Verifying"),
    (re.compile(r"==> *installing|==> *pouring", re.I), 75.0, "Installing"),
    (re.compile(r"==> *linking|==> *caveats|==> *summary", re.I), 92.0, "Linking"),
    (re.compile(r"pulling manifest", re.I), 2.0, "Reading the manifest"),
    (re.compile(r"writing manifest", re.I), 95.0, "Writing the manifest"),
    (re.compile(r"removing any unused layers", re.I), 97.0, "Tidying up"),
    (re.compile(r"^success", re.I), 100.0, "Done"),
)


def parse_progress(line: str) -> Tuple[Optional[float], str]:
    """How far along this line says we are, and what is happening.

    Returns (percent or None, label). None means the tool did not say, which
    the window shows as a busy bar rather than as nought per cent - a bar
    stuck at zero for four minutes is how a working install comes to look
    like a broken one.
    """
    text = (line or "").strip()
    if not text:
        return None, ""
    for pattern, at, label in _PHASES:
        if pattern.search(text):
            found = _PERCENT.search(text)
            if found:
                try:
                    return max(0.0, min(100.0, float(found.group(1)))), label
                except ValueError:            # pragma: no cover
                    pass
            return at, label
    found = _PERCENT.search(text)
    if found:
        try:
            percent = float(found.group(1))
        except ValueError:                    # pragma: no cover
            return None, text[:80]
        if 0.0 <= percent <= 100.0:
            # Ollama names the layer it is pulling; the digest is noise.
            label = re.sub(r"\b[0-9a-f]{12,}\b[.:]*", "", text.split("%")[0])
            label = re.sub(r"[▕▏█\s]+", " ", label)
            # The number is already in the bar; leaving it in the label makes
            # a line reading "pulling 47".
            label = re.sub(r"[\s.]*\d+(?:\.\d+)?\s*$", "", label)
            label = label.strip(" .:…-")
            # A layer's own line is "pulling <digest>: 100%", and with the
            # digest and the number gone there is nothing left worth showing.
            if label.lower() in ("", "pulling", "verifying", "writing"):
                label = "Downloading"
            return percent, label[:80]
    return None, text[:80]


#: Everything a terminal program writes that is not text. Ollama draws its
#: progress with a redrawn multi-row frame, so this is most of its output.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b[()][A-Z0-9]|\x1b[=>]")

#: The cursor moves that start a new row: column 1, up, down, next line. A
#: program redrawing a frame in place uses these where a script would use a
#: newline, and without treating them as one, a whole frame - the progress bar
#: and the heading above it - arrives as a single line, and whichever row is
#: read first wins. That is why a two-gigabyte download reported "reading the
#: manifest" from beginning to end.
_CURSOR_BREAK = re.compile(r"\x1b\[[0-9]*[GAEF]")

#: A spinner frame. Braille dots and the usual ASCII wheel, which otherwise
#: end up in the label.
_SPINNER = re.compile(r"[\u2800-\u28ff]|(?<= )[|/\\-](?= )")


def clean_terminal_text(text: str) -> str:
    """Strip the escape codes, leaving what a person would have read."""
    return _SPINNER.sub("", _ANSI.sub("", text)).strip()


#: "830 MB/2.0 GB" - how far through the current layer, in bytes.
_SIZES = re.compile(
    r"([\d.]+)\s*([KMGT]?B)\s*/\s*([\d.]+)\s*([KMGT]?B)", re.I)
_UNITS = {"b": 1, "kb": 10 ** 3, "mb": 10 ** 6, "gb": 10 ** 9, "tb": 10 ** 12}


def _bytes(amount: str, unit: str) -> Optional[float]:
    try:
        return float(amount) * _UNITS[unit.lower()]
    except (ValueError, KeyError):        # pragma: no cover - regex-guarded
        return None


def _human(size: float) -> str:
    for unit, cutoff in (("GB", 10 ** 9), ("MB", 10 ** 6), ("KB", 10 ** 3)):
        if size >= cutoff:
            return f"{size / cutoff:.1f} {unit}".replace(".0 ", " ")
    return f"{size:.0f} B"


class ProgressReader:
    """Turns a download's chatter into one steady percentage and one label.

    Ollama redraws a frame of several rows, so the heading it wrote first
    arrives again between every update of the bar underneath it. Reporting
    whatever the newest row says makes the display flicker between "reading
    the manifest" at two per cent and the real figure - which is what the
    progress bar looked like.

    So this keeps state. A row carrying bytes always wins over a row carrying
    only a phase; the percentage never goes backwards; and the size of the
    largest layer drives the bar, because a model is one big file and a
    handful of small ones and a bar that restarts for each reads as a
    failure.
    """

    def __init__(self) -> None:
        self.percent = 0.0
        self.label = ""
        self._largest_total = 0.0
        self._done_bytes = 0.0

    def feed(self, line: str) -> Optional[Tuple[float, str]]:
        """Read one line. Returns the new (percent, label), or None."""
        text = (line or "").strip()
        if not text:
            return None

        found = _SIZES.search(text)
        if found:
            done = _bytes(found.group(1), found.group(2))
            total = _bytes(found.group(3), found.group(4))
            if done is not None and total and total > 0:
                # A model is one big file plus a few small ones. The big one
                # is the download; the rest are noise on the bar.
                if total >= self._largest_total * 0.9:
                    self._largest_total = max(self._largest_total, total)
                    self._done_bytes = max(self._done_bytes, done)
                    percent = min(100.0, self._done_bytes / self._largest_total * 100.0)
                    self._advance(percent)
                    self.label = (f"Downloading {_human(self._done_bytes)} "
                                  f"of {_human(self._largest_total)}")
                    return self.percent, self.label
                return None

        percent, label = parse_progress(text)
        if percent is None:
            return None
        # A bare phase must not undo a download that is already under way.
        if self._largest_total and percent < self.percent:
            return None
        self._advance(percent)
        self.label = label or self.label
        return self.percent, self.label

    def _advance(self, percent: float) -> None:
        self.percent = max(self.percent, min(100.0, percent))

    def finish(self) -> Tuple[float, str]:
        self.percent = 100.0
        return self.percent, "Finished."


def _emit(pending: bytes, on_line: Callable[[str], None],
          final: bool = False) -> bytes:
    """Hand over every complete line in the buffer, keeping any remainder."""
    text = pending.decode("utf-8", "replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CURSOR_BREAK.sub("\n", text)
    *lines, remainder = text.split("\n")
    if final and remainder:
        lines.append(remainder)
        remainder = ""
    for line in lines:
        cleaned = clean_terminal_text(line)
        if cleaned:
            on_line(cleaned)
    return remainder.encode("utf-8", "replace")


#: What the tools say, and what it means. Left as (pattern, explanation) so
#: the original line is still shown underneath - a translation that is wrong
#: is worse than none, and the reader can always see what was actually said.
_EXPLANATIONS: Tuple[Tuple["re.Pattern", str], ...] = (
    (re.compile(r"pull model manifest.*(?:file does not exist|not found)", re.I),
     "Ollama has no model by that name. Check the spelling against "
     "ollama.com/library, or pick another from the Model list."),
    (re.compile(r"connection refused|could not connect|dial tcp", re.I),
     "Ollama is installed but its server is not running. Start it first."),
    (re.compile(r"no space left|not enough space|disk full", re.I),
     "There is not enough disk space for the model."),
    (re.compile(r"permission denied|operation not permitted", re.I),
     "macOS refused the install. Running Homebrew once in Terminal usually "
     "says what it wants."),
    (_UNKNOWN_CASK,
     "Homebrew does not have a package by that name any more. Install Ollama "
     "from ollama.com/download instead."),
    (re.compile(r"already installed", re.I),
     "It is already installed. Press Test to check the server answers."),
    (re.compile(r"(?:network|timeout|timed out|temporary failure|"
                r"could not resolve)", re.I),
     "The download could not reach the network. Check the connection and "
     "try again."),
)


def explain(output: str) -> str:
    """A plain sentence for a known failure, or empty when there isn't one."""
    for pattern, meaning in _EXPLANATIONS:
        if pattern.search(output or ""):
            return meaning
    return ""


def _stop(process: "subprocess.Popen") -> None:
    """SIGTERM the whole group, then SIGKILL if it is still there."""
    for signal_number in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(process.pid), signal_number)
        except (OSError, ProcessLookupError):
            return
        try:
            process.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue
