"""Mailbox accounts and the servers they live on.

The app began as an iCloud tool, so the mailbox used to be three fields on the
settings object. This turns it into a list: one entry per mailbox, each with
its own server, credentials and colour, so mail from several accounts can be
triaged together or one at a time.

Nothing here talks to a network. IMAPEngine takes a host and a port and does
not care where they came from; this module only decides what those should be.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Dict, Mapping, Optional, Tuple

#: Every provider below still advertises AUTH=PLAIN, so an app-specific
#: password is enough and none of this needs OAuth. Checked against the live
#: servers rather than taken from documentation.
DEFAULT_PORT = 993


@dataclass(frozen=True)
class MailHost:
    """A known provider: where to connect and what to tell the user to paste."""

    name: str
    label: str
    host: str
    port: int = DEFAULT_PORT
    domains: Tuple[str, ...] = ()
    secret_label: str = "App password"
    help_url: str = ""
    note: str = ""

    @property
    def is_custom(self) -> bool:
        return self.name == "custom"


HOSTS: Tuple[MailHost, ...] = (
    MailHost(
        name="icloud", label="iCloud", host="imap.mail.me.com",
        domains=("icloud.com", "me.com", "mac.com"),
        secret_label="App-specific password",
        help_url="https://account.apple.com/account/manage",
        note="Apple rejects your ordinary password over IMAP once two-factor "
             "is on. Generate an app-specific password instead.",
    ),
    MailHost(
        name="gmail", label="Gmail", host="imap.gmail.com",
        domains=("gmail.com", "googlemail.com"),
        secret_label="App password",
        help_url="https://myaccount.google.com/apppasswords",
        note="Needs 2-Step Verification switched on first; app passwords are "
             "only offered to accounts that have it. IMAP also has to be "
             "enabled in Gmail's own settings, under Forwarding and POP/IMAP.",
    ),
    MailHost(
        name="outlook", label="Outlook / Microsoft 365",
        host="outlook.office365.com",
        domains=("outlook.com", "hotmail.com", "live.com", "msn.com"),
        secret_label="App password",
        help_url="https://account.microsoft.com/security",
        note="Work and school accounts are often barred from password sign-in "
             "by their administrator, whatever the server advertises. If sign-in "
             "fails with the right password, that is usually why.",
    ),
    MailHost(
        name="yahoo", label="Yahoo Mail", host="imap.mail.yahoo.com",
        domains=("yahoo.com", "yahoo.co.uk", "ymail.com", "rocketmail.com"),
        secret_label="App password",
        help_url="https://login.yahoo.com/account/security",
    ),
    MailHost(
        name="fastmail", label="Fastmail", host="imap.fastmail.com",
        domains=("fastmail.com", "fastmail.fm"),
        secret_label="App password",
        help_url="https://app.fastmail.com/settings/security/apppasswords",
    ),
    MailHost(
        name="aol", label="AOL Mail", host="imap.aol.com",
        domains=("aol.com",),
        secret_label="App password",
        help_url="https://login.aol.com/account/security",
    ),
    MailHost(
        name="zoho", label="Zoho Mail", host="imap.zoho.com",
        domains=("zoho.com", "zohomail.com"),
        secret_label="App password",
        help_url="https://accounts.zoho.com/home#security",
    ),
    MailHost(
        name="gmx", label="GMX", host="imap.gmx.com",
        domains=("gmx.com", "gmx.net", "gmx.de", "gmx.co.uk"),
        secret_label="Password",
        note="IMAP has to be switched on in GMX's web settings before it works.",
    ),
    MailHost(
        name="proton", label="Proton Mail (Bridge)", host="127.0.0.1", port=1143,
        domains=("proton.me", "protonmail.com", "protonmail.ch", "pm.me"),
        secret_label="Bridge password",
        help_url="https://proton.me/mail/bridge",
        note="Proton does not expose IMAP directly. Its Bridge app runs on your "
             "Mac and serves a local IMAP port; the password comes from Bridge, "
             "not from your Proton account.",
    ),
    MailHost(
        name="custom", label="Other (IMAP)", host="", port=DEFAULT_PORT,
        secret_label="Password",
        note="Any IMAP server. Your provider publishes the host name, usually "
             "something like imap.example.com on port 993.",
    ),
)

_BY_NAME: Dict[str, MailHost] = {entry.name: entry for entry in HOSTS}
_BY_DOMAIN: Dict[str, MailHost] = {
    domain: entry for entry in HOSTS for domain in entry.domains
}

#: Offered in rotation to new accounts, so several mailboxes stay tellable
#: apart at a glance in the table.
ACCOUNT_COLORS: Tuple[str, ...] = (
    "#3B82F6",   # blue
    "#10B981",   # green
    "#F59E0B",   # amber
    "#8B5CF6",   # violet
    "#EC4899",   # pink
    "#14B8A6",   # teal
    "#F43F5E",   # rose
    "#6366F1",   # indigo
)

_ADDRESS = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def host_for(name: str) -> MailHost:
    """The preset called `name`, falling back to the custom one."""
    return _BY_NAME.get((name or "").strip().lower(), _BY_NAME["custom"])


def host_for_address(address: str) -> MailHost:
    """Guess the provider from the domain, so setup is usually one field."""
    _, _, domain = (address or "").strip().lower().partition("@")
    return _BY_DOMAIN.get(domain, _BY_NAME["custom"])


def host_for_server(server: str) -> MailHost:
    """The preset matching an IMAP host name, for error messages."""
    server = (server or "").strip().lower()
    for entry in HOSTS:
        if entry.host and entry.host.lower() == server:
            return entry
    return _BY_NAME["custom"]


def credential_hint(server: str) -> str:
    """What to tell someone whose password was rejected by `server`."""
    spec = host_for_server(server)
    if spec.is_custom:
        return ("Check the address, and whether this provider needs an app "
                "password rather than your ordinary one.")
    where = f" Generate one at {spec.help_url}." if spec.help_url else ""
    return f"{spec.label} needs a{'n' if spec.secret_label[0].lower() in 'aeiou' else ''} " \
           f"{spec.secret_label.lower()} rather than your ordinary password.{where}"


def choices() -> Tuple[Tuple[str, str], ...]:
    """(name, label) pairs for a menu, in the order defined above."""
    return tuple((entry.name, entry.label) for entry in HOSTS)


def valid_address(address: str) -> bool:
    return bool(_ADDRESS.match((address or "").strip()))


def new_id() -> str:
    """A handle for an account that has no address to derive one from."""
    return uuid.uuid4().hex[:12]


def stable_id(address: str) -> str:
    """A handle derived from the address, so it survives being rebuilt.

    Settings are normalised on every load and save. With a random handle, an
    account migrated from the old single-mailbox fields would be given a new
    one each time, and anything remembering a choice of mailbox would lose it
    on restart.
    """
    digest = hashlib.sha256((address or "").strip().lower().encode("utf-8"))
    return digest.hexdigest()[:12]


@dataclass
class Account:
    """One mailbox: where it is, what to call it, and whether to scan it."""

    id: str = ""
    label: str = ""
    address: str = ""
    preset: str = "custom"
    host: str = ""
    port: int = DEFAULT_PORT
    source_mailbox: str = "INBOX"
    connections: int = 4
    enabled: bool = True
    color: str = ""

    def __post_init__(self) -> None:
        self.address = (self.address or "").strip()
        self.id = (
            (self.id or "").strip()
            or (stable_id(self.address) if self.address else new_id())
        )
        self.preset = (self.preset or "custom").strip().lower()
        if self.preset not in _BY_NAME:
            self.preset = "custom"
        spec = _BY_NAME[self.preset]
        self.host = (self.host or "").strip() or spec.host
        try:
            self.port = int(self.port)
        except (TypeError, ValueError):
            self.port = spec.port
        if not 1 <= self.port <= 65535:
            self.port = spec.port
        self.source_mailbox = (self.source_mailbox or "").strip() or "INBOX"
        try:
            self.connections = max(1, min(8, int(self.connections)))
        except (TypeError, ValueError):
            self.connections = 4
        self.label = (self.label or "").strip() or self.default_label()
        self.color = (self.color or "").strip()

    def default_label(self) -> str:
        """What to call an account nobody has named: the local part, usually."""
        local, _, _ = self.address.partition("@")
        return local or host_for(self.preset).label

    @property
    def spec(self) -> MailHost:
        return host_for(self.preset)

    @property
    def is_configured(self) -> bool:
        return bool(self.address and self.host)

    @property
    def short(self) -> str:
        """A compact name for the table column."""
        return self.label or self.address or "(unnamed)"

    def describe(self) -> str:
        return f"{self.label} · {self.address}" if self.label != self.address else self.address

    @classmethod
    def for_address(cls, address: str, **overrides: Any) -> "Account":
        """Build an account from an address alone, guessing the provider."""
        spec = host_for_address(address)
        payload: Dict[str, Any] = {
            "address": address,
            "preset": spec.name,
            "host": spec.host,
            "port": spec.port,
        }
        payload.update(overrides)
        return cls(**payload)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Account":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})


def assign_colors(accounts) -> None:
    """Give every account a tint, keeping the ones already chosen."""
    taken = {a.color for a in accounts if a.color}
    spare = [c for c in ACCOUNT_COLORS if c not in taken]
    for account in accounts:
        if not account.color:
            account.color = spare.pop(0) if spare else ACCOUNT_COLORS[0]


def unique_labels(accounts) -> None:
    """Disambiguate accounts that would otherwise show the same name."""
    seen: Dict[str, int] = {}
    for account in accounts:
        base = account.label
        count = seen.get(base, 0)
        seen[base] = count + 1
        if count:
            _, _, domain = account.address.partition("@")
            account.label = f"{base} ({domain})" if domain else f"{base} {count + 1}"
