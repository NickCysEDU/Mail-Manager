#!/usr/bin/env bash
# Build "Mail Manager.app" from a clean checkout.
#
#   ./build_app.sh            # venv + deps + icon + tests + bundle
#   ./build_app.sh --skip-tests
#
set -euo pipefail

cd "$(dirname "$0")"

PYTHON_BIN="${PYTHON_BIN:-}"
SKIP_TESTS=0
for arg in "$@"; do
  case "$arg" in
    --skip-tests) SKIP_TESTS=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

# --- 1. Find a Python 3.11+ interpreter -------------------------------------
# Homebrew keeps python@3.x un-linked, so PATH alone is not enough; the
# framework and Cellar locations are searched too.
is_supported() {
  [[ -x "$1" ]] && "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'     >/dev/null 2>&1
}

# A universal2 interpreter, if one has been fetched, so the app runs natively
# on both Apple silicon and Intel rather than through Rosetta on one of them.
if [[ -z "$PYTHON_BIN" ]]; then
  for candidate in .toolchain/Python.framework/Versions/3.1[1-9]/bin/python3.1[1-9]; do
    if is_supported "$candidate" && [[ "$(lipo -archs "$candidate" 2>/dev/null)" == *arm64* && "$(lipo -archs "$candidate" 2>/dev/null)" == *x86_64* ]]; then
      PYTHON_BIN="$PWD/$candidate"
      break
    fi
  done
fi

if [[ -z "$PYTHON_BIN" ]]; then
  # An existing .venv already knows which interpreter works here.
  if is_supported ".venv/bin/python"; then
    PYTHON_BIN="$(cd .venv/bin && ./python -c 'import sys; print(sys.base_prefix)')/bin/python3"
    is_supported "$PYTHON_BIN" || PYTHON_BIN=""
  fi
fi

if [[ -z "$PYTHON_BIN" ]]; then
  for candidate in python3.14 python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && is_supported "$(command -v "$candidate")"; then
      PYTHON_BIN="$(command -v "$candidate")"
      break
    fi
  done
fi

if [[ -z "$PYTHON_BIN" ]]; then
  for candidate in \
      /opt/homebrew/opt/python@3.1[1-9]/bin/python3.1[1-9] \
      /usr/local/opt/python@3.1[1-9]/bin/python3.1[1-9] \
      /Library/Frameworks/Python.framework/Versions/3.1[1-9]/bin/python3.1[1-9]; do
    if is_supported "$candidate"; then
      PYTHON_BIN="$candidate"
      break
    fi
  done
fi

if [[ -z "$PYTHON_BIN" ]]; then
  echo "error: Python 3.11+ not found." >&2
  echo "       Install it with:  brew install python@3.12" >&2
  echo "       Or point at one:  PYTHON_BIN=/path/to/python3.12 ./build_app.sh" >&2
  exit 1
fi
echo "==> Using $PYTHON_BIN ($("$PYTHON_BIN" --version))"

# --- 2. Virtual environment --------------------------------------------------
# One environment per interpreter architecture. Reusing whichever .venv happens
# to exist is how a universal interpreter still produces a single-architecture
# app: the interpreter is chosen, and then quietly ignored in favour of the
# environment already on disk.
PY_ARCHS="$(lipo -archs "$PYTHON_BIN" 2>/dev/null | tr ' ' '-' || echo native)"
case "$PY_ARCHS" in
  *arm64*x86_64*|*x86_64*arm64*) VENV=".venv-universal" ;;
  *) VENV=".venv" ;;
esac

if [[ -d "$VENV" ]]; then
  EXISTING="$(cd "$VENV/bin" && ./python -c 'import sys; print(sys.base_prefix)' 2>/dev/null || true)"
  WANTED="$("$PYTHON_BIN" -c 'import sys; print(sys.base_prefix)' 2>/dev/null || true)"
  if [[ -n "$EXISTING" && -n "$WANTED" && "$EXISTING" != "$WANTED" ]]; then
    echo "==> $VENV was built from a different interpreter; recreating it"
    rm -rf "$VENV"
  fi
fi
if [[ ! -d "$VENV" ]]; then
  echo "==> Creating $VENV"
  "$PYTHON_BIN" -m venv "$VENV"
fi
echo "==> Using $VENV ($PY_ARCHS)"
# shellcheck disable=SC1091
source "$VENV/bin/activate"
python -m pip install --upgrade pip --quiet
echo "==> Installing dependencies"
python -m pip install --quiet -r requirements-dev.txt

# A couple of the Rust extensions publish one wheel per architecture, so a
# universal build needs the other half of each fetching and joining on.
if [[ "$(lipo -archs "$PYTHON_BIN" 2>/dev/null)" == *arm64*x86_64* ]] || \
   [[ "$(lipo -archs "$PYTHON_BIN" 2>/dev/null)" == *x86_64*arm64* ]]; then
  echo "==> Making the compiled dependencies universal"
  python tools/make_universal_deps.py || true
fi

# --- 3. Icon -----------------------------------------------------------------
if [[ ! -f assets/icon.icns ]]; then
  echo "==> Generating the app icon"
  QT_QPA_PLATFORM=offscreen python tools/make_icon.py
fi

# --- 4. Tests ----------------------------------------------------------------
if [[ "$SKIP_TESTS" -eq 0 ]]; then
  echo "==> Running the test suite"
  QT_QPA_PLATFORM=offscreen python -m pytest
fi

# --- 5. Bundle ---------------------------------------------------------------
echo "==> Building the .app bundle"
rm -rf build "dist/Mail Manager.app" "dist/iCloud Job Triage"
python -m PyInstaller --clean --noconfirm MailManager.spec

APP="dist/Mail Manager.app"
if [[ ! -d "$APP" ]]; then
  echo "error: build did not produce $APP" >&2
  exit 1
fi

# --- 6. Ad-hoc signature -----------------------------------------------------
# An ad-hoc signature has no identity of its own: macOS tells builds apart by
# their contents, so every rebuild looks like a different app and the Keychain
# asks permission again. A local certificate fixes that. Neither is trusted by
# Gatekeeper - that needs a paid Developer ID - so a first launch still wants
# right-click then Open either way.
SIGN_NAME="${MAILMANAGER_SIGN_NAME:-Mail Manager Local Signing}"
if [[ -n "${MAILMANAGER_SIGN_IDENTITY:-}" ]]; then
  IDENTITY="$MAILMANAGER_SIGN_IDENTITY"
elif security find-identity -v -p codesigning 2>/dev/null | grep -qF "$SIGN_NAME"; then
  IDENTITY="$SIGN_NAME"
else
  IDENTITY="-"
fi

if [[ "$IDENTITY" == "-" ]]; then
  echo "==> Signing (ad-hoc)"
  echo "    Every rebuild will look like a new app to the Keychain, so it will"
  echo "    ask permission again. To stop that:"
  echo "      ./tools/make_signing_identity.sh"
else
  echo "==> Signing as “$IDENTITY”"
fi
# Deliberately without --options runtime. The hardened runtime turns on
# library validation, which requires everything the process loads to carry the
# same Team ID. A local certificate has no Team ID at all, so the app is denied
# its own bundled Python and dies before it starts. The hardened runtime is
# only needed for notarisation, which needs a paid Developer ID anyway.
codesign --force --deep --sign "$IDENTITY" "$APP" 2>/dev/null || \
  echo "    (codesign unavailable; right-click → Open on first launch)"

if ! "$APP/Contents/MacOS/Mail Manager" --self-test >/dev/null 2>&1; then
  echo "    ! The signed bundle does not start. Re-signing ad-hoc." >&2
  codesign --force --deep --sign - "$APP" 2>/dev/null || true
fi
xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true

SIZE="$(du -sh "$APP" | cut -f1)"
ARCHS="$(lipo -archs "$APP/Contents/MacOS/Mail Manager" 2>/dev/null || echo unknown)"
echo
echo "==> Done. $APP ($SIZE, $ARCHS)"
case "$ARCHS" in
  *arm64*x86_64*|*x86_64*arm64*)
    echo "    Universal: runs natively on Apple silicon and on Intel." ;;
  *arm64*)
    echo "    Apple silicon only. For a universal build:" 
    echo "      ./tools/fetch_universal_python.sh && ./build_app.sh" ;;
  *x86_64*)
    echo "    Intel only. Apple silicon will run it under Rosetta. For both:"
    echo "      ./tools/fetch_universal_python.sh && ./build_app.sh" ;;
esac
echo "    open \"$APP\"                       # run it"
echo "    cp -R \"$APP\" /Applications/        # install it"
