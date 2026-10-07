#!/usr/bin/env python3
"""Entry point for Mail Manager.

Run from source:      python main.py
Run the bundled app:  open "dist/Mail Manager.app"
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


def _restrict(path: Path) -> None:
    """Owner-only, best effort. A log nobody can read is worse than none."""
    try:
        if path.exists():
            os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - platform or filesystem says no
        pass


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
        # Readable by its owner only: the log names mailboxes and the subjects
        # of messages as they are sorted, the same class of thing as the
        # settings file.
        _restrict(path)
        for index in range(1, file_handler.backupCount + 1):
            _restrict(path.with_name(f"{path.name}.{index}"))
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
    import buildinfo

    parser.add_argument("--version", action="version",
                        version=f"{APP_DISPLAY_NAME} {buildinfo.full()}")
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
    parser.add_argument(
        "--offline",
        action="store_true",
        help="With --self-test, skip the check that needs a network.",
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


def self_test(offline: bool = False) -> int:
    """Verify the runtime the app is actually running inside.

    ``offline`` skips the one check that needs a network, for a build
    machine, CI or a train.
    """
    import config

    ok = True

    def check(label: str, fn) -> None:
        nonlocal ok
        try:
            print(f"  {label:.<34} {fn()}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"  {label:.<34} FAILED: {type(exc).__name__}: {exc}")

    import buildinfo

    print(f"{APP_DISPLAY_NAME} {buildinfo.full()} - self test")
    print(f"  {'frozen bundle':.<34} {bool(getattr(sys, 'frozen', False))}")
    print(f"  {'python':.<34} {sys.version.split()[0]}")

    check("PySide6", lambda: __import__("PySide6").__version__)
    check("Qt platform plugins", _qt_plugin_check)
    check("anthropic SDK", lambda: __import__("anthropic").__version__)
    check("keyring backend",
          lambda: config.CredentialStore(read_timeout=5.0).backend_name())
    check("settings directory", lambda: config.app_support_dir())
    check("log directory", lambda: config.log_dir())
    check("app icon", lambda: _bundled_path("assets/icon.png") or "not bundled")
    check("dial lettering", _dial_face)

    import certs
    check("certificate bundle", certs.describe)

    import lexicon as _lexicon
    check("world lexicon", _lexicon.describe)

    def _encryption() -> str:
        """Seal and open a scrap, so the answer is demonstrated, not asserted:
        make a key, encrypt, decrypt, and confirm the plaintext is absent
        from the file, in a temporary directory that is then removed.
        """
        import tempfile
        import vault

        if not vault.cipher_available():
            return "no cipher library; summaries are not written to disk"
        # Its own box with the same short timeout as the other checks: this
        # runs inside the build, where a Keychain prompt would never be
        # answered.
        box = vault.Vault(config.CredentialStore(read_timeout=5.0))
        if not box.sealing:
            return "Keychain unavailable; summaries are not written to disk"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "verdicts.json"
            secret = "self-test-canary-9f2a"
            box.write(path, {"summary": secret})
            raw = path.read_bytes()
            if secret.encode() in raw:
                raise RuntimeError("the plaintext is still in the file")
            if box.read(path) != {"summary": secret}:
                raise RuntimeError("it did not decrypt back")
        return "AES-GCM, key in the Keychain, verified round trip"

    check("encryption at rest", _encryption)

    def _links_and_updates() -> str:
        """Both imported inside methods, so a bundle could lose them."""
        import shutil

        import link_open
        import update_dialog  # noqa: F401
        import updates

        if (link_open.opens("file:///etc/hosts")
                or not link_open.opens("https://example.com")):
            raise RuntimeError("links are not checked in this build")
        if not updates.newer("2.0.0", "1.0.0"):
            raise RuntimeError("versions do not compare in this build")
        missing = [tool for tool in ("codesign", "hdiutil", "ditto")
                   if shutil.which(tool) is None]
        if missing:
            raise RuntimeError(f"{', '.join(missing)} not found")
        return "links said before opening; updates can install"

    check("links and updates", _links_and_updates)

    def _touch_bar() -> str:
        """Imported inside a function, and a missing AppKit message would
        take the app down rather than fail politely."""
        import touchbar  # noqa: F401
        import touchbar_mac

        gaps = touchbar_mac._Runtime().missing()
        if gaps:
            raise RuntimeError("AppKit here lacks " + ", ".join(gaps[:4]))
        return f"{sum(map(len, touchbar_mac.NEEDED.values()))} messages answered"

    check("touch bar", _touch_bar)

    def _attachment_viewer() -> str:
        """The viewer is reached by a function-level import, so prove it.

        PyInstaller finds modules by reading the source and can miss one
        imported only inside a method, which would break the button in the
        shipped app rather than the build.
        """
        import attachments
        import attachment_view

        sample = attachments.Attachment(
            part="1", name="probe.png", content_type="image/png",
            size=8, data=b"\x89PNG\r\n\x1a\n")
        if sample.kind != "image":
            raise RuntimeError("sniffing does not work in this build")
        if attachments.safe_name("../../x") != "x":
            raise RuntimeError("filename sanitising does not work in this build")
        if not hasattr(attachment_view, "AttachmentViewer"):
            raise RuntimeError("the viewer is not in this build")

        # The Qt pieces, actually constructed: QtMultimedia and QtPdf were once
        # on the spec's exclude list while an import check said they were
        # present.
        working = ["images", "text"]
        from PySide6.QtMultimedia import QAudioDecoder, QMediaPlayer
        player = QMediaPlayer()
        decoder = QAudioDecoder()
        if player is None or decoder is None:
            raise RuntimeError("audio is not in this build")
        working.append("audio")
        from PySide6.QtPdf import QPdfDocument
        if QPdfDocument() is None:
            raise RuntimeError("PDF is not in this build")
        working.append("PDF")

        # Checked, not named: this line ends up in a public build log.
        import visualizers
        if len(visualizers.SCENES) < 2:
            raise RuntimeError("part of the audio pane is not in this build")

        # The pane draws on the graphics card when there is one. Built, not
        # just imported: the module importing proves the Python half, and the
        # Qt library behind it is the half a bundle can lose.
        from PySide6.QtOpenGL import QOpenGLFramebufferObjectFormat
        import attachment_widgets
        wanted = QOpenGLFramebufferObjectFormat()
        wanted.setSamples(4)
        if wanted.samples() != 4:
            raise RuntimeError("drawing on the graphics card is not in this build")
        if not hasattr(attachment_widgets, "_GpuCanvas"):
            raise RuntimeError("the audio pane cannot draw on the card in this build")
        working.append("drawing on the card")
        # The rider's lit world, imported from inside a method and so the
        # kind of module a bundle can lose; its models are built here, which
        # needs no card.
        import rider_gl
        if not rider_gl.ship_triangles() or "onRoad" not in rider_gl.ROAD_GLSL:
            raise RuntimeError("the rider's world is not in this build")
        working.append("the rider's world")
        import rider_sound
        if (len(rider_sound.make("tick0")) < 1000
                or len(rider_sound.make(rider_sound.note_name("pluck", 72, 0)))
                < 1000):
            raise RuntimeError("the rider's sounds are not in this build")
        working.append("the rider's sounds")
        # Kept, read back and beaten, in a folder of its own that is then
        # removed. Imported only when a ride ends, so a bundle without it would
        # fail there, inside the listener that plays the sounds, silencing them
        # too.
        import tempfile

        import rider_bests
        with tempfile.TemporaryDirectory() as folder:
            place = Path(folder) / "bests.json"
            track = rider_bests.fingerprint([b"self test"])
            rider_bests.Bests(place).offer(track, "Mono", 10)
            if rider_bests.Bests(place).offer(track, "Mono", 20) != (10, True):
                raise RuntimeError("the rider's bests are not kept")
        working.append("the rider's bests")
        # And what the road and the sounds are laid from: a chord heard in
        # its key, and a beat read off a kick. Imported in the analysis's
        # own processes and on the game's first frame, so a bundle without
        # them fails there rather than here.
        import math

        import harmony
        import trackstyle
        from array import array as _array

        rate = 8000
        chord = _array("h", [int(8000 * (math.sin(2 * math.pi * 261.63 * i / rate)
                                         + math.sin(2 * math.pi * 329.63 * i / rate)
                                         + math.sin(2 * math.pi * 392.0 * i / rate)))
                             for i in range(rate * 3)])
        heard = harmony.analyse(chord, rate, 1)
        if not heard or heard["key"]["tonic"] not in (0, 9):
            raise RuntimeError("the rider cannot hear a key")
        kick = [1.0 if i % 30 == 0 else 0.0 for i in range(60 * 20)]
        kit = {name: trackstyle._Kept(kick, 60.0)
               for name in ("Kick", "Snare", "Hats")}
        found = trackstyle.rhythm_of(kit, tempo=120.0)
        if not found or abs(found["tempo"] - 120.0) > 1.0:
            raise RuntimeError("the rider cannot read a beat")
        working.append("the rider's key and beat")
        # What the road is laid with, for the style just read: imported on
        # the game's first frame.
        import rider_layout
        plan = rider_layout.Plan(trackstyle.Style(steady=1.0, seed=7))
        drop = trackstyle.Section(0.0, 30.0, "drop", 0.9, True)
        if plan.figure(drop, 0, True)[0] not in rider_layout.DANGER:
            raise RuntimeError("the rider cannot lay a road")
        working.append("the rider's road")
        # The scope's screen on the card, imported the first time the scope is
        # drawn there. Without it the scope is drawn on the CPU, too slow at
        # full screen to notice as anything but a stutter, so it is looked for
        # here.
        import scope_gl
        if scope_gl.reach(2880, 1800) != (2160, 1800) or not hasattr(
                scope_gl, "Tube"):
            raise RuntimeError("the scope cannot draw on the card")
        working.append("the scope on the card")
        # The picture's allowance for the ear and the eye, made the first time
        # a track plays: asked here, so a bundle without it, or without the
        # Core Audio it asks, fails now.
        import av_sync
        allowance = av_sync.Allowance()
        if not -1.0 < allowance.ahead(60.0) < 1.0:
            raise RuntimeError("the picture cannot keep time with the sound")
        working.append("the picture on the beat")
        return ", ".join(working)

    check("attachment viewer", _attachment_viewer)

    def _analysis_worker() -> str:
        import attachment_audio

        return attachment_audio.worker_check()

    check("audio analysis", _analysis_worker)

    def _tls_probe() -> str:
        """A real handshake, because a path that exists is not proof."""
        import socket
        import ssl as _ssl
        host = "imap.mail.me.com"
        context = _ssl.create_default_context()
        with socket.create_connection((host, 993), timeout=8) as raw:
            with context.wrap_socket(raw, server_hostname=host) as secure:
                return f"{secure.version()} to {host}"

    if offline:
        print(f"  {'TLS handshake':.<34} skipped (--offline)")
    else:
        check("TLS handshake", _tls_probe)

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
        print("  API key ................. not needed, runs on your Mac")
    print("\nReady. Try:  ./dev check   then   ./dev scan")
    return 0


def show_config() -> int:
    """Print the effective configuration. Secrets are reported, never printed."""
    import providers

    from config import CredentialStore, Settings

    settings = Settings.load()
    # Nobody is watching a terminal command to click Allow, and the Keychain
    # asks again for every re-signed build, so give up after a few seconds and
    # say so.
    store = CredentialStore(read_timeout=5.0)
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
        return self_test(offline=args.offline)
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
    # Before QApplication: Qt reads the bundle name when it builds the menu
    # bar, so setting it afterwards is too late.
    if not getattr(sys, "frozen", False):
        import macname
        macname.set_application_name(APP_DISPLAY_NAME)

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

    # Before anything can open a connection: every one of them is TLS.
    import certs
    certs.ensure()

    install_exception_hook(app)

    from config import CredentialStore, Settings
    import widgets
    from gui import MainWindow

    settings = Settings.load()

    # Paint before the first window exists, so nothing is shown in the default
    # palette and then repainted.
    import theme
    theme.apply(app, settings.appearance_mode, settings.contrast, settings.readable)

    # Every message box, including the ones Qt raises itself, gets text you
    # can select and copy.
    widgets.install_selectable_messages(app)

    # Each window's controls on the Touch Bar, on a Mac that has one.
    import touchbar
    touchbar.install(app)

    store = CredentialStore()
    window = MainWindow(settings, store, demo=args.demo, dry_run=args.dry_run)
    from gui import DockReopen
    if args.demo:
        log.info("Demo mode: using bundled sample data, no network access.")
        window._load_demo_data()

    # However the app ends (the window closed, Quit, the session ending), every
    # background thread is stopped before the event loop returns.
    app.aboutToQuit.connect(window.shutdown)

    # A click on the Dock icon with the window closed brings it back, as it
    # does in any Mac app.
    app.installEventFilter(DockReopen(window, app))

    # With a menu bar item present, closing the window puts the app away
    # rather than ending it; Quit is what ends it.
    app.setQuitOnLastWindowClosed(not settings.menu_bar_icon)

    if settings.start_in_menu_bar and window.menu_bar.visible():
        log.info("Starting in the menu bar; the window is available from it.")
    else:
        window.show()
        window.raise_()
        window.activateWindow()

    try:
        return app.exec()
    finally:
        window.shutdown()
        log.info("Exited cleanly.")


def _dial_face() -> str:
    """Which face the meter dials will be lettered in, worth a line of its own.

    Asked for by name with a fallback list no machine has, Qt substituted
    whatever it had, differently on every platform, with nothing to say so.
    """
    import visualizers

    # The file in the bundle first: what goes wrong in a build is the file not
    # being in it. A failure, not a remark: as a remark, builds shipped without
    # the face under a self test that passed.
    where = _bundled_path(f"assets/fonts/{visualizers.FONT_FILE}")
    if not where:
        raise FileNotFoundError(
            f"{visualizers.FONT_FILE} is not in the bundle, so the dials "
            f"would be lettered in whatever Qt substitutes")
    # The Open Font License goes wherever the font does.
    if not _bundled_path(f"assets/fonts/{visualizers.FONT_LICENCE}"):
        raise FileNotFoundError(
            f"{visualizers.FONT_LICENCE} is not in the bundle, and the font "
            f"may not ship without it")
    # Qt will not register a font before there is an application, and the
    # self test usually runs before there is one.
    family = visualizers.dial_face()
    if family:
        return f"{family}, shipped with the app"
    return f"{where.name} bundled ({where.stat().st_size:,} bytes)"


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
    # First, before anything else runs. The audio analysis works in processes
    # of its own, and in the built app a new process is this program started
    # again: this tells it to be a worker rather than open a second window.
    import multiprocessing

    multiprocessing.freeze_support()
    sys.exit(main())
