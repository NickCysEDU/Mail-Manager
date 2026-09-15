"""A window for looking at what somebody attached, without running it.

The rule this is built around: **nothing here executes anything.** Images,
audio, PDFs and text render in-process; everything else is described and
offered as a save. Opening a saved file is done in Finder afterwards, where
the quarantine flag this app sets is the one macOS checks.

Three decisions worth knowing about, because they look like missing features:

*SVG is shown as text.* It is a document format with script elements and
external references, and rendering one means running somebody's XML through a
parser that can fetch URLs. The markup is readable; that is enough.

*HTML is shown as text.* Rendering it would load remote images, which is how
an attachment tells the sender you opened it.

*Archives are never expanded.* Saving a zip is fine. Browsing one means
walking paths a stranger chose, which is the whole zip-slip family.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QSize, Qt, QTimer, QUrl, Slot
from PySide6.QtGui import QGuiApplication, QImage, QPixmap
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog, QFrame,
                               QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QMessageBox, QPlainTextEdit,
                               QPushButton, QScrollArea, QSizePolicy, QSlider,
                               QStackedWidget, QVBoxLayout, QWidget)

import attachments
from widgets import _html, system_font

#: Text parts longer than this are truncated in the viewer. A log file
#: attached to a bug report can be tens of megabytes and nobody reads it in a
#: dialog.
TEXT_LIMIT = 400_000


def _muted(text: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("dim", "true")
    label.setWordWrap(True)
    return label


class ImagePane(QScrollArea):
    """Any format Qt can decode, with the bomb cases refused."""

    def __init__(self) -> None:
        super().__init__()
        self.setWidgetResizable(True)
        self._label = QLabel()
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setWidget(self._label)
        self._pixmap: Optional[QPixmap] = None

    def show_bytes(self, data: bytes) -> str:
        """Render, or say why not. Returns a status line."""
        image = QImage()
        # fromData decodes the header first, so a size check happens before
        # the pixels are ever allocated.
        if not image.loadFromData(data):
            self._label.setText("This does not decode as an image.")
            return "not a readable image"
        pixels = image.width() * image.height()
        if pixels > attachments.MAX_PIXELS:
            self._label.setText(
                f"{image.width()} x {image.height()} is too large to open "
                "safely.\nSave it and use Preview.")
            return f"{image.width()}x{image.height()}, refused"
        self._pixmap = QPixmap.fromImage(image)
        self._fit()
        return f"{image.width()} x {image.height()}"

    def _fit(self) -> None:
        if self._pixmap is None:
            return
        area = self.viewport().size()
        if self._pixmap.width() > area.width() or self._pixmap.height() > area.height():
            self._label.setPixmap(self._pixmap.scaled(
                area, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
        else:
            self._label.setPixmap(self._pixmap)

    def resizeEvent(self, event) -> None:      # noqa: N802 - Qt's name
        super().resizeEvent(event)
        self._fit()


class AudioPane(QWidget):
    """Play, pause, seek, volume, and a clock that says where you are."""

    def __init__(self) -> None:
        super().__init__()
        self._file: Optional[Path] = None
        self._player = None
        self._audio = None

        self.title = QLabel("")
        self.title.setWordWrap(True)
        self.play = QPushButton("Play")
        self.play.setFixedWidth(84)
        self.position = QSlider(Qt.Orientation.Horizontal)
        self.position.setRange(0, 0)
        self.clock = QLabel("0:00 / 0:00")
        self.clock.setFont(system_font())
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(70)
        self.volume.setFixedWidth(110)

        row = QHBoxLayout()
        row.addWidget(self.play)
        row.addWidget(self.position, 1)
        row.addWidget(self.clock)
        row.addSpacing(12)
        row.addWidget(QLabel("Volume"))
        row.addWidget(self.volume)

        layout = QVBoxLayout(self)
        layout.addStretch(1)
        layout.addWidget(self.title)
        layout.addLayout(row)
        layout.addWidget(_muted(
            "Playback is local. Nothing about this file leaves the machine."))
        layout.addStretch(1)

        self.play.clicked.connect(self._toggle)
        self.position.sliderMoved.connect(self._seek)
        self.volume.valueChanged.connect(self._set_volume)

    def _ensure_player(self) -> bool:
        if self._player is not None:
            return True
        try:
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        except ImportError:
            return False
        self._audio = QAudioOutput()
        self._audio.setVolume(self.volume.value() / 100)
        self._player = QMediaPlayer()
        self._player.setAudioOutput(self._audio)
        self._player.positionChanged.connect(self._moved)
        self._player.durationChanged.connect(self._duration)
        self._player.playbackStateChanged.connect(self._state)
        self._player.errorOccurred.connect(self._error)
        return True

    def load(self, path: Path, label: str) -> str:
        self.title.setText(label)
        self._file = path
        if not self._ensure_player():
            self.play.setEnabled(False)
            return "audio playback is unavailable in this build"
        # A local file, never a URL from the message.
        self._player.setSource(QUrl.fromLocalFile(str(path)))
        self.play.setEnabled(True)
        return "ready"

    @Slot()
    def _toggle(self) -> None:
        if self._player is None:
            return
        from PySide6.QtMultimedia import QMediaPlayer
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
        else:
            self._player.play()

    @Slot(int)
    def _seek(self, value: int) -> None:
        if self._player is not None:
            self._player.setPosition(value)

    @Slot(int)
    def _set_volume(self, value: int) -> None:
        if self._audio is not None:
            self._audio.setVolume(value / 100)

    @Slot(int)
    def _moved(self, value: int) -> None:
        if not self.position.isSliderDown():
            self.position.setValue(value)
        self._show_clock()

    @Slot(int)
    def _duration(self, value: int) -> None:
        self.position.setRange(0, value)
        self._show_clock()

    def _state(self, *_args) -> None:
        try:
            from PySide6.QtMultimedia import QMediaPlayer
            playing = self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        except Exception:      # noqa: BLE001
            playing = False
        self.play.setText("Pause" if playing else "Play")

    def _error(self, *_args) -> None:
        self.title.setText(self.title.text() + "  (this file will not play)")
        self.play.setEnabled(False)

    def _show_clock(self) -> None:
        self.clock.setText(
            f"{_mmss(self.position.value())} / {_mmss(self.position.maximum())}")

    def stop(self) -> None:
        if self._player is not None:
            self._player.stop()
            self._player.setSource(QUrl())


def _mmss(ms: int) -> str:
    seconds = max(0, int(ms // 1000))
    return f"{seconds // 60}:{seconds % 60:02d}"


class TextPane(QPlainTextEdit):
    """Always plain. HTML and SVG arrive here rather than at a renderer."""

    def __init__(self) -> None:
        super().__init__()
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)

    def show_bytes(self, data: bytes) -> str:
        clipped = data[:TEXT_LIMIT]
        text = clipped.decode("utf-8", "replace")
        if len(data) > TEXT_LIMIT:
            text += f"\n\n[... {len(data) - TEXT_LIMIT:,} more bytes not shown]"
        self.setPlainText(text)
        return f"{len(data):,} bytes of text"


class PdfPane(QWidget):
    """Qt's own PDF view, which does not run JavaScript or fetch anything."""

    def __init__(self) -> None:
        super().__init__()
        self._document = None
        self._view = None
        self._fallback = _muted("PDF viewing is unavailable in this build.")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._fallback)
        self._layout = layout

    def load(self, path: Path) -> str:
        try:
            from PySide6.QtPdf import QPdfDocument
            from PySide6.QtPdfWidgets import QPdfView
        except ImportError:
            return "PDF viewing is unavailable in this build"
        if self._view is None:
            self._document = QPdfDocument(self)
            self._view = QPdfView(self)
            self._view.setDocument(self._document)
            self._view.setPageMode(QPdfView.PageMode.MultiPage)
            self._fallback.hide()
            self._layout.addWidget(self._view)
        self._document.load(str(path))
        pages = self._document.pageCount()
        return f"{pages} page{'s' if pages != 1 else ''}"


class AttachmentViewer(QDialog):
    """The window. A list on the left, whatever it is on the right."""

    def __init__(self, found: List[attachments.Attachment], subject: str = "",
                 parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Attachments")
        self.setMinimumSize(QSize(860, 560))
        self._found = list(found)
        self._temp = Path(tempfile.mkdtemp(prefix="mm-attach-"))
        os.chmod(self._temp, 0o700)
        self._written: List[Path] = []

        self.list = QListWidget()
        self.list.setFixedWidth(260)
        for item in self._found:
            entry = QListWidgetItem(f"{item.shown}\n{item.human_size()}")
            if item.executable:
                entry.setText(f"{item.shown}\n{item.human_size()} · program")
            elif item.inline:
                entry.setText(f"{item.shown}\n{item.human_size()} · inline")
            self.list.addItem(entry)

        self.image = ImagePane()
        self.audio = AudioPane()
        self.text = TextPane()
        self.pdf = PdfPane()
        self.blank = QWidget()
        blank_layout = QVBoxLayout(self.blank)
        blank_layout.addStretch(1)
        self.blank_label = QLabel("")
        self.blank_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.blank_label.setWordWrap(True)
        blank_layout.addWidget(self.blank_label)
        blank_layout.addStretch(1)

        self.stack = QStackedWidget()
        for pane in (self.blank, self.image, self.audio, self.text, self.pdf):
            self.stack.addWidget(pane)

        self.heading = QLabel("")
        self.heading.setTextFormat(Qt.TextFormat.RichText)
        self.heading.setWordWrap(True)
        self.status = _muted("")
        self.warning = QLabel("")
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet("color: #c65b4e;")
        self.warning.hide()

        self.save_button = QPushButton("Save…")
        self.save_all = QPushButton("Save all…")
        self.copy_button = QPushButton("Copy image")
        self.copy_button.hide()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)

        actions = QHBoxLayout()
        actions.addWidget(self.save_button)
        actions.addWidget(self.save_all)
        actions.addWidget(self.copy_button)
        actions.addStretch(1)
        actions.addWidget(buttons)

        right = QVBoxLayout()
        right.addWidget(self.heading)
        right.addWidget(self.warning)
        right.addWidget(self.stack, 1)
        right.addWidget(self.status)
        right.addLayout(actions)

        body = QHBoxLayout(self)
        body.addWidget(self.list)
        frame = QFrame()
        frame.setLayout(right)
        frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        body.addWidget(frame, 1)

        self.list.currentRowChanged.connect(self._show)
        self.save_button.clicked.connect(self._save_current)
        self.save_all.clicked.connect(self._save_all)
        self.copy_button.clicked.connect(self._copy_image)
        if self._found:
            self.list.setCurrentRow(0)
        else:
            self.heading.setText("<b>Nothing is attached to this message.</b>")
            self.save_button.setEnabled(False)
            self.save_all.setEnabled(False)

    # -- showing one ------------------------------------------------------
    def _current(self) -> Optional[attachments.Attachment]:
        row = self.list.currentRow()
        return self._found[row] if 0 <= row < len(self._found) else None

    @Slot(int)
    def _show(self, _row: int) -> None:
        item = self._current()
        if item is None:
            return
        self.audio.stop()
        self.copy_button.hide()
        self.heading.setText(
            f"<b>{_html(item.shown)}</b><br>"
            f"{_html(item.content_type)} · {item.human_size()}")

        note = self._warning_for(item)
        self.warning.setText(note)
        self.warning.setVisible(bool(note))

        data = item.data or b""
        if not data:
            self.blank_label.setText("This part arrived empty.")
            self.stack.setCurrentWidget(self.blank)
            self.status.setText("")
            return

        kind, _mime = attachments.sniff(data[:32], item.content_type, item.name)
        if kind == "image" and item.ext != "svg":
            self.stack.setCurrentWidget(self.image)
            self.status.setText(self.image.show_bytes(data))
            self.copy_button.setVisible(True)
        elif kind == "audio":
            path = self._materialise(item)
            self.stack.setCurrentWidget(self.audio)
            self.status.setText(self.audio.load(path, item.shown))
        elif kind == "pdf":
            path = self._materialise(item)
            self.stack.setCurrentWidget(self.pdf)
            self.status.setText(self.pdf.load(path))
        elif kind in ("text", "program") or item.ext in ("svg", "html", "htm", "xml"):
            self.stack.setCurrentWidget(self.text)
            self.status.setText(self.text.show_bytes(data))
        else:
            self.blank_label.setText(
                f"{_describe(kind)}\n\nSave it if you want it.")
            self.stack.setCurrentWidget(self.blank)
            self.status.setText("")

    @staticmethod
    def _warning_for(item: attachments.Attachment) -> str:
        if item.executable:
            return ("This is a program, whatever it is called. Saving it is "
                    "fine; running it is not something this app will do, and "
                    "macOS will ask you about it if you try.")
        if item.archive:
            return ("An archive. It is saved as one file - nothing here "
                    "opens it, because what is inside chose its own paths.")
        if item.signature:
            return "A cryptographic signature part, not a document."
        if item.ext in ("svg", "html", "htm", "xml"):
            return ("Shown as text on purpose. Rendering it would run a "
                    "parser that can fetch remote content.")
        return ""

    def _materialise(self, item: attachments.Attachment) -> Path:
        """Write to a private temp file, because players want a path."""
        path = self._temp / item.filename
        if path not in self._written:
            path.write_bytes(item.data or b"")
            os.chmod(path, 0o600)
            self._written.append(path)
        return path

    # -- saving -----------------------------------------------------------
    @Slot()
    def _save_current(self) -> None:
        item = self._current()
        if item is None or not item.data:
            return
        if item.executable and not self._confirm_program(item):
            return
        start = str(Path.home() / "Downloads" / item.filename)
        chosen, _ = QFileDialog.getSaveFileName(self, "Save attachment", start)
        if not chosen:
            return
        self._write(Path(chosen), item)

    @Slot()
    def _save_all(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "Save every attachment into", str(Path.home() / "Downloads"))
        if not directory:
            return
        saved = 0
        for item in self._found:
            if not item.data:
                continue
            if item.executable and not self._confirm_program(item):
                continue
            self._write(attachments.unique_path(Path(directory), item.filename), item)
            saved += 1
        QMessageBox.information(
            self, "Saved", f"{saved} attachment{'s' if saved != 1 else ''} saved.")

    def _confirm_program(self, item: attachments.Attachment) -> bool:
        answer = QMessageBox.warning(
            self, "That is a program",
            f"{item.shown} is an executable, whatever its name suggests.\n\n"
            "It will be saved with the same quarantine flag a download gets, "
            "so macOS will check it before anything runs. Save it anyway?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        return answer == QMessageBox.StandardButton.Save

    def _write(self, path: Path, item: attachments.Attachment) -> None:
        try:
            path.write_bytes(item.data or b"")
            attachments.quarantine(path)
            self.status.setText(f"Saved to {path}")
        except OSError as exc:
            QMessageBox.warning(self, "Could not save", str(exc))

    @Slot()
    def _copy_image(self) -> None:
        item = self._current()
        if item is None or not item.data:
            return
        image = QImage()
        if image.loadFromData(item.data):
            QGuiApplication.clipboard().setImage(image)
            self.status.setText("Copied to the clipboard.")

    # -- tidying up -------------------------------------------------------
    def done(self, result: int) -> None:      # noqa: D102 - Qt's name
        self.audio.stop()
        QTimer.singleShot(0, self._sweep)
        super().done(result)

    def _sweep(self) -> None:
        for path in self._written:
            try:
                path.unlink()
            except OSError:
                pass
        try:
            self._temp.rmdir()
        except OSError:
            pass


def _describe(kind: str) -> str:
    return {
        "archive": "An archive.",
        "program": "A program.",
        "video": "A video file.",
        "other": "This is not a format the viewer opens.",
    }.get(kind, "This is not a format the viewer opens.")
