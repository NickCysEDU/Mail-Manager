"""Encryption at rest for the two files that describe your mail.

Most of what this app writes down is dull: which folders exist, how wide a
column is. Two files are not.

``verdicts.json`` holds a summary and a line of reasoning for every message
it has classified, which together are a readable index of somebody's
correspondence: who turned them down, what they applied for, when. And
``corrections.json`` holds every sender they have filed by hand, which is a
list of who writes to them.

Both used to sit in the clear. File permissions of ``0600`` and FileVault
already protect them from someone holding the disk, so the threat this closes
is the other one: **any other program running as the same user can read a file
in your home directory.** It cannot read a Keychain item belonging to this app
without the system asking you first, because Keychain access is granted per
application. Putting the key there and the ciphertext on disk moves those two
files behind that boundary, and takes them out of a Time Machine snapshot or a
synced folder in readable form.

What this is not: it is not protection from somebody who already has your
Mac unlocked and can run this app. Nothing stored on a machine can be.

**When it cannot encrypt, it does not write the sensitive parts at all.**
A missing cipher library or an unreachable Keychain degrades the feature -
verdicts are still cached, they just lose their summaries - rather than
degrading the promise. A cache is never worth writing somebody's mail to disk
in the clear.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

#: What a sealed file starts with, so an old plaintext one is recognised and
#: read rather than rejected.
MAGIC = b"MMSEAL1\n"

#: 96 bits, which is what AES-GCM expects and what it is safe to generate at
#: random for every single write.
NONCE_BYTES = 12
KEY_BYTES = 32

#: The Keychain account the key lives under, inside the app's existing
#: service. One key for every sealed file; the filename is bound in as
#: associated data so one cannot be swapped for another.
KEY_ACCOUNT = "local-data-key"


def cipher_available() -> bool:
    """Whether a vetted AEAD implementation is installed."""
    try:
        import cryptography.hazmat.primitives.ciphers.aead as _aead
    except Exception:      # noqa: BLE001 - any import failure means no cipher
        return False
    return hasattr(_aead, "AESGCM")


class Vault:
    """Reads and writes a JSON document, sealed when it can be.

    Deliberately not a general-purpose crypto layer. It does one thing: put a
    dict on disk in a form another program running as this user cannot read.
    """

    def __init__(self, store=None) -> None:
        self._store = store
        self._key: Optional[bytes] = None
        self._looked = False

    # -- the key ---------------------------------------------------------
    def _credential_store(self):
        if self._store is None:
            import config
            self._store = config.CredentialStore()
        return self._store

    def key(self) -> Optional[bytes]:
        """The data key, made on first use. None if the Keychain will not talk.

        Cached for the life of the object: a scan seals the cache once at the
        end, and asking the Keychain repeatedly is how you end up with a
        password prompt in the middle of somebody's work.
        """
        if self._looked:
            return self._key
        self._looked = True
        if not cipher_available():
            return None
        try:
            store = self._credential_store()
            existing = store.get(KEY_ACCOUNT)
            if existing:
                raw = base64.b64decode(existing)
                if len(raw) == KEY_BYTES:
                    self._key = raw
                    return self._key
            fresh = secrets.token_bytes(KEY_BYTES)
            store.set(KEY_ACCOUNT, base64.b64encode(fresh).decode("ascii"))
            self._key = fresh
        except Exception as exc:      # noqa: BLE001 - never fail a scan
            log.warning("Could not reach the Keychain for the data key (%s); "
                        "sensitive fields will not be written.", exc)
            self._key = None
        return self._key

    @property
    def sealing(self) -> bool:
        """Whether a write would actually be encrypted."""
        return self.key() is not None

    def describe(self) -> str:
        if self.sealing:
            return "Encrypted with a key in your Keychain."
        if not cipher_available():
            return ("Not encrypted: no cipher library is installed, so "
                    "summaries are not written to disk at all.")
        return ("Not encrypted: the Keychain is unavailable, so summaries are "
                "not written to disk at all.")

    # -- reading and writing ---------------------------------------------
    def read(self, path: Path) -> Optional[Any]:
        """The document at ``path``, sealed or not. None if unreadable.

        Never raises. A file that cannot be read is a cache that has to be
        rebuilt, which is slow rather than broken.
        """
        try:
            raw = path.read_bytes()
        except (OSError, ValueError):
            return None
        if not raw:
            return None
        if not raw.startswith(MAGIC):
            # Written before this existed, or by a build with no cipher.
            try:
                return json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return None
        key = self.key()
        if key is None:
            log.warning("%s is sealed and the key is unavailable.", path.name)
            return None
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            body = raw[len(MAGIC):]
            nonce, ciphertext = body[:NONCE_BYTES], body[NONCE_BYTES:]
            plain = AESGCM(key).decrypt(nonce, ciphertext,
                                        path.name.encode("utf-8"))
            return json.loads(plain.decode("utf-8"))
        except Exception as exc:      # noqa: BLE001 - a bad file is an empty one
            log.warning("Could not open %s (%s); starting fresh.", path.name, exc)
            return None

    def write(self, path: Path, document: Any) -> bool:
        """Seal and store. Returns False if nothing could be written."""
        try:
            payload = json.dumps(document).encode("utf-8")
        except (TypeError, ValueError) as exc:
            log.warning("Could not serialise %s (%s).", path.name, exc)
            return False

        key = self.key()
        if key is not None:
            try:
                from cryptography.hazmat.primitives.ciphers.aead import AESGCM

                # A fresh nonce every write. The filename is bound in, so a
                # sealed verdicts file cannot be renamed over corrections.
                nonce = secrets.token_bytes(NONCE_BYTES)
                sealed = AESGCM(key).encrypt(nonce, payload,
                                             path.name.encode("utf-8"))
                payload = MAGIC + nonce + sealed
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not seal %s (%s).", path.name, exc)
                return False
        return _atomic_write(path, payload)


def _atomic_write(path: Path, payload: bytes) -> bool:
    """Temp file, fsync, rename, 0600. Never raises."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(dir=str(path.parent),
                                        prefix=f".{path.stem}-", suffix=".tmp")
        try:
            with os.fdopen(handle, "wb") as out:
                out.write(payload)
                out.flush()
                os.fsync(out.fileno())
            os.chmod(name, 0o600)
            os.replace(name, path)
        except BaseException:
            try:
                os.unlink(name)
            except OSError:
                pass
            raise
        try:
            os.chmod(path, 0o600)
        except OSError:      # pragma: no cover - best effort
            pass
        return True
    except OSError as exc:
        log.warning("Could not write %s (%s).", path.name, exc)
        return False


#: One vault for the process. The key is fetched once and held.
_SHARED: Optional[Vault] = None


def shared() -> Vault:
    global _SHARED
    if _SHARED is None:
        _SHARED = Vault()
    return _SHARED


def reset() -> None:
    """Forget the cached key. For tests, and after the Keychain changes."""
    global _SHARED
    _SHARED = None
