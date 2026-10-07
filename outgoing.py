"""Mail that leaves: a message written here, and how it goes out.

A ``Draft`` is what the compose window holds. ``build`` turns it into the
bytes a server takes: text and HTML side by side, attachments after, and
for a reply the headers that keep it in its thread. ``send`` hands it to
the account's SMTP server, with the same app password IMAP uses; the
providers are known by name, the rest by convention. The copy in Sent is
the engine's business (see IMAPEngine.save_sent), since SMTP keeps none.
"""

from __future__ import annotations

import base64
import binascii
import html as _html
import re
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage as _Mime
from email.utils import formataddr, formatdate, getaddresses, make_msgid, parseaddr
from typing import List, Sequence, Tuple

import certs

#: What a sent message says it was written with. Not a version: nothing to
#: fingerprint, and it reads the same in every mail client.
MAILER = "Mail Manager"

#: The most a message may weigh, attachments and all: well under every
#: provider's limit, so the refusal is ours and says why.
MOST_BYTES = 25 * 1024 * 1024

#: How long a send may take before it is given up on.
TIMEOUT = 60.0


class SendError(RuntimeError):
    """A send that did not happen, in words for the person."""


@dataclass(frozen=True)
class SmtpHost:
    """Where an account's mail goes out, and how the connection is secured:
    straight TLS, or STARTTLS on a plain connection."""

    host: str
    port: int
    starttls: bool = False


#: The providers IMAP knows (see accounts.HOSTS), by the same names.
#: Checked against the providers' own instructions rather than guessed.
SMTP_HOSTS = {
    "icloud": SmtpHost("smtp.mail.me.com", 587, True),
    "gmail": SmtpHost("smtp.gmail.com", 465),
    "outlook": SmtpHost("smtp.office365.com", 587, True),
    "yahoo": SmtpHost("smtp.mail.yahoo.com", 465),
    "fastmail": SmtpHost("smtp.fastmail.com", 465),
    "aol": SmtpHost("smtp.aol.com", 465),
    "zoho": SmtpHost("smtp.zoho.com", 465),
    "gmx": SmtpHost("mail.gmx.com", 587, True),
    "proton": SmtpHost("127.0.0.1", 1025, True),
}

#: Providers that file a copy of what was sent themselves, so a second
#: copy from here would show twice.
KEEPS_SENT = frozenset({"gmail"})


def smtp_for(account) -> SmtpHost:
    """The account's outgoing server: as set on the account, else the
    provider's, else the usual name beside its IMAP host."""
    host = (getattr(account, "smtp_host", "") or "").strip()
    port = int(getattr(account, "smtp_port", 0) or 0)
    if host:
        return SmtpHost(host, port or 587, port != 465)
    known = SMTP_HOSTS.get(getattr(account, "preset", "") or "")
    if known is not None:
        return known
    imap = (getattr(account, "host", "") or "").strip()
    guessed = re.sub(r"^(imap|mail)\.", "smtp.", imap, flags=re.I) if imap else ""
    return SmtpHost(guessed or imap, port or 587, True)


@dataclass
class Attachment:
    name: str
    mime: str
    data: bytes


@dataclass
class Draft:
    """A message being written: who it is from and to, what it says, and
    what it is attached to in a thread."""

    from_address: str
    from_name: str = ""
    to: List[str] = field(default_factory=list)
    cc: List[str] = field(default_factory=list)
    bcc: List[str] = field(default_factory=list)
    subject: str = ""
    text: str = ""
    html: str = ""
    attachments: List[Attachment] = field(default_factory=list)
    in_reply_to: str = ""
    references: str = ""

    @property
    def recipients(self) -> List[str]:
        """Every address the message goes to, once each, in order."""
        seen: List[str] = []
        for address in [*self.to, *self.cc, *self.bcc]:
            bare = parseaddr(address)[1].lower()
            if bare and bare not in [parseaddr(s)[1].lower() for s in seen]:
                seen.append(address)
        return seen

    def problems(self) -> List[str]:
        """What stops this going, in words."""
        out = []
        if not self.recipients:
            out.append("Nobody is in To, Cc or Bcc.")
        for address in self.recipients:
            if not valid_address(parseaddr(address)[1]):
                out.append(f"{address!r} is not an address.")
        if not self.subject.strip() and not (self.text.strip() or self.html.strip()):
            out.append("There is no subject and nothing written.")
        weight = sum(len(a.data) for a in self.attachments)
        if weight > MOST_BYTES:
            out.append(f"The attachments weigh {weight // (1024 * 1024)} MB; "
                       f"the most that can go is {MOST_BYTES // (1024 * 1024)} MB.")
        return out


_ADDRESS = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")


def valid_address(address: str) -> bool:
    return bool(_ADDRESS.match((address or "").strip()))


def format_address(name: str, address: str) -> str:
    """``Dana Reyes <dana@example.com>``, with the name quoted when it holds
    anything an address parser would trip on; the address alone without a
    name. Not formataddr, which would encode a name with an accent in it
    into something nobody can read in a To field."""
    name = (name or "").strip()
    address = (address or "").strip()
    if not name or name == address:
        return address
    if any(ch in name for ch in ',;:<>@"()[]\\'):
        name = '"' + name.replace('\\', '\\\\').replace('"', '\\"') + '"'
    return f"{name} <{address}>"


def parse_addresses(text: str) -> List[str]:
    """Addresses as typed, one or many, with or without names: kept as
    written where they are whole, each once."""
    found: List[str] = []
    for name, address in getaddresses([text or ""]):
        address = address.strip()
        if not address:
            continue
        entry = formataddr((name.strip(), address)) if name.strip() else address
        if entry not in found:
            found.append(entry)
    return found


#: A picture written into the message as data, which the editor makes of a
#: picture put in.
_INLINE_PICTURE = re.compile(
    r"""<img\b([^>]*?)\bsrc=(["'])data:(image/[a-z0-9.+-]+);base64,"""
    r"""([A-Za-z0-9+/=\s]+)\2""", re.I)


def inline_pictures(html: str) -> Tuple[str, List[Tuple[str, str, bytes]]]:
    """The HTML with every embedded picture pointed at a part of its own, and
    those parts as (content id, image subtype, bytes). Gmail and others
    show nothing for a picture written into the page as data; one sent as
    a related part is shown everywhere."""
    parts: List[Tuple[str, str, bytes]] = []

    def swap(match) -> str:
        try:
            data = base64.b64decode(match.group(4))
        except (binascii.Error, ValueError):
            return match.group(0)
        cid = make_msgid()[1:-1]
        parts.append((cid, match.group(3).split("/", 1)[1].lower(), data))
        quote = match.group(2)
        return f"<img{match.group(1)}src={quote}cid:{cid}{quote}"

    return _INLINE_PICTURE.sub(swap, html or ""), parts


def build(draft: Draft) -> bytes:
    """The message as bytes, the way the server and the Sent folder get it."""
    mime = _Mime()
    mime["From"] = formataddr((draft.from_name.strip(), draft.from_address))
    if draft.to:
        mime["To"] = ", ".join(draft.to)
    if draft.cc:
        mime["Cc"] = ", ".join(draft.cc)
    mime["Subject"] = draft.subject.strip()
    mime["Date"] = formatdate(localtime=True)
    domain = draft.from_address.rsplit("@", 1)[-1] or None
    mime["Message-ID"] = make_msgid(domain=domain)
    if draft.in_reply_to:
        mime["In-Reply-To"] = draft.in_reply_to
        mime["References"] = " ".join(
            part for part in (draft.references.strip(), draft.in_reply_to)
            if part).strip()
    mime["X-Mailer"] = MAILER
    text = draft.text if draft.text.strip() else html_to_plain(draft.html)
    mime.set_content(text + ("\n" if not text.endswith("\n") else ""))
    if draft.html.strip():
        html, pictures = inline_pictures(draft.html)
        mime.add_alternative(html, subtype="html")
        if pictures:
            # The pictures ride with the HTML half, related to it, so a
            # reader shows them in place rather than as attachments.
            page = mime.get_payload()[-1]
            for cid, subtype, data in pictures:
                page.add_related(data, maintype="image", subtype=subtype,
                                 cid=f"<{cid}>")
    for item in draft.attachments:
        main, _, sub = (item.mime or "application/octet-stream").partition("/")
        mime.add_attachment(item.data, maintype=main or "application",
                            subtype=sub or "octet-stream",
                            filename=item.name or "attachment")
    return mime.as_bytes()


def html_to_plain(html: str) -> str:
    """A plain copy of what was written, for the half of the message a
    text-only reader sees."""
    import html_utils

    return html_utils.html_to_text(html or "").text if html else ""


# -- Replies and forwards --------------------------------------------------

def reply_subject(subject: str, forward: bool = False) -> str:
    cleaned = re.sub(r"^\s*((re|fwd?|aw|wg)\s*:\s*)+", "", subject or "",
                     flags=re.I).strip()
    return ("Fwd: " if forward else "Re: ") + cleaned


def thread_headers(message) -> Tuple[str, str]:
    """``In-Reply-To`` and ``References`` for a reply to ``message``."""
    message_id = (getattr(message, "message_id", "") or "").strip()
    references = (getattr(message, "references", "") or "").strip()
    if not message_id:
        return "", references
    return message_id, references


def _bare(address: str) -> str:
    return parseaddr(address or "")[1].lower()


def reply_addresses(message, own: Sequence[str], everyone: bool = False
                    ) -> Tuple[List[str], List[str]]:
    """Who a reply goes to: the sender, or whoever asked for replies; with
    ``everyone``, the other people on the message too, less ourselves."""
    ours = {_bare(a) for a in own if a}
    first = (getattr(message, "reply_to", "") or "").strip() or formataddr(
        ((getattr(message, "sender_name", "") or "").strip(),
         getattr(message, "sender_email", "") or ""))
    to = [entry for entry in parse_addresses(first) if _bare(entry) not in ours]
    if not everyone:
        return to or parse_addresses(first), []
    cc = []
    for header in ("to", "cc"):
        for entry in parse_addresses(getattr(message, header, "") or ""):
            bare = _bare(entry)
            if bare in ours or bare in {_bare(t) for t in to}:
                continue
            if bare not in {_bare(c) for c in cc}:
                cc.append(entry)
    return to or parse_addresses(first), cc


def _when(message) -> str:
    date = getattr(message, "date", None)
    if date is None:
        return ""
    try:
        local = date.astimezone()
    except (ValueError, OverflowError):
        local = date
    return local.strftime("%a, %d %b %Y at %I:%M %p").replace(" 0", " ")


def _who(message) -> str:
    name = (getattr(message, "sender_name", "") or "").strip()
    address = (getattr(message, "sender_email", "") or "").strip()
    return f"{name} <{address}>" if name and address else name or address


def quoted_text(message) -> str:
    """The message quoted under a reply, the way every client does it."""
    body = (getattr(message, "body_text", "") or "").rstrip()
    lines = [f"> {line}" if line else ">" for line in body.splitlines()]
    return f"On {_when(message)}, {_who(message)} wrote:\n" + "\n".join(lines)


def quoted_html(message) -> str:
    """The same, as HTML: the original as sent where there is one."""
    import html_utils

    original = getattr(message, "body_html", "") or ""
    inner = (html_utils.sanitise_for_view(original) if original.strip()
             else "<p>" + _html.escape(getattr(message, "body_text", "") or "")
             .replace("\n", "<br>") + "</p>")
    return (f"<p>On {_html.escape(_when(message))}, "
            f"{_html.escape(_who(message))} wrote:</p>"
            f"<blockquote style=\"margin:0 0 0 .8ex;border-left:2px solid #888;"
            f"padding-left:1ex\">{inner}</blockquote>")


def forward_text(message) -> str:
    head = "\n".join(line for line in (
        "---------- Forwarded message ----------",
        f"From: {_who(message)}",
        f"Date: {_when(message)}",
        f"Subject: {getattr(message, 'subject', '') or ''}",
        f"To: {getattr(message, 'to', '') or ''}") if line)
    return head + "\n\n" + (getattr(message, "body_text", "") or "").rstrip()


def forward_html(message) -> str:
    import html_utils

    original = getattr(message, "body_html", "") or ""
    inner = (html_utils.sanitise_for_view(original) if original.strip()
             else "<p>" + _html.escape(getattr(message, "body_text", "") or "")
             .replace("\n", "<br>") + "</p>")
    rows = "".join(
        f"<div><b>{label}:</b> {_html.escape(value)}</div>"
        for label, value in (("From", _who(message)), ("Date", _when(message)),
                             ("Subject", getattr(message, "subject", "") or ""),
                             ("To", getattr(message, "to", "") or ""))
        if value)
    return ("<p>---------- Forwarded message ----------</p>" + rows
            + "<br>" + inner)


# -- Sending ---------------------------------------------------------------

def send(raw: bytes, host: SmtpHost, address: str, password: str,
         sender: str, recipients: Sequence[str], factory=None) -> None:
    """Hand ``raw`` to the server for ``recipients``. ``factory`` makes the
    connection, for tests; otherwise smtplib does, over TLS either way."""
    if not host.host:
        raise SendError("This mailbox has no outgoing server. Add one in "
                        "Settings, under the mailbox.")
    context = ssl.create_default_context(cafile=certs.ensure())
    try:
        if factory is not None:
            client = factory(host)
        elif host.starttls:
            client = smtplib.SMTP(host.host, host.port, timeout=TIMEOUT)
        else:
            client = smtplib.SMTP_SSL(host.host, host.port, timeout=TIMEOUT,
                                      context=context)
        with client:
            client.ehlo()
            if host.starttls and factory is None:
                client.starttls(context=context)
                client.ehlo()
            try:
                client.login(address, password)
            except smtplib.SMTPAuthenticationError as exc:
                raise SendError(
                    "The server refused the password. Outgoing mail uses "
                    "the same app password as reading it; check it in "
                    "Settings.") from exc
            refused = client.sendmail(_bare(sender) or sender,
                                      [_bare(r) or r for r in recipients], raw)
            if refused:
                names = ", ".join(refused)
                raise SendError(f"The server would not take it for {names}.")
    except SendError:
        raise
    except smtplib.SMTPRecipientsRefused as exc:
        raise SendError("The server refused every recipient.") from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise SendError(f"Sending failed: {exc}") from exc
