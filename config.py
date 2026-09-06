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
    #: Which mailbox the window is showing. Empty means all of them.
    active_account: str = ""

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

    folder_root: str = DEFAULT_FOLDER_ROOT
    other_folder_root: str = DEFAULT_OTHER_ROOT
    non_job_routing: str = NonJobRouting.LEAVE.value
    auto_approve_non_job: bool = False
    subscribe_new_folders: bool = True

    last_window: str = TimeWindow.LAST_24_HOURS.name
    custom_start: str = ""
    custom_end: str = ""
    max_messages: int = 400

    window_geometry: str = ""
    splitter_state: str = ""
    table_state: str = ""
    show_log_panel: bool = False
    row_lines: int = 3

    # Unattended scanning
    schedule_minutes: int = 0            # 0 means off
    background_window_minutes: int = 180
    auto_file_background: bool = False
    background_agent: bool = False       # keep running when the app is closed
    menu_bar_icon: bool = True
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
        data["active_account"] = str(data.get("active_account", "")).strip()
        if not profiles.exists(str(data.get("sort_profile", ""))):
            data["sort_profile"] = profiles.DEFAULT_PROFILE
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
        data["schedule_minutes"] = _clamp_int(data["schedule_minutes"], 0, 10080, 0)
        data["background_window_minutes"] = _clamp_int(
            data["background_window_minutes"], 15, 20160, 180
        )
        if data["last_window"] not in {w.name for w in TimeWindow}:
            data["last_window"] = TimeWindow.LAST_24_HOURS.name
        for key in ("auto_approve_non_job", "subscribe_new_folders", "show_log_panel",
                    "hide_non_job", "fallback_to_rules", "auto_file_background",
                    "background_agent", "menu_bar_icon"):
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
        if self.active_account not in known:
            self.active_account = ""

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
                  if not self.active_account or a.id == self.active_account]
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

    def account_by_id(self, account_id: str) -> Optional[Account]:
        return next((a for a in self.mailboxes if a.id == account_id), None)

    @property
    def multi_account(self) -> bool:
        return len(self.enabled_accounts) > 1

    # -- profile ---------------------------------------------------------
    @property
    def profile(self) -> "profiles.Profile":
        return profiles.get(self.sort_profile)

    def folder_plan(self, delimiter: str = "/") -> FolderPlan:
        """The folder layout implied by the current profile and roots."""
        active = self.profile
        return FolderPlan(
            root=self.folder_root,
            other_root=self.other_folder_root,
            delimiter=delimiter or "/",
            detailed_job_folders=active.detailed_job_folders,
            topics=active.topics,
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

    def __init__(self, service: str = KEYCHAIN_SERVICE, backend: Any = None) -> None:
        self.service = service
        self._backend = backend

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
    def get(self, account: str) -> str:
        if not account:
            return ""
        try:
            return self._keyring().get_password(self.service, account) or ""
        except CredentialError:
            raise
        except Exception as exc:
            raise CredentialError(f"Could not read '{account}' from the Keychain: {exc}") from exc

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
        stored = self.get(self.provider_account(provider))
        if stored:
            return stored
        for variable in PROVIDER_ENV_KEYS.get((provider or "").strip().lower(), ()):
            value = os.environ.get(variable, "").strip()
            if value:
                return value
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
