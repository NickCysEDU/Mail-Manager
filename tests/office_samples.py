"""Word, Excel and PowerPoint files made up for the tests, in the XML Office
writes; the compound files older and protected ones are kept in; Office's
two kinds of password protection; and a PDF locked with a password.

Made here rather than kept as files: the repository keeps no documents.
The protection is written from Microsoft's description of it (MS-OFFCRYPTO,
MS-CFB) and the PDF's from Adobe's; each was checked when written against
readers that are not this app's: a separate Office decrypter for the two
protections, and Qt's own PDF reader, which these tests use, for the PDF.
"""

from __future__ import annotations

import base64
import hashlib
import io
import struct
import zipfile
import zlib

REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
W = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
     f'xmlns:r="{REL}" '
     'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
     'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
     'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture" '
     'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
     'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"')
S = ('xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
     f'xmlns:r="{REL}"')
P = ('xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
     'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
     f'xmlns:r="{REL}"')
XML = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
TYPES = (XML + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
         '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
         '<Default Extension="xml" ContentType="application/xml"/></Types>')


def package(parts: dict) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as made:
        for name, data in parts.items():
            made.writestr(name, data)
    return out.getvalue()


def relations(*found) -> str:
    """Relationships, each (id, type, target, outside)."""
    items = []
    for ident, kind, target, outside in found:
        mode = ' TargetMode="External"' if outside else ""
        items.append(f'<Relationship Id="{ident}" Type="{REL}/{kind}" '
                     f'Target="{target}"{mode}/>')
    return (XML + '<Relationships xmlns="http://schemas.openxmlformats.org/'
            f'package/2006/relationships">{"".join(items)}</Relationships>')


def png(red: int, green: int, blue: int, wide: int = 8, tall: int = 8) -> bytes:
    """A small picture of one colour."""
    def chunk(tag: bytes, body: bytes) -> bytes:
        piece = tag + body
        return struct.pack(">I", len(body)) + piece + struct.pack(">I", zlib.crc32(piece))

    rows = b"".join(b"\x00" + bytes((red, green, blue)) * wide for _ in range(tall))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", wide, tall, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def docx(body: str, styles: str = "", numbering: str = "", links=(),
         media=None, macros: bool = False) -> bytes:
    """A Word document of ``body``, the XML inside <w:body>."""
    parts = {"[Content_Types].xml": TYPES,
             "_rels/.rels": relations(("rId1", "officeDocument", "word/document.xml", False)),
             "word/document.xml": f"{XML}<w:document {W}><w:body>{body}</w:body></w:document>"}
    own = list(links)
    if styles:
        parts["word/styles.xml"] = f"{XML}<w:styles {W}>{styles}</w:styles>"
        own.append(("rIdStyles", "styles", "styles.xml", False))
    if numbering:
        parts["word/numbering.xml"] = f"{XML}<w:numbering {W}>{numbering}</w:numbering>"
        own.append(("rIdNumbering", "numbering", "numbering.xml", False))
    parts["word/_rels/document.xml.rels"] = relations(*own)
    for name, data in (media or {}).items():
        parts[f"word/media/{name}"] = data
    if macros:
        parts["word/vbaProject.bin"] = b"\x00" * 32
    return package(parts)


def paragraph(text: str, style: str = "", runs: str = "") -> str:
    styled = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f'<w:p>{styled}{runs or f"<w:r><w:t>{text}</w:t></w:r>"}</w:p>'


HEADINGS = ('<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/></w:style>'
            '<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/></w:style>'
            '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/></w:style>')


def xlsx(sheets, strings=(), formats="", styles=(), date1904: bool = False) -> bytes:
    """An Excel workbook: ``sheets`` are (name, rows XML, state)."""
    workbook_rels = [("rIdStrings", "sharedStrings", "sharedStrings.xml", False),
                     ("rIdStyles", "styles", "styles.xml", False)]
    entries = []
    parts = {"[Content_Types].xml": TYPES,
             "_rels/.rels": relations(("rId1", "officeDocument", "xl/workbook.xml", False))}
    for number, (name, rows, state) in enumerate(sheets, 1):
        hidden = f' state="{state}"' if state else ""
        entries.append(f'<sheet name="{name}" sheetId="{number}"{hidden} r:id="rIdSheet{number}"/>')
        workbook_rels.append((f"rIdSheet{number}", "worksheet",
                              f"worksheets/sheet{number}.xml", False))
        parts[f"xl/worksheets/sheet{number}.xml"] = (
            f"{XML}<worksheet {S}><sheetData>{rows}</sheetData></worksheet>")
    epoch = '<workbookPr date1904="1"/>' if date1904 else ""
    parts["xl/workbook.xml"] = (f"{XML}<workbook {S}>{epoch}<sheets>{''.join(entries)}"
                                "</sheets></workbook>")
    parts["xl/_rels/workbook.xml.rels"] = relations(*workbook_rels)
    shared = "".join(f"<si><t>{text}</t></si>" for text in strings)
    parts["xl/sharedStrings.xml"] = f"{XML}<sst {S}>{shared}</sst>"
    cells = "".join(f'<xf numFmtId="{ident}"/>' for ident in ("0", *styles))
    parts["xl/styles.xml"] = (f"{XML}<styleSheet {S}><numFmts>{formats}</numFmts>"
                              f"<cellXfs>{cells}</cellXfs></styleSheet>")
    return package(parts)


def pptx(slides, order=None, media=None) -> bytes:
    """A PowerPoint presentation: ``slides`` are the XML inside each
    slide's shape tree, in the order the presentation lists them unless
    ``order`` says otherwise (the slides' file numbers, in show order)."""
    order = order or list(range(1, len(slides) + 1))
    parts = {"[Content_Types].xml": TYPES,
             "_rels/.rels": relations(("rId1", "officeDocument", "ppt/presentation.xml", False))}
    listed = "".join(f'<p:sldId id="{255 + at}" r:id="rIdSlide{number}"/>'
                     for at, number in enumerate(order, 1))
    parts["ppt/presentation.xml"] = f"{XML}<p:presentation {P}><p:sldIdLst>{listed}</p:sldIdLst></p:presentation>"
    parts["ppt/_rels/presentation.xml.rels"] = relations(*(
        (f"rIdSlide{number}", "slide", f"slides/slide{number}.xml", False)
        for number in range(1, len(slides) + 1)))
    for number, tree in enumerate(slides, 1):
        parts[f"ppt/slides/slide{number}.xml"] = (
            f"{XML}<p:sld {P}><p:cSld><p:spTree>{tree}</p:spTree></p:cSld></p:sld>")
        parts[f"ppt/slides/_rels/slide{number}.xml.rels"] = relations(
            ("rIdPicture", "image", "../media/picture.png", False))
    for name, data in (media or {}).items():
        parts[f"ppt/media/{name}"] = data
    return package(parts)


def text_shape(text: str, title: bool = False, level: int = 0) -> str:
    placeholder = '<p:nvSpPr><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>' if title else ""
    depth = f' lvl="{level}"' if level else ""
    return (f"<p:sp>{placeholder}<p:txBody><a:p><a:pPr{depth}/><a:r><a:t>{text}</a:t>"
            "</a:r></a:p></p:txBody></p:sp>")


# -- Compound files -----------------------------------------------------------

_FREE, _END, _FAT = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD


def compound(streams: dict) -> bytes:
    """A Compound File (MS-CFB, version 3) holding ``streams`` at its root:
    512-byte sectors, and the streams under 4,096 bytes in the mini stream,
    as the format requires."""
    sector, mini, cutoff = 512, 64, 4096
    sectors, fat = [], []

    def place(data: bytes) -> int:
        count = max(1, -(-len(data) // sector))
        start = len(sectors)
        for at in range(count):
            sectors.append(data[at * sector:(at + 1) * sector].ljust(sector, b"\0"))
        fat.extend([start + at + 1 for at in range(count - 1)] + [_END])
        return start

    small_stream, mini_fat, starts = bytearray(), [], {}
    for name, data in streams.items():
        if len(data) < cutoff:
            count = max(1, -(-len(data) // mini))
            start = len(small_stream) // mini
            small_stream += data.ljust(count * mini, b"\0")
            mini_fat.extend([start + at + 1 for at in range(count - 1)] + [_END])
            starts[name] = start
    for name, data in streams.items():
        if len(data) >= cutoff:
            starts[name] = place(data)
    root_start = place(bytes(small_stream)) if small_stream else _END
    mini_table = struct.pack(f"<{len(mini_fat)}I", *mini_fat)
    mini_table += b"\xff" * (-len(mini_table) % sector)
    mini_fat_start = place(mini_table) if mini_fat else _END
    mini_sectors = len(mini_table) // sector
    entries = [("Root Entry", 5, root_start, len(small_stream))] + [
        (name, 2, starts[name], len(data)) for name, data in streams.items()]
    directory = bytearray()
    for index, (name, kind, start, size) in enumerate(entries):
        encoded = name.encode("utf-16-le") + b"\0\0"
        right = index + 1 if 1 <= index < len(entries) - 1 else _FREE
        child = 1 if index == 0 and len(entries) > 1 else _FREE
        directory += (encoded.ljust(64, b"\0")
                      + struct.pack("<HBBIII", len(encoded), kind, 1, _FREE, right, child)
                      + b"\0" * 36 + struct.pack("<IQ", start, size))
    while len(directory) % sector:
        directory += (b"\0" * 64 + struct.pack("<HBBIII", 0, 0, 0, _FREE, _FREE, _FREE)
                      + b"\0" * 36 + struct.pack("<IQ", 0, 0))
    directory_start = place(bytes(directory))
    tables = 1
    while len(sectors) + tables > 128 * tables:
        tables += 1
    first_table = len(sectors)
    fat.extend([_FAT] * tables)
    fat.extend([_FREE] * (128 * tables - len(fat)))
    for at in range(tables):
        sectors.append(struct.pack("<128I", *fat[at * 128:(at + 1) * 128]))
    difat = [first_table + at for at in range(tables)] + [_FREE] * (109 - tables)
    header = (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 16
              + struct.pack("<HHHHH", 0x3E, 3, 0xFFFE, 9, 6) + b"\0" * 6
              + struct.pack("<IIIIIIIII", 0, tables, directory_start, 0, cutoff,
                            mini_fat_start, mini_sectors, _END, 0)
              + struct.pack("<109I", *difat))
    return header + b"".join(sectors)


# -- Office's password protection (MS-OFFCRYPTO) ---------------------------------

def _aes(key: bytes, data: bytes, iv=None) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    worker = Cipher(algorithms.AES(key), modes.CBC(iv) if iv else modes.ECB()).encryptor()
    return worker.update(data) + worker.finalize()


def _padded(data: bytes, block: int = 16) -> bytes:
    return data + b"\0" * (-len(data) % block)


def sealed_agile(document: bytes, password: str, spins: int = 1000) -> bytes:
    """``document`` protected with ``password`` as Office has since 2010:
    AES-256 and SHA-512, the key hashed ``spins`` times (Office uses
    100,000; fewer keeps the tests quick and changes nothing else)."""
    salt = hashlib.sha256(b"key salt").digest()[:16]
    data_salt = hashlib.sha256(b"data salt").digest()[:16]
    secret = hashlib.sha256(b"the document's key").digest()
    verifier = hashlib.sha256(b"verifier").digest()[:16]
    digest = hashlib.sha512(salt + password.encode("utf-16-le")).digest()
    for spin in range(spins):
        digest = hashlib.sha512(struct.pack("<I", spin) + digest).digest()

    def key(block: bytes) -> bytes:
        return hashlib.sha512(digest + block).digest()[:32]

    input_value = _aes(key(bytes.fromhex("fea7d2763b4b9e79")), verifier, salt)
    hash_value = _aes(key(bytes.fromhex("d7aa0f6d3061344e")),
                      hashlib.sha512(verifier).digest(), salt)
    key_value = _aes(key(bytes.fromhex("146e0be7abacd0d6")), secret, salt)
    body = bytearray(struct.pack("<Q", len(document)))
    for index, start in enumerate(range(0, len(document), 4096)):
        iv = hashlib.sha512(data_salt + struct.pack("<I", index)).digest()[:16]
        body += _aes(secret, _padded(document[start:start + 4096]), iv)

    def b64(value: bytes) -> str:
        return base64.b64encode(value).decode("ascii")

    common = ('blockSize="16" keyBits="256" hashSize="64" cipherAlgorithm="AES" '
              'cipherChaining="ChainingModeCBC" hashAlgorithm="SHA512"')
    xml = (XML + '<encryption xmlns="http://schemas.microsoft.com/office/2006/encryption" '
           'xmlns:p="http://schemas.microsoft.com/office/2006/keyEncryptor/password">'
           f'<keyData saltSize="16" {common} saltValue="{b64(data_salt)}"/>'
           f'<dataIntegrity encryptedHmacKey="{b64(bytes(64))}" encryptedHmacValue="{b64(bytes(64))}"/>'
           '<keyEncryptors><keyEncryptor uri="http://schemas.microsoft.com/office/2006/keyEncryptor/password">'
           f'<p:encryptedKey spinCount="{spins}" saltSize="16" {common} saltValue="{b64(salt)}" '
           f'encryptedVerifierHashInput="{b64(input_value)}" '
           f'encryptedVerifierHashValue="{b64(hash_value)}" '
           f'encryptedKeyValue="{b64(key_value)}"/></keyEncryptor></keyEncryptors></encryption>')
    info = struct.pack("<HHI", 4, 4, 0x40) + xml.encode("utf-8")
    return compound({"EncryptionInfo": info, "EncryptedPackage": bytes(body)})


def sealed_standard(document: bytes, password: str) -> bytes:
    """``document`` protected with ``password`` as Office 2007 did: AES-128
    and SHA-1, the key hashed 50,000 times."""
    salt = hashlib.sha256(b"standard salt").digest()[:16]
    digest = hashlib.sha1(salt + password.encode("utf-16-le")).digest()
    for spin in range(50000):
        digest = hashlib.sha1(struct.pack("<I", spin) + digest).digest()
    digest = hashlib.sha1(digest + struct.pack("<I", 0)).digest()
    inner = hashlib.sha1(bytes(b ^ 0x36 for b in digest) + b"\x36" * 44).digest()
    outer = hashlib.sha1(bytes(b ^ 0x5C for b in digest) + b"\x5c" * 44).digest()
    key = (inner + outer)[:16]
    verifier = hashlib.sha256(b"standard verifier").digest()[:16]
    provider = "Microsoft Enhanced RSA and AES Cryptographic Provider\0".encode("utf-16-le")
    header = struct.pack("<IIIIIIII", 0x24, 0, 0x660E, 0x8004, 128, 0x18, 0, 0) + provider
    info = (struct.pack("<HHII", 3, 2, 0x24, len(header)) + header
            + struct.pack("<I", 16) + salt + _aes(key, verifier)
            + struct.pack("<I", 20) + _aes(key, _padded(hashlib.sha1(verifier).digest(), 32)))
    body = struct.pack("<Q", len(document)) + _aes(key, _padded(document))
    return compound({"EncryptionInfo": info, "EncryptedPackage": body})


# -- A PDF locked with a password ------------------------------------------------

_PDF_PAD = bytes.fromhex("28BF4E5E4E758A4164004E56FFFA01082E2E00B6D0683E802F0CA9FE6453697A")


def _rc4(key: bytes, data: bytes) -> bytes:
    state = list(range(256))
    j = 0
    for i in range(256):
        j = (j + state[i] + key[i % len(key)]) % 256
        state[i], state[j] = state[j], state[i]
    out = bytearray()
    i = j = 0
    for byte in data:
        i = (i + 1) % 256
        j = (j + state[i]) % 256
        state[i], state[j] = state[j], state[i]
        out.append(byte ^ state[(state[i] + state[j]) % 256])
    return bytes(out)


def locked_pdf(password: str, words: str = "Open sesame") -> bytes:
    """A one-page PDF that opens only with ``password``: the standard
    security handler, revision 2, as the PDF reference describes it."""
    user = (password.encode("latin-1") + _PDF_PAD)[:32]
    owner = _rc4(hashlib.md5(user).digest()[:5], user)
    file_id = hashlib.md5(b"a locked test page").digest()
    allowed = -44
    key = hashlib.md5(user + owner + allowed.to_bytes(4, "little", signed=True)
                      + file_id).digest()[:5]
    check = _rc4(key, _PDF_PAD)
    content = f"BT /F1 24 Tf 72 700 Td ({words}) Tj ET".encode("latin-1")
    stream = _rc4(hashlib.md5(key + (4).to_bytes(3, "little") + b"\0\0").digest()[:10],
                  content)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Filter /Standard /V 1 /R 2 /O <" + owner.hex().encode() + b"> /U <"
        + check.hex().encode() + b"> /P " + str(allowed).encode() + b" >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    table = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{at:010d} 00000 n \n".encode() for at in offsets)
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Encrypt 6 0 R "
            f"/ID [<{file_id.hex()}><{file_id.hex()}>] >>\nstartxref\n{table}\n%%EOF\n").encode()
    return bytes(out)
