"""Word, Excel and PowerPoint attachments: what one is, opening one that is
protected with a password, what is shown of each, the bounds on what a
sender can make reading one do, and the viewer showing them.

Only a PDF could be opened in the attachment viewer: a Word file was "an
archive", and one protected with a password "not a format the viewer
opens", which left every protected document unreadable here."""

from __future__ import annotations

import random
import re
import struct
import time

import pytest

import office
import office_samples as samples

WORDS = "Seedlings by the gate"


def _word(body: str, **extra) -> bytes:
    return samples.docx(body, **extra)


class TestWhatAnOfficeFileIs:
    def test_each_application_by_its_bytes(self):
        sheet = samples.xlsx([("Plots", "", "")])
        slides = samples.pptx([samples.text_shape(WORDS)])
        assert office.identify(_word(samples.paragraph(WORDS))) == office.Found(
            office.WORD, office.OPEN)
        assert office.identify(sheet) == office.Found(office.EXCEL, office.OPEN)
        assert office.identify(slides) == office.Found(office.POWERPOINT, office.OPEN)

    def test_a_zip_is_not_a_document(self):
        assert office.identify(samples.package({"notes.txt": "hello"})) is None
        assert office.identify(samples.package({"[Content_Types].xml": samples.TYPES})) is None
        assert office.identify(b"PK\x03\x04" + b"\x00" * 60) is None

    def test_macros_are_noticed(self):
        assert office.identify(_word(samples.paragraph(WORDS), macros=True)).macros
        assert not office.identify(_word(samples.paragraph(WORDS))).macros

    def test_a_protected_one_is_named_by_its_file(self):
        sealed = samples.sealed_agile(samples.xlsx([("Plots", "", "")]), "tulip")
        assert office.identify(sealed, "plots.xlsx") == office.Found(
            office.EXCEL, office.PASSWORD)
        named = office.identify(sealed, "plots.bin",
                                "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        assert named.kind == office.WORD
        unnamed = office.identify(sealed)
        assert unnamed.state == office.PASSWORD and unnamed.name == "Office document"

    def test_rights_management_and_the_older_formats(self):
        rights = samples.compound({"EncryptedPackage": b"x" * 5000,
                                   "DRMEncryptedTransform": b"y" * 100})
        assert office.identify(rights, "plan.docx") == office.Found(office.WORD, office.RIGHTS)
        # No key of its own: only the rights service holds it.
        keyless = samples.compound({"EncryptedPackage": b"x" * 5000})
        assert office.identify(keyless, "plan.pptx").state == office.RIGHTS
        for stream, kind in (("WordDocument", office.WORD), ("Workbook", office.EXCEL),
                             ("PowerPoint Document", office.POWERPOINT)):
            assert office.identify(samples.compound({stream: b"z" * 5000})) == (
                office.Found(kind, office.OLDER))

    def test_a_message_microsoft_sealed(self):
        assert office.identify(b"\x76\xe8\x04\x60" + b"\x00" * 40, "message.rpmsg").state == (
            office.SEALED)

    def test_damaged_bytes_are_nothing_in_particular(self):
        assert office.identify(office.CFB_MAGIC + b"\x00" * 600) is None
        whole = _word(samples.paragraph(WORDS))
        assert office.identify(whole[:len(whole) // 2]) is None

    def test_a_zip_of_too_many_entries_is_not_read(self, monkeypatch):
        monkeypatch.setattr(office, "MAX_PARTS", 3)
        assert office.identify(_word(samples.paragraph(WORDS))) is None

    def test_what_each_one_says_it_cannot_do(self):
        rights = office.explain(office.Found(office.EXCEL, office.RIGHTS))
        assert "This Excel workbook is protected by Microsoft's rights management" in rights
        assert "Outlook" in office.explain(office.Found("", office.SEALED))
        older = office.explain(office.Found(office.POWERPOINT, office.OLDER))
        assert older.startswith("An older PowerPoint file") and "Keynote" in older


class TestOpeningAProtectedDocument:
    ORIGINAL = _word(samples.paragraph(WORDS))

    @pytest.mark.parametrize("seal", [samples.sealed_agile, samples.sealed_standard],
                             ids=["since 2010", "Office 2007"])
    def test_it_opens_with_its_password_and_no_other(self, seal):
        sealed = seal(self.ORIGINAL, "tulip")
        assert office.identify(sealed, "plan.docx").state == office.PASSWORD
        assert office.decrypt(sealed, "tulip") == self.ORIGINAL
        assert WORDS in office.read(office.decrypt(sealed, "tulip")).html
        with pytest.raises(office.WrongPassword):
            office.decrypt(sealed, "Tulip")

    def test_any_characters_make_a_password(self):
        password = "Grüße 🌷 ünd ✓"
        sealed = samples.sealed_agile(self.ORIGINAL, password)
        assert office.decrypt(sealed, password) == self.ORIGINAL

    def test_a_protection_it_does_not_know_is_said_so(self):
        sealed = bytearray(samples.sealed_agile(self.ORIGINAL, "tulip"))
        at = bytes(sealed).index(struct.pack("<HHI", 4, 4, 0x40))
        sealed[at:at + 4] = struct.pack("<HH", 4, 3)        # extensible
        with pytest.raises(office.CannotRead) as said:
            office.decrypt(bytes(sealed), "tulip")
        assert not isinstance(said.value, office.WrongPassword)

    def test_a_certificate_is_not_a_password(self):
        sealed = samples.sealed_agile(self.ORIGINAL, "tulip")
        swapped = sealed.replace(b"keyEncryptor/password", b"keyEncryptor/certific")
        with pytest.raises(office.CannotRead, match="certificate"):
            office.decrypt(swapped, "tulip")

    def test_an_endless_hashing_is_refused_rather_than_done(self):
        sealed = samples.sealed_agile(self.ORIGINAL, "tulip", spins=1000)
        info = _stream(sealed, "EncryptionInfo")
        package = _stream(sealed, "EncryptedPackage")
        rebuilt = samples.compound({"EncryptionInfo": info, "EncryptedPackage": package})
        assert office.decrypt(rebuilt, "tulip") == self.ORIGINAL, "taken apart, it still opens"
        swollen = info.replace(b'spinCount="1000"', b'spinCount="9999999999"')
        assert swollen != info
        started = time.perf_counter()
        with pytest.raises(office.CannotRead, match="damaged"):
            office.decrypt(samples.compound({"EncryptionInfo": swollen,
                                             "EncryptedPackage": package}), "tulip")
        assert time.perf_counter() - started < 1.0


def _stream(made: bytes, name: str) -> bytes:
    return office._Compound(made).stream(name, 1 << 30)


class TestAWordDocument:
    def _shown(self, body: str, **extra) -> office.Shown:
        return office.read(_word(body, styles=samples.HEADINGS, **extra))

    def test_headings_and_the_words_on_them(self):
        shown = self._shown(samples.paragraph("The allotment", "Title")
                            + samples.paragraph("Autumn", "Heading2")
                            + samples.paragraph(WORDS))
        assert "<h1>The allotment</h1>" in shown.html
        assert "<h2>Autumn</h2>" in shown.html
        assert f"<p>{WORDS}</p>" in shown.html
        assert shown.summary == "7 words"

    def test_bold_italic_underline_and_the_rest(self):
        runs = ("<w:r><w:rPr><w:b/></w:rPr><w:t>firm</w:t></w:r>"
                "<w:r><w:rPr><w:i/></w:rPr><w:t>leaning</w:t></w:r>"
                '<w:r><w:rPr><w:u w:val="single"/></w:rPr><w:t>marked</w:t></w:r>'
                '<w:r><w:rPr><w:u w:val="none"/></w:rPr><w:t>plain</w:t></w:r>'
                "<w:r><w:rPr><w:strike/></w:rPr><w:t>struck</w:t></w:r>"
                '<w:r><w:rPr><w:b w:val="0"/></w:rPr><w:t>unbold</w:t></w:r>'
                '<w:r><w:rPr><w:vertAlign w:val="superscript"/></w:rPr><w:t>2</w:t></w:r>')
        html = self._shown(samples.paragraph("", runs=runs)).html
        for expected in ("<b>firm</b>", "<i>leaning</i>", "<u>marked</u>",
                         "<s>struck</s>", "<sup>2</sup>"):
            assert expected in html
        assert "<u>plain</u>" not in html and "<b>unbold</b>" not in html

    def test_colour_and_highlight(self):
        runs = ('<w:r><w:rPr><w:color w:val="C00000"/></w:rPr><w:t>warm</w:t></w:r>'
                '<w:r><w:rPr><w:highlight w:val="yellow"/></w:rPr><w:t>noted</w:t></w:r>'
                '<w:r><w:rPr><w:color w:val="FFFFFF"/></w:rPr><w:t>ghost</w:t></w:r>')
        html = self._shown(samples.paragraph("", runs=runs)).html
        assert '<span style="color:#c00000">warm</span>' in html
        assert "background-color:#ffff00" in html
        assert "#ffffff" not in html, "white words would vanish on the white page"
        assert "ghost" in html

    def test_pale_words_on_their_own_shading_keep_their_colour(self):
        cell = ('<w:tc><w:tcPr><w:shd w:val="clear" w:fill="1F3864"/></w:tcPr>'
                + samples.paragraph("", runs='<w:r><w:rPr><w:color w:val="FFFFFF"/>'
                                    "</w:rPr><w:t>Plot</w:t></w:r>") + "</w:tc>")
        html = self._shown(f"<w:tbl><w:tr>{cell}</w:tr></w:tbl>").html
        assert 'bgcolor="#1f3864"' in html
        assert "color:#ffffff" in html

    def test_lists_count_as_word_does(self):
        numbering = (
            '<w:abstractNum w:abstractNumId="0">'
            '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl>'
            '<w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="lowerLetter"/><w:lvlText w:val="%1.%2"/></w:lvl>'
            '</w:abstractNum>'
            '<w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0"><w:numFmt w:val="bullet"/>'
            '<w:lvlText w:val="•"/></w:lvl></w:abstractNum>'
            '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
            '<w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>')

        def item(text, number, level=0):
            return (f'<w:p><w:pPr><w:numPr><w:ilvl w:val="{level}"/><w:numId w:val="{number}"/>'
                    f"</w:numPr></w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>")

        html = self._shown(item("Dig", 1) + item("Rake", 1, 1) + item("Sow", 1, 1)
                           + item("Water", 1) + item("Spare gloves", 2),
                           numbering=numbering).html
        lines = re.findall(r"<p>(.*?)</p>", html)
        assert lines[0].endswith("1.&nbsp;Dig")
        assert lines[1].endswith("1.a&nbsp;Rake") and lines[1].startswith("&nbsp;" * 4)
        assert lines[2].endswith("1.b&nbsp;Sow")
        assert lines[3].endswith("2.&nbsp;Water"), "a deeper level ends and the count goes on"
        assert "•&nbsp;Spare gloves" in lines[4]

    def test_tables_keep_their_rows_and_spans(self):
        def cell(text, properties=""):
            return f"<w:tc><w:tcPr>{properties}</w:tcPr>{samples.paragraph(text)}</w:tc>"

        wide = '<w:gridSpan w:val="2"/>'
        starts = '<w:vMerge w:val="restart"/>'
        goes_on = "<w:vMerge/>"
        table = ("<w:tbl>"
                 f"<w:tr>{cell('Plot', wide)}</w:tr>"
                 f"<w:tr>{cell('Nine', starts)}{cell('Beans')}</w:tr>"
                 f"<w:tr>{cell('Nine again', goes_on)}{cell('Leeks')}</w:tr>"
                 "</w:tbl>")
        html = self._shown(table).html
        assert '<td colspan="2"><p>Plot</p></td>' in html
        assert "<p>Nine</p>" in html and "Nine again" not in html, "a merged cell is shown once"
        assert html.count("<tr>") == 3 and "<p>Leeks</p>" in html

    def test_a_link_says_where_it_goes_and_cannot_be_followed(self):
        body = ('<w:p><w:hyperlink r:id="rIdLink"><w:r><w:t>the plot map</w:t></w:r>'
                "</w:hyperlink></w:p>")
        html = self._shown(body, links=[("rIdLink", "hyperlink",
                                         "https://allotments.example/map", True)]).html
        assert "the plot map" in html and "(https://allotments.example/map)" in html
        assert "<a " not in html and "href" not in html

    def test_pictures_come_from_inside_and_nothing_else(self):
        drawing = ('<w:r><w:drawing><wp:inline><wp:extent cx="1905000" cy="952500"/>'
                   '<a:graphic><a:graphicData><pic:pic><pic:blipFill><a:blip r:embed="{ident}"/>'
                   "</pic:blipFill></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r>")
        body = "".join(samples.paragraph("", runs=drawing.format(ident=ident))
                       for ident in ("rIdPicture", "rIdMetafile", "rIdOutside"))
        shown = self._shown(body, links=[
            ("rIdPicture", "image", "media/leaf.png", False),
            ("rIdMetafile", "image", "media/logo.emf", False),
            ("rIdOutside", "image", "https://tracker.example/pixel.png", True)],
            media={"leaf.png": samples.png(40, 160, 60), "logo.emf": b"\x01\x00\x00\x00" * 20})
        assert list(shown.pictures) == ["mm-picture:1"]
        assert shown.pictures["mm-picture:1"] == samples.png(40, 160, 60)
        assert '<img src="mm-picture:1" width="200" height="100" />' in shown.html
        assert "tracker.example" not in shown.html
        assert any("format the viewer does not draw" in note for note in shown.notes)

    def test_a_text_box_is_read_after_its_paragraph(self):
        box = ('<w:r><mc:AlternateContent><mc:Choice Requires="wps"><w:drawing><wp:anchor>'
               "<a:graphic><a:graphicData><wps:wsp><wps:txbx><w:txbxContent>"
               f"{samples.paragraph('In the box')}</w:txbxContent></wps:txbx></wps:wsp>"
               "</a:graphicData></a:graphic></wp:anchor></w:drawing></mc:Choice>"
               f"<mc:Fallback>{samples.paragraph('Fallback copy')}</mc:Fallback>"
               "</mc:AlternateContent></w:r>")
        html = self._shown(samples.paragraph("", runs="<w:r><w:t>Before</w:t></w:r>" + box)).html
        assert html.index("Before") < html.index("In the box")
        assert "Fallback copy" not in html, "the same box twice"

    def test_hidden_deleted_and_field_words_are_not_shown(self):
        runs = ("<w:r><w:rPr><w:vanish/></w:rPr><w:t>hidden</w:t></w:r>"
                "<w:del><w:r><w:delText>deleted</w:delText></w:r></w:del>"
                "<w:ins><w:r><w:t>inserted</w:t></w:r></w:ins>"
                "<w:r><w:instrText>HYPERLINK secret</w:instrText></w:r>")
        html = self._shown(samples.paragraph("", runs=runs)).html
        assert "inserted" in html
        assert not any(word in html for word in ("hidden", "deleted", "HYPERLINK"))

    def test_its_words_are_words_not_markup(self):
        html = self._shown(samples.paragraph("&lt;b&gt;bold?&lt;/b&gt; &amp; &lt;img src=x&gt;")).html
        assert "&lt;b&gt;bold?&lt;/b&gt; &amp; &lt;img src=x&gt;" in html
        assert "<img" not in html


class TestAnExcelWorkbook:
    STRINGS = ("Plot", "Holder")

    def _shown(self, rows: str, **extra) -> office.Shown:
        extra.setdefault("strings", self.STRINGS)
        return office.read(samples.xlsx([("Plots", rows, "")], **extra))

    @staticmethod
    def _cells(html: str):
        return re.findall(r"<td(?: align=\"right\")?>(.*?)</td>", html)

    def test_text_numbers_and_the_grid(self):
        rows = ('<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
                '<row r="3"><c r="A3"><v>9</v></c><c r="B3" t="inlineStr"><is><t>Committee</t></is></c>'
                '<c r="C3" t="b"><v>1</v></c><c r="D3" t="e"><v>#DIV/0!</v></c></row>')
        shown = self._shown(rows)
        assert "<h3>Plots</h3>" in shown.html and shown.summary == "1 sheet"
        assert self._cells(shown.html) == ["Plot", "Holder", "", "", "9", "Committee",
                                           "TRUE", "#DIV/0!"]
        assert '<th bgcolor="#f2f2f2">3</th>' in shown.html, "the row's own number"
        assert '<td align="right">9</td>' in shown.html

    def test_number_formats(self):
        formats = ('<numFmt numFmtId="164" formatCode="&quot;$&quot;#,##0.00"/>'
                   '<numFmt numFmtId="165" formatCode="[$€-2] #,##0"/>'
                   '<numFmt numFmtId="166" formatCode="dd/mm/yyyy"/>')
        styles = ("4", "10", "164", "165", "166", "14", "22", "20")
        values = ("1234.5", "0.125", "-99.5", "2750", "46308", "46308", "46308.75", "0.75")
        cells = "".join(f'<c r="{chr(65 + at)}1" s="{at + 1}"><v>{value}</v></c>'
                        for at, value in enumerate(values))
        shown = self._shown(f'<row r="1">{cells}</row>', formats=formats, styles=styles)
        assert self._cells(shown.html) == [
            "1,234.50", "12.50%", "-$99.50", "€2,750", "2026-10-13", "2026-10-13",
            "2026-10-13 18:00", "18:00:00"]

    def test_the_1904_calendar(self):
        shown = office.read(samples.xlsx([("Plots", '<row r="1"><c r="A1" s="1"><v>44846</v></c></row>', "")],
                                         styles=("14",), date1904=True))
        assert self._cells(shown.html) == ["2026-10-13"]

    def test_a_hidden_sheet_is_left_out_and_said_to_be(self):
        book = samples.xlsx([("Shown", '<row r="1"><c r="A1"><v>1</v></c></row>', ""),
                             ("Secret", '<row r="1"><c r="A1"><v>2</v></c></row>', "hidden")])
        shown = office.read(book)
        assert "Secret" not in shown.html and shown.summary == "1 sheet"
        assert "1 hidden sheet is not shown." in shown.notes

    def test_a_long_sheet_is_shown_up_to_its_bound(self, monkeypatch):
        monkeypatch.setattr(office, "MAX_ROWS", 3)
        rows = "".join(f'<row r="{n}"><c r="A{n}"><v>{n}</v></c></row>' for n in range(1, 10))
        shown = self._shown(rows)
        assert self._cells(shown.html) == ["1", "2", "3"]
        assert "Only part of the sheet “Plots” is shown." in shown.notes

    def test_a_far_column_is_not_drawn(self):
        shown = self._shown('<row r="1"><c r="A1"><v>1</v></c><c r="ZZZ1"><v>2</v></c></row>')
        assert self._cells(shown.html) == ["1"]
        assert any("Only part" in note for note in shown.notes)


class TestAPowerPointPresentation:
    def test_slides_in_their_order_with_their_titles(self):
        first = samples.text_shape("Planting plan", title=True) + samples.text_shape("Beans")
        second = samples.text_shape("Watering", title=True) + samples.text_shape("Mornings", level=1)
        shown = office.read(samples.pptx([first, second], order=[2, 1]))
        assert shown.summary == "2 slides"
        html = shown.html
        assert html.index("Watering") < html.index("Planting plan"), "the list's order"
        assert "<h2>Planting plan</h2>" in html and "<p>Beans</p>" in html
        assert "<p>&nbsp;&nbsp;&nbsp;&nbsp;Mornings</p>" in html

    def test_a_table_and_a_picture(self):
        table = ('<p:graphicFrame><a:graphic><a:graphicData><a:tbl><a:tr>'
                 '<a:tc><a:txBody><a:p><a:r><a:t>Bed</a:t></a:r></a:p></a:txBody></a:tc>'
                 '<a:tc><a:txBody><a:p><a:r><a:t>Crop</a:t></a:r></a:p></a:txBody></a:tc>'
                 "</a:tr></a:tbl></a:graphicData></a:graphic></p:graphicFrame>")
        picture = ('<p:pic><p:blipFill><a:blip r:embed="rIdPicture"/></p:blipFill>'
                   '<p:spPr><a:xfrm><a:ext cx="952500" cy="952500"/></a:xfrm></p:spPr></p:pic>')
        shown = office.read(samples.pptx([table + picture],
                                         media={"picture.png": samples.png(200, 30, 30)}))
        assert "<td>Bed</td><td>Crop</td>" in shown.html
        assert '<img src="mm-picture:1" width="100" height="100" />' in shown.html


class TestWhatASenderCannotMakeItDo:
    def test_markup_office_never_writes_is_refused(self):
        laughs = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
                  '<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]>')
        made = samples.package({
            "[Content_Types].xml": samples.TYPES,
            "_rels/.rels": samples.relations(("rId1", "officeDocument", "word/document.xml", False)),
            "word/document.xml": laughs + f"<w:document {samples.W}><w:body>"
                                 "<w:p><w:r><w:t>&lol2;</w:t></w:r></w:p></w:body></w:document>"})
        started = time.perf_counter()
        with pytest.raises(office.CannotRead, match="markup"):
            office.read(made)
        assert time.perf_counter() - started < 1.0

    def test_a_part_that_swells_is_refused(self, monkeypatch):
        made = _word(samples.paragraph("x" * 50_000))
        monkeypatch.setattr(office, "MAX_PART", 10_000)
        with pytest.raises(office.CannotRead, match="too large"):
            office.read(made)

    def test_all_the_parts_together_are_bounded_too(self, monkeypatch):
        made = _word(samples.paragraph("x" * 20_000), styles=samples.HEADINGS + " " * 20_000)
        monkeypatch.setattr(office, "MAX_TOTAL", 30_000)
        with pytest.raises(office.CannotRead, match="too large"):
            office.read(made)

    def test_tables_nested_past_the_bound_stop_there(self):
        body = samples.paragraph("Top")
        for _ in range(office.MAX_DEPTH + 30):
            body = f"<w:tbl><w:tr><w:tc>{body}</w:tc></w:tr></w:tbl>"
        shown = office.read(_word(body))
        assert "Top" not in shown.html, "nothing past the bound is read"
        assert shown.html.count("<table") <= office.MAX_DEPTH + 1

    def test_a_relationship_cannot_leave_the_document(self):
        assert office._resolve("word", "../../../../etc/passwd") == "etc/passwd"
        assert office._resolve("word", "/word/media/a.png") == "word/media/a.png"
        assert office._resolve("ppt/slides", "../media/b.png") == "ppt/media/b.png"

    def test_pictures_are_bounded(self, monkeypatch):
        monkeypatch.setattr(office, "MAX_PICTURES", 2)
        drawing = ('<w:r><w:drawing><a:blip r:embed="rIdPicture"/></w:drawing></w:r>')
        shown = office.read(_word(samples.paragraph("", runs=drawing * 5), links=[
            ("rIdPicture", "image", "media/leaf.png", False)],
            media={"leaf.png": samples.png(1, 2, 3)}))
        assert len(shown.pictures) == 2
        assert "3 more pictures are not shown." in shown.notes

    def test_a_long_document_is_cut_short_and_said_to_be(self, monkeypatch):
        monkeypatch.setattr(office, "MAX_HTML", 2000)
        shown = office.read(_word(samples.paragraph(WORDS) * 400))
        assert len(shown.html) < 2400
        assert "It is long, and only its beginning is shown." in shown.notes

    @staticmethod
    def _loads_nothing(html: str) -> bool:
        sources = re.findall(r"""\b(?:src|href|background|data)\s*=\s*["']?([^"' >]+)""", html)
        return (all(source.startswith("mm-picture:") for source in sources)
                and not re.search(r"<(?:script|link|iframe|object|embed|style)\b", html))

    def test_the_page_names_nothing_it_would_load(self):
        drawing = ('<w:r><w:drawing><a:blip r:embed="rIdPicture"/></w:drawing></w:r>')
        body = (samples.paragraph("", runs=drawing)
                + '<w:p><w:hyperlink r:id="rIdLink"><w:r><w:t>map</w:t></w:r></w:hyperlink></w:p>')
        shown = office.read(_word(body, links=[
            ("rIdPicture", "image", "media/leaf.png", False),
            ("rIdLink", "hyperlink", "javascript:alert(1)", True)],
            media={"leaf.png": samples.png(1, 2, 3)}))
        assert self._loads_nothing(shown.html)

    @pytest.mark.parametrize("kind", ["word", "excel", "powerpoint", "sealed"])
    def test_damaged_files_raise_only_what_the_viewer_expects(self, kind):
        """A few hundred files with bytes changed at random: each is a
        document, nothing at all, or a reason it cannot be read, and none
        takes long to say which."""
        sources = {
            "word": _word(samples.paragraph(WORDS, "Heading1") + "<w:tbl><w:tr><w:tc>"
                          + samples.paragraph("cell") + "</w:tc></w:tr></w:tbl>",
                          styles=samples.HEADINGS),
            "excel": samples.xlsx([("Plots", '<row r="1"><c r="A1" t="s"><v>0</v></c>'
                                    '<c r="B1" s="1"><v>46309</v></c></row>', "")],
                                  strings=("Plot",), styles=("14",)),
            "powerpoint": samples.pptx([samples.text_shape("Plan", title=True)]),
            "sealed": samples.sealed_agile(_word(samples.paragraph(WORDS)), "tulip", spins=10),
        }
        original = sources[kind]
        chance = random.Random(f"office {kind}")
        for _ in range(250):
            damaged = bytearray(original)
            for _flip in range(chance.randint(1, 12)):
                at = chance.randrange(len(damaged))
                damaged[at] = chance.randrange(256)
            data = bytes(damaged)
            started = time.perf_counter()
            found = office.identify(data, "plan.docx")
            try:
                if found is not None and found.state == office.OPEN:
                    office.read(data)
                elif found is not None and found.state == office.PASSWORD:
                    office.read(office.decrypt(data, "tulip"))
            except office.CannotRead:
                pass
            assert time.perf_counter() - started < 2.0

    #: Values a sender may write wherever a number or a name is expected.
    ODD = ('"-1"', '"0"', '"99999999999"', '"abc"', '""', '"1e309"', '"%9"',
           '"ZZZZZZZZ99"', '"NaN"', '"-99999999999"')

    @pytest.mark.parametrize("kind", ["word", "excel", "powerpoint"])
    def test_odd_values_in_well_formed_parts_are_shown_or_refused(self, kind):
        """The same, with the XML itself changed before it is packed: every
        value swapped for one no writer would choose, and spans of markup
        cut out or doubled."""
        numbering = ('<w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:start w:val="1"/>'
                     '<w:numFmt w:val="lowerRoman"/><w:lvlText w:val="%1.%2"/></w:lvl></w:abstractNum>'
                     '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>')
        bodies = {
            "word": (samples.paragraph(WORDS, "Heading1")
                     + '<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>'
                       '</w:pPr><w:r><w:t>item</w:t></w:r></w:p>'
                     + '<w:tbl><w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr>'
                     + samples.paragraph("cell") + "</w:tc></w:tr></w:tbl>"),
            "excel": ('<row r="2"><c r="B2" s="1"><v>46308.5</v></c><c r="C2" t="s"><v>0</v></c>'
                      '<c r="D2" s="2"><v>0.5</v></c></row>'),
            "powerpoint": samples.text_shape("Plan", title=True) + samples.text_shape("Beans", level=2),
        }
        chance = random.Random(f"office values {kind}")
        for _ in range(150):
            text = bodies[kind]
            values = list(re.finditer(r'"[^"]*"', text))
            for found in chance.sample(values, k=min(len(values), chance.randint(1, 4)))[::-1]:
                text = text[:found.start()] + chance.choice(self.ODD) + text[found.end():]
            if chance.random() < 0.4:
                start = chance.randrange(len(text))
                end = min(len(text), start + chance.randint(1, 40))
                text = text[:start] + (text[start:end] * 2 if chance.random() < 0.5 else "") + text[end:]
            if kind == "word":
                made = _word(text, styles=samples.HEADINGS, numbering=numbering)
            elif kind == "excel":
                made = samples.xlsx([("Plots", text, "")], strings=("Plot",), styles=("14", "10"))
            else:
                made = samples.pptx([text])
            started = time.perf_counter()
            try:
                office.read(made)
            except office.CannotRead:
                pass
            assert time.perf_counter() - started < 2.0


class TestTheViewerOpensThem:
    """The attachment window, end to end: what it shows for each."""

    @staticmethod
    def _viewer(qtbot, *files):
        import attachments
        from attachment_view import AttachmentViewer

        found = [attachments.Attachment(part=str(number), name=name, content_type=declared,
                                        size=len(data), data=data)
                 for number, (name, data, declared) in enumerate(files, 1)]
        viewer = AttachmentViewer(found)
        qtbot.addWidget(viewer)
        viewer.resize(1000, 700)
        viewer.show()
        return viewer

    @staticmethod
    def _open(viewer, row: int) -> str:
        from PySide6.QtWidgets import QApplication

        viewer.list.setCurrentRow(row)
        QApplication.processEvents()
        return type(viewer.stack.currentWidget()).__name__

    WORD_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    def test_a_word_file_is_shown_not_called_an_archive(self, qtbot):
        from attachment_view import _row_label

        viewer = self._viewer(qtbot, ("plan.docx", _word(samples.paragraph(WORDS)), self.WORD_TYPE))
        assert self._open(viewer, 0) == "DocumentPane"
        assert WORDS in viewer.document.view.toPlainText()
        assert viewer.status.text() == "Word document, 4 words"
        assert viewer.warning.isHidden() and "archive" not in viewer.warning.text()
        assert _row_label(viewer._found[0]).endswith(" · Word")
        viewer._sweep()

    def test_macros_are_warned_of(self, qtbot):
        viewer = self._viewer(qtbot, ("plan.docm", _word(samples.paragraph(WORDS), macros=True),
                                      "application/vnd.ms-word.document.macroEnabled.12"))
        assert self._open(viewer, 0) == "DocumentPane"
        assert "macros, which the viewer never runs" in viewer.warning.text()
        viewer._sweep()

    def test_a_protected_file_asks_for_its_password_once(self, qtbot):
        sealed = samples.sealed_agile(_word(samples.paragraph(WORDS)), "tulip")
        viewer = self._viewer(qtbot, ("plan.docx", sealed, "application/octet-stream"),
                              ("notes.txt", b"just text", "text/plain"))
        assert self._open(viewer, 0) == "LockedPane"
        assert viewer.locked.title.text() == "This Word document is protected with a password."
        assert viewer.locked.field.echoMode() == viewer.locked.field.EchoMode.Password
        viewer.locked.field.setText("guess")
        viewer.locked._submit()
        assert viewer.locked.said.text() == "That password does not open it."
        assert type(viewer.stack.currentWidget()).__name__ == "LockedPane"
        viewer.locked.field.setText("tulip")
        viewer.locked._submit()
        assert type(viewer.stack.currentWidget()).__name__ == "DocumentPane"
        assert WORDS in viewer.document.view.toPlainText()
        assert viewer.locked.field.text() == "", "the password is not left in the field"
        self._open(viewer, 1)
        assert self._open(viewer, 0) == "DocumentPane", "opened once is enough"
        viewer._sweep()

    def test_saving_a_protected_file_saves_it_protected(self, qtbot, tmp_path, monkeypatch):
        import widgets

        sealed = samples.sealed_agile(_word(samples.paragraph(WORDS)), "tulip")
        viewer = self._viewer(qtbot, ("plan.docx", sealed, "application/octet-stream"))
        self._open(viewer, 0)
        viewer.locked.field.setText("tulip")
        viewer.locked._submit()
        target = tmp_path / "plan.docx"
        monkeypatch.setattr(widgets, "save_file", lambda *a, **k: (str(target), ""))
        viewer._save_current()
        assert target.read_bytes() == sealed, "the opened copy is never written"
        viewer._sweep()

    @pytest.mark.parametrize("name, data, says", [
        ("plan.docx", samples.compound({"EncryptedPackage": b"x" * 5000,
                                        "DRMEncryptedTransform": b"y" * 100}),
         "rights management"),
        ("old.doc", samples.compound({"WordDocument": b"z" * 5000}), "before 2007"),
        ("message.rpmsg", b"\x76\xe8\x04\x60" + b"\x00" * 60, "Outlook"),
    ], ids=["rights", "older", "sealed"])
    def test_what_cannot_be_opened_here_says_how_it_can(self, qtbot, name, data, says):
        viewer = self._viewer(qtbot, (name, data, "application/octet-stream"))
        self._open(viewer, 0)
        assert viewer.stack.currentWidget() is viewer.blank
        assert says in viewer.blank_label.text()
        viewer._sweep()

    def test_a_damaged_document_says_so(self, qtbot):
        made = _word(samples.paragraph(WORDS)).replace(b"</w:body>", b"</w:bodx>")
        broken = samples.package({
            "[Content_Types].xml": samples.TYPES,
            "_rels/.rels": samples.relations(("rId1", "officeDocument", "word/document.xml", False)),
            "word/document.xml": "<w:document><w:body><w:p>"})
        viewer = self._viewer(qtbot, ("a.docx", broken, self.WORD_TYPE),
                              ("b.docx", made, self.WORD_TYPE))
        self._open(viewer, 0)
        assert viewer.stack.currentWidget() is viewer.blank
        assert "damaged" in viewer.blank_label.text()
        viewer._sweep()

    def test_a_locked_pdf_asks_too(self, qtbot):
        viewer = self._viewer(qtbot, ("statement.pdf", samples.locked_pdf("tulip"),
                                      "application/pdf"))
        assert self._open(viewer, 0) == "LockedPane"
        assert viewer.locked.title.text() == "This PDF is protected with a password."
        viewer.locked.field.setText("lily")
        viewer.locked._submit()
        assert viewer.locked.said.text() == "That password does not open it."
        viewer.locked.field.setText("tulip")
        viewer.locked._submit()
        assert type(viewer.stack.currentWidget()).__name__ == "PdfPane"
        assert viewer.status.text() == "1 page"
        viewer._sweep()

    def test_the_details_say_what_the_document_is(self, qtbot):
        import attachment_meta
        import attachments

        item = attachments.Attachment(part="1", name="plan.docx", content_type=self.WORD_TYPE,
                                      size=0, data=_word(samples.paragraph(WORDS), macros=True))
        rows = dict(attachment_meta.facts_for(item))
        assert rows["Document"] == "Word document, opens here"
        assert rows["Macros"] == "yes, never run here"
        assert "Actually" not in rows, "a Word file is a zip by design"
        assert set(attachment_meta._OFFICE_STATES) == {
            office.OPEN, office.PASSWORD, office.RIGHTS, office.OLDER, office.SEALED}

    def test_its_pictures_are_drawn_and_nothing_else_is_loaded(self, qtbot):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QColor, QTextDocument
        from PySide6.QtWidgets import QApplication

        drawing = ('<w:r><w:drawing><wp:inline><wp:extent cx="952500" cy="952500"/>'
                   '<a:graphic><a:graphicData><pic:pic><pic:blipFill><a:blip r:embed="rIdPicture"/>'
                   "</pic:blipFill></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r>")
        made = _word(samples.paragraph("", runs=drawing), links=[
            ("rIdPicture", "image", "media/leaf.png", False)],
            media={"leaf.png": samples.png(40, 160, 60, 32, 32)})
        viewer = self._viewer(qtbot, ("leaf.docx", made, self.WORD_TYPE))
        self._open(viewer, 0)
        QApplication.processEvents()
        image = viewer.document.view.viewport().grab().toImage()
        green = QColor(40, 160, 60)
        seen = {image.pixelColor(x, y).name() for x in range(0, image.width(), 3)
                for y in range(0, min(image.height(), 200), 3)}
        assert green.name() in seen, "the picture the document carried is drawn"
        paper = viewer.document.view
        image_kind = QTextDocument.ResourceType.ImageResource
        assert paper.loadResource(image_kind, QUrl("file:///etc/hosts")) is None
        assert paper.loadResource(image_kind, QUrl("https://tracker.example/p.png")) is None
        viewer._sweep()
