"""The installer window names nothing of the Mac that built it.

Finder records the window's background picture as an alias and a bookmark,
and a picture on a disk image comes with an alias of the image: its path on
the build machine, the disk it was on and that disk's UUID.

Each test builds a small image of its own, gives it a layout as Finder
writes one (Finder cannot be driven from a test), cleans it as the build
does and reads every byte.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import plistlib
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("ds_store")
pytest.importorskip("mac_alias")

if sys.platform != "darwin" or shutil.which("hdiutil") is None:
    pytest.skip("disk images are made on a Mac", allow_module_level=True)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import dmg_layout  # noqa: E402

#: Who built it, in these tests, and on what.
BUILDER = "builder"


def _attach(path: Path, *flags: str) -> Path:
    attached = subprocess.run(
        ["hdiutil", "attach", "-noverify", "-noautoopen", "-nobrowse", *flags,
         str(path)], check=True, capture_output=True, text=True)
    return Path(attached.stdout.strip().splitlines()[-1].split("\t")[-1])


@pytest.fixture
def image(tmp_path):
    """A writable image like the build's, mounted where Finder does not
    look, made in the builder's home folder on a disk of the builder's.

    The builder's disk is an image too: an alias can only be made here of
    a file on a disk whose file numbers fit in 32 bits, as HFS+ ones do and
    the Mac's own disk's do not.
    """
    disk = tmp_path / "disk.dmg"
    subprocess.run(["hdiutil", "create", "-size", "20m", "-fs", "HFS+",
                    "-volname", f"Builder HD {os.getpid()}", "-ov", str(disk)],
                   check=True, capture_output=True)
    host = _attach(disk, "-readwrite")
    try:
        work = host / "Users" / BUILDER / "work"
        source = work / "staging"
        (source / ".background").mkdir(parents=True)
        shutil.copy(ROOT / "assets" / "dmg-background.png",
                    source / ".background" / "background.png")
        (source / "Mail Manager.app" / "Contents").mkdir(parents=True)
        (source / "Mail Manager.app" / "Contents" / "Info.plist").write_text(
            "<plist/>")
        (source / "Read me first.txt").write_text(
            "Drag the app onto Applications.\n")
        made = work / "Mail Manager.dmg.tmp.dmg"
        subprocess.run(["hdiutil", "create", "-volname", f"Layout {os.getpid()}",
                        "-srcfolder", str(source), "-ov", "-format", "UDRW",
                        "-fs", "HFS+", str(made)], check=True,
                       capture_output=True)
        mount = _attach(made, "-readwrite")
        try:
            yield mount, made, f"/Users/{BUILDER}"
        finally:
            subprocess.run(["hdiutil", "detach", str(mount), "-force"],
                           capture_output=True)
    finally:
        subprocess.run(["hdiutil", "detach", str(host), "-force"],
                       capture_output=True)


def _finders_layout(volume: Path, image: Path) -> dict:
    """What Finder writes, near enough: the picture's alias carrying an
    alias of the image it is on, its bookmark in two records, the window
    and the icons' places. Returns the records written."""
    from ds_store import DSStore
    from ds_store.store import DSStoreEntry
    from mac_alias import Alias, Bookmark

    alias = Alias.for_file(str(volume / ".background" / "background.png"))
    alias.volume.disk_image_alias = Alias.for_file(str(image))
    bookmark = Bookmark.for_file(str(image)).to_bytes()
    window = {"ShowStatusBar": False, "ShowToolbar": False,
              "WindowBounds": "{{200, 340}, {660, 420}}"}
    options = {"arrangeBy": "none", "backgroundType": 2, "iconSize": 128.0,
               "textSize": 13.0, "backgroundImageAlias": alias.to_bytes()}

    def place(x, y):
        return struct.pack(">IIII", x, y, 0xFFFFFFFF, 0xFFFF0000)

    records = {
        (".", b"bwsp"): plistlib.dumps(window, fmt=plistlib.FMT_BINARY),
        (".", b"icvp"): plistlib.dumps(options, fmt=plistlib.FMT_BINARY),
        (".", b"pBBk"): bookmark[:-48],
        (".", b"pBB0"): bookmark[-48:],
        ("Applications", b"Iloc"): place(490, 214),
        ("Mail Manager.app", b"Iloc"): place(170, 214),
        ("Read me first.txt", b"Iloc"): place(330, 350),
    }
    entries = [DSStoreEntry(name, code, b"blob", value)
               for (name, code), value in records.items()]
    entries.append(DSStoreEntry(".", b"vSrn", b"long", 1))
    store = volume / ".DS_Store"
    with DSStore.open(str(store), "w+", initial_entries=entries):
        pass
    _leave_finders_leftovers(store, bookmark, options["backgroundImageAlias"])
    return records


def _leave_finders_leftovers(store: Path, *older: bytes) -> None:
    """Finder writes the file again and again as it lays the window out, and
    what it wrote before stays where it was: in blocks it has freed, and
    past the end of a node. Copies of what the cleaning removes are left in
    both."""
    from ds_store import buddy

    size = store.stat().st_size
    with buddy.Allocator.open(str(store), "r+") as allocator:
        free = sorted((1 << width, offset)
                      for width, offsets in enumerate(allocator._free)
                      for offset in offsets if offset + (1 << width) + 4 <= size)
        for copy in older:
            room, offset = next(block for block in free if block[0] >= len(copy))
            free.remove((room, offset))
            allocator.write(offset, copy)
        directory = allocator.get_block(allocator["DSDB"])
        node = allocator.get_block(directory.read(">I")[0])
        allocator.write(node._offset + len(node) - 256, older[0][:256])


def _check(mount: Path, made: Path, home: str) -> list:
    own = dmg_layout.volume_of(mount)
    return dmg_layout.check(mount, home, BUILDER, own[1],
                            {dmg_layout.volume_of(made)[0]} - {own[0]})


def _resolve(alias: bytes) -> str:
    """Where CoreFoundation finds an alias's target, as Finder would."""
    cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
    cf.CFDataCreate.restype = ctypes.c_void_p
    cf.CFDataCreate.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long]
    cf.CFURLCreateBookmarkDataFromAliasRecord.restype = ctypes.c_void_p
    cf.CFURLCreateBookmarkDataFromAliasRecord.argtypes = [ctypes.c_void_p,
                                                          ctypes.c_void_p]
    cf.CFURLCreateByResolvingBookmarkData.restype = ctypes.c_void_p
    cf.CFURLCreateByResolvingBookmarkData.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_bool),
        ctypes.POINTER(ctypes.c_void_p)]
    cf.CFURLCopyFileSystemPath.restype = ctypes.c_void_p
    cf.CFURLCopyFileSystemPath.argtypes = [ctypes.c_void_p, ctypes.c_long]
    cf.CFStringGetCString.restype = ctypes.c_bool
    cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                      ctypes.c_long, ctypes.c_uint32]
    data = cf.CFDataCreate(None, alias, len(alias))
    bookmark = cf.CFURLCreateBookmarkDataFromAliasRecord(None, data)
    assert bookmark, "not an alias CoreFoundation can read"
    stale = ctypes.c_bool(False)
    error = ctypes.c_void_p()
    # Without asking anyone and without mounting anything.
    url = cf.CFURLCreateByResolvingBookmarkData(
        None, bookmark, (1 << 8) | (1 << 9), None, None, ctypes.byref(stale),
        ctypes.byref(error))
    assert url, "the alias leads nowhere"
    path = ctypes.create_string_buffer(4096)
    cf.CFStringGetCString(cf.CFURLCopyFileSystemPath(url, 0), path, 4096,
                          0x08000100)
    return path.value.decode()


class TestTheLayoutIsCleaned:
    def test_finders_layout_names_the_machine(self, image):
        """The check is shown failing first: it finds each thing it is for
        in a layout as Finder leaves it."""
        mount, made, home = image
        _finders_layout(mount, made)
        found = " | ".join(_check(mount, made, home))
        for what in ("its home folder", "its user name", "a disk image",
                     "one of its disks", "a disk's UUID"):
            assert what in found, (what, found)

    def test_cleaned_it_names_nothing(self, image):
        mount, made, home = image
        _finders_layout(mount, made)
        assert dmg_layout.clean(mount)
        assert _check(mount, made, home) == []

    def test_what_finder_wrote_is_kept_but_the_picture_records(self, image):
        mount, made, _home = image
        written = _finders_layout(mount, made)
        dmg_layout.clean(mount)
        left = {(name, code): value for name, code, _kind, value
                in dmg_layout._records(mount / ".DS_Store")}
        assert (".", b"pBBk") not in left and (".", b"pBB0") not in left
        for key in [(".", b"bwsp"), ("Applications", b"Iloc"),
                    ("Mail Manager.app", b"Iloc"), ("Read me first.txt", b"Iloc")]:
            assert left[key] == written[key], key
        assert left[(".", b"vSrn")] == 1
        before = plistlib.loads(written[(".", b"icvp")])
        after = plistlib.loads(left[(".", b"icvp")])
        assert after.pop("backgroundImageAlias") == dmg_layout.without_image(
            before.pop("backgroundImageAlias"))
        assert after == before

    def test_nothing_replaced_is_left_in_the_free_space(self, image):
        mount, made, _home = image
        written = _finders_layout(mount, made)
        nested = plistlib.loads(written[(".", b"icvp")])["backgroundImageAlias"]
        dmg_layout.clean(mount)
        data = (mount / ".DS_Store").read_bytes()
        for gone in (nested[-120:-80], written[(".", b"pBBk")][200:240],
                     written[(".", b"pBB0")][:24]):
            assert gone not in data

    def test_the_picture_is_still_found(self, image):
        mount, made, _home = image
        _finders_layout(mount, made)
        dmg_layout.clean(mount)
        left = {(name, code): value for name, code, _kind, value
                in dmg_layout._records(mount / ".DS_Store")}
        alias = plistlib.loads(left[(".", b"icvp")])["backgroundImageAlias"]
        found = Path(_resolve(alias)).resolve()
        assert found == (mount / ".background" / "background.png").resolve()

    def test_a_layout_finder_never_wrote_is_left_alone(self, image):
        """Finder could not be scripted, so the window is plain: there is
        nothing to clean and nothing to find."""
        mount, made, home = image
        assert not dmg_layout.clean(mount)
        assert _check(mount, made, home) == []


class TestTheAliasLosesOnlyTheImage:
    def test_only_the_alias_of_the_image_goes(self, image):
        from mac_alias import Alias

        mount, made, _home = image
        alias = Alias.for_file(str(mount / ".background" / "background.png"))
        plain = alias.to_bytes()
        alias.volume.disk_image_alias = Alias.for_file(str(made))
        nested = alias.to_bytes()
        assert len(nested) > len(plain)
        assert dmg_layout.without_image(nested) == plain
        assert dmg_layout.without_image(plain) == plain


class TestTheBuildUsesIt:
    def test_the_layout_is_cleaned_before_compressing_and_checked_after(self):
        script = (ROOT / "build_dmg.sh").read_text(encoding="utf-8")
        laid_out = script.index('tell disk "$VOLUME"')
        cleaned = script.index("tools/dmg_layout.py clean")
        compressed = script.index("hdiutil convert")
        checked = script.index("tools/dmg_layout.py check")
        assert laid_out < cleaned < compressed < checked
        failing = script[checked:script.index("fi", checked)]
        assert 'rm -f "$DMG"' in failing and "die" in failing
