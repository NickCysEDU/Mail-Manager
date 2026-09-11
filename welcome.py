"""First-run setup.

Four short pages: what the app does, the mailbox, the classifier, and the
folders it will use. It finishes with everything stored and the app ready to
scan, so a new user never has to hunt through Settings to get started.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QRadioButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWizard,
    QWizardPage,
)

import accounts
import profiles
import providers
import rulesets
from accounts import Account
import config
from config import CredentialError, CredentialStore, Settings
from models import APP_DISPLAY_NAME, FolderPlan
from widgets import _attr_url


def _watermark() -> Optional[QPixmap]:
    """The app's icon, sized for the wizard's side panel. None if missing."""
    import sys

    roots = [Path(__file__).resolve().parent]
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:                      # pragma: no cover - only in the .app
        roots.insert(0, Path(bundled))
    for root in roots:
        candidate = root / "assets" / "icon.png"
        if candidate.exists():
            pixmap = QPixmap(str(candidate))
            if not pixmap.isNull():
                return pixmap.scaled(
                    140, 140, Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)
    return None


def _body(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.RichText)
    label.setOpenExternalLinks(True)
    return label


class IntroPage(QWizardPage):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setTitle(f"Welcome to {APP_DISPLAY_NAME}")
        self.setSubTitle("Three short steps and you are ready to scan.")
        layout = QVBoxLayout(self)
        layout.addWidget(_body(
            "<p>This app reads your iCloud inbox, works out which messages are "
            "part of your job search, and files them into folders you approve.</p>"
            "<p><b>Nothing moves without you.</b> Every scan produces a list with a "
            "tick box per message. Only the ticked ones are filed, and anything the "
            "classifier is unsure about goes to <i>Needs Review</i> instead of a "
            "category folder.</p>"
            "<p>Messages are read without being marked as read, and the folders are "
            "created for you the first time you scan.</p>"
        ))
        layout.addStretch(1)


class AccountsPage(QWizardPage):
    """Link one mailbox, or several, before anything else happens.

    This used to ask for an iCloud address and an app-specific password and
    nothing else, so somebody whose mail is on Gmail could not finish setup,
    and nobody could add a second account without going to Settings
    afterwards. The app has supported both since long before this page did.
    """

    def __init__(self, store: CredentialStore, parent=None) -> None:
        super().__init__(parent)
        self._store = store
        self._accounts: list = []
        self._passwords: dict = {}
        self._editing = -1
        self.setTitle("Your mailboxes")
        self.setSubTitle(
            "Add as many as you like. Passwords go in the macOS Keychain, "
            "never into a file.")

        outer = QVBoxLayout(self)

        self.listing = QListWidget()
        self.listing.setMaximumHeight(110)
        self.listing.currentRowChanged.connect(self._show)
        outer.addWidget(self.listing)

        buttons = QHBoxLayout()
        self.add_button = QToolButton()
        self.add_button.setText("Add another")
        self.add_button.clicked.connect(self._add)
        buttons.addWidget(self.add_button)
        self.remove_button = QToolButton()
        self.remove_button.setText("Remove")
        self.remove_button.clicked.connect(self._remove)
        buttons.addWidget(self.remove_button)
        buttons.addStretch(1)
        outer.addLayout(buttons)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.preset = QComboBox()
        for name, label in accounts.choices():
            self.preset.addItem(label, name)
        self.preset.currentIndexChanged.connect(self._preset_changed)
        form.addRow("Provider", self.preset)

        self.email = QLineEdit()
        self.email.textChanged.connect(self._capture)
        form.addRow("Email address", self.email)

        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.textChanged.connect(self._capture)
        reveal = QToolButton()
        reveal.setText("Show")
        reveal.setCheckable(True)
        reveal.toggled.connect(
            lambda on: self.password.setEchoMode(
                QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.password, 1)
        row.addWidget(reveal)
        form.addRow("Password", holder)

        self.server = QLineEdit()
        self.server.textChanged.connect(self._capture)
        self.server_label = QLabel("IMAP server")
        form.addRow(self.server_label, self.server)

        outer.addLayout(form)

        self.hint = _body("")
        outer.addWidget(self.hint)
        outer.addStretch(1)

        self._add()

    # -- the list --------------------------------------------------------
    def _add(self) -> None:
        self._accounts.append(Account(preset="icloud"))
        self._editing = len(self._accounts) - 1
        self._refresh()
        self.email.setFocus()

    def _remove(self) -> None:
        if len(self._accounts) <= 1:
            return
        del self._accounts[self._editing]
        self._editing = max(0, self._editing - 1)
        self._refresh()

    def _refresh(self) -> None:
        self.listing.blockSignals(True)
        self.listing.clear()
        for account in self._accounts:
            self.listing.addItem(self._describe(account))
        self.listing.setCurrentRow(self._editing)
        self.listing.blockSignals(False)
        self.remove_button.setEnabled(len(self._accounts) > 1)
        self._show(self._editing)

    def _describe(self, account) -> str:
        if not account.address:
            return "New mailbox, enter an address"
        host = accounts.host_for(account.preset)
        ready = bool(self._passwords.get(account.address, "").strip())
        return (f"{account.address}  ·  {host.label}"
                f"  ·  {'ready' if ready else 'needs a password'}")

    def _show(self, index: int) -> None:
        if not (0 <= index < len(self._accounts)):
            return
        self._editing = index
        account = self._accounts[index]
        self._loading = True
        self.preset.setCurrentIndex(max(0, self.preset.findData(account.preset)))
        self.email.setText(account.address)
        self.password.setText(self._passwords.get(account.address, ""))
        self.server.setText(account.host)
        self._loading = False
        self._preset_changed()

    # -- the form --------------------------------------------------------
    def _preset_changed(self) -> None:
        name = self.preset.currentData() or "custom"
        host = accounts.host_for(name)
        custom = name == "custom"
        self.server_label.setVisible(custom)
        self.server.setVisible(custom)
        if not custom:
            self.server.setText(host.host)
        self.email.setPlaceholderText(
            f"you@{host.domains[0]}" if host.domains else "you@example.com")
        note = f"<p><b>{host.secret_label}.</b> {host.note}"
        if host.help_url:
            note += f' <a href="{_attr_url(host.help_url)}">Generate one</a>.'
        self.hint.setText(note + "</p>")
        if not getattr(self, "_loading", False):
            # Switching provider while an address from the old one is still in
            # the box is not a change of server, it is a different mailbox.
            # Keeping it is how a Gmail password once got filed under an
            # iCloud address.
            address = self.email.text().strip()
            belongs = accounts.host_for_address(address) if address else None
            if (address and belongs is not None and not belongs.is_custom
                    and belongs.name != name):
                self.email.clear()
                self.password.clear()
            self._capture()

    def _capture(self) -> None:
        """Read the form back into the account being edited, on every keystroke."""
        if getattr(self, "_loading", False):
            return
        if not (0 <= self._editing < len(self._accounts)):
            return
        name = self.preset.currentData() or "custom"
        host = accounts.host_for(name)
        account = self._accounts[self._editing]
        account.address = self.email.text().strip()
        account.preset = name
        account.host = (self.server.text().strip() if name == "custom"
                        else host.host)
        account.port = host.port
        if account.address:
            self._passwords[account.address] = self.password.text()
        item = self.listing.item(self._editing)
        if item is not None:
            item.setText(self._describe(account))
        self.completeChanged.emit()

    # -- what the wizard asks for ----------------------------------------
    def ready_accounts(self) -> list:
        """Only the ones with an address and a password worth storing."""
        return [a for a in self._accounts
                if a.address and accounts.valid_address(a.address)]

    def passwords(self) -> dict:
        return {a.address: self._passwords.get(a.address, "")
                for a in self.ready_accounts()}

    def isComplete(self) -> bool:  # noqa: N802
        """Next stays off until at least one address is a real address."""
        return bool(self.ready_accounts())

    def initializePage(self) -> None:
        stored = Settings.load()
        existing = [a for a in stored.mailboxes if a.address]
        if not existing:
            return
        self._accounts = [Account(**{f: getattr(a, f) for f in
                                     ("id", "label", "address", "preset",
                                      "host", "port")}) for a in existing]
        for account in self._accounts:
            try:
                self._passwords[account.address] = \
                    self._store.get_icloud_password(account.address)
            except CredentialError:
                self._passwords[account.address] = ""
        self._editing = 0
        self._refresh()


class ClassifierPage(QWizardPage):
    def __init__(self, store: CredentialStore, parent=None) -> None:
        super().__init__(parent)
        self._store = store
        self.setTitle("How messages get sorted")
        self.setSubTitle("The built-in sorter needs no account and no network.")

        self.backend = QComboBox()
        for name, label, _blurb in providers.provider_choices():
            self.backend.addItem(label, name)
        self.backend.currentIndexChanged.connect(self._backend_changed)

        self.field = QComboBox()
        for name, label, _blurb in rulesets.choices():
            self.field.addItem(label, name)

        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_label = QLabel("API key")
        self.note = _body("")

        form = QFormLayout(self)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.addRow("Sorter", self.backend)
        form.addRow("Your field", self.field)
        form.addRow(self.key_label, self.key)
        form.addRow(self.note)
        form.addRow(_body(
            "<p>The built-in sorter is a rule set: about 530 weighted signals covering "
            "the language hiring mail actually uses. It is instant, free, and no message "
            "leaves your Mac.</p>"
            "<p>You can switch to a language model later from the gear button in the "
            "toolbar. Either way, anything the sorter is unsure about goes to "
            "<i>Needs Review</i>.</p>"
        ))
        self._backend_changed()

    def _backend_changed(self) -> None:
        name = self.backend.currentData() or providers.DEFAULT_PROVIDER
        spec = providers.provider_class(name)
        needs_key = bool(spec.needs_api_key)
        self.key_label.setVisible(needs_key)
        self.key.setVisible(needs_key)
        self.key_label.setText(f"{spec.label} key")
        self.key.setPlaceholderText(spec.key_hint or "API key")
        self.field.setEnabled(name == providers.FALLBACK_PROVIDER)
        if needs_key:
            try:
                self.key.setText(self._store.get_provider_key(name))
            except CredentialError:
                self.key.clear()
        self.note.setText(f"<i>{spec.blurb}</i>")

    def initializePage(self) -> None:
        settings = Settings.load()
        index = self.backend.findData(settings.provider)
        self.backend.setCurrentIndex(max(0, index))
        index = self.field.findData(settings.ruleset)
        self.field.setCurrentIndex(max(0, index))
        self._backend_changed()


class PurposePage(QWizardPage):
    """What the app is for, which decides everything downstream."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setTitle("Which folders should be created?")
        self.setSubTitle(
            "Job-search folders, everyday folders, or both. Changeable later "
            "in Settings, and nothing is created until your first scan.")

        self.choice = QButtonGroup(self)
        self.choice.setExclusive(True)
        layout = QVBoxLayout(self)

        for index, (name, label, blurb) in enumerate(profiles.choices()):
            profile = profiles.get(name)
            button = QRadioButton(label)
            button.setProperty("profile", name)
            self.choice.addButton(button, index)
            # What each one builds, written from the profile itself so the
            # description cannot drift from what actually gets made.
            note = _body(
                f"<p style='margin:0 0 2px 22px'>{blurb}</p>"
                f"<p style='margin:0 0 10px 22px;opacity:0.75'>"
                f"<b>Creates</b> {profile.creates()}.</p>")
            layout.addWidget(button)
            layout.addWidget(note)
            if name == profiles.DEFAULT_PROFILE:
                button.setChecked(True)

        # The topic list, shown only when a profile that uses topics is picked.
        self.topics_box = QGroupBox("Which of these get a folder")
        topics_layout = QVBoxLayout(self.topics_box)
        topics_layout.addWidget(_body(
            "<p>Untick anything you would rather leave in your inbox. These are "
            "the topics the offline sorter can recognise; it cannot learn a new "
            "one, so the list is fixed.</p>"
        ))
        grid = QGridLayout()
        self.topic_checks = {}
        for position, topic in enumerate(profiles.ALL_TOPICS):
            check = QCheckBox(topic.label)
            self.topic_checks[topic] = check
            grid.addWidget(check, position // 2, position % 2)
        topics_layout.addLayout(grid)
        layout.addWidget(self.topics_box)
        layout.addStretch(1)

        self.choice.idToggled.connect(lambda *_: self._sync())
        self._sync()

    def profile_name(self) -> str:
        button = self.choice.checkedButton()
        return button.property("profile") if button else profiles.DEFAULT_PROFILE

    def chosen_topics(self):
        return tuple(topic for topic, check in self.topic_checks.items()
                     if check.isChecked())

    def _sync(self) -> None:
        """Show the topic list only when the chosen profile actually uses it."""
        profile = profiles.get(self.profile_name())
        self.topics_box.setVisible(bool(profile.topics))
        for topic, check in self.topic_checks.items():
            check.setChecked(topic in profile.topics)


class FoldersPage(QWizardPage):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setTitle("Folders")
        self.setSubTitle("Created in your mailbox on the first scan.")
        self.tree = _body("")
        self.schedule = QCheckBox("Also scan in the background every 3 hours")
        self.menu_bar = QCheckBox("Show an icon in the menu bar")
        self.menu_bar.setChecked(True)

        layout = QVBoxLayout(self)
        layout.addWidget(_body(
            "<p>The app creates these folders itself the first time it scans. You do "
            "not need to make them in Mail, and existing folders with the same names "
            "are reused rather than duplicated.</p>"
        ))
        layout.addWidget(self.tree)
        self.leftovers = _body(
            "<p>Messages that are not part of your job search stay where they are, "
            "in your inbox. Nothing outside your job search is moved unless you ask "
            "for it in Settings.</p>"
        )
        layout.addWidget(self.leftovers)
        layout.addWidget(self.schedule)
        layout.addWidget(self.menu_bar)
        layout.addStretch(1)

    def initializePage(self) -> None:
        wizard = self.wizard()
        purpose = getattr(wizard, "purpose", None)
        profile = profiles.get(purpose.profile_name() if purpose else profiles.DEFAULT_PROFILE)
        topics = purpose.chosen_topics() if purpose else profile.topics

        stored = Settings.load()
        plan = FolderPlan(
            root=stored.folder_root, other_root=stored.other_folder_root,
            detailed_job_folders=profile.detailed_job_folders, topics=topics,
        )
        names = list(plan.leaf_folders)
        if topics:
            names += [f for f in plan.other_folders(topics)[1:]]
        rows = "".join(f"<li><code>{name}</code></li>" for name in names)
        self.tree.setText(f"<ul style='margin-left:-18px'>{rows}</ul>")
        self.leftovers.setVisible(not profile.sorts_everything)


class SetupWizard(QWizard):
    """Collects everything a first scan needs, then writes it."""

    def __init__(self, settings: Settings, store: CredentialStore, parent=None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._store = store
        self.setWindowTitle(f"Set up {APP_DISPLAY_NAME}")
        self.setWizardStyle(QWizard.WizardStyle.MacStyle)
        # Qt's MacStyle wizard ships a stock watermark - a dress shirt and a
        # bow tie - which has nothing to do with this app and is the first
        # thing a new user sees. The app's own icon says what they opened.
        badge = _watermark()
        if badge is not None:
            self.setPixmap(QWizard.WizardPixmap.BackgroundPixmap, badge)
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage, True)
        self.setMinimumSize(680, 520)

        self.intro = IntroPage(self)
        self.mailbox = AccountsPage(store, self)
        self.purpose = PurposePage(self)
        self.classifier = ClassifierPage(store, self)
        self.folders = FoldersPage(self)
        for page in (self.intro, self.mailbox, self.purpose,
                     self.classifier, self.folders):
            self.addPage(page)

    def result_settings(self) -> Settings:
        """The settings chosen in the wizard, normalised."""
        from dataclasses import replace

        provider = self.classifier.backend.currentData() or providers.DEFAULT_PROVIDER
        profile = profiles.get(self.purpose.profile_name())
        linked = self.mailbox.ready_accounts()
        return replace(
            self._settings,
            mailboxes=list(linked),
            # The first one still fills the older single-mailbox fields, which
            # a good deal of the app reads.
            icloud_email=linked[0].address if linked else "",
            sort_profile=profile.name,
            topics=[t.value for t in self.purpose.chosen_topics()],
            non_job_routing=profile.non_job_routing.value,
            provider=provider,
            model=providers.default_model_for(provider),
            ruleset=self.classifier.field.currentData() or "general",
            schedule_minutes=180 if self.folders.schedule.isChecked() else 0,
            menu_bar_icon=self.folders.menu_bar.isChecked(),
        ).normalized()

    def save(self) -> Settings:
        from dataclasses import replace as _replace

        settings = self.result_settings()
        key = self.classifier.key.text() if settings.needs_api_key else ""
        try:
            for address, secret in self.mailbox.passwords().items():
                if secret.strip():
                    self._store.set_icloud_password(address, secret)
            if settings.needs_api_key and key:
                self._store.set_provider_key(settings.provider, key)
        except CredentialError:
            pass
        # A backend that needs a key and did not get one would fail on the
        # first scan with an authentication error, which is a poor way to
        # find out. Hand the work to the offline sorter instead: it needs
        # nothing, and the backend is one click away in the toolbar.
        if settings.needs_api_key and not key.strip():
            chosen = config.backend_for_key(settings.provider, settings.provider, "")
            settings = _replace(settings, provider=chosen,
                                model=providers.default_model_for(chosen)).normalized()
        settings.save()
        return settings
