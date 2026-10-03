"""The window that offers a new version, and the threads it works on."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QProgressBar,
                               QPushButton, QTextBrowser, QVBoxLayout)

import updates
from models import APP_DISPLAY_NAME, APP_VERSION


class Look(QThread):
    """Ask GitHub for the latest release."""

    found = Signal(object)      # a Release, or None
    failed = Signal(str)

    def run(self) -> None:
        try:
            self.found.emit(updates.fetch_latest())
        except Exception as exc:      # noqa: BLE001 - said, not raised
            self.failed.emit(str(exc))


class Fetch(QThread):
    """Download, check and stage an update. See updates.Installer."""

    stage = Signal(str)
    progress = Signal(float)
    ready = Signal(str)         # the script that swaps the apps
    failed = Signal(str)

    def __init__(self, installer: updates.Installer, parent=None) -> None:
        super().__init__(parent)
        self.installer = installer
        installer.stage = self.stage.emit
        installer.progress = self.progress.emit

    def run(self) -> None:
        try:
            self.ready.emit(str(self.installer.run()))
        except updates.UpdateError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:      # noqa: BLE001 - said, not raised
            self.failed.emit(f"The update failed: {exc}")


def notes(text: str, most: int = 1500) -> str:
    """What is new: the release notes' first section, or their start where
    they have no sections."""
    lines = (text or "").splitlines()
    first = next((index for index, line in enumerate(lines)
                  if line.startswith("## ")), None)
    if first is not None:
        lines = lines[first:]
        end = next((index for index, line in enumerate(lines)
                    if index and line.startswith("## ")), len(lines))
        lines = lines[:end]
    out = "\n".join(lines).strip()
    return out if len(out) <= most else out[:most].rstrip() + "…"


class UpdateDialog(QDialog):
    """A newer version: what is in it, and Later, Skip or Update."""

    #: The version not to be offered again.
    skipped = Signal(str)
    #: Downloaded and checked: quit, and the script puts it in place.
    restart = Signal(object, str)      # the Installer, the script

    def __init__(self, release: updates.Release, app=None, parent=None,
                 opener=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Update")
        self.release = release
        #: Where this copy can be replaced, or None to send the person to
        #: the release page instead.
        self.app = app if release.url else None
        self._opener = opener
        self._fetch: Optional[Fetch] = None
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.addWidget(QLabel(
            f"<b>{APP_DISPLAY_NAME} {release.version}</b> is out. "
            f"This is {APP_VERSION}."))
        self.notes = QTextBrowser()
        self.notes.setOpenExternalLinks(True)
        self.notes.setMarkdown(notes(release.notes) or "No notes.")
        self.notes.setMinimumSize(460, 160)
        layout.addWidget(self.notes, 1)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.hide()
        layout.addWidget(self.bar)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setProperty("dim", "true")
        self.status.hide()
        layout.addWidget(self.status)
        row = QHBoxLayout()
        self.skip_button = QPushButton("Skip This Version")
        self.skip_button.clicked.connect(self._skip)
        self.later_button = QPushButton("Later")
        self.later_button.clicked.connect(self.reject)
        self.update_button = QPushButton("Update" if self.app else
                                         "Download…")
        self.update_button.setToolTip(
            "Downloads it, checks it, and restarts into it." if self.app else
            "Opens the release page, to download it from there.")
        self.update_button.setDefault(True)
        from widgets import _paint_button

        _paint_button(self.update_button, "primary", bold=False)
        self.update_button.clicked.connect(self._update)
        row.addWidget(self.skip_button)
        row.addStretch(1)
        row.addWidget(self.later_button)
        row.addWidget(self.update_button)
        layout.addLayout(row)

    def _skip(self) -> None:
        self.skipped.emit(self.release.version)
        self.reject()

    def _page(self) -> None:
        QDesktopServices.openUrl(QUrl(self.release.page or updates.PAGE))
        self.accept()

    def _update(self) -> None:
        if self.app is None:
            self._page()
            return
        installer = updates.Installer(self.release, self.app,
                                      **({"opener": self._opener}
                                         if self._opener else {}))
        fetch = Fetch(installer, self)
        fetch.stage.connect(self._say)
        fetch.progress.connect(
            lambda share: self.bar.setValue(int(share * 1000)))
        fetch.ready.connect(lambda script: self._ready(installer, script))
        fetch.failed.connect(self._failed)
        self._fetch = fetch
        for button in (self.skip_button, self.update_button):
            button.setEnabled(False)
        self.later_button.setText("Cancel")
        self.later_button.clicked.disconnect()
        self.later_button.clicked.connect(self._cancel)
        self.bar.show()
        self._say("Starting")
        fetch.start()

    def _say(self, words: str) -> None:
        self.status.setText(f"{words}…")
        self.status.show()

    def _cancel(self) -> None:
        if self._fetch is not None:
            self._fetch.installer.cancelled = True
            self._fetch.wait(5000)
        self.reject()

    def _ready(self, installer, script: str) -> None:
        self._say("Restarting")
        self.restart.emit(installer, script)

    def _failed(self, why: str) -> None:
        self.bar.hide()
        self.status.setText(why)
        self.status.setProperty("dim", "false")
        self.status.setStyleSheet("color: #c65b4e;")
        self.status.show()
        self.later_button.setText("Close")
        self.later_button.clicked.disconnect()
        self.later_button.clicked.connect(self.reject)
        self.update_button.setText("Open the Release Page")
        self.update_button.clicked.disconnect()
        self.update_button.clicked.connect(self._page)
        self.update_button.setEnabled(True)
