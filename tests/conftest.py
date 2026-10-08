"""Shared fixtures and fakes.

Nothing in the suite touches the network or the macOS Keychain.
"""

from __future__ import annotations

import email.message
import imaplib
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imap_engine import encode_mutf7  # noqa: E402
from models import Category, Classification, EmailMessage, FolderPlan, OtherCategory, TriageItem  # noqa: E402


@pytest.fixture(autouse=True)
def no_update_checks(monkeypatch):
    """A window shown for five seconds starts an update check, which would
    ask GitHub; here it answers at once, without the network."""
    try:
        import update_dialog
    except ImportError:     # no Qt, so no window to start one
        return
    monkeypatch.setattr(update_dialog.Look, "run",
                        lambda self: self.failed.emit("no network in tests"))


@pytest.fixture(autouse=True)
def no_real_mail_servers(monkeypatch):
    """No test reaches a mail server. A window opening lists its inbox, so
    one shown with a password stored would try; a connection the test did
    not hand the engine itself fails at once, as an unreachable server
    does."""
    class Refused(imaplib.IMAP4_SSL):
        def __init__(self, *args, **kwargs):
            raise OSError("no network in tests")

    monkeypatch.setattr(imaplib, "IMAP4_SSL", Refused)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Point settings and logs at a temp dir for every test."""
    monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path / "app-home"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    yield tmp_path


@pytest.fixture(autouse=True)
def no_real_keychain(monkeypatch):
    """Make the real Keychain unreachable, and give the vault its own.

    With cryptography installed every save looks for a data key, and
    creating one on a locked or headless keychain blocks in securityd with
    nobody to approve it: on CI, a suite that never finishes.

    A store built with an explicit backend still uses it; only the fallback
    to the real thing is closed off, and it fails loudly rather than
    hanging.
    """
    import config
    import vault

    real = config.CredentialStore._keyring

    def refuse(self):
        if self._backend is not None:
            return real(self)
        raise AssertionError(
            "a test reached the real macOS Keychain. Pass a backend "
            "(InMemoryCredentialStore, or CredentialStore(backend=...)) "
            "instead - the suite must not depend on the machine it runs on.")

    monkeypatch.setattr(config.CredentialStore, "_keyring", refuse)
    monkeypatch.setattr(vault, "_SHARED",
                        vault.Vault(config.InMemoryCredentialStore()),
                        raising=False)
    yield
    vault.reset()


@pytest.fixture(autouse=True)
def dialog_calls(monkeypatch):
    """Neutralise every modal dialog so a test can never block on one.

    Yields the intercepted calls as ``(kind, title, text)`` tuples. A test
    that needs a specific answer can monkeypatch the same method again:
    test-level patches are applied after this fixture.
    """
    try:
        from PySide6.QtWidgets import QDialog, QFileDialog, QMessageBox
    except ImportError:  # pragma: no cover - PySide6 is a hard dependency
        yield []
        return

    calls = []

    def record_static(kind):
        def handler(*args, **kwargs):
            title = args[1] if len(args) > 1 else ""
            text = args[2] if len(args) > 2 else ""
            calls.append((kind, str(title), str(text)))
            return QMessageBox.StandardButton.Ok
        return staticmethod(handler)

    for kind in ("information", "warning", "critical", "question", "about"):
        monkeypatch.setattr(QMessageBox, kind, record_static(kind))

    def record_exec(self):
        calls.append(("exec", self.windowTitle(), self.text()))
        # Cancel by default: no test may accidentally confirm a destructive action.
        return QMessageBox.StandardButton.Cancel

    monkeypatch.setattr(QMessageBox, "exec", record_exec)

    def record_dialog_exec(self):
        calls.append(("dialog", self.windowTitle(), type(self).__name__))
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(QDialog, "exec", record_dialog_exec)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: ("", "")))
    yield calls


def build_mime(
    subject: str = "Hello",
    sender: str = "Dana Reyes <dana@northwind.example>",
    to: str = "you@icloud.example",
    plain: Optional[str] = "Plain body text that is comfortably longer than forty characters.",
    html: Optional[str] = None,
    date: str = "Fri, 04 Sep 2026 12:34:56 -0700",
    extra_headers: Optional[Dict[str, str]] = None,
    attachment: Optional[Tuple[str, bytes]] = None,
    charset: str = "utf-8",
) -> bytes:
    """Serialise a realistic RFC 822 message."""
    message = email.message.EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = to
    message["Date"] = date
    message["Message-ID"] = "<test@northwind.example>"
    for key, value in (extra_headers or {}).items():
        message[key] = value

    if plain is not None and html is not None:
        message.set_content(plain, charset=charset)
        message.add_alternative(html, subtype="html", charset=charset)
    elif html is not None:
        message.set_content(html, subtype="html", charset=charset)
    else:
        message.set_content(plain or "", charset=charset)

    if attachment is not None:
        name, payload = attachment
        message.add_attachment(
            payload, maintype="application", subtype="pdf", filename=name
        )
    return message.as_bytes()


@pytest.fixture
def mime_factory():
    return build_mime


class FakeIMAP:
    """A small, strict stand-in for ``imaplib.IMAP4_SSL``: it rejects unquoted
    mailbox names and unknown commands, so protocol mistakes fail tests
    rather than behave differently against the real server.
    """

    def __init__(
        self,
        folders: Optional[Sequence[str]] = None,
        messages: Optional[Dict[str, bytes]] = None,
        capabilities: Sequence[str] = ("IMAP4REV1", "UIDPLUS", "MOVE"),
        delimiter: str = "/",
        password: str = "app-specific",
        internaldates: Optional[Dict[str, str]] = None,
        post_auth_capabilities: Optional[Sequence[str]] = None,
        structures: Optional[Dict[str, str]] = None,
    ) -> None:
        self.folders: List[str] = list(folders or ["INBOX", "Sent Messages", "Archive"])
        self.messages: Dict[str, bytes] = dict(messages or {})
        #: What the greeting advertises - iCloud's is much shorter than the truth.
        self.capabilities = tuple(capabilities)
        #: What an explicit CAPABILITY command returns after login.
        self.post_auth_capabilities = (
            tuple(post_auth_capabilities) if post_auth_capabilities is not None
            else tuple(capabilities)
        )
        self.delimiter = delimiter
        self.password = password
        self.internaldates = dict(internaldates or {})
        #: BODYSTRUCTURE per UID, for the tests that need the server's own
        #: description of a message's parts.
        self.structures: Dict[str, str] = dict(structures or {})
        self.deleted: set = set()
        self.copies: List[Tuple[str, str]] = []
        #: UIDs handed out to copies, so no two collide.
        self._next_copy_uid = 9000
        self.expunged: List[str] = []
        self.commands: List[Tuple[str, tuple]] = []
        self.selected: Optional[str] = None
        self.readonly: Optional[bool] = None
        self.logged_in = False
        self.logged_out = False
        self.closed = False
        self.subscribed: List[str] = []
        self.search_results: Optional[List[str]] = None
        self.fail_copy_to: Optional[str] = None
        self.fail_store = False
        #: Flags set through STORE, per uid, so a test can check them.
        self.flags: Dict[str, set] = {}
        #: (mailbox, flags, raw) for every APPEND.
        self.appended: List[Tuple[str, str, bytes]] = []
        self.fail_append = False

    @staticmethod
    def _unquote(name) -> str:
        if isinstance(name, bytes):
            name = name.decode()
        name = str(name)
        if name.startswith('"') and name.endswith('"'):
            return name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        return name

    def _record(self, name: str, args: tuple) -> None:
        self.commands.append((name, args))

    def login(self, user: str, password: str):
        self._record("LOGIN", (user,))
        if password != self.password:
            raise imaplib.IMAP4.error(b"AUTHENTICATIONFAILED Invalid credentials")
        self.logged_in = True
        return ("OK", [b"LOGIN completed"])

    def capability(self):
        self._record("CAPABILITY", ())
        if not self.logged_in:
            return ("OK", [" ".join(self.capabilities).encode()])
        return ("OK", [" ".join(self.post_auth_capabilities).encode()])

    def logout(self):
        self.logged_out = True
        return ("BYE", [b"logging out"])

    def shutdown(self):
        self.logged_out = True

    def close(self):
        self.closed = True
        self.selected = None
        return ("OK", [b"CLOSE completed"])

    def list(self, directory='""', pattern="*"):
        self._record("LIST", (directory, pattern))
        if pattern in ('""', b'""'):
            return ("OK", [f'(\\Noselect) "{self.delimiter}" ""'.encode()])
        lines = []
        for name in self.folders:
            flags = "\\HasNoChildren"
            if any(other.startswith(name + self.delimiter) for other in self.folders):
                flags = "\\HasChildren"
            # Real servers send modified UTF-7, never raw UTF-8.
            lines.append(
                f'({flags}) "{self.delimiter}" "{encode_mutf7(name)}"'.encode("ascii")
            )
        return ("OK", lines)

    def create(self, mailbox):
        raw = str(mailbox)
        assert raw.startswith('"') and raw.endswith('"'), "mailbox names must be quoted"
        name = self._unquote(mailbox)
        self._record("CREATE", (name,))
        if name in self.folders:
            return ("NO", [b"[ALREADYEXISTS] Mailbox already exists"])
        self.folders.append(name)
        return ("OK", [b"CREATE completed"])

    def subscribe(self, mailbox):
        self.subscribed.append(self._unquote(mailbox))
        return ("OK", [b"SUBSCRIBE completed"])

    def select(self, mailbox='"INBOX"', readonly=False):
        name = self._unquote(mailbox)
        self._record("SELECT", (name, readonly))
        if name not in self.folders:
            return ("NO", [b"Mailbox does not exist"])
        self.selected = name
        self.readonly = readonly
        return ("OK", [str(len(self.messages)).encode()])

    def expunge(self):
        self._record("EXPUNGE", ())
        for uid in list(self.deleted):
            self.messages.pop(uid, None)
            self.expunged.append(uid)
        self.deleted.clear()
        return ("OK", [b"EXPUNGE completed"])

    def uid(self, command: str, *args):
        command = command.upper()
        self._record("UID " + command, args)
        handler = getattr(self, f"_uid_{command.lower()}", None)
        if handler is None:
            raise imaplib.IMAP4.error(f"Unsupported command {command}".encode())
        return handler(*args)

    def _uid_search(self, *args):
        if self.search_results is not None:
            uids = self.search_results
        else:
            uids = sorted(self.messages, key=int)
        return ("OK", [" ".join(uids).encode()])

    def _uid_fetch(self, uid_set, spec):
        response = []
        # A partial fetch is honoured the way a real server honours it: the
        # first N bytes, labelled as such, with the true size alongside.
        partial = re.search(r"BODY\.PEEK\[\]<0\.(\d+)>", str(spec))
        for uid in str(uid_set).split(","):
            raw = self.messages.get(uid)
            if raw is None:
                continue
            internaldate = self.internaldates.get(uid, "04-Sep-2026 12:34:56 -0700")
            sent = raw[:int(partial.group(1))] if partial else raw
            structure = self.structures.get(uid, "")
            prefix = (
                f'1 (UID {uid} INTERNALDATE "{internaldate}" '
                f"RFC822.SIZE {len(raw)} FLAGS (\\Seen) "
                + (f"BODYSTRUCTURE {structure} " if structure else "")
                + (f"BODY[]<0> {{{len(sent)}}}" if partial else f"BODY[] {{{len(sent)}}}")
            ).encode()
            response.append((prefix, sent))
            response.append(b")")
        return ("OK", response)

    def _uid_copy(self, uid_set, mailbox):
        raw = str(mailbox)
        assert raw.startswith('"') and raw.endswith('"'), "mailbox names must be quoted"
        name = self._unquote(mailbox)
        if self.fail_copy_to == name:
            return ("NO", [b"[TRYCREATE] Mailbox does not exist"])
        if name not in self.folders:
            return ("NO", [b"[TRYCREATE] Mailbox does not exist"])
        copied = [uid for uid in str(uid_set).split(",") if uid]
        for uid in copied:
            self.copies.append((uid, name))
        if "UIDPLUS" not in self.post_auth_capabilities:
            return ("OK", [b"COPY completed"])
        # A UIDPLUS server reports the copies' UIDs, and the caller needs
        # them: a COPY does not keep a message's UID, so without this there
        # is no way to name the copy afterwards.
        assigned = []
        for _ in copied:
            self._next_copy_uid += 1
            assigned.append(str(self._next_copy_uid))
        receipt = (f"[COPYUID 1 {','.join(copied)} {','.join(assigned)}] "
                   "COPY completed").encode()
        return ("OK", [receipt])

    def _uid_store(self, uid_set, mode, flags):
        if self.fail_store:
            return ("NO", [b"STORE failed"])
        adding = "+FLAGS" in str(mode)
        wanted = {f for f in str(flags).strip("()").split() if f}
        for uid in str(uid_set).split(","):
            if adding and "Deleted" in str(flags):
                self.deleted.add(uid)
            held = self.flags.setdefault(uid, set())
            if adding:
                held |= wanted
            else:
                held -= wanted
        return ("OK", [b"STORE completed"])

    def append(self, mailbox, flags, date_time, message):
        self._record("APPEND", (mailbox,))
        name = self._unquote(mailbox)
        if name != mailbox and not str(mailbox).startswith('"'):
            raise AssertionError("APPEND mailbox must be quoted")
        if self.fail_append:
            return ("NO", [b"APPEND failed"])
        if name not in self.folders:
            return ("NO", [b"TRYCREATE no such mailbox"])
        self.appended.append((name, str(flags), message))
        return ("OK", [b"APPEND completed"])

    def _uid_expunge(self, uid_set):
        for uid in str(uid_set).split(","):
            if uid in self.deleted:
                self.deleted.discard(uid)
                self.messages.pop(uid, None)
                self.expunged.append(uid)
        return ("OK", [b"EXPUNGE completed"])


@pytest.fixture
def fake_imap_factory():
    return FakeIMAP


class FakeResponse:
    def __init__(self, text: str = "", stop_reason: str = "end_turn", usage=None, stop_details=None):
        self.content = [type("Block", (), {"type": "text", "text": text})()] if text else []
        self.stop_reason = stop_reason
        self.usage = usage or type("Usage", (), {
            "input_tokens": 1200, "output_tokens": 180, "cache_read_input_tokens": 0
        })()
        self.stop_details = stop_details


class FakeMessages:
    def __init__(self, owner: "FakeAnthropic", beta: bool) -> None:
        self._owner = owner
        self._beta = beta

    def create(self, **kwargs):
        return self._owner._handle(kwargs, beta=self._beta)


class FakeBeta:
    def __init__(self, owner: "FakeAnthropic") -> None:
        self.messages = FakeMessages(owner, beta=True)


class FakeAnthropic:
    """Records requests and replays scripted responses."""

    def __init__(self, responses=None, handler=None):
        self.messages = FakeMessages(self, beta=False)
        self.beta = FakeBeta(self)
        self.requests: List[dict] = []
        self.beta_requests: List[dict] = []
        self._responses = list(responses or [])
        self._handler = handler

    def _handle(self, kwargs: dict, beta: bool):
        (self.beta_requests if beta else self.requests).append(kwargs)
        if self._handler is not None:
            return self._handler(kwargs, beta)
        if not self._responses:
            return FakeResponse(_default_payload())
        result = self._responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        if callable(result):
            return result(kwargs, beta)
        return result


def _default_payload() -> str:
    import json

    return json.dumps({
        "summary": "A recruiter is inviting you to interview. Book a slot this week.",
        "is_job_related": True,
        "category": "INTERVIEW",
        "other_category": "NOT_APPLICABLE",
        "confidence_score": 0.97,
        "reasoning": "The body links to calendly.com and offers times. Runner-up NEXT_STEPS rejected.",
    })


class ApiStatusError(Exception):
    """Mimics ``anthropic.APIStatusError`` closely enough for the retry logic."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class RateLimitError(ApiStatusError):
    def __init__(self, message="rate limited"):
        super().__init__(message, 429)


class APIConnectionError(Exception):
    pass


@pytest.fixture
def fake_anthropic():
    return FakeAnthropic


@pytest.fixture
def default_payload():
    return _default_payload


def make_email(uid: str = "1", **overrides) -> EmailMessage:
    defaults = dict(
        uid=uid,
        subject="Interview invitation",
        sender_name="Dana Reyes",
        sender_email="dana@northwind.example",
        date=datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc),
        body_text="We would like to schedule a technical interview.",
    )
    defaults.update(overrides)
    return EmailMessage(**defaults)


def make_classification(**overrides) -> Classification:
    defaults = dict(
        summary="A recruiter invited you to interview. Pick a slot this week.",
        is_job_related=True,
        category=Category.INTERVIEW,
        other_category=OtherCategory.NOT_APPLICABLE,
        confidence_score=0.98,
        reasoning="Calendly link plus explicit invitation.",
        model="claude-opus-5",
    )
    defaults.update(overrides)
    return Classification(**defaults)


def make_item(**overrides) -> TriageItem:
    email_kwargs = overrides.pop("email_kwargs", {})
    classification_kwargs = overrides.pop("classification_kwargs", {})
    defaults = dict(
        email=make_email(**email_kwargs),
        classification=make_classification(**classification_kwargs),
        folders=FolderPlan(),
    )
    defaults.update(overrides)
    return TriageItem(**defaults)


@pytest.fixture
def email_factory():
    return make_email


@pytest.fixture
def classification_factory():
    return make_classification


@pytest.fixture
def item_factory():
    return make_item


@pytest.fixture(scope="session")
def qapp():
    """One QApplication for the whole session, for tests that build widgets."""
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def reap_deleted_widgets():
    """Actually destroy what each test asked to be destroyed.

    ``deleteLater`` only queues the deletion, so without an event loop every
    dialog a test builds lives for the session, and ``setStyleSheet``
    restyles every live widget: a theme change took 0.17s after one dialog
    pair and 0.89s after five, enough to push a test past its timeout.
    """
    yield
    try:
        from PySide6.QtCore import QEvent
        from PySide6.QtWidgets import QApplication
    except ImportError:                      # pragma: no cover - no Qt here
        return
    app = QApplication.instance()
    if app is None:
        return
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


#: The repository root, for git calls that must run from inside it.
GIT_ROOT = Path(__file__).resolve().parents[1]


def git_binary() -> str:
    """The first git on this machine that will actually run.

    With Xcode installed but its licence never accepted, ``/usr/bin/git`` is
    a shim that refuses every command, and tests read the empty ``git
    ls-files`` as every file being untracked. Look for one that works, and
    say plainly when none does.
    """
    import shutil
    import subprocess

    candidates = [
        shutil.which("git") or "/usr/bin/git",
        "/Library/Developer/CommandLineTools/usr/bin/git",
        "/opt/homebrew/bin/git",
        "/usr/local/bin/git",
    ]
    problems = []
    for candidate in candidates:
        try:
            probe = subprocess.run([candidate, "rev-parse", "--git-dir"],
                                   cwd=GIT_ROOT, capture_output=True,
                                   text=True)
        except OSError as exc:
            problems.append(f"{candidate}: {exc}")
            continue
        if probe.returncode == 0:
            return candidate
        problems.append(f"{candidate}: {probe.stderr.strip() or probe.returncode}")
    raise RuntimeError("no working git found:\n  " + "\n  ".join(problems))


def git_lines(*arguments: str) -> list:
    """Run git and insist it worked, so a broken git cannot read as an empty
    repository."""
    import subprocess

    done = subprocess.run([git_binary(), *arguments], cwd=GIT_ROOT,
                          capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(arguments)} failed ({done.returncode}): "
            f"{done.stderr.strip()}")
    return [line for line in done.stdout.splitlines() if line]


def git_check_ignore(path: str) -> bool:
    """True when git ignores this path.

    check-ignore answers 0 for ignored and 1 for not; anything else means
    git did not answer. Read as either, a caller would fail confusingly or
    quietly stop checking that private files stay out of the repository.
    """
    import subprocess

    done = subprocess.run([git_binary(), "check-ignore", "-q", str(path)],
                          cwd=GIT_ROOT, capture_output=True, text=True)
    if done.returncode not in (0, 1):
        raise RuntimeError(
            f"git check-ignore could not run ({done.returncode}): "
            f"{done.stderr.strip()}")
    return done.returncode == 0
