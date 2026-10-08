"""The Touch Bar drawn on AppKit, on the real platform.

The rest of the suite runs offscreen, where there is no AppKit to draw on.
These run a script in a subprocess on the Mac's own platform, once for each
architecture this Python can run as, and check what AppKit itself holds:
the bar on the window, the items on it, and a press of each kind sent the
way AppKit sends it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from test_gpu_canvas import real_platform_or_skip

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(sys.platform != "darwin",
                                reason="AppKit is macOS only")

HEAD = textwrap.dedent("""
    import ctypes, json, os, sys, tempfile
    os.environ["ICLOUD_TRIAGE_HOME"] = tempfile.mkdtemp()
    sys.path.insert(0, {root!r})
    from PySide6.QtCore import QEventLoop, QTimer, Qt
    from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox,
                                   QPushButton, QSlider, QVBoxLayout, QWidget)
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("Mail Manager")
    import touchbar
    if not touchbar.install(app):
        print(json.dumps({{"skip": "no Touch Bar support here"}}))
        sys.exit(0)
    renderer = touchbar._renderer
    rt = renderer.rt
    _id = ctypes.c_void_p

    def spin(ms=150):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    def send_action(control):
        rt.send(control, "sendAction:to:", rt.send(control, "action"),
                rt.send(control, "target"), restype=ctypes.c_bool,
                argtypes=[_id, _id])

    out = {{}}
""")


#: What AppKit logs, once a process, when its own NSSliderTouchBarItem builds
#: its slider: a width constraint with a constant past its own limit, which
#: it then substitutes. Seen with a bare item and nothing of ours set on it,
#: so it is AppKit's, and the press-and-hold bar needs that item (AppKit
#: hands the held finger to it; a slider in a custom item gets nothing).
#: Every other layout complaint is still ours.
APPKITS_OWN = "_NSLayoutConstraintNumberExceedsLimit"


def layout_complaints(stderr: str) -> list:
    """AppKit's complaints about a layout, bar the one that is its own."""
    return [line for line in stderr.splitlines()
            if "NSLayoutConstraint" in line and APPKITS_OWN not in line]


def _architectures():
    found = [os.uname().machine]
    if found[0] == "arm64" and shutil.which("arch"):
        lipo = subprocess.run(["lipo", "-archs", sys.executable],
                              capture_output=True, text=True)
        if "x86_64" in lipo.stdout.split():
            probe = subprocess.run(["arch", "-x86_64", sys.executable, "-c",
                                    "import PySide6.QtWidgets"],
                                   capture_output=True)
            if probe.returncode == 0:
                found.append("x86_64")
    return found


def _run(arch: str, *parts: str) -> dict:
    real_platform_or_skip()
    script = HEAD.format(root=str(ROOT)) + "".join(
        textwrap.dedent(part) for part in parts) + "\nprint(json.dumps(out))\n"
    env = {key: value for key, value in os.environ.items()
           if key != "QT_QPA_PLATFORM"}
    command = [sys.executable, "-c", script]
    if arch != os.uname().machine:
        command = ["arch", f"-{arch}"] + command
    done = subprocess.run(command, capture_output=True, text=True,
                          timeout=120, env=env, cwd=str(ROOT))
    lines = [line for line in done.stdout.splitlines() if line.startswith("{")]
    assert done.returncode == 0 and lines, done.stderr[-3000:]
    result = json.loads(lines[-1])
    if "skip" in result:
        pytest.skip(result["skip"])
    result["stderr"] = done.stderr
    return result


@pytest.fixture(params=_architectures())
def arch(request):
    return request.param


def test_every_message_it_sends_is_answered_here(arch):
    result = _run(arch, """
        import touchbar_mac
        out["missing"] = rt.missing()
        touchbar_mac.NEEDED["NSTouchBar"] += ("noSuchMessage:",)
        out["caught"] = rt.missing()
    """)
    assert result["missing"] == []
    assert result["caught"] == ["NSTouchBarnoSuchMessage:"]


BAR = """
    window = QWidget()
    layout = QVBoxLayout(window)
    clicks = []
    button = QPushButton("Scan && Analyze")
    button.clicked.connect(lambda: clicks.append(1))
    box = QCheckBox("Strobe")
    combo = QComboBox()
    combo.addItems(["All", "Job", "Other", "Ticked"])
    strip = QComboBox()
    strip.addItems([f"Category {i}" for i in range(12)])
    slider = QSlider(Qt.Orientation.Horizontal)
    slider.setRange(0, 100)
    slider.setValue(70)
    for widget in (button, box, combo, strip, slider):
        layout.addWidget(widget)
    bar = touchbar.give(window, [
        touchbar.Button("scan", "Scan", button, title="Scan",
                        image="arrow.clockwise", role="primary"),
        touchbar.Toggle("strobe", "Strobe", box),
        touchbar.Choice("show", "Show", combo),
        touchbar.Choice("category", "Category", strip, style="popover"),
        touchbar.Space("flexible"),
        touchbar.Popover("more", "More", [
            touchbar.Slider("volume", "Volume", slider,
                            ends=("speaker.fill", "speaker.wave.3.fill"))],
            hold=touchbar.Slider("volume-held", "Volume", slider,
                                 ends=("speaker.fill", "speaker.wave.3.fill"))),
    ], "probe")
    window.show()
    spin(400)
    handle = bar._handle
"""


def test_a_bar_is_put_on_its_window(arch):
    result = _run(arch, BAR, """
        out["attached"] = renderer.window_bar(window) == handle.touchbar
        out["top"] = renderer.identifiers(handle.touchbar)
        out["nested"] = renderer.identifiers(handle.nested["more"])
        item = handle.items["scan"]
        out["title"] = rt.text(rt.send(item, "title"))
        out["show"] = [rt.text(rt.send(handle.controls["show"],
                                       "labelForSegment:", i,
                                       argtypes=[ctypes.c_long]))
                       for i in range(4)]
        out["strip"] = rt.send(handle.controls["category"], "numberOfItems",
                               restype=ctypes.c_long)
    """)
    assert result["attached"]
    assert result["top"] == [
        "com.mailmanager.probe.scan", "com.mailmanager.probe.strobe",
        "com.mailmanager.probe.show", "com.mailmanager.probe.category",
        "NSTouchBarItemIdentifierFlexibleSpace", "com.mailmanager.probe.more"]
    assert result["nested"] == ["com.mailmanager.probe.volume"]
    assert result["title"] == "Scan"
    assert result["show"] == ["All", "Job", "Other", "Ticked"]
    assert result["strip"] == 12
    assert not layout_complaints(result["stderr"]), \
        "AppKit complained about an item's layout"


def test_each_kind_of_press_arrives_as_appkit_sends_it(arch):
    result = _run(arch, BAR, """
        view = rt.send(handle.items["scan"], "view")
        rt.send(view, "performClick:", None, argtypes=[_id])
        spin()
        out["clicks"] = len(clicks)
        toggle = handle.controls["strobe"]
        rt.send(toggle, "setSelected:forSegment:", True, 0,
                argtypes=[ctypes.c_bool, ctypes.c_long])
        send_action(toggle)
        spin()
        out["strobe"] = box.isChecked()
        # What AppKit says, not a flip: on again stays on.
        send_action(toggle)
        spin()
        out["strobe_again"] = box.isChecked()
        segments = handle.controls["show"]
        rt.send(segments, "setSelectedSegment:", 2, argtypes=[ctypes.c_long])
        send_action(segments)
        spin()
        out["show"] = combo.currentIndex()
        rt.send(renderer.handler, "scrubber:didSelectItemAtIndex:",
                handle.controls["category"], 5, argtypes=[_id, ctypes.c_long])
        spin()
        out["category"] = strip.currentIndex()
        out["label"] = rt.text(rt.send(handle.popovers["category"],
                                       "collapsedRepresentationLabel"))
        knob = handle.controls["volume"]
        rt.send(knob, "setDoubleValue:", 30.0, argtypes=[ctypes.c_double])
        send_action(knob)
        spin(300)
        out["volume"] = slider.value()
        button.setEnabled(False)
        spin()
        rt.send(view, "performClick:", None, argtypes=[_id])
        spin()
        out["disabled"] = len(clicks)
    """)
    assert result["clicks"] == 1
    assert result["strobe"] is True and result["strobe_again"] is True
    assert result["show"] == 2
    assert (result["category"], result["label"]) == (5, "Category 5")
    assert result["volume"] == 30
    assert result["disabled"] == 1, "a disabled button was pressed"


def test_a_held_popover_opens_onto_its_own_slider(arch):
    """The press-and-hold bar is AppKit's slider item, bound both ways."""
    result = _run(arch, BAR, """
        popover = handle.items["more"]
        held = rt.send(popover, "pressAndHoldTouchBar")
        out["has_bar"] = bool(held)
        out["idents"] = renderer.identifiers(held) if held else []
        out["principal"] = rt.text(rt.send(held, "principalItemIdentifier")) if held else ""
        item = handle.items["volume-held"]
        out["ends"] = [bool(rt.send(item, "minimumValueAccessory")),
                       bool(rt.send(item, "maximumValueAccessory"))]
        out["widths"] = [rt.send(item, "minimumSliderWidth", restype=ctypes.c_double),
                         rt.send(item, "maximumSliderWidth", restype=ctypes.c_double)]
        out["popover_idents"] = renderer.identifiers(handle.nested["more"])
        knob = handle.controls["volume-held"]
        out["knob_was"] = rt.send(knob, "doubleValue", restype=ctypes.c_double)
        rt.send(knob, "setDoubleValue:", 25.0, argtypes=[ctypes.c_double])
        send_action(knob)
        spin(300)
        out["value"] = slider.value()
        # And the way the item itself reports, as AppKit may: the item is
        # not a control, so its action is sent to the handler by hand.
        item = handle.items["volume-held"]
        rt.send(knob, "setDoubleValue:", 40.0, argtypes=[ctypes.c_double])
        rt.send(rt.send(item, "target"), "act:", item, argtypes=[_id])
        spin(300)
        out["value_from_item"] = slider.value()
        slider.setValue(55)
        spin(400)
        out["knob"] = rt.send(knob, "doubleValue", restype=ctypes.c_double)
    """)
    assert result["has_bar"], "no press-and-hold bar on the popover"
    assert result["idents"] == ["com.mailmanager.probe.volume-held"]
    # In the middle of the bar, between its two end icons, at a set width.
    assert result["principal"] == "com.mailmanager.probe.volume-held"
    assert result["ends"] == [True, True]
    import touchbar_mac

    assert result["widths"] == [touchbar_mac.HOLD_LEAST, touchbar_mac.HOLD_MOST]
    assert result["popover_idents"] == ["com.mailmanager.probe.volume"]
    assert result["knob_was"] == 70.0
    assert result["value"] == 25
    assert result["value_from_item"] == 40
    assert result["knob"] == 55.0
    assert not layout_complaints(result["stderr"])


def test_the_bar_follows_the_window(arch):
    result = _run(arch, BAR, """
        combo.setCurrentIndex(3)
        slider.setValue(55)
        box.setChecked(True)
        button.setText("Stop")
        spin(300)
        out["segment"] = rt.send(handle.controls["show"], "selectedSegment",
                                 restype=ctypes.c_long)
        out["value"] = rt.send(handle.controls["volume"], "doubleValue",
                               restype=ctypes.c_double)
        out["on"] = rt.send(handle.controls["strobe"],
                            "isSelectedForSegment:", 0,
                            restype=ctypes.c_bool, argtypes=[ctypes.c_long])
        box.hide()
        spin(300)
        out["top"] = renderer.identifiers(handle.touchbar)
        count = lambda: rt.send(handle.controls["category"], "numberOfItems",
                                restype=ctypes.c_long)
        out["before"] = count()
        strip.addItems(["Category 12", "Category 13", "Category 14"])
        spin(300)
        out["after"] = count()
    """)
    assert result["segment"] == 3
    assert result["value"] == 55.0
    assert result["on"] is True
    assert "com.mailmanager.probe.strobe" not in result["top"]
    assert (result["before"], result["after"]) == (12, 15)


def test_a_window_going_takes_its_bar_with_it(arch):
    result = _run(arch, BAR, """
        view = rt.send(handle.items["scan"], "view")
        window.deleteLater()
        spin()
        spin()
        out["released"] = handle.released
        out["targets"] = sum(1 for entry in renderer._targets.values()
                             if entry[0] is handle)
        rt.send(renderer.handler, "act:", view, argtypes=[_id])
        spin()
        out["clicks"] = len(clicks)
    """)
    assert result["released"] is True
    assert result["targets"] == 0
    assert result["clicks"] == 0


def test_the_apps_own_windows(arch):
    result = _run(arch, """
        from config import InMemoryCredentialStore, Settings
        from gui import MainWindow
        window = MainWindow(Settings(icloud_email="you@icloud.example"),
                            InMemoryCredentialStore(), demo=True)
        window.show()
        spin(600)
        bar = touchbar.of(window)
        handle = bar._handle
        out["attached"] = renderer.window_bar(window) == handle.touchbar
        out["top"] = renderer.identifiers(handle.touchbar)
        out["custom"] = rt.text(rt.send(handle.touchbar,
                                        "customizationIdentifier"))
        allowed = rt.send(handle.touchbar,
                          "customizationAllowedItemIdentifiers")
        out["allowed"] = [rt.text(rt.send(allowed, "objectAtIndex:", i,
                                          argtypes=[ctypes.c_ulong]))
                          for i in range(rt.send(allowed, "count",
                                                 restype=ctypes.c_ulong))]
        out["scan"] = rt.text(rt.send(handle.items["scan"], "title"))
        window.close()
    """)
    assert result["attached"]
    expected = [f"com.mailmanager.main.{key}" for key in (
        "scan", "apply", "undo")]
    assert result["top"][:3] == expected
    assert result["custom"] == "com.mailmanager.main"
    assert {"com.mailmanager.main.more", "com.mailmanager.main.options",
            "NSTouchBarItemIdentifierFlexibleSpace"} <= set(result["allowed"])
    assert result["scan"] == "Reload"
    assert not layout_complaints(result["stderr"])
