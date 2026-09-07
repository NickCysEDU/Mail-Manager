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
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

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
    """Run one of the commands above. Returns (ok, output)."""
    if not command:
        return False, "Nothing to run."
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=timeout)
    except FileNotFoundError:
        return False, f"{command[0]} is not on this Mac."
    except subprocess.TimeoutExpired:
        return False, "Timed out."
    except OSError as exc:
        return False, str(exc)
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode == 0, output.strip()
