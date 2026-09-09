# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build recipe for “iCloud Job Triage.app”.

Build with:

    pyinstaller --clean --noconfirm iCloudJobTriage.spec

The result is dist/"iCloud Job Triage.app" — a self-contained bundle with its
own Python and Qt. No terminal, no virtualenv, and no daily redeploy.
"""

import os
import subprocess
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

APP_NAME = "Mail Manager"
BUNDLE_ID = "com.mailmanager.icloudjobtriage"
VERSION = "1.0.0"

def _target_arch() -> str:
    """What to build for: a single architecture, or both.

    Universal is only possible if every compiled module carries both slices.
    Asking PyInstaller for it when one does not produces a confusing failure
    deep in the build, so this checks first and says so plainly.
    """
    wanted = os.environ.get("MAILMANAGER_TARGET_ARCH", "universal2").strip()
    if wanted in ("arm64", "x86_64"):
        return wanted
    if wanted in ("", "native", "none"):
        return None

    import site
    thin = []
    for directory in site.getsitepackages():
        base = Path(directory)
        if not base.is_dir():
            continue
        for module in base.rglob("*.so"):
            result = subprocess.run(["lipo", "-archs", str(module)],
                                    capture_output=True, text=True)
            if result.returncode == 0 and len(result.stdout.split()) == 1:
                thin.append(module.name)
    if thin:
        print(f"  ! {len(thin)} module(s) carry one architecture "
              f"({', '.join(sorted(set(thin))[:3])}…).")
        print("    Run: python tools/make_universal_deps.py")
        print("    Building for this machine only.")
        return None
    return "universal2"


TARGET_ARCH = _target_arch()

ROOT = Path(SPECPATH).resolve()

# Which commit this app came from. There is no git inside a .app, so it is
# written down here and read back by buildinfo at runtime.
_stamp = ROOT / "BUILD_STAMP"
try:
    import sys as _sys

    _sys.path.insert(0, str(ROOT))
    import buildinfo as _buildinfo

    _buildinfo.write_stamp(_stamp)
    print(f"==> Build stamp: {_stamp.read_text().strip()}")
except Exception as _exc:      # never fail a build over a label
    print(f"==> No build stamp ({_exc})")

datas = [(str(_stamp), ".")] if _stamp.exists() else []
for asset in ("icon.png", "icon.icns"):
    path = ROOT / "assets" / asset
    if path.exists():
        datas.append((str(path), "assets"))

# Imported lazily inside the window, so static analysis misses it; `--demo`
# must work in the shipped bundle too.
hiddenimports = ["demo_data", "providers", "rules_engine", "rulesets",
                 "scheduler", "menubar", "welcome", "flowlayout",
                 "accounts", "autoreply", "certs", "helpmode", "ondevice",
                 "profiles", "theme", "macname"]

# The CA bundle. Without it a frozen app has no certificates at all, because
# the path Python was compiled with points at a framework the user does not
# have.
try:
    import certifi
    datas.append((certifi.where(), "certifi"))
except ImportError:      # pragma: no cover - flagged by the self test instead
    print("  ! certifi is not installed; the app will not be able to verify TLS.")

# keyring resolves its backends dynamically, so PyInstaller cannot see them.
hiddenimports += [
    "keyring.backends",
    "keyring.backends.macOS",
    "keyring.backends.fail",
    "keyring.backends.null",
    "keyring.backends.chainer",
]
hiddenimports += collect_submodules("anthropic")

# Qt modules this app never touches. Dropping them roughly halves the bundle.
excludes = [
    "tkinter", "test", "unittest", "pydoc_data", "lib2to3",
    "pytest", "PyInstaller",
    "PySide6.Qt3DAnimation", "PySide6.Qt3DCore", "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput", "PySide6.Qt3DLogic", "PySide6.Qt3DRender",
    "PySide6.QtBluetooth", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtDesigner", "PySide6.QtGraphs", "PySide6.QtGraphsWidgets",
    "PySide6.QtHelp", "PySide6.QtLocation", "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets", "PySide6.QtNfc", "PySide6.QtOpcUa",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtPositioning",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D",
    "PySide6.QtQuickControls2", "PySide6.QtQuickWidgets", "PySide6.QtRemoteObjects",
    "PySide6.QtScxml", "PySide6.QtSensors", "PySide6.QtSerialBus",
    "PySide6.QtSerialPort", "PySide6.QtSpatialAudio", "PySide6.QtSql",
    "PySide6.QtStateMachine", "PySide6.QtTest", "PySide6.QtTextToSpeech",
    "PySide6.QtUiTools", "PySide6.QtWebChannel", "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineQuick", "PySide6.QtWebEngineWidgets", "PySide6.QtWebSockets",
    "PySide6.QtHttpServer", "PySide6.QtNetworkAuth",
]

a = Analysis(
    ["main.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # no terminal window, ever
    disable_windowed_traceback=False,
    argv_emulation=False,
    # universal2 when the interpreter and every extension module can supply
    # both slices, which tools/make_universal_deps.py arranges. Override with
    # MAILMANAGER_TARGET_ARCH=arm64 or x86_64 to build one on its own.
    target_arch=TARGET_ARCH,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "assets" / "icon.icns") if (ROOT / "assets" / "icon.icns").exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)

app = BUNDLE(
    coll,
    name=f"{APP_NAME}.app",
    icon=str(ROOT / "assets" / "icon.icns") if (ROOT / "assets" / "icon.icns").exists() else None,
    bundle_identifier=BUNDLE_ID,
    version=VERSION,
    info_plist={
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": "Mail Manager",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundlePackageType": "APPL",
        "LSApplicationCategoryType": "public.app-category.productivity",
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,   # follow light/dark mode
        "NSHumanReadableCopyright": "Runs entirely on your Mac. Credentials live in the Keychain.",
        "NSAppTransportSecurity": {"NSAllowsArbitraryLoads": False},
    },
)
