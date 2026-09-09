"""Settings persistence and Keychain-backed credential storage.

Secrets (the iCloud app-specific password and the Anthropic API key) never
touch disk in plaintext - they live in the macOS Keychain via ``keyring``.
Everything else lives in a JSON file under ``Application Support``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import accounts as accounts_mod
import profiles
import providers
from accounts import Account
from models import (
    APP_NAME,
    FolderPlan,
    DEFAULT_CONFIDENCE_THRESHOLD,
    DEFAULT_FOLDER_ROOT,
    DEFAULT_OTHER_ROOT,
    NonJobRouting,
    TimeWindow,
)

log = logging.getLogger(__name__)

#: Keychain service name. Visible in Keychain Access under this label.
#: Unchanged from the first release so existing Keychain entries keep working.
KEYCHAIN_SERVICE = "iCloud Job Triage"
ANTHROPIC_ACCOUNT = "anthropic-api-key"

DEFAULT_IMAP_HOST = "imap.mail.me.com"
DEFAULT_IMAP_PORT = 993
DEFAULT_PROVIDER = providers.DEFAULT_PROVIDER
DEFAULT_MODEL = providers.default_model_for(DEFAULT_PROVIDER)

#: Environment variables consulted when no key is stored in the Keychain.
PROVIDER_ENV_KEYS = {
    "anthropic": ("ANTHROPIC_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "openai": ("OPENAI_API_KEY",),
    "ollama": (),
    "rules": (),
}

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


class CredentialError(RuntimeError):
    """Raised when the system keychain cannot be read or written."""


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
def app_support_dir() -> Path:
    """Per-user application data directory."""
    override = os.environ.get("ICLOUD_TRIAGE_HOME")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / APP_NAME


#: The directory used before the app was renamed to Mail Manager. Settings
#: written under the old name are moved across on first load, the same way the
#: Keychain service name was left alone so stored credentials keep working.
LEGACY_APP_NAME = "iCloud Job Triage"


def legacy_app_support_dir() -> Optional[Path]:
    """Where settings lived under the previous name, if that path is different."""
    if os.environ.get("ICLOUD_TRIAGE_HOME"):
        return None
    current = app_support_dir()
    legacy = current.parent / LEGACY_APP_NAME
    return None if legacy == current else legacy


def settings_path() -> Path:
    return app_support_dir() / "settings.json"


def migrate_legacy_support_files() -> list:
    """Carry pre-rename data across. Returns the paths that were brought over.

    The originals are left where they are, so downgrading to an older build
    still finds its own configuration.
    """
    legacy_dir = legacy_app_support_dir()
    if legacy_dir is None or not legacy_dir.is_dir():
        return []
    current_dir = app_support_dir()
    moved = []
    try:
        candidates = sorted(legacy_dir.iterdir())
    except OSError as exc:
        log.warning("Could not read %s (%s).", legacy_dir, exc)
        return []
    for source in candidates:
        if not source.is_file() or source.name.startswith("."):
            continue
        target = current_dir / source.name
        if target.exists():
            continue
        try:
            current_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            os.chmod(target, 0o600)
        except OSError as exc:
            log.warning("Could not carry %s over (%s).", source.name, exc)
            continue
        moved.append(target)
    if moved:
        log.info("Carried %d file(s) over from the previous application name.", len(moved))
    return moved


def log_dir() -> Path:
    override = os.environ.get("ICLOUD_TRIAGE_HOME")
    if override:
        return Path(override).expanduser() / "Logs"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / APP_NAME
    return app_support_dir() / "logs"


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
@dataclass
class Settings:
    """Non-secret, user-visible configuration."""

    #: Every mailbox the app knows about. The five fields below it describe
    #: the first one and are kept in step with it, because the app shipped
    #: with a single mailbox and plenty of code still reads it that way.
    mailboxes: List[Account] = field(default_factory=list)
    #: Which mailboxes the next scan reads. Empty means all of them.
    active_accounts: List[str] = field(default_factory=list)

    icloud_email: str = ""
    imap_host: str = DEFAULT_IMAP_HOST
    imap_port: int = DEFAULT_IMAP_PORT
    source_mailbox: str = "INBOX"
    imap_connections: int = 4
    fetch_bytes: int = 65536

    provider: str = DEFAULT_PROVIDER
    model: str = ""              # empty means "this provider's default"
    base_url: str = ""           # override the endpoint (LM Studio, OpenRouter, …)
    fallback_to_rules: bool = True
    ruleset: str = "general"
    effort: str = "medium"
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD
    max_body_chars: int = 4000
    concurrency: int = 4
    batch_size: int = 6

    #: What the app is being used for, which decides the folder layout.
    sort_profile: str = profiles.DEFAULT_PROFILE
    #: Topics the user has picked, overriding the profile's own list. Empty
    #: means "whatever the profile says", which is the usual case.
    topics: List[str] = field(default_factory=list)

    folder_root: str = DEFAULT_FOLDER_ROOT
    other_folder_root: str = DEFAULT_OTHER_ROOT
    non_job_routing: str = NonJobRouting.LEAVE.value
    auto_approve_non_job: bool = False
    subscribe_new_folders: bool = True
    #: Whether a scan should file mail the way it was corrected last time.
    #: On by default: a sorter that keeps making the same mistake after being
    #: told is the single most annoying thing one can do.
    learn_from_corrections: bool = True
    #: Whether verdicts from earlier scans may be reused when the message and
    #: the settings behind them have not changed. On by default: re-reading
    #: the same six days every morning is the common case, not the exception.
    reuse_verdicts: bool = True

    last_window: str = TimeWindow.LAST_24_HOURS.name
    custom_start: str = ""
    custom_end: str = ""
    max_messages: int = 400

    window_geometry: str = ""
    splitter_state: str = ""
    table_state: str = ""
    show_log_panel: bool = False
    row_lines: int = 3
    #: Columns the user has hidden, by index. The tick box is column 0 and is
    #: never hideable, so it is never in here.
    hidden_columns: List[int] = field(default_factory=list)

    # Appearance. Three separate axes: which way round the colours go, how far
    # apart the ends are, and whether the layout is tuned for reading.
    appearance_mode: str = "system"      # system | light | dark
    contrast: str = "normal"             # normal | high | maximum
    readable: bool = False
    #: How much room the main window gives things. Separate from readable,
    #: which is about type rather than space, so the two compose.
    density: str = "comfortable"
    #: False once the row height has been set by hand. Until then it follows
    #: the spacing, which is what choosing "compact" is asking for.
    row_lines_auto: bool = True
    #: Hovering anything explains it. Off by default; a tooltip nobody asked
    #: for is noise, and this makes asking explicit.
    help_mode: bool = False

    # Auto reply. Nothing is ever sent; drafts are saved for review.
    auto_reply: bool = False
    reply_rules: List[dict] = field(default_factory=list)
    #: The name signed at the bottom of a drafted reply.
    reply_signature: str = ""

    # Unattended scanning
    schedule_minutes: int = 0            # 0 means off
    background_window_minutes: int = 180
    auto_file_background: bool = False
    background_agent: bool = False       # keep running when the app is closed
    menu_bar_icon: bool = True
    #: Closing the window leaves the menu bar item running instead of quitting.
    close_to_menu_bar: bool = True
    #: Launch straight to the menu bar, with no window.
    start_in_menu_bar: bool = False
    hide_non_job: bool = False

    def __post_init__(self) -> None:
        # An empty model means "whatever this backend's default is". Resolving
        # it here means every Settings instance is usable, not just the ones
        # that happen to have been through normalized().
        if not str(self.model).strip():
            self.model = providers.default_model_for(self.provider)

    # -- validation ------------------------------------------------------
    def normalized(self) -> "Settings":
        """Return a copy with every field clamped into a usable range."""
        data = asdict(self)
        data["icloud_email"] = str(data["icloud_email"]).strip()
        data["mailboxes"] = [
            m if isinstance(m, Account) else Account.from_dict(m)
            for m in (data.get("mailboxes") or [])
            if isinstance(m, (Account, Mapping))
        ]
        chosen = data.get("active_accounts") or data.get("active_account") or []
        if isinstance(chosen, str):                 # an older single-mailbox choice
            chosen = [chosen] if chosen.strip() else []
        data["active_accounts"] = [str(a).strip() for a in chosen if str(a).strip()]
        if not profiles.exists(str(data.get("sort_profile", ""))):
            data["sort_profile"] = profiles.DEFAULT_PROFILE
        known = {t.value for t in profiles.ALL_TOPICS}
        data["topics"] = [str(t) for t in (data.get("topics") or []) if str(t) in known]
        data["imap_host"] = str(data["imap_host"]).strip() or DEFAULT_IMAP_HOST
        data["imap_port"] = _valid_int(data["imap_port"], 1, 65535, DEFAULT_IMAP_PORT)
        data["source_mailbox"] = str(data["source_mailbox"]).strip() or "INBOX"
        data["imap_connections"] = _clamp_int(data["imap_connections"], 1, 8, 4)
        data["fetch_bytes"] = _clamp_int(data["fetch_bytes"], 8192, 5_000_000, 65536)
        provider = str(data["provider"]).strip().lower()
        data["provider"] = provider if provider in providers.PROVIDERS_BY_NAME else DEFAULT_PROVIDER
        data["model"] = (
            str(data["model"]).strip() or providers.default_model_for(data["provider"])
        )
        data["base_url"] = str(data["base_url"]).strip()
        import rulesets as _rulesets
        data["ruleset"] = _rulesets.get(data["ruleset"]).name
        data["effort"] = data["effort"] if data["effort"] in EFFORT_LEVELS else "medium"
        data["confidence_threshold"] = _clamp_float(data["confidence_threshold"], 0.5, 1.0, DEFAULT_CONFIDENCE_THRESHOLD)
        data["max_body_chars"] = _clamp_int(data["max_body_chars"], 500, 200000, 4000)
        data["concurrency"] = _clamp_int(data["concurrency"], 1, 16, 4)
        data["batch_size"] = _clamp_int(data["batch_size"], 1, 25, 6)
        data["folder_root"] = str(data["folder_root"]).strip() or DEFAULT_FOLDER_ROOT
        data["other_folder_root"] = str(data["other_folder_root"]).strip() or DEFAULT_OTHER_ROOT
        data["non_job_routing"] = NonJobRouting.parse(data["non_job_routing"]).value
        # A profile whose whole point is sorting the rest of the inbox cannot
        # also be set to leave the rest of the inbox alone; that combination
        # would file nothing at all and look like a broken app.
        if (profiles.get(data["sort_profile"]).sorts_everything
                and NonJobRouting.parse(data["non_job_routing"]) is NonJobRouting.LEAVE):
            data["non_job_routing"] = NonJobRouting.FILE.value
        data["max_messages"] = _clamp_int(data["max_messages"], 1, 5000, 400)
        data["row_lines"] = _clamp_int(data["row_lines"], 1, 6, 3)
        import autoreply as _autoreply
        raw_rules = data.get("reply_rules") or []
        # Read every rule and write it back out, so a file written before
        # rules had condition and action lists is upgraded once, here, rather
        # than being converted again on every read.
        data["reply_rules"] = [
            _autoreply.Rule.from_dict(r).to_dict()
            for r in raw_rules if isinstance(r, (dict, Mapping))
        ]
        data["reply_signature"] = str(data.get("reply_signature", "")).strip()
        data["hidden_columns"] = sorted({
            int(c) for c in (data.get("hidden_columns") or [])
            if isinstance(c, (int, float)) and 1 <= int(c) <= 32
        })
        import theme as _theme
        if data["appearance_mode"] not in dict(_theme.MODES):
            data["appearance_mode"] = "system"
        if data["contrast"] not in dict(_theme.CONTRASTS):
            data["contrast"] = "normal"
        if data["density"] not in {n for n, _l, _b in _theme.DENSITIES}:
            data["density"] = "comfortable"
        data["schedule_minutes"] = _clamp_int(data["schedule_minutes"], 0, 10080, 0)
        data["background_window_minutes"] = _clamp_int(
            data["background_window_minutes"], 15, 20160, 180
        )
        if data["last_window"] not in {w.name for w in TimeWindow}:
            data["last_window"] = TimeWindow.LAST_24_HOURS.name
        for key in ("auto_approve_non_job", "subscribe_new_folders", "show_log_panel",
                    "hide_non_job", "fallback_to_rules", "auto_file_background",
                    "background_agent", "menu_bar_icon", "close_to_menu_bar",
                    "start_in_menu_bar", "readable", "help_mode", "auto_reply",
                    "row_lines_auto", "learn_from_corrections",
                    "reuse_verdicts"):
            data[key] = bool(data[key])
        settled = Settings(**data)
        settled._sync_mailboxes()
        return settled

    @property
    def window(self) -> TimeWindow:
        return TimeWindow.from_name(self.last_window)

    @property
    def routing(self) -> NonJobRouting:
        return NonJobRouting.parse(self.non_job_routing)

    @property
    def provider_class(self) -> type:
        return providers.provider_class(self.provider)

    @property
    def provider_label(self) -> str:
        return self.provider_class.label

    @property
    def needs_api_key(self) -> bool:
        return bool(self.provider_class.needs_api_key)

    @property
    def on_device(self) -> bool:
        return bool(self.provider_class.on_device)

    def is_configured(self) -> bool:
        return any(a.is_configured for a in self.mailboxes) or bool(self.icloud_email)

    # -- mailboxes -------------------------------------------------------
    def _sync_mailboxes(self) -> None:
        """Reconcile the mailbox list with the original single-mailbox fields.

        The app shipped with one mailbox described by five flat fields, and a
        good deal of code still reads them. Rather than rewrite all of it, the
        first mailbox and those fields are kept as two views of one thing: an
        older settings file grows a mailbox, and a newer one keeps the flat
        fields pointing at whichever mailbox comes first.
        """
        if not self.mailboxes and self.icloud_email:
            self.mailboxes = [Account.for_address(
                self.icloud_email,
                host=self.imap_host,
                port=self.imap_port,
                source_mailbox=self.source_mailbox,
                connections=self.imap_connections,
            )]
        if self.mailboxes:
            first = self.mailboxes[0]
            self.icloud_email = first.address
            self.imap_host = first.host
            self.imap_port = first.port
            self.source_mailbox = first.source_mailbox
            self.imap_connections = first.connections
        accounts_mod.assign_colors(self.mailboxes)
        accounts_mod.unique_labels(self.mailboxes)
        known = {a.id for a in self.mailboxes}
        self.active_accounts = [a for a in self.active_accounts if a in known]

    @property
    def accounts(self) -> List[Account]:
        """Every mailbox, configured or not."""
        return list(self.mailboxes)

    @property
    def enabled_accounts(self) -> List[Account]:
        return [a for a in self.mailboxes if a.enabled and a.is_configured]

    @property
    def scan_accounts(self) -> List[Account]:
        """The mailboxes the next scan will read: the selected one, or all."""
        chosen = [a for a in self.enabled_accounts
                  if not self.active_accounts or a.id in self.active_accounts]
        return chosen or self.enabled_accounts or [self.primary_account]

    @property
    def primary_account(self) -> Account:
        """The first mailbox, invented from the flat fields if there is none.

        Settings built in code rather than loaded from disk may have no
        mailbox list at all. Rather than make every caller handle that, this
        always returns something connectable.
        """
        if self.mailboxes:
            return self.mailboxes[0]
        return Account.for_address(
            self.icloud_email,
            host=self.imap_host,
            port=self.imap_port,
            source_mailbox=self.source_mailbox,
            connections=self.imap_connections,
        )

    @property
    def scans_every_mailbox(self) -> bool:
        return not self.active_accounts

    def account_by_id(self, account_id: str) -> Optional[Account]:
        return next((a for a in self.mailboxes if a.id == account_id), None)

    @property
    def multi_account(self) -> bool:
        return len(self.enabled_accounts) > 1

    # -- profile ---------------------------------------------------------
    @property
    def effective_row_lines(self) -> int:
        """Lines per table row: the spacing's, unless one was chosen."""
        if not self.row_lines_auto:
            return self.row_lines
        import theme
        return theme.density(self.density).row_lines

    @property
    def profile(self) -> "profiles.Profile":
        return profiles.get(self.sort_profile)

    @property
    def rules(self) -> list:
        """The reply rules as objects, defaulting to the shipped set."""
        import autoreply
        if not self.reply_rules:
            return autoreply.default_rules()
        return [autoreply.Rule.from_dict(r) for r in self.reply_rules]

    def set_rules(self, rules) -> None:
        self.reply_rules = [r.to_dict() for r in rules]

    @property
    def replies_armed(self) -> bool:
        """Whether any rule would actually do something."""
        return self.auto_reply and any(
            r.enabled and r.ready for r in self.rules)

    @property
    def chosen_topics(self) -> tuple:
        """Topics that get a folder: the user's picks, else the profile's."""
        if not self.topics:
            return self.profile.topics
        picked = {t for t in self.topics}
        return tuple(t for t in profiles.ALL_TOPICS if t.value in picked)

    def folder_plan(self, delimiter: str = "/") -> FolderPlan:
        """The folder layout implied by the current profile and roots."""
        active = self.profile
        return FolderPlan(
            root=self.folder_root,
            other_root=self.other_folder_root,
            delimiter=delimiter or "/",
            detailed_job_folders=active.detailed_job_folders,
            topics=self.chosen_topics,
        )

    # -- serialisation ---------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Settings":
        known = {f.name for f in fields(cls)}
        filtered: Dict[str, Any] = {}
        for key, value in (data or {}).items():
            if key not in known:
                continue
            filtered[key] = value
        try:
            return cls(**filtered).normalized()
        except TypeError:
            log.warning("Settings file contained unusable values; falling back to defaults.")
            return cls()

    # -- sharing a configuration -----------------------------------------
    #: Never leaves the machine in an export: window geometry is meaningless
    #: elsewhere, and the mailbox list is exported without its passwords,
    #: which stay in the Keychain where they belong.
    PRIVATE_FIELDS = ("window_geometry", "splitter_state", "table_state")

    def export_text(self) -> str:
        """A readable copy of the settings, safe to send to somebody else.

        JSON with a comment header rather than an opaque blob, so it can be
        read and edited in any text editor and diffed like anything else.
        No secret is ever in here: passwords and API keys live in the Keychain
        and are not part of this object at all.
        """
        payload = {k: v for k, v in self.normalized().to_dict().items()
                   if k not in self.PRIVATE_FIELDS}
        # Mailboxes keep their addresses and servers, which is the useful part,
        # and never had passwords in them to begin with.
        header = (
            f"# {APP_NAME} settings\n"
            f"# Exported {datetime.now().astimezone():%Y-%m-%d %H:%M %Z}\n"
            "#\n"
            "# No passwords or API keys are in this file. Those live in the\n"
            "# macOS Keychain, and have to be entered again on another Mac.\n"
            "# Import it from Settings, or drop it in place of settings.json.\n"
        )
        return header + json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"

    @classmethod
    def import_text(cls, text: str) -> "Settings":
        """Read what export_text wrote. Raises ValueError with a reason."""
        stripped = "\n".join(
            line for line in (text or "").splitlines() if not line.lstrip().startswith("#")
        ).strip()
        if not stripped:
            raise ValueError("That file is empty, or contains only comments.")
        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"That is not a settings file this app wrote: {exc.msg} "
                f"(line {exc.lineno})."
            ) from exc
        if not isinstance(raw, Mapping):
            raise ValueError(
                "That file holds a "
                f"{type(raw).__name__}, not a set of settings."
            )
        known = {f.name for f in fields(cls)}
        if not known & set(raw):
            raise ValueError(
                "That file has none of the settings this app uses, so it is "
                "probably from a different program."
            )
        return cls.from_dict(raw)

    # -- disk ------------------------------------------------------------
    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Settings":
        if path is None:
            migrate_legacy_support_files()
            path = settings_path()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            log.warning("Could not read settings at %s (%s); using defaults.", path, exc)
            return cls()
        if not isinstance(raw, Mapping):
            return cls()
        return cls.from_dict(raw)

    def save(self, path: Optional[Path] = None) -> Path:
        """Atomically persist settings (temp file + rename)."""
        path = path or settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.normalized().to_dict(), indent=2, sort_keys=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".settings-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        try:
            os.chmod(path, 0o600)
        except OSError:  # pragma: no cover - best effort
            pass
        return path


def _clamp_int(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _valid_int(value: Any, low: int, high: int, default: int) -> int:
    """Like :func:`_clamp_int`, but an out-of-range value falls back.

    Clamping suits preferences (a concurrency of 500 clearly means "as many as
    you can"). A port number is an exact value: clamping 0 to 1 would silently
    produce a connection attempt nobody asked for.
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if low <= number <= high else default


def _clamp_float(value: Any, low: float, high: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def parse_iso(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------
class CredentialStore:
    """Thin, testable wrapper over ``keyring``.

    ``keyring`` is imported lazily so that the domain and IMAP tests run on
    machines (and CI images) with no Keychain available.
    """

    def __init__(self, service: str = KEYCHAIN_SERVICE, backend: Any = None,
                 read_timeout: Optional[float] = None) -> None:
        self.service = service
        self._backend = backend
        if read_timeout is not None:
            self.read_timeout = read_timeout

    # -- backend ---------------------------------------------------------
    def _keyring(self) -> Any:
        if self._backend is not None:
            return self._backend
        try:
            import keyring  # noqa: WPS433 - deliberate lazy import
        except Exception as exc:  # pragma: no cover - only when keyring is absent
            raise CredentialError(
                "The 'keyring' package is not available, so credentials cannot be "
                "stored in the macOS Keychain. Install it with: pip install keyring"
            ) from exc
        self._backend = keyring
        return keyring

    def available(self) -> bool:
        try:
            self._keyring()
        except CredentialError:
            return False
        return True

    def backend_name(self) -> str:
        try:
            keyring = self._keyring()
            backend = keyring.get_keyring()
            return f"{type(backend).__module__}.{type(backend).__name__}"
        except Exception:  # pragma: no cover
            return "unavailable"

    # -- generic ---------------------------------------------------------
    #: How long to wait for the Keychain before deciding it is not going to
    #: answer. Only the headless paths use this; a window can afford to wait
    #: because the person is there to click the button.
    read_timeout: Optional[float] = None

    def get(self, account: str) -> str:
        if not account:
            return ""
        try:
            if self.read_timeout:
                return self._get_with_timeout(account, self.read_timeout)
            return self._keyring().get_password(self.service, account) or ""
        except CredentialError:
            raise
        except Exception as exc:
            raise CredentialError(f"Could not read '{account}' from the Keychain: {exc}") from exc

    def _get_with_timeout(self, account: str, timeout: float) -> str:
        """Read the Keychain, giving up rather than waiting forever.

        macOS asks permission the first time a particular build of an app
        touches an entry, and identifies the app by its code signature. A
        rebuilt or re-signed copy is a different app as far as the Keychain is
        concerned, so it asks again. With a window on screen that is a dialog;
        run from a launchd agent with nobody watching it is a process that
        never returns, which is how a nightly scan silently stops happening.
        """
        outcome: Dict[str, Any] = {}

        def read() -> None:
            try:
                outcome["value"] = self._keyring().get_password(self.service, account) or ""
            except BaseException as exc:  # noqa: BLE001 - handed back below
                outcome["error"] = exc

        import threading
        worker = threading.Thread(target=read, daemon=True, name="keychain-read")
        worker.start()
        worker.join(timeout)
        if worker.is_alive():
            raise CredentialError(
                f"The Keychain did not answer within {timeout:.0f} seconds for "
                f"'{account}'. This usually means macOS is waiting for someone "
                "to allow access, which cannot happen with no window on screen. "
                "Open Mail Manager once and let it read the password, ticking "
                "Always Allow, then unattended scans will work again."
            )
        if "error" in outcome:
            raise CredentialError(
                f"Could not read '{account}' from the Keychain: {outcome['error']}")
        return outcome.get("value", "")

    def set(self, account: str, secret: str) -> None:
        if not account:
            raise CredentialError("A Keychain account name is required.")
        try:
            if secret:
                self._keyring().set_password(self.service, account, secret)
            else:
                self.delete(account)
        except CredentialError:
            raise
        except Exception as exc:
            raise CredentialError(f"Could not save '{account}' to the Keychain: {exc}") from exc

    def delete(self, account: str) -> None:
        if not account:
            return
        try:
            self._keyring().delete_password(self.service, account)
        except CredentialError:
            raise
        except Exception:
            # keyring raises PasswordDeleteError when nothing is stored; that is
            # the desired end state either way.
            return

    # -- typed accessors -------------------------------------------------
    @staticmethod
    def icloud_account(email: str) -> str:
        return f"icloud:{(email or '').strip().lower()}"

    def get_mailbox_password(self, email: str) -> str:
        """The stored password for a mailbox, whichever provider it is on."""
        if not (email or "").strip():
            return ""
        return self.get(self.icloud_account(email))

    def set_mailbox_password(self, email: str, password: str) -> None:
        if not (email or "").strip():
            raise CredentialError("Enter the email address before saving the password.")
        self.set(self.icloud_account(email), password)

    #: The original names. Keychain entries are keyed by address, so these
    #: were never iCloud-specific in practice, only in what they were called.
    get_icloud_password = get_mailbox_password
    set_icloud_password = set_mailbox_password

    @staticmethod
    def provider_account(provider: str) -> str:
        """Keychain account name for a provider's API key.

        Anthropic keeps its original account name so an existing Keychain entry
        keeps working after this app gained other backends.
        """
        name = (provider or "").strip().lower()
        return ANTHROPIC_ACCOUNT if name in ("", "anthropic") else f"apikey:{name}"

    def get_provider_key(self, provider: str) -> str:
        """The key for a backend: the Keychain first, then the environment.

        A Keychain that will not answer must not stop the environment being
        read. Somebody with ANTHROPIC_API_KEY exported has told us the key
        already, and refusing to look because a permission prompt went
        unanswered would be a strange way to repay that.
        """
        trouble: Optional[CredentialError] = None
        try:
            stored = self.get(self.provider_account(provider))
        except CredentialError as exc:
            stored, trouble = "", exc
        if stored:
            return stored
        for variable in PROVIDER_ENV_KEYS.get((provider or "").strip().lower(), ()):
            value = os.environ.get(variable, "").strip()
            if value:
                return value
        if trouble is not None:
            raise trouble
        return ""

    def set_provider_key(self, provider: str, key: str) -> None:
        self.set(self.provider_account(provider), (key or "").strip())

    # The original two-argument-free helpers, kept for the Anthropic path.
    def get_anthropic_key(self) -> str:
        return self.get_provider_key("anthropic")

    def set_anthropic_key(self, key: str) -> None:
        self.set_provider_key("anthropic", key)


class InMemoryCredentialStore(CredentialStore):
    """Keychain-free store used by the test suite and headless runs."""

    def __init__(self, service: str = KEYCHAIN_SERVICE) -> None:
        super().__init__(service=service, backend=object())
        self._data: Dict[str, str] = {}

    def available(self) -> bool:
        return True

    def backend_name(self) -> str:
        return "in-memory (not persisted)"

    def get(self, account: str) -> str:
        if account == ANTHROPIC_ACCOUNT and account not in self._data:
            return os.environ.get("ANTHROPIC_API_KEY", "").strip()
        return self._data.get(account, "")

    def set(self, account: str, secret: str) -> None:
        if not account:
            raise CredentialError("A Keychain account name is required.")
        if secret:
            self._data[account] = secret
        else:
            self._data.pop(account, None)

    def delete(self, account: str) -> None:
        self._data.pop(account, None)
