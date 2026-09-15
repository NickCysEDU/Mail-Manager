"""What this app is, who to tell when something is wrong, and where the code is.

A desktop app that reads somebody's mail owes them a plain answer to three
questions: what is it, where did it come from, and what happens to my data.
This window answers all three in one place, and gives them a way to report a
security problem without going looking for one.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QDesktopServices, QGuiApplication, QPixmap
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
                               QPushButton, QSizePolicy, QVBoxLayout, QWidget)

import buildinfo
from models import APP_DISPLAY_NAME
from widgets import _attr_url, _html

#: Where the project lives. One constant, so the three links cannot drift.
REPOSITORY = "https://github.com/NickCysEDU/Mail-Manager"
SECURITY_URL = f"{REPOSITORY}/security/advisories/new"
ISSUES_URL = f"{REPOSITORY}/issues"
LICENCE_URL = f"{REPOSITORY}/blob/main/LICENSE"
THIRD_PARTY_URL = f"{REPOSITORY}/blob/main/THIRD-PARTY-LICENSES.md"
HANDBOOK_URL = f"{REPOSITORY}/blob/main/docs/HANDBOOK.md"

#: Required disclosure, shown in full rather than behind a link.
AI_DISCLOSURE = (
    "During development and campaign preparation, the Mail Manager team used "
    "AI-assisted tools in a limited supporting role, including coding "
    "assistance, copy editing, and the preparation of some sample display "
    "content."
)


def _link(url: str, text: str) -> str:
    return f'<a href="{_attr_url(url)}">{_html(text)}</a>'


class AboutDialog(QDialog):
    """The app's own description of itself."""

    def __init__(self, settings, store, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"About {APP_DISPLAY_NAME}")
        # Wide enough for the four buttons to show their whole labels. At 520
        # they were clipped to "t a security con" and "iew the source".
        self.setMinimumWidth(660)
        self._settings = settings
        self._store = store

        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        layout.addLayout(self._heading())
        layout.addWidget(self._what_it_does())
        layout.addWidget(self._separator())
        layout.addWidget(self._where_your_data_goes())
        layout.addWidget(self._separator())
        layout.addWidget(self._this_build())
        layout.addWidget(self._separator())
        layout.addWidget(self._disclosure())
        layout.addLayout(self._actions())

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

        # Word-wrapped labels report their height from their width, and a
        # layout only learns the final width once. Without this the first
        # paragraph came up one line short and clipped its own last sentence.
        layout.activate()
        self.adjustSize()

    # -- the pieces ------------------------------------------------------
    def _heading(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(14)

        icon = QLabel()
        pixmap = self._icon()
        if pixmap is not None:
            icon.setPixmap(pixmap)
            icon.setFixedSize(pixmap.size())
            row.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)

        titles = QVBoxLayout()
        titles.setSpacing(2)
        name = QLabel(f"<span style='font-size:19px'><b>{APP_DISPLAY_NAME}</b></span>")
        name.setTextFormat(Qt.TextFormat.RichText)
        version = QLabel(buildinfo.short())
        version.setProperty("dim", "true")
        version.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        licence = QLabel(
            f"MIT licensed. {_link(LICENCE_URL, 'Read the licence')}. "
            f"Qt is included under the LGPL v3; "
            f"{_link(THIRD_PARTY_URL, 'third-party notices')}.")
        licence.setOpenExternalLinks(True)
        licence.setProperty("dim", "true")
        titles.addWidget(name)
        titles.addWidget(version)
        titles.addWidget(licence)
        row.addLayout(titles, 1)
        return row

    def _icon(self):
        from pathlib import Path
        import sys
        root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        for candidate in (root / "assets" / "icon.png",
                          Path(__file__).resolve().parent / "assets" / "icon.png"):
            if candidate.exists():
                pixmap = QPixmap(str(candidate))
                if not pixmap.isNull():
                    ratio = self.devicePixelRatioF() if hasattr(
                        self, "devicePixelRatioF") else 1.0
                    scaled = pixmap.scaled(
                        int(72 * ratio), int(72 * ratio),
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation)
                    scaled.setDevicePixelRatio(ratio)
                    return scaled
        return None

    def _body(self, html: str) -> QLabel:
        label = QLabel(html)
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setOpenExternalLinks(True)
        # Links clickable and text selectable, but never focused: a focus ring
        # around a paragraph reads as an input somebody is meant to fill in.
        label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction)
        label.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        label.setSizePolicy(label.sizePolicy().horizontalPolicy(),
                            QSizePolicy.Policy.MinimumExpanding)
        return label

    def _separator(self) -> QWidget:
        line = QWidget()
        line.setFixedHeight(1)
        line.setStyleSheet("background: palette(mid);")
        return line

    def _what_it_does(self) -> QLabel:
        return self._body(
            "Reads a window of your inbox over IMAP, works out what each "
            "message is, and shows you a table with a summary, a category, a "
            "destination folder and a confidence score for every row. You "
            "tick what you want filed. "
            "<b>Nothing moves until you press Apply.</b><br><br>"
            f"{_link(HANDBOOK_URL, 'The handbook')} explains how it decides."
        )

    def _where_your_data_goes(self) -> QLabel:
        provider = getattr(self._settings, "provider_label", "")
        on_device = self._on_device()
        if on_device:
            where = (f"<b>{provider}</b> runs on this Mac. No message text "
                     "leaves the machine.")
        else:
            where = (f"Message text is sent to <b>{provider}</b> so it can be "
                     "classified. Attachments are never uploaded, only their "
                     "filenames.")
        return self._body(
            f"<b>Where your mail goes.</b> {where}<br>"
            "Passwords and API keys are stored in the macOS Keychain, never "
            "in a file. Messages are fetched without being marked as read."
            f"<br>{self._at_rest()}"
        )

    def _at_rest(self) -> str:
        """What happens to the two files that describe your mail."""
        try:
            import vault
            # Only the first letter: lowercasing the whole sentence turns
            # Keychain, which is a product name, into keychain.
            said = vault.shared().describe()
            return (f"What it remembers between scans: "
                    f"{said[:1].lower()}{said[1:]}")
        except Exception:      # noqa: BLE001 - a label, never worth an error
            return ""

    def _on_device(self) -> bool:
        try:
            import providers
            return bool(providers.provider_class(self._settings.provider).on_device)
        except Exception:      # noqa: BLE001 - a label, never worth an error
            return False

    def _this_build(self) -> QLabel:
        settings = self._settings
        try:
            keychain = self._store.backend_name()
        except Exception:      # noqa: BLE001
            keychain = "unavailable"
        return self._body(
            "<b>This build.</b><br>"
            f"Sorter: <code>{settings.provider_label}</code> · "
            f"<code>{settings.model}</code><br>"
            f"Files at: {settings.confidence_threshold * 100:.0f}% confidence<br>"
            f"Keychain: <code>{keychain}</code>"
        )

    def _disclosure(self) -> QLabel:
        label = self._body(f"<i>{AI_DISCLOSURE}</i>")
        label.setProperty("dim", "true")
        return label

    def _actions(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)

        security = QPushButton("Security concern…")
        security.setToolTip(
            "Opens a private security advisory on GitHub. Please do not "
            "include real credentials or real message content.")
        security.clicked.connect(lambda: self._open(SECURITY_URL))
        row.addWidget(security)

        issues = QPushButton("Report a bug…")
        issues.setToolTip("Opens the issue tracker on GitHub.")
        issues.clicked.connect(lambda: self._open(ISSUES_URL))
        row.addWidget(issues)

        source = QPushButton("Source code…")
        source.setToolTip("Opens the repository on GitHub.")
        source.clicked.connect(lambda: self._open(REPOSITORY))
        row.addWidget(source)

        row.addStretch(1)

        copy = QPushButton("Copy build details")
        copy.setToolTip(
            "Copies the version, the commit and this Mac's details, which is "
            "what a bug report needs.")
        copy.clicked.connect(self._copy_build)
        row.addWidget(copy)
        return row

    # -- actions ---------------------------------------------------------
    def _open(self, url: str) -> None:
        QDesktopServices.openUrl(QUrl(url))

    def _copy_build(self) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(buildinfo.full())
