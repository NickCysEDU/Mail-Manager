"""./dev starts the app through macOS, from a small bundle (launch() in dev).
Run here for real in a pseudo-terminal, with `open` replaced by a function
that writes down what it was given and reads the environment file the way
the bundle's script does."""
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(sys.platform != "darwin",
                                reason="the launch is the Mac's")

#: Every kind of character a value has broken on: a ~ after a colon, both
#: quotes, a dollar, a backslash, a line break and something outside ASCII.
AWKWARD = "a b'c\"d$e\\f\ng ü ~/x :~/y ''"


def _launch_function() -> str:
    lines = (ROOT / "dev").read_text().splitlines()
    start = lines.index("launch() {")
    return "\n".join(lines[start:lines.index("}", start) + 1])


@pytest.fixture(scope="module")
def launched(tmp_path_factory):
    home = tmp_path_factory.mktemp("checkout")
    script = home / "launch.sh"
    script.write_text(f"""set -euo pipefail
ROOT="{home}"
cd "$ROOT"
PY=false
{_launch_function()}
open() {{
  printf '%s\\0' "$@" > "$ROOT/open-args"
  local file="" next=0 arg
  for arg in "$@"; do
    if [[ $next == 1 ]]; then file="$arg"; break; fi
    [[ "$arg" == --args ]] && next=1
  done
  env -i /bin/bash -c 'source "$1"; env -0' _ "$file" > "$ROOT/app-env"
}}
launch --demo
""")
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:~/.local/bin",
           "HOME": str(home), "MM_AWKWARD": AWKWARD, "MM_EMPTY": "",
           "__CFBundleIdentifier": "com.example.terminal",
           "XPC_SERVICE_NAME": "0"}
    # A pseudo-terminal: away from one, launch() runs the old way.
    subprocess.run(["script", "-q", "/dev/null", "/bin/bash", str(script)],
                   env=env, stdin=subprocess.DEVNULL, check=True,
                   capture_output=True, timeout=60)
    told = (home / "open-args").read_bytes().split(b"\0")[:-1]
    passed = {}
    for item in (home / "app-env").read_bytes().split(b"\0"):
        if b"=" in item:
            name, value = item.split(b"=", 1)
            passed[name.decode()] = value.decode()
    return home, [arg.decode() for arg in told], passed, env


class TestTheSourceIsLaunchedAsAnApp:
    def test_it_is_never_run_under_rosetta(self, launched):
        home = launched[0]
        info = plistlib.loads((home / "build" / "dev.noindex"
                               / "Mail Manager (source).app" / "Contents"
                               / "Info.plist").read_bytes())
        assert info["LSRequiresNativeExecution"] is True
        assert info["LSArchitecturePriority"][0] == "arm64"
        assert info["CFBundleIdentifier"] != "com.mailmanager.icloudjobtriage"

    def test_open_waits_for_it_and_is_handed_the_arguments(self, launched):
        _home, told, _passed, _env = launched
        assert told[:2] == ["-W", "-n"]
        assert told[told.index("--args") + 2:] == ["--demo"]

    def test_the_environment_is_never_on_a_command_line(self, launched):
        _home, told, _passed, _env = launched
        assert "--env" not in told
        assert not any("MM_AWKWARD" in arg or "a b'c" in arg for arg in told)

    def test_it_reaches_the_app_exactly(self, launched):
        _home, _told, passed, env = launched
        assert passed["MM_AWKWARD"] == AWKWARD
        assert passed["MM_EMPTY"] == ""
        assert passed["PATH"] == env["PATH"]

    def test_but_not_what_names_the_terminal_s_own_app(self, launched):
        passed = launched[2]
        assert "__CFBundleIdentifier" not in passed
        assert "XPC_SERVICE_NAME" not in passed

    def test_the_file_it_went_in_is_gone(self, launched):
        home = launched[0]
        assert not list((home / "build" / "dev.noindex").glob("environment.*"))
