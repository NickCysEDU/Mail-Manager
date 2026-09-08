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
BREW_FORMULA = "ollama"

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
    brew = homebrew()
    return [brew, "install", "--cask", BREW_FORMULA] if brew else None


def start_command() -> Optional[List[str]]:
    """The command that would start the server, if Ollama is installed."""
    binary = find_binary()
    if binary:
        return [binary, "serve"]
    app = find_app()
    return ["/usr/bin/open", "-a", app] if app else None


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
            label = re.sub(r"\b[0-9a-f]{12,}\b\.*", "", text.split("%")[0])
            label = re.sub(r"[▕▏█\s]+", " ", label)
            # The number is already in the bar; leaving it in the label makes
            # a line reading "pulling 47".
            label = re.sub(r"[\s.]*\d+(?:\.\d+)?\s*$", "", label).strip(" .…")
            return percent, (label or "Downloading")[:80]
    return None, text[:80]


def _emit(pending: bytes, on_line: Callable[[str], None],
          final: bool = False) -> bytes:
    """Hand over every complete line in the buffer, keeping any remainder."""
    pending = pending.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    *lines, remainder = pending.split(b"\n")
    if final and remainder:
        lines.append(remainder)
        remainder = b""
    for line in lines:
        text = line.decode("utf-8", "replace").strip()
        if text:
            on_line(text)
    return remainder


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
