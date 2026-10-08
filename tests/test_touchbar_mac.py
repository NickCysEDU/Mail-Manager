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

    def spin_until(check, ms=5000):
        # Until check() holds, or ms have gone: for a change that waits on
        # a timer of its own, which a loaded machine delays.
        import time

        ends = time.monotonic() + ms / 1000.0
        while not check() and time.monotonic() < ends:
            spin(50)

    def send_action(control):
        rt.send(control, "sendAction:to:", rt.send(control, "action"),
                rt.send(control, "target"), restype=ctypes.c_bool,
                argtypes=[_id, _id])

    out = {{}}
""")


def layout_complaints(stderr: str) -> list:
    """AppKit's complaints about a layout."""
    return [line for line in stderr.splitlines()
            if "NSLayoutConstraint" in line]


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
                            ends=("speaker.fill", "speaker.wave.3.fill"))]),
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


def test_a_popover_opens_on_a_tap_and_nothing_else(arch):
    """A finger held on a popover's button and dragged opened a slider of
    its own, which drove something other than what the button was named
    for. A popover now opens on a tap alone."""
    result = _run(arch, BAR, """
        popover = handle.items["more"]
        out["held"] = bool(rt.send(popover, "pressAndHoldTouchBar"))
    """)
    assert result["held"] is False


def test_a_knob_left_between_two_values_stays_where_it_was_left(arch):
    """AppKit reports where the finger left the knob, between two whole
    values; set to the one it rounds to, the knob hopped under the finger."""
    result = _run(arch, BAR, """
        knob = handle.controls["volume"]
        rt.send(knob, "setDoubleValue:", 42.7, argtypes=[ctypes.c_double])
        send_action(knob)
        spin_until(lambda: slider.value() == 43)
        spin(int(touchbar.HELD_FOR * 1000) + 300)
        out["value"] = slider.value()
        # Something else about it changes, and it is all sent again.
        slider.setEnabled(False)
        spin_until(lambda: not rt.send(knob, "isEnabled",
                                       restype=ctypes.c_bool))
        slider.setEnabled(True)
        spin_until(lambda: rt.send(knob, "isEnabled", restype=ctypes.c_bool))
        out["knob"] = rt.send(knob, "doubleValue", restype=ctypes.c_double)
        slider.setValue(60)
        spin_until(lambda: rt.send(knob, "doubleValue",
                                   restype=ctypes.c_double) == 60.0)
        out["followed"] = rt.send(knob, "doubleValue", restype=ctypes.c_double)
    """)
    assert result["value"] == 43
    assert result["knob"] == 42.7
    assert result["followed"] == 60.0


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


LOOK = """
    def views(root):
        found = [root]
        subs = rt.send(root, "subviews")
        count = rt.send(subs, "count", restype=ctypes.c_ulong) if subs else 0
        for index in range(count):
            found += views(rt.send(subs, "objectAtIndex:", index,
                                   argtypes=[ctypes.c_ulong]))
        return found

    def name_of(view):
        return rt.objc.object_getClassName(view).decode()

    def look(root):
        knobs = []
        for view in views(root):
            if name_of(view).endswith("NSSliderKnob"):
                layer = rt.send(view, "layer")
                knobs.append([rt.send(layer, "cornerRadius",
                                      restype=ctypes.c_double),
                              rt.text(rt.send(layer, "cornerCurve"))])
        return {"knobs": knobs}
"""


def test_a_slider_on_the_bar_has_a_rounded_knob(arch):
    """AppKit draws a slider's knob on the bar as a sharp-cornered square;
    the system's own sliders have a rounded knob. AppKit may draw its views
    afresh, so the look must hold after an update."""
    result = _run(arch, BAR, LOOK, """
        plain = handle.controls["volume"]
        out["plain"] = look(plain)
        # As if AppKit had drawn it afresh: a square knob.
        for view in views(plain):
            if name_of(view).endswith("NSSliderKnob"):
                rt.send(rt.send(view, "layer"), "setCornerRadius:", 0.0,
                        argtypes=[ctypes.c_double])
        slider.setValue(15)
        spin(400)
        out["plain_after"] = look(plain)
    """)
    import touchbar_mac

    for key in ("plain", "plain_after"):
        knobs = result[key]["knobs"]
        assert knobs, f"no knob in the {key} slider"
        assert all(radius == touchbar_mac.KNOB_ROUNDING and curve == "continuous"
                   for radius, curve in knobs), (key, knobs)
    assert not layout_complaints(result["stderr"])


def test_a_slider_draws_with_a_rounded_knob_on_black(arch):
    """The pixels themselves, in the bar's own appearance: the knob's
    corners are not white and its middle is."""
    result = _run(arch, BAR, LOOK, """
        import touchbar_mac
        from touchbar_mac import NSRect
        look_of_bar = rt.send(rt.cls("NSAppearance"), "_functionRowAppearance")
        if not look_of_bar:
            print(json.dumps({"skip": "no Touch Bar appearance here"}))
            sys.exit(0)
        item = rt.send(handle.items["volume"], "view")
        host = rt.send(rt.send(rt.cls("NSView"), "alloc"), "initWithFrame:",
                       NSRect(0, 0, 420, 30), argtypes=[NSRect])
        rt.send(host, "setWantsLayer:", True, argtypes=[ctypes.c_bool])
        rt.send(rt.send(host, "layer"), "setBackgroundColor:",
                rt.send(rt.send(rt.cls("NSColor"), "blackColor"), "CGColor"),
                argtypes=[_id])
        # Off every screen, so Core Animation draws it unseen.
        window = rt.send(rt.send(rt.cls("NSWindow"), "alloc"),
                         "initWithContentRect:styleMask:backing:defer:",
                         NSRect(-6000, -6000, 420, 30), 0, 2, False,
                         argtypes=[NSRect, ctypes.c_ulong, ctypes.c_ulong,
                                   ctypes.c_bool])
        rt.send(window, "setContentView:", host, argtypes=[_id])
        rt.send(window, "setAppearance:", look_of_bar, argtypes=[_id])
        rt.send(host, "addSubview:", item, argtypes=[_id])
        rt.send(item, "setFrame:", NSRect(10, 0, 400, 30), argtypes=[NSRect])
        rt.send(window, "orderFront:", None, argtypes=[_id])
        slider.setValue(40)
        spin(600)
        rt.send(host, "layoutSubtreeIfNeeded")
        # As tall as it needs, and in the middle of the bar, as AppKit puts it.
        size = rt.send(item, "fittingSize", restype=touchbar_mac.NSSize)
        rt.send(item, "setFrame:", NSRect(10, (30 - size.height) / 2,
                                          size.width, size.height),
                argtypes=[NSRect])
        rt.send(host, "layoutSubtreeIfNeeded")
        spin(300)
        scale = 2
        cg = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        cg.CGColorSpaceCreateDeviceRGB.restype = ctypes.c_void_p
        cg.CGBitmapContextCreate.restype = ctypes.c_void_p
        cg.CGBitmapContextCreate.argtypes = [
            ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t,
            ctypes.c_size_t, ctypes.c_void_p, ctypes.c_uint32]
        cg.CGContextScaleCTM.argtypes = [ctypes.c_void_p, ctypes.c_double,
                                         ctypes.c_double]
        cg.CGBitmapContextGetData.restype = ctypes.c_void_p
        cg.CGBitmapContextGetData.argtypes = [ctypes.c_void_p]
        cg.CGBitmapContextGetBytesPerRow.restype = ctypes.c_size_t
        cg.CGBitmapContextGetBytesPerRow.argtypes = [ctypes.c_void_p]
        width, height = 420 * scale, 30 * scale
        context = cg.CGBitmapContextCreate(None, width, height, 8, 0,
                                           cg.CGColorSpaceCreateDeviceRGB(),
                                           2 | (2 << 12))
        cg.CGContextScaleCTM(context, scale, scale)
        rt.send(rt.send(host, "layer"), "renderInContext:", context,
                argtypes=[_id])
        row = cg.CGBitmapContextGetBytesPerRow(context)
        data = ctypes.string_at(cg.CGBitmapContextGetData(context), row * height)

        def brightness(x, y):
            # x, y in points from the window's bottom left.
            px, py = int(x * scale), height - 1 - int(y * scale)
            blue, green, red, _ = data[py * row + px * 4: py * row + px * 4 + 4]
            return max(red, green, blue)

        import platform

        # A rectangle comes back through objc_msgSend_stret on Intel.
        returns_rect = ctypes.cast(
            rt.objc.objc_msgSend_stret if platform.machine() == "x86_64"
            else rt.objc.objc_msgSend, _id).value

        def rect(receiver, selector, *args, argtypes=()):
            return ctypes.CFUNCTYPE(NSRect, _id, _id, *argtypes)(returns_rect)(
                receiver, rt.sel(selector), *args)

        def frame(view):
            got = rect(view, "convertRect:toView:", rect(view, "bounds"), None,
                       argtypes=[NSRect, _id])
            return [got.x, got.y, got.width, got.height]

        knobs = [frame(view) for view in views(item)
                 if name_of(view).endswith("NSSliderKnob")
                 and not rt.send(view, "isHidden", restype=ctypes.c_bool)
                 and frame(view)[2] > 4]
        x, y, w, h = max(knobs, key=lambda box: box[2] * box[3])
        out["knob"] = [x, y, w, h]
        out["middle"] = brightness(x + w / 2, y + h / 2)
        inset = 0.75
        out["corners"] = [brightness(x + inset, y + inset),
                          brightness(x + w - inset, y + inset),
                          brightness(x + inset, y + h - inset),
                          brightness(x + w - inset, y + h - inset)]
        rt.send(window, "orderOut:", None, argtypes=[_id])
    """)
    assert result["middle"] > 230, result
    assert max(result["corners"]) < 140, result


#: The app's part of the bar beside the Control Strip as it comes, folded
#: to its four keys, and what a popover has once its close button is in.
APP_ROOM = 685.0
POPOVER_ROOM = 613.0
#: Between two items.
GAP = 8.0


def test_the_visualisers_bar_fits_beside_the_control_strip(arch):
    """What does not fit, AppKit leaves off, lowest priority first. Every
    normal and high item of the visualiser's bars, in every scene, windowed
    and full screen, and of each popover, fits in the room it has: nothing
    comes and goes for want of room but what is marked to give way."""
    result = _run(arch, """
        import math, struct
        import attachments, visualizers
        from attachment_view import AttachmentViewer
        from touchbar_mac import NSRect, NSSize
        look_of_bar = rt.send(rt.cls("NSAppearance"), "_functionRowAppearance")
        if not look_of_bar:
            print(json.dumps({"skip": "no Touch Bar appearance here"}))
            sys.exit(0)
        rate = 22050
        pcm = b"".join(struct.pack("<h", int(9000 * math.sin(
            2 * math.pi * 220.0 * i / rate))) for i in range(rate))
        data = (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt "
                + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
                + b"data" + struct.pack("<I", len(pcm)) + pcm)
        viewer = AttachmentViewer([attachments.Attachment(
            part="1", name="tone.wav", content_type="audio/wav",
            size=len(data), data=data)])
        viewer.show()
        viewer.list.setCurrentRow(0)
        spin_until(lambda: viewer.stack.currentWidget() is viewer.audio)
        # Measured in the bar's own look, whose type and keys are larger
        # than a window's: off every screen, so nothing is seen.
        window = rt.send(rt.send(rt.cls("NSWindow"), "alloc"),
                         "initWithContentRect:styleMask:backing:defer:",
                         NSRect(-9000, -9000, 1100, 30), 0, 2, False,
                         argtypes=[NSRect, ctypes.c_ulong, ctypes.c_ulong,
                                   ctypes.c_bool])
        rt.send(window, "setAppearance:", look_of_bar, argtypes=[_id])
        host = rt.send(window, "contentView")

        def width(made):
            view = rt.send(made, "view")
            if not rt.send(view, "window"):
                rt.send(host, "addSubview:", view, argtypes=[_id])
            rt.send(host, "layoutSubtreeIfNeeded")
            size = rt.send(view, "fittingSize", restype=NSSize)
            if size.width < 1:
                size = rt.send(view, "frame", restype=NSRect)
            return size.width

        def measure(bar, state):
            spin(200)
            handle = bar._handle
            for key, keys in bar.arrangement().items():
                kept = [k for k in keys if bar.flat[k].kind != "space"
                        and bar.flat[k].priority in ("normal", "high")]
                widths = {k: width(handle.items[k]) for k in kept}
                room = sum(widths.values()) + GAP * max(0, len(kept) - 1)
                out.setdefault("rows", []).append(
                    [state, key or "top", room, widths])

        GAP = 8.0
        audio = viewer.audio
        audio.enable_box.setChecked(False)
        measure(touchbar.of(viewer), "picture off")
        audio.enable_box.setChecked(True)
        for scene in visualizers.SCENES:
            audio.scene_box.setCurrentText(scene.name)
            measure(touchbar.of(viewer), scene.name)
        audio._go_full_screen()
        spin(300)
        for scene in visualizers.SCENES:
            audio.scene_box.setCurrentText(scene.name)
            measure(touchbar.of(audio._full), "full screen, " + scene.name)
        audio._full.close()
    """)
    over = [row for row in result["rows"]
            if row[2] > (APP_ROOM if row[1] == "top" else POPOVER_ROOM)]
    assert over == [], over
    assert len(result["rows"]) > 20
    assert not layout_complaints(result["stderr"])
