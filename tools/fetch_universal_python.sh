#!/usr/bin/env bash
# Fetch a universal2 Python into .toolchain, without needing admin rights.
#
# A Mac only ships one architecture of Python per install, and Homebrew builds
# for whichever architecture Homebrew itself is. Building an app that runs
# natively on both needs an interpreter that contains both, and python.org's
# installer is the one that does. This unpacks it into the project rather than
# installing it, so nothing outside this directory changes and removing
# .toolchain undoes all of it.
#
#   ./tools/fetch_universal_python.sh [version]
#
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION="${1:-3.11.9}"
TOOLCHAIN="$PWD/.toolchain"
FRAMEWORK="$TOOLCHAIN/Python.framework"
SHORT="${VERSION%.*}"
URL="https://www.python.org/ftp/python/${VERSION}/python-${VERSION}-macos11.pkg"

G=$'\033[32m'; R=$'\033[31m'; N=$'\033[0m'
say() { printf '%s==>%s %s\n' "$G" "$N" "$*" >&2; }
die() { printf '%serror:%s %s\n' "$R" "$N" "$*" >&2; exit 1; }

PY="$FRAMEWORK/Versions/$SHORT/bin/python$SHORT"
if [[ -x "$PY" ]] && "$PY" -c 'import ssl' 2>/dev/null; then
  say "Already present: $("$PY" -V 2>&1) ($(lipo -archs "$PY"))"
  exit 0
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

say "Downloading Python $VERSION (universal2)"
curl -fsSL --max-time 600 -o "$WORK/python.pkg" "$URL" || die "download failed: $URL"

say "Unpacking"
pkgutil --expand-full "$WORK/python.pkg" "$WORK/expanded" >/dev/null
PAYLOAD="$WORK/expanded/Python_Framework.pkg/Payload"
[[ -d "$PAYLOAD" ]] || die "the installer did not contain a framework payload"
rm -rf "$FRAMEWORK"
mkdir -p "$TOOLCHAIN"
cp -R "$PAYLOAD" "$FRAMEWORK"

# The framework records where it expects to live. Since it is living somewhere
# else, every reference to that path has to be rewritten or nothing loads.
say "Relocating it to $TOOLCHAIN"
/usr/bin/python3 - "$FRAMEWORK" <<'PYEOF'
import pathlib, subprocess, sys

root = pathlib.Path(sys.argv[1])
old = "/Library/Frameworks/Python.framework"
new = str(root)

def is_macho(path):
    try:
        with path.open("rb") as fh:
            return fh.read(4) in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe",
                                  b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca")
    except OSError:
        return False

changed = 0
for path in root.rglob("*"):
    if not path.is_file() or path.is_symlink() or not is_macho(path):
        continue
    listing = subprocess.run(["otool", "-L", str(path)],
                             capture_output=True, text=True).stdout
    refs = {line.split()[0] for line in listing.splitlines()[1:]
            if line.strip().startswith(old)}
    ident = subprocess.run(["otool", "-D", str(path)],
                           capture_output=True, text=True).stdout.splitlines()
    own = [l.strip() for l in ident[1:] if l.strip().startswith(old)]
    if not refs and not own:
        continue
    args = ["install_name_tool"]
    for value in own:
        args += ["-id", value.replace(old, new)]
    for ref in refs:
        args += ["-change", ref, ref.replace(old, new)]
    args.append(str(path))
    if subprocess.run(args, capture_output=True).returncode == 0:
        subprocess.run(["codesign", "-f", "-s", "-", str(path)], capture_output=True)
        changed += 1
print(f"    rewrote {changed} binaries")
PYEOF

"$PY" -c 'import ssl' 2>/dev/null || die "the relocated interpreter cannot import ssl"
say "Ready: $("$PY" -V 2>&1) ($(lipo -archs "$PY"))"
echo
echo "  Build a universal app with it:"
echo "    PYTHON_BIN=\"$PY\" ./build_app.sh"
