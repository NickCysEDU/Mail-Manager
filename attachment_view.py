"""A window for looking at what somebody attached, without running it.

The rule this is built around: **nothing here executes anything.** Images,
audio, PDFs and text render in-process; everything else is described and
offered as a save. Opening a saved file happens in Finder afterwards, against
the quarantine flag the save sets.

Three refusals that look like missing features and are not:

*SVG is shown as text.* It is a document format with script elements and
external references, and rendering one runs somebody's XML through a parser
that can fetch URLs. The markup is readable; that is enough.

*HTML is shown as text.* Rendering it loads remote images, which is how an
attachment tells the sender it was opened.

*Archives are never expanded.* Saving a zip is fine. Browsing one means
walking paths a stranger chose.

Bytes arrive one part at a time. The list is built from the server's
description of the message, which costs a fraction of a second, and a part is
only fetched when somebody looks at it - so a six megabyte message opens at
once rather than after all six megabytes.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Callable, List, Optional

from PySide6.QtCore import QSize, Qt, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import (QAction, QGuiApplication, QImage, QKeySequence,
                           QPixmap)
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog,
                               QDialogButtonBox, QFileDialog, QFrame,
                               QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QMessageBox, QPlainTextEdit,
                               QPushButton, QScrollArea, QSizePolicy,
                               QSlider, QStackedWidget, QVBoxLayout, QWidget)

import attachment_meta
import attachments
from attachment_widgets import SeekBar, Spectrum
from widgets import _html, system_font

#: Text longer than this is truncated on screen. A log file attached to a bug
#: report can be tens of megabytes and nobody reads it in a dialog.
TEXT_LIMIT = 400_000

#: Cover art is attacker-controlled bytes from inside another file, so it
#: gets its own ceiling on top of the image one.
MAX_ART_PIXELS = 16_000_000


def _muted(text: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("dim", "true")
    label.setWordWrap(True)
    return label


def _combo(options, tip: str) -> QComboBox:
    """A dropdown wide enough for its longest option, popup included.

    Qt sizes a combo to whatever is selected and lets the popup inherit that
    width, so a short current item clips every longer one in the list.
    """
    box = QComboBox()
    box.addItems(options)
    box.setToolTip(tip)
    box.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
    metrics = box.fontMetrics()
    widest = max((metrics.horizontalAdvance(option) for option in options),
                 default=60)
    box.setMinimumWidth(widest + 44)
    view = box.view()
    if view is not None:
        view.setMinimumWidth(widest + 32)
        view.setTextElideMode(Qt.TextElideMode.ElideNone)
    return box


class _FetchThread(QThread):
    """One part, off the interface thread.

    Fetching on the thread that handles clicks is why selecting an attachment
    stuttered: a megabyte and a half over IMAP is a second or two, and for
    that time nothing repaints. The window stays live now and says what it is
    waiting for.
    """

    done = Signal(int, object)      # row, bytes
    failed = Signal(int, str)

    def __init__(self, fetch, row: int, item, parent=None) -> None:
        super().__init__(parent)
        self._fetch = fetch
        self._row = row
        self._item = item

    def run(self) -> None:
        try:
            data = self._fetch(self._item)
        except Exception as exc:      # noqa: BLE001 - reported, never raised
            self.failed.emit(self._row, str(exc))
            return
        self.done.emit(self._row, data or b"")


class ImagePane(QWidget):
    """Any format Qt decodes, at whatever size you want to see it."""

    MODES = ("Fit", "Fill", "100%", "200%", "400%")

    def __init__(self) -> None:
        super().__init__()
        self._pixmap: Optional[QPixmap] = None
        self._mode = "Fit"

        self.mode_box = _combo(
            list(self.MODES),
            "How big the image is drawn. Fit shows all of it; Fill crops it "
            "to the window; the percentages are its own size.")
        self.mode_box.currentTextChanged.connect(self._set_mode)
        # Words, not symbols. A bare + next to a bare - tells a first-time
        # reader nothing about what it will do.
        self.zoom_out = QPushButton("Smaller")
        self.zoom_in = QPushButton("Larger")
        self.zoom_out.setToolTip("Step down through the sizes in the list.")
        self.zoom_in.setToolTip("Step up through the sizes in the list.")
        self.zoom_out.clicked.connect(lambda: self._step(-1))
        self.zoom_in.clicked.connect(lambda: self._step(1))

        self.area = QScrollArea()
        self.area.setWidgetResizable(True)
        self.area.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label = QLabel()
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.area.setWidget(self.label)

        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.addWidget(QLabel("Size"))
        bar.addWidget(self.mode_box)
        bar.addWidget(self.zoom_out)
        bar.addWidget(self.zoom_in)
        bar.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(bar)
        layout.addWidget(self.area, 1)

    def show_bytes(self, data: bytes) -> str:
        image = QImage()
        if not image.loadFromData(data):
            self.label.setText("This does not decode as an image.")
            self._pixmap = None
            return "not a readable image"
        pixels = image.width() * image.height()
        if pixels > attachments.MAX_PIXELS:
            self.label.setText(
                f"{image.width()} x {image.height()} is too large to open "
                "safely.\nSave it and use Preview.")
            self._pixmap = None
            return f"{image.width()}x{image.height()}, refused"
        self._pixmap = QPixmap.fromImage(image)
        self._apply()
        return f"{image.width()} x {image.height()}"

    @Slot(str)
    def _set_mode(self, mode: str) -> None:
        self._mode = mode
        self._apply()

    def _step(self, direction: int) -> None:
        order = list(self.MODES)
        try:
            index = order.index(self._mode)
        except ValueError:
            index = 0
        index = min(len(order) - 1, max(0, index + direction))
        self.mode_box.setCurrentText(order[index])

    def _apply(self) -> None:
        if self._pixmap is None:
            return
        area = self.area.viewport().size()
        mode = self._mode
        if mode == "Fit":
            scaled = self._pixmap.scaled(
                area, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
        elif mode == "Fill":
            scaled = self._pixmap.scaled(
                area, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation)
        else:
            factor = {"100%": 1.0, "200%": 2.0, "400%": 4.0}.get(mode, 1.0)
            target = QSize(int(self._pixmap.width() * factor),
                           int(self._pixmap.height() * factor))
            # Never build a pixmap bigger than the safety ceiling.
            if target.width() * target.height() > attachments.MAX_PIXELS:
                target = self._pixmap.size()
            scaled = self._pixmap.scaled(
                target, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
        self.label.setPixmap(scaled)
        self.label.resize(scaled.size())
        self.area.setWidgetResizable(mode in ("Fit", "Fill"))

    def resizeEvent(self, event) -> None:      # noqa: N802 - Qt's name
        super().resizeEvent(event)
        if self._mode in ("Fit", "Fill"):
            self._apply()


class TextPane(QWidget):
    """Always plain. HTML and SVG arrive here rather than at a renderer."""

    SIZES = (11, 13, 15, 18, 22)

    def __init__(self) -> None:
        super().__init__()
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self._size = 13
        self._wrapped = False

        self.wrap_button = QPushButton("Wrap")
        self.wrap_button.setCheckable(True)
        self.wrap_button.setToolTip(
            "Fold long lines so they fit the window instead of running off "
            "the right-hand side.")
        self.wrap_button.toggled.connect(self._set_wrap)
        smaller = QPushButton("Smaller text")
        larger = QPushButton("Larger text")
        smaller.setToolTip("Reduce the type size.")
        larger.setToolTip("Increase the type size.")
        smaller.clicked.connect(lambda: self._resize(-1))
        larger.clicked.connect(lambda: self._resize(1))

        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.addWidget(self.wrap_button)
        bar.addWidget(smaller)
        bar.addWidget(larger)
        bar.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(bar)
        layout.addWidget(self.view, 1)
        self._set_wrap(False)
        self._apply_font()

    def show_bytes(self, data: bytes) -> str:
        clipped = data[:TEXT_LIMIT]
        text = clipped.decode("utf-8", "replace")
        if len(data) > TEXT_LIMIT:
            text += f"\n\n[... {len(data) - TEXT_LIMIT:,} more bytes not shown]"
        self.view.setPlainText(text)
        lines = text.count("\n") + 1
        return f"{len(data):,} bytes, {lines:,} lines"

    @Slot(bool)
    def _set_wrap(self, wrapped: bool) -> None:
        self._wrapped = wrapped
        self.view.setLineWrapMode(
            QPlainTextEdit.LineWrapMode.WidgetWidth if wrapped
            else QPlainTextEdit.LineWrapMode.NoWrap)

    def _resize(self, direction: int) -> None:
        order = list(self.SIZES)
        nearest = min(order, key=lambda s: abs(s - self._size))
        index = min(len(order) - 1, max(0, order.index(nearest) + direction))
        self._size = order[index]
        self._apply_font()

    def _apply_font(self) -> None:
        font = self.view.font()
        font.setPointSize(self._size)
        self.view.setFont(font)


class AudioPane(QWidget):
    """Play it, see it, and read what the file says about itself."""

    def __init__(self) -> None:
        super().__init__()
        self._player = None
        self._audio = None
        self._decoder = None
        self._path: Optional[Path] = None

        self.art = QLabel()
        self.art.setFixedSize(112, 112)
        self.art.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.art.setScaledContents(False)
        self.art.hide()

        self.title = QLabel("")
        self.title.setWordWrap(True)
        self.tags = _muted("")

        self.spectrum = Spectrum()

        self.play = QPushButton("Play")
        self.play.setFixedWidth(84)
        self.position = SeekBar()
        self.clock = QLabel("0:00 / 0:00")
        self.clock.setFont(system_font())
        self.clock.setMinimumWidth(96)
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(70)
        self.volume.setFixedWidth(104)
        self.volume.setToolTip("Volume")

        header = QHBoxLayout()
        header.addWidget(self.art)
        text = QVBoxLayout()
        text.addWidget(self.title)
        text.addWidget(self.tags)
        text.addStretch(1)
        header.addLayout(text, 1)

        controls = QHBoxLayout()
        controls.addWidget(self.play)
        controls.addWidget(self.position, 1)
        controls.addWidget(self.clock)
        controls.addSpacing(10)
        controls.addWidget(QLabel("Vol"))
        controls.addWidget(self.volume)

        layout = QVBoxLayout(self)
        layout.addLayout(header)
        layout.addWidget(self.spectrum)
        layout.addLayout(controls)
        layout.addWidget(_muted(
            "Playback is local. Nothing about this file leaves the machine."))
        layout.addStretch(1)

        self.play.clicked.connect(self._toggle)
        self.position.seeked.connect(self._seek)
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

    def load(self, path: Path, item) -> str:
        self.stop()
        self._path = path
        self.title.setText(f"<b>{_html(item.shown)}</b>")

        data = item.data or b""
        tags, art = attachment_meta.audio_facts(data)
        wanted = [tags.get(k) for k in ("Artist", "Album", "Year") if tags.get(k)]
        self.tags.setText(_html(" · ".join(wanted)) if wanted else "")
        if tags.get("Title"):
            self.title.setText(
                f"<b>{_html(tags['Title'])}</b><br>"
                f"<span style='opacity:0.7'>{_html(item.shown)}</span>")
        self._show_art(art)

        if not self._ensure_player():
            self.play.setEnabled(False)
            return "audio playback is unavailable in this build"
        # A local file, never a URL from the message.
        self._player.setSource(QUrl.fromLocalFile(str(path)))
        self.play.setEnabled(True)
        self._start_analysis(path)
        return "ready"

    def _show_art(self, art: Optional[bytes]) -> None:
        """Cover art is bytes from inside another file. Treat it as hostile."""
        self.art.clear()
        self.art.hide()
        if not art or len(art) > attachment_meta.MAX_ART:
            return
        kind, _mime = attachments.sniff(art[:32])
        if kind != "image":
            return
        image = QImage()
        if not image.loadFromData(art):
            return
        if image.width() * image.height() > MAX_ART_PIXELS:
            return
        self.art.setPixmap(QPixmap.fromImage(image).scaled(
            self.art.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))
        self.art.show()

    def _start_analysis(self, path: Path) -> None:
        import attachment_audio

        self.spectrum.clear()

        def done(frames) -> None:
            self.spectrum.set_frames(frames, attachment_audio.RATE)
            self._decoder = None

        def failed(_detail: str) -> None:
            self._decoder = None

        self._decoder = attachment_audio.decode(path, done, failed)

    # -- transport --------------------------------------------------------
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
            self.spectrum.set_position(value)
            self._show_clock(value)

    @Slot(int)
    def _set_volume(self, value: int) -> None:
        if self._audio is not None:
            self._audio.setVolume(value / 100)

    @Slot(int)
    def _moved(self, value: int) -> None:
        self.position.report(value)
        self.spectrum.set_position(value)
        self._show_clock(self.position.value())

    @Slot(int)
    def _duration(self, value: int) -> None:
        self.position.setRange(0, value)
        self._show_clock(self.position.value())

    def _state(self, *_args) -> None:
        try:
            from PySide6.QtMultimedia import QMediaPlayer
            playing = (self._player.playbackState()
                       == QMediaPlayer.PlaybackState.PlayingState)
        except Exception:      # noqa: BLE001
            playing = False
        self.play.setText("Pause" if playing else "Play")
        self.spectrum.set_playing(playing)

    def _error(self, *_args) -> None:
        self.title.setText(self.title.text() + "  (this file will not play)")
        self.play.setEnabled(False)
        self.spectrum.clear()

    def _show_clock(self, position: int) -> None:
        self.clock.setText(f"{_mmss(position)} / {_mmss(self.position.maximum())}")

    def stop(self) -> None:
        self.spectrum.set_playing(False)
        self.spectrum.clear()
        self._decoder = None
        if self._player is not None:
            self._player.stop()
            self._player.setSource(QUrl())


def _mmss(ms: int) -> str:
    seconds = max(0, int(ms // 1000))
    return f"{seconds // 60}:{seconds % 60:02d}"


class PdfPane(QWidget):
    """Qt's own PDF view, which runs no JavaScript and fetches nothing."""

    MODES = {"Fit width": "FitToWidth", "Whole page": "FitInView",
             "Actual size": "Custom"}

    def __init__(self) -> None:
        super().__init__()
        self._document = None
        self._view = None
        self.mode_box = _combo(
            list(self.MODES),
            "How the page is sized. Fit width fills the window across; "
            "Whole page shows one page at a time.")
        self.mode_box.currentTextChanged.connect(self._set_mode)
        self.mode_box.setEnabled(False)

        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.addWidget(QLabel("Page size"))
        bar.addWidget(self.mode_box)
        bar.addStretch(1)

        self._fallback = _muted("PDF viewing is unavailable in this build.")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.addLayout(bar)
        self._layout.addWidget(self._fallback)

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
            self._layout.addWidget(self._view, 1)
            self.mode_box.setEnabled(True)
        self._document.load(str(path))
        self._set_mode(self.mode_box.currentText())
        pages = self._document.pageCount()
        return f"{pages} page{'s' if pages != 1 else ''}"

    @Slot(str)
    def _set_mode(self, label: str) -> None:
        if self._view is None:
            return
        from PySide6.QtPdfWidgets import QPdfView
        mode = getattr(QPdfView.ZoomMode, self.MODES.get(label, "FitToWidth"),
                       QPdfView.ZoomMode.FitToWidth)
        self._view.setZoomMode(mode)
        if label == "Actual size":
            self._view.setZoomFactor(1.0)


class MetadataPane(QPlainTextEdit):
    """What the file says about itself, as text nothing can render."""

    def __init__(self) -> None:
        super().__init__()
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)

    def show_for(self, item) -> None:
        rows = attachment_meta.facts_for(item)
        width = max((len(label) for label, _ in rows), default=0)
        # Plain text on purpose: these values were written by whoever sent
        # the file, and a rich text view would render whatever they chose.
        self.setPlainText("\n".join(f"{label.ljust(width)}   {value}"
                                    for label, value in rows))


class AttachmentViewer(QDialog):
    """The window. A list on the left, whatever it is on the right.

    ``fetch`` is called with one attachment and must return its bytes, or
    raise. It is only called for parts nobody has looked at yet, which is
    what keeps a six megabyte message from being downloaded to see the first
    thing in it.
    """

    def __init__(self, found: List[attachments.Attachment], subject: str = "",
                 parent=None, fetch: Optional[Callable] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Attachments")
        self.setMinimumSize(QSize(900, 600))
        self._found = list(found)
        self._fetch = fetch
        self._temp = Path(tempfile.mkdtemp(prefix="mm-attach-"))
        os.chmod(self._temp, 0o700)
        self._written: List[Path] = []
        self._last_dir = str(Path.home() / "Downloads")
        self._showing_metadata = False
        #: row -> thread, so a part is never fetched twice at once.
        self._fetching: dict = {}
        #: the row whose arrival should switch the view.
        self._awaiting: Optional[int] = None

        self.list = QListWidget()
        self.list.setFixedWidth(280)
        self.list.setToolTip(
            "Everything attached to this message. Pick one to look at it.")
        for item in self._found:
            entry = QListWidgetItem(_row_label(item))
            entry.setToolTip(_row_tooltip(item))
            self.list.addItem(entry)

        self.hint = _muted(
            "Click an attachment to open it. Nothing here is ever run, and "
            "nothing is saved unless you say so.\n"
            "Space plays audio · arrow keys move and scrub · Info shows "
            "details · Save keeps a copy.")

        self.image = ImagePane()
        self.audio = AudioPane()
        self.text = TextPane()
        self.pdf = PdfPane()
        self.meta = MetadataPane()
        self.blank = QWidget()
        blank_layout = QVBoxLayout(self.blank)
        blank_layout.addStretch(1)
        self.blank_label = QLabel("")
        self.blank_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.blank_label.setWordWrap(True)
        blank_layout.addWidget(self.blank_label)
        blank_layout.addStretch(1)

        self.stack = QStackedWidget()
        for pane in (self.blank, self.image, self.audio, self.text, self.pdf,
                     self.meta):
            self.stack.addWidget(pane)

        self.heading = QLabel("")
        self.heading.setTextFormat(Qt.TextFormat.RichText)
        self.heading.setWordWrap(True)
        self.status = _muted("")
        self.warning = QLabel("")
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet("color: #c65b4e;")
        self.warning.hide()

        self.info_button = QPushButton("Info")
        self.info_button.setCheckable(True)
        self.info_button.setToolTip(
            "What the file says about itself: type, size, checksum, and any "
            "tags it carries.")
        self.info_button.toggled.connect(self._toggle_metadata)
        self.save_button = QPushButton("Save…")
        self.save_button.setToolTip(
            "Keep a copy of this attachment. It is saved with the same "
            "quarantine mark a download gets, so macOS will check it.")
        self.save_all = QPushButton("Save all…")
        self.save_all.setToolTip(
            "Choose a folder and keep a copy of everything attached.")
        self.copy_button = QPushButton("Copy image")
        self.copy_button.setToolTip(
            "Put this image on the clipboard, to paste somewhere else.")
        self.copy_button.hide()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)

        actions = QHBoxLayout()
        actions.addWidget(self.info_button)
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

        left = QVBoxLayout()
        left.addWidget(self.list, 1)
        left.addWidget(self.hint)
        left_frame = QFrame()
        left_frame.setLayout(left)
        left_frame.setFixedWidth(296)

        body = QHBoxLayout(self)
        body.addWidget(left_frame)
        frame = QFrame()
        frame.setLayout(right)
        frame.setSizePolicy(QSizePolicy.Policy.Expanding,
                            QSizePolicy.Policy.Expanding)
        body.addWidget(frame, 1)

        self.list.currentRowChanged.connect(self._show)
        self.save_button.clicked.connect(self._save_current)
        self.save_all.clicked.connect(self._save_all)
        self.copy_button.clicked.connect(self._copy_image)
        self._add_shortcuts()

        if self._found:
            self.list.setCurrentRow(0)
        else:
            self.heading.setText("<b>Nothing is attached to this message.</b>")
            for button in (self.save_button, self.save_all, self.info_button):
                button.setEnabled(False)

    def _add_shortcuts(self) -> None:
        """Space plays, arrows move, the ordinary ones save and close."""
        def add(sequence, slot) -> None:
            action = QAction(self)
            action.setShortcut(QKeySequence(sequence))
            action.triggered.connect(slot)
            self.addAction(action)

        add("Space", self._space)
        add(QKeySequence.StandardKey.Save, self._save_current)
        add("Ctrl+I", lambda: self.info_button.toggle())
        add("Down", lambda: self._step_row(1))
        add("Up", lambda: self._step_row(-1))
        add("Right", lambda: self._nudge(5000))
        add("Left", lambda: self._nudge(-5000))

    def _space(self) -> None:
        if self.stack.currentWidget() is self.audio and self.audio.play.isEnabled():
            self.audio._toggle()

    def _nudge(self, delta: int) -> None:
        if self.stack.currentWidget() is self.audio:
            target = max(0, min(self.audio.position.maximum(),
                                self.audio.position.value() + delta))
            self.audio.position.setValue(target)
            self.audio._seek(target)

    def _step_row(self, direction: int) -> None:
        row = self.list.currentRow() + direction
        if 0 <= row < self.list.count():
            self.list.setCurrentRow(row)

    # -- showing one ------------------------------------------------------
    def _current(self) -> Optional[attachments.Attachment]:
        row = self.list.currentRow()
        return self._found[row] if 0 <= row < len(self._found) else None

    def _start_fetch(self, row: int, item, then_show: bool) -> None:
        """Ask for one part on a thread, and keep the window responsive."""
        if self._fetch is None or row in self._fetching:
            return
        thread = _FetchThread(self._fetch, row, item, self)
        self._fetching[row] = thread
        thread.done.connect(self._fetched)
        thread.failed.connect(self._fetch_failed)
        thread.finished.connect(lambda r=row: self._fetching.pop(r, None))
        if then_show:
            self._awaiting = row
        thread.start()

    @Slot(int, object)
    def _fetched(self, row: int, data: bytes) -> None:
        if not 0 <= row < len(self._found):
            return
        item = self._found[row]
        if data:
            self._found[row] = attachments.Attachment(
                part=item.part, name=item.name, content_type=item.content_type,
                encoding=item.encoding, size=len(data), cid=item.cid,
                inline=item.inline, data=data)
            self.list.item(row).setText(_row_label(self._found[row]))
        if self._awaiting == row:
            self._awaiting = None
            if self.list.currentRow() == row:
                self._render(row)
        self._prefetch_around(self.list.currentRow())

    @Slot(int, str)
    def _fetch_failed(self, row: int, detail: str) -> None:
        if self._awaiting == row:
            self._awaiting = None
            self.blank_label.setText(f"Could not fetch it.\n\n{detail}")
            self.stack.setCurrentWidget(self.blank)
            self.status.setText("")

    def _prefetch_around(self, row: int) -> None:
        """Quietly pull the neighbours, so stepping through is instant."""
        if self._fetch is None:
            return
        for offset in (1, -1):
            other = row + offset
            if 0 <= other < len(self._found) and self._found[other].data is None:
                if len(self._fetching) < 2:
                    self._start_fetch(other, self._found[other], then_show=False)

    @Slot(int)
    def _show(self, row: int) -> None:
        item = self._current()
        if item is None:
            return
        self.audio.stop()
        self.copy_button.hide()
        self.heading.setText(
            f"<b>{_html(item.shown)}</b><br>"
            f"{_html(item.content_type)} · {_size_label(item)}")

        if item.data is None:
            if self._fetch is None:
                self.blank_label.setText("Not downloaded.")
                self.stack.setCurrentWidget(self.blank)
                return
            self.warning.hide()
            self.blank_label.setText(
                f"Fetching {_size_label(item)}…\n\n"
                "The window stays usable while this happens.")
            self.stack.setCurrentWidget(self.blank)
            self.status.setText("")
            self._start_fetch(row, item, then_show=True)
            return
        self._render(row)
        self._prefetch_around(row)

    def _render(self, row: int) -> None:
        item = self._found[row] if 0 <= row < len(self._found) else None
        if item is None or item.data is None:
            return
        self.heading.setText(
            f"<b>{_html(item.shown)}</b><br>"
            f"{_html(item.content_type)} · {item.human_size()}")

        note = self._warning_for(item)
        self.warning.setText(note)
        self.warning.setVisible(bool(note))

        if self._showing_metadata:
            self.meta.show_for(item)
            self.stack.setCurrentWidget(self.meta)
            self.status.setText("")
            return

        data = item.data or b""
        kind, _mime = attachments.sniff(data[:32], item.content_type, item.name)
        if kind == "image" and item.ext != "svg":
            self.stack.setCurrentWidget(self.image)
            self.status.setText(self.image.show_bytes(data))
            self.copy_button.setVisible(True)
        elif kind == "audio":
            path = self._materialise(item)
            self.stack.setCurrentWidget(self.audio)
            self.status.setText(self.audio.load(path, item))
        elif kind == "pdf":
            path = self._materialise(item)
            self.stack.setCurrentWidget(self.pdf)
            self.status.setText(self.pdf.load(path))
        elif kind in ("text", "program") or item.ext in ("svg", "html", "htm", "xml"):
            self.stack.setCurrentWidget(self.text)
            self.status.setText(self.text.show_bytes(data))
        else:
            self.blank_label.setText(
                f"{_describe(kind)}\n\nSave it, or press Info to see what it is.")
            self.stack.setCurrentWidget(self.blank)
            self.status.setText("")

    @Slot(bool)
    def _toggle_metadata(self, showing: bool) -> None:
        self._showing_metadata = showing
        self._show(self.list.currentRow())

    @staticmethod
    def _warning_for(item) -> str:
        if item.executable:
            return ("This is a program, whatever it is called. Saving it is "
                    "fine; running it is not something this app will do, and "
                    "macOS will ask you about it if you try.")
        if item.archive:
            return ("An archive. It is saved as one file - nothing here opens "
                    "it, because what is inside chose its own paths.")
        if item.signature:
            return "A cryptographic signature part, not a document."
        if item.ext in ("svg", "html", "htm", "xml"):
            return ("Shown as text on purpose. Rendering it would run a "
                    "parser that can fetch remote content.")
        return ""

    def _materialise(self, item) -> Path:
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
        if item is None:
            return
        if item.data is None:
            self.status.setText("Still fetching that one - try again in a moment.")
            return
        if item.executable and not self._confirm_program(item):
            return
        chosen, _ = QFileDialog.getSaveFileName(
            self, "Save attachment", str(Path(self._last_dir) / item.filename))
        if not chosen:
            return
        self._last_dir = str(Path(chosen).parent)
        self._write(Path(chosen), item)

    @Slot()
    def _save_all(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "Save every attachment into", self._last_dir)
        if not directory:
            return
        self._last_dir = directory
        saved = 0
        skipped = 0
        for row, item in enumerate(self._found):
            if item.data is None:
                skipped += 1
                continue
            if item.executable and not self._confirm_program(item):
                continue
            self._write(attachments.unique_path(Path(directory), item.filename), item)
            saved += 1
        message = f"{saved} attachment{'s' if saved != 1 else ''} saved."
        if skipped:
            message += (f"\n\n{skipped} had not finished downloading. Open "
                        "them once, then save again.")
        QMessageBox.information(self, "Saved", message)

    def _confirm_program(self, item) -> bool:
        answer = QMessageBox.warning(
            self, "That is a program",
            f"{item.shown} is an executable, whatever its name suggests.\n\n"
            "It will be saved with the same quarantine flag a download gets, "
            "so macOS will check it before anything runs. Save it anyway?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        return answer == QMessageBox.StandardButton.Save

    def _write(self, path: Path, item) -> None:
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


def _row_tooltip(item) -> str:
    """What this attachment is, in a sentence, before it is opened."""
    kind = item.kind if item.data else _kind_from_type(item)
    said = {
        "image": "An image. It opens here.",
        "audio": "A sound file. It plays here.",
        "video": "A video. It can be saved, not played here.",
        "pdf": "A PDF. It opens here.",
        "text": "Text. It opens here.",
        "archive": "An archive. It can be saved; nothing here opens it.",
        "program": "A program. It can be saved, never run.",
    }.get(kind, "It can be saved; this is not a format the viewer opens.")
    return f"{item.shown}\n{_size_label(item)}\n{said}"


def _row_label(item) -> str:
    kind = item.kind if item.data else _kind_from_type(item)
    suffix = {"program": " · program", "archive": " · archive",
              "image": " · image", "audio": " · audio", "video": " · video",
              "pdf": " · PDF", "text": " · text"}.get(kind, "")
    return f"{item.shown}\n{_size_label(item)}{suffix}"


def _size_label(item) -> str:
    """The size of the file, not the size of the base64 that carried it.

    BODYSTRUCTURE reports the encoded length, which for base64 is a third
    larger than the thing itself. Showing that means the list says 2.5 MB
    and the saved file is 1.8 MB.
    """
    if item.data is not None:
        return item.human_size()
    size = item.size
    if (item.encoding or "").lower() == "base64" and size:
        size = int(size * 3 / 4)
    approximate = attachments.Attachment(
        part=item.part, name=item.name, content_type=item.content_type,
        size=size)
    return approximate.human_size()


#: What a suffix says, for the list before anything is downloaded. Servers
#: routinely declare application/octet-stream for an ordinary m4a.
_BY_SUFFIX = {
    "image": "png jpg jpeg gif webp heic heif tif tiff bmp avif",
    "audio": "mp3 m4a aac wav flac ogg oga opus aiff aif wma",
    "video": "mp4 mov m4v avi mkv webm wmv",
    "pdf": "pdf",
    "text": "txt md csv tsv log json xml yml yaml svg html htm",
    "archive": "zip tar gz tgz bz2 xz 7z rar",
    "program": "app exe dmg pkg sh command scpt jar msi",
}


def _kind_from_type(item) -> str:
    declared = (item.content_type or "").lower().split(";")[0]
    for prefix, kind in (("image/", "image"), ("audio/", "audio"),
                         ("video/", "video"), ("text/", "text")):
        if declared.startswith(prefix):
            return kind
    if declared == "application/pdf":
        return "pdf"
    suffix = item.ext
    if suffix:
        for kind, extensions in _BY_SUFFIX.items():
            if suffix in extensions.split():
                return kind
    return "other"


def _describe(kind: str) -> str:
    return {
        "archive": "An archive.",
        "program": "A program.",
        "video": "A video file.",
        "other": "This is not a format the viewer opens.",
    }.get(kind, "This is not a format the viewer opens.")
