"""What a file says about itself, read without trusting it.

Every parser here walks a hostile byte string. They share three rules: never
seek past what was actually read, never allocate on a length the file states,
and never raise - a metadata panel that crashes the window is worse than one
that says "unknown".

Cover art matters for a second reason. An image pulled out of an audio tag is
attacker-controlled bytes handed to an image decoder, so the caller gets the
bytes and the size, and the same sniffing every other attachment gets.
"""

from __future__ import annotations

import struct
from typing import Dict, List, Optional, Tuple

#: No single metadata value is worth more than this on screen.
MAX_VALUE = 300

#: Cover art larger than this is ignored rather than decoded.
MAX_ART = 12 * 1024 * 1024


def _text(raw: bytes, limit: int = MAX_VALUE) -> str:
    try:
        out = raw.decode("utf-8", "replace")
    except Exception:      # noqa: BLE001
        return ""
    out = "".join(c for c in out if c.isprintable() or c in " \t")
    return out.strip()[:limit]


# -- images ---------------------------------------------------------------
def image_facts(data: bytes) -> Dict[str, str]:
    """Dimensions from the header, plus whether EXIF carries a location."""
    facts: Dict[str, str] = {}
    size = _image_size(data)
    if size:
        facts["Dimensions"] = f"{size[0]} x {size[1]} pixels"
    exif = _exif(data)
    facts.update(exif)
    return facts


def _image_size(data: bytes) -> Optional[Tuple[int, int]]:
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
            return struct.unpack(">II", data[16:24])
        if data[:3] == b"GIF" and len(data) >= 10:
            return struct.unpack("<HH", data[6:10])
        if data[:2] == b"BM" and len(data) >= 26:
            return abs(struct.unpack("<i", data[18:22])[0]), \
                   abs(struct.unpack("<i", data[22:26])[0])
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            if data[12:16] == b"VP8X" and len(data) >= 30:
                w = int.from_bytes(data[24:27], "little") + 1
                h = int.from_bytes(data[27:30], "little") + 1
                return w, h
        if data[:2] == b"\xff\xd8":
            i = 2
            while i + 9 < len(data) and i < 4_000_000:
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                              0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    height, width = struct.unpack(">HH", data[i + 5:i + 9])
                    return width, height
                if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                length = struct.unpack(">H", data[i + 2:i + 4])[0]
                i += 2 + max(length, 2)
    except Exception:      # noqa: BLE001
        return None
    return None


def _exif(data: bytes) -> Dict[str, str]:
    """Camera, date and - the one worth showing - whether GPS is present."""
    facts: Dict[str, str] = {}
    try:
        start = data.find(b"Exif\x00\x00", 0, 200_000)
        if start < 0:
            return facts
        tiff = start + 6
        if len(data) < tiff + 8:
            return facts
        endian = data[tiff:tiff + 2]
        order = "<" if endian == b"II" else ">" if endian == b"MM" else None
        if order is None:
            return facts
        offset = struct.unpack(order + "I", data[tiff + 4:tiff + 8])[0]
        if not 0 < offset < len(data) - tiff:
            return facts
        entries_at = tiff + offset
        count = struct.unpack(order + "H", data[entries_at:entries_at + 2])[0]
        count = min(count, 200)
        wanted = {0x010F: "Camera make", 0x0110: "Camera model",
                  0x0132: "Taken", 0x9003: "Taken", 0x0112: "Orientation"}
        for index in range(count):
            at = entries_at + 2 + index * 12
            if at + 12 > len(data):
                break
            tag, kind, length = struct.unpack(order + "HHI", data[at:at + 8])
            if tag == 0x8825:
                facts["Location"] = "this image carries GPS coordinates"
                continue
            if tag not in wanted or kind != 2 or length > MAX_VALUE:
                continue
            value_at = struct.unpack(order + "I", data[at + 8:at + 12])[0]
            if length <= 4:
                raw = data[at + 8:at + 8 + length]
            else:
                raw = data[tiff + value_at: tiff + value_at + length]
            value = _text(raw.rstrip(b"\x00"))
            if value:
                facts.setdefault(wanted[tag], value)
    except Exception:      # noqa: BLE001
        return facts
    return facts


# -- audio ----------------------------------------------------------------
def audio_facts(data: bytes) -> Tuple[Dict[str, str], Optional[bytes]]:
    """(tags, cover art bytes). Either may be empty."""
    if data[:3] == b"ID3":
        return _id3(data)
    if data[4:8] == b"ftyp":
        return _mp4(data)
    if data[:4] == b"fLaC":
        return _flac(data)
    return {}, None


def _id3(data: bytes) -> Tuple[Dict[str, str], Optional[bytes]]:
    facts: Dict[str, str] = {}
    art: Optional[bytes] = None
    try:
        if len(data) < 10:
            return facts, None
        version = data[3]
        size = 0
        for byte in data[6:10]:
            size = (size << 7) | (byte & 0x7F)      # syncsafe
        end = min(10 + size, len(data))
        at = 10
        names = {b"TIT2": "Title", b"TPE1": "Artist", b"TALB": "Album",
                 b"TDRC": "Year", b"TYER": "Year", b"TCON": "Genre",
                 b"TRCK": "Track"}
        while at + 10 <= end:
            frame = data[at:at + 4]
            if not frame.strip(b"\x00"):
                break
            if version >= 4:
                length = 0
                for byte in data[at + 4:at + 8]:
                    length = (length << 7) | (byte & 0x7F)
            else:
                length = struct.unpack(">I", data[at + 4:at + 8])[0]
            if length <= 0 or at + 10 + length > end:
                break
            body = data[at + 10:at + 10 + length]
            if frame in names:
                facts.setdefault(names[frame], _text(body[1:].rstrip(b"\x00")))
            elif frame == b"APIC" and art is None and length <= MAX_ART:
                art = _apic(body)
            at += 10 + length
    except Exception:      # noqa: BLE001
        pass
    return {k: v for k, v in facts.items() if v}, art


def _apic(body: bytes) -> Optional[bytes]:
    try:
        end = body.find(b"\x00", 1)
        if end < 0:
            return None
        rest = body[end + 2:]          # skip mime NUL and picture type
        nul = rest.find(b"\x00")
        return rest[nul + 1:] if nul >= 0 else None
    except Exception:      # noqa: BLE001
        return None


def _mp4(data: bytes) -> Tuple[Dict[str, str], Optional[bytes]]:
    """Walk the atom tree far enough to find ilst, bounded throughout."""
    facts: Dict[str, str] = {}
    art: Optional[bytes] = None
    names = {b"\xa9nam": "Title", b"\xa9ART": "Artist", b"\xa9alb": "Album",
             b"\xa9day": "Year", b"\xa9gen": "Genre", b"trkn": "Track"}

    def walk(start: int, stop: int, depth: int = 0) -> None:
        nonlocal art
        at = start
        guard = 0
        while at + 8 <= stop and guard < 500:
            guard += 1
            try:
                length = struct.unpack(">I", data[at:at + 4])[0]
            except struct.error:
                return
            kind = data[at + 4:at + 8]
            if length < 8 or at + length > stop:
                return
            if kind in (b"moov", b"udta", b"meta", b"ilst"):
                inner = at + 8 + (4 if kind == b"meta" else 0)
                if depth < 6:
                    walk(inner, at + length, depth + 1)
            elif kind in names or kind == b"covr":
                body = data[at + 8:at + length]
                value = body[16:] if len(body) > 16 else b""
                if kind == b"covr":
                    if art is None and len(value) <= MAX_ART:
                        art = value
                else:
                    text = _text(value)
                    if text:
                        facts.setdefault(names[kind], text)
            at += length

    try:
        walk(0, min(len(data), 40_000_000))
    except Exception:      # noqa: BLE001
        pass
    return facts, art


def _flac(data: bytes) -> Tuple[Dict[str, str], Optional[bytes]]:
    facts: Dict[str, str] = {}
    art: Optional[bytes] = None
    try:
        at = 4
        guard = 0
        while at + 4 <= len(data) and guard < 200:
            guard += 1
            header = data[at]
            last = header & 0x80
            kind = header & 0x7F
            length = int.from_bytes(data[at + 1:at + 4], "big")
            body = data[at + 4:at + 4 + length]
            if kind == 4:                              # VORBIS_COMMENT
                facts.update(_vorbis(body))
            elif kind == 6 and art is None:            # PICTURE
                art = _flac_picture(body)
            at += 4 + length
            if last:
                break
    except Exception:      # noqa: BLE001
        pass
    return facts, art


def _vorbis(body: bytes) -> Dict[str, str]:
    facts: Dict[str, str] = {}
    try:
        at = 0
        vendor = struct.unpack("<I", body[at:at + 4])[0]
        at += 4 + vendor
        count = struct.unpack("<I", body[at:at + 4])[0]
        at += 4
        wanted = {"TITLE": "Title", "ARTIST": "Artist", "ALBUM": "Album",
                  "DATE": "Year", "GENRE": "Genre"}
        for _ in range(min(count, 100)):
            length = struct.unpack("<I", body[at:at + 4])[0]
            at += 4
            pair = body[at:at + length].decode("utf-8", "replace")
            at += length
            key, _, value = pair.partition("=")
            if key.upper() in wanted:
                facts.setdefault(wanted[key.upper()], value[:MAX_VALUE])
    except Exception:      # noqa: BLE001
        pass
    return facts


def _flac_picture(body: bytes) -> Optional[bytes]:
    try:
        at = 4
        mime_len = struct.unpack(">I", body[at:at + 4])[0]
        at += 4 + mime_len
        desc_len = struct.unpack(">I", body[at:at + 4])[0]
        at += 4 + desc_len + 16
        data_len = struct.unpack(">I", body[at:at + 4])[0]
        at += 4
        return body[at:at + data_len] if data_len <= MAX_ART else None
    except Exception:      # noqa: BLE001
        return None


# -- pdf ------------------------------------------------------------------
def pdf_facts(data: bytes) -> Dict[str, str]:
    facts: Dict[str, str] = {}
    try:
        head = data[:12].decode("ascii", "replace")
        if head.startswith("%PDF-"):
            facts["PDF version"] = head[5:8].strip()
        window = data[:2_000_000]
        for key, label in (("/Title", "Title"), ("/Author", "Author"),
                           ("/Producer", "Producer"), ("/Creator", "Creator")):
            at = window.find(key.encode())
            if at < 0:
                continue
            rest = window[at + len(key):at + len(key) + MAX_VALUE + 2].lstrip()
            if rest[:1] == b"(":
                end = rest.find(b")")
                if end > 0:
                    value = _text(rest[1:end])
                    if value:
                        facts[label] = value
        if b"/Encrypt" in window:
            facts["Encrypted"] = "yes"
    except Exception:      # noqa: BLE001
        pass
    return facts


def facts_for(item) -> List[Tuple[str, str]]:
    """Everything worth showing about one attachment, in display order."""
    rows: List[Tuple[str, str]] = [
        ("Name", item.shown),
        ("Type", item.content_type or "unknown"),
        ("Size", item.human_size()),
    ]
    if item.encoding:
        rows.append(("Sent as", item.encoding))
    if item.part:
        rows.append(("MIME part", item.part))
    if item.inline:
        rows.append(("Placement", "inline in the message"))
    data = item.data or b""
    if not data:
        rows.append(("Contents", "not downloaded yet"))
        return rows
    import attachments

    kind, sniffed = attachments.sniff(data[:32], item.content_type, item.name)
    if sniffed and sniffed != item.content_type:
        rows.append(("Actually", f"{sniffed} (the declared type differs)"))
    if kind == "image":
        rows += sorted(image_facts(data).items())
    elif kind == "audio":
        tags, _art = audio_facts(data)
        rows += sorted(tags.items())
    elif kind == "pdf":
        rows += sorted(pdf_facts(data).items())
    import hashlib
    rows.append(("SHA-256", hashlib.sha256(data).hexdigest()))
    return rows
