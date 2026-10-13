"""Word, Excel and PowerPoint attachments, read for the attachment viewer.

A document here is only ever read: as the XML its words are kept in, into
plain HTML of its words, tables and pictures, for a view that fetches
nothing. No macro runs, and no field, link or embedded object is followed.
Its parts are read in memory and never unpacked to disk, so nothing in it
can name a path to be written to.

A document protected with a password is opened with the password, in
memory, and the opened copy is never written anywhere. One protected by
Microsoft's rights management cannot be opened outside Office at all, and
neither can a message Microsoft sealed (``.rpmsg``); they say so, and how
to open them.

Everything a sender controls is bounded: how many parts a document has, how
large each may grow when unpacked, how many rows, slides and pictures are
shown, and how deep tables may nest. A document past a bound is shown up to
it, with a note saying what was left out.
"""

from __future__ import annotations

import base64
import hashlib
import html as _html
import io
import re
import struct
import zipfile
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree

WORD, EXCEL, POWERPOINT = "word", "excel", "powerpoint"
#: What each is called where a person reads it.
NAMES = {WORD: "Word document", EXCEL: "Excel workbook",
         POWERPOINT: "PowerPoint presentation"}

#: The states a file can be found in.
OPEN = "open"            # readable as it is
PASSWORD = "password"    # encrypted with a password, which opens it here
RIGHTS = "rights"        # Microsoft's rights management: Office only
OLDER = "older"          # the binary formats from before 2007
SEALED = "sealed"        # a message Microsoft sealed: Outlook only

#: An Office file's first bytes when it is a Compound File: the older
#: formats, and every protected one.
CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

#: Bounds on what a sender can make this do.
MAX_PARTS = 5000                 # entries in a document's zip
MAX_PART = 24 * 1024 * 1024      # any one part, unpacked
MAX_TOTAL = 96 * 1024 * 1024     # every part read, unpacked
MAX_PACKAGE = 64 * 1024 * 1024   # a protected document, opened
MAX_ROWS = 2000                  # rows shown of each sheet
MAX_COLUMNS = 60                 # columns shown of each sheet
MAX_CELLS = 60000                # cells shown in all
MAX_SLIDES = 300
MAX_PICTURES = 60
MAX_PICTURE = 16 * 1024 * 1024
MAX_HTML = 4 * 1024 * 1024       # the page built, before it is cut short
MAX_DEPTH = 24                   # tables within tables, groups within groups

#: Picture formats the view draws, by their first bytes.
_DRAWN = (b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"BM", b"II*\x00", b"MM\x00*")

_BY_EXTENSION = {
    **dict.fromkeys("doc docx docm dot dotx dotm".split(), WORD),
    **dict.fromkeys("xls xlsx xlsm xlsb xlt xltx xltm".split(), EXCEL),
    **dict.fromkeys("ppt pptx pptm pps ppsx ppsm pot potx potm".split(), POWERPOINT),
}
_BY_DECLARED = (("wordprocessingml", WORD), ("msword", WORD),
                ("spreadsheetml", EXCEL), ("ms-excel", EXCEL),
                ("presentationml", POWERPOINT), ("ms-powerpoint", POWERPOINT))
_APP = {WORD: "Word or Pages", EXCEL: "Excel or Numbers",
        POWERPOINT: "PowerPoint or Keynote"}


class CannotRead(Exception):
    """A document that cannot be shown, with the reason in words."""


class WrongPassword(CannotRead):
    """The password given does not open the document."""


@dataclass(frozen=True)
class Found:
    """What an attachment is, as far as Office goes."""

    kind: str           # WORD, EXCEL or POWERPOINT; "" where it cannot tell
    state: str          # OPEN, PASSWORD, RIGHTS, OLDER or SEALED
    macros: bool = False

    @property
    def name(self) -> str:
        return NAMES.get(self.kind, "Office document")


@dataclass
class Shown:
    """A document as the viewer shows it."""

    kind: str
    html: str
    #: Pictures by the names the page uses for them.
    pictures: Dict[str, bytes] = field(default_factory=dict)
    #: What was left out, in words.
    notes: List[str] = field(default_factory=list)
    #: Its size, in words: "1,318 words", "3 sheets", "12 slides".
    summary: str = ""


def _extension(name: str) -> str:
    stem, dot, ext = (name or "").strip().rpartition(".")
    return ext.lower() if dot and stem else ""


def guess(name: str, declared: str) -> str:
    """Which application a file is for, by its name or its declared type."""
    kind = _BY_EXTENSION.get(_extension(name), "")
    if kind:
        return kind
    declared = (declared or "").lower()
    return next((kind for marker, kind in _BY_DECLARED if marker in declared), "")


def _entries(data: bytes) -> Optional[int]:
    """How many entries a zip says it holds, from its last record, before
    anything reads them all: None for one that does not say, as a zip of
    more than 65,535 entries cannot."""
    end = data.rfind(b"PK\x05\x06", max(0, len(data) - 65_557))
    if end < 0 or end + 22 > len(data):
        return None
    total = struct.unpack_from("<H", data, end + 10)[0]
    return None if total == 0xFFFF else total


def identify(data: bytes, name: str = "", declared: str = "") -> Optional[Found]:
    """What ``data`` is, if it is an Office file: the bytes decide, and the
    name and declared type only name a protected one, whose contents are
    hidden. None for anything else, an ordinary zip included."""
    if not data:
        return None
    if data.startswith(b"PK\x03\x04"):
        count = _entries(data)
        if count is None or count > MAX_PARTS:
            return None
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as package:
                names = set(package.namelist())
        except (zipfile.BadZipFile, ValueError, OSError, EOFError,
                NotImplementedError, RuntimeError, struct.error):
            return None
        if "[Content_Types].xml" not in names:
            return None
        macros = any(entry.lower().endswith("vbaproject.bin") for entry in names)
        for main, kind in (("word/document.xml", WORD), ("xl/workbook.xml", EXCEL),
                           ("ppt/presentation.xml", POWERPOINT)):
            if main in names:
                return Found(kind, OPEN, macros)
        return None
    if data.startswith(CFB_MAGIC):
        try:
            streams = _Compound(data).names()
        except CannotRead:
            return None
        named = guess(name, declared)
        if streams & {"DRMEncryptedDataSpace", "DRMEncryptedTransform",
                      "\x09DRMContent", "\x09DRMDataSpace"}:
            return Found(named, RIGHTS)
        if "EncryptedPackage" in streams:
            return Found(named, PASSWORD if "EncryptionInfo" in streams else RIGHTS)
        if "WordDocument" in streams:
            return Found(WORD, OLDER)
        if streams & {"Workbook", "Book"}:
            return Found(EXCEL, OLDER)
        if "PowerPoint Document" in streams:
            return Found(POWERPOINT, OLDER)
        return None
    if _extension(name) == "rpmsg" or "rpmsg" in (declared or "").lower():
        return Found("", SEALED)
    return None


def explain(found: Found) -> str:
    """What to say about a file the viewer cannot show."""
    if found.state == RIGHTS:
        return (f"This {found.name} is protected by Microsoft's rights "
                "management, which only Microsoft Office can open, signed in "
                "with an account the sender allowed to read it. Save it and "
                "open it in Word, Excel or PowerPoint.")
    if found.state == SEALED:
        return ("This is a message Microsoft sealed with its message "
                "encryption. Only Outlook opens it, signed in with the address "
                "it was sent to; the message it came in usually carries a link "
                "to read it on Microsoft's site instead.")
    if found.state == OLDER:
        return (f"An older {found.name.split()[0]} file, in the format from "
                "before 2007, which the viewer does not read. Save it to open "
                f"it in {_APP.get(found.kind, 'Office')}.")
    return "This is not a format the viewer opens."


# -- The Compound File, where older and protected documents are kept -------

class _Compound:
    """A Compound File Binary (MS-CFB): just enough of it to list what it
    holds and read the few streams a protected document needs. Every sector
    number is checked against the file and every chain against loops; a
    file that breaks either is refused."""

    END = 0xFFFFFFFE
    #: Sector numbers this high are markers, never sectors.
    MARKERS = 0xFFFFFFFA

    def __init__(self, data: bytes) -> None:
        if len(data) < 512 or not data.startswith(CFB_MAGIC):
            raise CannotRead("This is not a compound file.")
        major, order, shift, mini_shift = struct.unpack_from("<HHHH", data, 26)
        if order != 0xFFFE or shift not in (9, 12) or mini_shift != 6:
            raise CannotRead("The file's header is damaged.")
        self.data = data
        self.size = 1 << shift
        self.sectors = (len(data) + self.size - 1) // self.size - 1
        (fat_count, first_directory, _signature, self.cutoff, first_mini_fat,
         _mini_fat_count, first_difat, _difat_count) = struct.unpack_from(
            "<IIIIIIII", data, 44)
        per_sector = self.size // 4
        difat = list(struct.unpack_from("<109I", data, 76))
        for sector in self._walk(first_difat):
            entries = struct.unpack_from(f"<{per_sector}I", self._sector(sector))
            difat.extend(entries[:-1])
        fat: List[int] = []
        for sector in difat[:fat_count]:
            if sector >= self.MARKERS:
                continue
            fat.extend(struct.unpack_from(f"<{per_sector}I", self._sector(sector)))
        self.fat = fat
        directory = self._read_chain(first_directory, self.fat, self._sector, None)
        self.entries: List[Tuple[str, int, int, int]] = []
        for offset in range(0, len(directory) - 127, 128):
            length, kind = struct.unpack_from("<HB", directory, offset + 64)
            start, size = struct.unpack_from("<IQ", directory, offset + 116)
            if major == 3:
                size &= 0xFFFFFFFF
            if kind not in (1, 2, 5) or not 2 <= length <= 64 or length % 2:
                self.entries.append(("", 0, 0, 0))
                continue
            name = directory[offset:offset + length - 2].decode("utf-16-le", "replace")
            self.entries.append((name, kind, start, size))
        if not self.entries or self.entries[0][1] != 5:
            raise CannotRead("The file has no root.")
        _root, _kind, root_start, root_size = self.entries[0]
        self.mini_stream = (self._read_chain(root_start, self.fat, self._sector,
                                             root_size) if root_size else b"")
        mini_fat = (self._read_chain(first_mini_fat, self.fat, self._sector, None)
                    if first_mini_fat < self.MARKERS else b"")
        self.mini_fat = list(struct.unpack_from(f"<{len(mini_fat) // 4}I", mini_fat))

    def _sector(self, number: int) -> bytes:
        if number >= self.MARKERS or number >= self.sectors:
            raise CannotRead("The file points past its end.")
        start = (number + 1) * self.size
        return self.data[start:start + self.size]

    def _walk(self, first: int):
        """The DIFAT's sectors, in order: each names the next in its last
        word."""
        seen = set()
        sector = first
        while sector < self.MARKERS:
            if sector in seen or len(seen) > self.sectors:
                raise CannotRead("The file's chains run in a loop.")
            seen.add(sector)
            yield sector
            last = self._sector(sector)[-4:]
            sector = struct.unpack("<I", last)[0] if len(last) == 4 else self.END

    def _read_chain(self, first: int, table: List[int], read,
                    size: Optional[int]) -> bytes:
        if size is not None and size > len(self.data):
            raise CannotRead("A stream claims to be larger than the file.")
        out = bytearray()
        seen = set()
        sector = first
        while sector < self.MARKERS:
            if sector in seen:
                raise CannotRead("The file's chains run in a loop.")
            seen.add(sector)
            out += read(sector)
            if size is not None and len(out) >= size:
                break
            if len(out) > len(self.data):
                raise CannotRead("A stream runs past the file.")
            if sector >= len(table):
                raise CannotRead("The file's table is short.")
            sector = table[sector]
        return bytes(out if size is None else out[:size])

    def _mini_sector(self, number: int) -> bytes:
        start = number * 64
        if start >= len(self.mini_stream):
            raise CannotRead("The file points past its end.")
        return self.mini_stream[start:start + 64]

    def names(self) -> set:
        return {name for name, kind, _start, _size in self.entries if kind}

    def stream(self, wanted: str, limit: int) -> bytes:
        for name, kind, start, size in self.entries:
            if kind != 2 or name != wanted:
                continue
            if size > limit:
                raise CannotRead("The document is too large to open here.")
            if size < self.cutoff:
                return self._read_chain(start, self.mini_fat, self._mini_sector, size)
            return self._read_chain(start, self.fat, self._sector, size)
        raise CannotRead("The document's protection is damaged.")


# -- Opening a document protected with a password (MS-OFFCRYPTO) -----------

#: The block keys the agile scheme derives each of its keys with.
_VERIFIER_INPUT = bytes.fromhex("fea7d2763b4b9e79")
_VERIFIER_VALUE = bytes.fromhex("d7aa0f6d3061344e")
_KEY_VALUE = bytes.fromhex("146e0be7abacd0d6")
_HASHES = {"SHA1": "sha1", "SHA256": "sha256", "SHA384": "sha384",
           "SHA512": "sha512"}
_PASSWORD_KEY = "{http://schemas.microsoft.com/office/2006/keyEncryptor/password}encryptedKey"
#: Office's own limit on how many times a password is hashed.
_MAX_SPINS = 10_000_000


def decrypt(data: bytes, password: str) -> bytes:
    """The document inside a password-protected Office file, opened with
    ``password``: raises WrongPassword when it does not open it, and
    CannotRead for a protection this does not know."""
    try:
        box = _Compound(data)
        info = box.stream("EncryptionInfo", 1024 * 1024)
        package = box.stream("EncryptedPackage", MAX_PACKAGE + 8 + 4096)
        if len(info) < 8 or len(package) < 8:
            raise CannotRead("The document's protection is damaged.")
        major, minor = struct.unpack_from("<HH", info, 0)
        if (major, minor) == (4, 4):
            return _agile(info, package, password)
        if minor == 2 and major in (2, 3, 4):
            return _standard(info, package, password)
    except (struct.error, ValueError, OverflowError) as exc:
        raise CannotRead("The document's protection is damaged.") from exc
    raise CannotRead("It is protected in a way the viewer does not open. "
                     "Save it and open it in Office.")


def _aes(key: bytes, data: bytes, iv: Optional[bytes] = None) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    data = data[:len(data) - len(data) % 16]
    mode = modes.CBC(iv) if iv is not None else modes.ECB()
    worker = Cipher(algorithms.AES(key), mode).decryptor()
    return worker.update(data) + worker.finalize()


def _fit(value: bytes, size: int) -> bytes:
    """``value`` cut or padded to ``size`` bytes, as the scheme pads: 0x36."""
    return value[:size] if len(value) >= size else value + b"\x36" * (size - len(value))


def _agile(info: bytes, package: bytes, password: str) -> bytes:
    """ECMA-376 agile encryption, Office's own since 2010."""
    root = _xml(info[8:])
    key_data = next((found for found in root.iter()
                     if _local(found.tag) == "keyData"), None)
    encrypted_key = next((found for found in root.iter()
                          if found.tag == _PASSWORD_KEY), None)
    if key_data is None or encrypted_key is None:
        raise CannotRead("It is protected by a certificate rather than a "
                         "password, which only Office opens.")
    for element in (key_data, encrypted_key):
        if (element.get("cipherAlgorithm") != "AES"
                or element.get("cipherChaining") != "ChainingModeCBC"
                or element.get("hashAlgorithm") not in _HASHES):
            raise CannotRead("It is protected with a cipher the viewer does "
                             "not know. Save it and open it in Office.")
    try:
        spins = int(encrypted_key.get("spinCount", "100000"))
        key_bits = int(encrypted_key.get("keyBits", "0"))
        package_bits = int(key_data.get("keyBits", "0"))
        block = int(key_data.get("blockSize", "16"))
        salt_size = int(encrypted_key.get("saltSize", "16"))
        hash_size = int(encrypted_key.get("hashSize", "20"))
        salt = base64.b64decode(encrypted_key.get("saltValue", ""))
        data_salt = base64.b64decode(key_data.get("saltValue", ""))
        hash_input = base64.b64decode(encrypted_key.get("encryptedVerifierHashInput", ""))
        hash_value = base64.b64decode(encrypted_key.get("encryptedVerifierHashValue", ""))
        key_value = base64.b64decode(encrypted_key.get("encryptedKeyValue", ""))
    except (ValueError, TypeError) as exc:
        raise CannotRead("The document's protection is damaged.") from exc
    if (not 0 <= spins <= _MAX_SPINS or key_bits not in (128, 192, 256)
            or package_bits not in (128, 192, 256) or block != 16 or not salt
            or not 0 < hash_size <= 64 or not 0 < salt_size <= 64):
        raise CannotRead("The document's protection is damaged.")
    hashing = _HASHES[encrypted_key.get("hashAlgorithm")]
    digest = hashlib.new(hashing, salt + password.encode("utf-16-le")).digest()
    count = struct.Struct("<I").pack
    for spin in range(spins):
        digest = hashlib.new(hashing, count(spin) + digest).digest()

    def key(block_key: bytes) -> bytes:
        return _fit(hashlib.new(hashing, digest + block_key).digest(), key_bits // 8)

    iv = _fit(salt, 16)
    verifier = _aes(key(_VERIFIER_INPUT), hash_input, iv)[:salt_size]
    expected = _aes(key(_VERIFIER_VALUE), hash_value, iv)[:hash_size]
    if not expected or hashlib.new(hashing, verifier).digest()[:hash_size] != expected:
        raise WrongPassword("That password does not open it.")
    secret = _aes(key(_KEY_VALUE), key_value, iv)[:package_bits // 8]
    if len(secret) != package_bits // 8:
        raise CannotRead("The document's protection is damaged.")
    size = struct.unpack_from("<Q", package, 0)[0]
    if size > MAX_PACKAGE:
        raise CannotRead("The document is too large to open here.")
    data_hashing = _HASHES[key_data.get("hashAlgorithm")]
    body = package[8:]
    out = bytearray()
    for index, start in enumerate(range(0, len(body), 4096)):
        segment_iv = _fit(hashlib.new(data_hashing, data_salt + count(index)).digest(), 16)
        out += _aes(secret, body[start:start + 4096], segment_iv)
        if len(out) >= size:
            break
    return bytes(out[:size])


def _standard(info: bytes, package: bytes, password: str) -> bytes:
    """ECMA-376 standard encryption, Office 2007's: AES with SHA-1."""
    header_size = struct.unpack_from("<I", info, 8)[0]
    (_flags, _extra, algorithm, hashing, key_bits) = struct.unpack_from(
        "<IIIII", info, 12)
    verifier_at = 12 + header_size
    salt_size = struct.unpack_from("<I", info, verifier_at)[0]
    salt = info[verifier_at + 4:verifier_at + 20]
    encrypted_verifier = info[verifier_at + 20:verifier_at + 36]
    encrypted_hash = info[verifier_at + 40:verifier_at + 72]
    if algorithm not in (0x660E, 0x660F, 0x6610) or hashing not in (0, 0x8004):
        raise CannotRead("It is protected with a cipher the viewer does not "
                         "know. Save it and open it in Office.")
    key_bits = key_bits or 128
    if (key_bits not in (128, 192, 256) or salt_size != 16
            or len(encrypted_verifier) < 16 or len(encrypted_hash) < 32):
        raise CannotRead("The document's protection is damaged.")
    count = struct.Struct("<I").pack
    digest = hashlib.sha1(salt + password.encode("utf-16-le")).digest()
    for spin in range(50000):
        digest = hashlib.sha1(count(spin) + digest).digest()
    digest = hashlib.sha1(digest + count(0)).digest()
    inner = hashlib.sha1(bytes(b ^ 0x36 for b in digest) + b"\x36" * 44).digest()
    outer = hashlib.sha1(bytes(b ^ 0x5C for b in digest) + b"\x5c" * 44).digest()
    key = (inner + outer)[:key_bits // 8]
    verifier = _aes(key, encrypted_verifier)
    if hashlib.sha1(verifier).digest() != _aes(key, encrypted_hash)[:20]:
        raise WrongPassword("That password does not open it.")
    size = struct.unpack_from("<Q", package, 0)[0]
    if size > MAX_PACKAGE:
        raise CannotRead("The document is too large to open here.")
    return _aes(key, package[8:8 + size + 16])[:size]


# -- Reading the XML ---------------------------------------------------------

def _refuse_markup(data: bytes) -> None:
    """No Office part declares a document type: one that does is refused
    rather than have its entities expanded."""
    if b"<!doctype" in data[:4096].lower() or b"<!entity" in data.lower():
        raise CannotRead("The document carries markup Office never writes.")


def _xml(data: bytes) -> ElementTree.Element:
    _refuse_markup(data)
    try:
        return ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise CannotRead("The document is damaged.") from exc


def _local(tag) -> str:
    """A tag or attribute's name without its namespace: the strict and the
    transitional forms of Office's XML name the same things alike."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _children(element, name: str):
    return [child for child in (element if element is not None else ())
            if _local(child.tag) == name]


def _child(element, name: str):
    for child in (element if element is not None else ()):
        if _local(child.tag) == name:
            return child
    return None


def _get(element, name: str, default: str = "") -> str:
    """An attribute by its name alone, whatever its namespace."""
    if element is None:
        return default
    for key, value in element.attrib.items():
        if _local(key) == name:
            return value
    return default


def _relation(element, name: str = "id") -> str:
    """A relationship id an element points by (r:id, r:embed)."""
    if element is None:
        return ""
    for key, value in element.attrib.items():
        if _local(key) == name and "relationships" in key:
            return value
    return ""


def _on(properties, name: str) -> bool:
    """A toggle in a run's properties: present, and not switched off."""
    found = _child(properties, name)
    return found is not None and _get(found, "val", "1").lower() not in (
        "0", "false", "off", "none")


def _text(value: str) -> str:
    return _html.escape(value, quote=False)


def _hex(value: str) -> str:
    """A colour Office wrote as six hex digits, as CSS, or ""."""
    return f"#{value.lower()}" if re.fullmatch(r"[0-9A-Fa-f]{6}", value or "") else ""


def _pale(colour: str) -> bool:
    """Whether text in ``colour`` would vanish on a white page."""
    return bool(colour) and all(int(colour[at:at + 2], 16) >= 0xE0 for at in (1, 3, 5))


class _Package:
    """An Office Open XML file: its parts, each read only up to a bound."""

    def __init__(self, data: bytes) -> None:
        try:
            self.zip = zipfile.ZipFile(io.BytesIO(data))
            infos = self.zip.infolist()
        except (zipfile.BadZipFile, ValueError, OSError, EOFError,
                struct.error) as exc:
            raise CannotRead("The document is damaged.") from exc
        if len(infos) > MAX_PARTS:
            raise CannotRead("The document has too many parts to open here.")
        self.names = {info.filename for info in infos}
        self.read_so_far = 0
        self.notes: List[str] = []

    def part(self, name: str, limit: Optional[int] = None) -> Optional[bytes]:
        if name not in self.names:
            return None
        # Read now, not when the module was: a bound a test or a caller
        # changes holds.
        limit = MAX_PART if limit is None else limit
        try:
            with self.zip.open(name) as handle:
                data = handle.read(limit + 1)
        except (zipfile.BadZipFile, zlib.error, OSError, EOFError, RuntimeError,
                NotImplementedError, ValueError, struct.error) as exc:
            raise CannotRead("The document is damaged.") from exc
        if len(data) > limit:
            raise CannotRead("The document is too large to open here.")
        self.read_so_far += len(data)
        if self.read_so_far > MAX_TOTAL:
            raise CannotRead("The document is too large to open here.")
        return data

    def xml(self, name: Optional[str]) -> Optional[ElementTree.Element]:
        data = self.part(name) if name else None
        return _xml(data) if data is not None else None

    def relations(self, name: str) -> Dict[str, Tuple[str, str, bool]]:
        """A part's relationships: id to (the part it names, its type, and
        whether it is outside the document)."""
        folder, _, leaf = name.rpartition("/")
        found = self.xml(f"{folder}/_rels/{leaf}.rels" if folder
                         else f"_rels/{leaf}.rels")
        out: Dict[str, Tuple[str, str, bool]] = {}
        for item in (found if found is not None else ()):
            if _local(item.tag) != "Relationship":
                continue
            target = item.get("Target", "")
            outside = item.get("TargetMode", "") == "External"
            out[item.get("Id", "")] = (target if outside else _resolve(folder, target),
                                       item.get("Type", ""), outside)
        return out

    def main(self, fallback: str) -> str:
        """The document's own part, as its relationships name it."""
        for target, kind, outside in self.relations("").values():
            if not outside and kind.endswith("/officeDocument") and target in self.names:
                return target
        return fallback

    def related(self, name: str, ending: str) -> Optional[str]:
        """The part ``name`` relates to by a relationship whose type ends
        with ``ending``: its styles, its numbering, its shared strings."""
        for target, kind, outside in self.relations(name).values():
            if not outside and kind.endswith(ending) and target in self.names:
                return target
        return None


def _resolve(folder: str, target: str) -> str:
    """A relationship's target as a part name, kept inside the package."""
    path = target.lstrip("/") if target.startswith("/") else (
        f"{folder}/{target}" if folder else target)
    parts: List[str] = []
    for piece in path.split("/"):
        if piece in ("", "."):
            continue
        if piece == "..":
            if parts:
                parts.pop()
            continue
        parts.append(piece)
    return "/".join(parts)


#: English Metric Units in a pixel at 96 to the inch.
_EMU = 9525.0
#: The widest a picture is shown, in pixels: about a page's text.
_WIDEST = 640.0


class _Page:
    """The HTML being built, the pictures it names, and the notes on what
    was left out."""

    def __init__(self, package: _Package) -> None:
        self.package = package
        self.out: List[str] = []
        self.size = 0
        self.full = False
        self.pictures: Dict[str, bytes] = {}
        self.undrawn = 0
        self.unpictured = 0

    def add(self, piece: str) -> None:
        if self.full:
            return
        self.out.append(piece)
        self.size += len(piece)
        if self.size > MAX_HTML:
            self.full = True

    def picture(self, part: Optional[str], wide: float = 0, tall: float = 0) -> str:
        """An image tag for the picture at ``part``, or nothing: a picture
        past the bound, or in a format the view does not draw, is counted
        and left out."""
        if not part or part not in self.package.names:
            return ""
        if len(self.pictures) >= MAX_PICTURES:
            self.unpictured += 1
            return ""
        try:
            data = self.package.part(part, MAX_PICTURE)
        except CannotRead:
            self.unpictured += 1
            return ""
        if not data or not data.startswith(_DRAWN):
            self.undrawn += 1
            return ""
        name = f"mm-picture:{len(self.pictures) + 1}"
        self.pictures[name] = data
        size = ""
        if wide > 0 and tall > 0:
            scale = min(1.0, _WIDEST / wide)
            size = f' width="{max(1, int(wide * scale))}" height="{max(1, int(tall * scale))}"'
        return f'<img src="{name}"{size} />'

    def notes(self) -> List[str]:
        said = list(self.package.notes)
        if self.full:
            said.append("It is long, and only its beginning is shown.")
        if self.undrawn:
            said.append(f"{self.undrawn} picture{'s are' if self.undrawn != 1 else ' is'} "
                        "in a format the viewer does not draw.")
        if self.unpictured:
            said.append(f"{self.unpictured} more picture"
                        f"{'s are' if self.unpictured != 1 else ' is'} not shown.")
        return said


def read(data: bytes) -> Shown:
    """A Word, Excel or PowerPoint file as the viewer shows it. Raises
    CannotRead with the reason for one that cannot be shown."""
    found = identify(data)
    if found is None or found.state != OPEN:
        raise CannotRead("This is not a document the viewer reads.")
    package = _Package(data)
    page = _Page(package)
    try:
        if found.kind == WORD:
            summary = _Word(page).read()
        elif found.kind == EXCEL:
            summary = _Excel(page).read()
        else:
            summary = _PowerPoint(page).read()
    except RecursionError as exc:
        raise CannotRead("The document is nested too deeply to show.") from exc
    except (ValueError, KeyError, IndexError, TypeError, struct.error,
            OverflowError) as exc:
        raise CannotRead("The document is damaged.") from exc
    return Shown(found.kind, "".join(page.out), page.pictures, page.notes(),
                 summary)


# -- Word --------------------------------------------------------------------

_ROMAN = (("m", 1000), ("cm", 900), ("d", 500), ("cd", 400), ("c", 100),
          ("xc", 90), ("l", 50), ("xl", 40), ("x", 10), ("ix", 9), ("v", 5),
          ("iv", 4), ("i", 1))
#: Word's highlighter colours.
_MARKER = {"yellow": "#ffff00", "green": "#00ff00", "cyan": "#00ffff",
           "magenta": "#ff00ff", "blue": "#0000ff", "red": "#ff0000",
           "darkBlue": "#000080", "darkCyan": "#008080", "darkGreen": "#008000",
           "darkMagenta": "#800080", "darkRed": "#800000", "darkYellow": "#808000",
           "darkGray": "#808080", "lightGray": "#c0c0c0", "black": "#000000"}


def _roman(number: int) -> str:
    out = []
    for letters, value in _ROMAN:
        while number >= value:
            out.append(letters)
            number -= value
    return "".join(out)


def _letters(number: int) -> str:
    out = ""
    while number > 0:
        number, rest = divmod(number - 1, 26)
        out = chr(ord("a") + rest) + out
    return out


def _counted(style: str, number: int) -> str:
    number = max(0, min(number, 100_000))
    if style == "lowerLetter":
        return _letters(number)
    if style == "upperLetter":
        return _letters(number).upper()
    if style == "lowerRoman":
        return _roman(number)
    if style == "upperRoman":
        return _roman(number).upper()
    return str(number)


class _Word:
    def __init__(self, page: _Page) -> None:
        self.page = page
        self.package = page.package
        self.main = self.package.main("word/document.xml")
        self.links = self.package.relations(self.main)
        self.headings = self._headings()
        self.levels = self._numbering()
        self.counts: Dict[Tuple[str, int], int] = {}
        self.words = 0
        #: The shading behind the text being read, which lets pale text show.
        self.shade = ""

    def read(self) -> str:
        root = self.package.xml(self.main)
        body = _child(root, "body")
        if body is None:
            raise CannotRead("The document is damaged.")
        for element in body:
            self._block(element, 0)
            if self.page.full:
                break
        return f"{self.words:,} word{'s' if self.words != 1 else ''}"

    def _headings(self) -> Dict[str, int]:
        """Which paragraph styles are headings, and of what level."""
        root = self.package.xml(self.package.related(self.main, "/styles"))
        level: Dict[str, int] = {}
        based: Dict[str, str] = {}
        for style in _children(root, "style"):
            ident = _get(style, "styleId")
            name = _get(_child(style, "name"), "val").lower()
            outline = _get(_child(_child(style, "pPr"), "outlineLvl"), "val")
            found = re.fullmatch(r"heading ([1-9])", name)
            if name == "title":
                level[ident] = 1
            elif found:
                level[ident] = int(found.group(1))
            elif outline.isdigit() and int(outline) < 9:
                level[ident] = int(outline) + 1
            parent = _get(_child(style, "basedOn"), "val")
            if parent:
                based[ident] = parent
        for ident in list(based):
            parent, hops = based.get(ident), 0
            while ident not in level and parent and hops < 12:
                if parent in level:
                    level[ident] = level[parent]
                parent, hops = based.get(parent), hops + 1
        return level

    def _numbering(self) -> Dict[str, Dict[int, Tuple[str, str, int]]]:
        """Each list's levels: how an item is marked, and where it starts."""
        root = self.package.xml(self.package.related(self.main, "/numbering"))
        abstract: Dict[str, Dict[int, Tuple[str, str, int]]] = {}
        for item in _children(root, "abstractNum"):
            levels = {}
            for level in _children(item, "lvl"):
                try:
                    depth = int(_get(level, "ilvl", "0"))
                    start = int(_get(_child(level, "start"), "val", "1"))
                except ValueError:
                    continue
                levels[depth] = (_get(_child(level, "numFmt"), "val", "decimal"),
                                 _get(_child(level, "lvlText"), "val", "%1."), start)
            abstract[_get(item, "abstractNumId")] = levels
        return {_get(item, "numId"): abstract.get(
                    _get(_child(item, "abstractNumId"), "val"), {})
                for item in _children(root, "num")}

    def _block(self, element, depth: int) -> None:
        if depth > MAX_DEPTH or self.page.full:
            return
        kind = _local(element.tag)
        if kind == "p":
            self._paragraph(element, depth)
        elif kind == "tbl":
            self._table(element, depth)
        elif kind in ("sdt", "customXml", "smartTag", "AlternateContent"):
            inner = {"sdt": "sdtContent", "AlternateContent": "Choice"}.get(kind)
            holder = _child(element, inner) if inner else element
            for child in (holder if holder is not None else ()):
                self._block(child, depth + 1)

    def _marker(self, properties) -> str:
        numbered = _child(properties, "numPr")
        if numbered is None:
            return ""
        ident = _get(_child(numbered, "numId"), "val")
        try:
            depth = max(0, min(8, int(_get(_child(numbered, "ilvl"), "val", "0"))))
        except ValueError:
            depth = 0
        levels = self.levels.get(ident)
        if not levels or ident == "0":
            return ""
        style, template, start = levels.get(depth, ("bullet", "•", 1))
        for key in [key for key in self.counts if key[0] == ident and key[1] > depth]:
            del self.counts[key]
        count = self.counts.get((ident, depth), start - 1) + 1
        self.counts[(ident, depth)] = count
        indent = "&nbsp;" * 4 * depth
        if style in ("bullet", "none"):
            return f"{indent}{'•◦▪'[depth % 3] if style == 'bullet' else ''}&nbsp;"

        def number(found) -> str:
            level = int(found.group(1)) - 1
            fmt = levels.get(level, ("decimal", "", 1))
            return _counted(fmt[0], self.counts.get((ident, level), fmt[2]))

        return f"{indent}{_text(re.sub(r'%([1-9])', number, template))}&nbsp;"

    def _paragraph(self, element, depth: int) -> None:
        properties = _child(element, "pPr")
        level = self.headings.get(_get(_child(properties, "pStyle"), "val"))
        align = {"center": "center", "right": "right", "end": "right",
                 "both": "justify", "distribute": "justify"}.get(
            _get(_child(properties, "jc"), "val"), "")
        outer = self.shade
        fill = _hex(_get(_child(properties, "shd"), "fill"))
        if fill:
            self.shade = fill
        boxes: List = []
        inline = self._inline(element, 0, boxes)
        tag = f"h{min(level, 4)}" if level else "p"
        aligned = f' align="{align}"' if align else ""
        shaded = f' style="background-color:{fill}"' if fill else ""
        self.page.add(f"<{tag}{aligned}{shaded}>{self._marker(properties)}{inline}</{tag}>")
        self.shade = outer
        for box in boxes:
            for inner in box:
                self._block(inner, depth + 1)

    def _inline(self, element, depth: int, boxes: List) -> str:
        if depth > MAX_DEPTH:
            return ""
        out = []
        for child in element:
            kind = _local(child.tag)
            if kind == "r":
                out.append(self._run(child, boxes))
            elif kind == "hyperlink":
                out.append(self._link(child, depth, boxes))
            elif kind in ("ins", "smartTag", "customXml", "fldSimple", "bdo",
                          "dir", "moveTo"):
                out.append(self._inline(child, depth + 1, boxes))
            elif kind in ("sdt", "AlternateContent"):
                holder = _child(child, "sdtContent" if kind == "sdt" else "Choice")
                if holder is not None:
                    out.append(self._inline(holder, depth + 1, boxes))
        return "".join(out)

    def _link(self, element, depth: int, boxes: List) -> str:
        words = self._inline(element, depth + 1, boxes)
        target, _kind, outside = self.links.get(_relation(element), ("", "", False))
        shown = f'<span style="color:#1a5fb4; text-decoration:underline">{words}</span>'
        # Where it goes, written out: the viewer opens no links, and a
        # link's words need not say where it leads.
        if outside and target and _text(target) not in words:
            shown += f' <span style="color:#6e6e73">({_text(target)})</span>'
        return shown

    def _run(self, run, boxes: List) -> str:
        properties = _child(run, "rPr")
        if _on(properties, "vanish") or _on(properties, "specVanish"):
            return ""
        pieces = []
        for child in run:
            kind = _local(child.tag)
            if kind == "t":
                value = child.text or ""
                self.words += len(value.split())
                pieces.append(_text(value))
            elif kind == "tab":
                pieces.append("&emsp;")
            elif kind in ("br", "cr"):
                pieces.append("<br />")
            elif kind == "noBreakHyphen":
                pieces.append("‑")
            elif kind in ("drawing", "pict", "AlternateContent", "object"):
                pieces.append(self._drawing(child, boxes))
        if not pieces:
            return ""
        shown = "".join(pieces)
        if _on(properties, "b"):
            shown = f"<b>{shown}</b>"
        if _on(properties, "i"):
            shown = f"<i>{shown}</i>"
        underline = _child(properties, "u")
        if underline is not None and _get(underline, "val", "single") != "none":
            shown = f"<u>{shown}</u>"
        if _on(properties, "strike") or _on(properties, "dstrike"):
            shown = f"<s>{shown}</s>"
        position = _get(_child(properties, "vertAlign"), "val")
        if position in ("superscript", "subscript"):
            tag = "sup" if position == "superscript" else "sub"
            shown = f"<{tag}>{shown}</{tag}>"
        style = []
        behind = (_MARKER.get(_get(_child(properties, "highlight"), "val"), "")
                  or _hex(_get(_child(properties, "shd"), "fill")))
        if behind:
            style.append(f"background-color:{behind}")
        colour = _hex(_get(_child(properties, "color"), "val"))
        # Pale text was written for the shading behind it; on the white page
        # without that shading it would vanish, so it takes the page's own.
        if colour and colour != "#000000" and not (_pale(colour) and not (
                behind or self.shade)):
            style.append(f"color:{colour}")
        if style:
            shown = f'<span style="{"; ".join(style)}">{shown}</span>'
        return shown

    def _drawing(self, element, boxes: List) -> str:
        """A picture in the text, and any text box it carries, which is
        shown after its paragraph."""
        if _local(element.tag) == "AlternateContent":
            choice = _child(element, "Choice")
            return "".join(self._drawing(inner, boxes)
                           for inner in (choice if choice is not None else ()))
        out = []
        wide = tall = 0.0
        for found in element.iter():
            kind = _local(found.tag)
            if kind == "extent":
                try:
                    wide = int(_get(found, "cx", "0")) / _EMU
                    tall = int(_get(found, "cy", "0")) / _EMU
                except ValueError:
                    wide = tall = 0.0
            elif kind in ("blip", "imagedata"):
                ident = _relation(found, "embed") or _relation(found, "id")
                target, _kind, outside = self.links.get(ident, ("", "", True))
                if not outside:
                    out.append(self.page.picture(target, wide, tall))
            elif kind == "txbxContent":
                boxes.append(list(found))
        return "".join(out)

    def _table(self, element, depth: int) -> None:
        self.page.add('<table border="1" cellspacing="0" cellpadding="4">')
        for row in _children(element, "tr"):
            self.page.add("<tr>")
            for cell in _children(row, "tc"):
                properties = _child(cell, "tcPr")
                try:
                    span = max(1, min(64, int(_get(_child(properties, "gridSpan"),
                                                   "val", "1"))))
                except ValueError:
                    span = 1
                fill = _hex(_get(_child(properties, "shd"), "fill"))
                attributes = (f' colspan="{span}"' if span > 1 else "") + (
                    f' bgcolor="{fill}"' if fill else "")
                merged = _child(properties, "vMerge")
                self.page.add(f"<td{attributes}>")
                if merged is None or _get(merged, "val") == "restart":
                    outer = self.shade
                    self.shade = fill or outer
                    for inner in cell:
                        self._block(inner, depth + 1)
                    self.shade = outer
                self.page.add("</td>")
            self.page.add("</tr>")
        self.page.add("</table>")


# -- Excel -------------------------------------------------------------------

#: Excel's own number formats that are dates, and those that are times.
_DATE_FORMATS = set(range(14, 18)) | {22, 27, 30, 36, 50, 57}
_TIME_FORMATS = set(range(18, 22)) | {45, 46, 47}
_BUILT_IN = {1: "0", 2: "0.00", 3: "#,##0", 4: "#,##0.00", 9: "0%", 10: "0.00%",
             11: "0.00E+00", 37: "#,##0", 38: "#,##0", 39: "#,##0.00",
             40: "#,##0.00", 48: "##0.0E+0", 49: "@"}
#: Everything in a number format that shows nothing of the number: quoted
#: words, bracketed colours and locales, escaped characters, padding.
_DECORATION = re.compile(r'"[^"]*"|\[[^\]]*\]|\\.|_.|\*.')
_CURRENCY = "$£€¥₹"


def _column(reference: str) -> int:
    """The column a cell reference is in, from 1: "C7" is 3."""
    number = 0
    for letter in reference[:4]:
        if not ("A" <= letter.upper() <= "Z"):
            break
        number = number * 26 + (ord(letter.upper()) - 64)
    return number


class _Excel:
    def __init__(self, page: _Page) -> None:
        self.page = page
        self.package = page.package
        self.main = self.package.main("xl/workbook.xml")
        self.cells = 0
        self.base = datetime(1899, 12, 30)
        self.strings: List[str] = []
        self.formats: List[str] = []

    def read(self) -> str:
        book = self.package.xml(self.main)
        links = self.package.relations(self.main)
        if _get(_child(book, "workbookPr"), "date1904") in ("1", "true"):
            self.base = datetime(1904, 1, 1)
        self.strings = self._strings()
        self.formats = self._formats()
        shown = hidden = 0
        for sheet in _children(_child(book, "sheets"), "sheet"):
            if _get(sheet, "state") in ("hidden", "veryHidden"):
                hidden += 1
                continue
            target, kind, outside = links.get(_relation(sheet), ("", "", True))
            title = _get(sheet, "name", "Sheet")
            self.page.add(f"<h3>{_text(title)}</h3>")
            shown += 1
            if outside or not kind.endswith("/worksheet") or target not in self.package.names:
                self.page.add('<p style="color:#6e6e73">A chart or a sheet of '
                              "another kind, which the viewer does not draw.</p>")
                continue
            self._sheet(target, title)
            if self.page.full:
                break
        if hidden:
            self.package.notes.append(
                f"{hidden} hidden sheet{'s are' if hidden != 1 else ' is'} not shown.")
        return f"{shown} sheet{'s' if shown != 1 else ''}"

    def _strings(self) -> List[str]:
        root = self.package.xml(self.package.related(self.main, "/sharedStrings"))
        out = []
        for item in _children(root, "si"):
            pieces = []
            for child in item:
                kind = _local(child.tag)
                if kind == "t":
                    pieces.append(child.text or "")
                elif kind == "r":
                    pieces.extend(found.text or "" for found in _children(child, "t"))
            out.append("".join(pieces))
        return out

    def _formats(self) -> List[str]:
        """The number format of each cell style, by its index."""
        root = self.package.xml(self.package.related(self.main, "/styles"))
        custom = {_get(item, "numFmtId"): _get(item, "formatCode")
                  for item in _children(_child(root, "numFmts"), "numFmt")}
        out = []
        for style in _children(_child(root, "cellXfs"), "xf"):
            ident = _get(style, "numFmtId", "0")
            number = int(ident) if ident.isdigit() else 0
            if ident in custom:
                out.append(custom[ident])
            elif number in _DATE_FORMATS:
                out.append("date")
            elif number in _TIME_FORMATS:
                out.append("time")
            else:
                out.append(_BUILT_IN.get(number, ""))
        return out

    def _sheet(self, part: str, title: str) -> None:
        data = self.package.part(part)
        if data is None:
            return
        _refuse_markup(data)
        rows: List[Tuple[int, Dict[int, Tuple[str, bool]]]] = []
        widest = 0
        cut = False
        try:
            for _event, element in ElementTree.iterparse(io.BytesIO(data)):
                if _local(element.tag) != "row":
                    continue
                if len(rows) >= MAX_ROWS or self.cells >= MAX_CELLS:
                    cut = True
                    break
                try:
                    number = int(_get(element, "r", str(len(rows) + 1)))
                except ValueError:
                    number = len(rows) + 1
                cells: Dict[int, Tuple[str, bool]] = {}
                for at, cell in enumerate(_children(element, "c"), 1):
                    column = _column(_get(cell, "r")) or at
                    if column > MAX_COLUMNS:
                        cut = True
                        continue
                    value = self._value(cell)
                    if value[0]:
                        cells[column] = value
                        widest = max(widest, column)
                        self.cells += 1
                element.clear()
                if cells:
                    rows.append((number, cells))
        except ElementTree.ParseError as exc:
            raise CannotRead("The document is damaged.") from exc
        if not rows:
            self.page.add('<p style="color:#6e6e73">Empty.</p>')
            return
        grey = ' bgcolor="#f2f2f2"'
        head = "".join(f"<th{grey}>{_letters(n).upper()}</th>"
                       for n in range(1, widest + 1))
        self.page.add('<table border="1" cellspacing="0" cellpadding="3">'
                      f"<tr><th{grey}></th>{head}</tr>")
        for number, cells in rows:
            line = [f"<tr><th{grey}>{number}</th>"]
            for column in range(1, widest + 1):
                value, numeric = cells.get(column, ("", False))
                aligned = ' align="right"' if numeric else ""
                line.append(f"<td{aligned}>{value}</td>")
            line.append("</tr>")
            self.page.add("".join(line))
        self.page.add("</table>")
        if cut:
            self.package.notes.append(f"Only part of the sheet “{title}” "
                                      "is shown.")

    def _value(self, cell) -> Tuple[str, bool]:
        """A cell's value as shown, and whether it is a number."""
        kind = _get(cell, "t", "n")
        raw = _child(cell, "v")
        value = raw.text if raw is not None and raw.text else ""
        if kind == "s":
            try:
                return _text(self.strings[int(value)]), False
            except (ValueError, IndexError):
                return "", False
        if kind == "inlineStr":
            inline = _child(cell, "is")
            return _text("".join(found.text or "" for found in (
                inline.iter() if inline is not None else ())
                if _local(found.tag) == "t")), False
        if kind in ("str", "e", "d"):
            return _text(value), False
        if kind == "b":
            return ("TRUE" if value == "1" else "FALSE"), False
        if not value:
            return "", False
        try:
            number = float(value)
        except ValueError:
            return _text(value), False
        if number != number or number in (float("inf"), float("-inf")):
            return _text(value), False
        try:
            style = self.formats[int(_get(cell, "s", "0"))]
        except (ValueError, IndexError):
            style = ""
        return _text(self._number(number, style)), True

    def _number(self, number: float, style: str) -> str:
        """``number`` as its cell's format would show it, near enough: its
        places, thousands, percent, currency, or the date it stands for."""
        section = (style or "").split(";")[0]
        symbol = ""
        bracketed = re.search(r"\[\$([^\]\-]*)", section)
        if bracketed:
            symbol = bracketed.group(1)
        quoted = re.findall(r'"([^"]*)"', section)
        symbol = symbol or next((word for word in quoted if word in _CURRENCY), "")
        code = _DECORATION.sub("", section).lower().replace(" ", "")
        for mark in _CURRENCY:
            if mark in code:
                symbol = symbol or mark
                code = code.replace(mark, "")
        if style == "date" or (style != "time" and re.search(r"[dy]", code)):
            return self._when(number, with_time=bool(re.search(r"[hs]", code))
                              or number % 1 != 0)
        if style == "time" or re.search(r"[hs]", code):
            return self._when(number, with_date=False)
        sign = "-" if number < 0 else ""
        if code.endswith("%"):
            places = len(code.split(".")[1].rstrip("%")) if "." in code else 0
            return f"{number * 100:.{min(places, 10)}f}%"
        if "e+" in code:
            return f"{number:.2E}"
        if re.fullmatch(r"[#,0]*0(\.[0#]+)?", code):
            places = len(code.split(".")[1]) if "." in code else 0
            grouped = "," if "," in code else ""
            return f"{sign}{symbol}{abs(number):{grouped}.{min(places, 10)}f}"
        if number == int(number) and abs(number) < 1e15:
            return f"{sign}{symbol}{abs(int(number))}"
        return f"{sign}{symbol}{abs(number):.10g}"

    def _when(self, serial: float, with_time: bool = False,
              with_date: bool = True) -> str:
        if not -1 <= serial <= 2958465:          # Excel's own last day
            return f"{serial:.10g}"
        day = self.base
        if self.base.year == 1899 and serial < 60:
            day += timedelta(days=1)             # Excel's 1900 was a leap year
        moment = day + timedelta(days=serial)
        if not with_date:
            return moment.strftime("%H:%M:%S")
        return moment.strftime("%Y-%m-%d %H:%M" if with_time else "%Y-%m-%d")


# -- PowerPoint -------------------------------------------------------------

class _PowerPoint:
    def __init__(self, page: _Page) -> None:
        self.page = page
        self.package = page.package
        self.main = self.package.main("ppt/presentation.xml")
        self.links: Dict[str, Tuple[str, str, bool]] = {}

    def read(self) -> str:
        presentation = self.package.xml(self.main)
        order = self.package.relations(self.main)
        slides = []
        for slide in _children(_child(presentation, "sldIdLst"), "sldId"):
            target, kind, outside = order.get(_relation(slide), ("", "", True))
            if not outside and kind.endswith("/slide") and target in self.package.names:
                slides.append(target)
        for number, part in enumerate(slides, 1):
            if number > MAX_SLIDES or self.page.full:
                self.package.notes.append(
                    f"Only the first {number - 1} slides are shown.")
                break
            root = self.package.xml(part)
            self.links = self.package.relations(part)
            hidden = _get(root, "show") in ("0", "false")
            self.page.add(f'<h3 style="color:#6e6e73">Slide {number}'
                          f"{' (hidden)' if hidden else ''}</h3>")
            self._shapes(_child(_child(root, "cSld"), "spTree"), 0)
            self.page.add("<hr />")
        return f"{len(slides)} slide{'s' if len(slides) != 1 else ''}"

    def _shapes(self, tree, depth: int) -> None:
        if tree is None or depth > MAX_DEPTH:
            return
        for shape in tree:
            kind = _local(shape.tag)
            if kind == "sp":
                self._shape(shape)
            elif kind == "grpSp":
                self._shapes(shape, depth + 1)
            elif kind == "graphicFrame":
                table = next((found for found in shape.iter()
                              if _local(found.tag) == "tbl"), None)
                if table is not None:
                    self._table(table)
            elif kind == "pic":
                self._picture(shape)
            elif kind == "AlternateContent":
                self._shapes(_child(shape, "Choice"), depth + 1)

    def _shape(self, shape) -> None:
        placeholder = next((_get(found, "type") for found in shape.iter()
                            if _local(found.tag) == "ph"), None)
        title = placeholder in ("title", "ctrTitle")
        for paragraph in _children(_child(shape, "txBody"), "p"):
            words = self._words(paragraph)
            if not words.strip():
                continue
            if title:
                self.page.add(f"<h2>{words}</h2>")
                continue
            try:
                depth = max(0, min(8, int(_get(_child(paragraph, "pPr"), "lvl", "0"))))
            except ValueError:
                depth = 0
            self.page.add(f"<p>{'&nbsp;' * 4 * depth}{words}</p>")

    def _words(self, paragraph) -> str:
        out = []
        for piece in paragraph:
            kind = _local(piece.tag)
            if kind in ("r", "fld"):
                properties = _child(piece, "rPr")
                shown = _text("".join(found.text or "" for found in _children(piece, "t")))
                if _get(properties, "b") in ("1", "true"):
                    shown = f"<b>{shown}</b>"
                if _get(properties, "i") in ("1", "true"):
                    shown = f"<i>{shown}</i>"
                if _get(properties, "u", "none") not in ("none", ""):
                    shown = f"<u>{shown}</u>"
                out.append(shown)
            elif kind == "br":
                out.append("<br />")
        return "".join(out)

    def _table(self, table) -> None:
        self.page.add('<table border="1" cellspacing="0" cellpadding="4">')
        for row in _children(table, "tr"):
            self.page.add("<tr>")
            for cell in _children(row, "tc"):
                if _get(cell, "hMerge") in ("1", "true") or _get(cell, "vMerge") in ("1", "true"):
                    continue
                words = "<br />".join(self._words(paragraph) for paragraph in
                                      _children(_child(cell, "txBody"), "p"))
                self.page.add(f"<td>{words}</td>")
            self.page.add("</tr>")
        self.page.add("</table>")

    def _picture(self, shape) -> None:
        wide = tall = 0.0
        ident = ""
        for found in shape.iter():
            kind = _local(found.tag)
            if kind == "ext" and not wide:
                try:
                    wide = int(_get(found, "cx", "0")) / _EMU
                    tall = int(_get(found, "cy", "0")) / _EMU
                except ValueError:
                    pass
            elif kind == "blip":
                ident = _relation(found, "embed")
        target, _kind, outside = self.links.get(ident, ("", "", True))
        if not outside:
            picture = self.page.picture(target, wide, tall)
            if picture:
                self.page.add(f"<p>{picture}</p>")
