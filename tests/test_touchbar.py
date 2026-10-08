"""The Touch Bar: items bound to the controls they mirror, kept in step,
and each window's bar. tests/test_touchbar_mac.py draws them on AppKit."""

from __future__ import annotations

import math
import struct
import types

import pytest


@pytest.fixture
def recorder(qapp):
    """Draw with a renderer that writes down what it was asked to do."""
    import touchbar

    class Recorder:
        def __init__(self):
            self.calls = []

        def build(self, bar):
            self.calls.append(("build", bar.name))
            return types.SimpleNamespace(bar=bar, released=0)

        def arrange(self, handle, arranged):
            self.calls.append(("arrange", arranged))

        def update(self, handle, key, state):
            self.calls.append(("update", key, state))

        def attach(self, handle, window):
            self.calls.append(("attach", window))
            return True

        def release(self, handle):
            handle.released += 1
            self.calls.append(("release", handle.bar.name))

        def updates(self, key):
            return [call[2] for call in self.calls
                    if call[0] == "update" and call[1] == key]

    made = Recorder()
    touchbar.use(made)
    yield made
    touchbar.use(None)


def _window(qtbot):
    from PySide6.QtWidgets import QVBoxLayout, QWidget

    window = QWidget()
    QVBoxLayout(window)
    qtbot.addWidget(window)
    return window


def _add(window, widget):
    widget.setParent(window)
    window.layout().addWidget(widget)
    return widget


class TestItemsMirrorTheirControls:
    def test_a_button_says_what_its_action_says_and_presses_it(self, qapp):
        from PySide6.QtGui import QAction

        import touchbar

        action = QAction("&Undo Last Filing")
        fired = []
        action.triggered.connect(lambda: fired.append(1))
        item = touchbar.Button("undo", "Undo", action)
        assert item.state()["title"] == "Undo Last Filing"
        item.act()
        assert fired == [1]
        action.setEnabled(False)
        assert item.state()["enabled"] is False
        item.act()
        assert fired == [1], "a disabled action was pressed"
        action.setVisible(False)
        assert not item.present()

    def test_a_button_takes_its_colour_from_the_buttons_role(self, qtbot):
        from PySide6.QtWidgets import QPushButton

        import touchbar
        from widgets import _paint_button

        window = _window(qtbot)
        button = _add(window, QPushButton("Scan && Analyze"))
        item = touchbar.Button("scan", "Scan", button)
        assert item.state()["title"] == "Scan & Analyze"
        assert item.state()["role"] is None
        button.setDefault(True)
        assert item.state()["role"] == "primary"
        _paint_button(button, "danger")
        assert item.state()["role"] == "danger"

    def test_a_toggle_follows_its_tick_box_and_sets_it(self, qtbot):
        from PySide6.QtWidgets import QCheckBox

        import touchbar

        window = _window(qtbot)
        box = _add(window, QCheckBox("Strobe"))
        clicked = []
        box.clicked.connect(lambda on: clicked.append(on))
        item = touchbar.Toggle("strobe", "Strobe", box)
        assert item.state()["on"] is False
        item.act(True)
        assert box.isChecked() and clicked == [True]
        item.act(True)
        assert clicked == [True], "set to what it already was"
        item.act()
        assert not box.isChecked()
        box.setEnabled(False)
        item.act(True)
        assert not box.isChecked(), "a disabled box was ticked"

    def test_a_choice_follows_a_combo_box_and_chooses(self, qtbot):
        from PySide6.QtWidgets import QComboBox

        import touchbar

        window = _window(qtbot)
        combo = _add(window, QComboBox())
        combo.addItems(["Show: everything", "Show: job mail only"])
        item = touchbar.Choice("show", "Show", combo,
                               short={"Show: everything": "All"})
        assert item.state()["options"] == ["All", "Show: job mail only"]
        item.act(1)
        assert combo.currentIndex() == 1
        assert item.state()["index"] == 1
        item.act(7)
        assert combo.currentIndex() == 1, "an option that is not there"
        combo.addItem("Show: ticked only")
        assert len(item.state()["options"]) == 3
        # A disabled combo box still takes a new index from code.
        combo.setEnabled(False)
        item.act(0)
        assert combo.currentIndex() == 1, "a disabled combo box was changed"

    def test_a_choice_runs_its_before_and_its_own_enabled(self, qtbot):
        from PySide6.QtWidgets import QComboBox

        import touchbar

        window = _window(qtbot)
        combo = _add(window, QComboBox())
        combo.addItems(["Rave", "Waterfall"])
        combo.setEnabled(False)
        said = []
        item = touchbar.Choice("scene", "Scene", combo, enabled=True,
                               before=lambda: said.append("on"))
        item.act(1)
        assert said == ["on"] and combo.currentIndex() == 1
        item.act(1)
        assert said == ["on"], "the same choice again ran before"

    def test_a_choice_over_buttons_or_rebuilt_actions(self, qtbot):
        from PySide6.QtGui import QAction
        from PySide6.QtWidgets import QToolButton

        import touchbar

        window = _window(qtbot)
        buttons = []
        for text in ("24 hours", "3 days"):
            button = _add(window, QToolButton())
            button.setText(text)
            button.setCheckable(True)
            button.setAutoExclusive(True)
            buttons.append(button)
        buttons[0].setChecked(True)
        item = touchbar.Choice("period", "Period", buttons)
        item.act(1)
        assert buttons[1].isChecked() and item.state()["index"] == 1

        menu = {"actions": []}

        def rebuild(checked):
            menu["actions"] = []
            for index, text in enumerate(("Claude", "Gemini")):
                action = QAction(text)
                action.setCheckable(True)
                action.setChecked(index == checked)
                action.triggered.connect(lambda _=False, i=index: rebuild(i))
                menu["actions"].append(action)

        rebuild(0)
        chosen = touchbar.Choice("model", "Model", lambda: menu["actions"])
        chosen.act(1)
        assert chosen.state() == {"options": ["Claude", "Gemini"], "index": 1,
                                  "title": "Gemini", "enabled": True}

    def test_a_slider_waits_to_settle_and_holds_its_value(self, qtbot):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QSlider

        import touchbar

        window = _window(qtbot)
        slider = _add(window, QSlider(Qt.Orientation.Horizontal))
        slider.setRange(0, 1000)
        seeks = []
        item = touchbar.Slider("seek", "", slider, settle=80,
                               change=seeks.append)
        item.act(400.4)
        item.act(611.6)
        assert seeks == [], "acted before it settled"
        assert item.state()["value"] == 612, "the knob was pulled back"
        qtbot.waitUntil(lambda: seeks == [612], timeout=1000)
        item.act(5000)
        qtbot.waitUntil(lambda: seeks[-1] == 1000, timeout=1000)
        slider.setEnabled(False)
        plain = touchbar.Slider("volume", "", slider)
        plain.act(10)
        assert slider.value() == 0, "a disabled slider was moved"

    def test_a_popover_is_there_while_anything_in_it_is(self, qtbot):
        from PySide6.QtWidgets import QPushButton

        import touchbar

        window = _window(qtbot)
        button = _add(window, QPushButton("Colours"))
        item = touchbar.Popover("more", "More", [
            touchbar.Button("colours", "Colours", button)])
        assert item.present()
        button.hide()
        assert not item.present()

    def test_a_popover_held_opens_onto_a_slider_that_follows_the_finger(
            self, qtbot, recorder):
        """Hold and drag, as the brightness control: the held slider is an
        item of its own, mirrored to the same control, kept out of the
        popover's own bar."""
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QSlider

        import touchbar

        window = _window(qtbot)
        slider = _add(window, QSlider(Qt.Orientation.Horizontal))
        slider.setRange(0, 100)
        held = touchbar.Slider("glow-held", "Glow", slider)
        item = touchbar.Popover("beam", "Beam", [
            touchbar.Slider("glow", "Glow", slider, width=140)], hold=held)
        bar = touchbar.give(window, [item], "beam-test")
        window.show()
        assert item.hold is held and held in item.children()
        assert bar.arrangement()["beam"] == ["glow"], "the held one has its own bar"
        described = bar.describe()[0]
        assert described["hold"]["key"] == "glow-held"
        assert [entry["key"] for entry in described["items"]] == ["glow"]
        assert bar.press("glow-held", 42.4)
        assert slider.value() == 42
        slider.setValue(70)
        qtbot.waitUntil(lambda: bool(recorder.updates("glow-held"))
                        and recorder.updates("glow-held")[-1]["value"] == 70,
                        timeout=2000)

    def test_only_when_adds_to_an_items_own_condition(self, qapp):
        import touchbar

        flags = {"pane": True, "own": True}
        item = touchbar.Button("x", "X", lambda: None,
                               when=lambda: flags["own"])
        touchbar.only_when([item], lambda: flags["pane"])
        assert item.present()
        flags["pane"] = False
        assert not item.present()
        flags.update(pane=True, own=False)
        assert not item.present()

    def test_two_items_with_one_key_are_refused(self, qtbot):
        import touchbar

        window = _window(qtbot)
        with pytest.raises(ValueError):
            touchbar.give(window, [
                touchbar.Button("same", "A", lambda: None),
                touchbar.Popover("more", "More", [
                    touchbar.Button("same", "B", lambda: None)])], "twice")


class TestABarKeepsInStep:
    def _bar(self, qtbot, recorder, tracked=True):
        from PySide6.QtWidgets import QCheckBox, QComboBox, QVBoxLayout, QWidget

        import touchbar

        if tracked:
            window = _window(qtbot)
        else:
            window = QWidget()
            QVBoxLayout(window)
        box = _add(window, QCheckBox("Strobe"))
        combo = _add(window, QComboBox())
        combo.addItems(["A", "B"])
        bar = touchbar.give(window, [
            touchbar.Toggle("strobe", "Strobe", box),
            touchbar.Choice("pick", "Pick", combo,
                            when=box.isChecked, watch=[box.toggled]),
        ], "probe")
        window.show()
        qtbot.waitUntil(lambda: bar._handle is not None, timeout=1000)
        return window, box, combo, bar

    def test_it_is_built_and_attached_when_its_window_shows(
            self, qtbot, recorder):
        window, _box, _combo, _bar = self._bar(qtbot, recorder)
        names = [call[0] for call in recorder.calls]
        assert names[:2] == ["build", "attach"]
        assert ("arrange", {"": ["strobe"]}) in recorder.calls

    def test_only_what_changed_is_sent(self, qtbot, recorder):
        window, box, combo, bar = self._bar(qtbot, recorder)
        sent = len(recorder.updates("pick"))
        box.setChecked(True)
        qtbot.waitUntil(lambda: recorder.updates("strobe")[-1]["on"],
                        timeout=1000)
        assert recorder.calls[-3][0] == "arrange" or any(
            call == ("arrange", {"": ["strobe", "pick"]})
            for call in recorder.calls)
        assert len(recorder.updates("pick")) == sent, \
            "an unchanged item was sent again"
        combo.setCurrentIndex(1)
        qtbot.waitUntil(lambda: recorder.updates("pick")[-1]["index"] == 1,
                        timeout=1000)

    def test_a_refused_press_is_sent_back(self, qtbot, recorder):
        window, box, combo, bar = self._bar(qtbot, recorder)
        box.setEnabled(False)
        qtbot.waitUntil(
            lambda: recorder.updates("strobe")[-1]["enabled"] is False,
            timeout=1000)
        sent = len(recorder.updates("strobe"))
        assert bar.press("strobe", True)
        assert not box.isChecked()
        qtbot.waitUntil(lambda: len(recorder.updates("strobe")) > sent,
                        timeout=1000)
        assert recorder.updates("strobe")[-1]["on"] is False

    def test_a_failing_press_does_not_escape(self, qtbot, recorder):
        import touchbar

        window = _window(qtbot)

        def broken():
            raise RuntimeError("no")

        bar = touchbar.give(window, [touchbar.Button("x", "X", broken)], "b")
        assert bar.press("x") is False
        assert bar.press("nothing") is False

    def test_a_failing_renderer_turns_the_bar_off(self, qtbot, recorder):
        window, box, combo, bar = self._bar(qtbot, recorder)

        def refuse(*_args):
            raise RuntimeError("AppKit said no")

        recorder.update = refuse
        box.setChecked(True)
        qtbot.waitUntil(lambda: bar._handle is None, timeout=1000)
        assert recorder.calls[-1] == ("release", "probe")

    def test_its_window_going_releases_it_once(self, qtbot, recorder, qapp):
        from PySide6.QtCore import QCoreApplication, QEvent

        import touchbar

        window, box, combo, bar = self._bar(qtbot, recorder, tracked=False)
        handle = bar._handle
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        qapp.processEvents()
        assert handle.released == 1
        assert all(kept is not bar for kept in touchbar._bars)

    def test_a_second_bar_replaces_the_first(self, qtbot, recorder):
        import touchbar

        window, box, combo, first = self._bar(qtbot, recorder)
        handle = first._handle
        second = touchbar.give(window, [
            touchbar.Toggle("strobe", "Strobe", box)], "again")
        assert touchbar.of(window) is second
        assert handle.released == 1


class TestTheMainWindowsBar:
    @pytest.fixture
    def window(self, qapp, qtbot):
        from config import InMemoryCredentialStore, Settings
        from gui import MainWindow

        made = MainWindow(Settings(icloud_email="you@icloud.example"),
                          InMemoryCredentialStore())
        qtbot.addWidget(made)
        yield made
        made.close()

    def test_what_it_shows(self, window):
        import touchbar

        bar = touchbar.of(window)
        assert bar.customizable
        arranged = bar.arrangement()
        top = [key for key in arranged[""] if not key.startswith("space")]
        assert top == ["scan", "apply", "undo", "show", "category", "ticks",
                       "more", "options"]
        assert arranged["more"] == ["visualise", "briefing", "find", "new-message",
                                    "open-message", "links", "clear", "rescan",
                                    "replies"]
        assert bar.flat["links"].state()["enabled"] is False, \
            "Links with no message open"
        assert arranged["options"] == ["period", "model", "help", "settings"]

    def test_scan_becomes_stop_while_it_works(self, window):
        import touchbar

        bar = touchbar.of(window)
        assert bar.flat["scan"].state()["title"] == "Scan"
        window._set_busy(True, "Working")
        state = bar.flat["scan"].state()
        assert (state["title"], state["image"], state["role"]) == (
            "Stop", "stop.fill", "danger")
        window._set_busy(False)
        assert bar.flat["scan"].state()["role"] == "primary"

    def test_pressing_scan_presses_the_button(self, window):
        import touchbar

        pressed = []
        window.scan_button.clicked.disconnect()
        window.scan_button.clicked.connect(lambda: pressed.append(1))
        assert touchbar.of(window).press("scan")
        assert pressed == [1]

    def test_the_filters_and_the_period(self, window):
        import touchbar

        bar = touchbar.of(window)
        assert bar.flat["show"].state()["options"] == [
            "All", "Job mail", "Not job", "Ticked"]
        bar.press("show", 3)
        assert window.show_combo.currentIndex() == 3
        bar.press("period", 2)
        assert list(window.window_buttons.values())[2].isChecked()

    def test_the_model_list_is_the_menus_and_switches_it(self, window):
        import touchbar

        bar = touchbar.of(window)
        state = bar.flat["model"].state()
        assert state["options"][state["index"]] == "Built-in rule set"
        target = state["options"].index("Claude Opus 5")
        bar.press("model", target)
        assert window.settings.model == "claude-opus-5"
        assert bar.flat["model"].state()["index"] == target, \
            "the rebuilt menu was not read"

    def test_ticks_and_apply(self, window, item_factory):
        import touchbar

        bar = touchbar.of(window)
        window.model.set_items([item_factory(), item_factory()])
        window._update_status()
        bar.press("ticks-all")
        approved = window.model.summary().approved
        assert approved
        assert bar.flat["apply"].state()["title"] == f"Apply {approved}"


def _tone(seconds: float = 1.0, rate: int = 22050) -> bytes:
    count = int(rate * seconds)
    pcm = b"".join(struct.pack("<h", int(9000 * math.sin(
        2 * math.pi * 220.0 * i / rate))) for i in range(count))
    return (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", len(pcm)) + pcm)


class TestTheViewersBar:
    @pytest.fixture
    def viewer(self, qtbot):
        import attachments
        from attachment_view import AttachmentViewer

        data = _tone()
        found = [attachments.Attachment(part="1", name="tone.wav",
                                        content_type="audio/wav",
                                        size=len(data), data=data),
                 attachments.Attachment(part="2", name="note.txt",
                                        content_type="text/plain", size=5,
                                        data=b"hello")]
        made = AttachmentViewer(found)
        qtbot.addWidget(made)
        made.show()
        made.list.setCurrentRow(0)
        return made

    def _keys(self, viewer):
        import touchbar

        return [entry["key"] for entry in touchbar.of(viewer).describe()]

    def test_a_track_with_the_visualiser_off_and_on(self, viewer, qtbot):
        qtbot.waitUntil(lambda: viewer.stack.currentWidget() is viewer.audio,
                        timeout=3000)
        keys = self._keys(viewer)
        assert {"play", "seek", "visualiser", "sound"} <= set(keys)
        assert "scene" not in keys and "picture" not in keys
        viewer.audio.enable_box.setChecked(True)
        keys = self._keys(viewer)
        assert {"play", "scene", "strobe", "picture", "full", "sound"} <= set(keys)
        assert "seek" not in keys

    def test_the_volume_is_a_button_to_tap_or_hold_with_the_picture_off_or_on(
            self, viewer, qtbot):
        """A plain slider on the bar jumps to where it is touched, which for
        a volume can mean all the way up; the system's own volume is a
        button that opens a slider and follows a held finger from where the
        volume was."""
        import touchbar

        qtbot.waitUntil(lambda: viewer.stack.currentWidget() is viewer.audio,
                        timeout=3000)
        bar = touchbar.of(viewer)
        for picture in (False, True):
            viewer.audio.enable_box.setChecked(picture)
            shown = {entry["key"]: entry for entry in bar.describe()}
            assert shown["sound"]["kind"] == "popover"
            assert shown["sound"]["hold"]["key"] == "volume-held"
            plain = [key for key, entry in shown.items()
                     if entry["kind"] == "slider"
                     and bar.flat[key].source is viewer.audio.volume]
            assert plain == [], picture

    def test_choosing_a_scene_turns_the_visualiser_on(self, viewer, qtbot):
        import touchbar
        import visualizers

        qtbot.waitUntil(lambda: viewer.stack.currentWidget() is viewer.audio,
                        timeout=3000)
        bar = touchbar.of(viewer)
        names = [scene.name for scene in visualizers.SCENES]
        assert not bar.press("scene", 0), "a scene chosen while it was off"
        assert bar.press("visualiser", True)
        assert viewer.audio.enable_box.isChecked()
        assert bar.press("scene", names.index("Music rider"))
        assert viewer.audio.scene_box.currentText() == "Music rider"
        keys = self._keys(viewer)
        assert "game" in keys and "beam" not in keys
        game = next(entry for entry in bar.describe() if entry["key"] == "game")
        assert [child["key"] for child in game["items"]] == [
            "game-mode", "level", "sounds", "effects"]
        bar.press("level", 3)
        assert viewer.audio.level_box.currentText() == "Expert"

    def test_play_shows_what_pressing_it_will_do(self, viewer, qtbot):
        import touchbar

        bar = touchbar.of(viewer)
        assert bar.flat["play"].state()["image"] == "play.fill"
        viewer.audio._playing = False
        heard = []
        viewer.audio.playingChanged.connect(heard.append)
        viewer.audio._playing = True
        assert bar.flat["play"].state()["image"] == "pause.fill"

    def test_text_has_its_own_and_the_list_moves(self, viewer, qtbot):
        import touchbar

        bar = touchbar.of(viewer)
        assert bar.press("next")
        qtbot.waitUntil(lambda: viewer.list.currentRow() == 1, timeout=1000)
        qtbot.waitUntil(lambda: viewer.stack.currentWidget() is viewer.text,
                        timeout=3000)
        keys = self._keys(viewer)
        assert "wrap" in keys and "play" not in keys

    def test_the_full_screen_has_a_bar_that_leaves(self, viewer, qtbot):
        import touchbar

        qtbot.waitUntil(lambda: viewer.stack.currentWidget() is viewer.audio,
                        timeout=3000)
        viewer.audio.enable_box.setChecked(True)
        viewer.audio._go_full_screen()
        full = viewer.audio._full
        bar = touchbar.of(full)
        keys = [entry["key"] for entry in bar.describe()]
        assert {"play", "scene", "strobe", "leave"} <= set(keys)
        assert "full" not in keys and "seek" not in keys
        assert bar.press("leave")
        qtbot.waitUntil(lambda: not full.isVisible(), timeout=2000)


class TestDialogs:
    def test_any_dialog_gets_its_pages_and_buttons(self, qtbot):
        from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QTabWidget,
                                       QVBoxLayout, QWidget)

        import touchbar

        dialog = QDialog()
        qtbot.addWidget(dialog)
        layout = QVBoxLayout(dialog)
        tabs = QTabWidget()
        tabs.addTab(QWidget(), "&One")
        tabs.addTab(QWidget(), "Two")
        layout.addWidget(tabs)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                               | QDialogButtonBox.StandardButton.Cancel)
        gone = box.addButton("Delete", QDialogButtonBox.ButtonRole.DestructiveRole)
        box.button(QDialogButtonBox.StandardButton.Ok).setDefault(True)
        layout.addWidget(box)
        dialog.show()
        bar = touchbar.give(dialog, touchbar.dialog_items(dialog), "dialog")
        shown = {entry["key"]: entry for entry in bar.describe()}
        assert shown["pages"]["options"] == ["One", "Two"]
        roles = {entry["title"]: entry["role"] for entry in shown.values()
                 if entry["kind"] == "button"}
        assert roles["OK"] == "primary" and roles["Delete"] == "destructive"
        assert roles["Cancel"] is None
        bar.press("pages", 1)
        assert tabs.currentIndex() == 1
        gone.hide()
        assert "Delete" not in {entry.get("title") for entry in bar.describe()}

    def test_a_dialog_coming_forward_is_given_a_bar(self, qtbot, recorder):
        from PySide6.QtWidgets import QDialog, QDialogButtonBox, QVBoxLayout

        import touchbar

        dialog = QDialog()
        qtbot.addWidget(dialog)
        QVBoxLayout(dialog).addWidget(QDialogButtonBox(
            QDialogButtonBox.StandardButton.Close))
        dialog.show()
        assert touchbar.of(dialog) is None
        touchbar._focus_changed(dialog.windowHandle())
        bar = touchbar.of(dialog)
        assert bar is not None and bar.name == "dialog"
        assert ("build", "dialog") in recorder.calls
        touchbar._focus_changed(dialog.windowHandle())
        assert touchbar.of(dialog) is bar, "a second bar was made"

    def test_a_wizard_gets_its_own_buttons(self, qtbot):
        from PySide6.QtWidgets import QWizard, QWizardPage

        import touchbar

        wizard = QWizard()
        qtbot.addWidget(wizard)
        wizard.addPage(QWizardPage())
        wizard.addPage(QWizardPage())
        wizard.show()
        bar = touchbar.give(wizard, touchbar.dialog_items(wizard), "wizard")
        titles = [entry["title"] for entry in bar.describe()
                  if entry["kind"] == "button"]
        assert "Cancel" in titles
        assert any(title in ("Next", "Continue") for title in titles), titles

    def test_settings_shows_each_pages_choices(self, qtbot):
        from config import InMemoryCredentialStore, Settings
        from settings_dialog import SettingsDialog

        import touchbar

        dialog = SettingsDialog(Settings(icloud_email="you@icloud.example"),
                                InMemoryCredentialStore())
        qtbot.addWidget(dialog)
        bar = touchbar.of(dialog)
        keys = lambda: {entry["key"] for entry in bar.describe()}
        dialog.tabs.setCurrentIndex(5)
        assert {"pages", "mode", "contrast", "density"} <= keys()
        assert "provider" not in keys()
        bar.press("mode", 2)
        assert dialog.mode_combo.currentText() == "Always dark"
        bar.press("pages", 1)
        assert dialog.tabs.currentIndex() == 1
        assert "provider" in keys() and "mode" not in keys()
        titles = {entry.get("title") for entry in bar.describe()}
        assert {"Save", "Cancel"} <= titles

    @pytest.mark.parametrize("build", ["update", "links", "about",
                                       "colours"])
    def test_dialogs_built_without_a_box_have_bars(self, qtbot, build):
        import touchbar

        if build == "update":
            import updates
            from update_dialog import UpdateDialog

            dialog = UpdateDialog(updates.Release(
                version="9.9.9", notes="", url="", sha256="", size=0,
                page="https://example.com"))
            expected = {"skip", "later", "update"}
        elif build == "links":
            from link_open import LinkList

            dialog = LinkList(["https://example.com/a"])
            expected = {"copy", "close", "open"}
        elif build == "about":
            from about import AboutDialog
            from config import InMemoryCredentialStore, Settings

            dialog = AboutDialog(Settings(), InMemoryCredentialStore())
            expected = {"security", "bug", "source", "copy"}
        else:
            from PySide6.QtGui import QColor

            from colour_picker import ColourWindow

            dialog = ColourWindow(QColor("red"), QColor("black"))
            expected = {"load", "aim", "reset", "close"}
        qtbot.addWidget(dialog)
        bar = touchbar.of(dialog)
        assert bar is not None
        assert expected <= {entry["key"] for entry in bar.describe()}


def test_every_message_the_mac_side_sends_is_checked_first():
    """A message AppKit does not understand ends the process, so each one
    sent must be in NEEDED, which is checked before anything is drawn; and
    NEEDED holds nothing else, or it checks for something never sent."""
    import ast
    from pathlib import Path

    import touchbar_mac

    source = Path(touchbar_mac.__file__).read_text()
    sent = set()
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "send" and len(node.args) >= 2):
            selector = node.args[1]
            if isinstance(selector, ast.Constant):
                sent.add(selector.value)
            else:
                # The one message chosen at run time: a colour by role.
                sent |= set(touchbar_mac.BEZEL.values())
    needed = {selector for selectors in touchbar_mac.NEEDED.values()
              for selector in selectors}
    assert sent - needed == set(), "sent without being checked"
    assert needed - sent == set(), "checked but never sent"
