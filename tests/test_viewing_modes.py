"""Every viewing mode the window can be in, with room for everything.

"Xxxxxx xxx xxxxxxx xxxxx xxx xxxxxx xxx xxx xxx xx xx xxx xxxxxx xxxx xx
xxxxxxxx xxx xxxxxx xxxxx xxx xxxxxxxxxx xx xx xxxxxx."

The individual layout tests elsewhere each check one control at one size.
This walks *every* visible widget in a window and asks two questions of
each: is it inside the thing that holds it, and is it at least as big as
it says it needs to be. Then it does that for the main window with the
preview under the table and beside it, for the attachment viewer with
each of the scenes that carry extra controls, for the full-screen
visualiser, and for the dialogs - at the sizes each of them can be.

Two things are deliberately not faults:

*A fixed width.* ``setFixedWidth`` is a decision, and the style's own
minimum for a push button is 80 px whatever is written on it - the
transport's play button is 52 and carries one glyph.

*Anything inside a scroll area.* Being taller than the view is what a
scroll area is for.
"""

from __future__ import annotations

from array import array

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import (QAbstractScrollArea, QApplication,  # noqa: E402
                               QWidget)

import attachment_audio  # noqa: E402
import theme  # noqa: E402
from config import InMemoryCredentialStore, Settings  # noqa: E402
from gui import MainWindow  # noqa: E402

from test_layout import make_item  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def dressed(qapp):
    """A known appearance, and the default one back afterwards.

    How tall a row of controls is depends on the theme in force, and
    every test file in this process shares one application: run after
    something that left a different density applied, the same window lays
    out differently and a sweep of it passes or fails by what ran before
    it. So it is set here, and put back.
    """
    def dress(spacing="comfortable"):
        theme.apply(qapp, "dark", "normal", False, spacing=spacing)
    yield dress
    theme.apply(qapp, "system", "normal", False, spacing="comfortable")


@pytest.fixture
def window(qapp, tmp_path, monkeypatch, dressed):
    monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
    dressed()
    made = MainWindow(Settings(icloud_email="a@b.com"),
                      InMemoryCredentialStore())
    made.model.set_items([make_item(str(i)) for i in range(8)])
    made.show()
    yield made
    # Closed *and* deleted: close() only hides, and a window left alive
    # restyles with every theme change for the rest of the session. See
    # TestTheWindowFixtureDestroysItsWindow.
    made.close()
    made.deleteLater()


def _skip(child) -> bool:
    """Whether this one is not worth asking about.

    Inside a scroll area, where being bigger than the view is the point;
    or inside something that has been collapsed to nothing, where Qt
    leaves the children at whatever geometry they last had and every one
    of them is "outside" a parent with no size.
    """
    at = child
    while at is not None:
        if isinstance(at, QAbstractScrollArea):
            return True
        parent = at.parentWidget()
        if parent is not None:
            if at.width() <= 1 or at.height() <= 1:
                return True
            box = at.geometry()
            # Parked wholly outside its parent: a splitter that has been
            # dragged shut moves the child out of the way rather than
            # resizing it - measured, a collapsed preview sits at x=-1581
            # keeping its full width. That is shut, not mislaid.
            if (box.top() >= parent.height() or box.left() >= parent.width()
                    or box.bottom() < 0 or box.right() < 0):
                return True
        at = parent
    return False


def faults(widget) -> list:
    """Everything visible in ``widget`` that has nowhere to be."""
    found = []
    for child in widget.findChildren(QWidget):
        if not child.isVisible() or child.width() == 0:
            continue
        parent = child.parentWidget()
        if parent is None or _skip(child):
            continue
        if child.minimumWidth() == child.maximumWidth():
            continue
        name = getattr(child, "text", lambda: "")() or child.objectName()
        box = child.geometry()
        over = max(box.right() - parent.width(),
                   box.bottom() - parent.height(), -box.left(), -box.top())
        if over > 1:
            found.append(
                f"{child.__class__.__name__}({name!r:.30}) is {over}px "
                f"outside the {parent.__class__.__name__} holding it")
        least = child.minimumSizeHint()
        if least.width() > 0 and child.width() < least.width() - 1:
            found.append(
                f"{child.__class__.__name__}({name!r:.30}) is squeezed to "
                f"{child.width()}px of the {least.width()} it asks for")
    return sorted(set(found))


class TestTheMainWindow:
    """Both places the preview can sit, at every size the window can be."""

    #: The last of these is the smallest window the app allows. See
    #: MainWindow.setMinimumSize: below it there is not room to draw what
    #: is in the window, which is not a smaller window but a broken one.
    SIZES = ((1600, 1000), (1280, 820), (1024, 700), (900, 650), (800, 580))

    @pytest.mark.parametrize("size", SIZES)
    @pytest.mark.parametrize("place", ["below", "right"])
    @pytest.mark.parametrize("spacing", ["comfortable", "compact", "dense"])
    def test_everything_has_room(self, qapp, window, dressed, size, place,
                                 spacing):
        dressed(spacing)
        window.resize(*size)
        window.set_preview_position(place)
        window.table.selectRow(0)
        qapp.processEvents()
        assert not faults(window), (
            f"at {size[0]}x{size[1]} with the preview {place}:\n  "
            + "\n  ".join(faults(window)))

    def test_the_preview_keeps_its_words_at_the_narrowest(self, qapp,
                                                          window):
        """The narrowest the preview is ever given: beside the table on
        an 800px window. "Source:", a box holding "Exactly what the model
        was sent" and an Attachments button need 385px and the half they
        sit in is 306, so the row has to wrap - squeezed onto one line the
        box elides what is in it."""
        window.resize(800, 580)
        window.set_preview_position("right")
        window.table.selectRow(0)
        qapp.processEvents()
        from PySide6.QtGui import QFontMetrics

        preview = window.preview
        # Against the words rather than against sizeHint. A hint is the
        # size a control would like, padding and all, and the roomiest of
        # the three densities pads a box by more than a hundred pixels -
        # losing some of that is not losing any of the text.
        for control in (preview.body_mode, preview.folder_combo):
            metrics = QFontMetrics(control.font())
            words = max(metrics.horizontalAdvance(control.itemText(i))
                        for i in range(control.count()) or [0]) if \
                control.count() else 0
            assert control.width() >= words + 40, (
                f"the box is {control.width()}px and the longest thing in "
                f"it is {words}px of text plus an arrow")
        for control in (preview.attachments_button, preview.reset_button):
            metrics = QFontMetrics(control.font())
            words = metrics.horizontalAdvance(control.text())
            assert control.width() >= words + 8, (
                f"{control.text()!r} is on a button {control.width()}px "
                f"wide and the words alone are {words}px")

    @pytest.mark.parametrize("size", [(1600, 1000), (800, 580)])
    def test_the_preview_can_be_shut_and_opened(self, qapp, window, size):
        window.resize(*size)
        qapp.processEvents()
        for sizes in (([1, 0], "shut"), ([1, 1], "open")):
            window.splitter.setSizes(sizes[0])
            qapp.processEvents()
            assert not faults(window), (
                f"at {size[0]}x{size[1]} with the preview {sizes[1]}:\n  "
                + "\n  ".join(faults(window)))


class TestTheAttachmentViewer:
    """Where this session's controls live: the waveform, the strobe pair
    and the scene-specific boxes."""

    SIZES = ((1400, 900), (1000, 700), (820, 560))
    #: The scenes that bring extra controls with them.
    SCENES = ("Rave", "Oscilloscope", "Music rider", "VU meters")

    @staticmethod
    def _pane(qapp, size, scene, source="Manual"):
        from attachment_view import AudioPane

        pane = AudioPane()
        pane.resize(*size)
        pane.show()
        pane.enable_box.setChecked(True)
        pane.scene_box.setCurrentText(scene)
        pane.spectrum.set_frames(
            [array("f", [0.4] * attachment_audio.BANDS) for _ in range(200)],
            attachment_audio.RATE)
        pane.wave.set_shape([0.4 + 0.5 * (i % 7) / 7.0 for i in range(400)])
        pane.wave.set_span(180_000)
        pane.wave.set_position(60_000)
        pane.strobe_source.setCurrentText(source)
        qapp.processEvents()
        return pane

    @pytest.mark.parametrize("size", SIZES)
    @pytest.mark.parametrize("scene", SCENES)
    def test_everything_has_room(self, qapp, size, scene):
        pane = self._pane(qapp, size, scene)
        try:
            assert not faults(pane), (
                f"at {size[0]}x{size[1]} showing {scene}:\n  "
                + "\n  ".join(faults(pane)))
        finally:
            pane.close()
            pane.deleteLater()

    @pytest.mark.parametrize("size", SIZES)
    def test_the_waveform_is_up_and_inside_the_pane(self, qapp, size):
        pane = self._pane(qapp, size, "Rave")
        try:
            assert not pane.wave.isHidden(), (
                "the track has a shape and the bar is not showing it")
            assert pane.wave.width() <= pane.width(), (
                f"the waveform is {pane.wave.width()}px wide in a "
                f"{pane.width()}px pane")
            assert pane.wave.height() >= 20, (
                f"the waveform is {pane.wave.height()}px tall, which is not "
                f"enough to read a track off")
        finally:
            pane.close()
            pane.deleteLater()

    @pytest.mark.parametrize("source", ["Manual", "Bass"])
    def test_the_strobe_controls_fit_either_way_round(self, qapp, source):
        """The captions change with the mode, and "shape" is wider than
        "sens"."""
        pane = self._pane(qapp, (820, 560), "Rave", source=source)
        try:
            assert not faults(pane), (
                f"with the strobe on {source}:\n  " + "\n  ".join(faults(pane)))
        finally:
            pane.close()
            pane.deleteLater()


class TestTheFullScreenView:
    """No furniture, and the controls that do come up have to fit."""

    @pytest.mark.parametrize("size", [(1920, 1080), (1512, 982), (1280, 800)])
    def test_everything_has_room(self, qapp, size):
        from attachment_view import AudioPane
        from attachment_widgets import FullScreenSpectrum

        pane = AudioPane()
        pane.enable_box.setChecked(True)
        pane.spectrum.set_frames(
            [array("f", [0.4] * attachment_audio.BANDS) for _ in range(200)],
            attachment_audio.RATE)
        full = FullScreenSpectrum(pane.spectrum, pane)
        full.resize(*size)
        full.show()
        qapp.processEvents()
        try:
            assert not faults(full), (
                f"full screen at {size[0]}x{size[1]}:\n  "
                + "\n  ".join(faults(full)))
        finally:
            full.close()
            full.deleteLater()
            pane.close()
            pane.deleteLater()


class TestTheDialogs:
    """Everything the window can put in front of itself."""

    @pytest.mark.parametrize("size", [(1200, 800), (900, 650), (760, 540)])
    def test_the_briefing_has_room(self, qapp, window, size):
        import briefing
        import briefing_dialog

        report = briefing.build(window.model.items, mailboxes=("Inbox",))
        dialog = briefing_dialog.BriefingDialog(report, parent=window)
        dialog.resize(min(size[0], 900), min(size[1], 700))
        dialog.show()
        qapp.processEvents()
        try:
            assert not faults(dialog), (
                f"the briefing at {size[0]}x{size[1]}:\n  "
                + "\n  ".join(faults(dialog)))
        finally:
            dialog.close()
            dialog.deleteLater()

    @pytest.mark.parametrize("size", [(1200, 800), (900, 650), (760, 540)])
    def test_the_settings_have_room(self, qapp, window, size):
        from settings_dialog import SettingsDialog

        dialog = SettingsDialog(window.settings, InMemoryCredentialStore(),
                                parent=window)
        dialog.resize(min(size[0], 900), min(size[1], 700))
        dialog.show()
        qapp.processEvents()
        try:
            assert not faults(dialog), (
                f"the settings at {size[0]}x{size[1]}:\n  "
                + "\n  ".join(faults(dialog)))
        finally:
            dialog.close()
            dialog.deleteLater()
