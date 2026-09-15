"""What is attached to a message, and what is safe to do with it.

Attachments are the one part of an email that is a file rather than text,
which makes them the one part that can still hurt you in 2026. Everything
here assumes the sender is hostile.

Three separate problems, kept separate:

*Naming.* A filename in a message is not a filename, it is a suggestion from
a stranger. It can traverse directories, contain a null byte, be four
thousand characters long, or use a right-to-left override so that
``photo_gnp.exe`` displays as ``photo_exe.png``. :func:`safe_name` produces
something safe to write to disk; :func:`display_name` produces something safe
to show, which is not the same job.

*Typing.* The declared content type is also a suggestion. A part claiming to
be a PNG may be anything, so :func:`sniff` reads the leading bytes and the
viewer trusts that rather than the header.

*Opening.* Nothing here opens anything. The viewer renders a known-safe set
in-process and offers to save the rest; running an attachment is the user's
business, done in Finder, with the quarantine flag the save put there.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional, Tuple

#: Beyond this, a part is listed but not fetched without the user insisting.
#: Large enough for an ordinary offer letter or photograph, small enough that
#: a careless double-click does not pull fifty megabytes over a hotel network.
FETCH_WITHOUT_ASKING = 8 * 1024 * 1024

#: A hard ceiling on any single fetch, whatever the user asks for.
MAX_FETCH = 64 * 1024 * 1024

#: Decoded pixels. A 100-megapixel image is 400 MB in memory at 32bpp, which
#: is how a "decompression bomb" works: a few kilobytes of PNG, a gigabyte of
#: canvas. Qt is asked for the size before the pixels.
MAX_PIXELS = 64_000_000

#: Filesystem limit on most systems, in bytes rather than characters.
MAX_NAME_BYTES = 200

#: Characters that reorder what a name looks like without changing what it is.
_BIDI = "".join(chr(c) for c in (
    0x200E, 0x200F, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E,
    0x2066, 0x2067, 0x2068, 0x2069,
))

#: Extensions macOS will execute, or that ask another program to.
_EXECUTABLE = frozenset("""
app command scpt scptd workflow action osax bundle kext
exe com scr bat cmd pif msi msp cpl jar
sh bash zsh csh ksh fish py rb pl php lua ps1 psm1 vbs vbe js jse wsf wsh hta
dmg pkg mpkg iso
""".split())

#: Archives. Never expanded here - an archive is saved, not browsed, because
#: expanding one is how zip slip and zip bombs arrive.
_ARCHIVE = frozenset("zip tar gz tgz bz2 xz 7z rar lzh cab".split())

#: Executables, by their own first bytes. These are listed first and map to
#: "program" rather than to whatever the part claimed to be: a message may
#: declare image/png and begin with MZ, and the one thing that must not
#: happen is handing those bytes to an image decoder to find out.
_PROGRAM: Tuple[Tuple[bytes, str], ...] = (
    (b"MZ", "application/x-dosexec"),
    (b"\x7fELF", "application/x-executable"),
    (b"\xca\xfe\xba\xbe", "application/x-mach-binary"),
    (b"\xcf\xfa\xed\xfe", "application/x-mach-binary"),
    (b"\xce\xfa\xed\xfe", "application/x-mach-binary"),
    (b"\xfe\xed\xfa\xcf", "application/x-mach-binary"),
    (b"\xfe\xed\xfa\xce", "application/x-mach-binary"),
    (b"#!", "text/x-script"),
    (b"\xde\xd0\xd0\xdd", "application/x-mach-binary"),
)

#: Magic numbers, longest first so a prefix never wins over a longer match.
_MAGIC: Tuple[Tuple[bytes, str, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image", "image/png"),
    (b"GIF87a", "image", "image/gif"),
    (b"GIF89a", "image", "image/gif"),
    (b"\xff\xd8\xff", "image", "image/jpeg"),
    (b"BM", "image", "image/bmp"),
    (b"II*\x00", "image", "image/tiff"),
    (b"MM\x00*", "image", "image/tiff"),
    (b"%PDF-", "pdf", "application/pdf"),
    (b"ID3", "audio", "audio/mpeg"),
    (b"\xff\xfb", "audio", "audio/mpeg"),
    (b"\xff\xf3", "audio", "audio/mpeg"),
    (b"\xff\xf2", "audio", "audio/mpeg"),
    (b"OggS", "audio", "audio/ogg"),
    (b"fLaC", "audio", "audio/flac"),
    (b"PK\x03\x04", "archive", "application/zip"),
    (b"Rar!\x1a\x07", "archive", "application/x-rar-compressed"),
    (b"7z\xbc\xaf\x27\x1c", "archive", "application/x-7z-compressed"),
    (b"\x1f\x8b", "archive", "application/gzip"),
    (b"\xfd7zXZ\x00", "archive", "application/x-xz"),
    (b"%!PS", "other", "application/postscript"),
    (b"\xd0\xcf\x11\xe0", "other", "application/vnd.ms-office"),
)


def _strip_controls(text: str) -> str:
    """Remove bidi overrides and anything in the Cc/Cf categories."""
    out = []
    for character in text:
        if character in _BIDI:
            continue
        if unicodedata.category(character) in ("Cc", "Cf", "Cs", "Co", "Cn"):
            continue
        out.append(character)
    return "".join(out)


def display_name(raw: str) -> str:
    """A filename safe to put in a label.

    Bidi controls are removed rather than escaped: the point of the attack is
    that the string renders differently from how it reads, and a label has no
    way to show that. Whatever is left reads in one direction.
    """
    cleaned = _strip_controls(raw or "").strip()
    cleaned = cleaned.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    # No separators: a label that reads like a path is a label that can
    # claim to be somewhere it is not.
    cleaned = cleaned.replace("\\", "/").split("/")[-1].strip() or cleaned.strip()
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    if len(cleaned) > 120:
        cleaned = cleaned[:117] + "..."
    return cleaned or "(unnamed)"


def safe_name(raw: str, fallback: str = "attachment") -> str:
    """A filename safe to write to disk.

    Only the last path component survives, on either separator, so
    ``../../.ssh/authorized_keys`` and ``C:\\Windows\\x`` both become a name.
    Leading dots go: a file called ``.bash_profile`` is not what was attached.
    """
    cleaned = _strip_controls(raw or "")
    cleaned = cleaned.replace("\x00", "")
    cleaned = cleaned.replace("\\", "/").split("/")[-1]
    cleaned = cleaned.strip().strip(".")
    cleaned = re.sub(r'[\x00-\x1f\x7f:<>"|?*]', "_", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    while cleaned.startswith("."):
        cleaned = cleaned[1:]
    if cleaned in ("", ".", ".."):
        cleaned = fallback
    encoded = cleaned.encode("utf-8")[:MAX_NAME_BYTES]
    cleaned = encoded.decode("utf-8", "ignore") or fallback
    return cleaned


def extension(name: str) -> str:
    _, dot, ext = safe_name(name).rpartition(".")
    return ext.lower() if dot else ""


def sniff(head: bytes, declared: str = "", name: str = "") -> Tuple[str, str]:
    """(kind, content type) from the bytes, falling back to the header.

    The bytes win. A part declaring ``image/png`` that begins ``MZ`` is not a
    PNG, and the viewer must not hand it to an image decoder to find out.
    """
    for magic, mime in _PROGRAM:
        if head.startswith(magic):
            return "program", mime
    for magic, kind, mime in _MAGIC:
        if head.startswith(magic):
            return kind, mime
    if head[:4] == b"RIFF" and head[8:12] in (b"WAVE", b"WEBP"):
        if head[8:12] == b"WAVE":
            return "audio", "audio/wav"
        return "image", "image/webp"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"M4A ", b"M4B ", b"M4P "):
            return "audio", "audio/mp4"
        if brand in (b"heic", b"heix", b"hevc", b"mif1"):
            return "image", "image/heic"
        return "video", "video/mp4"

    declared = (declared or "").lower().split(";")[0].strip()
    if declared.startswith("image/"):
        return "image", declared
    if declared.startswith("audio/"):
        return "audio", declared
    if declared.startswith("video/"):
        return "video", declared
    if declared == "application/pdf":
        return "pdf", declared
    if declared.startswith("text/"):
        return "text", declared

    ext = extension(name)
    if ext in _ARCHIVE:
        return "archive", "application/octet-stream"
    if head and _looks_textual(head):
        return "text", declared or "text/plain"
    return "other", declared or "application/octet-stream"


def _looks_textual(head: bytes) -> bool:
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
        return True
    except UnicodeDecodeError:
        printable = sum(1 for b in head if 0x20 <= b < 0x7f or b in (9, 10, 13))
        return printable / max(len(head), 1) > 0.85


@dataclass(frozen=True)
class Attachment:
    """One part of a message, as the server described it.

    ``part`` is the IMAP section number ("2", "3.1"), which is how a single
    attachment is fetched without pulling the whole message down again.
    """

    part: str
    name: str
    content_type: str = "application/octet-stream"
    encoding: str = ""
    size: int = 0
    cid: str = ""
    inline: bool = False
    data: Optional[bytes] = field(default=None, repr=False, compare=False)

    @property
    def shown(self) -> str:
        return display_name(self.name)

    @property
    def filename(self) -> str:
        return safe_name(self.name, fallback=f"part-{self.part or '1'}")

    @property
    def ext(self) -> str:
        return extension(self.name)

    @property
    def kind(self) -> str:
        """What it actually is, once any bytes are in hand."""
        if self.data:
            return sniff(self.data[:32], self.content_type, self.name)[0]
        declared = (self.content_type or "").lower().split(";")[0]
        for prefix, kind in (("image/", "image"), ("audio/", "audio"),
                             ("video/", "video"), ("text/", "text")):
            if declared.startswith(prefix):
                return kind
        if declared == "application/pdf":
            return "pdf"
        if self.ext in _ARCHIVE:
            return "archive"
        return "other"

    @property
    def executable(self) -> bool:
        """Whether saving this should come with a warning.

        True for a known extension, and true for anything whose bytes say
        program whatever it claimed to be.
        """
        if self.ext in _EXECUTABLE:
            return True
        return bool(self.data) and sniff(self.data[:32])[0] == "program"

    @property
    def archive(self) -> bool:
        return self.ext in _ARCHIVE or self.kind == "archive"

    @property
    def signature(self) -> bool:
        """S/MIME and PGP parts, which are machinery rather than content."""
        declared = (self.content_type or "").lower()
        return (self.filename.lower() in ("smime.p7s", "signature.asc")
                or "pkcs7-signature" in declared
                or "pgp-signature" in declared)

    @property
    def viewable(self) -> bool:
        """Whether the viewer will render it rather than only offer to save."""
        return self.kind in ("image", "audio", "video", "pdf", "text")

    def human_size(self) -> str:
        size = float(self.size)
        for unit in ("bytes", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                return f"{int(size)} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} GB"


def unique_path(directory, name: str):
    """A path in ``directory`` that does not overwrite anything.

    Saving an attachment must never replace a file the person already had,
    so a collision gets " (2)" rather than silence.
    """
    from pathlib import Path

    directory = Path(directory)
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    candidate = directory / name
    counter = 2
    while candidate.exists():
        suffix = f".{ext}" if ext else ""
        candidate = directory / f"{stem} ({counter}){suffix}"
        counter += 1
    return candidate


def quarantine(path) -> bool:
    """Mark a saved file as downloaded, so Gatekeeper checks it.

    Without this an attachment saved by this app is more trusted by macOS
    than the same file saved by a browser, which is exactly backwards.
    """
    import subprocess

    value = f"0083;{os.times().elapsed:08x};Mail Manager;"
    try:
        subprocess.run(
            ["xattr", "-w", "com.apple.quarantine", value, str(path)],
            check=True, capture_output=True, timeout=10,
        )
        return True
    except Exception:      # noqa: BLE001 - a missing xattr is not a failure
        return False


def demo_attachments(message) -> list:
    """Synthetic parts for demo mode, generated rather than shipped.

    Demo mode has no mailbox to ask, and the repository ships no message
    data, so anything shown here is made up on the spot: a small PNG drawn
    by arithmetic, a text file, and a PDF that is a valid one-page document.
    """
    import struct
    import zlib

    def png(width: int, height: int) -> bytes:
        def chunk(tag: bytes, body: bytes) -> bytes:
            piece = tag + body
            return struct.pack(">I", len(body)) + piece + struct.pack(
                ">I", zlib.crc32(piece))

        rows = b"".join(
            b"\x00" + bytes(v for x in range(width)
                            for v in ((x * 5) % 256, (y * 7) % 256, 160))
            for y in range(height))
        return (b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(rows))
                + chunk(b"IEND", b""))

    def pdf(line: str) -> bytes:
        body = (f"BT /F1 18 Tf 60 700 Td ({line}) Tj ET").encode("ascii", "replace")
        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
            b"<< /Length " + str(len(body)).encode() + b" >>\nstream\n" + body
            + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        ]
        out = bytearray(b"%PDF-1.4\n")
        offsets = []
        for number, obj in enumerate(objects, start=1):
            offsets.append(len(out))
            out += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
        start = len(out)
        out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
        for offset in offsets:
            out += f"{offset:010d} 00000 n \n".encode()
        out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
                f"startxref\n{start}\n%%EOF\n").encode()
        return bytes(out)

    names = list(getattr(message, "attachments", ()) or ())
    made = []
    for index, name in enumerate(names, start=1):
        ext = extension(name)
        if ext == "pdf":
            data = pdf(f"Demo attachment - {safe_name(name)}")
            mime = "application/pdf"
        elif ext in ("png", "jpg", "jpeg", "gif", "webp"):
            data = png(320, 200)
            mime = "image/png"
        else:
            data = (f"{name}\n\nDemo mode generates this text rather than "
                    "shipping a file.\n").encode("utf-8")
            mime = "text/plain"
        made.append(Attachment(part=str(index), name=name, content_type=mime,
                               size=len(data), data=data))
    if not made:
        sample = png(320, 200)
        made.append(Attachment(part="1", name="sample-image.png",
                               content_type="image/png", size=len(sample),
                               data=sample))
    return made
