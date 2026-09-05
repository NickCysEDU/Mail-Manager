#!/usr/bin/env python3
"""Entry point for iCloud Mail Job Triage.

Run from source:      python main.py
Run the bundled app:  open "dist/iCloud Job Triage.app"
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
import traceback
from pathlib import Path
from typing import Optional

# When frozen by PyInstaller the bundle root is not on sys.path by default.
if getattr(sys, "frozen", False):  # pragma: no cover - only in the .app
    sys.path.insert(0, str(Path(sys.executable).resolve().parent))

from models import APP_DISPLAY_NAME, APP_NAME, APP_VERSION  # noqa: E402

LOG_FORMAT = "%(asctime)s  %(levelname)-7s %(name)-14s %(message)s"


def configure_logging(level: str = "INFO", echo: bool = False) -> Path:
    """Log to a rotating file in ~/Library/Logs and to stderr when attached."""
    from config import log_dir

    directory = log_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        directory = Path.home()
    path = directory / "triage.log"

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    try:
        file_handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(file_handler)
    except OSError:  # pragma: no cover - read-only home
        pass

    if echo or (sys.stderr is not None and sys.stderr.isatty()):
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter("%(levelname)-7s %(name)-14s %(message)s"))
        root.addHandler(stream)

    # httpx/anthropic are chatty at DEBUG and leak URLs into the log.
    for noisy in ("httpx", "httpcore", "httpx2", "httpcore2", "anthropic", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return path


def install_exception_hook(app) -> None:
    """Surface unexpected exceptions instead of dying silently in a .app."""
    from PySide6.QtWidgets import QMessageBox

    def hook(exc_type, exc_value, exc_tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):  # pragma: no cover
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        logging.getLogger("main").critical("Unhandled exception:\n%s", text)
        try:
            box = QMessageBox(
                QMessageBox.Icon.Critical,
                "Unexpected error",
                f"{exc_type.__name__}: {exc_value}",
            )
            box.setDetailedText(text)
            box.exec()
        except Exception:  # pragma: no cover
            pass

    sys.excepthook = hook


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="icloud-job-triage", description=__doc__)
    parser.add_argument("--version", action="version", version=f"{APP_DISPLAY_NAME} {APP_VERSION}")
    parser.add_argument(
        "--log-level",
        default=os.environ.get("ICLOUD_TRIAGE_LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO).",
    )
    parser.add_argument(
        "--reset-settings",
        action="store_true",
        help="Delete the saved settings file before starting. Keychain secrets are kept.",
    )
    parser.add_argument(
        "--print-paths",
        action="store_true",
        help="Print the settings and log locations, then exit.",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Check that every runtime dependency resolves, then exit. "
             "Useful for verifying a freshly built .app bundle.",
    )

    develop = parser.add_argument_group("development")
    parser.add_argument(
        "--scan-once",
        action="store_true",
        help="Run one unattended scan and exit. This is what the background "
             "agent calls; it prints a one-line summary and writes a status file.",
    )

    develop = parser.add_argument_group("development")
    develop.add_argument(
        "--demo",
        action="store_true",
        help="Open the window filled with bundled sample mail. No credentials, "
             "no network, and nothing can move real messages.",
    )
    develop.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and analyze your real mailbox, but disable folder moves.",
    )
    develop.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Shorthand for --log-level DEBUG, and mirror the log to the terminal.",
    )
    develop.add_argument(
        "--set-credentials",
        action="store_true",
        help="Store the iCloud address, app-specific password and Anthropic key in the "
             "Keychain from the terminal, then exit. Skips the settings dialog.",
    )
    develop.add_argument(
        "--show-config",
        action="store_true",
        help="Print the current settings and which credentials are present "
             "(never the secrets themselves), then exit.",
    )
    return parser


def _clean_argv(argv: Optional[list]) -> list:
    """Drop the process-serial-number argument macOS can pass on launch."""
    source = sys.argv[1:] if argv is None else argv
    return [arg for arg in source if not arg.startswith("-psn_")]


def self_test() -> int:
    """Verify the runtime the app is actually running inside."""
    import config

    ok = True

    def check(label: str, fn) -> None:
        nonlocal ok
        try:
            print(f"  {label:.<34} {fn()}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"  {label:.<34} FAILED: {type(exc).__name__}: {exc}")

    print(f"{APP_DISPLAY_NAME} {APP_VERSION} - self test")
    print(f"  {'frozen bundle':.<34} {bool(getattr(sys, 'frozen', False))}")
    print(f"  {'python':.<34} {sys.version.split()[0]}")

    check("PySide6", lambda: __import__("PySide6").__version__)
    check("Qt platform plugins", _qt_plugin_check)
    check("anthropic SDK", lambda: __import__("anthropic").__version__)
    check("keyring backend", lambda: config.CredentialStore().backend_name())
    check("settings directory", lambda: config.app_support_dir())
    check("log directory", lambda: config.log_dir())
    check("app icon", lambda: _bundled_path("assets/icon.png") or "not bundled")

    print("\n  " + ("All checks passed." if ok else "Some checks FAILED."))
    return 0 if ok else 1


def set_credentials() -> int:
    """Store credentials from the terminal - faster than the settings dialog."""
    import getpass

    from config import CredentialError, CredentialStore, Settings

    settings = Settings.load()
    store = CredentialStore()
    print(f"Storing secrets in the macOS Keychain (service “{store.service}”).")
    print("Press Return to keep the current value.\n")

    try:
        current = settings.icloud_email
        prompt = f"iCloud email [{current}]: " if current else "iCloud email: "
        email = input(prompt).strip() or current
        if not email:
            print("An iCloud email address is required.", file=sys.stderr)
            return 2

        has_password = bool(store.get_icloud_password(email))
        password = getpass.getpass(
            f"App-specific password [{'unchanged' if has_password else 'not set'}]: "
        ).strip()

        api_key = ""
        if settings.needs_api_key:
            has_key = bool(store.get_provider_key(settings.provider))
            api_key = getpass.getpass(
                f"{settings.provider_label} API key "
                f"[{'unchanged' if has_key else 'not set'}]: "
            ).strip()
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.", file=sys.stderr)
        return 130

    try:
        if password:
            store.set_icloud_password(email, password)
        if api_key:
            store.set_provider_key(settings.provider, api_key)
    except CredentialError as exc:
        print(f"Keychain error: {exc}", file=sys.stderr)
        return 1

    settings.icloud_email = email
    settings.save()

    print()
    print(f"  iCloud email ............ {email}")
    print(f"  App-specific password ... {'stored' if store.get_icloud_password(email) else 'MISSING'}")
    print(f"  Model backend ........... {settings.provider_label} · {settings.model}")
    if settings.needs_api_key:
        stored = bool(store.get_provider_key(settings.provider))
        print(f"  {settings.provider_label + ' key':.<24} {'stored' if stored else 'MISSING'}")
    else:
        print("  API key ................. not needed - this backend runs on your Mac")
    print("\nReady. Try:  ./dev check   then   ./dev scan")
    return 0


def show_config() -> int:
    """Print the effective configuration. Secrets are reported, never printed."""
    import providers

    from config import CredentialStore, Settings

    settings = Settings.load()
    store = CredentialStore()
    print(f"{APP_DISPLAY_NAME} {APP_VERSION} - configuration\n")
    for field, value in sorted(settings.to_dict().items()):
        if field in ("window_geometry", "splitter_state", "table_state"):
            value = f"<{len(str(value))} bytes>" if value else "<unset>"
        print(f"  {field:.<28} {value}")

    print(f"\n  model backend: {settings.provider_label} · {settings.model}"
          f"{'  (on this Mac, no key needed)' if settings.on_device else ''}")

    print("\n  credentials (macOS Keychain)")
    try:
        password = bool(store.get_icloud_password(settings.icloud_email))
        print(f"    {'backend':.<26} {store.backend_name()}")
        print(f"    {'app-specific password':.<26} {'stored' if password else 'MISSING'}")
        api_key = True
        for name in providers.PROVIDERS_BY_NAME:
            cls = providers.provider_class(name)
            if not cls.needs_api_key:
                continue
            present = bool(store.get_provider_key(name))
            marker = " ←" if name == settings.provider else ""
            if name == settings.provider:
                api_key = present
            print(f"    {cls.label.lower():.<26} {'stored' if present else 'not set'}{marker}")
        if not (password and api_key):
            print("\n  Run  ./dev creds  to fill in what is missing.")
    except Exception as exc:  # noqa: BLE001
        print(f"    could not read the Keychain: {exc}")
    return 0


def _qt_plugin_check() -> str:
    from PySide6.QtCore import QLibraryInfo

    path = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath))
    platforms = path / "platforms"
    if not platforms.is_dir():
        raise RuntimeError(f"no platform plugins under {path}")
    return f"{len(list(platforms.glob('*.dylib')))} found"


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(_clean_argv(argv))
    log_path = configure_logging("DEBUG" if args.verbose else args.log_level,
                                 echo=args.verbose)

    import config

    if args.self_test:
        return self_test()
    if args.scan_once:
        import scheduler

        record = scheduler.run_once()
        print(record.describe())
        return 1 if record.error else 0
    if args.set_credentials:
        return set_credentials()
    if args.show_config:
        return show_config()

    # Actions run before the informational flags, so combinations such as
    # `--reset-settings --print-paths` behave the way they read.
    if args.reset_settings:
        try:
            config.settings_path().unlink()
            print(f"Removed {config.settings_path()}")
        except FileNotFoundError:
            pass

    if args.print_paths:
        print(f"settings: {config.settings_path()}")
        print(f"logs:     {log_path}")
        print(f"keychain: {config.KEYCHAIN_SERVICE}")
        return 0

    log = logging.getLogger("main")
    log.info("Starting %s %s (python %s)", APP_DISPLAY_NAME, APP_VERSION, sys.version.split()[0])

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QFontDatabase, QIcon
    from PySide6.QtWidgets import QApplication

    QApplication.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeMenuBar, False)
    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_DISPLAY_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName("Mail Manager")
    app.setOrganizationDomain("mailmanager.local")

    # Use the macOS system UI font (SF Pro) everywhere, explicitly, so no
    # stylesheet or widget default can substitute something else.
    app.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont))

    icon_path = _bundled_path("assets/icon.png")
    if icon_path is not None:
        app.setWindowIcon(QIcon(str(icon_path)))

    install_exception_hook(app)

    from config import CredentialStore, Settings
    from gui import MainWindow

    settings = Settings.load()
    store = CredentialStore()
    window = MainWindow(settings, store, demo=args.demo, dry_run=args.dry_run)
    if args.demo:
        log.info("Demo mode: using bundled sample data, no network access.")
        window._load_demo_data()

    # Belt and braces: whether the user closes the window, picks Quit, or the
    # session ends, every background thread is stopped before the event loop
    # returns. Nothing is left running after the app disappears.
    app.aboutToQuit.connect(window.shutdown)

    window.show()
    window.raise_()
    window.activateWindow()

    try:
        return app.exec()
    finally:
        window.shutdown()
        log.info("Exited cleanly.")


def _bundled_path(relative: str) -> Optional[Path]:
    """Resolve a resource both from source and from inside the .app bundle."""
    roots = [Path(__file__).resolve().parent]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:  # pragma: no cover - only in the .app
        roots.insert(0, Path(meipass))
    for root in roots:
        candidate = root / relative
        if candidate.exists():
            return candidate
    return None


if __name__ == "__main__":
    sys.exit(main())
