"""Making sure TLS can verify a certificate, wherever the app is running.

Python's ssl module looks for a CA bundle at a path chosen when the
interpreter was compiled. That path is right on the machine that built it and
frequently wrong everywhere else - inside a frozen app there is no Python
framework at all, and the baked path points into a directory the user has
never had.

Every connection this app makes is TLS: IMAP to the mail server, HTTPS to
whichever model backend. So this is checked once at startup, and if the
compiled-in bundle is not there, the one shipped inside the app is used
instead. Nothing here ever disables verification; if no bundle can be found it
says so and lets the failure happen honestly.
"""

from __future__ import annotations

import logging
import os
import ssl
import sys
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

#: Where macOS keeps a system bundle, as a last resort before giving up.
_SYSTEM_BUNDLES = (
    "/etc/ssl/cert.pem",
    "/private/etc/ssl/cert.pem",
    "/usr/local/etc/openssl@3/cert.pem",
)


def compiled_bundle() -> Optional[Path]:
    """The bundle Python was built to look for, if it is actually there."""
    paths = ssl.get_default_verify_paths()
    for candidate in (paths.cafile, paths.openssl_cafile):
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    for directory in (paths.capath, paths.openssl_capath):
        if directory and Path(directory).is_dir() and any(Path(directory).iterdir()):
            return Path(directory)
    return None


def bundled_bundle() -> Optional[Path]:
    """The CA bundle shipped inside the application."""
    try:
        import certifi
    except ImportError:
        certifi = None
    if certifi is not None:
        where = Path(certifi.where())
        if where.is_file():
            return where
    # PyInstaller puts data files beside the executable at runtime.
    base = getattr(sys, "_MEIPASS", None)
    if base:
        for relative in ("certifi/cacert.pem", "cacert.pem"):
            candidate = Path(base) / relative
            if candidate.is_file():
                return candidate
    return None


def system_bundle() -> Optional[Path]:
    return next((Path(p) for p in _SYSTEM_BUNDLES if Path(p).is_file()), None)


def ensure() -> Optional[Path]:
    """Point OpenSSL at a bundle that exists. Returns the one in use.

    Sets ``SSL_CERT_FILE``, which OpenSSL reads when a context loads its
    default certificates, so every ``ssl.create_default_context()`` made after
    this - including the ones inside imaplib and http.client - picks it up
    without any of them having to know.
    """
    chosen = os.environ.get("SSL_CERT_FILE")
    if chosen and Path(chosen).is_file():
        return Path(chosen)

    existing = compiled_bundle()
    if existing is not None:
        return existing

    for source, describe in ((bundled_bundle, "the bundle shipped with the app"),
                             (system_bundle, "the system bundle")):
        found = source()
        if found is not None:
            os.environ["SSL_CERT_FILE"] = str(found)
            os.environ.setdefault("SSL_CERT_DIR", str(found.parent))
            log.info("Using %s for certificate verification (%s).", describe, found)
            return found

    log.warning(
        "No certificate bundle found. TLS connections cannot be verified."
    )
    return None


def describe() -> str:
    """One line for the self test.

    Origin is worked out before ``ensure`` runs, because ensure sets
    ``SSL_CERT_FILE`` and after that the compiled-in path reports whatever was
    just chosen - which would make every bundle look like the original.
    """
    was_compiled_in = compiled_bundle() is not None
    found = ensure()
    if found is None:
        return "no CA bundle found. TLS verification will fail"
    origin = "compiled in" if was_compiled_in else "shipped with the app"
    return f"{found} ({origin})"
