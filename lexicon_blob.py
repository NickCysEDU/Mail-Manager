"""The lexicon as a file the app can read without reading it.

The world-knowledge tables ship as JSON, which has to be decompressed,
parsed, and built into dictionaries before the first lookup - seventy
milliseconds and ten megabytes, every launch, for tables that are read-only
and never change between releases. That is fine at today's size. It is
linear in it, and the tables only ever grow: ten times the brands would be
seven hundred milliseconds and a hundred megabytes, which is not fine.

So there is a second form. Each table is written once, at build time, as a
sorted array of ``key\\0value`` records with an index of offsets in front of
it. At run time the file is memory-mapped and looked up by binary search, so
a lookup touches one page of a file the operating system was going to cache
anyway. Nothing is parsed, nothing is allocated per entry, and the cost of
opening it does not depend on how much is in it.

The format, little-endian throughout::

    magic     6 bytes   b"MMLX\\x01\\x00"
    sections  uint32    how many tables follow
    for each section:
        name       16 bytes  NUL-padded ASCII
        count      uint32    records in the table
        index_at   uint32    absolute offset of the offset array
        data_at    uint32    absolute offset of the records
        data_len   uint32    bytes of records
    index arrays:  count x uint32, each an offset within the section's data
    records:       key + b"\\0" + value, in ascending key order

Keys are compared as bytes, and written in that order, so the search never
has to decode anything it is not returning.

This module is deliberately able to fail. :func:`open_blob` returns None for
a file that is missing, truncated, or from a future version, and the caller
falls back to the JSON. A world-knowledge table is an enhancement; nothing
here is ever worth taking a scan down for.
"""

from __future__ import annotations

import mmap
import struct
from pathlib import Path
from typing import Dict, Iterator, List, Mapping, Optional, Tuple

MAGIC = b"MMLX\x01\x00"
_HEADER = struct.Struct("<6sI")
_SECTION = struct.Struct("<16sIIII")
_NAME_WIDTH = 16


def build(sections: Mapping[str, Mapping[str, str]], path: Path) -> Path:
    """Write the tables out. Keys are encoded UTF-8 and sorted as bytes."""
    names = sorted(sections)
    if any(len(name.encode("ascii")) > _NAME_WIDTH for name in names):
        raise ValueError("section names must fit in 16 bytes")

    encoded: Dict[str, List[Tuple[bytes, bytes]]] = {}
    for name in names:
        rows = [(str(key).encode("utf-8"), str(value).encode("utf-8"))
                for key, value in sections[name].items()]
        rows.sort(key=lambda pair: pair[0])
        if any(b"\0" in key for key, _ in rows):
            raise ValueError("keys may not contain NUL")
        encoded[name] = rows

    head = _HEADER.size + _SECTION.size * len(names)
    # Two passes: sizes first, so every offset can be absolute and the reader
    # never has to add anything up.
    offset = head
    layout = []
    for name in names:
        rows = encoded[name]
        index_at = offset
        offset += 4 * len(rows)
        data_at = offset
        data_len = sum(len(key) + 1 + len(value) for key, value in rows)
        offset += data_len
        layout.append((name, len(rows), index_at, data_at, data_len))

    out = bytearray()
    out += _HEADER.pack(MAGIC, len(names))
    for name, count, index_at, data_at, data_len in layout:
        out += _SECTION.pack(name.encode("ascii").ljust(_NAME_WIDTH, b"\0"),
                             count, index_at, data_at, data_len)
    for name, *_rest in layout:
        rows = encoded[name]
        within = 0
        index = bytearray()
        for key, value in rows:
            index += struct.pack("<I", within)
            within += len(key) + 1 + len(value)
        out += index
        for key, value in rows:
            out += key + b"\0" + value

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))
    return path


class Table:
    """One sorted table inside the blob."""

    __slots__ = ("_view", "_count", "_index_at", "_data_at", "_data_len")

    def __init__(self, view, count: int, index_at: int, data_at: int,
                 data_len: int) -> None:
        self._view = view
        self._count = count
        self._index_at = index_at
        self._data_at = data_at
        self._data_len = data_len

    def __len__(self) -> int:
        return self._count

    def _record(self, position: int) -> Tuple[bytes, bytes]:
        start = struct.unpack_from("<I", self._view, self._index_at + 4 * position)[0]
        if position + 1 < self._count:
            end = struct.unpack_from(
                "<I", self._view, self._index_at + 4 * (position + 1))[0]
        else:
            end = self._data_len
        blob = self._view[self._data_at + start:self._data_at + end]
        key, _, value = bytes(blob).partition(b"\0")
        return key, value

    def get(self, key: str, default=None):
        """Binary search. Returns the value as ``str``, or ``default``."""
        wanted = (key or "").encode("utf-8")
        low, high = 0, self._count - 1
        while low <= high:
            middle = (low + high) // 2
            found, value = self._record(middle)
            if found == wanted:
                return value.decode("utf-8")
            if found < wanted:
                low = middle + 1
            else:
                high = middle - 1
        return default

    def __contains__(self, key: str) -> bool:
        return self.get(key, None) is not None

    def __iter__(self) -> Iterator[str]:
        for position in range(self._count):
            yield self._record(position)[0].decode("utf-8")

    def items(self) -> Iterator[Tuple[str, str]]:
        for position in range(self._count):
            key, value = self._record(position)
            yield key.decode("utf-8"), value.decode("utf-8")


class Blob:
    """A memory-mapped lexicon. Read-only, and closed when it is dropped."""

    def __init__(self, path: Path) -> None:
        self._handle = open(path, "rb")
        try:
            self._map = mmap.mmap(self._handle.fileno(), 0,
                                  access=mmap.ACCESS_READ)
        except (ValueError, OSError):
            self._handle.close()
            raise
        self._view = memoryview(self._map)
        self.tables: Dict[str, Table] = {}
        self._read_header()

    def _read_header(self) -> None:
        magic, count = _HEADER.unpack_from(self._view, 0)
        if magic != MAGIC:
            raise ValueError("not a lexicon blob, or a version this cannot read")
        size = len(self._map)
        for index in range(count):
            raw = _SECTION.unpack_from(self._view, _HEADER.size + _SECTION.size * index)
            name, rows, index_at, data_at, data_len = raw
            if (index_at + 4 * rows > size or data_at + data_len > size):
                raise ValueError("the lexicon blob is truncated")
            self.tables[name.rstrip(b"\0").decode("ascii")] = Table(
                self._view, rows, index_at, data_at, data_len)

    def table(self, name: str) -> Optional[Table]:
        return self.tables.get(name)

    def close(self) -> None:
        try:
            self._view.release()
        except (BufferError, ValueError):  # pragma: no cover - already gone
            return
        self._map.close()
        self._handle.close()

    def __enter__(self) -> "Blob":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def open_blob(path: Path) -> Optional[Blob]:
    """Open one, or return None. Never raises.

    Every reason this can fail - no file, a truncated file, a file written by
    a newer build - has the same answer: use the JSON instead.
    """
    try:
        return Blob(path)
    except (OSError, ValueError, struct.error):
        return None
