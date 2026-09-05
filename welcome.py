"""First-run setup.

Four short pages: what the app does, the mailbox, the classifier, and the
folders it will use. It finishes with everything stored and the app ready to
scan, so a new user never has to hunt through Settings to get started.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWizard,
    QWizardPage,
)

import providers
import rulesets
from config import CredentialError, CredentialStore, Settings
from models import APP_DISPLAY_NAME, FolderPlan


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


class MailboxPage(QWizardPage):
    def __init__(self, store: CredentialStore, parent=None) -> None:
        super().__init__(parent)
        self._store = store
        self.setTitle("Your iCloud mailbox")
        self.setSubTitle("Stored in the macOS Keychain, never in a file.")

        self.email = QLineEdit()
        self.email.setPlaceholderText("you@icloud.com")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("xxxx-xxxx-xxxx-xxxx")
        reveal = QToolButton()
        reveal.setText("Show")
        reveal.setCheckable(True)
        reveal.toggled.connect(
            lambda on: self.password.setEchoMode(
                QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password
            )
        )
        row = QHBoxLayout()
        row.addWidget(self.password, 1)
        row.addWidget(reveal)
        holder = QWidget()
        holder.setLayout(row)
        row.setContentsMargins(0, 0, 0, 0)

        form = QFormLayout(self)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.addRow("iCloud email", self.email)
        form.addRow("App-specific password", holder)
        form.addRow(_body(
            "<p>iCloud rejects your normal Apple ID password over IMAP when two-factor "
            "authentication is on, so you need an app-specific password:</p>"
            "<ol><li>Open <a href='https://account.apple.com'>account.apple.com</a> and "
            "sign in</li><li>Go to <b>Sign-In and Security</b>, then "
            "<b>App-Specific Passwords</b></li><li>Create one, name it "
            "<i>Mail Manager</i>, and paste it above</li></ol>"
        ))
        self.registerField("email*", self.email)
        self.registerField("password", self.password)

    def initializePage(self) -> None:
        settings = Settings.load()
        if settings.icloud_email:
            self.email.setText(settings.icloud_email)
            try:
                self.password.setText(self._store.get_icloud_password(settings.icloud_email))
            except CredentialError:
                pass


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


class FoldersPage(QWizardPage):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setTitle("Folders")
        self.setSubTitle("Created in your iCloud account on the first scan.")
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
        layout.addWidget(_body(
            "<p>Messages that are not part of your job search stay where they are, "
            "in your inbox. Nothing outside your job search is moved unless you ask "
            "for it in Settings.</p>"
        ))
        layout.addWidget(self.schedule)
        layout.addWidget(self.menu_bar)
        layout.addStretch(1)

    def initializePage(self) -> None:
        plan = FolderPlan(root=Settings.load().folder_root)
        rows = "".join(f"<li><code>{name}</code></li>" for name in plan.leaf_folders)
        self.tree.setText(f"<ul style='margin-left:-18px'>{rows}</ul>")


class SetupWizard(QWizard):
    """Collects everything a first scan needs, then writes it."""

    def __init__(self, settings: Settings, store: CredentialStore, parent=None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._store = store
        self.setWindowTitle(f"Set up {APP_DISPLAY_NAME}")
        self.setWizardStyle(QWizard.WizardStyle.MacStyle)
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage, True)
        self.setMinimumSize(680, 520)

        self.intro = IntroPage(self)
        self.mailbox = MailboxPage(store, self)
        self.classifier = ClassifierPage(store, self)
        self.folders = FoldersPage(self)
        for page in (self.intro, self.mailbox, self.classifier, self.folders):
            self.addPage(page)

    def result_settings(self) -> Settings:
        """The settings chosen in the wizard, normalised."""
        from dataclasses import replace

        provider = self.classifier.backend.currentData() or providers.DEFAULT_PROVIDER
        return replace(
            self._settings,
            icloud_email=self.mailbox.email.text().strip(),
            provider=provider,
            model=providers.default_model_for(provider),
            ruleset=self.classifier.field.currentData() or "general",
            schedule_minutes=180 if self.folders.schedule.isChecked() else 0,
            menu_bar_icon=self.folders.menu_bar.isChecked(),
        ).normalized()

    def save(self) -> Settings:
        settings = self.result_settings()
        try:
            if self.mailbox.password.text():
                self._store.set_icloud_password(
                    settings.icloud_email, self.mailbox.password.text()
                )
            if settings.needs_api_key and self.classifier.key.text():
                self._store.set_provider_key(settings.provider, self.classifier.key.text())
        except CredentialError:
            pass
        settings.save()
        return settings
