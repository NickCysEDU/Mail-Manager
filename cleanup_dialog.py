"""The window for clearing out a mailbox.

Deleting mail cannot be undone and the server does not ask twice, so one
rule: **the number comes from the server, before the delete.** Count sends a
read-only SEARCH with exactly the criteria Delete will use, and the
confirmation quotes its answer. Delete stays disabled until then, and again
the moment any control changes.

The suggestions only fill the boxes in. The sentence under the filters is
:meth:`cleanup.Criteria.describe`, so what the dialog says and what the
server is asked cannot drift apart, and the confirmation names the folder,
the count and the criteria in one sentence, with the number on its button.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog,
                               QDialogButtonBox, QFormLayout, QFrame,
                               QGroupBox, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QMessageBox, QPlainTextEdit,
                               QProgressBar, QPushButton, QSizePolicy,
                               QSpinBox, QVBoxLayout, QWidget)

import cleanup
import widgets
from cleanup import Criteria

log = logging.getLogger(__name__)

#: Above this many messages the confirmation asks for a second, deliberate
#: click: a speed bump, not a limit.
LOTS = 200


class ClearOutDialog(QDialog):
    """Pick what to clear out of one folder, see how many, then delete.

    Listing folders, counting and deleting each run on a worker thread: all
    three are slow against a real mailbox.
    """

    #: Emitted after a successful delete, with how many went, so the window
    #: can say so in the status bar and drop any rows that no longer exist.
    cleared = Signal(int)

    def __init__(self, account, password: str, items: Sequence[object] = (),
                 protected: Sequence[str] = (), parent=None) -> None:
        super().__init__(parent)
        self.account = account
        self.password = password
        self._worker = None
        self._counted: Optional[int] = None
        self._counted_for: Optional[Criteria] = None
        self._folder_counts: Dict[str, int] = {}

        self.setWindowTitle("Clear out mail")
        self.setModal(True)
        self.setMinimumWidth(720)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.addWidget(self._header())

        body = QHBoxLayout()
        body.setSpacing(14)
        body.addWidget(self._suggestions_pane(items, protected), 3)
        body.addWidget(self._filters_pane(), 4)
        layout.addLayout(body, 1)

        layout.addWidget(self._outcome_pane())
        layout.addWidget(self._buttons())

        self._refresh_sentence()
        self._load_folders()

    # -- the parts -------------------------------------------------------
    def _header(self) -> QWidget:
        text = QLabel(
            "Choose what to clear out, count it, then delete it. The count "
            "comes from the server, and nothing is deleted until you have "
            "seen it.")
        text.setWordWrap(True)
        text.setProperty("secondary", "true")
        return text

    def _suggestions_pane(self, items, protected) -> QWidget:
        box = QGroupBox("Worth clearing")
        column = QVBoxLayout(box)
        column.setSpacing(8)

        self.suggestions = cleanup.suggest(items, protected)
        self.suggestion_list = QListWidget()
        self.suggestion_list.setSelectionMode(
            QListWidget.SelectionMode.NoSelection)
        self.suggestion_list.setAlternatingRowColors(True)
        # A long address is elided, not scrolled sideways.
        self.suggestion_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.suggestion_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        widgets.describe(
            self.suggestion_list, "Suggested piles of mail",
            "Senders and kinds of mail the last scan found a lot of. Ticking "
            "one fills the filters in; nothing is deleted by ticking.")

        if not self.suggestions:
            empty = QLabel(
                "Nothing to suggest yet.\n\nScan some mail and the app will "
                "point out the senders you have most of. You can also just "
                "type a sender or a subject on the right.")
            empty.setWordWrap(True)
            empty.setProperty("secondary", "true")
            empty.setAlignment(Qt.AlignmentFlag.AlignTop)
            column.addWidget(empty, 1)
            self.suggestion_list.setVisible(False)
            column.addWidget(self.suggestion_list)
            return box

        for suggestion in self.suggestions:
            row = QListWidgetItem(f"{suggestion.label}\n{suggestion.reason}")
            row.setFlags(row.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            row.setCheckState(Qt.CheckState.Unchecked)
            row.setData(Qt.ItemDataRole.UserRole, suggestion)
            self.suggestion_list.addItem(row)
        self.suggestion_list.itemChanged.connect(self._suggestions_changed)
        column.addWidget(self.suggestion_list, 1)

        note = QLabel("Ticking one of these fills in the filters. It does "
                      "not delete anything.")
        note.setWordWrap(True)
        note.setProperty("secondary", "true")
        column.addWidget(note)
        return box

    def _filters_pane(self) -> QWidget:
        box = QGroupBox("What to delete")
        form = QFormLayout(box)
        form.setSpacing(8)
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.folder_combo = QComboBox()
        self.folder_combo.addItem("INBOX", "INBOX")
        self.folder_combo.setSizePolicy(QSizePolicy.Policy.Expanding,
                                        QSizePolicy.Policy.Fixed)
        widgets.describe(self.folder_combo, "Folder",
                         "Which folder to clear out. Only this one is touched.")
        self.folder_combo.currentIndexChanged.connect(self._changed)
        form.addRow("Folder", self.folder_combo)

        self.senders = QPlainTextEdit()
        self.senders.setPlaceholderText(
            "news@shop.example\nor just shop.example for the whole domain")
        self.senders.setFixedHeight(66)
        widgets.describe(self.senders, "Senders",
                         "One per line. A domain matches everyone at it.")
        self.senders.textChanged.connect(self._changed)
        form.addRow("From", self.senders)

        self.subjects = QPlainTextEdit()
        self.subjects.setPlaceholderText("One per line, e.g. Your receipt")
        self.subjects.setFixedHeight(50)
        widgets.describe(self.subjects, "Subjects",
                         "One per line. Any message whose subject contains "
                         "one of these matches.")
        self.subjects.textChanged.connect(self._changed)
        form.addRow("Subject has", self.subjects)

        self.age = QSpinBox()
        self.age.setRange(0, 3650)
        self.age.setValue(0)
        self.age.setSpecialValueText("any age")
        self.age.setSuffix(" days")
        widgets.describe(self.age, "Older than",
                         "Only mail older than this. Zero means any age.")
        self.age.valueChanged.connect(self._changed)
        form.addRow("Older than", self.age)

        self.only_seen = QCheckBox("Only mail I have already read")
        self.only_seen.setChecked(True)
        self.only_seen.setToolTip(
            "Leave unread mail alone.")
        self.only_seen.toggled.connect(self._changed)
        form.addRow("", self.only_seen)

        self.only_bulk = QCheckBox("Only mail sent to a list")
        self.only_bulk.setToolTip(
            "Newsletters, promotions and notifications.")
        self.only_bulk.toggled.connect(self._changed)
        form.addRow("", self.only_bulk)
        return box

    def _outcome_pane(self) -> QWidget:
        pane = QFrame()
        column = QVBoxLayout(pane)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)

        self.sentence = QLabel("")
        self.sentence.setWordWrap(True)
        column.addWidget(self.sentence)

        self.count_label = QLabel("Not counted yet.")
        self.count_label.setWordWrap(True)
        font = QFont(self.count_label.font())
        font.setBold(True)
        self.count_label.setFont(font)
        column.addWidget(self.count_label)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.progress.setTextVisible(True)
        column.addWidget(self.progress)
        return pane

    def _buttons(self) -> QWidget:
        buttons = QDialogButtonBox()
        self.count_button = QPushButton("Count")
        self.count_button.setToolTip(
            "Ask the server how many messages match. Nothing is deleted.")
        self.count_button.setDefault(True)
        self.count_button.clicked.connect(self._count)
        buttons.addButton(self.count_button,
                          QDialogButtonBox.ButtonRole.ActionRole)

        self.delete_button = QPushButton("Delete")
        widgets._paint_button(self.delete_button, "destructive")
        self.delete_button.setEnabled(False)
        self.delete_button.setToolTip("Count first.")
        self.delete_button.clicked.connect(self._delete)
        buttons.addButton(self.delete_button,
                          QDialogButtonBox.ButtonRole.DestructiveRole)

        self.stop_button = QPushButton("Stop")
        self.stop_button.setVisible(False)
        self.stop_button.clicked.connect(self._stop)
        buttons.addButton(self.stop_button,
                          QDialogButtonBox.ButtonRole.ResetRole)

        close = buttons.addButton(QDialogButtonBox.StandardButton.Close)
        close.clicked.connect(self.reject)
        return buttons

    # -- what the controls say -------------------------------------------
    def criteria(self) -> Criteria:
        """The controls, as the thing the server will be asked."""
        return Criteria(
            folder=self.folder_combo.currentData() or "INBOX",
            senders=tuple(_lines(self.senders.toPlainText())),
            subjects=tuple(_lines(self.subjects.toPlainText())),
            older_than_days=self.age.value(),
            only_seen=self.only_seen.isChecked(),
            only_bulk=self.only_bulk.isChecked(),
        )

    def _changed(self, *_args) -> None:
        """Any control moving invalidates the count: a count answers particular
        criteria, and leaving Delete lit after they change is how somebody
        agrees to forty and deletes four thousand.
        """
        self._counted = None
        self._counted_for = None
        self.delete_button.setEnabled(False)
        self.delete_button.setText("Delete")
        self.count_label.setText("Not counted yet.")
        self._refresh_sentence()

    def _refresh_sentence(self) -> None:
        criteria = self.criteria()
        if not criteria.is_armed:
            self.sentence.setText(
                "Choose a sender, a subject, an age or "
                "“only mail sent to a list”.")
            self.count_button.setEnabled(False)
            return
        self.count_button.setEnabled(True)
        self.sentence.setText("This deletes " + criteria.describe() + ".")

    def _suggestions_changed(self, _item) -> None:
        """Fill the filters in from whatever is ticked. They stay editable: a
        suggestion is a starting point.
        """
        senders: List[str] = []
        bulk = False
        for row in range(self.suggestion_list.count()):
            row_item = self.suggestion_list.item(row)
            if row_item.checkState() != Qt.CheckState.Checked:
                continue
            suggestion = row_item.data(Qt.ItemDataRole.UserRole)
            for address in suggestion.senders:
                if address not in senders:
                    senders.append(address)
            bulk = bulk or suggestion.bulk_only
        self.senders.blockSignals(True)
        self.senders.setPlainText("\n".join(senders))
        self.senders.blockSignals(False)
        if bulk and not self.only_bulk.isChecked():
            self.only_bulk.blockSignals(True)
            self.only_bulk.setChecked(True)
            self.only_bulk.blockSignals(False)
        self._changed()

    # -- talking to the server -------------------------------------------
    def _load_folders(self) -> None:
        from workers import FolderListWorker

        worker = FolderListWorker(self.account, self.password, parent=self)
        worker.finished_ok.connect(self._folders_arrived)
        worker.failed.connect(lambda _t, detail: log.info(
            "Could not list folders for the clear-out dialog (%s).", detail))
        self._folder_worker = worker
        worker.start()

    def _folders_arrived(self, found: List[Tuple[str, int]]) -> None:
        chosen = self.folder_combo.currentData()
        self.folder_combo.blockSignals(True)
        self.folder_combo.clear()
        for name, count in found:
            label = name if count < 0 else f"{name} — {count:,} message(s)"
            self.folder_combo.addItem(label, name)
            self._folder_counts[name] = count
        index = self.folder_combo.findData(chosen)
        self.folder_combo.setCurrentIndex(max(0, index))
        self.folder_combo.blockSignals(False)

    def _count(self) -> None:
        from workers import CountMatchingWorker

        criteria = self.criteria()
        if not criteria.is_armed:
            return
        self._busy(True, "Counting…")
        worker = CountMatchingWorker(self.account, self.password, criteria,
                                     parent=self)
        worker.progress.connect(self._show_progress)
        worker.failed.connect(self._worker_failed)
        worker.finished_ok.connect(
            lambda count, c=criteria: self._count_arrived(count, c))
        worker.finished.connect(lambda: self._busy(False))
        self._worker = worker
        worker.start()

    def _count_arrived(self, count: int, criteria: Criteria) -> None:
        self._counted = count
        self._counted_for = criteria
        if count:
            self.count_label.setText(
                f"{count:,} message(s) match in {criteria.folder}.")
            self.delete_button.setEnabled(True)
            self.delete_button.setText(f"Delete {count:,}")
        else:
            self.count_label.setText(
                f"Nothing in {criteria.folder} matches.")
            self.delete_button.setEnabled(False)
            self.delete_button.setText("Delete")

    def _delete(self) -> None:
        from workers import CleanOutWorker

        criteria = self.criteria()
        if self._counted is None or self._counted_for != criteria:
            # The button is disabled whenever this is true; checked again here
            # because being wrong costs deleted mail.
            self._changed()
            return
        if not self._confirm(self._counted, criteria):
            return
        self._busy(True, "Deleting…")
        worker = CleanOutWorker(self.account, self.password, criteria,
                                parent=self)
        worker.progress.connect(self._show_progress)
        worker.failed.connect(self._worker_failed)
        worker.finished_ok.connect(
            lambda removed, c=criteria: self._deleted(removed, c))
        worker.finished.connect(lambda: self._busy(False))
        self._worker = worker
        worker.start()

    def _confirm(self, count: int, criteria: Criteria) -> bool:
        """The last thing between the user and a delete: the number and the
        criteria in one sentence, and the number on the button too, so
        agreeing means having read it.
        """
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Delete these messages?")
        box.setText(f"Delete {count:,} message(s) from "
                    f"{criteria.folder} on {self.account.label}?")
        box.setInformativeText(
            f"This deletes {criteria.describe()}.\n\n"
            "They are removed from the server. This cannot be undone.")
        go = box.addButton(f"Delete {count:,}",
                           QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        widgets.selectable(box)
        box.exec()
        if box.clickedButton() is not go:
            return False
        if count < LOTS:
            return True
        second = QMessageBox(self)
        second.setIcon(QMessageBox.Icon.Warning)
        second.setWindowTitle("That is a lot of mail")
        second.setText(f"{count:,} messages is more than anybody re-reads.")
        second.setInformativeText(
            "Nothing here is recoverable afterwards. Are you sure?")
        yes = second.addButton("Yes, delete them",
                               QMessageBox.ButtonRole.DestructiveRole)
        second.addButton(QMessageBox.StandardButton.Cancel)
        second.setDefaultButton(QMessageBox.StandardButton.Cancel)
        widgets.selectable(second)
        second.exec()
        return second.clickedButton() is yes

    def _deleted(self, removed: int, criteria: Criteria) -> None:
        self.count_label.setText(
            f"Deleted {removed:,} message(s) from {criteria.folder}.")
        self._counted = None
        self._counted_for = None
        self.delete_button.setEnabled(False)
        self.delete_button.setText("Delete")
        self.cleared.emit(removed)
        self._load_folders()

    # -- housekeeping ----------------------------------------------------
    def _busy(self, busy: bool, what: str = "") -> None:
        self.progress.setVisible(busy)
        self.stop_button.setVisible(busy)
        self.count_button.setEnabled(not busy and self.criteria().is_armed)
        self.delete_button.setEnabled(
            not busy and self._counted is not None and self._counted > 0)
        for control in (self.folder_combo, self.senders, self.subjects,
                        self.age, self.only_seen, self.only_bulk,
                        self.suggestion_list):
            control.setEnabled(not busy)
        if busy:
            self.progress.setRange(0, 0)
            self.progress.setFormat(what)

    def _show_progress(self, done: int, total: int, message: str) -> None:
        if total > 1:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
        else:
            self.progress.setRange(0, 0)
        self.progress.setFormat(message)

    def _worker_failed(self, title: str, detail: str) -> None:
        widgets.say(self, QMessageBox.Icon.Warning, title, detail)

    def _stop(self) -> None:
        worker = self._worker
        if worker is not None and worker.isRunning():
            worker.cancel()
            self.progress.setFormat("Stopping…")

    def reject(self) -> None:
        """Close, but never while one of these threads is running: they are
        children of the dialog, and Qt aborts the process when a running
        QThread is destroyed.
        """
        if not self._stop_everything():
            return
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._stop_everything():
            event.accept()
        else:
            event.ignore()

    def _stop_everything(self) -> bool:
        from PySide6.QtCore import QThread

        ok = True
        for worker in self.findChildren(QThread):
            if not worker.isRunning():
                continue
            stop = getattr(worker, "stop", None)
            if stop is not None and stop(4000):
                continue
            widgets._abandon(worker)
        return ok


def _lines(text: str) -> List[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]
