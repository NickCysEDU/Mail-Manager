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

from PySide6.QtCore import (QEvent, QSize, Qt, QThread, QTimer, QUrl,
                            Signal, Slot)
from PySide6.QtGui import (QAction, QGuiApplication, QImage, QKeySequence,
                           QPixmap)
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog,
                               QDialogButtonBox, QFileDialog, QFrame,
                               QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QMessageBox, QPlainTextEdit,
                               QPushButton, QScrollArea, QSizePolicy,
                               QSlider, QStackedWidget, QVBoxLayout, QWidget)

import shiboken6

import attachment_meta
import attachments
from attachment_widgets import (FlowHolder, FlowRow, SeekBar, Spectrum,
                                Waveform)
from widgets import _html, system_font

#: Text longer than this is truncated on screen. A log file attached to a bug
#: report can be tens of megabytes and nobody reads it in a dialog.
TEXT_LIMIT = 400_000

#: Cover art is attacker-controlled bytes from inside another file, so it
#: gets its own ceiling on top of the image one.
MAX_ART_PIXELS = 16_000_000


def _hz(value) -> str:
    """A frequency as a person writes it: 630, 1k, 10k."""
    if value >= 1000:
        thousands = value / 1000.0
        return f"{thousands:.0f}k" if thousands == int(thousands) else f"{thousands:.1f}k"
    return f"{int(value)}"


class _Slider(QSlider):
    """A slider that goes back to where it started when double-clicked."""

    def __init__(self, default: int, *args) -> None:
        super().__init__(*args)
        self._default = int(default)

    def mouseDoubleClickEvent(self, event) -> None:      # noqa: N802 - Qt's
        self.setValue(self._default)
        self.sliderReleased.emit()
        event.accept()


def _muted(text: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("dim", "true")
    label.setWordWrap(True)
    return label


#: The size everything in the visualiser controls is written at.
CONTROL_POINT_SIZE = 11.5


def _match_text(root) -> None:
    """Give every control under ``root`` the same text size."""
    font = root.font()
    font.setPointSizeF(CONTROL_POINT_SIZE)
    root.setFont(font)
    for child in root.findChildren(QWidget):
        child.setFont(font)


def _labelled(text: str, control) -> QWidget:
    """A caption and its control as one thing.

    The row wraps, and a bare label followed by a bare slider could be
    split across the break - leaving a caption on one line and the slider
    it names on the next, which reads as two unrelated controls.
    """
    holder = QWidget()
    row = QHBoxLayout(holder)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(6)
    caption = QLabel(text)
    caption.setToolTip(control.toolTip())
    row.addWidget(caption)
    row.addWidget(control)
    return holder


#: The viewer's own choices that outlive it, beside the rider's bests: kept
#: apart from the mail settings, which the main window saves whole.
VIEWER_PREFS = "viewer.json"


def _viewer_prefs() -> dict:
    """What the viewer was left set to, or nothing."""
    import json

    import config

    try:
        with open(config.app_support_dir() / VIEWER_PREFS,
                  encoding="utf-8") as handle:
            found = json.load(handle)
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def _keep_viewer_pref(name: str, value) -> None:
    """Remember one of the viewer's choices for next time."""
    import json
    import logging

    import config

    path = config.app_support_dir() / VIEWER_PREFS
    prefs = _viewer_prefs()
    prefs[name] = value
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(".part")
        partial.write_text(json.dumps(prefs, indent=1), encoding="utf-8")
        partial.replace(path)
    except OSError as exc:
        logging.getLogger(__name__).info(
            "Could not keep the viewer's %s (%s).", name, exc)


def _relabel(holder: QWidget, text: str, tip: str) -> None:
    """Change what a ``_labelled`` control is called, and its tooltip.

    Two of these controls mean different things in Manual, and a slider
    captioned "sens" that sets the shape of a flash is worse than no
    caption at all.
    """
    row = holder.layout()
    caption = row.itemAt(0).widget()
    control = row.itemAt(1).widget()
    caption.setText(text)
    caption.setToolTip(tip)
    control.setToolTip(tip)


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


#: Transport symbols. Both are plain text, so they take the button's own
#: colour and scale with its font instead of needing a themed icon set.
#: How far J and L jump.
SKIP_MS = 10_000

PLAY_GLYPH = "\u25b6"
PAUSE_GLYPH = "\u275a\u275a"


def _name_transport(button, playing: bool) -> None:
    """Symbol on the button, word everywhere a word is still needed."""
    button.setText(PAUSE_GLYPH if playing else PLAY_GLYPH)
    word = "Pause" if playing else "Play"
    button.setToolTip(word)
    button.setAccessibleName(word)


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

    #: Playing or not, whenever that changes.
    playingChanged = Signal(bool)

    #: How loud the game's sounds are against the music, out of a hundred,
    #: until somebody moves the slider.
    EFFECTS = 50
    #: The narrowest the seek bar may be made, however small the window.
    SEEK_LEAST = 160
    #: Where it is kept. A new name when the default changes, so a level
    #: set while trying an older one is not carried over.
    EFFECTS_PREF = "effects_level"
    #: What each of the rider's games is, in a line, under the controls.
    GAME_ABOUT = {
        "Mono": "Take the coloured blocks and miss the grey ones. Colours "
                "in a row are worth more each; a grey breaks the chain.",
        "Ninja": "Mono with far more grey blocks. Coins beside each one pay "
                 "for riding close, and a clean run pays double.",
        "Wakeboard": "Mono, but the up arrow jumps off the road. A jump "
                     "where the road crests is worth the most.",
        "Puzzle": "Colours drop into a grid three wide. Three or more of a "
                  "colour touching clear and score; greys clutter it.",
    }

    def __init__(self) -> None:
        super().__init__()
        self._player = None
        self._audio = None
        self._decoder = None
        self._path: Optional[Path] = None
        #: Bumped per file, so a late analysis for an earlier one is dropped.
        self._analysis_token = 0

        self.art = QLabel()
        self.art.setFixedSize(44, 44)
        self.art.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.art.setScaledContents(False)
        self.art.hide()

        self.title = QLabel("")
        self.title.setWordWrap(True)
        self.tags = _muted("")

        self.spectrum = Spectrum()
        import attachment_audio
        import visualizers

        self.scene_box = _combo(
            [scene.name for scene in visualizers.SCENES],
            "Which visualiser to draw. All of them read the same equaliser.")
        self.scene_box.currentTextChanged.connect(self._scene_chosen)
        self.enable_box = QCheckBox("Visualiser")
        self.enable_box.setToolTip(
            "Draw the music while it plays. Off by default.")
        self.enable_box.toggled.connect(self._enable_visualiser)
        self.strobe_box = QCheckBox("Strobe")
        self.strobe_box.setToolTip(
            "Flash the scene on the beat. Off by default.\n\n"
            "High Rate and Sensitivity reach about ten flashes a second. "
            "This can trigger photosensitive epilepsy.")
        self.strobe_box.toggled.connect(self.spectrum.set_strobe)
        self.colour_button = QPushButton("Colours")
        self.colour_button.setToolTip(
            "Meter colours and frequencies")
        self.colour_button.clicked.connect(self._choose_colours)
        self.full_button = QPushButton("Full screen")
        self.full_button.setToolTip(
            "Full screen (F). Escape or F comes back.")
        # Words, not symbols. A glyph saves eighty pixels and costs
        # anybody who has not met it before knowing what the button does,
        # which is the wrong trade for a control somebody uses once.
        for button in (self.colour_button, self.full_button):
            button.setAccessibleName(button.text())
        self.full_button.clicked.connect(self._go_full_screen)
        self.spectrum.set_labels([_hz(c) for c in attachment_audio.CENTRES])

        from attachment_widgets import Spectrum as _Spectrum

        self.shape_box = _combo(
            [name for name, _ in _Spectrum.SHAPES],
            "How tall the visualiser is. Portrait suits the dials and the "
            "tunnel; the strip keeps it out of the way.")
        self.shape_box.currentTextChanged.connect(self._shape_chosen)

        # Two controls, not one. They do different things and lumping them
        # behind a single "Flash" slider made both of them hard to find.

        self.sense = QSlider(Qt.Orientation.Horizontal)
        self.sense.setRange(0, 100)
        self.sense.setValue(50)
        self.sense.setFixedWidth(74)
        self.sense.setToolTip(
            "How big a jump in the bass counts as a hit. Right of centre "
            "catches a soft beat; left waits for something obvious.")
        self.sense.valueChanged.connect(
            lambda value: self.spectrum.set_strobe_sense(value / 100.0))

        self.flash = QSlider(Qt.Orientation.Horizontal)
        self.flash.setRange(0, 100)
        self.flash.setValue(50)
        self.flash.setFixedWidth(74)
        self.flash.setToolTip(
            "How soon after one flash the next may fire, from every few "
            "bars to every beat it can find.")
        self.flash.valueChanged.connect(
            lambda value: self.spectrum.set_strobe_rate(value / 100.0))

        self.decay = QSlider(Qt.Orientation.Horizontal)
        self.decay.setRange(3, 150)
        self.decay.setValue(28)
        self.decay.setFixedWidth(74)
        self.decay.setToolTip(
            "How long the oscilloscope's phosphor keeps glowing, from a "
            "hundredth of a second to a second and a half.")
        self.decay.valueChanged.connect(
            lambda value: self.spectrum.set_decay(value / 100.0))

        # Words, not prepositions. "on" and "every" were shorter and told
        # nobody what the slider under them did.
        self.sense_box = _labelled("Sensitivity", self.sense)
        self.rate_box = _labelled("Rate", self.flash)
        self.decay_box = _labelled("Glow", self.decay)

        import visualizers as _vis

        self.mode_box = _combo(
            list(_vis.by_name("Oscilloscope").MODES),
            "How the beam is driven. Sweep goes round once a frame; X-Y "
            "plots left against right, which is what a record cut for a "
            "scope draws its picture in.")
        self.mode_box.currentTextChanged.connect(self.spectrum.set_scope_mode)
        self.mode_box_holder = _labelled("Beam", self.mode_box)

        # And the rider's two games, which share a road and score
        # nothing alike. Its own box rather than the scope's, because the
        # two lists have nothing to do with each other and a combo that
        # is refilled when the scene changes loses what was chosen.
        self.game_box = _combo(
            list(_vis.by_name("Music rider").MODES),
            "Which game to play on the road.\n\n"
            "Mono: colours are points on a chain and greys are hazards. "
            "A chain breaks when you take a grey.\n\n"
            "Ninja: the same, with far more to dodge and twice as much "
            "for getting down the track without touching one.\n\n"
            "Wakeboard: Mono, but the up arrow leaves the road. A jump "
            "taken where the road crests is worth the most.\n\n"
            "Puzzle: a colour is worth nothing until three of them touch "
            "in the grid, and the grid is three columns deep by six.")
        self.game_box.currentTextChanged.connect(self.spectrum.set_scope_mode)
        # Not hidden itself: its holder is what comes and goes, and a
        # combo hidden inside a holder that is shown stays hidden - which
        # is how the game could not be chosen in the window at all, only
        # the word "game" with nothing after it.
        self.game_box_holder = _labelled("Game", self.game_box)
        # How hard it is. The rider is one scene for the session, so it is
        # told directly; a new level is a new run, as a new game is.
        rider = _vis.by_name("Music rider")
        self.level_box = _combo(
            list(rider.DIFFICULTIES),
            "How hard the game is.\n\n"
            "Easy: half the obstacles, more room between everything, four "
            "beats of warning and a shield that comes back quickly.\n\n"
            "Normal: as the game was built.\n\n"
            "Hard: more obstacles closer together, less warning, and a "
            "slower shield.\n\n"
            "Expert: the most obstacles, two beats of warning and no shield "
            "at all. Worth half as much again.")
        self.level_box.setAccessibleName("Difficulty")
        kept = _viewer_prefs().get("difficulty")
        self.level_box.setCurrentText(kept if kept in rider.DIFFICULTIES
                                      else "Normal")
        rider.set_difficulty(self.level_box.currentText())
        self.level_box.currentTextChanged.connect(self._level_chosen)
        self.level_box_holder = _labelled("Level", self.level_box)
        # The rider's own sounds: a note for every block taken, climbing
        # as a run goes on, and a thump with the music ducking under it
        # when one is hit. On unless somebody says otherwise. See
        # rider_sound.
        self.sound_box = QCheckBox("Sounds")
        self.sound_box.setChecked(True)
        self.sound_box.setToolTip(
            "The game's own sounds: a note for each block you take, rising "
            "as the chain grows, and a thump for a hit. Key X.")
        self.sound_box.toggled.connect(self._sounds_changed)
        # How loud those are against the music: a share of the player's
        # own volume, so the balance stays where it was put when the music
        # is turned up or down. Remembered, and heard as it is let go.
        self.effects = _Slider(self.EFFECTS, Qt.Orientation.Horizontal)
        self.effects.setRange(0, 100)
        kept = _viewer_prefs().get(self.EFFECTS_PREF, self.EFFECTS)
        self.effects.setValue(int(kept) if isinstance(kept, (int, float))
                              and 0 <= kept <= 100 else self.EFFECTS)
        self.effects.setFixedWidth(74)
        self.effects.setToolTip(
            "How loud the game's sounds are against the music. Half by "
            "default; double-click to go back to half.")
        self.effects.setAccessibleName("Effects volume")
        self._keep_effects = QTimer(self)
        self._keep_effects.setSingleShot(True)
        self._keep_effects.setInterval(400)
        self._keep_effects.timeout.connect(
            lambda: _keep_viewer_pref(self.EFFECTS_PREF, self.effects.value()))
        self.effects.valueChanged.connect(self._keep_effects.start)
        self.effects.sliderReleased.connect(self._preview_effects)
        # The tick box names the slider: the game's sounds, on or off, and
        # how loud. Apart, a wrapping row could put them on different lines.
        self.effects_box = QWidget()
        pair = QHBoxLayout(self.effects_box)
        pair.setContentsMargins(0, 0, 0, 0)
        pair.setSpacing(6)
        pair.addWidget(self.sound_box)
        pair.addWidget(self.effects)
        self.sound_box.toggled.connect(self.effects.setEnabled)
        self._board = None
        #: The track being played, as the bests know it, and the result
        #: last offered to them. See _keep_best.
        self._track_key = None
        self._kept_result = None
        self._best_keeper = None
        # The tick box and the two sliders that shape it, as one block: on
        # their own the sliders said "Sensitivity" and "Rate" with nothing
        # to say what of.
        from attachment_widgets import Spectrum as _Spec

        self.strobe_source = _combo(
            list(_Spec.STROBE_SOURCES),
            "Which part of the sound sets the strobe off.\n\n"
            f"{_Spec.BY_HAND}: only the keys flash it.\n\n"
            f"{AudioPane.BY_HAND_KEY} held is a light that stays on. "
            f"{AudioPane.SPAM_KEY} held is a strobe, twelve a second. "
            f"Both work on the other settings too.")
        self.strobe_source.currentTextChanged.connect(
            self.spectrum.set_strobe_source)
        # What to press, said where the choice is made.
        #
        # Picking "Manual" turns the automatic strobe off and leaves
        # nothing on screen to say what turns it on. It was in the full
        # screen key card, behind ?, which is no use to somebody who has
        # just chosen it from a menu and is waiting for something to
        # happen.
        self.by_hand = QLabel(
            f"{AudioPane.BY_HAND_KEY} hold · {AudioPane.SPAM_KEY} strobe")
        self.by_hand.setFont(system_font())
        self.by_hand.setStyleSheet("color: #8fd0ff;")
        self.by_hand.setToolTip(
            "Tap it for a flash, hold it for a held light.")
        self.by_hand.setVisible(False)
        self.strobe_source.currentTextChanged.connect(self._show_by_hand)
        self.source_box = _labelled("Listens to", self.strobe_source)
        # When a scene sets the strobe up for itself, the controls have to
        # move with it, or they show one thing while another happens.
        self.spectrum.strobe_settings_changed.connect(self._show_strobe)

        # The picture's own settings, in one panel under one button: its
        # shape, how one scene gives way to the next, the strobe, and its
        # timing against the sound. Each was a control of its own in the
        # row under the picture, and few people touch any of them.
        from PySide6.QtWidgets import QGridLayout, QMenu, QWidgetAction

        changes = [name for name, _ in _Spectrum.CHANGES]
        self.change_box = _combo(
            changes,
            "How one scene gives way to the next, chosen from the menu or "
            "with the number keys. Cut changes at once.")
        self.change_box.setAccessibleName("Scene change")
        kept = _viewer_prefs().get(self.CHANGE_PREF)
        if kept in changes:
            self.change_box.setCurrentText(kept)
        self._change_chosen(self.change_box.currentText(), keep=False)
        self.change_box.currentTextChanged.connect(self._change_chosen)

        # When the picture is shown against the sound: see av_sync.
        self.timing_said = QLabel(self.timing_words(self.sync_trim()))
        self.timing_slider = QSlider(Qt.Orientation.Horizontal)
        self.timing_slider.setRange(-self.SYNC_MOST, self.SYNC_MOST)
        self.timing_slider.setSingleStep(5)
        self.timing_slider.setPageStep(25)
        self.timing_slider.setValue(self.sync_trim())
        self.timing_slider.setMinimumWidth(200)
        self.timing_slider.setAccessibleName("Picture against the music")
        self.timing_slider.setToolTip(
            "Move the picture against the music, if the beat you see is "
            "not the beat you hear. Right is earlier, left later.")
        self.timing_slider.valueChanged.connect(self._timing_moved)
        timing_back = QPushButton("Back to 0")
        timing_back.clicked.connect(lambda: self.timing_slider.setValue(0))
        self.timing_note = _muted("")
        self.timing_note.setWordWrap(False)

        def line(*widgets) -> QWidget:
            holder = QWidget()
            row = QHBoxLayout(holder)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(12)
            for widget in widgets:
                row.addWidget(widget)
            row.addStretch(1)
            return holder

        self.picture_panel = QWidget()
        grid = QGridLayout(self.picture_panel)
        grid.setContentsMargins(16, 12, 16, 14)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(8)
        rows = ((QLabel("Shape"), self.shape_box),
                (QLabel("Scene change"), self.change_box),
                None,
                (self.strobe_box, self.source_box),
                (None, line(self.sense_box, self.rate_box)),
                (None, self.by_hand),
                None,
                (QLabel("Timing"), self.timing_said),
                (None, line(self.timing_slider, timing_back)),
                (None, self.timing_note))
        for index, row in enumerate(rows):
            if row is None:
                grid.setRowMinimumHeight(index, 6)
                continue
            name, control = row
            if name is not None:
                grid.addWidget(name, index, 0, Qt.AlignmentFlag.AlignRight
                               | Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(control, index, 1, Qt.AlignmentFlag.AlignLeft
                           | Qt.AlignmentFlag.AlignVCenter)
        grid.setColumnStretch(1, 1)
        self._picture_menu = QMenu(self)
        holder = QWidgetAction(self._picture_menu)
        holder.setDefaultWidget(self.picture_panel)
        self._picture_menu.addAction(holder)
        self._picture_menu.aboutToShow.connect(self._show_timing_note)
        self.picture_button = QPushButton("Picture…")
        self.picture_button.setAccessibleName("Picture")
        self.picture_button.setToolTip(
            "The picture's shape, how one scene gives way to the next, the "
            "strobe (S) and the picture's timing against the music.")
        self.picture_button.clicked.connect(self._show_picture_panel)

        # What belongs to one scene, and is there only while it is the
        # scene: the rider's game, the scope's beam, the meters' colours.
        # Each its own item in the row, so the row can wrap between them.
        self.scene_controls = {
            "Music rider": (self.game_box_holder, self.level_box_holder,
                            self.effects_box),
            "Oscilloscope": (self.mode_box_holder, self.decay_box),
            "VU meters": (self.colour_button,),
        }
        for controls in self.scene_controls.values():
            for widget in controls:
                widget.hide()

        # The visualiser's own line - on or off, which scene, and the
        # picture's settings and full screen at the far end - with the
        # scene's own controls under it, on a line that wraps.
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(12)
        top.addWidget(self.enable_box)
        top.addWidget(self.scene_box)
        top.addStretch(1)
        top.addWidget(self.picture_button)
        top.addWidget(self.full_button)
        self.visual_row = FlowRow(spacing=14)
        for controls in self.scene_controls.values():
            for widget in controls:
                self.visual_row.addWidget(widget)
        self.scene_row = FlowHolder(self.visual_row)
        self.scene_row.hide()
        self.visual_holder = QWidget()
        rows = QVBoxLayout(self.visual_holder)
        rows.setContentsMargins(0, 0, 0, 0)
        rows.setSpacing(8)
        rows.addLayout(top)
        rows.addWidget(self.scene_row)
        self.game_about = _muted(self.GAME_ABOUT.get(
            self.game_box.currentText(), ""))
        self.game_about.setAccessibleName("What this game is")
        self.game_about.hide()
        self.game_box.currentTextChanged.connect(
            lambda name: self.game_about.setText(self.GAME_ABOUT.get(name, "")))
        rows.addWidget(self.game_about)
        # One size for the whole section. Checkboxes, combo boxes, buttons
        # and plain labels each come with their own idea of how big their
        # text should be, and side by side in one row that reads as a mess.
        _match_text(self.visual_holder)
        _match_text(self.picture_panel)
        self._visual_controls = (self.scene_box, self.picture_button,
                                 self.full_button,
                                 *(widget for controls in
                                   self.scene_controls.values()
                                   for widget in controls))
        # Everything except the tick box starts unavailable, because the
        # visualiser starts off.
        self._grey_visual_controls(False)

        self._playing = False
        self._full_play = None
        self.play = QPushButton(PLAY_GLYPH)
        self.play.setFixedWidth(52)
        _name_transport(self.play, False)
        self.position = SeekBar()
        # Above the bar: what the track looks like, so seeking is aiming
        # rather than guessing. Windowed only - the full-screen view has
        # its own bar and no room for furniture.
        self.wave = Waveform()
        self.position.setMinimumWidth(self.SEEK_LEAST)
        self.wave.setMinimumWidth(self.SEEK_LEAST)
        self.clock = QLabel("0:00 / 0:00")
        self.clock.setFont(system_font())
        self.clock.setMinimumWidth(84)
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(70)
        self.volume.setFixedWidth(84)
        self.volume.setToolTip("Volume")

        # What the track is, beside a thumbnail of its cover. In place of
        # the window's own heading rather than under it: two headings over
        # one picture was a line of type the picture could have had.
        self.header_row = QWidget()
        header = QHBoxLayout(self.header_row)
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(10)
        header.addWidget(self.art)
        text = QVBoxLayout()
        text.setSpacing(0)
        text.addStretch(1)
        text.addWidget(self.title)
        text.addWidget(self.tags)
        text.addStretch(1)
        header.addLayout(text, 1)
        self.header_row.hide()

        controls = QHBoxLayout()
        # As tall with the slider as with the waveform that replaces it,
        # so the line does not change height, and move everything under
        # it, once a track has been read.
        controls.addStrut(self.wave.minimumHeight())
        controls.addWidget(self.play)
        # The waveform *is* the seek bar once there is one: it says where
        # you are and what is there, which a slider cannot. The slider
        # stays for the rest of the time - a track nobody has analysed
        # has no shape to draw, and a transport with nothing to drag is
        # worse than a plain one.
        controls.addWidget(self.position, 1)
        controls.addWidget(self.wave, 1)
        controls.addWidget(self.clock)
        controls.addSpacing(8)
        controls.addWidget(QLabel("Volume"))
        controls.addWidget(self.volume)

        layout = QVBoxLayout(self)
        # Level with the list beside it: the header is the top of the pane.
        # The other sides stay the style's, which depend on where the pane
        # ends up (-1 asks for them then).
        layout.setContentsMargins(-1, 0, -1, -1)
        layout.addWidget(self.header_row)
        # No stretch. With one it competed with the spacer at the foot of
        # this layout and took half the spare room rather than the height
        # its shape asks for. Its maximum caps it and its floor lets it
        # give way, which is all the control that is needed.
        layout.addWidget(self.spectrum)
        # A gap under the picture. Packed tight the transport's first row
        # of pixels landed on the scene's last one - not enough to see,
        # but they should not be touching either.
        layout.addSpacing(8)
        layout.addLayout(controls)
        # Below the transport, outside the picture. Putting them inside the
        # visualiser frame meant they were hidden whenever it was, and they
        # were hidden by a one-shot timer that fired before the analysis
        # finished - so they never came back.
        layout.addWidget(self.visual_holder)
        layout.addStretch(1)

        self.play.clicked.connect(self._toggle)
        self.position.seeked.connect(self._seek)
        self.wave.seeked.connect(self._seek)
        # One of the two is up at a time, and the waveform decides which.
        self.wave.shapeChanged.connect(self._show_scrubber)
        self._show_scrubber(False)
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
        # The picture shows what is heard when it is seen, not what has
        # been handed to the audio device - see av_sync.
        import av_sync

        self.spectrum.allowance = av_sync.Allowance()
        self.spectrum.allowance.trim = self.sync_trim() / 1000.0
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
        data = item.data or b""
        tags, art = attachment_meta.audio_facts(data)
        # Its own name for itself where it has one, and who made it; the
        # file's name and size where it does not.
        self.title.setText(f"<b>{_html(tags.get('Title') or item.shown)}</b>")
        wanted = [tags.get(k) for k in ("Artist", "Album", "Year") if tags.get(k)]
        self.tags.setText(_html(" · ".join(wanted)) if wanted else
                          f"{_html(item.content_type)} · {item.human_size()}")
        self._show_art(art)
        self.header_row.setVisible(True)

        if not self._ensure_player():
            self.play.setEnabled(False)
            return "audio playback is unavailable in this build"
        # A local file, never a URL from the message.
        self._player.setSource(QUrl.fromLocalFile(str(path)))
        self.spectrum.follow(lambda: self._player.position() if self._player else 0)
        self.play.setEnabled(True)
        self._sync_visual_controls()
        if self.enable_box.isChecked():
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

    def _cancel_analysis(self) -> None:
        """Stop any analysis in flight and forget it."""
        handle, self._decoder = self._decoder, None
        if handle is not None:
            cancel = getattr(handle, "cancel", None)
            if cancel is not None:
                cancel()

    def _start_analysis(self, path: Path) -> None:
        """Decode and analyse in the background, and survive being closed.

        The decoder finishes on its own schedule, which may be after the
        window has gone. Calling into a deleted widget from that callback
        raises out of Qt's event loop, so both ends are checked: a token
        that changes when another file is loaded, and shiboken's own
        liveness check on the widget itself.
        """
        import attachment_audio

        # Open if it was: the progress bar takes the picture's place, rather
        # than the picture closing and everything under it moving up.
        self.spectrum.clear(keep_open=True)
        self.wave.clear()
        self._analysis_token += 1
        token = self._analysis_token

        def alive() -> bool:
            if token != self._analysis_token:
                return False
            try:
                import shiboken6

                return shiboken6.isValid(self) and shiboken6.isValid(self.spectrum)
            except Exception:      # noqa: BLE001 - assume alive without it
                return True

        def done(result) -> None:
            if not alive():
                return
            frames, shapes, vectors, calibration, beats = result
            self.spectrum.set_calibration(calibration)
            self.spectrum.set_working(None)
            # Unless they came early and these are the same maps again:
            # setting them twice starts the strobe's count over mid-song.
            if beats != early.get("maps"):
                self.spectrum.set_beats(beats)
            self.spectrum.set_traces(shapes, vectors)
            # Set again, because the position the pane is at has to be
            # read against the finished list rather than the one that
            # arrived early. It is the same list on an uninterrupted run,
            # and then there is nothing to do: the pane knows, and the
            # waveform is not worked out a second time.
            if frames is not self.spectrum.frames_list():
                self.spectrum.set_frames(frames, attachment_audio.RATE)
                self.wave.set_shape(
                    attachment_audio.outline(frames, calibration))
            self._decoder = None

        def bands(result) -> None:
            """The picture, as soon as there is one to show.

            Eight of the nine scenes draw from the bands alone, and the
            two passes that follow take another two thirds as long again -
            so waiting for all of it before showing anything is most of
            the wait anybody sees. The scene starts here and the traces
            arrive underneath it a moment later.
            """
            if not alive():
                return
            frames, calibration, shape = result
            self.spectrum.set_calibration(calibration)
            self.spectrum.set_working(None)
            self.spectrum.set_frames(frames, attachment_audio.RATE)
            self.spectrum.set_contour(shape)
            import rider_bests

            self._track_key = rider_bests.fingerprint(frames)
            self.wave.set_shape(attachment_audio.outline(frames, calibration))

        def failed(_detail: str) -> None:
            if alive():
                self._decoder = None
                self.spectrum.set_working(None)
                # Nothing more is coming, the drums' beat included.
                self.spectrum.set_rhythm(None)

        def progress(fraction: float) -> None:
            if alive():
                self.spectrum.set_working(fraction)

        # Any earlier analysis is told to stop rather than left to finish a
        # file nobody is looking at.
        self._cancel_analysis()
        self.spectrum.set_working(0.0)
        self.spectrum.expect_rhythm()
        def kit(elements) -> None:
            """The drums, which arrive a few seconds after the picture."""
            if alive():
                self.spectrum.set_elements(elements)

        def harmonised(found) -> None:
            """The key and the chords, which the game's sounds are
            played in. See harmony."""
            if alive():
                self.spectrum.set_harmony(found)
                if self._board is not None:
                    self._board.set_harmony(found)

        early: dict = {}

        def beaten(maps) -> None:
            """The beats, straight after the picture: the game's road is
            laid on them, so the sooner the better."""
            if alive():
                early["maps"] = maps
                self.spectrum.set_beats(maps)

        def rhythmic(found) -> None:
            """The drums' own tempo and beat, which the game lays its
            road on in preference to the beat maps'."""
            if alive():
                self.spectrum.set_rhythm(found)

        self._decoder = attachment_audio.decode(path, done, failed, progress,
                                                kit, bands, harmonised, beaten,
                                                rhythmic)

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
            self.spectrum.seek_to(value)
            self.wave.set_position(value)
            self._show_clock(value)

    @Slot(int)
    def _set_volume(self, value: int) -> None:
        if self._audio is not None:
            self._audio.setVolume(value / 100)

    @Slot(bool)
    def _show_scrubber(self, drawn: bool) -> None:
        """Put up whichever of the two can do the job."""
        self.position.setVisible(not drawn)
        self.wave.setVisible(drawn)

    @Slot(int)
    def _moved(self, value: int) -> None:
        self.position.report(value)
        self.spectrum.set_position(value)
        self.wave.set_position(value)
        self._show_clock(self.position.value())

    @Slot(int)
    def _duration(self, value: int) -> None:
        self.position.setRange(0, value)
        self.wave.set_span(value)
        self._show_clock(self.position.value())

    @Slot(bool)
    def _grey_visual_controls(self, on: bool) -> None:
        """Off should look off.

        Disabling alone still reads as available on this platform, so the
        whole group is dimmed as well - there is no point offering a full
        screen button for something that is not being drawn.
        """
        for widget in self._visual_controls:
            widget.setEnabled(on)
            widget.setGraphicsEffect(None)
        # The panel's controls too: the strobe, which has a key of its own,
        # is not something to be set for a picture that is not there.
        self.picture_panel.setEnabled(on)
        # Hidden rather than dimmed. Half a dozen greyed-out controls is
        # more to read than none, and none of them can be used.
        self._show_visual_controls(on)
        self.visual_row.invalidate()

    def _show_visual_controls(self, on: bool) -> None:
        scene = self.scene_box.currentText()
        owned = {}
        for name, controls in self.scene_controls.items():
            for widget in controls:
                owned[widget] = name
        for widget in self._visual_controls:
            widget.setVisible(on and owned.get(widget, scene) == scene)
        # The scene's line goes with its last control, or an empty line
        # would still be given its gap.
        self.scene_row.setVisible(on and scene in self.scene_controls)
        self.game_about.setVisible(on and scene == "Music rider")

    def _enable_visualiser(self, on: bool) -> None:
        """Off means off: no decode, no timer, no widget with a height.

        Analysing a track costs a few seconds of one core and a few hundred
        kilobytes. Nobody should pay that for something they have switched
        off, so the decode only starts when this is ticked.
        """
        self._grey_visual_controls(on)
        self._apply_budget()
        if not on:
            self._analysis_token += 1
            self._cancel_analysis()
            self.spectrum.set_working(None)
            self.spectrum.set_playing(False)
            self.spectrum.clear()
            self.wave.clear()
            return
        if self._path is not None:
            self._start_analysis(self._path)
            from PySide6.QtMultimedia import QMediaPlayer
            if (self._player is not None
                    and self._player.playbackState()
                    == QMediaPlayer.PlaybackState.PlayingState):
                self.spectrum.set_playing(True)

    def _spectrum_budget(self, fresh: bool = True) -> int:
        """How much height the scene may have without evicting anything.

        Asked of the layout rather than totted up by hand: whatever the
        layout says it needs at minimum, less what the scene is currently
        claiming, is what everything else needs. The rest is the scene's.

        A shape that wanted more than this used to take it anyway - the
        layout could not fit the transport underneath and drew it on top
        of the picture instead.
        """
        layout = self.layout()
        if layout is None:
            return self.HEIGHT if hasattr(self, "HEIGHT") else 240
        # Ask for a fresh answer. invalidate() only marks the layout dirty;
        # minimumSize() keeps handing back the old number until it is made
        # to recompute, so a control row that had just grown was measured
        # at its previous height and the scene was given room that was no
        # longer there.
        if fresh:
            self.visual_row.invalidate()
            layout.activate()
        # At this width. The plain minimum measures a row that wraps at
        # whatever width it last had, which after a resize is the old one:
        # the scene was short by a line of controls on a window made
        # wider, and the room it should have had sat empty underneath.
        needed_by_everything = layout.minimumHeightForWidth(self.width())
        if needed_by_everything < 0:
            needed_by_everything = layout.minimumSize().height()
        claimed_by_scene = self.spectrum.minimumHeight()
        needed_by_the_rest = max(0, needed_by_everything - claimed_by_scene)
        return max(120, self.height() - needed_by_the_rest - 8)

    def _apply_budget(self, fresh: bool = True) -> None:
        budget = self._spectrum_budget(fresh)
        if fresh or max(80, budget) != self.spectrum._budget:
            self.spectrum.set_budget(budget)

    def resizeEvent(self, incoming) -> None:      # noqa: N802 - Qt's name
        super().resizeEvent(incoming)
        self._apply_budget()

    @Slot(str)
    def _shape_chosen(self, name: str) -> None:
        from attachment_widgets import Spectrum as _Spectrum

        for label, ratio in _Spectrum.SHAPES:
            if label == name:
                self._apply_budget()
                self.spectrum.set_aspect(ratio)
                return

    @Slot(str)
    def _show_strobe(self, source: str, rate: float, sense: float) -> None:
        """Move the controls to where a scene has just put the strobe.

        Without blocking their signals this would come straight back as
        "the user moved a slider", which is the one thing that stops a
        scene setting itself up at all.
        """
        for widget, value in ((self.flash, int(round(rate * 100))),
                              (self.sense, int(round(sense * 100)))):
            widget.blockSignals(True)
            widget.setValue(value)
            widget.blockSignals(False)
        index = self.strobe_source.findText(source)
        if index >= 0:
            self.strobe_source.blockSignals(True)
            self.strobe_source.setCurrentIndex(index)
            self.strobe_source.blockSignals(False)

    def _scene_chosen(self, name: str) -> None:
        import visualizers

        scene = visualizers.by_name(name)
        self.spectrum.set_scene(scene)
        self._sounds_changed()
        # Only the meters have colours to set, so the button only appears
        # when there is something for it to do.
        self._show_visual_controls(self.enable_box.isChecked())
        self.visual_row.invalidate()
        # Scenes bring their own controls - the scope has two more than
        # anything else - so the row can get taller and the scene's share
        # of the pane has to be worked out again. Without this the extra
        # line pushed the transport up into the picture.
        self._apply_budget()

    # -- the rider's sounds -------------------------------------------------
    def _sound_board(self):
        """The rider's sounds, made the first time they are wanted."""
        if self._board is None:
            import config
            import rider_sound

            self._board = rider_sound.SoundBoard(
                config.cache_dir() / "sounds",
                volume=lambda: self.volume.value() / 100.0,
                duck=self._duck, later=self._later,
                level=lambda: self.effects.value() / 100.0)
            rider_sound.make_elsewhere(self._board.folder)
            # In the record's key, if it has been heard already.
            self._board.set_harmony(self.spectrum.harmony())
        return self._board

    #: How far either way the picture may be moved against the music, in
    #: milliseconds, and where that is kept. A new name when what the
    #: picture is timed against changes, so an old setting made up for
    #: something since put right is not carried over.
    SYNC_MOST = 250
    SYNC_PREF = "sync_ms"

    def sync_trim(self) -> int:
        """The listener's own adjustment of the picture against the music,
        in ms: more is the picture earlier. Remembered."""
        kept = _viewer_prefs().get(self.SYNC_PREF, 0)
        if not isinstance(kept, (int, float)) or abs(kept) > self.SYNC_MOST:
            return 0
        return int(kept)

    def set_sync_trim(self, ms: int) -> int:
        """Move the picture against the music by ``ms``, and remember it."""
        ms = int(max(-self.SYNC_MOST, min(self.SYNC_MOST, int(ms))))
        _keep_viewer_pref(self.SYNC_PREF, ms)
        if self.spectrum.allowance is not None:
            self.spectrum.allowance.trim = ms / 1000.0
        return ms

    @staticmethod
    def timing_words(ms: int) -> str:
        """What a trim does, in words."""
        if ms == 0:
            return "Picture on the beat as measured"
        way = "earlier" if ms > 0 else "later"
        return f"Picture {abs(ms)} ms {way}"

    @Slot(int)
    def _timing_moved(self, value: int) -> None:
        self.timing_said.setText(self.timing_words(self.set_sync_trim(value)))

    @Slot()
    def _show_timing_note(self) -> None:
        """What is already allowed for, which is known once a track has
        played, and the trim as it was last left."""
        self.timing_slider.blockSignals(True)
        self.timing_slider.setValue(self.sync_trim())
        self.timing_slider.blockSignals(False)
        self.timing_said.setText(self.timing_words(self.sync_trim()))
        allowance = self.spectrum.allowance
        if allowance is not None:
            screen = self.spectrum.screen()
            refresh = screen.refreshRate() if screen is not None else 60.0
            self.timing_note.setText(
                f"Already allowed for: {allowance.audio() * 1000:.0f} ms of "
                f"sound output and {allowance.display(refresh) * 1000:.0f} "
                f"ms of screen.")
        else:
            self.timing_note.setText(
                "Allowed for automatically once a track plays.")

    @Slot()
    def _show_picture_panel(self) -> None:
        """The picture's settings, in a panel under the button."""
        self._picture_menu.popup(self.picture_button.mapToGlobal(
            self.picture_button.rect().bottomLeft()))

    #: Where the scene change is kept.
    CHANGE_PREF = "scene_change"

    @Slot(str)
    def _change_chosen(self, name: str, keep: bool = True) -> None:
        from attachment_widgets import Spectrum as _Spectrum

        seconds = dict(_Spectrum.CHANGES).get(name)
        if seconds is None:
            return
        self.spectrum.set_change(seconds)
        if keep:
            _keep_viewer_pref(self.CHANGE_PREF, name)

    @Slot()
    def _preview_effects(self) -> None:
        """A pickup at the level just set, so moving the slider is setting
        it by ear - if the game's sounds are on."""
        if self.sound_box.isChecked():
            self._sound_board().preview(self.spectrum._scene)

    def _later(self, seconds: float, action) -> None:
        """Do ``action`` in ``seconds``, to the millisecond: the notes of
        an arpeggio, and the echoes of a note taken.

        A timer of the pane's own, precise and let go once it has fired.
        ``QTimer.singleShot`` with a timer type and a function is not a
        signature PySide has: the first echo raised, the pane let go of
        the game's listener for it, and the game went silent for the rest
        of the session - with nothing but a line in the log to say so."""
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setTimerType(Qt.TimerType.PreciseTimer)

        def fire() -> None:
            timer.deleteLater()
            action()

        timer.timeout.connect(fire)
        timer.start(max(0, int(round(seconds * 1000))))

    @Slot()
    def _sounds_changed(self, *_args) -> None:
        """Listen to the game while it is on screen: for its sounds, if
        the box says so, and for the end of a run, whose best is kept."""
        rider = self.scene_box.currentText() == "Music rider"
        self.spectrum.set_listener(self._listen_to_game if rider else None)
        if rider and self.sound_box.isChecked():
            # Once the worker has had time to write them.
            QTimer.singleShot(1500, self._sound_board().prepare)

    def _listen_to_game(self, scene) -> None:
        """After every frame of the game: its sounds, and its best."""
        if self.sound_box.isChecked():
            self._sound_board().listen(scene)
        self._keep_best(scene)

    def _keep_best(self, scene) -> None:
        """A run that has just finished, against the best this track has
        been ridden to in that game; the scene shows the answer."""
        result = getattr(scene, "_result", None)
        if (result is None or result is self._kept_result
                or self._track_key is None):
            return
        self._kept_result = result
        if not result.get("whole", True):
            # Skipped through, or started part way in: not the track.
            scene.best_before, scene.new_best = None, False
            return
        if self._best_keeper is None:
            import config
            import rider_bests

            self._best_keeper = rider_bests.Bests(
                config.app_support_dir() / "rider-bests.json")
        before, beaten = self._best_keeper.offer(
            self._track_key, self.best_kept_as(result), int(result["worth"]))
        scene.best_before = before
        scene.new_best = beaten

    @staticmethod
    def best_kept_as(result: dict) -> str:
        """What a run's best is kept under: its game, and its level unless
        that is Normal - which is what every best before there were levels
        was, so those stay where they are."""
        level = result.get("difficulty") or "Normal"
        return (result["mode"] if level == "Normal"
                else f"{result['mode']} {level}")

    @Slot(str)
    def _level_chosen(self, name: str) -> None:
        import visualizers as _vis

        _vis.by_name("Music rider").set_difficulty(name)
        _keep_viewer_pref("difficulty", name)

    def _duck(self, depth: float, seconds: float) -> None:
        """The music, down by ``depth`` and back over ``seconds``."""
        if self._audio is None:
            return
        from PySide6.QtCore import QVariantAnimation

        animation = getattr(self, "_ducking", None)
        if animation is None:
            animation = self._ducking = QVariantAnimation(self)
            # Against the slider on every step, so moving it mid-duck is
            # honoured, and the last step is the slider exactly.
            animation.valueChanged.connect(
                lambda share: self._audio is not None and self._audio.setVolume(
                    self.volume.value() / 100.0 * float(share)))
        animation.stop()
        animation.setStartValue(max(0.0, 1.0 - depth))
        animation.setEndValue(1.0)
        animation.setDuration(max(1, int(seconds * 1000)))
        animation.start()

    @Slot()
    def _choose_colours(self) -> None:
        from colour_picker import ColourWindow

        dial, background = self.spectrum.colours
        window = ColourWindow(dial, background, self,
                              centres=self.spectrum.dial_centres())
        window.changed.connect(
            lambda d, b: self.spectrum.set_colours(dial=d, background=b))
        window.bands_changed.connect(self.spectrum.set_dial_centres)
        window.exec()

    def release_full_screen(self) -> None:
        """Undo everything the full-screen view wired into this pane.

        Signals outlive the widgets they were connected to, and a lambda
        holding a label that Qt has destroyed raises out of the next
        emission - which is every time the position moves.
        """
        for signal, handle in getattr(self, "_full_links", []):
            try:
                signal.disconnect(handle)
            except (RuntimeError, TypeError):      # already gone
                pass
        self._full_links = []
        self._full = None
        self._full_play = None
        self._full_effects = ()
        self._by_hand_echo = []
        card = getattr(self, "_full_card", None)
        if card is not None and shiboken6.isValid(card):
            card.setParent(None)
            card.deleteLater()
        self._full_card = None

    def transport(self, action: str) -> None:
        """J, K and L, wherever they were pressed."""
        if action == "toggle":
            if self.play.isEnabled():
                self._toggle()
            return
        delta = SKIP_MS if action == "forward" else -SKIP_MS
        target = max(0, min(self.position.maximum(),
                            self.position.value() + delta))
        self.position.setValue(target)
        self._seek(target)

    #: The key that flashes the strobe by hand, written where somebody
    #: choosing "Manual" will see it.
    #:
    #: G rather than F. F was already the key that goes full screen, in
    #: this pane and in the viewer above it, so the one key somebody in
    #: Manual has to know was the one key that also left the room. G is
    #: next to it and next to A, S and D, so the left hand still covers
    #: everything.
    BY_HAND_KEY = "G"

    #: The other one: held, it fires over and over instead of holding the
    #: light up. Named beside the first wherever the first is named, so
    #: that somebody who finds one finds both.
    SPAM_KEY = "H"

    #: The keys that play the visualiser, and what each one does.
    #:
    #: Numbers for scenes because there are eight of them and they are in
    #: a fixed order, so the number is the same key every time whatever
    #: the combo box happens to be showing. The rest sit under the left
    #: hand while the right hand is on the numbers: S switches the strobe
    #: on and off, A and D walk through what it is listening to, M goes
    #: straight to listening to nobody, and G and H are the strobe itself.
    #: G held is a light that stays on; H held is a strobe at twelve a
    #: second. One key cannot be both, and both are worth having.
    #:
    #: J, K, L, space and escape are the transport and are handled where
    #: they always were; these are the ones that are new.
    VJ_KEYS = {
        Qt.Key.Key_S: ("strobe", 0),
        Qt.Key.Key_A: ("reaction", -1),
        Qt.Key.Key_D: ("reaction", 1),
        Qt.Key.Key_M: ("by-hand", 0),
        Qt.Key.Key_G: ("flash", 1),
        Qt.Key.Key_H: ("spam", 1),
        # The lanes, for the scene that is a game. They do nothing at all
        # in the other eight, and ``vj`` says so by returning False, which
        # leaves the key to whatever else wanted it.
        Qt.Key.Key_Left: ("lane", -1),
        Qt.Key.Key_Right: ("lane", 1),
        # And up, for the one game that is not locked to the road.
        Qt.Key.Key_Up: ("jump", 1),
        # The game's sounds, on and off.
        Qt.Key.Key_X: ("sounds", 0),
    }

    @staticmethod
    def vj_action(key):
        """What a key press means, or None if it means nothing here."""
        found = AudioPane.VJ_KEYS.get(key)
        if found is not None:
            return found
        if Qt.Key.Key_1 <= key <= Qt.Key.Key_9:
            return ("scene", key - Qt.Key.Key_1)
        return None

    def vj(self, action: str, value: int = 0) -> bool:
        """One of the playing keys, wherever it was pressed.

        Everything goes through this pane's own controls rather than
        straight at the spectrum, so that what the boxes show is what is
        happening - the same reason the full screen controls are wired to
        the ones in the window rather than to the widget.
        """
        if action == "scene":
            import visualizers

            if not 0 <= value < len(visualizers.SCENES):
                return False
            self.scene_box.setCurrentText(visualizers.SCENES[value].name)
            return True
        if action == "lane":
            return self._steer(value)
        if action == "jump":
            return self._jump()
        if action == "sounds":
            if self._steering() is None:
                return False
            self.sound_box.setChecked(not self.sound_box.isChecked())
            return True
        if action == "reaction" and self._steering() is not None:
            # A and D drive the game while the game is on screen. They step
            # through what the strobe listens to the rest of the time.
            return self._steer(value)
        if action == "strobe":
            self.strobe_box.setChecked(not self.strobe_box.isChecked())
            return True
        if action in ("reaction", "by-hand"):
            from attachment_widgets import Spectrum as _Spec

            name = (_Spec.BY_HAND if action == "by-hand"
                    else self.spectrum.cycle_strobe_source(value))
            self.strobe_source.setCurrentText(name)
            return True
        if action in ("flash", "unflash", "spam", "unspam"):
            wants = action in ("flash", "spam")
            # Pressing the strobe key with the strobe switched off did
            # nothing at all, because the master switch is what scenes
            # ask before they light up. Reaching for the light is asking
            # for the light, so the box is ticked rather than ignored.
            if wants and not self.strobe_box.isChecked():
                self.strobe_box.setChecked(True)
            if action in ("spam", "unspam"):
                self.spectrum.spam_flash(wants)
            else:
                self.spectrum.hold_flash(wants)
            return True
        return False

    def _steering(self):
        """The scene that wants the arrow keys, if the current one does."""
        scene = getattr(self.spectrum, "_scene", None)
        return scene if callable(getattr(scene, "steer", None)) else None

    def _steer(self, way: int) -> bool:
        """Move a lane, if there is a lane to move."""
        scene = self._steering()
        if scene is None:
            return False
        scene.steer(way)
        self.spectrum.update()
        return True

    def _jump(self) -> bool:
        """Leave the road, if the game on screen is the one that can.

        Returns False everywhere else, which leaves the key to whatever
        else wanted it - the list scrolls on the arrows the rest of the
        time.
        """
        scene = getattr(self.spectrum, "_scene", None)
        leap = getattr(scene, "jump", None)
        if not callable(leap) or not leap():
            return False
        self.spectrum.update()
        return True

    #: What the two sliders are called, and what they do, in each mode.
    #: Manual has nothing firing by itself, so "what counts as a hit" and
    #: "how soon another may follow" have nothing to say; the two things
    #: a hand strobe does have are how fast it repeats and how it comes
    #: up and goes down.
    SENSE_WORDS = (
        "Sensitivity", "How big a jump counts as a hit, from fussy to "
                       "eager.")
    RATE_WORDS = (
        "Rate", "How soon after one flash the next may fire, from every "
                "few bars to every beat it can find.")
    HAND_SENSE_WORDS = (
        "Shape", "The shape of a flash you play by hand. Left is on and "
                 "off with nothing in between; right fades up and back "
                 "down.")
    HAND_RATE_WORDS = (
        "Rate", "How fast the strobe repeats while the strobe key is "
                "held, from about five a second to thirty.")

    def _show_by_hand(self, source: str) -> None:
        """Say which key flashes it, whenever the strobe waits for one."""
        from attachment_widgets import Spectrum as _Spec

        wanted = source == _Spec.BY_HAND
        self.by_hand.setVisible(wanted)
        _relabel(self.sense_box,
                 *(self.HAND_SENSE_WORDS if wanted else self.SENSE_WORDS))
        _relabel(self.rate_box,
                 *(self.HAND_RATE_WORDS if wanted else self.RATE_WORDS))
        for label in getattr(self, "_by_hand_echo", ()):
            try:
                label.setVisible(wanted)
            except RuntimeError:      # the full screen window has gone
                pass

    def _sync_visual_controls(self) -> None:
        """Available whenever there is a sound file, analysed or not."""
        self.visual_holder.setVisible(self._path is not None)

    @Slot()
    def _go_full_screen(self) -> None:
        """The scene on the whole screen, with the controls it needs.

        Play, seek, volume, theme, strobe and colours all come along -
        leaving the window to change any of them would defeat the point.
        The bar fades after a few seconds of stillness and comes back on
        the first movement.
        """
        from attachment_widgets import FullScreenSpectrum

        home = self.spectrum.parentWidget()
        layout = home.layout() if home is not None else None
        where = layout.indexOf(self.spectrum) if layout is not None else -1
        full = FullScreenSpectrum(self.spectrum, self)
        self._full = full
        # Something in the hole the scene left. Without it the pane simply
        # has a gap in it and nothing anywhere says where the picture went,
        # so a full-screen window on another display is lost.
        if layout is not None and where >= 0:
            layout.insertWidget(where, self._full_notice(full))

        play = QPushButton()
        play.setFixedWidth(52)
        _name_transport(play, self._playing)
        play.clicked.connect(self._toggle)
        self._full_play = play

        seek = SeekBar()
        seek.setRange(0, self.position.maximum())
        seek.setValue(self.position.value())
        seek.seeked.connect(self._seek)
        seek.seeked.connect(self.position.setValue)
        # Connections from the pane's own widgets to widgets that only
        # exist while full screen does. They have to be undone when it
        # closes, or the next seek calls into a deleted label.
        # Both signals. valueChanged carries a seek or a track change;
        # ``moved`` carries the player's own reports, which are the ones
        # that arrive while a track is playing. Following only the first
        # left this bar at zero for the whole song.
        self._full_links = [
            (self.position.valueChanged, self.position.valueChanged.connect(
                seek.report)),
            (self.position.moved, self.position.moved.connect(seek.report))]

        clock = QLabel(self.clock.text())
        clock.setFont(system_font())
        clock.setMinimumWidth(104)
        clock.setStyleSheet("color: #e8e8ee;")
        def tick(value: int) -> None:
            clock.setText(f"{_mmss(value)} / {_mmss(self.position.maximum())}")

        self._full_links.append(
            (self.position.valueChanged,
             self.position.valueChanged.connect(tick)))
        self._full_links.append(
            (self.position.moved, self.position.moved.connect(tick)))

        volume = QSlider(Qt.Orientation.Horizontal)
        volume.setRange(0, 100)
        volume.setValue(self.volume.value())
        volume.setFixedWidth(110)
        volume.setToolTip("Volume")
        volume.valueChanged.connect(self.volume.setValue)

        # The game's sounds against the music, as in the window - there
        # only while the game is.
        effects = QSlider(Qt.Orientation.Horizontal)
        effects.setRange(0, 100)
        effects.setValue(self.effects.value())
        effects.setFixedWidth(80)
        effects.setToolTip(self.effects.toolTip())
        effects.setAccessibleName("Effects volume")
        effects.valueChanged.connect(self.effects.setValue)
        effects.sliderReleased.connect(self._preview_effects)
        effects_label = QLabel(self.sound_box.text())
        effects_label.setToolTip(self.effects.toolTip())
        effects.setEnabled(self.sound_box.isChecked())
        self._full_links += [
            (self.effects.valueChanged,
             self.effects.valueChanged.connect(effects.setValue)),
            (self.sound_box.toggled,
             self.sound_box.toggled.connect(effects.setEnabled))]
        self._full_effects = (effects_label, effects)

        import visualizers
        scene = _combo([s.name for s in visualizers.SCENES],
                       "Which visualiser to draw.  Keys 1 to "
                       f"{len(visualizers.SCENES)}.")
        scene.setCurrentText(self.scene_box.currentText())
        scene.currentTextChanged.connect(self.scene_box.setCurrentText)
        scene.currentTextChanged.connect(self._scene_chosen)

        strobe = QCheckBox("Strobe")
        strobe.setToolTip("Flash the scene on the beat. Key S.\n\n"
                          "G flashes it by hand. Tap for a flash, hold "
                          "for a held light.")
        strobe.setChecked(self.strobe_box.isChecked())
        strobe.toggled.connect(self.strobe_box.setChecked)

        from attachment_widgets import Spectrum as _Spec
        reaction = _combo(list(_Spec.STROBE_SOURCES),
                          "What the strobe listens to.  Keys A and D, "
                          "and M for nothing at all.")
        reaction.setCurrentText(self.strobe_source.currentText())
        reaction.currentTextChanged.connect(self.strobe_source.setCurrentText)

        # And the same hint, on the bar, for the same reason.
        by_hand = QLabel(
            f"{self.BY_HAND_KEY} hold · {self.SPAM_KEY} strobe")
        by_hand.setFont(system_font())
        by_hand.setStyleSheet("color: #8fd0ff;")
        by_hand.setVisible(reaction.currentText() == _Spec.BY_HAND)
        self._by_hand_echo = [by_hand]

        # Back the other way as well. The keys drive this pane's own
        # controls, so without these the picture changed and the box in
        # front of it went on saying what it used to be.
        self._full_links += [
            (self.scene_box.currentTextChanged,
             self.scene_box.currentTextChanged.connect(scene.setCurrentText)),
            (self.strobe_box.toggled,
             self.strobe_box.toggled.connect(strobe.setChecked)),
            (self.strobe_source.currentTextChanged,
             self.strobe_source.currentTextChanged.connect(
                 reaction.setCurrentText)),
        ]

        colours = QPushButton("Colours…")
        colours.clicked.connect(self._choose_colours)

        leave = QPushButton("Close")
        leave.clicked.connect(full.close)

        full.add_control(play)
        full.add_control(seek, stretch=1)
        full.add_control(clock)
        full.add_control(QLabel("Volume"))
        full.add_control(volume)
        full.add_control(effects_label)
        full.add_control(effects)

        def game_only(name: str) -> None:
            for widget in (effects_label, effects):
                widget.setVisible(name == "Music rider")

        game_only(self.scene_box.currentText())
        self._full_links.append(
            (self.scene_box.currentTextChanged,
             self.scene_box.currentTextChanged.connect(game_only)))
        for widget in (scene, strobe, reaction, by_hand, colours):
            full.add_control(widget)
        full.add_control(leave)

        import touchbar

        touchbar.give(full, self.touch_bar_items(full), "fullscreen")
        full.showFullScreen()

    # -- the Touch Bar -------------------------------------------------------
    def touch_bar_items(self, full=None) -> list:
        """The transport, the scenes, the strobe and the picture, for the
        viewer's Touch Bar or, given ``full``, the full screen's."""
        import touchbar

        on = self.enable_box.isChecked
        caption = lambda holder: holder.layout().itemAt(0).widget().text()
        items = [
            touchbar.Button("back", "Back ten seconds",
                            lambda: self.transport("back"), title="",
                            image="gobackward.10", priority="low"),
            touchbar.Button("play", "Play", self.play, title="",
                            image=lambda: ("pause.fill" if self._playing
                                           else "play.fill"),
                            watch=[self.playingChanged], priority="high"),
            touchbar.Button("forward", "Forward ten seconds",
                            lambda: self.transport("forward"), title="",
                            image="goforward.10", priority="low"),
        ]
        if full is None:
            items += [
                touchbar.Slider("seek", "", self.position, settle=150,
                                change=self._seek_to, width=240,
                                when=lambda: not on(),
                                watch=[self.position.moved,
                                       self.enable_box.toggled]),
                touchbar.Toggle("visualiser", "Visualiser", self.enable_box,
                                when=lambda: not on()),
            ]
        items += [
            touchbar.Choice("scene", "Scene", self.scene_box, style="list",
                            width=300, priority="high",
                            when=lambda: on() or full is not None,
                            watch=[self.enable_box.toggled]),
            touchbar.Toggle("strobe", "Strobe", self.strobe_box,
                            when=lambda: on() or full is not None,
                            watch=[self.enable_box.toggled]),
            touchbar.Button("flash", "Flash", self._flash_once,
                            priority="low",
                            when=self.strobe_box.isChecked),
            touchbar.Popover("game", "Game", [
                touchbar.Choice("game-mode", "Game", self.game_box),
                touchbar.Choice("level", "Level", self.level_box),
                touchbar.Toggle("sounds", "Sounds", self.sound_box),
                touchbar.Slider("effects", "Effects", self.effects,
                                width=110),
            ], title=lambda: self.game_box.currentText(),
                when=lambda: on() or full is not None,
                watch=[self.enable_box.toggled]),
            touchbar.Popover("beam", "Beam", [
                touchbar.Choice("beam-mode", "Beam", self.mode_box),
                touchbar.Slider("glow", "Glow", self.decay, width=140),
            ], title=lambda: self.mode_box.currentText(),
                when=lambda: on() or full is not None,
                watch=[self.enable_box.toggled]),
            touchbar.Button("colours", "Colours", self.colour_button,
                            when=lambda: on() or full is not None,
                            watch=[self.enable_box.toggled]),
            touchbar.Popover("picture", "Picture", [
                touchbar.Toggle("picture-on", "Visualiser", self.enable_box),
                touchbar.Choice("shape", "Shape", self.shape_box,
                                style="list", width=190),
                touchbar.Choice("change", "Scene change", self.change_box),
                touchbar.Choice("source", "Listens to", self.strobe_source,
                                style="list", width=190),
                touchbar.Slider("sense", "Sensitivity", self.sense,
                                width=100, priority="low",
                                title=lambda: caption(self.sense_box)),
                touchbar.Slider("rate", "Rate", self.flash, width=100,
                                priority="low",
                                title=lambda: caption(self.rate_box)),
            ], when=lambda: on() or full is not None,
                watch=[self.enable_box.toggled]),
        ]
        if full is None:
            items += [
                touchbar.Button("full", "Full screen", self.full_button,
                                title="",
                                image="arrow.up.left.and.arrow.down.right",
                                when=on, watch=[self.enable_box.toggled],
                                priority="high"),
                touchbar.Slider("volume", "", self.volume, width=130,
                                ends=("speaker.fill", "speaker.wave.3.fill"),
                                when=lambda: not on()),
            ]
        else:
            items.append(touchbar.Button(
                "leave", "Leave full screen", full.close, title="",
                image="arrow.down.right.and.arrow.up.left",
                priority="high"))
        return items

    def _seek_to(self, value: int) -> None:
        """Go to ``value``, as dragging the seek bar there does."""
        self.position.setValue(value)
        self._seek(value)

    def _flash_once(self) -> None:
        """One flash of the strobe, as a tap of its key gives."""
        self.vj("flash")
        QTimer.singleShot(90, self, lambda: self.vj("unflash"))

    def _state(self, *_args) -> None:
        try:
            from PySide6.QtMultimedia import QMediaPlayer
            playing = (self._player.playbackState()
                       == QMediaPlayer.PlaybackState.PlayingState)
        except Exception:      # noqa: BLE001
            playing = False
        changed = playing != self._playing
        self._playing = playing
        _name_transport(self.play, playing)
        twin = getattr(self, "_full_play", None)
        if twin is not None and shiboken6.isValid(twin):
            _name_transport(twin, playing)
        self.spectrum.set_playing(playing and self.enable_box.isChecked())
        if changed:
            self.playingChanged.emit(playing)

    def _error(self, *_args) -> None:
        self.title.setText(self.title.text() + "  (this file will not play)")
        self.play.setEnabled(False)
        self.spectrum.clear()

    def _full_notice(self, full) -> QWidget:
        """The card that stands in for the scene while it is full screen."""
        card = QWidget()
        card.setObjectName("fullScreenNotice")
        card.setMinimumHeight(72)
        row = QHBoxLayout(card)
        row.setContentsMargins(14, 12, 14, 12)
        row.setSpacing(10)
        said = QLabel("Playing full screen.")
        said.setFont(system_font())
        row.addWidget(said)
        row.addStretch(1)
        find = QPushButton("Bring it to the front")
        find.setToolTip("Raise the full screen window.")
        find.clicked.connect(lambda: self._raise_full_screen())
        row.addWidget(find)
        leave = QPushButton("Leave full screen")
        leave.clicked.connect(full.close)
        row.addWidget(leave)
        self._full_card = card
        return card

    def _raise_full_screen(self) -> None:
        """Put the full screen window back in front of everything."""
        full = getattr(self, "_full", None)
        if full is None or not shiboken6.isValid(full):
            return
        full.showFullScreen()
        full.raise_()
        full.activateWindow()

    def _show_clock(self, position: int) -> None:
        self.clock.setText(f"{_mmss(position)} / {_mmss(self.position.maximum())}")

    def stop(self) -> None:
        full = getattr(self, "_full", None)
        if full is not None:
            full.close()
            self._full = None
        self.visual_holder.setVisible(False)
        # Anything still decoding is for a file nobody is looking at now.
        self._analysis_token += 1
        self.spectrum.set_playing(False)
        self.spectrum.set_working(None)
        self.spectrum.clear()
        # The old track's length and place, which the player will not
        # say again for a track it no longer has.
        self.wave.forget_track()
        self._track_key = None
        self._kept_result = None
        # And its key: the next track's sounds are played in the next
        # track's, or in none until it has been heard.
        if self._board is not None:
            self._board.set_harmony(None)
        # cancel(), not just forget: the analysis runs on a QThread, and Qt
        # calls qFatal if one is destroyed while it is still running.
        self._cancel_analysis()
        if self._player is not None:
            self._player.stop()
            self._player.setSource(QUrl())

    def event(self, incoming) -> bool:
        """Last stop before Qt deletes this pane and the thread under it;
        and the picture's share, whenever the layout around it changes."""
        if incoming.type() == QEvent.Type.DeferredDelete:
            self._cancel_analysis()
        handled = super().event(incoming)
        if incoming.type() == QEvent.Type.LayoutRequest:
            # Something around the picture changed size - the cover and
            # title arriving, a line of controls wrapping - and its share
            # is worked out again. Only on a resize, it kept the share it
            # had, and the rows under it were squeezed or left a gap until
            # the window was next resized. Not while the pane is being
            # taken apart, when the picture may already be gone.
            import shiboken6

            spectrum = getattr(self, "spectrum", None)
            if spectrum is not None and shiboken6.isValid(spectrum):
                self._apply_budget(fresh=False)
        return handled

    def closeEvent(self, incoming) -> None:      # noqa: N802 - Qt's name
        self._cancel_analysis()
        super().closeEvent(incoming)


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
                 parent=None, fetch: Optional[Callable] = None,
                 library: bool = False) -> None:
        super().__init__(parent)
        # The same window serves two jobs. As a library it is somebody's
        # own music rather than a message's parts, so it gains a way to
        # add tracks and loses the ones about saving copies of something
        # that arrived in the post.
        self.library = bool(library)
        self.setWindowTitle("Visualiser" if library else "Attachments")
        # Narrower than the attachment window is allowed to be: the
        # visualiser has to be usable on a small screen. Tall enough that
        # the picture still has room once the transport and the controls
        # have theirs. Widened to what the controls need once they are
        # built: see showEvent.
        self._least = QSize(720 if library else 900, 600)
        self.setMinimumSize(self._least)
        self._sized = False
        self._found = list(found)
        self._fetch = fetch
        self._temp = Path(tempfile.mkdtemp(prefix="mm-attach-"))
        os.chmod(self._temp, 0o700)
        self._written: List[Path] = []
        self._last_dir = str(Path.home() / "Downloads")
        self._showing_metadata = False
        #: In flight, by row, and what is waiting behind them.
        self._workers: dict = {}
        self._queue: List[int] = []
        #: the row whose arrival should switch the view.
        self._awaiting: Optional[int] = None

        self.list = QListWidget()
        # No fixed width. At 280 inside a narrower panel its right edge
        # was outside the frame, so three sides of its border were drawn
        # and the fourth was cut - which reads as a border that cannot
        # make up its mind.
        self.list.setMinimumWidth(180)
        self.list.setToolTip(
            "Everything attached to this message. Pick one to look at it.")
        for item in self._found:
            entry = QListWidgetItem(_row_label(item))
            entry.setToolTip(_row_tooltip(item))
            self.list.addItem(entry)

        # Not the dim style. This is the only place the keys are written
        # down, and a run-on line of grey text separated by middots is
        # something people's eyes slide off rather than read.
        opening = ("Add a track, then click it to watch. Nothing is sent "
                   "anywhere and nothing is kept."
                   if library else
                   "Click an attachment to open it. Nothing here is ever "
                   "run, and nothing is saved unless you say so.")
        keys = [("K / Space", "play or pause"),
                ("J&nbsp;&nbsp;L", "back or on ten seconds"),
                ("← →", "scrub"),
                ("↑ ↓", "move between tracks" if library
                 else "move between attachments"),
                ("F", "full screen"),
                ("⌘I", "show details")]
        if not library:
            keys.append(("⌘S", "save a copy"))
        self.hint = QLabel(
            f"<p style='margin:0 0 6px 0'>{opening}</p>"
            "<table cellspacing='0' cellpadding='0'>"
            + "".join(
                "<tr>"
                f"<td style='padding:1px 8px 1px 0'><b>{key}</b></td>"
                f"<td style='padding:1px 0'>{what}</td>"
                "</tr>"
                for key, what in keys)
            + "</table>")
        self.hint.setTextFormat(Qt.TextFormat.RichText)
        self.hint.setWordWrap(True)

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
            "Save a copy. Quarantined like a download.")
        self.save_all = QPushButton("Save all…")
        self.save_all.setToolTip(
            "Choose a folder and keep a copy of everything attached.")
        self.copy_button = QPushButton("Copy image")
        self.copy_button.setToolTip(
            "Put this image on the clipboard, to paste somewhere else.")
        self.copy_button.hide()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)

        self.add_button = QPushButton("\uff0b  Add a track")
        self.add_button.setToolTip("Choose sound files from this machine.")
        self.add_button.clicked.connect(self._add_tracks)
        self.add_button.setVisible(self.library)
        for widget in (self.save_button, self.save_all):
            widget.setVisible(not self.library)

        actions = QHBoxLayout()
        actions.addWidget(self.status)
        actions.addSpacing(12)
        actions.addWidget(self.add_button)
        actions.addWidget(self.info_button)
        actions.addWidget(self.save_button)
        actions.addWidget(self.save_all)
        actions.addWidget(self.copy_button)
        actions.addStretch(1)
        actions.addWidget(buttons)


        # The status shares the footnote's line. On its own it was a line
        # of window given over to the word "ready", which the picture
        # playing in front of it had already said.
        self.status.setAlignment(Qt.AlignmentFlag.AlignLeft
                                 | Qt.AlignmentFlag.AlignVCenter)

        right = QVBoxLayout()
        right.addWidget(self.heading)
        right.addWidget(self.warning)
        right.addWidget(self.stack, 1)
        right.addLayout(actions)

        left = QVBoxLayout()
        left.addWidget(self.list, 1)
        left.addWidget(self.hint)
        left_frame = QFrame()
        left_frame.setLayout(left)
        # Narrower for the library: its list holds file names rather than
        # a message's parts, and the picture is what the window is for.
        left_frame.setFixedWidth(232 if self.library else 296)

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
        self._give_touch_bar()

    def _give_touch_bar(self) -> None:
        """Moving through the list, the pane's own controls, and saving."""
        import touchbar

        def showing(pane):
            return lambda: self.stack.currentWidget() is pane

        moved = [self.stack.currentChanged]
        several = lambda: self.list.count() > 1
        rows = [self.list.currentRowChanged, self.list.model().rowsInserted]
        touchbar.give(self, [
            touchbar.Button("previous", "Previous", lambda: self._step_row(-1),
                            title="", image="chevron.up", priority="low",
                            when=several, watch=rows),
            touchbar.Button("next", "Next", lambda: self._step_row(1),
                            title="", image="chevron.down", priority="low",
                            when=several, watch=rows),
            *touchbar.only_when(self.audio.touch_bar_items(),
                                showing(self.audio), moved),
            *touchbar.only_when([
                touchbar.Button("smaller", "Smaller", self.image.zoom_out,
                                title="", image="minus.magnifyingglass"),
                touchbar.Button("larger", "Larger", self.image.zoom_in,
                                title="", image="plus.magnifyingglass"),
            ], showing(self.image), moved),
            touchbar.Button("copy", "Copy image", self.copy_button),
            *touchbar.only_when([
                touchbar.Toggle("wrap", "Wrap", self.text.wrap_button),
            ], showing(self.text), moved),
            touchbar.Space("flexible"),
            touchbar.Toggle("info", "Info", self.info_button,
                            priority="low"),
            touchbar.Button("save", "Save", self.save_button, title="",
                            image="square.and.arrow.down", priority="low"),
            touchbar.Button("add", "Add a track", self.add_button,
                            title="Add", image="plus"),
        ], "viewer")

    def _add_tracks(self) -> None:
        """Pick sound files and list them as if they had arrived attached."""
        from PySide6.QtWidgets import QFileDialog, QMessageBox

        chosen, _ = QFileDialog.getOpenFileNames(
            self, "Choose sound files", "",
            "Audio (*.mp3 *.m4a *.aac *.wav *.aiff *.aif *.flac *.ogg "
            "*.oga *.opus *.wma);;Any file (*)")
        if not chosen:
            return
        refused = []
        for name in chosen:
            path = Path(name)
            try:
                data = path.read_bytes()
            except OSError as exc:
                refused.append(f"{path.name}: {exc}")
                continue
            kind, mime = attachments.sniff(data[:4096], name=path.name)
            if kind != "audio":
                refused.append(f"{path.name}: reads as {kind or 'something else'}")
                continue
            self._found.append(attachments.Attachment(
                part=str(len(self._found) + 1), name=path.name,
                content_type=mime, size=len(data), data=data))
            self.list.addItem(QListWidgetItem(_row_label(self._found[-1])))
        if refused:
            QMessageBox.information(
                self, "Some files were not added", "\n".join(refused))
        if self.list.count() and self.list.currentRow() < 0:
            self.list.setCurrentRow(0)

    def _add_shortcuts(self) -> None:
        """Space plays, arrows move, the ordinary ones save and close."""
        def add(sequence, slot) -> None:
            action = QAction(self)
            action.setShortcut(QKeySequence(sequence))
            action.triggered.connect(slot)
            self.addAction(action)

        add("Space", self._space)
        # The transport keys a media player is expected to have. J and L
        # jump ten seconds, K plays and pauses - the same three keys, in
        # the same places, whether or not the visualiser is on.
        add("J", lambda: self._nudge(-SKIP_MS))
        add("K", self._space)
        add("L", lambda: self._nudge(SKIP_MS))
        add("F", self._toggle_full_screen)
        add(QKeySequence.StandardKey.Save, self._save_current)
        add("Ctrl+I", lambda: self.info_button.toggle())
        add("Down", lambda: self._step_row(1))
        add("Up", lambda: self._step_row(-1))
        add("Right", lambda: self._nudge(5000))
        add("Left", lambda: self._nudge(-5000))

    def _toggle_full_screen(self) -> None:
        """F, from the window. Escape or F again comes back."""
        if self.stack.currentWidget() is not self.audio:
            return
        if not self.audio.full_button.isEnabled():
            return
        full = getattr(self.audio, "_full", None)
        if full is not None:
            full.close()
        else:
            self.audio._go_full_screen()

    # -- the playing keys, in a window as well as full screen -------------
    def _plays(self, event, held: bool) -> bool:
        """Hand a playing key to the audio pane, if it wants it.

        These worked only in full screen. The keys that play the scene are
        the same keys wherever the scene is, and a strobe you can only
        reach by leaving the window is not much of a strobe.

        Auto-repeat is dropped on both sides, as it is full screen: a
        keyboard repeating a held key would switch a held light off and on
        again at its own rate.
        """
        if event.isAutoRepeat():
            return False
        if self.stack.currentWidget() is not self.audio:
            return False
        if getattr(self.audio, "_full", None) is not None:
            return False      # the full screen window is handling them
        found = self.audio.vj_action(event.key())
        if found is None:
            return False
        action, value = found
        if action in ("flash", "spam"):
            action = action if held else "un" + action
        elif not held:
            return False
        return bool(self.audio.vj(action, value))

    def keyPressEvent(self, event) -> None:      # noqa: N802 - Qt's name
        if self._plays(event, held=True):
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:      # noqa: N802 - Qt's name
        if self._plays(event, held=False):
            event.accept()
            return
        super().keyReleaseEvent(event)

    def _space(self) -> None:
        if self.stack.currentWidget() is self.audio and self.audio.play.isEnabled():
            self.audio._toggle()

    def _nudge(self, delta: int) -> None:
        if self.stack.currentWidget() is self.audio:
            # The arrows drive the game while the game is on screen. The
            # shortcut gets the key before any widget does, so the choice
            # has to be made here rather than in a key handler.
            if self.audio.vj("lane", -1 if delta < 0 else 1):
                return
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
        """Queue one part. One request at a time, because one connection.

        Two threads on the same IMAP socket do not race politely: they
        interleave inside TLS and the server drops the connection with a bad
        record MAC. So there is a single worker and a queue, and whatever the
        person is actually looking at goes to the front of it.
        """
        if self._fetch is None:
            return
        if then_show:
            self._awaiting = row
        if row in self._queue or row in self._workers:
            return
        if then_show:
            self._queue.insert(0, row)
        else:
            self._queue.append(row)
        self._pump()

    #: Never more than the connection pool holds. Each fetch checks a
    #: connection out, so more workers than connections only queues inside
    #: the source and gains nothing.
    LANES = 3

    def _pump(self) -> None:
        while self._queue and len(self._workers) < self.LANES:
            row = self._queue.pop(0)
            if not 0 <= row < len(self._found):
                continue
            if self._found[row].data is not None or row in self._workers:
                continue
            thread = _FetchThread(self._fetch, row, self._found[row], self)
            self._workers[row] = thread
            thread.done.connect(self._fetched)
            thread.failed.connect(self._fetch_failed)
            thread.finished.connect(lambda r=row: self._worker_done(r))
            thread.start()

    def _worker_done(self, row: int) -> None:
        self._workers.pop(row, None)
        self._pump()

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
        self.heading.show()

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
            # Start the neighbours now rather than when this one lands: the
            # pool has lanes to spare and the second attachment is usually
            # the next thing clicked.
            self._prefetch_around(row)
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
        self.heading.show()

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
            # The pane says what the track is, beside its cover.
            self.heading.hide()
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
            return "An archive. It is saved as one file."
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
            self.status.setText("Still fetching that one. Try again in a moment.")
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
            f"{item.shown} is an executable.\n\nIt will be quarantined "
            "like a download. Save it anyway?",
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

    #: The size the visualiser opens at, where the screen has room for it.
    OPENS_AT = QSize(1120, 800)

    def showEvent(self, event) -> None:      # noqa: N802 - Qt's name
        """The first time: never narrower than the controls, and a size the
        picture is worth looking at.

        A fixed minimum narrower than the transport let the window be made
        small enough that the clock was drawn over the seek bar.
        """
        if not self._sized:
            self._sized = True
            self.setMinimumSize(self._least.expandedTo(
                QSize(self.minimumSizeHint().width(), 0)))
            screen = self.screen()
            room = screen.availableGeometry() if screen is not None else None
            # Unless it was given a size before it was shown.
            if (self.library and room is not None
                    and not self.testAttribute(Qt.WidgetAttribute.WA_Resized)):
                wanted = QSize(min(self.OPENS_AT.width(),
                                   int(room.width() * 0.9)),
                               min(self.OPENS_AT.height(),
                                   int(room.height() * 0.9)))
                self.resize(wanted.expandedTo(self.minimumSize()))
        super().showEvent(event)

    # -- tidying up -------------------------------------------------------
    def done(self, result: int) -> None:      # noqa: D102 - Qt's name
        self.audio.stop()
        QTimer.singleShot(0, self._sweep)
        super().done(result)

    def _sweep(self) -> None:
        # Stop reading the files before deleting them. Sweeping only ever
        # unlinked them, which left an analysis running against a path
        # that no longer existed and, worse, a thread still going after
        # the window it reports to had gone. The decode used to finish
        # almost at once so the window was small; it is longer now that
        # the drums are picked out afterwards, and a thread that outlives
        # its widget takes the process with it.
        self.audio.stop()
        self.audio._cancel_analysis()
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
