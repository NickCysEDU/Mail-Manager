"""iCloud Mail IMAP client.

Connects to ``imap.mail.me.com:993`` over TLS with ``imaplib``, fetches
messages in a time window without marking them read (``BODY.PEEK``), creates
the ``Job Search`` folder tree when missing, and moves approved messages with
``UID COPY`` / ``UID STORE +FLAGS \\Deleted`` / ``UID EXPUNGE``.

Two safety properties are load-bearing:

* A message is **never** flagged ``\\Deleted`` until its ``COPY`` to the target
  mailbox has been confirmed ``OK``. A failed copy leaves the original intact.
* ``UID EXPUNGE`` (RFC 4315) is used whenever the server advertises ``UIDPLUS``
  so that unrelated messages the user had already flagged ``\\Deleted`` are not
  swept up. iCloud advertises UIDPLUS; if a server ever does not, the move
  report says so explicitly rather than expunging silently.
"""

from __future__ import annotations

import base64
import email
import email.policy
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from attachments import Attachment
import imaplib
import logging
import re
import socket
import ssl
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime
from typing import Callable, Dict, Iterator, List, Optional, Sequence, Tuple

import accounts
import html_utils
from models import DEFAULT_OTHER_ROOT, EmailMessage, FolderPlan, imap_since_date

log = logging.getLogger(__name__)

DEFAULT_HOST = "imap.mail.me.com"
DEFAULT_PORT = 993
DEFAULT_TIMEOUT = 45.0

#: UIDs per FETCH command. Small enough to stream progress, large enough to
#: avoid a round trip per message.
FETCH_BATCH = 40

#: Bytes fetched per message. Attachments sit after the text parts in every
#: real-world MIME layout, so a partial fetch keeps the readable content and
#: skips the payload. Measured on a live iCloud account: 5.2 MB -> 1.5 MB for
#: 60 messages, with the text of all 60 intact.
DEFAULT_FETCH_BYTES = 65536

#: Parallel IMAP connections used for the fetch phase. iCloud spends about
#: 27 ms of server time per message regardless of size, and that parallelises:
#: 120 messages took 9.5 s on one connection and 3.7 s on four.
DEFAULT_CONNECTIONS = 4
#: Below this many messages, extra logins cost more than they save.
PARALLEL_THRESHOLD = 24
#: UIDs per COPY/STORE command, bounded so the command line stays sane.
COMMAND_BATCH = 100

ProgressCallback = Callable[[int, int, str], None]


class IMAPError(RuntimeError):
    """Any failure talking to the mail server."""


class IMAPAuthError(IMAPError):
    """Login was rejected."""


class IMAPConnectionError(IMAPError):
    """The server could not be reached or the TLS handshake failed."""


class ScanCancelled(IMAPError):
    """The user cancelled an in-flight operation."""


# --------------------------------------------------------------------------
# Modified UTF-7 (RFC 3501 §5.1.3)
# --------------------------------------------------------------------------
def encode_mutf7(text: str) -> str:
    """Encode a mailbox name to IMAP modified UTF-7."""
    out: List[str] = []
    buffer: List[str] = []

    def flush() -> None:
        if not buffer:
            return
        encoded = base64.b64encode("".join(buffer).encode("utf-16-be")).decode("ascii")
        out.append("&" + encoded.rstrip("=").replace("/", ",") + "-")
        buffer.clear()

    for char in text:
        code = ord(char)
        if char == "&":
            flush()
            out.append("&-")
        elif 0x20 <= code <= 0x7E:
            flush()
            out.append(char)
        else:
            buffer.append(char)
    flush()
    return "".join(out)


def decode_mutf7(text: str) -> str:
    """Decode an IMAP modified UTF-7 mailbox name."""
    out: List[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char != "&":
            out.append(char)
            index += 1
            continue
        end = text.find("-", index + 1)
        if end == -1:
            # Unterminated shift sequence: emit the rest verbatim.
            out.append(text[index:])
            break
        chunk = text[index + 1:end]
        if chunk == "":
            out.append("&")
        else:
            b64 = chunk.replace(",", "/")
            b64 += "=" * (-len(b64) % 4)
            try:
                out.append(base64.b64decode(b64, validate=True).decode("utf-16-be"))
            except Exception:
                out.append(text[index:end + 1])
        index = end + 1
    return "".join(out)


#: Curly quotes and the like, which arrive when a password is copied out of a
#: web page or a note rather than typed.
_SMART_QUOTES = {
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
    "\u2032": "'", "\u2033": '"', "\u00a0": " ",
}


def clean_secret(secret: str) -> str:
    """Tidy a pasted password without changing what it actually is.

    Pasting is how most app passwords arrive, and a paste brings things with
    it: a trailing newline, a wrapping pair of quotes from a note, curly
    quotes from a web page, a non-breaking space. A newline is the damaging
    one - imaplib puts the password inside a quoted string, and a line ending
    inside that string ends the command early, so the server sees a quote that
    never closes and answers "unmatch quote", which reads as a wrong password.
    """
    text = str(secret or "")
    for wrong, right in _SMART_QUOTES.items():
        text = text.replace(wrong, right)
    # Anything that would end or corrupt the command line.
    text = "".join(ch for ch in text if ch >= " " and ch != "\x7f")
    text = text.strip()
    # A password copied with its surrounding quotes.
    for quote in ('"', "'"):
        if len(text) >= 2 and text.startswith(quote) and text.endswith(quote):
            text = text[1:-1].strip()
    return text


def quote_mailbox(name: str) -> str:
    """Quote + mUTF-7 encode a mailbox name for use as a command argument."""
    encoded = encode_mutf7(name)
    escaped = encoded.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


# --------------------------------------------------------------------------
# Response parsing
# --------------------------------------------------------------------------
_LIST_RE = re.compile(
    rb'^\((?P<flags>[^)]*)\)\s+(?:"(?P<delim>(?:\\.|[^"\\])*)"|(?P<nil>NIL))\s+(?P<name>.+)$',
    re.DOTALL,
)
_UID_RE = re.compile(rb"\bUID\s+(\d+)")
_SIZE_RE = re.compile(rb"\bRFC822\.SIZE\s+(\d+)")
_FLAGS_RE = re.compile(rb"\bFLAGS\s+\(([^)]*)\)")
_INTERNALDATE_RE = re.compile(
    rb'\bINTERNALDATE\s+"\s*(\d{1,2})-([A-Za-z]{3})-(\d{4})\s+'
    rb'(\d{2}):(\d{2}):(\d{2})\s+([+-]\d{4})"'
)
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


@dataclass(frozen=True)
class MailboxInfo:
    """One entry from a ``LIST`` response."""

    name: str
    delimiter: str
    flags: Tuple[str, ...] = ()

    @property
    def selectable(self) -> bool:
        return "\\noselect" not in {f.lower() for f in self.flags}


def parse_list_line(line) -> Optional[MailboxInfo]:
    """Parse a single ``LIST`` response item into a :class:`MailboxInfo`."""
    if line is None:
        return None
    if isinstance(line, tuple):
        # Literal form: (b'(\\HasNoChildren) "/" {7}', b'Archive')
        prefix, literal = line[0], line[1]
        if isinstance(prefix, str):
            prefix = prefix.encode("utf-8", "surrogateescape")
        if isinstance(literal, str):
            literal = literal.encode("utf-8", "surrogateescape")
        prefix = re.sub(rb"\{\d+\}\s*$", b"", prefix.rstrip())
        quoted = b'"' + literal.replace(b"\\", b"\\\\").replace(b'"', b'\\"') + b'"'
        return parse_list_line(prefix + b" " + quoted)
    if isinstance(line, str):
        line = line.encode("utf-8", "surrogateescape")
    match = _LIST_RE.match(line.strip())
    if not match:
        return None

    flags = tuple(
        flag.decode("ascii", "replace")
        for flag in match.group("flags").split()
    )
    delim_raw = match.group("delim")
    delimiter = "" if delim_raw is None else delim_raw.decode("ascii", "replace").replace("\\\\", "\\")

    raw_name = match.group("name").strip()
    if raw_name.startswith(b'"') and raw_name.endswith(b'"') and len(raw_name) >= 2:
        raw_name = raw_name[1:-1]
        raw_name = raw_name.replace(b'\\"', b'"').replace(b"\\\\", b"\\")
    name = decode_mutf7(raw_name.decode("ascii", "replace"))
    return MailboxInfo(name=name, delimiter=delimiter, flags=flags)


def parse_internaldate(blob: bytes) -> Optional[datetime]:
    """Extract an ``INTERNALDATE`` as a timezone-aware UTC datetime."""
    match = _INTERNALDATE_RE.search(blob or b"")
    if not match:
        return None
    day, mon, year, hour, minute, second, zone = match.groups()
    month = _MONTHS.get(mon.decode("ascii", "replace").lower())
    if not month:
        return None
    sign = 1 if zone[:1] == b"+" else -1
    offset = timedelta(
        hours=int(zone[1:3]),
        minutes=int(zone[3:5]),
    ) * sign
    try:
        local = datetime(
            int(year), month, int(day), int(hour), int(minute), int(second),
            tzinfo=timezone(offset),
        )
    except ValueError:
        return None
    return local.astimezone(timezone.utc)


def _decode_header_value(value) -> str:
    """Decode an RFC 2047 header into a plain string, never raising."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    text = str(value)
    if "=?" not in text:
        return text.strip()
    try:
        return str(make_header(decode_header(text))).strip()
    except Exception:
        parts: List[str] = []
        try:
            for chunk, charset in decode_header(text):
                if isinstance(chunk, bytes):
                    parts.append(chunk.decode(charset or "utf-8", "replace"))
                else:
                    parts.append(chunk)
            return "".join(parts).strip()
        except Exception:
            return text.strip()


def _part_text(part: Message) -> str:
    """Decode one MIME part to text, tolerating broken charsets."""
    try:
        payload = part.get_payload(decode=True)
    except Exception:
        payload = None
    if payload is None:
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""
    charset = part.get_content_charset() or "utf-8"
    for candidate in (charset, "utf-8", "latin-1"):
        try:
            return payload.decode(candidate, "strict")
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", "replace")


def extract_body(message: Message) -> Tuple[html_utils.ExtractedText, Tuple[str, ...]]:
    """Reduce a parsed message to plain text plus attachment filenames.

    ``text/plain`` wins unless it is essentially empty (a very common pattern:
    a one-line "view this email in your browser" fallback next to the real
    HTML body), in which case the HTML alternative is used.
    """
    plain_parts: List[str] = []
    html_parts: List[str] = []
    attachments: List[str] = []

    for part in message.walk():
        if part.is_multipart():
            continue
        content_type = (part.get_content_type() or "").lower()
        disposition = (part.get_content_disposition() or "").lower()
        filename = part.get_filename()
        if filename:
            attachments.append(_decode_header_value(filename))
        if disposition == "attachment":
            continue
        if content_type == "text/plain":
            text = _part_text(part)
            # Some ATS mail sends a full HTML document as text/plain.
            if html_utils.looks_like_html(text):
                html_parts.append(text)
            else:
                plain_parts.append(text)
        elif content_type == "text/html":
            html_parts.append(_part_text(part))

    plain = "\n\n".join(p for p in plain_parts if p).strip()
    html = "\n\n".join(p for p in html_parts if p).strip()

    if plain and len(plain) >= 40 and not html_utils.looks_like_html(plain):
        extracted = html_utils.plain_to_text(plain)
        if html:
            # Recover link targets the plain-text alternative usually loses.
            from_html = html_utils.html_to_text(html)
            merged = list(extracted.links)
            for link in from_html.links:
                if link not in merged and len(merged) < 40:
                    merged.append(link)
            extracted = html_utils.ExtractedText(
                extracted.text, tuple(merged), html_utils.notable_links(merged)
            )
    elif html:
        extracted = html_utils.html_to_text(html)
        if plain and not extracted.text:
            extracted = html_utils.plain_to_text(plain)
    else:
        extracted = html_utils.plain_to_text(plain)

    return extracted, tuple(dict.fromkeys(attachments))


def parse_message(
    raw: bytes,
    uid: str,
    internaldate: Optional[datetime] = None,
    flags: Sequence[str] = (),
    size: int = 0,
    source_folder: str = "INBOX",
) -> EmailMessage:
    """Turn raw RFC 822 bytes into an :class:`~models.EmailMessage`."""
    try:
        message = email.message_from_bytes(raw, policy=email.policy.compat32)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Could not parse message UID %s: %s", uid, exc)
        return EmailMessage(
            uid=uid,
            subject="(unreadable message)",
            date=internaldate,
            body_text="This message could not be parsed.",
            size=size,
            flags=tuple(flags),
            source_folder=source_folder,
        )

    subject = _decode_header_value(message.get("Subject"))
    from_header = _decode_header_value(message.get("From"))
    sender_name, sender_email = parseaddr(from_header)
    if not sender_name and sender_email:
        sender_name = ""
    sender_name = _decode_header_value(sender_name)

    date = internaldate
    if date is None:
        try:
            header_date = message.get("Date")
            if header_date:
                parsed = parsedate_to_datetime(str(header_date))
                if parsed is not None:
                    date = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
                    date = date.astimezone(timezone.utc)
        except (TypeError, ValueError):
            date = None

    extracted, attachments = extract_body(message)

    return EmailMessage(
        uid=uid,
        subject=subject,
        sender_name=sender_name,
        sender_email=sender_email,
        date=date,
        body_text=extracted.text,
        message_id=_decode_header_value(message.get("Message-ID")),
        in_reply_to=_decode_header_value(message.get("In-Reply-To")),
        references=_decode_header_value(message.get("References")),
        to=_decode_header_value(message.get("To")),
        reply_to=_decode_header_value(message.get("Reply-To")),
        list_unsubscribe=_decode_header_value(message.get("List-Unsubscribe")),
        auto_submitted=_decode_header_value(message.get("Auto-Submitted")),
        precedence=_decode_header_value(message.get("Precedence")),
        x_auto_response_suppress=_decode_header_value(
            message.get("X-Auto-Response-Suppress")),
        size=size or len(raw),
        flags=tuple(flags),
        links=extracted.links,
        attachments=attachments,
        source_folder=source_folder,
    )


# --------------------------------------------------------------------------
# Move planning / reporting
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class MovePlan:
    """A single approved move."""

    uid: str
    target_folder: str
    subject: str = ""
    #: Which mailbox the message is in. Empty on a single-account setup.
    account_id: str = ""
    #: Which folder it is in right now. Empty means the account's configured
    #: source mailbox, which is the ordinary case; undo sets it, because by
    #: then each message is sitting in whichever folder it was filed into.
    source_folder: str = ""


@dataclass
class MoveReport:
    """Outcome of :meth:`IMAPEngine.move_messages`."""

    moved: Dict[str, str] = field(default_factory=dict)          # uid -> folder
    failed: Dict[str, str] = field(default_factory=dict)         # uid -> error
    created_folders: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    expunged: bool = False
    #: Where each message ended up: source uid -> the uid it was given in the
    #: destination folder. A COPY assigns a new one, and without this the
    #: only way back was to guess that it had not - which is wrong on every
    #: server that has ever been written.
    new_uids: Dict[str, str] = field(default_factory=dict)

    @property
    def moved_count(self) -> int:
        return len(self.moved)

    @property
    def failed_count(self) -> int:
        return len(self.failed)

    def describe(self) -> str:
        parts = [f"{self.moved_count} moved"]
        if self.failed:
            parts.append(f"{self.failed_count} failed")
        if self.created_folders:
            parts.append(f"{len(self.created_folders)} folder(s) created")
        return ", ".join(parts)


@dataclass
class ScanResult:
    """Outcome of :meth:`IMAPEngine.fetch_window`."""

    messages: List[EmailMessage] = field(default_factory=list)
    candidate_uids: List[str] = field(default_factory=list)
    truncated_to_max: bool = False
    warnings: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------
class IMAPEngine:
    """A stateful connection to an IMAP account.

    Not thread safe: one engine per worker thread. The GUI creates a fresh
    engine for each scan and each apply operation.
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        timeout: float = DEFAULT_TIMEOUT,
        ssl_context: Optional[ssl.SSLContext] = None,
        connection_factory: Optional[Callable[..., imaplib.IMAP4]] = None,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.timeout = timeout
        self._ssl_context = ssl_context
        self._connection_factory = connection_factory
        self.conn: Optional[imaplib.IMAP4] = None
        #: Held in memory only, so the fetch phase can open extra connections.
        self._credentials: Optional[Tuple[str, str]] = None
        self.capabilities: Tuple[str, ...] = ()
        self.delimiter: str = "/"
        self._selected: Optional[str] = None
        self._selected_readonly: Optional[bool] = None

    # -- lifecycle -------------------------------------------------------
    def connect(self, email_address: str, password: str) -> None:
        """Open a TLS connection and authenticate."""
        email_address = clean_secret(email_address)
        password = clean_secret(password)
        if not email_address:
            raise IMAPAuthError("Enter the mailbox email address.")
        if not password:
            raise IMAPAuthError(
                "Enter the app password for this mailbox. Ordinary account passwords "
                "are rejected by IMAP when two-factor authentication is enabled."
            )
        if " " in email_address:
            raise IMAPAuthError(
                f"“{email_address}” has a space in it, which is not a valid "
                "email address. Check for a stray character."
            )

        try:
            if self._connection_factory is not None:
                self.conn = self._connection_factory(self.host, self.port)
            else:
                context = self._ssl_context or ssl.create_default_context()
                self.conn = imaplib.IMAP4_SSL(
                    self.host, self.port, ssl_context=context, timeout=self.timeout
                )
        except (ssl.SSLError, socket.gaierror, socket.timeout, OSError) as exc:
            raise IMAPConnectionError(
                f"Could not reach {self.host}:{self.port} - {exc}"
            ) from exc
        except imaplib.IMAP4.error as exc:
            raise IMAPConnectionError(f"Server refused the connection: {exc}") from exc

        try:
            self._authenticate(email_address, password)
        except imaplib.IMAP4.error as exc:
            detail = _error_text(exc)
            self._safe_shutdown()
            if "AUTHENTICATIONFAILED" in detail.upper() or "invalid credentials" in detail.lower():
                raise IMAPAuthError(
                    "The server rejected those credentials. "
                    + accounts.credential_hint(self.host)
                ) from exc
            raise IMAPAuthError(f"Login failed: {detail}") from exc
        except (OSError, socket.timeout) as exc:
            self._safe_shutdown()
            raise IMAPConnectionError(f"Connection dropped during login: {exc}") from exc

        self._credentials = (email_address.strip(), password)
        self.capabilities = self._read_capabilities()
        self.delimiter = self._detect_delimiter()
        log.info("Connected to %s (capabilities: %d, delimiter %r)",
                 self.host, len(self.capabilities), self.delimiter)

    def _authenticate(self, email_address: str, password: str) -> None:
        """Sign in, preferring SASL PLAIN over the LOGIN command.

        LOGIN puts both values into a command line and quotes only the
        password, so anything unusual in either has to survive being parsed as
        IMAP syntax. AUTHENTICATE PLAIN sends them base64 encoded, where no
        character means anything, and every provider this app knows about
        advertises it. LOGIN remains the fallback for anything that does not.
        """
        conn = self._require_conn()
        capabilities = {c.upper() for c in getattr(conn, "capabilities", ())}
        if "AUTH=PLAIN" in capabilities and hasattr(conn, "authenticate"):
            try:
                conn.authenticate(
                    "PLAIN",
                    lambda _challenge: f"\0{email_address}\0{password}".encode("utf-8"),
                )
                return
            except imaplib.IMAP4.error as exc:
                # A refusal is a refusal; only fall back when the mechanism
                # itself was the problem.
                if "AUTHENTICATIONFAILED" in _error_text(exc).upper():
                    raise
                log.debug("SASL PLAIN did not work (%s); trying LOGIN.", exc)
        conn.login(email_address, password)

    def _read_capabilities(self) -> Tuple[str, ...]:
        """Read the server's capabilities *after* authenticating.

        This matters more than it looks. imaplib caches the capability list
        from the pre-auth greeting, and iCloud's greeting advertises eight
        capabilities while its post-login list advertises twenty-one - UIDPLUS
        among them. Trusting the cached set would make the app believe UIDPLUS
        is unavailable and fall back to a full EXPUNGE of the source mailbox,
        which is exactly the destructive path this engine exists to avoid.
        """
        conn = self._require_conn()
        found: List[str] = []
        try:
            typ, data = conn.capability()
            if typ == "OK":
                for chunk in data or ():
                    if isinstance(chunk, (bytes, bytearray)):
                        found.extend(chunk.decode("ascii", "replace").split())
                    elif isinstance(chunk, str):
                        found.extend(chunk.split())
        except (imaplib.IMAP4.error, OSError) as exc:
            log.warning("Post-login CAPABILITY failed (%s); using the greeting's list.", exc)

        if not found:
            found = [
                cap.decode("ascii", "replace") if isinstance(cap, bytes) else str(cap)
                for cap in (conn.capabilities or ())
            ]
        # Keep whatever the greeting advertised too; the union is what the
        # server has told us it supports.
        for cap in (conn.capabilities or ()):
            name = cap.decode("ascii", "replace") if isinstance(cap, bytes) else str(cap)
            if name not in found:
                found.append(name)
        return tuple(found)

    def has_capability(self, name: str) -> bool:
        upper = name.upper()
        return any(cap.upper() == upper or cap.upper().startswith(upper + "=") for cap in self.capabilities)

    def logout(self) -> None:
        if self.conn is None:
            return
        try:
            if self._selected is not None:
                try:
                    self.conn.close()
                except Exception:
                    pass
            self.conn.logout()
        except Exception:
            self._safe_shutdown()
        finally:
            self.conn = None
            self._credentials = None
            self._selected = None
            self._selected_readonly = None

    def _safe_shutdown(self) -> None:
        try:
            if self.conn is not None:
                self.conn.shutdown()
        except Exception:
            pass
        finally:
            self.conn = None

    def __enter__(self) -> "IMAPEngine":
        return self

    def __exit__(self, *exc_info) -> None:
        self.logout()

    @contextmanager
    def session(self, email_address: str, password: str) -> Iterator["IMAPEngine"]:
        self.connect(email_address, password)
        try:
            yield self
        finally:
            self.logout()

    # -- low level -------------------------------------------------------
    def _require_conn(self) -> imaplib.IMAP4:
        if self.conn is None:
            raise IMAPError("Not connected. Call connect() first.")
        return self.conn

    def _cmd(self, description: str, func: Callable[..., Tuple[str, list]], *args) -> list:
        """Run an imaplib call, converting failures into :class:`IMAPError`."""
        try:
            typ, data = func(*args)
        except imaplib.IMAP4.abort as exc:
            raise IMAPConnectionError(f"{description} failed - the server closed the connection: {_error_text(exc)}") from exc
        except imaplib.IMAP4.error as exc:
            raise IMAPError(f"{description} failed: {_error_text(exc)}") from exc
        except (OSError, socket.timeout) as exc:
            raise IMAPConnectionError(f"{description} failed: {exc}") from exc
        if typ != "OK":
            detail = _first_text(data)
            raise IMAPError(f"{description} failed: {detail or typ}")
        return data

    def _detect_delimiter(self) -> str:
        """Ask the server for its hierarchy delimiter instead of assuming '/'."""
        try:
            data = self._cmd("Reading the mailbox hierarchy", self._require_conn().list, '""', '""')
        except IMAPError:
            return "/"
        for line in data or ():
            info = parse_list_line(line)
            if info and info.delimiter:
                return info.delimiter
        for line in data or ():
            info = parse_list_line(line)
            if info:
                return info.delimiter or "/"
        return "/"

    # -- folders ---------------------------------------------------------
    def list_folders(self) -> List[MailboxInfo]:
        data = self._cmd("Listing mailboxes", self._require_conn().list)
        folders: List[MailboxInfo] = []
        for line in data or ():
            info = parse_list_line(line)
            if info and info.name:
                folders.append(info)
        return folders

    def folder_names(self) -> List[str]:
        return [f.name for f in self.list_folders()]

    def folder_plan(self, root: str, other_root: str = DEFAULT_OTHER_ROOT) -> FolderPlan:
        """A :class:`FolderPlan` bound to this server's hierarchy delimiter."""
        return FolderPlan(root=root, delimiter=self.delimiter or "/", other_root=other_root)

    def ensure_folders(
        self,
        plan: FolderPlan,
        subscribe: bool = True,
        progress: Optional[ProgressCallback] = None,
    ) -> List[str]:
        """Create the whole ``Job Search`` tree; returns the folders created."""
        return self.ensure_folder_paths(plan.all_folders, subscribe=subscribe, progress=progress)

    def ensure_folder_paths(
        self,
        folders: Sequence[str],
        subscribe: bool = True,
        progress: Optional[ProgressCallback] = None,
    ) -> List[str]:
        """Create any of ``folders`` that does not already exist.

        Parents must appear before children in ``folders``; :class:`FolderPlan`
        guarantees that ordering for the trees it builds.
        """
        wanted = [f for f in dict.fromkeys(folders) if f]
        if not wanted:
            return []
        existing = {name.lower() for name in self.folder_names()}
        created: List[str] = []
        for index, folder in enumerate(wanted, start=1):
            if progress:
                progress(index, len(wanted), f"Checking folder “{folder}”")
            if folder.lower() in existing:
                continue
            self._create_folder(folder)
            created.append(folder)
            existing.add(folder.lower())
            if subscribe:
                self._subscribe_folder(folder)
        return created

    def _create_folder(self, folder: str) -> None:
        conn = self._require_conn()
        try:
            typ, data = conn.create(quote_mailbox(folder))
        except imaplib.IMAP4.error as exc:
            detail = _error_text(exc)
            if "ALREADYEXISTS" in detail.upper():
                return
            raise IMAPError(f"Could not create “{folder}”: {detail}") from exc
        if typ != "OK":
            detail = _first_text(data)
            if "ALREADYEXISTS" in (detail or "").upper():
                return
            raise IMAPError(f"Could not create “{folder}”: {detail or typ}")
        log.info("Created mailbox %s", folder)

    def _subscribe_folder(self, folder: str) -> None:
        try:
            self._require_conn().subscribe(quote_mailbox(folder))
        except Exception as exc:  # subscription is a nicety, never fatal
            log.debug("Could not subscribe to %s: %s", folder, exc)

    # -- selection -------------------------------------------------------
    def select(self, mailbox: str = "INBOX", readonly: bool = True) -> int:
        if self._selected == mailbox and self._selected_readonly == readonly:
            return -1
        if self._selected is not None:
            try:
                self._require_conn().close()
            except Exception:
                pass
            self._selected = None
        data = self._cmd(
            f"Opening “{mailbox}”",
            self._require_conn().select,
            quote_mailbox(mailbox),
            readonly,
        )
        self._selected = mailbox
        self._selected_readonly = readonly
        try:
            return int(_first_text(data) or 0)
        except (TypeError, ValueError):
            return 0

    # -- search / fetch --------------------------------------------------
    def search_window(self, start: datetime, end: Optional[datetime] = None) -> List[str]:
        """UIDs whose INTERNALDATE falls in ``[start, end)``.

        IMAP ``SINCE`` has one-day granularity, so the server-side filter is
        deliberately widened by a day at each edge and the exact bound is then
        applied client-side against the real ``INTERNALDATE``.
        """
        conn = self._require_conn()
        criteria: List[str] = ["SINCE", imap_since_date(start - timedelta(days=1))]
        if end is not None:
            criteria += ["BEFORE", imap_since_date(end + timedelta(days=2))]
        data = self._cmd("Searching", conn.uid, "SEARCH", None, *criteria)
        raw = _first_bytes(data)
        if not raw:
            return []
        return [uid.decode("ascii", "replace") for uid in raw.split() if uid.isdigit()]

    def fetch_window(
        self,
        start: datetime,
        end: Optional[datetime] = None,
        mailbox: str = "INBOX",
        max_messages: int = 400,
        progress: Optional[ProgressCallback] = None,
        cancel: Optional[threading.Event] = None,
        connections: int = DEFAULT_CONNECTIONS,
        max_bytes: int = DEFAULT_FETCH_BYTES,
        on_batch: Optional[Callable[[List[EmailMessage]], None]] = None,
    ) -> ScanResult:
        """Fetch every message in the window, newest first, without marking read.

        ``on_batch``, if given, is called with each group of messages as it
        arrives, so a caller can start work on them rather than waiting for
        the whole window. It is called from whichever thread did the fetching,
        which on a parallel scan is not the caller's, and the messages it is
        handed are the same objects that end up in the result.
        """
        result = ScanResult()
        self.select(mailbox, readonly=True)

        if progress:
            progress(0, 1, "Searching the mailbox…")
        uids = self.search_window(start, end)
        _check_cancel(cancel)

        # Newest first, then cap.
        ordered = sorted(uids, key=lambda value: int(value), reverse=True)
        if max_messages and len(ordered) > max_messages:
            result.truncated_to_max = True
            result.warnings.append(
                f"The window contains {len(ordered)} messages; only the {max_messages} "
                "most recent were fetched. Raise “Max messages per scan” in Settings "
                "to include the rest."
            )
            ordered = ordered[:max_messages]
        result.candidate_uids = list(ordered)

        total = len(ordered)
        if total == 0:
            if progress:
                progress(1, 1, "No messages in this window.")
            return result

        workers = 1
        if connections > 1 and total >= PARALLEL_THRESHOLD:
            # Roughly 20 messages per connection: a sibling login costs about
            # 0.9 s and saves about 27 ms per message it takes over, so it pays
            # for itself well before 20. Measured on a live account: 120
            # messages took 9.1 s on one connection and 3.7 s on four.
            workers = max(1, min(int(connections), 8, (total + 19) // 20))

        if workers > 1:
            messages = self._fetch_parallel(
                ordered, mailbox, workers, max_bytes, progress, cancel, result,
                on_batch,
            )
        else:
            messages = self._fetch_serial(ordered, mailbox, max_bytes, progress,
                                          cancel, on_batch)

        for message in messages:
            if message.date is not None:
                if message.date < start:
                    continue
                if end is not None and message.date > end:
                    continue
            result.messages.append(message)

        result.messages.sort(
            key=lambda m: (m.date or datetime.min.replace(tzinfo=timezone.utc)), reverse=True
        )
        return result

    # -- attachments ------------------------------------------------------
    def message_size(self, uid: str) -> int:
        """RFC822.SIZE for one message, or 0 if the server will not say.

        Asked before fetching, so a fifty-megabyte message can be declined
        rather than discovered halfway down a hotel connection.
        """
        conn = self._require_conn()
        try:
            data = self._cmd("Checking size", conn.uid, "FETCH", str(uid),
                             "(RFC822.SIZE)")
        except Exception:      # noqa: BLE001 - a size is a courtesy
            return 0
        for item in data or ():
            blob = item if isinstance(item, bytes) else (
                item[0] if isinstance(item, tuple) and item else b"")
            found = _SIZE_RE.search(blob or b"")
            if found:
                return int(found.group(1))
        return 0

    def describe_attachments(self, uid: str) -> List["Attachment"]:
        """What is attached, without downloading any of it.

        One FETCH of BODYSTRUCTURE. The window can open on this alone, which
        is the difference between a list appearing at once and waiting for
        six megabytes of somebody's holiday photographs first.
        """
        import attachments as _attachments

        conn = self._require_conn()
        data = self._cmd("Listing attachments", conn.uid, "FETCH", str(uid),
                         "(BODYSTRUCTURE)")
        blob = b""
        for item in data or ():
            blob += item if isinstance(item, bytes) else (
                item[0] if isinstance(item, tuple) and item else b"")
        found = []
        for part in parse_bodystructure(blob):
            name = part.get("name") or ""
            content_type = (part.get("content_type") or "").lower()
            disposition = part.get("disposition") or ""
            if content_type.startswith("multipart/"):
                continue
            if not name and disposition != "attachment" and not part.get("cid"):
                continue
            if not name:
                name = part.get("cid") or f"part-{part['part']}"
            found.append(_attachments.Attachment(
                part=part["part"], name=name, content_type=content_type,
                encoding=part.get("encoding", ""), size=int(part.get("size") or 0),
                cid=part.get("cid", ""), inline=(disposition == "inline"),
                data=None))
        return found

    def fetch_part(self, uid: str, part: str, encoding: str = "",
                   limit: int = 0) -> bytes:
        """One MIME section, decoded. Nothing else comes down the wire.

        This is what makes opening an attachment quick: asking for section 3
        of a message fetches that section, not the message. A six megabyte
        mail with three attachments used to cost six megabytes to look at any
        one of them.
        """
        import attachments as _attachments

        ceiling = int(limit) if limit else _attachments.MAX_FETCH
        ceiling = max(1, min(ceiling, _attachments.MAX_FETCH))
        conn = self._require_conn()
        item = f"BODY.PEEK[{part}]<0.{ceiling}>"
        data = self._cmd("Fetching attachment", conn.uid, "FETCH", str(uid),
                         f"({item})")
        raw = b""
        for entry in data or ():
            if isinstance(entry, tuple) and len(entry) >= 2 and entry[1]:
                raw = entry[1]
                break
        return _decode_part(raw, encoding)

    def fetch_attachments(self, uid: str, limit: int = 0) -> List["Attachment"]:
        """Every attached part of one message, with its bytes.

        The whole message is fetched rather than individual sections: the
        alternative is parsing BODYSTRUCTURE by hand, and Python's email
        package already decodes base64, quoted-printable and the header
        encodings correctly. BODY.PEEK keeps the message unread, as
        everywhere else here.

        ``limit`` caps the fetch. Zero means the module default.
        """
        import attachments as _attachments

        ceiling = int(limit) if limit else _attachments.MAX_FETCH
        ceiling = max(1, min(ceiling, _attachments.MAX_FETCH))
        conn = self._require_conn()
        data = self._cmd(
            "Fetching attachments", conn.uid, "FETCH", str(uid),
            f"(BODY.PEEK[]<0.{ceiling}>)",
        )
        raw = b""
        for item in data or ():
            if isinstance(item, tuple) and len(item) >= 2 and item[1]:
                raw = item[1]
                break
        if not raw:
            return []
        return attachments_of(raw)

    def _fetch_serial(
        self, uids: Sequence[str], mailbox: str, max_bytes: int,
        progress: Optional[ProgressCallback], cancel: Optional[threading.Event],
        on_batch: Optional[Callable[[List[EmailMessage]], None]] = None,
    ) -> List[EmailMessage]:
        messages: List[EmailMessage] = []
        total = len(uids)
        fetched = 0
        for batch in _chunks(list(uids), FETCH_BATCH):
            _check_cancel(cancel)
            arrived = self._fetch_batch(batch, mailbox, max_bytes)
            messages.extend(arrived)
            if on_batch and arrived:
                on_batch(arrived)
            fetched += len(batch)
            if progress:
                progress(min(fetched, total), total,
                         f"Fetched {min(fetched, total)} of {total} messages…")
        return messages

    def _fetch_parallel(
        self, uids: Sequence[str], mailbox: str, workers: int, max_bytes: int,
        progress: Optional[ProgressCallback], cancel: Optional[threading.Event],
        result: ScanResult,
        on_batch: Optional[Callable[[List[EmailMessage]], None]] = None,
    ) -> List[EmailMessage]:
        """Fetch across several connections at once.

        iCloud spends roughly the same server time per message whatever its
        size, and that cost parallelises cleanly: on a live account, 120
        messages took 9.5 s on one connection and 3.7 s on four.
        """
        shards = [list(uids[index::workers]) for index in range(workers)]
        collected: List[EmailMessage] = []
        done = 0
        total = len(uids)
        lock = threading.Lock()

        def run(index: int, shard: List[str]) -> List[EmailMessage]:
            nonlocal done
            engine = self if index == 0 else self._clone()
            try:
                if index != 0:
                    engine.select(mailbox, readonly=True)
                out: List[EmailMessage] = []
                for batch in _chunks(shard, FETCH_BATCH):
                    _check_cancel(cancel)
                    arrived = engine._fetch_batch(batch, mailbox, max_bytes)
                    out.extend(arrived)
                    with lock:
                        done += len(batch)
                        current = done
                        # Inside the lock: several connections finish at once,
                        # and the consumer is not expecting to be called from
                        # two of them at the same moment.
                        if on_batch and arrived:
                            on_batch(arrived)
                    if progress:
                        progress(min(current, total), total,
                                 f"Fetched {min(current, total)} of {total} messages "
                                 f"on {workers} connections…")
                return out
            finally:
                if index != 0:
                    engine.logout()

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="triage-imap") as pool:
            futures = [pool.submit(run, index, shard)
                       for index, shard in enumerate(shards) if shard]
            for future in futures:
                try:
                    collected.extend(future.result())
                except ScanCancelled:
                    raise
                except IMAPError as exc:
                    # One connection failing must not lose the whole scan; the
                    # rest of the shards still return their messages.
                    log.warning("A parallel fetch connection failed: %s", exc)
                    result.warnings.append(
                        f"One of {workers} fetch connections failed ({exc}); "
                        "some messages in this window may be missing."
                    )
        return collected

    def _fetch_batch(
        self, uids: Sequence[str], mailbox: str, max_bytes: int = DEFAULT_FETCH_BYTES
    ) -> List[EmailMessage]:
        conn = self._require_conn()
        uid_set = ",".join(uids)
        body_item = f"BODY.PEEK[]<0.{int(max_bytes)}>" if max_bytes > 0 else "BODY.PEEK[]"
        # BODYSTRUCTURE costs nothing and is the only way to know what is
        # attached to a message this fetch is deliberately truncating. Without
        # it a six megabyte message reports whichever attachments happened to
        # begin inside the first sixty-four kilobytes, which is not a number
        # anybody can act on.
        data = self._cmd(
            "Fetching messages",
            conn.uid,
            "FETCH",
            uid_set,
            f"(UID INTERNALDATE RFC822.SIZE FLAGS BODYSTRUCTURE {body_item})",
        )
        messages: List[EmailMessage] = []
        for item in data or ():
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            prefix, raw = item[0], item[1]
            if not isinstance(prefix, (bytes, bytearray)):
                continue
            if not isinstance(raw, (bytes, bytearray)):
                continue
            uid_match = _UID_RE.search(prefix)
            if not uid_match:
                continue
            uid = uid_match.group(1).decode("ascii")
            size_match = _SIZE_RE.search(prefix)
            flags_match = _FLAGS_RE.search(prefix)
            flags = tuple(
                flag.decode("ascii", "replace")
                for flag in (flags_match.group(1).split() if flags_match else ())
            )
            true_size = int(size_match.group(1)) if size_match else len(raw)
            message = parse_message(
                bytes(raw),
                uid=uid,
                internaldate=parse_internaldate(prefix),
                flags=flags,
                size=true_size,
                source_folder=mailbox,
            )
            if max_bytes > 0 and true_size > len(raw):
                # Only part of the message was downloaded; say so rather than
                # letting the classifier believe it saw everything.
                message.truncated = True
                message.original_length = true_size
            described = attachment_names(parse_bodystructure(bytes(prefix)))
            if described:
                # The server's own list beats whatever survived the cut.
                message.attachments = described
            messages.append(message)
        if len(messages) < len(uids):
            # Every skipped item above is a message the caller asked for and
            # will never see. Nothing observed in the wild reaches this, but a
            # silent shortfall is the kind of thing that should never be
            # silent, so say so rather than quietly returning fewer.
            log.warning(
                "Fetched %d of %d requested messages from %s; the server returned "
                "something unparseable for the rest.",
                len(messages), len(uids), mailbox,
            )
        return messages

    #: Where each provider keeps drafts. Checked in order against what the
    #: server actually lists, because the name is not standard.
    DRAFT_CANDIDATES = ("Drafts", "INBOX.Drafts", "[Gmail]/Drafts",
                        "[Google Mail]/Drafts", "Draft")

    def drafts_mailbox(self) -> Optional[str]:
        """The account's Drafts mailbox, found rather than guessed.

        Preference is given to whichever folder the server flags \\Drafts,
        since that is the one the mail client will show.
        """
        try:
            listed = self.list_folders()
        except IMAPError:
            return None
        for info in listed:
            if any("drafts" in flag.lower() for flag in info.flags):
                return info.name
        names = {info.name.lower(): info.name for info in listed}
        for candidate in self.DRAFT_CANDIDATES:
            if candidate.lower() in names:
                return names[candidate.lower()]
        return None

    def save_draft(self, raw: bytes, mailbox: Optional[str] = None) -> str:
        """Append a message to Drafts. Returns the mailbox it went to."""
        target = mailbox or self.drafts_mailbox()
        if not target:
            raise IMAPError(
                "This account has no Drafts mailbox, so there is nowhere to put "
                "the reply. Create one called Drafts and try again."
            )
        conn = self._require_conn()
        self._cmd("Saving a draft", conn.append, quote_mailbox(target),
                  "(\\Draft)", None, raw)
        return target

    #: Flags a rule is allowed to set. Anything else is somebody's typo, or a
    #: flag the server would reject and take the whole command down with it.
    SETTABLE_FLAGS = {"seen": r"\Seen", "flagged": r"\Flagged",
                      "answered": r"\Answered"}

    def set_flags(self, uids: Sequence[str], flags: Sequence[str],
                  add: bool = True, mailbox: str = "INBOX") -> int:
        """Add or remove flags on messages. Returns how many were touched.

        The mailbox is selected writable first, which SELECT will not do while
        a scan holds it read-only, so this always re-selects.
        """
        wanted = [self.SETTABLE_FLAGS[f] for f in flags
                  if f in self.SETTABLE_FLAGS]
        uids = [str(u).strip() for u in uids if str(u).strip().isdigit()]
        if not wanted or not uids:
            return 0
        conn = self._require_conn()
        self.select(mailbox, readonly=False)
        mode = "+FLAGS.SILENT" if add else "-FLAGS.SILENT"
        touched = 0
        for batch in _chunks(uids, COMMAND_BATCH):
            self._cmd(
                f"Setting flags on {len(batch)} message"
                f"{'' if len(batch) == 1 else 's'}",
                conn.uid, "STORE", ",".join(batch), mode,
                "(" + " ".join(wanted) + ")",
            )
            touched += len(batch)
        return touched

    def _clone(self) -> "IMAPEngine":
        """A second connection to the same account, for parallel fetching."""
        if not self._credentials:
            raise IMAPError("Not connected.")
        sibling = IMAPEngine(
            host=self.host, port=self.port, timeout=self.timeout,
            ssl_context=self._ssl_context, connection_factory=self._connection_factory,
        )
        sibling.connect(*self._credentials)
        return sibling

    # -- moves -----------------------------------------------------------
    def move_messages(
        self,
        plans: Sequence[MovePlan],
        mailbox: str = "INBOX",
        progress: Optional[ProgressCallback] = None,
        cancel: Optional[threading.Event] = None,
    ) -> MoveReport:
        """Copy, flag deleted, and expunge - in that order, per target folder."""
        report = MoveReport()
        plans = [p for p in plans if p.target_folder]
        if not plans:
            return report

        self.select(mailbox, readonly=False)
        conn = self._require_conn()

        by_folder: Dict[str, List[str]] = {}
        for plan in plans:
            by_folder.setdefault(plan.target_folder, []).append(plan.uid)

        total = len(plans)
        done = 0
        deleted_uids: List[str] = []
        missing_receipts: set = set()

        for folder, uids in by_folder.items():
            _check_cancel(cancel)
            for batch in _chunks(uids, COMMAND_BATCH):
                _check_cancel(cancel)
                uid_set = ",".join(batch)
                if progress:
                    progress(done, total, f"Copying {len(batch)} message(s) to “{folder}”…")

                # 1. COPY - must succeed before anything is marked for deletion.
                try:
                    response = self._cmd(
                        f"Copying to “{folder}”",
                        conn.uid, "COPY", uid_set, quote_mailbox(folder),
                    )
                except IMAPError as exc:
                    message = str(exc)
                    for uid in batch:
                        report.failed[uid] = message
                    done += len(batch)
                    if progress:
                        progress(done, total, f"Copy to “{folder}” failed.")
                    continue

                # 2. STORE +FLAGS \Deleted on the originals.
                try:
                    self._cmd(
                        "Flagging originals as deleted",
                        conn.uid, "STORE", uid_set, "+FLAGS.SILENT", r"(\Deleted)",
                    )
                except IMAPError as exc:
                    for uid in batch:
                        report.failed[uid] = (
                            f"Copied to “{folder}” but the original could not be removed: {exc}"
                        )
                    done += len(batch)
                    continue

                # Where they landed, so they can be put back. A COPY gives
                # every message a new UID in the destination, so the one the
                # message had here says nothing about where it is now.
                landed = copied_uids(response)
                for uid in batch:
                    report.moved[uid] = folder
                    if uid in landed:
                        report.new_uids[uid] = landed[uid]
                if not landed:
                    missing_receipts.add(folder)
                deleted_uids.extend(batch)
                done += len(batch)
                if progress:
                    progress(done, total, f"Filed {done} of {total} message(s)…")

        if missing_receipts:
            # Named, because "this server" is no help to somebody with more
            # than one account set up.
            report.warnings.append(
                f"{self.host} gave no new UIDs for the copies in "
                + ", ".join(f"\u201c{name}\u201d"
                            for name in sorted(missing_receipts))
                + ". Undo cannot put those messages back. Move them "
                "yourself.")

        # 3. EXPUNGE.
        if deleted_uids:
            if progress:
                progress(total, total, "Expunging originals…")
            report.expunged = self._expunge(deleted_uids, report)
        return report

    def empty_folder(self, folder: str, progress: Optional[ProgressCallback] = None,
                     cancel: Optional[threading.Event] = None) -> int:
        """Delete everything in one folder, in as few commands as possible.

        Clearing thousands of messages one at a time is the slow way: each
        one is a round trip to the server, and a mailbox with five thousand
        in it takes as long as five thousand round trips. This flags them
        a hundred UIDs at a time and expunges once, which is a handful of
        commands however many messages there are.

        Returns how many were removed. The folder itself stays.

        Nothing here can touch another folder: the UIDs come from a SEARCH
        of this one, and the STORE and EXPUNGE are addressed to those UIDs.
        """
        self.select(folder, readonly=False)
        return self.delete_uids(self._search_all(), progress=progress,
                                cancel=cancel)

    def count_folder(self, folder: str) -> int:
        """How many messages are in a folder, without fetching any of them."""
        self.select(folder, readonly=True)
        return len(self._search_all())

    # -- clearing out by criteria ----------------------------------------
    def search_criteria(self, criteria, readonly: bool = True) -> List[str]:
        """UIDs matching a :class:`cleanup.Criteria`, in one command.

        The server does the matching. That is the whole point: it already
        indexes From, Subject and Date, so this costs one round trip against
        a mailbox of any size, where fetching the headers and filtering them
        here would cost one per message and be the slow thing all over again.

        Non-ASCII is handled by asking for ``CHARSET UTF-8`` and sending the
        terms as raw bytes. imaplib encodes ``str`` arguments as ASCII and
        raises on anything else, so a search for a subject with an accent in
        it would otherwise fail before it left the machine.
        """
        conn = self._require_conn()
        self.select(criteria.folder, readonly=readonly)
        tokens = criteria.search_tokens()
        if any(not token.isascii() for token in tokens):
            args = ["CHARSET", "UTF-8"] + [t.encode("utf-8") for t in tokens]
        else:
            args = tokens
        data = self._cmd("Searching the folder", conn.uid, "SEARCH", None, *args)
        found: List[str] = []
        for chunk in data or ():
            if isinstance(chunk, bytes):
                found.extend(chunk.decode("ascii", "ignore").split())
            elif isinstance(chunk, str):
                found.extend(chunk.split())
        return found

    def count_matching(self, criteria) -> int:
        """How many messages the criteria would delete. Changes nothing.

        Its own read-only command rather than a number worked out from the
        last scan, because the number in a "delete 4,312 messages?" question
        has to be the server's answer to the same question that is about to
        be asked destructively. A stale one is how somebody agrees to four
        thousand and gets forty thousand.
        """
        return len(self.search_criteria(criteria, readonly=True))

    def delete_matching(self, criteria, progress: Optional[ProgressCallback] = None,
                        cancel: Optional[threading.Event] = None) -> int:
        """Delete everything matching the criteria. Returns how many went.

        Refuses criteria that ask for nothing. An unarmed one matches the
        whole folder, and "the text box was empty" is not a thing anybody
        should be able to mean by it - emptying a folder wholesale is
        :meth:`empty_folder`, which says so in its name and asks first.
        """
        if not criteria.is_armed:
            raise IMAPError(
                "Nothing was chosen to clear out, so nothing was deleted.")
        uids = self.search_criteria(criteria, readonly=False)
        return self.delete_uids(uids, progress=progress, cancel=cancel)

    def delete_uids(self, uids: Sequence[str],
                    progress: Optional[ProgressCallback] = None,
                    cancel: Optional[threading.Event] = None) -> int:
        """Flag and expunge a set of UIDs in the selected mailbox.

        A hundred per command. The mailbox has already been selected by
        whoever found the UIDs, and they are addressed by UID throughout, so
        this cannot reach a message in another folder however long it runs.
        """
        uids = list(uids)
        total = len(uids)
        if not total:
            return 0
        conn = self._require_conn()
        done = 0
        for batch in _chunks(uids, COMMAND_BATCH):
            if cancel is not None and cancel.is_set():
                break
            self._cmd("Flagging for deletion", conn.uid, "STORE",
                      ",".join(batch), "+FLAGS.SILENT", r"(\Deleted)")
            done += len(batch)
            if progress:
                progress(done, total, f"Marked {done} of {total}\u2026")
        if progress:
            progress(total, total, "Removing them\u2026")
        report = MoveReport()
        self._expunge(uids[:done], report)
        return done

    def _search_all(self) -> List[str]:
        conn = self._require_conn()
        data = self._cmd("Listing the folder", conn.uid, "SEARCH", None, "ALL")
        found: List[str] = []
        for chunk in data or ():
            if isinstance(chunk, bytes):
                found.extend(chunk.decode("ascii", "ignore").split())
            elif isinstance(chunk, str):
                found.extend(chunk.split())
        return found

    def _expunge(self, uids: Sequence[str], report: MoveReport) -> bool:
        conn = self._require_conn()
        if self.has_capability("UIDPLUS"):
            ok = True
            for batch in _chunks(list(uids), COMMAND_BATCH):
                try:
                    self._cmd("Expunging", conn.uid, "EXPUNGE", ",".join(batch))
                except IMAPError as exc:
                    ok = False
                    report.warnings.append(
                        f"Messages were copied and flagged as deleted, but the expunge "
                        f"failed ({exc}). They will disappear from the source mailbox "
                        f"the next time it is expunged."
                    )
            return ok
        report.warnings.append(
            "This server does not advertise UIDPLUS, so a full EXPUNGE was used. "
            "Any message you had already flagged as deleted in this mailbox was "
            "also removed."
        )
        try:
            self._cmd("Expunging", conn.expunge)
        except IMAPError as exc:
            report.warnings.append(f"Expunge failed: {exc}")
            return False
        return True

    # -- diagnostics -----------------------------------------------------
    def probe(self, email_address: str, password: str) -> Dict[str, object]:
        """Connect, look around, and disconnect. Used by “Test Connection”."""
        self.connect(email_address, password)
        try:
            folders = self.list_folders()
            count = self.select("INBOX", readonly=True)
            return {
                "host": f"{self.host}:{self.port}",
                "delimiter": self.delimiter,
                "folders": [f.name for f in folders],
                "folder_count": len(folders),
                "inbox_messages": count,
                "uidplus": self.has_capability("UIDPLUS"),
                "capabilities": list(self.capabilities),
            }
        finally:
            self.logout()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
#: The UIDPLUS receipt for a COPY: the validity, the source set, and the
#: destination set, in that order and in matching order within the sets.
_COPYUID = re.compile(r"\[COPYUID\s+\d+\s+(\S+)\s+([^\]\s]+)\]", re.I)


def _expand_uid_set(text: str) -> List[str]:
    """Turn "5:7,9" into ["5", "6", "7", "9"].

    RFC 4315 guarantees the two sets in a COPYUID line correspond position by
    position once expanded, which is the whole reason to expand them.
    """
    out: List[str] = []
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            low, _, high = part.partition(":")
            try:
                start, end = int(low), int(high)
            except ValueError:
                continue
            if start > end:
                start, end = end, start
            out.extend(str(n) for n in range(start, end + 1))
        else:
            out.append(part)
    return out


def copied_uids(data) -> Dict[str, str]:
    """Read a COPY response and say where each message landed.

    Servers that advertise UIDPLUS answer a COPY with the UIDs they assigned.
    Servers that do not answer with nothing useful, and the caller has to fall
    back to looking the message up by its Message-ID.
    """
    for line in reversed(list(data or ())):
        text = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
        found = _COPYUID.search(text)
        if not found:
            continue
        sources = _expand_uid_set(found.group(1))
        targets = _expand_uid_set(found.group(2))
        if sources and len(sources) == len(targets):
            return dict(zip(sources, targets))
    return {}


def _chunks(items: Sequence[str], size: int) -> Iterator[List[str]]:
    for index in range(0, len(items), size):
        yield list(items[index:index + size])


def _check_cancel(cancel: Optional[threading.Event]) -> None:
    if cancel is not None and cancel.is_set():
        raise ScanCancelled("Cancelled.")


def _first_bytes(data) -> bytes:
    if not data:
        return b""
    first = data[0]
    if isinstance(first, tuple):
        first = first[0]
    if isinstance(first, (bytes, bytearray)):
        return bytes(first)
    if isinstance(first, str):
        return first.encode("utf-8", "replace")
    return b""


def _first_text(data) -> str:
    return _first_bytes(data).decode("utf-8", "replace").strip()


def _error_text(exc: BaseException) -> str:
    args = getattr(exc, "args", ())
    if args:
        first = args[0]
        if isinstance(first, (bytes, bytearray)):
            return first.decode("utf-8", "replace")
        return str(first)
    return str(exc)


def attachments_of(raw: bytes) -> List["Attachment"]:
    """Parse a raw message into its attached parts, bytes included.

    Inline parts referenced by the body (a signature image, a logo) are
    reported too but marked ``inline``, so the viewer can list them
    separately rather than pretending a tracking pixel is a document.
    """
    import attachments as _attachments

    try:
        message = email.message_from_bytes(raw, policy=email.policy.default)
    except Exception:      # noqa: BLE001 - a broken message has no attachments
        return []

    found: List[_attachments.Attachment] = []
    for index, part in enumerate(message.walk(), start=1):
        if part.is_multipart():
            continue
        disposition = (part.get_content_disposition() or "").lower()
        filename = part.get_filename()
        cid = (part.get("Content-ID") or "").strip("<>")
        if disposition not in ("attachment", "inline") and not filename:
            continue
        if not filename and not cid:
            continue
        try:
            payload = part.get_payload(decode=True) or b""
        except Exception:      # noqa: BLE001 - one bad part is not the message
            payload = b""
        name = _decode_header_value(filename) if filename else (cid or f"part-{index}")
        found.append(_attachments.Attachment(
            part=str(index),
            name=name,
            content_type=(part.get_content_type() or "").lower(),
            encoding=(part.get("Content-Transfer-Encoding") or "").lower(),
            size=len(payload),
            cid=cid,
            inline=(disposition == "inline" or bool(cid and not filename)),
            data=payload or None,
        ))
    return found


# -- BODYSTRUCTURE ---------------------------------------------------------
# The server will describe a message's parts without sending them, which is
# the only way to know what is attached to a six megabyte message when the
# scan only downloads the first sixty-four kilobytes of it. The reply is a
# nested parenthesised list, so it needs a real parser rather than a regex.

def _tokenise(raw: bytes):
    """Atoms, quoted strings, literals and parentheses, in order."""
    i, n = 0, len(raw)
    while i < n:
        ch = raw[i:i + 1]
        if ch in b" \t\r\n":
            i += 1
        elif ch in b"()":
            yield ch.decode()
            i += 1
        elif ch == b'"':
            j = i + 1
            out = bytearray()
            while j < n:
                if raw[j:j + 1] == b"\\" and j + 1 < n:
                    out += raw[j + 1:j + 2]
                    j += 2
                elif raw[j:j + 1] == b'"':
                    break
                else:
                    out += raw[j:j + 1]
                    j += 1
            yield out.decode("utf-8", "replace")
            i = j + 1
        elif ch == b"{":
            close = raw.find(b"}", i)
            if close < 0:
                return
            length = int(raw[i + 1:close] or 0)
            start = close + 1
            while raw[start:start + 1] in (b"\r", b"\n"):
                start += 1
            yield raw[start:start + length].decode("utf-8", "replace")
            i = start + length
        else:
            j = i
            while j < n and raw[j:j + 1] not in b" \t\r\n()":
                j += 1
            word = raw[i:j].decode("utf-8", "replace")
            yield None if word.upper() == "NIL" else word
            i = j


def _nest(tokens):
    """Turn the token stream into nested lists."""
    stack, current = [], []
    for token in tokens:
        if token == "(":
            stack.append(current)
            current = []
        elif token == ")":
            done = current
            if not stack:
                return done
            current = stack.pop()
            current.append(done)
        else:
            current.append(token)
    return current


def _walk_structure(node, path, out) -> None:
    """Collect one entry per leaf part, numbered the way IMAP numbers them."""
    if not isinstance(node, list) or not node:
        return
    if isinstance(node[0], list):
        # multipart: children first, then the subtype
        index = 1
        for child in node:
            if isinstance(child, list):
                _walk_structure(child, path + [str(index)], out)
                index += 1
            else:
                break
        return
    # A leaf: type subtype (params) id description encoding size ...
    maintype = (node[0] or "").lower() if len(node) > 0 else ""
    subtype = (node[1] or "").lower() if len(node) > 1 else ""
    params = node[2] if len(node) > 2 and isinstance(node[2], list) else []
    content_id = node[3] if len(node) > 3 else None
    encoding = (node[5] or "") if len(node) > 5 else ""
    try:
        size = int(node[6]) if len(node) > 6 and node[6] is not None else 0
    except (TypeError, ValueError):
        size = 0

    def pairs(items):
        found = {}
        for i in range(0, len(items) - 1, 2):
            key = items[i]
            if isinstance(key, str):
                found[key.lower()] = items[i + 1]
        return found

    attrs = pairs(params)
    name = attrs.get("name") or ""
    disposition = ""
    # The disposition block sits after the body fields; find the first list
    # that looks like ("attachment" ("filename" "x")).
    for extra in node[7:]:
        if isinstance(extra, list) and extra and isinstance(extra[0], str):
            if extra[0].lower() in ("attachment", "inline"):
                disposition = extra[0].lower()
                if len(extra) > 1 and isinstance(extra[1], list):
                    name = pairs(extra[1]).get("filename") or name
                break
    out.append({
        "part": ".".join(path) if path else "1",
        "name": _decode_header_value(name) if name else "",
        "content_type": f"{maintype}/{subtype}" if maintype else "",
        "encoding": (encoding or "").lower(),
        "size": size,
        "cid": (content_id or "").strip("<>") if isinstance(content_id, str) else "",
        "disposition": disposition,
    })


def parse_bodystructure(raw: bytes) -> list:
    """Every leaf part the server described, as plain dicts."""
    try:
        start = raw.upper().find(b"BODYSTRUCTURE")
        if start >= 0:
            raw = raw[start + len(b"BODYSTRUCTURE"):]
        tree = _nest(_tokenise(raw))
        while isinstance(tree, list) and len(tree) == 1 and isinstance(tree[0], list):
            tree = tree[0]
        out: list = []
        _walk_structure(tree, [], out)
        return out
    except Exception:      # noqa: BLE001 - a description is not worth a crash
        return []


def attachment_names(parts) -> tuple:
    """The names a person would call attachments, in order, deduplicated."""
    import attachments as _attachments

    names = []
    for part in parts:
        name = part.get("name") or ""
        content_type = (part.get("content_type") or "").lower()
        disposition = part.get("disposition") or ""
        if not name:
            if disposition != "attachment":
                continue
            name = f"part-{part.get('part', '1')}"
        if content_type.startswith("multipart/"):
            continue
        # The body itself is not an attachment.
        if not disposition and content_type in ("text/plain", "text/html") and not name:
            continue
        shown = _attachments.display_name(name)
        if shown not in names:
            names.append(shown)
    return tuple(names)


def _decode_part(raw: bytes, encoding: str) -> bytes:
    """Undo the transfer encoding a part was sent in."""
    encoding = (encoding or "").strip().lower()
    if encoding == "base64":
        import base64
        # Servers wrap base64; ignore anything that is not alphabet.
        return base64.b64decode(re.sub(rb"[^A-Za-z0-9+/=]", b"", raw), validate=False)
    if encoding in ("quoted-printable", "quotedprintable"):
        import quopri
        return quopri.decodestring(raw)
    return raw
