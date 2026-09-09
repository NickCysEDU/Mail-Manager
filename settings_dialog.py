"""Settings, and the dialogs that hang off it.

Split out of gui.py, which had grown to six and a half thousand lines. This
half of it is everything a person configures - mailboxes, the analysis
backend, folders, reply rules, appearance - plus the window that manages
locally installed models.

A pure move: every class is exactly as it was in gui.py, and gui re-exports
them so nothing that imports from there has to change.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import Qt, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QDoubleValidator
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QSizePolicy, QSlider, QSpinBox, QTabWidget, QToolButton, QVBoxLayout,
    QWidget)

import accounts
import autoreply
import corrections
import verdict_cache
import helpmode
import ondevice
import profiles
import providers
import theme
from accounts import Account
from config import (EFFORT_LEVELS, CredentialError, CredentialStore, Settings)
from imap_engine import clean_secret
from models import (Category, FolderPlan, NonJobRouting, OtherCategory,
                    TriageItem)
from widgets import (ACCENT_GREEN, ACCENT_RED, AdaptiveLineEdit, WrappingList,
                     _abandon, _compact_button, _html, _paint_button,
                     _scrollable, _separator, menu_text, selectable)
from workers import ConnectionTestWorker


class _RuleRow(QWidget):
    """One line of a rule: some combo boxes, a value, and a way to delete it.

    Conditions and actions are close enough to share the plumbing. What
    differs is which combo boxes there are and what a value looks like, and
    both subclasses answer that in `_value_kind`.
    """

    changed = Signal()
    removed = Signal(object)

    def __init__(self, mailboxes=None, folders=None, parent=None) -> None:
        super().__init__(parent)
        self._mailboxes = list(mailboxes or [])
        self._folders = list(folders or [])
        self._value_widget: Optional[QWidget] = None
        self._quiet = False

        # A tall editor - a template, some guidance - goes underneath rather
        # than in the line, or the combo boxes beside it float in the middle
        # of a hundred points of nothing.
        self.stack = QVBoxLayout(self)
        self.stack.setContentsMargins(0, 0, 0, 0)
        self.stack.setSpacing(4)
        self.row = QHBoxLayout()
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(6)
        self.stack.addLayout(self.row)

    # -- the value editor, which changes shape with the field --------------
    #: Value editors too tall to sit in the line with the combo boxes.
    TALL = ("template", "guidance")

    def _build_value(self, kind: str, value: str) -> None:
        """Swap in the editor this kind of value deserves."""
        if self._value_widget is not None:
            self._value_widget.setParent(None)
            self._value_widget.deleteLater()
            self._value_widget = None
        widget = self._make_value_widget(kind, value)
        self._value_widget = widget
        if widget is None:
            return
        if kind in self.TALL:
            self.stack.addWidget(widget)
        else:
            self.row.insertWidget(self.row.count() - 1, widget, 3)

    def _make_value_widget(self, kind: str, value: str) -> Optional[QWidget]:
        if kind == "none":
            spacer = QLabel("")
            spacer.setSizePolicy(QSizePolicy.Policy.Expanding,
                                 QSizePolicy.Policy.Preferred)
            return spacer
        if kind in ("category", "topic", "mailbox", "folder_pick"):
            combo = QComboBox()
            combo.setEditable(kind == "folder_pick")
            combo.setSizePolicy(QSizePolicy.Policy.Ignored,
                                QSizePolicy.Policy.Fixed)
            combo.setMinimumWidth(84)
            for item_value, label in self._choices(kind):
                combo.addItem(label, item_value)
            found = combo.findData(value)
            if found >= 0:
                combo.setCurrentIndex(found)
            elif combo.isEditable():
                combo.setEditText(value)
            combo.currentIndexChanged.connect(self._touched)
            if combo.isEditable():
                combo.editTextChanged.connect(self._touched)
            return combo
        if kind in ("template", "guidance"):
            box = QPlainTextEdit()
            box.setPlainText(value)
            box.setMinimumHeight(84)
            box.setMaximumHeight(150)
            box.setPlaceholderText(
                "Hello {first_name},\n\n…\n\nBest wishes,\n{me}"
                if kind == "template" else
                "What the reply has to do, in your own words."
            )
            box.textChanged.connect(self._touched)
            return box
        if kind == "number":
            edit = AdaptiveLineEdit("0.90")
            edit.setValidator(QDoubleValidator(0.0, 100000.0, 3))
        else:
            edit = AdaptiveLineEdit("what to look for", "text")
        edit.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        edit.setMinimumWidth(70)
        edit.setText(value)
        edit.textChanged.connect(self._touched)
        return edit

    def _choices(self, kind: str) -> List[Tuple[str, str]]:
        if kind == "category":
            return [(c.value, c.label) for c in Category
                    if c is not Category.UNCLASSIFIED_OTHER]
        if kind == "topic":
            return [(t.value, t.label) for t in profiles.ALL_TOPICS]
        if kind == "mailbox":
            return [(a.address or a.id, a.address or a.label) for a in self._mailboxes]
        return [(f, f) for f in self._folders]

    def _value_text(self) -> str:
        widget = self._value_widget
        if isinstance(widget, QComboBox):
            return (widget.currentData() if not widget.isEditable()
                    else widget.currentText()) or ""
        if isinstance(widget, QPlainTextEdit):
            return widget.toPlainText()
        if isinstance(widget, QLineEdit):
            return widget.text()
        return ""

    def _touched(self, *_args) -> None:
        if not self._quiet:
            self.changed.emit()

    def _delete_button(self) -> QToolButton:
        return _compact_button("−", "Remove this line",
                               lambda: self.removed.emit(self))


class ConditionRow(_RuleRow):
    """Field, operator, value - the shape every mail rule has ever had."""

    def __init__(self, condition, mailboxes=None, parent=None) -> None:
        super().__init__(mailboxes=mailboxes, parent=parent)
        self._quiet = True

        self.field_combo = QComboBox()
        for name, label, _kind in autoreply.FIELDS:
            self.field_combo.addItem(label, name)
        self.field_combo.setCurrentIndex(
            max(0, self.field_combo.findData(condition.field)))
        self.field_combo.setMinimumWidth(96)
        self.field_combo.setSizePolicy(QSizePolicy.Policy.Ignored,
                                       QSizePolicy.Policy.Fixed)
        self.field_combo.currentIndexChanged.connect(self._field_changed)
        self.row.addWidget(self.field_combo, 3)
        self._explain_field()

        self.operator_combo = QComboBox()
        self.operator_combo.setMinimumWidth(88)
        self.operator_combo.setSizePolicy(QSizePolicy.Policy.Ignored,
                                          QSizePolicy.Policy.Fixed)
        self.operator_combo.currentIndexChanged.connect(self._operator_changed)
        self.row.addWidget(self.operator_combo, 3)

        self.row.addWidget(self._delete_button())
        self._fill_operators(condition.operator)
        self._build_value(self._value_kind(), condition.value)
        self._quiet = False

    def _value_kind(self) -> str:
        field = self.field_combo.currentData() or "anywhere"
        kind = autoreply.field_kind(field)
        if kind == "flag":
            return "none"
        return kind

    def _fill_operators(self, wanted: str = "") -> None:
        was_quiet, self._quiet = self._quiet, True
        self.operator_combo.blockSignals(True)
        self.operator_combo.clear()
        field = self.field_combo.currentData() or "anywhere"
        for name, label in autoreply.operators_for(field):
            self.operator_combo.addItem(label, name)
        found = self.operator_combo.findData(wanted)
        self.operator_combo.setCurrentIndex(max(0, found))
        self.operator_combo.blockSignals(False)
        self._quiet = was_quiet

    def _explain_field(self) -> None:
        """Say what this field looks at, for the help switch to show."""
        field = self.field_combo.currentData() or "anywhere"
        self.field_combo.setToolTip(autoreply.FIELD_HELP.get(field, ""))

    def _field_changed(self) -> None:
        self._fill_operators()
        self._build_value(self._value_kind(), "")
        self._explain_field()
        self._touched()

    def _operator_changed(self) -> None:
        self._touched()

    def value(self):
        return autoreply.Condition(
            field=self.field_combo.currentData() or "anywhere",
            operator=self.operator_combo.currentData() or "contains",
            value=self._value_text(),
        )


class ActionRow(_RuleRow):
    """What to do, and whatever that needs typing into it."""

    def __init__(self, action, folders=None, parent=None) -> None:
        super().__init__(folders=folders, parent=parent)
        self._quiet = True

        self.kind_combo = QComboBox()
        for name, label, _needs in autoreply.ACTION_KINDS:
            self.kind_combo.addItem(label, name)
        self.kind_combo.setCurrentIndex(max(0, self.kind_combo.findData(action.kind)))
        self.kind_combo.setMinimumWidth(130)
        self.kind_combo.setSizePolicy(QSizePolicy.Policy.Ignored,
                                      QSizePolicy.Policy.Fixed)
        self.kind_combo.currentIndexChanged.connect(self._kind_changed)
        self.row.addWidget(self.kind_combo, 3)
        self._explain_kind()

        self.row.addWidget(self._delete_button())
        self._build_value(self._value_kind(), action.value)
        self._quiet = False

    def _value_kind(self) -> str:
        needs = autoreply.action_input(self.kind_combo.currentData() or "draft")
        return "folder_pick" if needs == "folder" else needs

    def _explain_kind(self) -> None:
        kind = self.kind_combo.currentData() or "draft"
        self.kind_combo.setToolTip(autoreply.ACTION_HELP.get(kind, ""))

    def _kind_changed(self) -> None:
        self._build_value(self._value_kind(), "")
        self._explain_kind()
        self._touched()

    def value(self):
        return autoreply.Action(kind=self.kind_combo.currentData() or "draft",
                                value=self._value_text())


class ModelsDialog(QDialog):
    """What is installed on this Mac, and how to add to it or remove from it.

    Downloading a model was already possible from the Analysis tab; there was
    no way to see what you had or to get rid of it, so a machine slowly filled
    with several gigabytes each and nothing said so.
    """

    def __init__(self, endpoint: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Local models")
        self.setMinimumSize(560, 420)
        self.resize(620, 480)
        self._endpoint = endpoint or ondevice.DEFAULT_ENDPOINT
        self._worker = None
        self._probe = None

        outer = QVBoxLayout(self)
        blurb = QLabel(
            "Models run on this Mac. Nothing is sent anywhere and there is "
            "nothing to pay for, but each one takes a few gigabytes of disk "
            "and the first message after a scan starts is slow while it loads."
        )
        blurb.setWordWrap(True)
        outer.addWidget(blurb)

        self.listing = WrappingList()
        self.listing.setMinimumHeight(160)
        self.listing.setMaximumWidth(16777215)
        self.listing.currentRowChanged.connect(self._selection_changed)
        outer.addWidget(self.listing, 1)

        row = QHBoxLayout()
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh)
        row.addWidget(self.refresh_button)
        self.remove_button = QPushButton("Remove…")
        _paint_button(self.remove_button, "destructive")
        self.remove_button.clicked.connect(self._remove_selected)
        row.addWidget(self.remove_button)
        row.addStretch(1)
        outer.addLayout(row)

        outer.addWidget(_separator())

        add = QHBoxLayout()
        add.addWidget(QLabel("Add"))
        self.catalogue = QComboBox()
        self.catalogue.setEditable(True)
        self.catalogue.setMinimumWidth(220)
        for choice in providers.provider_class("ollama").models:
            self.catalogue.addItem(f"{choice.label} — {choice.note}", choice.value)
        self.catalogue.setToolTip(
            "Any name from ollama.com/library works, not only these.")
        add.addWidget(self.catalogue, 1)
        self.download_button = QPushButton("Download")
        _paint_button(self.download_button, "primary")
        self.download_button.clicked.connect(self._download)
        add.addWidget(self.download_button)
        self.stop_button = QPushButton("Stop")
        _paint_button(self.stop_button, "danger")
        self.stop_button.setVisible(False)
        self.stop_button.clicked.connect(self._stop)
        add.addWidget(self.stop_button)
        outer.addLayout(add)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setVisible(False)
        outer.addWidget(self.progress)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        outer.addWidget(self.status)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        outer.addWidget(buttons)

        self.refresh()

    # -- what is here ----------------------------------------------------
    def refresh(self) -> None:
        """Re-read the list, off the thread that draws the window."""
        if self._probe is not None:
            return
        from workers import InstalledModelsWorker

        self.status.setText("Reading what is installed…")
        worker = InstalledModelsWorker(self._endpoint, parent=self)
        self._probe = worker
        worker.finished_ok.connect(self._show_models)
        worker.finished.connect(lambda: setattr(self, "_probe", None))
        worker.start()

    @Slot(object)
    def _show_models(self, outcome) -> None:
        models, error = outcome
        self.listing.clear()
        if error:
            self.status.setText(
                f"Ollama is not answering on {self._endpoint}. Start it from "
                "Settings → Analysis, then press Refresh.")
            self._selection_changed(-1)
            return
        if not models:
            self.status.setText(
                "No models yet. Pick one below and press Download.")
            self._selection_changed(-1)
            return
        for model in models:
            entry = QListWidgetItem(
                f"{model.name}\n{model.describe()} · {model.status_text}")
            entry.setData(Qt.ItemDataRole.UserRole, model.name)
            entry.setToolTip(f"Added {model.modified}" if model.modified else "")
            self.listing.addItem(entry)
        self.listing.measure()
        self.listing.setCurrentRow(0)
        total = sum(m.size for m in models)
        self.status.setText(
            f"{len(models)} model{'' if len(models) == 1 else 's'}, "
            f"{ondevice._human(float(total))} of disk in total.")

    def _selection_changed(self, row: int) -> None:
        self.remove_button.setEnabled(row >= 0 and self._worker is None)

    def _selected_name(self) -> str:
        item = self.listing.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else ""

    # -- adding and removing ---------------------------------------------
    def _download(self) -> None:
        wanted = (self.catalogue.currentData()
                  if self.catalogue.currentIndex() >= 0
                  and self.catalogue.currentText().startswith(
                      self.catalogue.itemText(self.catalogue.currentIndex()))
                  else self.catalogue.currentText().strip())
        wanted = (wanted or self.catalogue.currentText()).strip()
        command = ondevice.pull_command(wanted)
        if not command:
            self.status.setText(
                "Ollama is not installed. Settings → Analysis can install it.")
            return
        self._run("pull", command, f"Downloading {wanted}…")

    def _remove_selected(self) -> None:
        name = self._selected_name()
        if not name:
            return
        answer = QMessageBox.question(
            self, "Remove this model?",
            f"Delete “{name}” from this Mac?\n\nIt can be downloaded again, "
            "which means waiting for the whole thing a second time.",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Cancel)
        if answer != QMessageBox.StandardButton.Yes:
            return
        command = ondevice.remove_command(name)
        if not command:
            self.status.setText("Ollama is not installed, so there is nothing "
                                "to remove it with.")
            return
        self._run("remove", command, f"Removing {name}…")

    def _run(self, step: str, command, saying: str) -> None:
        from workers import OnDeviceWorker

        if self._worker is not None:
            return
        worker = OnDeviceWorker(step, command, parent=self)
        self._worker = worker
        self.status.setText(saying)
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self.download_button.setEnabled(False)
        self.remove_button.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self.stop_button.setVisible(True)
        self.stop_button.setEnabled(True)
        worker.progress.connect(self._on_progress)
        worker.finished_ok.connect(self._on_done)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(lambda: setattr(self, "_worker", None))
        worker.start()

    @Slot(int, int, str)
    def _on_progress(self, done: int, total: int, message: str) -> None:
        if total:
            self.progress.setRange(0, total)
            self.progress.setValue(max(0, min(total, done)))
        else:
            self.progress.setRange(0, 0)
        if message:
            self.status.setText(message[:120])

    @Slot(str, str)
    def _on_failed(self, title: str, detail: str) -> None:
        self._finish()
        self.status.setText(f"{title}: {detail}"[:200])

    @Slot(object)
    def _on_done(self, result) -> None:
        self._finish()
        self.status.setText(result.describe())
        if not result.ok and not result.cancelled:
            box = QMessageBox(QMessageBox.Icon.Warning, "Local models",
                              result.describe(), parent=self)
            box.setDetailedText("\n".join(result.lines[-40:]))
            selectable(box)
            box.exec()
        self.refresh()

    def _finish(self) -> None:
        self.progress.setRange(0, 100)
        self.progress.setVisible(False)
        self.stop_button.setVisible(False)
        self.download_button.setEnabled(True)
        self.refresh_button.setEnabled(True)
        self._selection_changed(self.listing.currentRow())

    def _stop(self) -> None:
        worker = self._worker
        if worker is None:
            return
        self.stop_button.setEnabled(False)
        self.status.setText("Stopping…")
        worker.cancel()

    # -- leaving ---------------------------------------------------------
    def done(self, result: int) -> None:  # noqa: N802
        for name in ("_worker", "_probe"):
            spare = getattr(self, name, None)
            setattr(self, name, None)
            if spare is not None and spare.isRunning() and not spare.stop(4000):
                _abandon(spare)
        super().done(result)


class SettingsDialog(QDialog):
    """Credentials, model, routing and folder configuration."""

    def __init__(self, settings: Settings, store: CredentialStore, parent=None,
                 sample_items: Sequence[TriageItem] = ()) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumSize(640, 480)
        self.resize(760, 680)
        self._settings = settings
        self._store = store
        self._worker: Optional[ConnectionTestWorker] = None
        self._loading_models = False
        self._provider_seen = ""
        #: Working copies. Nothing is written until OK.
        self._accounts: List[Account] = []
        self._account_index = 0
        self._account_passwords: Dict[str, str] = {}
        self._removed_accounts: List[Account] = []
        self._row_lines_touched = False
        self._loading_rule = False
        #: What is on screen behind this dialog, so a rule can be tried on it.
        self._sample_items: List[TriageItem] = list(sample_items)

        self.tabs = QTabWidget()
        # Each tab scrolls, so no amount of text can be cut off at any size.
        self.tabs.addTab(_scrollable(self._build_account_tab()), "Mailboxes")
        self.tabs.addTab(_scrollable(self._build_ai_tab()), "Analysis")
        self.tabs.addTab(_scrollable(self._build_folders_tab()), "Folders")
        self.tabs.addTab(_scrollable(self._build_reply_tab()), "Rules")
        self.tabs.addTab(_scrollable(self._build_appearance_tab()), "Appearance")

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        self.status = QLabel("")
        self.status.setWordWrap(True)

        # The same help switch as the window's, because most of the writing
        # that benefits from it is in here.
        self.help_button = helpmode.HelpButton()
        self.help_button.setChecked(settings.help_mode)
        self.help_button.toggled.connect(self._toggle_help)
        header = QHBoxLayout()
        header.addStretch(1)
        header.addWidget(self.help_button)

        layout = QVBoxLayout(self)
        layout.addLayout(header)
        layout.addWidget(self.tabs)
        layout.addWidget(self.status)
        layout.addWidget(self.buttons)

        self._load_values()

    # -- tabs ------------------------------------------------------------
    def _build_account_tab(self) -> QWidget:
        """Every mailbox, in a list you can see all of at once.

        The previous version hid the list behind a dropdown and captured edits
        silently when you moved away from a field, which left no way to tell
        whether anything had been kept. This shows every mailbox, marks which
        ones are ready to scan, and writes each edit into the selected mailbox
        as it is typed - so the list is the confirmation.
        """
        page = QWidget()
        outer = QVBoxLayout(page)

        self.account_list = QListWidget()
        self.account_list.setMinimumHeight(130)
        self.account_list.setAlternatingRowColors(True)
        self.account_list.currentRowChanged.connect(self._account_selected)
        self.account_list.itemChanged.connect(self._account_ticked)
        self.account_list.setToolTip(
            "Every mailbox this app knows about. Untick one to leave it out of "
            "scans without forgetting it."
        )
        outer.addWidget(self.account_list)

        buttons = QHBoxLayout()
        self.add_account_button = QPushButton("Add mailbox")
        _paint_button(self.add_account_button, "primary", bold=False)
        self.add_account_button.setToolTip("Set up another mailbox")
        self.add_account_button.clicked.connect(self._add_account)
        buttons.addWidget(self.add_account_button)

        self.remove_account_button = QPushButton("Remove")
        _paint_button(self.remove_account_button, "destructive", bold=False)
        self.remove_account_button.setToolTip(
            "Forget the selected mailbox. Nothing in it is touched, and its "
            "password is removed from the Keychain."
        )
        self.remove_account_button.clicked.connect(self._remove_account)
        buttons.addWidget(self.remove_account_button)

        self.test_imap_button = QPushButton("Test this mailbox")
        self.test_imap_button.setToolTip(
            "Sign in to the selected mailbox now and report what happens")
        self.test_imap_button.clicked.connect(lambda: self._run_test("imap"))
        buttons.addWidget(self.test_imap_button)
        buttons.addStretch(1)
        outer.addLayout(buttons)

        self.account_summary = QLabel()
        self.account_summary.setWordWrap(True)
        self.account_summary.setProperty("dim", "true")
        outer.addWidget(self.account_summary)
        outer.addWidget(_separator())

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.preset_combo = QComboBox()
        for name, label in accounts.choices():
            self.preset_combo.addItem(label, name)
        self.preset_combo.currentIndexChanged.connect(self._preset_changed)
        form.addRow("Provider", self.preset_combo)

        self.email_edit = QLineEdit()
        self.email_edit.setPlaceholderText("you@example.com")
        self.email_edit.editingFinished.connect(self._address_entered)
        form.addRow("Email address", self.email_edit)

        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        reveal = QToolButton()
        reveal.setText("Show")
        reveal.setCheckable(True)
        reveal.toggled.connect(
            lambda on: self.password_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password
            )
        )
        self.get_password_button = QToolButton()
        self.get_password_button.setText("Get one…")
        self.get_password_button.clicked.connect(self._open_password_page)
        password_row = QHBoxLayout()
        password_row.addWidget(self.password_edit, 1)
        password_row.addWidget(reveal)
        password_row.addWidget(self.get_password_button)
        self.password_label = QLabel("App password")
        form.addRow(self.password_label, password_row)

        self.provider_note = QLabel()
        self.provider_note.setWordWrap(True)
        self.provider_note.setOpenExternalLinks(True)
        self.provider_note.setProperty("dim", "true")
        form.addRow("", self.provider_note)

        self.account_label_edit = QLineEdit()
        self.account_label_edit.setPlaceholderText("shown in the table and menus")
        form.addRow("Name", self.account_label_edit)

        self.advanced_box = QGroupBox("Server")
        self.advanced_box.setCheckable(True)
        self.advanced_box.setChecked(False)
        self.advanced_box.setToolTip(
            "Filled in from the provider. Only worth opening if your provider "
            "uses something unusual."
        )
        advanced = QFormLayout(self.advanced_box)
        self.host_edit = QLineEdit()
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.mailbox_edit = QLineEdit()
        self.mailbox_edit.setPlaceholderText("INBOX")
        self.connections_spin = QSpinBox()
        self.connections_spin.setRange(1, 8)
        advanced.addRow("IMAP host", self.host_edit)
        advanced.addRow("IMAP port", self.port_spin)
        advanced.addRow("Mailbox to scan", self.mailbox_edit)
        advanced.addRow("Parallel connections", self.connections_spin)
        # Folders per mailbox, because people keep separate mailboxes for
        # separate reasons - a work account where a folder called Job Search
        # would be conspicuous, a personal one where it does not matter.
        self.account_root_edit = QLineEdit()
        self.account_root_edit.setToolTip(
            "Where this mailbox's job-search folders go. Leave empty to use "
            "the one on the Folders tab.")
        self.account_other_root_edit = QLineEdit()
        self.account_other_root_edit.setToolTip(
            "Where this mailbox's sorted non-job mail goes. Leave empty to "
            "use the one on the Folders tab.")
        advanced.addRow("Job Search folder", self.account_root_edit)
        advanced.addRow("Sorted mail folder", self.account_other_root_edit)
        form.addRow(self.advanced_box)

        self.fetch_kb_spin = QSpinBox()
        self.fetch_kb_spin.setRange(8, 4096)
        self.fetch_kb_spin.setSuffix(" KB")
        self.fetch_kb_spin.setToolTip(
            "How much of each message to download. Enough for the text; the "
            "rest is attachments, which are never read."
        )
        form.addRow("Download per message", self.fetch_kb_spin)
        outer.addLayout(form)

        # Every field writes into the selected mailbox as it changes, so the
        # list above is always showing the truth and there is nothing to press.
        for widget in (self.email_edit, self.account_label_edit, self.host_edit,
                       self.mailbox_edit, self.account_root_edit,
                       self.account_other_root_edit):
            widget.textChanged.connect(self._capture_account)
        for widget in (self.port_spin, self.connections_spin):
            widget.valueChanged.connect(self._capture_account)
        self.password_edit.textChanged.connect(self._capture_account)

        note = QLabel(
            "Passwords go into the macOS Keychain, never into a file. Every "
            "provider here wants an app password rather than the one you sign "
            "in with, which is a good thing: it can be revoked on its own."
        )
        note.setWordWrap(True)
        note.setProperty("dim", "true")
        outer.addWidget(note)
        outer.addStretch(1)
        return page

    # -- the mailbox list -------------------------------------------------
    def _load_accounts(self, settings: Settings) -> None:
        """Take a working copy of the mailbox list, editable until OK."""
        self._accounts: List[Account] = [replace(a) for a in settings.accounts]
        if not self._accounts and settings.icloud_email:
            self._accounts = [replace(settings.primary_account)]
        self._account_index = 0 if self._accounts else -1
        self._refresh_account_list()

    def _account_status(self, account: Account) -> str:
        """What still needs doing to this mailbox, in a few words."""
        if not account.address:
            return "no address yet"
        if not accounts.valid_address(account.address):
            return "that address does not look right"
        if not account.host:
            return "no server - choose a provider, or type a host"
        if not self._password_for(account):
            return "no password yet"
        return "ready" if account.enabled else "ready, not scanned"

    def _password_for(self, account: Account) -> str:
        if account.address in self._account_passwords:
            return self._account_passwords[account.address]
        try:
            return self._store.get_mailbox_password(account.address)
        except CredentialError:
            return ""

    def _refresh_account_list(self) -> None:
        self.account_list.blockSignals(True)
        self.account_list.clear()
        for account in self._accounts:
            name = account.describe() if account.address else "New mailbox"
            item = QListWidgetItem(f"{name}   —   {self._account_status(account)}")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if account.enabled else Qt.CheckState.Unchecked)
            item.setToolTip(
                f"{account.address or 'No address yet'}\n"
                f"{account.host or 'No server'}:{account.port}\n"
                f"{self._account_status(account)}"
            )
            self.account_list.addItem(item)
        if self._accounts:
            self.account_list.setCurrentRow(
                min(max(self._account_index, 0), len(self._accounts) - 1))
        self.account_list.blockSignals(False)

        ready = sum(1 for a in self._accounts if self._account_status(a).startswith("ready"))
        total = len(self._accounts)
        self.account_summary.setText(
            f"{total} mailbox{'es' if total != 1 else ''}, {ready} ready to scan."
            + ("" if ready == total else
               " Anything not ready is listed above with what it still needs.")
        )
        self.remove_account_button.setEnabled(bool(self._accounts))
        self.test_imap_button.setEnabled(bool(self._accounts))
        for widget in (self.preset_combo, self.email_edit, self.password_edit,
                       self.account_label_edit, self.advanced_box):
            widget.setEnabled(bool(self._accounts))
        if self._accounts:
            self._show_account(min(max(self._account_index, 0), len(self._accounts) - 1))

    def _account_ticked(self, item: QListWidgetItem) -> None:
        row = self.account_list.row(item)
        if 0 <= row < len(self._accounts):
            self._accounts[row].enabled = item.checkState() == Qt.CheckState.Checked
            self._refresh_account_list()

    def _show_account(self, index: int) -> None:
        if not (0 <= index < len(self._accounts)):
            return
        account = self._accounts[index]
        self._account_index = index
        editors = (self.preset_combo, self.email_edit, self.host_edit,
                   self.port_spin, self.mailbox_edit, self.connections_spin,
                   self.account_label_edit, self.password_edit,
                   self.account_root_edit, self.account_other_root_edit)
        for widget in editors:
            widget.blockSignals(True)
        self.preset_combo.setCurrentIndex(
            max(0, self.preset_combo.findData(account.preset)))
        self.email_edit.setText(account.address)
        self.host_edit.setText(account.host)
        self.port_spin.setValue(account.port)
        self.mailbox_edit.setText(account.source_mailbox)
        self.connections_spin.setValue(account.connections)
        self.account_label_edit.setText(account.label)
        self.password_edit.setText(self._password_for(account))
        self.account_root_edit.setText(account.folder_root)
        self.account_other_root_edit.setText(account.other_folder_root)
        for widget in editors:
            widget.blockSignals(False)
        # Open the server box on its own when it holds something unexpected.
        spec = accounts.host_for(account.preset)
        self.advanced_box.setChecked(
            bool(account.host) and account.host != spec.host)
        self._describe_preset(account.preset)

    def _capture_account(self) -> None:
        """Fold the form back into the mailbox it belongs to.

        Called on every keystroke rather than only when the selection moves, so
        the list is always describing what has actually been entered.
        """
        if not (0 <= self._account_index < len(self._accounts)):
            return
        account = self._accounts[self._account_index]
        address = self.email_edit.text().strip()
        account.preset = self.preset_combo.currentData() or "custom"
        account.address = address
        account.host = self.host_edit.text().strip()
        account.port = self.port_spin.value()
        account.source_mailbox = self.mailbox_edit.text().strip() or "INBOX"
        account.connections = self.connections_spin.value()
        account.label = self.account_label_edit.text().strip()
        account.folder_root = self.account_root_edit.text().strip()
        account.other_folder_root = self.account_other_root_edit.text().strip()
        if address:
            # Cleaned on the way in as well as on the way out, so what is shown,
            # what is stored and what is sent are all the same thing.
            self._account_passwords[address] = clean_secret(self.password_edit.text())
        self._refresh_list_row(self._account_index)

    def _refresh_list_row(self, index: int) -> None:
        """Update one row in place, without rebuilding and stealing focus."""
        item = self.account_list.item(index)
        if item is None or not (0 <= index < len(self._accounts)):
            return
        account = self._accounts[index]
        name = account.describe() if account.address else "New mailbox"
        self.account_list.blockSignals(True)
        item.setText(f"{name}   —   {self._account_status(account)}")
        self.account_list.blockSignals(False)
        ready = sum(1 for a in self._accounts
                    if self._account_status(a).startswith("ready"))
        total = len(self._accounts)
        self.account_summary.setText(
            f"{total} mailbox{'es' if total != 1 else ''}, {ready} ready to scan."
        )

    def _account_selected(self, index: int) -> None:
        if index < 0 or index == self._account_index:
            return
        self._show_account(index)

    def _add_account(self) -> None:
        """Start a new mailbox, and put the cursor where typing should begin."""
        self._accounts.append(Account())
        self._account_index = len(self._accounts) - 1
        self._refresh_account_list()
        self.account_list.setCurrentRow(self._account_index)
        self.email_edit.setFocus()
        self.status.setText(
            "New mailbox: choose a provider and type its address. It is kept "
            "when you press OK."
        )

    def _remove_account(self) -> None:
        if not (0 <= self._account_index < len(self._accounts)):
            return
        going = self._accounts[self._account_index]
        if going.address and QMessageBox.question(
            self, "Remove mailbox",
            f"Stop scanning {going.address}?\n\nNothing in the mailbox is "
            "touched. Its password is removed from the Keychain.",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return
        self._removed_accounts.append(going)
        del self._accounts[self._account_index]
        self._account_index = max(0, self._account_index - 1)
        self._refresh_account_list()

    def _preset_changed(self) -> None:
        """Follow the provider, the way the model page follows the backend.

        Changing the provider on a mailbox that already holds somebody else's
        address is not a change of server, it is a different mailbox. Keeping
        the old address is how an iCloud address ends up pointed at Gmail's
        server, which then quietly scans nothing.
        """
        name = self.preset_combo.currentData() or "custom"
        spec = accounts.host_for(name)
        if spec.host:
            self.host_edit.setText(spec.host)
            self.port_spin.setValue(spec.port)

        address = self.email_edit.text().strip()
        belongs_to = accounts.host_for_address(address) if address else None
        if address and belongs_to is not None and not belongs_to.is_custom \
                and belongs_to.name != name:
            self.email_edit.clear()
            self.password_edit.clear()
            self.account_label_edit.clear()
            self.status.setText(
                f"{_html(address)} is {belongs_to.label}, so it has been "
                f"cleared. Enter the {spec.label} address for this mailbox."
            )
            address = ""

        self.email_edit.setPlaceholderText(
            f"you@{spec.domains[0]}" if spec.domains else "you@example.com")
        placeholder = {label for _n, label in accounts.choices()}
        if self.account_label_edit.text().strip() in placeholder:
            self.account_label_edit.setText(address.split("@")[0] if address else "")
        self._describe_preset(name)
        self._capture_account()

    def _address_entered(self) -> None:
        """Fill in the server from the domain, unless it is already set."""
        address = self.email_edit.text().strip()
        if not address:
            return
        guessed = accounts.host_for_address(address)
        current = self.preset_combo.currentData() or "custom"
        if current == "custom" and not guessed.is_custom:
            self.preset_combo.setCurrentIndex(
                max(0, self.preset_combo.findData(guessed.name)))
        current_label = self.account_label_edit.text().strip()
        placeholder = {label for _name, label in accounts.choices()}
        if not current_label or current_label in placeholder:
            self.account_label_edit.setText(address.split("@")[0])
        self._capture_account()

    def _describe_preset(self, name: str) -> None:
        spec = accounts.host_for(name)
        self.password_label.setText(spec.secret_label)
        self.get_password_button.setVisible(bool(spec.help_url))
        self.get_password_button.setToolTip(
            f"Open {spec.help_url}" if spec.help_url else "")
        self.password_edit.setPlaceholderText(spec.secret_label.lower())
        parts = []
        if spec.note:
            parts.append(_html(spec.note))
        if spec.help_url:
            parts.append(f"<a href='{spec.help_url}'>{_html(spec.help_url)}</a>")
        self.provider_note.setText("<br>".join(parts))
        self.provider_note.setVisible(bool(parts))

    def _open_password_page(self) -> None:
        spec = accounts.host_for(self.preset_combo.currentData() or "custom")
        if spec.help_url:
            QDesktopServices.openUrl(QUrl(spec.help_url))

    def _build_ai_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.provider_combo = QComboBox()
        for name, label, _blurb in providers.provider_choices():
            self.provider_combo.addItem(label, name)
        self.provider_combo.currentIndexChanged.connect(self._provider_changed)

        self.provider_blurb = QLabel()
        self.provider_blurb.setWordWrap(True)
        self.provider_blurb.setProperty("dim", "true")

        # Shown only for the on-device backend, and only when it needs setting
        # up. The equivalent of "get one" beside an API key field.
        self.ollama_note = QLabel()
        self.ollama_note.setWordWrap(True)
        self.ollama_note.setOpenExternalLinks(True)
        self.ollama_note.setVisible(False)
        self.ollama_button = QPushButton("Install Ollama")
        self.ollama_button.setVisible(False)
        # The same shape the window uses for a scan: a bar that means
        # something, a line saying what is happening, and one red way to stop.
        self.ollama_progress = QProgressBar()
        self.ollama_progress.setRange(0, 100)
        self.ollama_progress.setTextVisible(True)
        self.ollama_progress.setVisible(False)
        self.ollama_step_label = QLabel("")
        self.ollama_step_label.setWordWrap(True)
        self.ollama_step_label.setProperty("dim", "true")
        self.ollama_step_label.setVisible(False)
        self.ollama_stop = QPushButton("Stop")
        self.ollama_stop.setVisible(False)
        _paint_button(self.ollama_stop, "danger")
        self.ollama_stop.clicked.connect(self._stop_ollama_step)
        self._ollama_worker = None
        self._ollama_probe = None
        self._ollama_state = None
        self._keychain_worker = None
        self.ollama_button.clicked.connect(self._do_ollama_step)

        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setMinimumWidth(280)
        self.model_combo.currentIndexChanged.connect(self._model_changed)
        self.model_note = QLabel()
        self.model_note.setWordWrap(True)
        self.model_note.setStyleSheet("opacity:0.8")

        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        reveal_key = QToolButton()
        reveal_key.setText("Show")
        reveal_key.setCheckable(True)
        reveal_key.toggled.connect(
            lambda on: self.api_key_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password
            )
        )
        self.key_link = QLabel()
        self.key_link.setOpenExternalLinks(True)
        key_row = QHBoxLayout()
        key_row.addWidget(self.api_key_edit, 1)
        key_row.addWidget(reveal_key)
        key_row.addWidget(self.key_link)
        self.key_row_widget = QWidget()
        self.key_row_widget.setLayout(key_row)
        key_row.setContentsMargins(0, 0, 0, 0)
        self.key_label = QLabel("API key")

        self.base_url_edit = QLineEdit()
        self.base_url_edit.setPlaceholderText("leave blank for the default endpoint")
        self.base_url_label = QLabel("Endpoint")

        self.test_model_button = QPushButton("Test this model")
        self.test_model_button.clicked.connect(lambda: self._run_test("claude"))
        _paint_button(self.test_model_button, "primary")
        self.refresh_models_button = QPushButton("Refresh model list")
        self.refresh_models_button.setToolTip(
            "Ask the service which models it currently serves. Providers retire "
            "model ids without warning, so a built-in list goes stale."
        )
        self.refresh_models_button.clicked.connect(self._refresh_models)
        model_test_row = QHBoxLayout()
        model_test_row.addWidget(self.test_model_button)
        model_test_row.addWidget(self.refresh_models_button)
        model_test_row.addStretch(1)
        model_test_widget = QWidget()
        model_test_widget.setLayout(model_test_row)
        model_test_row.setContentsMargins(0, 0, 0, 0)

        self.effort_combo = QComboBox()
        self.effort_combo.addItems(list(EFFORT_LEVELS))
        self.effort_label = QLabel("Reasoning effort")

        # A probability on a two-decimal spinner reads as a number to be
        # nudged. It is really "how sure before this files itself", which is a
        # position on a range, so it is one - with the figure spelled out and
        # what it means underneath.
        self.threshold_slider = QSlider(Qt.Orientation.Horizontal)
        self.threshold_slider.setRange(50, 100)
        self.threshold_slider.setSingleStep(1)
        self.threshold_slider.setPageStep(5)
        self.threshold_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.threshold_slider.setTickInterval(10)
        self.threshold_slider.setMinimumWidth(200)
        self.threshold_slider.setToolTip(
            "Messages the sorter is less sure about than this are held for you "
            "to look at, and are never pre-ticked."
        )
        self.threshold_value = QLabel()
        self.threshold_value.setMinimumWidth(52)
        self.threshold_value.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.threshold_row = QWidget()
        threshold_layout = QHBoxLayout(self.threshold_row)
        threshold_layout.setContentsMargins(0, 0, 0, 0)
        threshold_layout.addWidget(self.threshold_slider, 1)
        threshold_layout.addWidget(self.threshold_value)
        self.threshold_note = QLabel()
        self.threshold_note.setWordWrap(True)
        self.threshold_note.setProperty("dim", "true")
        self.threshold_slider.valueChanged.connect(self._threshold_changed)

        self.concurrency_spin = QSpinBox()
        self.concurrency_spin.setRange(1, 16)
        self.concurrency_spin.setToolTip("How many requests to run in parallel.")

        self.batch_spin = QSpinBox()
        self.batch_spin.setRange(1, 25)
        self.batch_spin.setToolTip(
            "Emails per request. The instructions are ~2,800 tokens and are re-sent "
            "with every request, so batching is the single biggest saving available. "
            "Larger batches are cheaper but give each email less attention. "
            "Set to 1 to send one email per request."
        )

        self.body_chars_spin = QSpinBox()
        self.body_chars_spin.setRange(1000, 200000)
        self.body_chars_spin.setSingleStep(1000)
        self.body_chars_spin.setGroupSeparatorShown(True)
        self.body_chars_spin.setToolTip(
            "Longer bodies are trimmed at a paragraph boundary. The model is told when this "
            "happens and lowers its confidence accordingly."
        )

        self.max_messages_spin = QSpinBox()
        self.max_messages_spin.setRange(1, 5000)
        self.max_messages_spin.setSingleStep(50)
        self.max_messages_spin.setGroupSeparatorShown(True)

        self.fallback_check = QCheckBox(
            "If the backend is unreachable, classify locally with the built-in rules"
        )
        self.fallback_check.setToolTip(
            "Keeps a scan useful when the network, the key or the quota fails. "
            "Rows classified this way say so, and are held to the same confidence bar."
        )

        form.addRow("Model backend", self.provider_combo)
        form.addRow("", self.provider_blurb)
        form.addRow("", self.ollama_note)
        ollama_row = QHBoxLayout()
        ollama_row.addWidget(self.ollama_button)
        ollama_row.addWidget(self.ollama_stop)
        self.manage_models_button = QPushButton("Manage models…")
        self.manage_models_button.setToolTip(
            "See what is installed on this Mac, download another, remove one.")
        self.manage_models_button.setVisible(False)
        self.manage_models_button.clicked.connect(self._open_models)
        ollama_row.addWidget(self.manage_models_button)
        ollama_row.addStretch(1)
        form.addRow("", ollama_row)
        form.addRow("", self.ollama_progress)
        form.addRow("", self.ollama_step_label)
        form.addRow("Model", self.model_combo)
        form.addRow("", self.model_note)
        form.addRow(self.key_label, self.key_row_widget)
        form.addRow(self.base_url_label, self.base_url_edit)
        form.addRow("", model_test_widget)
        form.addRow(_separator())
        form.addRow(self.effort_label, self.effort_combo)
        form.addRow("File it without asking", self.threshold_row)
        form.addRow("", self.threshold_note)
        form.addRow("Emails per request", self.batch_spin)
        form.addRow("Parallel requests", self.concurrency_spin)
        form.addRow("Max characters per email", self.body_chars_spin)
        form.addRow("Max messages per scan", self.max_messages_spin)
        form.addRow("", self.fallback_check)

        note = QLabel(
            "The confidence threshold does the real safety work here, not the model: "
            "anything the backend is unsure about goes to Needs Review whichever one you "
            "pick. A small local model is a perfectly reasonable choice - it will simply "
            "send more mail to Needs Review."
        )
        note.setWordWrap(True)
        form.addRow(note)
        return page

    # -- the on-device backend needs software, not a key -------------------
    def _refresh_ollama_panel(self, spec) -> None:
        """Say what is missing for the on-device backend, and offer to fix it."""
        if not hasattr(self, "ollama_note"):
            return
        on_device = bool(getattr(spec, "on_device", False))
        # Decided here and nowhere else. It used to be turned on further down,
        # past two early returns, so a backend whose probe had not answered
        # yet could leave it hidden with Ollama selected.
        self.manage_models_button.setVisible(on_device)
        if not on_device:
            self.ollama_note.setVisible(False)
            self.ollama_button.setVisible(False)
            return

        # The network half of this is asked for in the background: an
        # endpoint that drops packets costs the full timeout, and the endpoint
        # is a field somebody can type anything into.
        self._start_ollama_probe()
        state = getattr(self, "_ollama_state", None)
        self.ollama_note.setVisible(True)
        if state is None:
            self.ollama_note.setText("Checking what is installed…")
            self.ollama_button.setVisible(False)
            return
        step = state.next_step()

        if not step:
            self.ollama_note.setText(
                f"{_html(state.describe())} Installed models: "
                f"{_html(', '.join(state.models[:6]))}"
            )
            self.ollama_button.setVisible(False)
            return

        if step == "install":
            command = ondevice.install_command()
            if command:
                self.ollama_note.setText(
                    "Ollama is not installed. It runs a model on this Mac, so "
                    "nothing leaves it and there is nothing to pay for. "
                    "Homebrew is available, so this can install it for you."
                )
                self.ollama_button.setText("Install Ollama")
            else:
                self.ollama_note.setText(
                    "Ollama is not installed. It runs a model on this Mac, so "
                    "nothing leaves it and there is nothing to pay for. "
                    f"Download it from <a href='{ondevice.DOWNLOAD_URL}'>"
                    f"{ondevice.DOWNLOAD_URL}</a>, then come back here."
                )
                self.ollama_button.setText("Open the download page")
        elif step == "start":
            self.ollama_note.setText(
                "Ollama is installed but its server is not answering on "
                f"{_html(self.base_url_edit.text().strip() or ondevice.DEFAULT_ENDPOINT)}."
            )
            self.ollama_button.setText("Start Ollama")
        else:
            model = self._chosen_model() or "llama3.2:3b"
            self.ollama_note.setText(
                f"Ollama is running but has no models. {_html(model)} needs to be "
                "downloaded once, which is a couple of gigabytes."
            )
            self.ollama_button.setText(f"Download {model}")
        self.ollama_button.setVisible(True)

    def _open_models(self) -> None:
        """What is installed, and how to add to it or remove from it."""
        dialog = ModelsDialog(self.base_url_edit.text().strip(), self)
        dialog.exec()
        # Whatever was added or removed changes what the model list should say.
        self._recheck_ollama()
        self._refresh_installed_models()

    def _refresh_installed_models(self) -> None:
        """Offer the models this Mac actually has, for a local backend."""
        spec = providers.provider_class(
            self.provider_combo.currentData() or providers.DEFAULT_PROVIDER)
        if not getattr(spec, "on_device", False):
            return
        state = getattr(self, "_ollama_state", None)
        if state is None or not state.models:
            return
        wanted = self._chosen_model()
        self._loading_models = True
        known = {self.model_combo.itemData(i)
                 for i in range(self.model_combo.count())}
        for name in state.models:
            if name not in known:
                self.model_combo.addItem(f"{name} — installed", name)
        self._loading_models = False
        index = self.model_combo.findData(wanted)
        if index >= 0:
            self.model_combo.setCurrentIndex(index)

    def _do_ollama_step(self) -> None:
        """Run whichever step the panel is offering, off the UI thread."""
        if self._ollama_worker is not None:
            return
        state = getattr(self, "_ollama_state", None) or ondevice.status()
        step = state.next_step()
        if step == "install" and not ondevice.install_command():
            QDesktopServices.openUrl(QUrl(ondevice.DOWNLOAD_URL))
            return

        command = {
            "install": ondevice.install_command,
            "start": ondevice.start_command,
        }.get(step, lambda: ondevice.pull_command(self._chosen_model() or "llama3.2:3b"))()
        if not command:
            self.status.setText("Nothing to run for that step.")
            return

        if step == "start":
            self._begin_ollama_start(command)
            return

        alternatives = ondevice.install_commands() if step == "install" else ()
        self._begin_ollama_step(step, command, alternatives)

    def _begin_ollama_start(self, command) -> None:
        """Start the server and wait for it to answer, rather than assuming."""
        from workers import OnDeviceStartWorker

        worker = OnDeviceStartWorker(
            command, self.base_url_edit.text().strip(), parent=self)
        self._ollama_worker = worker
        self.ollama_button.setEnabled(False)
        self.ollama_stop.setVisible(True)
        self.ollama_progress.setVisible(True)
        self.ollama_progress.setRange(0, 0)          # it cannot say how long
        self.ollama_step_label.setVisible(True)
        self.ollama_step_label.setText("Starting Ollama…")
        self.status.setText("Starting Ollama…")
        worker.progress.connect(self._on_ollama_progress)
        worker.log_message.connect(self._on_ollama_log)
        worker.failed.connect(self._on_ollama_failed)
        worker.finished_ok.connect(self._on_ollama_done)
        worker.finished.connect(self._clear_ollama_worker)
        worker.start()

    def _begin_ollama_step(self, step: str, command, alternatives=()) -> None:
        """Hand the command to a thread and show it working."""
        from workers import OnDeviceWorker

        worker = OnDeviceWorker(step, command, alternatives, parent=self)
        self._ollama_worker = worker
        self.ollama_button.setEnabled(False)
        self.ollama_stop.setVisible(True)
        self.ollama_progress.setVisible(True)
        self.ollama_progress.setValue(0)
        self.ollama_step_label.setVisible(True)
        self.ollama_step_label.setText("Starting…")
        self.status.setText(
            "Installing Ollama…" if step == "install"
            else "Downloading the model… this is a couple of gigabytes.")

        worker.progress.connect(self._on_ollama_progress)
        worker.log_message.connect(self._on_ollama_log)
        worker.failed.connect(self._on_ollama_failed)
        worker.finished_ok.connect(self._on_ollama_done)
        worker.finished.connect(self._clear_ollama_worker)
        worker.start()

    @Slot(int, int, str)
    def _on_ollama_progress(self, done: int, total: int, message: str) -> None:
        if total:
            self.ollama_progress.setRange(0, total)
            self.ollama_progress.setValue(max(0, min(total, done)))
        else:                                   # nothing to go on: busy bar
            self.ollama_progress.setRange(0, 0)
        if message:
            self.ollama_step_label.setText(message[:120])

    @Slot(str)
    def _on_ollama_log(self, message: str) -> None:
        window = self.parent()
        if hasattr(window, "_append_log"):
            window._append_log(message)

    @Slot(str, str)
    def _on_ollama_failed(self, title: str, detail: str) -> None:
        """A worker that raised rather than finishing. Never silent."""
        self._finish_ollama_ui()
        self.ollama_step_label.setText(f"{title}: {detail}"[:160])
        self.status.setText(f"{title}: {detail}"[:200])
        self._recheck_ollama()

    @Slot(object)
    def _on_ollama_done(self, result) -> None:
        self._finish_ollama_ui()
        self.status.setText(result.describe())
        self.ollama_step_label.setText(result.describe()[:160])
        if not result.ok and not result.cancelled:
            QMessageBox.warning(
                self, "On-device setup",
                result.describe() + "\n\n"
                + "\n".join(result.lines[-6:])[:800])
        self._recheck_ollama()

    def _finish_ollama_ui(self) -> None:
        self.ollama_progress.setRange(0, 100)
        self.ollama_progress.setVisible(False)
        self.ollama_stop.setVisible(False)
        self.ollama_button.setEnabled(True)

    def _clear_ollama_worker(self) -> None:
        self._ollama_worker = None

    def _stop_ollama_step(self) -> None:
        worker = self._ollama_worker
        if worker is None:
            return
        self.ollama_stop.setEnabled(False)
        self.ollama_step_label.setText("Stopping…")
        worker.cancel()

    def _start_ollama_probe(self) -> None:
        """Ask the server what it has, without making anybody wait for it."""
        if getattr(self, "_ollama_probe", None) is not None:
            return
        from workers import OnDeviceProbeWorker

        probe = OnDeviceProbeWorker(
            self.base_url_edit.text().strip() or ondevice.DEFAULT_ENDPOINT,
            parent=self)
        self._ollama_probe = probe
        probe.finished_ok.connect(self._on_ollama_probed)
        probe.finished.connect(self._clear_ollama_probe)
        probe.start()

    @Slot(object)
    def _on_ollama_probed(self, state) -> None:
        previous = getattr(self, "_ollama_state", None)
        self._ollama_state = state
        # Only redraw when something changed, so a probe every few seconds
        # does not fight with somebody reading the panel.
        if previous is None or previous.next_step() != state.next_step() \
                or previous.models != state.models:
            self._paint_ollama_panel()

    def _clear_ollama_probe(self) -> None:
        self._ollama_probe = None

    def _paint_ollama_panel(self) -> None:
        name = self.provider_combo.currentData() or providers.DEFAULT_PROVIDER
        spec = providers.provider_class(name)
        if getattr(spec, "on_device", False):
            self._refresh_ollama_panel(spec)

    def _recheck_ollama(self) -> None:
        """Re-read the panel for whichever backend is selected."""
        name = self.provider_combo.currentData() or providers.DEFAULT_PROVIDER
        self._refresh_ollama_panel(providers.provider_class(name))

    def _provider_changed(self) -> None:
        """Repopulate the model list and show only the fields this backend uses."""
        name = self.provider_combo.currentData() or providers.DEFAULT_PROVIDER
        spec = providers.provider_class(name)

        self.provider_blurb.setText(spec.blurb)

        previous = self.model_combo.currentText().strip()
        self._loading_models = True
        self.model_combo.clear()
        for choice in spec.models:
            self.model_combo.addItem(choice.label, choice.value)
        # Typing a name is right for a hosted backend - they release models
        # faster than any bundled list can follow, and today's list going
        # stale is a real thing that happened. It is wrong for a local one:
        # there the valid names are exactly the models on this Mac, that set
        # is knowable, and a typo becomes a scan that fails on every message.
        self.model_combo.setEditable(not getattr(spec, "on_device", False))
        self._loading_models = False

        stored = self._settings.model if self._settings.provider == name else ""
        wanted = stored or (previous if self._provider_seen == name else "") or spec.default_model
        index = self.model_combo.findData(wanted)
        if index >= 0:
            self.model_combo.setCurrentIndex(index)
        elif self.model_combo.isEditable():
            self.model_combo.setEditText(wanted)
        else:
            # A local model that is not in the bundled list but is installed:
            # keep it, so choosing it once does not lose it on the next open.
            self.model_combo.addItem(wanted, wanted)
            self.model_combo.setCurrentIndex(self.model_combo.count() - 1)
        self._provider_seen = name
        self._model_changed()

        needs_key = bool(spec.needs_api_key)
        self.key_label.setVisible(needs_key)
        self.key_row_widget.setVisible(needs_key)
        # A short label keeps the form's label column narrow; the backend is
        # already named in the dropdown directly above.
        self.key_label.setText("API key")
        self.api_key_edit.setPlaceholderText(spec.key_hint or "API key")
        self.key_link.setText(
            f"<a href='{spec.key_url}'>get one</a>" if spec.key_url else ""
        )
        if needs_key:
            try:
                self.api_key_edit.setText(self._store.get_provider_key(name))
            except CredentialError:
                self.api_key_edit.clear()
        else:
            self.api_key_edit.clear()

        self._refresh_ollama_panel(spec)

        self.base_url_label.setVisible(bool(spec.supports_base_url))
        self.base_url_edit.setVisible(bool(spec.supports_base_url))
        if spec.on_device:
            self.base_url_edit.setPlaceholderText("http://localhost:11434")
        else:
            self.base_url_edit.setPlaceholderText("leave blank for the default endpoint")

        # Reasoning effort is an Anthropic concept; hide it elsewhere.
        anthropic_selected = name == providers.AnthropicProvider.name
        self.effort_label.setVisible(anthropic_selected)
        self.effort_combo.setVisible(anthropic_selected)

        is_rules = name == providers.FALLBACK_PROVIDER
        self.batch_spin.setEnabled(not spec.on_device)
        self.batch_spin.setToolTip(
            "Not used: local backends gain nothing from batching, and small local "
            "models handle it badly."
            if spec.on_device else
            "Emails per request. The instructions are ~2,800 tokens and are re-sent "
            "with every request, so batching is the single biggest saving available."
        )
        self.fallback_check.setEnabled(not is_rules)
        if is_rules:
            self.fallback_check.setToolTip(
                "Not applicable: the rules engine is itself the fallback."
            )
        self.test_model_button.setText(
            "Check Ollama is running" if spec.on_device and not is_rules
            else ("Try the rule set" if is_rules else "Test this model")
        )
        self.refresh_models_button.setVisible(bool(spec.can_list_models))

    def _chosen_model(self) -> str:
        """The selected model id, or whatever the user typed instead.

        ``currentData()`` still points at the last *selected* item after the
        user types a name of their own, so the visible text has to be checked
        against that item's label before the id can be trusted.
        """
        text = self.model_combo.currentText().strip()
        index = self.model_combo.currentIndex()
        if index >= 0 and text == self.model_combo.itemText(index):
            return str(self.model_combo.itemData(index) or text)
        return text

    @Slot()
    def _refresh_models(self) -> None:
        """Replace the dropdown with what the service actually offers."""
        settings = self.collect()
        spec = providers.provider_class(settings.provider)
        if not spec.can_list_models:
            self.status.setText(f"{spec.label} does not publish a model list.")
            return
        self.refresh_models_button.setEnabled(False)
        self.status.setText("Asking the service for its model list…")
        QApplication.processEvents()
        provider = None
        try:
            provider = providers.build_provider(
                settings.provider,
                api_key=self.api_key_edit.text().strip(),
                model=settings.model,
                base_url=settings.base_url,
                timeout=30.0,
            )
            found = provider.list_models()
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            self.status.setText(
                f"<span style='color:{ACCENT_RED}'>Could not fetch the model list: "
                f"{_html(str(exc)[:300])}</span>"
            )
            return
        finally:
            self.refresh_models_button.setEnabled(True)
            if provider is not None:
                provider.close()

        if not found:
            self.status.setText("The service returned no usable models.")
            return

        wanted = self._chosen_model()
        known = {choice.value: choice for choice in spec.models}
        self._loading_models = True
        self.model_combo.clear()
        for value in found:
            choice = known.get(value)
            self.model_combo.addItem(choice.label if choice else value, value)
        self._loading_models = False
        index = self.model_combo.findData(wanted)
        self.model_combo.setCurrentIndex(index if index >= 0 else 0)
        self._model_changed()
        self.status.setText(
            f"<span style='color:{ACCENT_GREEN}'>{spec.label} currently serves "
            f"{len(found)} model(s); the list above is now live.</span>"
        )

    def _model_changed(self) -> None:
        if getattr(self, "_loading_models", False):
            return
        name = self.provider_combo.currentData() or providers.DEFAULT_PROVIDER
        spec = providers.provider_class(name)
        value = self._chosen_model()
        note = next((c.note for c in spec.models if c.value == value), "")
        rate = spec.pricing.get(value)
        if spec.on_device:
            price = "free, and nothing leaves this Mac"
        elif rate:
            price = f"about ${rate[0]:.2f} in / ${rate[1]:.2f} out per million tokens"
        else:
            price = ""
        self.model_note.setText(" · ".join(part for part in (note, price) if part))

    def _build_reply_tab(self) -> QWidget:
        """Rules that act on matching mail. Nothing here ever sends anything."""
        page = QWidget()
        outer = QVBoxLayout(page)

        headline = QLabel(
            "<b>Replies are drafted, never sent.</b> A rule can write a reply "
            "into your Drafts mailbox, file a message, tick it, flag it or mark "
            "it read — but nothing leaves your account without you pressing send "
            "in your mail app."
        )
        headline.setWordWrap(True)
        outer.addWidget(headline)

        top = QHBoxLayout()
        # Two switches, because they are two different promises. A rule that
        # only files and ticks has touched nobody's mailbox and runs at the
        # end of every scan; one that writes a draft waits to be asked.
        self.sorting_rules_check = QCheckBox("Apply filing rules after a scan")
        self.sorting_rules_check.setToolTip(
            "Rules whose only actions are filing, ticking or leaving a "
            "message alone. They change nothing on the server, so they run "
            "with every scan.")
        top.addWidget(self.sorting_rules_check)
        self.auto_reply_check = QCheckBox("Draft replies after a scan")
        self.auto_reply_check.setToolTip(
            "Rules that write a reply into your Drafts mailbox. Nothing is "
            "ever sent.")
        top.addWidget(self.auto_reply_check)
        top.addStretch(1)
        top.addWidget(QLabel("Sign as"))
        self.signature_edit = AdaptiveLineEdit(
            "the name to sign off with", "your name", "name")
        self.signature_edit.setMinimumWidth(120)
        self.signature_edit.setMaximumWidth(200)
        top.addWidget(self.signature_edit)
        outer.addLayout(top)
        outer.addWidget(_separator())

        body = QHBoxLayout()
        body.setSpacing(10)
        outer.addLayout(body, 1)

        # -- left: the rules, in the order they run ------------------------
        left = QVBoxLayout()
        left.setSpacing(4)
        order_note = QLabel(
            "Rules run top to bottom, after the sorter and after anything "
            "learned from your corrections — so a rule always wins.")
        order_note.setProperty("dim", "true")
        order_note.setWordWrap(True)
        left.addWidget(order_note)

        self.rule_list = WrappingList()
        self.rule_list.setMinimumWidth(150)
        self.rule_list.setMaximumWidth(230)
        self.rule_list.setSizePolicy(QSizePolicy.Policy.Preferred,
                                     QSizePolicy.Policy.Expanding)
        # Wrap rather than elide. A rule named for what it does is longer than
        # this column, and half a name is no name at all.
        self.rule_list.currentRowChanged.connect(self._rule_selected)
        self.rule_list.itemChanged.connect(self._rule_ticked)
        left.addWidget(self.rule_list, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(4)
        for text, tip, slot in (
            ("＋", "Add a rule", self._add_rule),
            ("⧉", "Duplicate this rule", self._duplicate_rule),
            ("−", "Remove this rule", self._remove_rule),
            ("↑", "Run this rule earlier", lambda: self._move_rule(-1)),
            ("↓", "Run this rule later", lambda: self._move_rule(1)),
        ):
            button = _compact_button(text, tip, slot)
            buttons.addWidget(button)
            if text == "−":
                self.remove_rule_button = button
        buttons.addStretch(1)
        left.addLayout(buttons)
        body.addLayout(left)

        # -- right: the rule itself ----------------------------------------
        right = QVBoxLayout()
        right.setSpacing(6)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Name"))
        self.rule_name_edit = AdaptiveLineEdit("what this rule is for", "name")
        self.rule_name_edit.setMinimumWidth(120)
        self.rule_name_edit.setSizePolicy(QSizePolicy.Policy.Ignored,
                                          QSizePolicy.Policy.Fixed)
        self.rule_name_edit.editingFinished.connect(self._rule_renamed)
        name_row.addWidget(self.rule_name_edit, 1)
        self.rule_enabled = QCheckBox("On")
        self.rule_enabled.toggled.connect(self._rule_enabled_toggled)
        name_row.addWidget(self.rule_enabled)
        right.addLayout(name_row)

        match_row = QHBoxLayout()
        match_row.addWidget(QLabel("Match"))
        self.rule_match = QComboBox()
        self.rule_match.addItem("all of these conditions", "all")
        self.rule_match.addItem("any of these conditions", "any")
        self.rule_match.currentIndexChanged.connect(self._rule_edited)
        match_row.addWidget(self.rule_match)
        match_row.addStretch(1)
        add_condition = QToolButton()
        add_condition.setText("Add a condition")
        add_condition.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        add_condition.clicked.connect(self._add_condition)
        match_row.addWidget(add_condition)
        right.addLayout(match_row)

        self.conditions_box = QWidget()
        self.conditions_layout = QVBoxLayout(self.conditions_box)
        self.conditions_layout.setContentsMargins(0, 0, 0, 0)
        self.conditions_layout.setSpacing(4)
        right.addWidget(self.conditions_box)

        right.addWidget(_separator())

        action_row = QHBoxLayout()
        action_row.addWidget(QLabel("<b>Then</b>"))
        action_row.addStretch(1)
        add_action = QToolButton()
        add_action.setText("Add an action")
        add_action.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        add_action.clicked.connect(self._add_action)
        action_row.addWidget(add_action)
        right.addLayout(action_row)

        self.actions_box = QWidget()
        self.actions_layout = QVBoxLayout(self.actions_box)
        self.actions_layout.setContentsMargins(0, 0, 0, 0)
        self.actions_layout.setSpacing(4)
        right.addWidget(self.actions_box)

        self.template_note = QLabel(
            "In a template, {first_name}, {sender}, {subject} and {me} are "
            "filled in. Anything in [square brackets] is left for you to "
            "complete and is listed at the bottom of the draft."
        )
        self.template_note.setWordWrap(True)
        self.template_note.setProperty("dim", "true")
        right.addWidget(self.template_note)

        switches = QHBoxLayout()
        self.rule_skip_bulk = QCheckBox("Skip bulk mail")
        self.rule_skip_bulk.setToolTip(
            "Anything carrying an unsubscribe header. Leave this on for rules "
            "that reply: writing back to a mailing list is at best useless and "
            "at worst embarrassing."
        )
        self.rule_skip_bulk.toggled.connect(self._rule_edited)
        switches.addWidget(self.rule_skip_bulk)
        self.rule_stop_after = QCheckBox("Stop here when this matches")
        self.rule_stop_after.setToolTip(
            "Later rules are skipped for that message. Useful for an exception "
            "you put at the top of the list."
        )
        self.rule_stop_after.toggled.connect(self._rule_edited)
        switches.addWidget(self.rule_stop_after)
        switches.addStretch(1)
        right.addLayout(switches)

        self.rule_summary = QLabel("")
        self.rule_summary.setWordWrap(True)
        right.addWidget(self.rule_summary)

        try_row = QHBoxLayout()
        self.try_rule_button = QToolButton()
        self.try_rule_button.setText("Try it on the last scan")
        self.try_rule_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.try_rule_button.clicked.connect(self._try_rule)
        try_row.addWidget(self.try_rule_button)
        self.try_rule_result = QLabel("")
        self.try_rule_result.setWordWrap(True)
        try_row.addWidget(self.try_rule_result, 1)
        right.addLayout(try_row)

        right.addStretch(1)
        body.addLayout(right, 1)
        return page

    # -- reply rules ------------------------------------------------------
    def _load_rules(self, settings: Settings) -> None:
        self._rules = list(settings.rules)
        self._rule_index = 0
        self._condition_rows: List[ConditionRow] = []
        self._action_rows: List[ActionRow] = []
        self.auto_reply_check.setChecked(settings.auto_reply)
        self.sorting_rules_check.setChecked(settings.apply_sorting_rules)
        self.signature_edit.setText(settings.reply_signature)
        self._refresh_rule_list()

    def _refresh_rule_list(self) -> None:
        """Redraw the list on the left without disturbing what is being edited."""
        self.rule_list.blockSignals(True)
        self.rule_list.clear()
        for rule in self._rules:
            entry = QListWidgetItem(self._rule_label(rule))
            entry.setFlags(entry.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            entry.setCheckState(Qt.CheckState.Checked if rule.enabled
                                else Qt.CheckState.Unchecked)
            entry.setToolTip(self._rule_tooltip(rule))
            self.rule_list.addItem(entry)
        self._rule_index = max(0, min(self._rule_index, len(self._rules) - 1))
        self.rule_list.setCurrentRow(self._rule_index)
        self.rule_list.blockSignals(False)
        self.rule_list.measure()
        self.remove_rule_button.setEnabled(len(self._rules) > 1)
        self._show_rule(self._rule_index)

    def _show_rule(self, index: int) -> None:
        if not (0 <= index < len(self._rules)):
            return
        rule = self._rules[index]
        self._rule_index = index
        self._loading_rule = True
        self.rule_enabled.setChecked(rule.enabled)
        self.rule_name_edit.setText(rule.name)
        self.rule_match.setCurrentIndex(max(0, self.rule_match.findData(rule.match)))
        self.rule_skip_bulk.setChecked(rule.skip_bulk)
        self.rule_stop_after.setChecked(rule.stop_after)
        self._rebuild_condition_rows(rule)
        self._rebuild_action_rows(rule)
        self._loading_rule = False
        self.try_rule_result.setText("")
        self._describe_rule()

    def _clear_rows(self, layout, rows: List) -> None:
        for row in rows:
            layout.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
        rows.clear()

    def _rebuild_condition_rows(self, rule) -> None:
        self._clear_rows(self.conditions_layout, self._condition_rows)
        for condition in rule.conditions:
            self._add_condition_row(condition)
        if not rule.conditions:
            self._empty_note(self.conditions_layout,
                             "No conditions yet, so this rule never runs.")

    def _rebuild_action_rows(self, rule) -> None:
        self._clear_rows(self.actions_layout, self._action_rows)
        for action in rule.actions:
            self._add_action_row(action)
        if not rule.actions:
            self._empty_note(self.actions_layout, "No actions yet.")

    def _empty_note(self, layout, text: str) -> None:
        note = QLabel(text)
        note.setProperty("dim", "true")
        layout.addWidget(note)

    def _add_condition_row(self, condition) -> None:
        self._drop_notes(self.conditions_layout)
        row = ConditionRow(condition, mailboxes=self._accounts)
        row.changed.connect(self._rule_edited)
        row.removed.connect(self._remove_condition_row)
        self.conditions_layout.addWidget(row)
        self._condition_rows.append(row)

    def _add_action_row(self, action) -> None:
        self._drop_notes(self.actions_layout)
        row = ActionRow(action, folders=self._folder_choices())
        row.changed.connect(self._rule_edited)
        row.removed.connect(self._remove_action_row)
        self.actions_layout.addWidget(row)
        self._action_rows.append(row)

    def _drop_notes(self, layout) -> None:
        """Take away the “nothing here yet” line once there is something."""
        for index in reversed(range(layout.count())):
            widget = layout.itemAt(index).widget()
            if isinstance(widget, QLabel):
                layout.removeWidget(widget)
                widget.deleteLater()

    def _folder_choices(self) -> List[str]:
        """Folders a rule can file into: whatever this configuration creates.

        Editable, so a folder that is not in this list is still allowed - the
        list is a shortcut, not a fence.
        """
        root = (self.root_edit.text().strip() if hasattr(self, "root_edit")
                else "") or self._settings.folder_root
        other = (self.other_root_edit.text().strip()
                 if hasattr(self, "other_root_edit")
                 else "") or self._settings.other_folder_root
        plan = FolderPlan(root=root, other_root=other)
        choices = list(plan.all_folders)
        choices += [plan.for_other_category(topic) for topic in OtherCategory
                    if topic is not OtherCategory.NOT_APPLICABLE]
        seen: List[str] = []
        for folder in choices:
            if folder and folder not in seen:
                seen.append(folder)
        return seen

    def _remove_condition_row(self, row) -> None:
        if row in self._condition_rows:
            self._condition_rows.remove(row)
            self.conditions_layout.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
        self._rule_edited()
        if not self._condition_rows:
            self._empty_note(self.conditions_layout,
                             "No conditions yet, so this rule never runs.")

    def _remove_action_row(self, row) -> None:
        if row in self._action_rows:
            self._action_rows.remove(row)
            self.actions_layout.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
        self._rule_edited()
        if not self._action_rows:
            self._empty_note(self.actions_layout, "No actions yet.")

    def _add_condition(self) -> None:
        self._capture_rule()
        self._add_condition_row(autoreply.Condition())
        self._rule_edited()

    def _add_action(self) -> None:
        self._capture_rule()
        self._add_action_row(autoreply.Action())
        self._rule_edited()

    def _capture_rule(self) -> None:
        """Read the editor back into the rule it is showing."""
        if getattr(self, "_loading_rule", False):
            return
        if not (0 <= self._rule_index < len(self._rules)):
            return
        rule = self._rules[self._rule_index]
        rule.enabled = self.rule_enabled.isChecked()
        rule.name = self.rule_name_edit.text().strip() or "New rule"
        rule.match = self.rule_match.currentData() or "all"
        rule.skip_bulk = self.rule_skip_bulk.isChecked()
        rule.stop_after = self.rule_stop_after.isChecked()
        rule.conditions = [row.value() for row in self._condition_rows]
        rule.actions = [row.value() for row in self._action_rows]

    def _rule_edited(self, *_args) -> None:
        if getattr(self, "_loading_rule", False):
            return
        self._capture_rule()
        self._describe_rule()
        self._refresh_current_list_item()

    def _describe_rule(self) -> None:
        """Say in one place what this rule does, and what is wrong with it."""
        if not (0 <= self._rule_index < len(self._rules)):
            return
        rule = self._rules[self._rule_index]
        drafts = rule.drafts_a_reply
        self.template_note.setVisible(drafts)
        problems = rule.problems()
        if problems:
            self.rule_summary.setText(
                "<b>Not ready:</b> " + " ".join(problems)
                + (" A rule with something missing never runs."
                   if rule.enabled else "")
            )
            self.rule_summary.setProperty("tone", "warn")
        else:
            self.rule_summary.setText(rule.describe())
            self.rule_summary.setProperty("tone", "")
        self.rule_summary.style().unpolish(self.rule_summary)
        self.rule_summary.style().polish(self.rule_summary)

    def _refresh_current_list_item(self) -> None:
        entry = self.rule_list.item(self._rule_index)
        if entry is None:
            return
        rule = self._rules[self._rule_index]
        self.rule_list.blockSignals(True)
        entry.setText(self._rule_label(rule))
        self.rule_list.measure()
        entry.setCheckState(Qt.CheckState.Checked if rule.enabled
                            else Qt.CheckState.Unchecked)
        entry.setToolTip(self._rule_tooltip(rule))
        self.rule_list.blockSignals(False)

    def _rule_label(self, rule) -> str:
        """The name, marked when the rule is not finished enough to run."""
        return ("⚠ " if rule.problems() else "") + menu_text(rule.name)

    def _rule_tooltip(self, rule) -> str:
        """The whole name, which the list is too narrow to show, and the gist."""
        problems = rule.problems()
        if problems:
            return f"{rule.name}\n\nNot ready:\n" + "\n".join(
                f"• {problem}" for problem in problems)
        return f"{rule.name}\n\n{rule.describe()}"

    def _rule_renamed(self) -> None:
        self._rule_edited()

    def _rule_enabled_toggled(self, on: bool) -> None:
        self._rule_edited()

    def _rule_ticked(self, entry) -> None:
        """The checkbox in the list, which is the fastest way to turn one off."""
        index = self.rule_list.row(entry)
        if not (0 <= index < len(self._rules)):
            return
        self._rules[index].enabled = entry.checkState() == Qt.CheckState.Checked
        if index == self._rule_index:
            self._loading_rule = True
            self.rule_enabled.setChecked(self._rules[index].enabled)
            self._loading_rule = False
            self._describe_rule()

    def _rule_selected(self, index: int) -> None:
        if index == self._rule_index:
            return
        self._capture_rule()
        self._show_rule(index)

    def _add_rule(self) -> None:
        self._capture_rule()
        self._rules.append(autoreply.Rule(
            conditions=[autoreply.Condition()], actions=[autoreply.Action()]))
        self._rule_index = len(self._rules) - 1
        self._refresh_rule_list()

    def _duplicate_rule(self) -> None:
        self._capture_rule()
        if not (0 <= self._rule_index < len(self._rules)):
            return
        copied = autoreply.Rule.from_dict(self._rules[self._rule_index].to_dict())
        copied.name = f"{copied.name} (copy)"
        copied.enabled = False
        self._rules.insert(self._rule_index + 1, copied)
        self._rule_index += 1
        self._refresh_rule_list()

    def _remove_rule(self) -> None:
        if len(self._rules) <= 1:
            return
        self._loading_rule = True
        del self._rules[self._rule_index]
        self._rule_index = max(0, self._rule_index - 1)
        self._loading_rule = False
        self._refresh_rule_list()

    def _move_rule(self, step: int) -> None:
        self._capture_rule()
        target = self._rule_index + step
        if not (0 <= target < len(self._rules)):
            return
        rules = self._rules
        rules[self._rule_index], rules[target] = rules[target], rules[self._rule_index]
        self._rule_index = target
        self._refresh_rule_list()

    def _try_rule(self) -> None:
        """Run every switched-on rule over the messages already on screen.

        Reading a rule and knowing what it will do are different things. This
        answers the second question against real mail, without touching the
        mailbox or the model - a rule that would ask the model reports that it
        matched, and nothing is drafted.
        """
        self._capture_rule()
        samples = list(self._sample_items)
        if not samples:
            self.try_rule_result.setText(
                "Nothing to try it on yet. Run a scan, then come back.")
            return
        rules = [r for r in self._rules if r.enabled and r.ready]
        if not rules:
            self.try_rule_result.setText(
                "No rule is both switched on and finished.")
            return
        hits = []
        for item in samples:
            outcome = autoreply.apply_rules(rules, item.email, item.classification)
            if outcome is not None:
                hits.append((item, outcome))
        if not hits:
            self.try_rule_result.setText(
                f"No match in the {len(samples)} message"
                f"{'' if len(samples) == 1 else 's'} on screen.")
            return
        lines = [f"<b>{len(hits)} of {len(samples)} matched.</b>"]
        for item, outcome in hits[:4]:
            lines.append(
                f"• {menu_text(item.email.subject_display[:52])} — "
                f"{outcome.describe()} ({menu_text(outcome.rule_name)})")
        if len(hits) > 4:
            lines.append(f"…and {len(hits) - 4} more.")
        self.try_rule_result.setText("<br>".join(lines))

    def _build_appearance_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.mode_combo = QComboBox()
        for value, label in theme.MODES:
            self.mode_combo.addItem(label, value)
        form.addRow("Appearance", self.mode_combo)

        self.contrast_combo = QComboBox()
        for value, label in theme.CONTRASTS:
            self.contrast_combo.addItem(label, value)
        form.addRow("Contrast", self.contrast_combo)
        contrast_note = QLabel(
            "High contrast darkens the supporting colours until every one of "
            "them passes against its own background. Maximum goes further and "
            "drops colour altogether: black on white, or white on black, with "
            "nothing depending on hue."
        )
        contrast_note.setWordWrap(True)
        contrast_note.setProperty("dim", "true")
        form.addRow("", contrast_note)

        self.density_combo = QComboBox()
        for value, label, _blurb in theme.DENSITIES:
            self.density_combo.addItem(label, value)
        form.addRow("Spacing", self.density_combo)
        self.density_note = QLabel()
        self.density_note.setWordWrap(True)
        self.density_note.setProperty("dim", "true")
        form.addRow("", self.density_note)
        self.density_combo.currentIndexChanged.connect(self._density_changed)
        form.addRow(_separator())

        self.help_check = QCheckBox("Explain things on hover")
        self.help_check.setToolTip(
            "The same switch as the ? in the corner of the window. With it on, "
            "resting the pointer on anything explains what it does."
        )
        form.addRow("", self.help_check)
        help_note = QLabel(
            "Off by default, because a tooltip nobody asked for is in the way. "
            "The circled ? at the top right of the window toggles the same "
            "setting, and is filled in while it is on."
        )
        help_note.setWordWrap(True)
        help_note.setProperty("dim", "true")
        form.addRow("", help_note)
        form.addRow(_separator())

        self.readable_check = QCheckBox("Tune the layout for reading")
        form.addRow("", self.readable_check)
        readable_note = QLabel(
            "Larger type with a little more tracking, taller rows, heavier "
            "column headings, a wider focus ring, and more space inside every "
            "control. Independent of contrast - it changes the spacing rather "
            "than the colours."
        )
        readable_note.setWordWrap(True)
        readable_note.setProperty("dim", "true")
        form.addRow("", readable_note)

        self.rows_spin = QSpinBox()
        self.rows_spin.setRange(1, 6)
        self.rows_spin.setSuffix(" lines per row")
        self.rows_spin.valueChanged.connect(self._rows_chosen_by_hand)
        self.rows_spin.setToolTip(
            "How many lines of a summary or subject to show before it is cut "
            "off. Taller rows show more and fit fewer."
        )
        form.addRow("Row height", self.rows_spin)

        form.addRow(_separator())
        transfer = QHBoxLayout()
        export_button = QPushButton("Export settings…")
        export_button.setToolTip(
            "Write every setting to a text file you can read, keep, or move to "
            "another Mac. No passwords or keys are in it."
        )
        export_button.clicked.connect(self._export_settings)
        import_button = QPushButton("Import settings…")
        import_button.setToolTip("Read a settings file exported from this app")
        import_button.clicked.connect(self._import_settings)
        transfer.addWidget(export_button)
        transfer.addWidget(import_button)
        transfer.addStretch(1)
        form.addRow("Settings file", transfer)
        transfer_note = QLabel(
            "Plain JSON with a comment header. Passwords and API keys are not "
            "in it - they stay in the macOS Keychain and are entered again on "
            "the other Mac."
        )
        transfer_note.setWordWrap(True)
        transfer_note.setProperty("dim", "true")
        form.addRow("", transfer_note)

        # Applied as they are changed: a colour choice you cannot see until you
        # press OK is a colour choice made blind.
        for widget in (self.mode_combo, self.contrast_combo):
            widget.currentIndexChanged.connect(self._preview_appearance)
        self.readable_check.toggled.connect(self._preview_appearance)
        return page

    def _rows_chosen_by_hand(self) -> None:
        """Touching the spinner means this is now a deliberate choice."""
        self._row_lines_touched = True

    def _export_settings(self) -> None:
        """Write the current settings, including anything not yet saved."""
        default = str(Path.home() / "Downloads" / "Mail Manager settings.txt")
        path, _chosen = QFileDialog.getSaveFileName(
            self, "Export settings", default, "Text files (*.txt *.json);;All files (*)")
        if not path:
            return
        try:
            Path(path).write_text(self.collect().export_text(), encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            QMessageBox.warning(self, "Export failed", f"Could not write that file:\n{exc}")
            return
        self.status.setText(f"Exported to {Path(path).name}")

    def _import_settings(self) -> None:
        """Read a settings file and load it into the open dialog.

        Loaded into the form rather than applied straight away, so it can be
        looked at, adjusted and cancelled like any other change.
        """
        path, _chosen = QFileDialog.getOpenFileName(
            self, "Import settings", str(Path.home() / "Downloads"),
            "Text files (*.txt *.json);;All files (*)")
        if not path:
            return
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            QMessageBox.warning(self, "Import failed", f"Could not read that file:\n{exc}")
            return
        try:
            incoming = Settings.import_text(text)
        except ValueError as exc:
            QMessageBox.warning(self, "That file cannot be used", str(exc))
            return

        # The window's own layout belongs to this Mac, not to the file.
        for field in Settings.PRIVATE_FIELDS:
            setattr(incoming, field, getattr(self._settings, field))
        self._settings = incoming
        self._load_values()
        self._preview_appearance()
        count = len(incoming.accounts)
        self.status.setText(
            f"Loaded {Path(path).name}: {count} mailbox"
            f"{'es' if count != 1 else ''}. Passwords still need entering. "
            "Press OK to keep it."
        )

    def _density_changed(self) -> None:
        """Say what the choice does, and show it straight away."""
        name = self.density_combo.currentData() or "comfortable"
        blurb = next((b for n, _l, b in theme.DENSITIES if n == name), "")
        self.density_note.setText(blurb)
        self._preview_appearance()

    def _toggle_help(self, on: bool) -> None:
        """Mirror the window's switch, and keep the checkbox in step."""
        helpmode.install(QApplication.instance(), on)
        if hasattr(self, "help_check") and self.help_check.isChecked() != on:
            self.help_check.blockSignals(True)
            self.help_check.setChecked(on)
            self.help_check.blockSignals(False)
        self.status.setText(
            "Help is on. Rest the pointer on anything to see what it does."
            if on else "Help is off."
        )

    def _threshold_changed(self) -> None:
        """Say what the number means, since a percentage on its own does not."""
        percent = self.threshold_slider.value()
        self.threshold_value.setText(f"{percent}%")
        if percent >= 97:
            describes = ("Only the clearest cases file themselves. Almost "
                         "everything waits for you.")
        elif percent >= 92:
            describes = ("The recommended setting. Confident readings file "
                         "themselves; anything arguable waits for you.")
        elif percent >= 80:
            describes = ("More gets filed without asking, and more of it will "
                         "be wrong. Worth pairing with a model backend.")
        else:
            describes = ("Almost everything files itself, including readings "
                         "the sorter is barely sure of. Rarely what you want.")
        self.threshold_note.setText(describes)

    def _preview_appearance(self) -> None:
        app = QApplication.instance()
        if app is None:
            return
        theme.apply(app,
                    self.mode_combo.currentData() or "system",
                    self.contrast_combo.currentData() or "normal",
                    self.readable_check.isChecked(),
                    self.density_combo.currentData() or "comfortable")
        window = self.parent()
        if hasattr(window, "_apply_spacing"):
            # Show the spacing on the window behind the dialog, not just here.
            was = window.settings.density
            window.settings.density = self.density_combo.currentData() or "comfortable"
            window._apply_spacing()
            window.settings.density = was

    def _build_folders_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.root_edit = QLineEdit()
        self.other_root_edit = QLineEdit()

        self.routing_combo = QComboBox()
        for member in NonJobRouting:
            self.routing_combo.addItem(member.label, member.value)
        self.routing_combo.currentIndexChanged.connect(self._routing_changed)

        self.auto_non_job_check = QCheckBox(
            "Pre-tick confidently classified non-job mail as well"
        )
        self.subscribe_check = QCheckBox("Subscribe to folders this app creates")

        self.folders_preview = QLabel("")
        self.folders_preview.setWordWrap(True)
        self.folders_preview.setTextFormat(Qt.TextFormat.RichText)
        self.root_edit.textChanged.connect(self._update_folder_preview)
        self.other_root_edit.textChanged.connect(self._update_folder_preview)
        # The per-mailbox fields show what they would inherit, so "empty"
        # never means "nowhere".
        self.root_edit.textChanged.connect(self._sync_root_placeholders)
        self.other_root_edit.textChanged.connect(self._sync_root_placeholders)

        form.addRow("Job Search folder", self.root_edit)
        form.addRow("Non-job mail", self.routing_combo)
        form.addRow("Sorted mail folder", self.other_root_edit)
        form.addRow("", self.auto_non_job_check)
        form.addRow("", self.subscribe_check)
        form.addRow(_separator())
        form.addRow("Will use", self.folders_preview)
        form.addRow(_separator())
        form.addRow("Repeat scans", self._build_learned_box())
        return page

    def _build_learned_box(self) -> QWidget:
        """What the app has picked up from being corrected, and a way out.

        Anything that changes where mail goes has to be visible and has to be
        undoable, or it stops being a feature and starts being the app having
        opinions behind your back.
        """
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.reuse_check = QCheckBox("Reuse verdicts from earlier scans")
        self.reuse_check.setToolTip(
            "A message cannot change once it is sent, so re-scanning an "
            "overlapping window need not pay to analyze it twice. Turning "
            "this off analyzes everything, every time.")
        layout.addWidget(self.reuse_check)

        self.cache_summary = QLabel("")
        self.cache_summary.setWordWrap(True)
        self.cache_summary.setStyleSheet("opacity:0.75")
        layout.addWidget(self.cache_summary)

        clear_row = QHBoxLayout()
        clear_row.setSpacing(6)
        self.clear_cache_button = QPushButton("Discard kept verdicts")
        self.clear_cache_button.setToolTip(
            "Throw them away. The next scan analyzes everything again.")
        self.clear_cache_button.clicked.connect(self._clear_verdicts)
        clear_row.addWidget(self.clear_cache_button)
        clear_row.addStretch(1)
        layout.addLayout(clear_row)
        layout.addWidget(_separator())

        self.learn_check = QCheckBox(
            "File mail the way I corrected it last time")
        self.learn_check.setToolTip(
            "When you move a message to a folder the app did not suggest, it "
            "remembers, and files the next message from that sender the same "
            "way.")
        layout.addWidget(self.learn_check)

        self.learned_summary = QLabel("")
        self.learned_summary.setWordWrap(True)
        self.learned_summary.setStyleSheet("opacity:0.75")
        layout.addWidget(self.learned_summary)

        self.learned_list = QListWidget()
        self.learned_list.setAlternatingRowColors(True)
        self.learned_list.setMaximumHeight(150)
        self.learned_list.setToolTip(
            "Select a row and press Forget to undo what was learned from it.")
        layout.addWidget(self.learned_list)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self.forget_button = QPushButton("Forget selected")
        self.forget_button.setToolTip(
            "Undo what the app learned from the selected row.")
        self.forget_button.clicked.connect(self._forget_selected)
        self.forget_all_button = QPushButton("Forget everything")
        self.forget_all_button.setToolTip(
            "Throw away every correction the app has learned from.")
        self.forget_all_button.clicked.connect(self._forget_everything)
        buttons.addWidget(self.forget_button)
        buttons.addWidget(self.forget_all_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.learned_list.itemSelectionChanged.connect(self._learned_selection)
        self._reload_learned()
        self._reload_cache_summary()
        return box

    def _reload_cache_summary(self) -> None:
        try:
            cache = verdict_cache.VerdictCache.load()
        except Exception:  # noqa: BLE001 - a cache is never worth an error
            cache = verdict_cache.VerdictCache()
        self._verdicts = cache
        self.cache_summary.setText(cache.describe())
        self.clear_cache_button.setEnabled(len(cache) > 0)

    def _clear_verdicts(self) -> None:
        cache = getattr(self, "_verdicts", None)
        if cache is None or not len(cache):
            return
        confirmed = QMessageBox.question(
            self, "Discard kept verdicts?",
            f"This throws away {len(cache):,} verdict(s). Nothing is lost "
            "except the time it took to produce them - the next scan will "
            "analyze every message again.",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Cancel)
        if confirmed is not QMessageBox.StandardButton.Yes:
            return
        cache.clear()
        cache.save()
        self._reload_cache_summary()

    def _memory(self):
        """The corrections file, loaded once per dialog."""
        if getattr(self, "_corrections", None) is None:
            try:
                self._corrections = corrections.Memory.load()
            except Exception:  # noqa: BLE001 - a broken file is an empty one
                self._corrections = corrections.Memory()
        return self._corrections

    def _reload_learned(self) -> None:
        memory = self._memory()
        self.learned_list.clear()
        for learned in memory.summary():
            leaf = learned.folder.rsplit("/", 1)[-1]
            row = QListWidgetItem(f"{learned.key}  →  {leaf}")
            row.setToolTip(learned.because.capitalize() + ".")
            row.setData(Qt.ItemDataRole.UserRole, learned.key)
            self.learned_list.addItem(row)
        self.learned_summary.setText(memory.describe())
        empty = self.learned_list.count() == 0
        self.forget_all_button.setEnabled(not empty)
        self._learned_selection()

    def _learned_selection(self) -> None:
        self.forget_button.setEnabled(bool(self.learned_list.selectedItems()))

    def _forget_selected(self) -> None:
        keys = [row.data(Qt.ItemDataRole.UserRole)
                for row in self.learned_list.selectedItems()]
        if not keys:
            return
        memory = self._memory()
        for key in keys:
            memory.forget(key)
        self._save_memory(memory)
        self._reload_learned()

    def _forget_everything(self) -> None:
        memory = self._memory()
        if not len(memory):
            return
        confirmed = QMessageBox.question(
            self, "Forget every correction?",
            f"This throws away all {len(memory)} correction(s) the app has "
            "learned from. Mail will go wherever the sorter puts it until you "
            "start correcting it again.\n\nThis cannot be undone.",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Cancel)
        if confirmed is not QMessageBox.StandardButton.Yes:
            return
        memory.clear()
        self._save_memory(memory)
        self._reload_learned()

    def _save_memory(self, memory) -> None:
        try:
            memory.save()
        except OSError as exc:
            QMessageBox.warning(
                self, "Could not save",
                f"The corrections file could not be written:\n\n{exc}")

    # -- values ----------------------------------------------------------
    def _load_values(self) -> None:
        settings = self._settings
        self._load_accounts(settings)
        self._load_rules(settings)
        self.fetch_kb_spin.setValue(max(8, settings.fetch_bytes // 1024))

        provider_index = self.provider_combo.findData(settings.provider)
        self.provider_combo.setCurrentIndex(max(0, provider_index))
        self.base_url_edit.setText(settings.base_url)
        self._provider_changed()
        self.effort_combo.setCurrentText(settings.effort)
        self.threshold_slider.setValue(int(round(settings.confidence_threshold * 100)))
        self._threshold_changed()
        self.concurrency_spin.setValue(settings.concurrency)
        self.batch_spin.setValue(settings.batch_size)
        self.body_chars_spin.setValue(settings.max_body_chars)
        self.max_messages_spin.setValue(settings.max_messages)
        self.fallback_check.setChecked(settings.fallback_to_rules)

        self.root_edit.setText(settings.folder_root)
        self.other_root_edit.setText(settings.other_folder_root)
        routing_index = self.routing_combo.findData(settings.routing.value)
        self.routing_combo.setCurrentIndex(max(0, routing_index))
        self.auto_non_job_check.setChecked(settings.auto_approve_non_job)
        self.subscribe_check.setChecked(settings.subscribe_new_folders)
        self.learn_check.setChecked(settings.learn_from_corrections)
        self._sync_root_placeholders()
        self.reuse_check.setChecked(settings.reuse_verdicts)

        self.mode_combo.setCurrentIndex(
            max(0, self.mode_combo.findData(settings.appearance_mode)))
        self.contrast_combo.setCurrentIndex(
            max(0, self.contrast_combo.findData(settings.contrast)))
        self.readable_check.setChecked(settings.readable)
        self.help_check.setChecked(settings.help_mode)
        self.help_check.toggled.connect(
            lambda on: self.help_button.setChecked(on))
        self.density_combo.setCurrentIndex(
            max(0, self.density_combo.findData(settings.density)))
        self._density_changed()
        self.rows_spin.setValue(settings.effective_row_lines)

        self._read_keychain(settings.icloud_email)

        self._routing_changed()
        self._update_folder_preview()

    def _read_keychain(self, address: str) -> None:
        """Fill in the stored password, off the thread that draws the window.

        This used to be a plain call here. macOS asks permission whenever the
        app's signature changes, which is every rebuild, and the window froze
        behind the prompt asking about it.
        """
        from workers import KeychainReadWorker

        self.status.setText("Reading the Keychain…")
        worker = KeychainReadWorker(self._store, address, parent=self)
        self._keychain_worker = worker
        worker.finished_ok.connect(self._on_keychain_read)
        worker.finished.connect(lambda: setattr(self, "_keychain_worker", None))
        worker.start()

    @Slot(object)
    def _on_keychain_read(self, found: dict) -> None:
        if found.get("error"):
            self.status.setText(
                f"<span style='color:{ACCENT_RED}'>{_html(found['error'])}</span>")
            return
        # Only if nobody has started typing in the meantime.
        if not self.password_edit.text():
            self.password_edit.setText(found.get("password", ""))
        self.status.setText(f"Keychain backend: {found.get('backend', '')}")

    def _routing_changed(self) -> None:
        routing = NonJobRouting.parse(self.routing_combo.currentData())
        filing = routing is NonJobRouting.FILE
        self.other_root_edit.setEnabled(filing)
        self.auto_non_job_check.setEnabled(filing)
        self._update_folder_preview()

    def _sync_root_placeholders(self) -> None:
        if not hasattr(self, "account_root_edit"):
            return
        self.account_root_edit.setPlaceholderText(
            self.root_edit.text().strip() or "Job Search")
        self.account_other_root_edit.setPlaceholderText(
            self.other_root_edit.text().strip() or "Sorted Mail")

    def _update_folder_preview(self) -> None:
        plan = FolderPlan(
            root=self.root_edit.text() or "Job Search",
            other_root=self.other_root_edit.text() or "Sorted Mail",
        )
        lines = [f"• {_html(folder)}" for folder in plan.leaf_folders]
        routing = NonJobRouting.parse(self.routing_combo.currentData())
        if routing is NonJobRouting.FILE:
            lines.append(
                f"• {_html(plan.other_root)}/… - one subfolder per topic, created only when used"
            )
        self.folders_preview.setText("<br>".join(lines))

    def collect(self) -> Settings:
        data = asdict(self._settings)
        self._capture_account()
        self._capture_rule()
        kept = [a for a in self._accounts if a.address]
        data.update(
            mailboxes=kept,
            icloud_email=kept[0].address if kept else "",
            fetch_bytes=self.fetch_kb_spin.value() * 1024,
            provider=self.provider_combo.currentData() or providers.DEFAULT_PROVIDER,
            model=self._chosen_model(),
            base_url=self.base_url_edit.text().strip(),
            effort=self.effort_combo.currentText(),
            appearance_mode=self.mode_combo.currentData() or "system",
            contrast=self.contrast_combo.currentData() or "normal",
            readable=self.readable_check.isChecked(),
            density=self.density_combo.currentData() or "comfortable",
            help_mode=self.help_check.isChecked(),
            row_lines=self.rows_spin.value(),
            row_lines_auto=self._settings.row_lines_auto and not self._row_lines_touched,
            auto_reply=self.auto_reply_check.isChecked(),
            apply_sorting_rules=self.sorting_rules_check.isChecked(),
            reply_signature=self.signature_edit.text().strip(),
            reply_rules=[r.to_dict() for r in self._rules],
            confidence_threshold=self.threshold_slider.value() / 100.0,
            concurrency=self.concurrency_spin.value(),
            batch_size=self.batch_spin.value(),
            max_body_chars=self.body_chars_spin.value(),
            max_messages=self.max_messages_spin.value(),
            fallback_to_rules=self.fallback_check.isChecked(),
            folder_root=self.root_edit.text().strip() or "Job Search",
            other_folder_root=self.other_root_edit.text().strip() or "Sorted Mail",
            non_job_routing=NonJobRouting.parse(self.routing_combo.currentData()).value,
            auto_approve_non_job=self.auto_non_job_check.isChecked(),
            subscribe_new_folders=self.subscribe_check.isChecked(),
            learn_from_corrections=self.learn_check.isChecked(),
            reuse_verdicts=self.reuse_check.isChecked(),
        )
        return Settings(**data).normalized()

    def persist_credentials(self, settings: Settings) -> None:
        self._capture_account()
        problems = []
        for address, secret in self._account_passwords.items():
            if not address:
                continue
            try:
                self._store.set_mailbox_password(address, secret)
            except CredentialError as exc:
                problems.append(f"{address}: {exc}")
        # A mailbox that was removed should not leave its password behind.
        for account in self._removed_accounts:
            if account.address and not any(
                    a.address == account.address for a in self._accounts):
                try:
                    self._store.set_mailbox_password(account.address, "")
                except CredentialError:
                    pass
        self._removed_accounts = []
        if settings.needs_api_key:
            try:
                self._store.set_provider_key(settings.provider, self.api_key_edit.text())
            except CredentialError as exc:
                problems.append(str(exc))
        if problems:
            raise CredentialError("; ".join(problems))

    # -- tests -----------------------------------------------------------
    def _run_test(self, mode: str) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        settings = self.collect()
        if mode == "imap" and not settings.icloud_email:
            self.status.setText("Enter the mailbox email address first.")
            return
        self.test_imap_button.setEnabled(False)
        self.test_model_button.setEnabled(False)
        self.status.setText("Testing…")

        self._worker = ConnectionTestWorker(
            mode=mode,
            settings=settings,
            mailbox_password=self.password_edit.text(),
            api_key=self.api_key_edit.text(),
            parent=self,
        )
        self._worker.finished_ok.connect(self._test_succeeded)
        self._worker.failed.connect(self._test_failed)
        self._worker.finished.connect(self._test_finished)
        self._worker.start()

    @Slot(object)
    def _test_succeeded(self, result: dict) -> None:
        if result.get("mode") == "imap":
            folders = result.get("folders") or []
            job_folders = [f for f in folders if f.lower().startswith(self.root_edit.text().lower())]
            message = (
                f"Connected to {result['host']}. {result['folder_count']} mailboxes, "
                f"{result['inbox_messages']} messages in INBOX, hierarchy delimiter "
                f"“{result['delimiter']}”, UIDPLUS {'yes' if result['uidplus'] else 'no'}."
            )
            if job_folders:
                message += f" Existing triage folders: {', '.join(job_folders)}."
        else:
            cost = result.get("cost") or 0.0
            price = "free (on this Mac)" if result.get("on_device") else f"≈${cost:.5f} for this call"
            message = (
                f"{result.get('provider_label', 'The model')} · {result['model']} replied in "
                f"{result['seconds']}s - the test email was classified as "
                f"{result['category']} at {result['confidence'] * 100:.0f}% confidence "
                f"({result['input_tokens']:,} in / {result['output_tokens']:,} out, {price})."
            )
            # On a hosted backend a few seconds is noise. On a model running
            # here it is the whole story: at a minute a message, a scan of a
            # full inbox is an afternoon, and knowing that up front is the
            # difference between "it is broken" and "it is slow".
            seconds = float(result.get("seconds") or 0)
            if result.get("on_device") and seconds >= 8:
                each = seconds
                message += (
                    f"\n\nThat is {each:.0f}s for one message on this Mac, so "
                    f"about {each * 40 / 60:.0f} minutes for 40 and "
                    f"{each * 100 / 60:.0f} for 100. A smaller model is faster, "
                    "and the built-in rule set is instant."
                )
            for note in result.get("degradations") or ():
                message += f"\nNote: {note}"
        self.status.setText(f"<span style='color:{ACCENT_GREEN}'>{_html(message)}</span>")

    @Slot(str, str)
    def _test_failed(self, title: str, detail: str) -> None:
        self.status.setText(
            f"<span style='color:{ACCENT_RED}'><b>{_html(title)}</b><br>{_html(detail)}</span>"
        )

    @Slot()
    def _test_finished(self) -> None:
        self.test_imap_button.setEnabled(True)
        self.test_model_button.setEnabled(True)

    def _stop_test_worker(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None and worker.isRunning() and not worker.stop(3000):
            _abandon(worker)

    def _stop_ollama_workers(self) -> bool:
        """Shut down the on-device threads. False means "do not close yet".

        A probe is thrown away without ceremony. An install is not: Homebrew
        part-way through unpacking a cask is not a good thing to kill because
        somebody pressed Escape, so they are asked first.
        """
        for name in ("_ollama_probe", "_keychain_worker"):
            spare = getattr(self, name, None)
            setattr(self, name, None)
            if spare is not None and spare.isRunning() and not spare.stop(2000):
                _abandon(spare)

        worker = getattr(self, "_ollama_worker", None)
        if worker is None or not worker.isRunning():
            return True
        what = ("Ollama is still installing." if worker.step == "install"
                else "The model is still downloading.")
        answer = QMessageBox.question(
            self, "Still running", f"{what}\n\nStop it, or keep this window "
            "open until it finishes?",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Discard,
            QMessageBox.StandardButton.Cancel)
        if answer != QMessageBox.StandardButton.Discard:
            return False
        self._ollama_worker = None
        if not worker.stop(8000):
            _abandon(worker)
        return True

    def done(self, result: int) -> None:  # noqa: N802
        """Qt funnels OK, Cancel, Escape and the close box through here."""
        if not self._stop_ollama_workers():
            return
        self._stop_test_worker()
        super().done(result)

    def closeEvent(self, event) -> None:  # noqa: N802
        if not self._stop_ollama_workers():
            event.ignore()
            return
        self._stop_test_worker()
        super().closeEvent(event)


# ==========================================================================
# Main window
# ==========================================================================
