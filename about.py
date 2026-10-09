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
                               QPushButton, QVBoxLayout, QWidget)

import buildinfo
from models import APP_DISPLAY_NAME
from widgets import _attr_url, _html

#: Where the project lives. One constant, so the three links cannot drift.
REPOSITORY = "https://github.com/NickCysEDU/Mail-Manager"
SECURITY_URL = f"{REPOSITORY}/security/advisories/new"
ISSUES_URL = f"{REPOSITORY}/issues"
LICENCE_URL = f"{REPOSITORY}/blob/main/LICENSE"
THIRD_PARTY_URL = f"{REPOSITORY}/blob/main/THIRD-PARTY-LICENSES.md"
LEGAL_URL = f"{REPOSITORY}/blob/main/LEGAL.md"
HANDBOOK_URL = f"{REPOSITORY}/blob/main/docs/HANDBOOK.md"

ICON_SIZE = 56


def _link(url: str, text: str) -> str:
    return f'<a href="{_attr_url(url)}">{_html(text)}</a>'


class AboutDialog(QDialog):
    """The app's own description of itself."""

    def __init__(self, settings, store, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"About {APP_DISPLAY_NAME}")
        self._settings = settings
        self._store = store

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)
        layout.addLayout(self._heading())
        layout.addWidget(self._where_your_data_goes())
        layout.addWidget(self._this_build())
        layout.addSpacing(4)
        layout.addLayout(self._actions())

        import touchbar

        security, issues, source, copy = self._links
        touchbar.give(self, [
            touchbar.Button("security", "Security concern", security),
            touchbar.Button("bug", "Report a bug", issues),
            touchbar.Button("source", "Source code", source),
            touchbar.Button("copy", "Copy build details", copy,
                            title="Copy details"),
            *touchbar.button_items(self),
        ], "about")
        self._placed = False
        self._fit()

    def _fit(self) -> None:
        """As wide as the row of buttons, and exactly as tall as the words
        need at that width. A wrapped label guesses its height from a width
        of its own choosing, which left a gap under every paragraph."""
        layout = self.layout()
        layout.activate()
        margins = layout.contentsMargins()
        width = max(layout.minimumSize().width(),
                    self._button_row.sizeHint().width() + margins.left() + margins.right())
        self.setFixedSize(width, layout.totalHeightForWidth(width))

    def showEvent(self, event) -> None:      # noqa: N802 - Qt's name
        """On the screen, whole: centred over the window that opened it, and
        no taller than the screen has room for."""
        super().showEvent(event)
        if self._placed:
            return
        self._placed = True
        screen = self.screen()
        if screen is None:
            return
        room = screen.availableGeometry()
        # The frame round the window counts: a title bar's worth below the
        # screen's edge is still off the screen.
        shell = self.frameGeometry().size() - self.size()
        outer = self.size().boundedTo(room.size() - shell) + shell
        anchor = self.parentWidget().frameGeometry() if self.parentWidget() else room
        x = anchor.center().x() - outer.width() // 2
        y = anchor.center().y() - outer.height() // 2
        x = max(room.left(), min(x, room.right() + 1 - outer.width()))
        y = max(room.top(), min(y, room.bottom() + 1 - outer.height()))
        self.move(x, y)

    def _heading(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(14)

        icon = QLabel()
        pixmap = self._icon()
        if pixmap is not None:
            icon.setPixmap(pixmap)
            icon.setFixedSize(pixmap.size() / pixmap.devicePixelRatio())
            row.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)

        titles = QVBoxLayout()
        titles.setSpacing(2)
        name = QLabel(f"<span style='font-size:17px'><b>{APP_DISPLAY_NAME}</b></span>")
        name.setTextFormat(Qt.TextFormat.RichText)
        version = QLabel(buildinfo.short())
        version.setProperty("dim", "true")
        version.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        what = self._body(
            "Sorts your inbox into folders. <b>Nothing moves until you press "
            f"Apply.</b> {_link(HANDBOOK_URL, 'How it decides')}")
        titles.addWidget(name)
        titles.addWidget(version)
        titles.addSpacing(4)
        titles.addWidget(what)
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
                    ratio = self.devicePixelRatioF()
                    scaled = pixmap.scaled(
                        int(ICON_SIZE * ratio), int(ICON_SIZE * ratio),
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
        return label

    def _where_your_data_goes(self) -> QLabel:
        provider = self._settings.provider_label
        if self._on_device():
            where = (f"<b>{provider}</b> runs on this Mac. No message text "
                     "leaves the machine.")
        else:
            where = (f"Message text is sent to <b>{provider}</b> to be sorted. "
                     "Attachments are never uploaded, only their filenames.")
        return self._body(
            f"{where} Passwords and API keys stay in the macOS Keychain, and "
            f"messages are fetched without being marked as read. {self._at_rest()}"
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
        label = self._body(
            f"{_html(settings.provider_label)} · {_html(settings.model)} · files at "
            f"{settings.confidence_threshold * 100:.0f}% confidence · "
            f"Keychain: {_html(keychain)}<br>"
            f"MIT licence, no warranty: {_link(LICENCE_URL, 'licence')}, "
            f"{_link(THIRD_PARTY_URL, 'third-party notices')} (Qt is LGPL v3), "
            f"{_link(LEGAL_URL, 'legal notices')}")
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

        copy = QPushButton("Copy build details")
        copy.setToolTip(
            "Copies the version, the commit and this Mac's details, which is "
            "what a bug report needs.")
        copy.clicked.connect(self._copy_build)
        row.addWidget(copy)
        row.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        row.addWidget(buttons)
        self._links = (security, issues, source, copy)
        self._button_row = row
        return row

    def _open(self, url: str) -> None:
        QDesktopServices.openUrl(QUrl(url))

    def _copy_build(self) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(buildinfo.full())
