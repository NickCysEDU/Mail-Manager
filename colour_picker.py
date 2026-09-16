"""Choosing the two colours the meter scene is drawn in.

Two ways in, because they suit different intentions. A colour wheel is right
when somebody knows what they want. Sampling from a photograph is right when
they want the meters to match something - a record sleeve, a room, a poster -
and could not name the colour if asked.

The image never leaves the machine and is never kept: it is decoded, shown,
clicked on for a pixel, and dropped when the window closes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QImage, QImageReader, QPixmap
from PySide6.QtWidgets import (QColorDialog, QDialog, QDialogButtonBox,
                               QFileDialog, QHBoxLayout, QLabel, QMessageBox,
                               QPushButton, QVBoxLayout, QWidget)

#: A photograph is decoded to look at, not to keep. Anything past this is
#: scaled down first, so a forty megapixel picture cannot fill memory.
MAX_SIDE = 1600


def readable_formats() -> str:
    """A file dialog filter covering whatever Qt can actually decode here."""
    names = sorted({bytes(f).decode().lower()
                    for f in QImageReader.supportedImageFormats()})
    patterns = " ".join(f"*.{name}" for name in names)
    return f"Images ({patterns});;All files (*)"


class Swatch(QPushButton):
    """A button that is the colour it stands for."""

    picked = Signal(QColor)

    def __init__(self, colour: QColor, label: str) -> None:
        super().__init__(label)
        self._colour = QColor(colour)
        self.setMinimumWidth(132)
        self.clicked.connect(self._choose)
        self._restyle()

    def colour(self) -> QColor:
        return QColor(self._colour)

    def set_colour(self, colour: QColor) -> None:
        self._colour = QColor(colour)
        self._restyle()
        self.picked.emit(self.colour())

    def _restyle(self) -> None:
        dark = self._colour.lightnessF() < 0.55
        text = "#ffffff" if dark else "#111111"
        self.setStyleSheet(
            f"QPushButton {{ background: {self._colour.name()}; color: {text};"
            f" border: 1px solid rgba(128,128,128,120); padding: 6px 10px; }}")

    def _choose(self) -> None:
        chosen = QColorDialog.getColor(self._colour, self, "Choose a colour")
        if chosen.isValid():
            self.set_colour(chosen)


class ImageSampler(QLabel):
    """A picture you take a colour out of by clicking it."""

    sampled = Signal(QColor)

    def __init__(self) -> None:
        super().__init__()
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(QSize(420, 250))
        self.setCursor(QCursor(Qt.CursorShape.CrossCursor))
        self.setText("Load a picture and click it to take a colour.")
        self.setWordWrap(True)
        self._image: Optional[QImage] = None

    def load(self, path: Path) -> str:
        reader = QImageReader(str(path))
        reader.setAutoTransform(True)
        image = reader.read()
        if image.isNull():
            return f"That is not a picture this build can read ({reader.errorString()})."
        if max(image.width(), image.height()) > MAX_SIDE:
            image = image.scaled(MAX_SIDE, MAX_SIDE,
                                 Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
        self._image = image
        self._draw()
        return f"{image.width()} x {image.height()}. Click anywhere to take that colour."

    def _draw(self) -> None:
        if self._image is None:
            return
        self.setPixmap(QPixmap.fromImage(self._image).scaled(
            self.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event) -> None:      # noqa: N802 - Qt's name
        super().resizeEvent(event)
        self._draw()

    def mousePressEvent(self, event) -> None:      # noqa: N802 - Qt's name
        pixmap = self.pixmap()
        if self._image is None or pixmap is None or pixmap.isNull():
            return
        # The pixmap is centred and scaled, so a click has to be mapped back
        # through both before it means a pixel.
        offset_x = (self.width() - pixmap.width()) / 2.0
        offset_y = (self.height() - pixmap.height()) / 2.0
        x = event.position().x() - offset_x
        y = event.position().y() - offset_y
        if not (0 <= x < pixmap.width() and 0 <= y < pixmap.height()):
            return
        scale_x = self._image.width() / pixmap.width()
        scale_y = self._image.height() / pixmap.height()
        colour = self._image.pixelColor(int(x * scale_x), int(y * scale_y))
        if colour.isValid():
            self.sampled.emit(colour)


class ColourWindow(QDialog):
    """Both colours, a wheel for each, and a picture to sample from."""

    changed = Signal(QColor, QColor)      # dial, background
    bands_changed = Signal(tuple)         # one frequency per meter

    def __init__(self, dial: QColor, background: QColor, parent=None,
                 centres=()) -> None:
        super().__init__(parent)
        self.setWindowTitle("Meters")
        self.setMinimumWidth(560)
        self._centres = tuple(centres)

        self.dial = Swatch(dial, "Dial colour")
        self.background = Swatch(background, "Background")
        self.dial.setToolTip("The arc, the numbers and the needle.")
        self.background.setToolTip("Behind the meters.")
        self._target = self.dial

        self.to_dial = QPushButton("Sample to dial")
        self.to_background = QPushButton("Sample to background")
        self.to_dial.setCheckable(True)
        self.to_background.setCheckable(True)
        self.to_dial.setChecked(True)
        self.to_dial.setToolTip("Clicks on the picture set the dial colour.")
        self.to_background.setToolTip(
            "Clicks on the picture set the background colour.")
        self.to_dial.clicked.connect(lambda: self._aim(self.dial))
        self.to_background.clicked.connect(lambda: self._aim(self.background))

        self.open_button = QPushButton("Load a picture…")
        self.open_button.setToolTip(
            "Any picture this build can read. It is used to take a colour "
            "from and is not kept.")
        self.open_button.clicked.connect(self._open)

        self.sampler = ImageSampler()
        self.note = QLabel("")
        self.note.setWordWrap(True)
        self.note.setProperty("dim", "true")

        self.reset = QPushButton("Back to red on black")
        self.reset.clicked.connect(self._reset)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.accept)

        swatches = QHBoxLayout()
        swatches.addWidget(self.dial)
        swatches.addWidget(self.background)
        swatches.addStretch(1)
        swatches.addWidget(self.reset)

        aim = QHBoxLayout()
        aim.addWidget(self.open_button)
        aim.addWidget(self.to_dial)
        aim.addWidget(self.to_background)
        aim.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Pick a colour, or take one out of a picture."))
        layout.addLayout(swatches)
        layout.addLayout(aim)
        layout.addWidget(self.sampler, 1)
        layout.addWidget(self.note)
        if self._centres:
            layout.addWidget(self._band_box())
        layout.addWidget(buttons)

        self.dial.picked.connect(self._announce)
        self.background.picked.connect(self._announce)
        self.sampler.sampled.connect(self._take)

    # -- which frequency each meter reads ---------------------------------
    def _band_box(self):
        """One spin box per meter, so the rack can be pointed anywhere.

        Nothing is re-analysed when these change: the frames already in
        memory are re-read against the new centres, so a three minute
        track responds immediately.
        """
        from PySide6.QtWidgets import QGridLayout, QGroupBox, QSpinBox

        box = QGroupBox("What each meter reads")
        grid = QGridLayout(box)
        grid.setHorizontalSpacing(14)
        self._spins = []
        for index, hertz in enumerate(self._centres):
            spin = QSpinBox()
            # 20 Hz to the Nyquist limit of the decode: outside that there
            # is nothing in the signal to point a meter at.
            spin.setRange(20, 24_000)
            spin.setSingleStep(10)
            spin.setValue(int(hertz))
            spin.setSuffix(" Hz")
            spin.setAccessibleName(f"Meter {index + 1} frequency")
            spin.valueChanged.connect(self._bands_edited)
            self._spins.append(spin)
            grid.addWidget(QLabel(f"{index + 1}"), index // 5, (index % 5) * 2)
            grid.addWidget(spin, index // 5, (index % 5) * 2 + 1)

        back = QPushButton("Back to the original ten")
        back.clicked.connect(self._reset_bands)
        grid.addWidget(back, 2, 0, 1, 10)
        return box

    def _bands_edited(self, *_args) -> None:
        self.bands_changed.emit(tuple(spin.value() for spin in self._spins))

    def _reset_bands(self) -> None:
        import attachment_audio

        for spin, hertz in zip(self._spins, attachment_audio.DIAL_CENTRES):
            spin.blockSignals(True)
            spin.setValue(int(hertz))
            spin.blockSignals(False)
        self._bands_edited()

    def _aim(self, swatch: Swatch) -> None:
        self._target = swatch
        self.to_dial.setChecked(swatch is self.dial)
        self.to_background.setChecked(swatch is self.background)

    def _open(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(
            self, "Open a picture", str(Path.home()), readable_formats())
        if not chosen:
            return
        outcome = self.sampler.load(Path(chosen))
        self.note.setText(outcome)
        if outcome.startswith("That is not"):
            QMessageBox.information(self, "Cannot read that", outcome)

    def _take(self, colour: QColor) -> None:
        self._target.set_colour(colour)
        which = "Dial" if self._target is self.dial else "Background"
        self.note.setText(f"{which} set to {colour.name()}.")

    def _reset(self) -> None:
        self.dial.set_colour(QColor(226, 62, 48))
        self.background.set_colour(QColor(6, 4, 6))

    def _announce(self, *_args) -> None:
        self.changed.emit(self.dial.colour(), self.background.colour())
