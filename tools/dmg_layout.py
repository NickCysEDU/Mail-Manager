#!/usr/bin/env python3
"""Keep the Mac that built the disk image out of its installer window.

Finder records the window's layout in the image's .DS_Store, and the
background picture as an alias and a bookmark. The picture is on a disk
image, so both also record the image itself: its path on the build machine,
the name of the disk it was on and that disk's UUID.

``clean`` edits Finder's file where it lies: the bookmark goes, the alias
loses the part that describes the disk image (needed only to mount an image
that is not mounted, and the file is only ever read from the mounted image),
and every byte the file no longer uses is zeroed. Finder ignores a layout
file written afresh by ds_store, so it is edited rather than rewritten.
``check`` fails when anything in a finished image outside the app still
names the Mac it was built on.

    python tools/dmg_layout.py clean "/Volumes/Mail Manager"
    python tools/dmg_layout.py check "/Volumes/Mail Manager"
"""

from __future__ import annotations

import getpass
import os
import plistlib
import re
import struct
import sys
from pathlib import Path

#: Finder's bookmark of the background picture, in two records: the first
#: 1,500 bytes, and the rest.
PICTURE_RECORDS = (b"pBBk", b"pBB0")

#: The part of an alias that is an alias of the disk image its target is on.
IMAGE_ALIAS = 20

UUID = re.compile(rb"[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}")

#: Runs of printable text, for names stored as one path part each.
TEXT = re.compile(rb"[\x20-\x7e]{3,}")


class _Raw:
    """ds_store with its decoders set aside: Finder's bookmark does not
    always decode (its length counts a second record), and values are kept
    as the bytes Finder wrote."""

    def __enter__(self):
        import ds_store.store as store_module

        self._module = store_module
        self._decoders = store_module.codecs
        store_module.codecs = {}
        return self

    def __exit__(self, *_exc):
        self._module.codecs = self._decoders


def _records(store: Path) -> list:
    """Every record in a .DS_Store, each value as the bytes Finder wrote."""
    from ds_store import DSStore

    with _Raw(), DSStore.open(str(store), "r") as opened:
        return [(entry.filename, entry.code, entry.type,
                 bytes(entry.value)
                 if isinstance(entry.value, (bytes, bytearray))
                 else entry.value)
                for entry in opened]


def without_image(alias: bytes) -> bytes:
    """Finder's alias less the alias of the disk image its target is on,
    every other byte as Finder wrote it."""
    version = struct.unpack(">h", alias[6:8])[0]
    at = 8 + (142 if version == 2 else 50)
    kept = bytearray(alias[:at])
    while at + 4 <= len(alias):
        tag, length = struct.unpack(">hh", alias[at:at + 4])
        size = 4 + length + (length & 1)
        if tag == -1 and length == 0:
            break
        if tag != IMAGE_ALIAS:
            kept += alias[at:at + size]
        at += size
    kept += alias[at:]
    kept[4:6] = struct.pack(">h", len(kept))
    return bytes(kept)


def clean(volume: Path) -> bool:
    """Take what names the build machine out of the layout, in place.
    Returns whether there was a layout to clean."""
    from ds_store import DSStore
    from ds_store.store import DSStoreEntry

    store = volume / ".DS_Store"
    if not store.is_file():
        return False
    with _Raw(), DSStore.open(str(store), "r+") as opened:
        for code in PICTURE_RECORDS:
            if list(opened.find(".", code)):
                opened.delete(".", code)
        for entry in list(opened.find(".", b"icvp")):
            options = plistlib.loads(bytes(entry.value))
            alias = options.get("backgroundImageAlias")
            if alias:
                options["backgroundImageAlias"] = without_image(bytes(alias))
                opened.insert(DSStoreEntry(
                    ".", b"icvp", entry.type,
                    plistlib.dumps(options, fmt=plistlib.FMT_BINARY)))
    _zero_the_unused(store)
    return True


def _zero_the_unused(store: Path) -> None:
    """Zero every byte the file does not use. What a record held before it
    was replaced or removed is otherwise still there, in a freed block or
    past the end of a node."""
    from ds_store import buddy
    from ds_store.store import DSStoreEntry

    data = bytearray(store.read_bytes())
    used = bytearray(len(data))

    def mark(offset: int, length: int) -> None:
        # The allocator counts from the fourth byte of the file.
        used[offset + 4:offset + 4 + length] = b"\1" * length

    used[:36] = b"\1" * 36
    with buddy.Allocator.open(str(store), "r") as allocator:
        mark(allocator._root._offset, allocator._root_block_size())
        directory = allocator.get_block(allocator["DSDB"])
        mark(directory._offset, 20)
        root = directory.read(">I")[0]

        def walk(number: int) -> None:
            block = allocator.get_block(number)
            following, count = block.read(">II")
            children = []
            for _ in range(count):
                if following:
                    children.append(block.read(">I")[0])
                DSStoreEntry.read(block)
            mark(block._offset, block.tell())
            if following:
                for child in children + [following]:
                    walk(child)

        with _Raw():
            walk(root)
    store.write_bytes(bytes(value if keep else 0
                            for value, keep in zip(data, used)))


def volume_of(path: Path) -> tuple:
    """The name and UUID of the disk ``path`` is on."""
    from mac_alias import osx

    mount = osx.statfs(str(path)).f_mntonname
    found = osx.getattrlist(mount, [0, osx.ATTR_VOL_NAME | osx.ATTR_VOL_UUID,
                                    0, 0, 0], 0)
    return str(found[0]), str(found[1]).upper()


def check(volume: Path, home: str, user: str, own_uuid: str,
          machine_names=()) -> list:
    """What, outside the app, names the Mac the image was built on: its
    home folder or user name, a disk image (the one Finder was laying out),
    one of its disks by name, or any disk's UUID but the image's own."""
    def forms(text: str) -> list:
        encoded = [text.encode("utf-8"), text.encode("utf-16-be")]
        return [form for form in encoded if form]

    home = home.rstrip("/")
    said = {
        "its home folder": [home, home.lstrip("/"), home.replace("/", ":")],
        "a disk image": [".dmg"],
        "one of its disks": [name for name in machine_names if len(name) >= 3],
    }
    found = []
    for folder, folders, files in os.walk(volume):
        folders[:] = [name for name in folders if not name.endswith(".app")]
        for name in files:
            path = Path(folder) / name
            if path.is_symlink():
                continue
            data = path.read_bytes()
            where = str(path.relative_to(volume))
            for what, texts in said.items():
                if any(form in data for text in texts for form in forms(text)):
                    found.append(f"{where}: {what}")
            if len(user) >= 3:
                parts = {run.decode() for run in TEXT.findall(data)}
                if (user in parts or any(form in data for form in forms(
                        f"/{user}/") + forms(f":{user}:"))):
                    found.append(f"{where}: its user name")
            if name == ".DS_Store":
                others = {match.decode().upper() for match in UUID.findall(data)}
                if others - {own_uuid.upper()}:
                    found.append(f"{where}: a disk's UUID")
    return found


def main(argv: list) -> int:
    if len(argv) != 3 or argv[1] not in ("clean", "check"):
        print("usage: dmg_layout.py clean|check VOLUME", file=sys.stderr)
        return 2
    volume = Path(argv[2])
    if argv[1] == "clean":
        clean(volume)
        return 0
    name, uuid = volume_of(volume)
    # The start-up disk, by the name an alias gives it.
    machine = {volume_of(Path("/"))[0]} - {name}
    found = check(volume, str(Path.home()), getpass.getuser(), uuid, machine)
    for line in found:
        print(f"names this Mac: {line}", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
