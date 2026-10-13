"""A message in its own window, and the window a message is written in,
against a stand-in for the main window."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import outgoing
from models import EmailMessage


@pytest.fixture
def windows(qapp):
    """The windows a test opened, closed after it. They delete themselves
    on close, so qtbot must not be asked to close them again."""
    import shiboken6

    kept = []
    yield kept
    for window in kept:
        if shiboken6.isValid(window):
            window._saved = True        # no question on the way out
            window.close()


def _message(uid="1", **fields):
    base = dict(uid=uid, subject=f"Subject {uid}", sender_name="Dana Reyes",
                sender_email="dana@northwind.example", to="you@icloud.example",
                cc="pat@acme.example", message_id=f"<{uid}@northwind.example>",
                date=datetime(2026, 10, 6, 15, 30, tzinfo=timezone.utc),
                body_text=f"Body {uid}", body_html=f"<p>Body <b>{uid}</b></p>",
                flags=("\\Seen",), attachments=("cv.pdf",))
    base.update(fields)
    return EmailMessage(**base)


class _Owner:
    """What the windows ask of the main window, remembered."""

    def __init__(self, count=3):
        self.settings = SimpleNamespace(show_images=False)
        self.model = SimpleNamespace(items=[SimpleNamespace(email=_message(str(n)), moved=False)
                                            for n in range(1, count + 1)])
        self.calls = []
        #: Messages the window asked to have read whole.
        self.read = []
        self.accounts = [SimpleNamespace(id="a1", address="you@icloud.example",
                                         describe=lambda: "you · you@icloud.example")]

    def compose(self, kind, row):
        self.calls.append(("compose", kind, row))

    def act_on_rows(self, what, rows, where=None):
        self.calls.append(("act", what, list(rows), where))
        if what in ("archive", "delete", "junk", "move"):
            # As the main window does: the row stays, marked moved.
            for row in rows:
                self.model.items[row].moved = True

    def select_row(self, row):
        self.calls.append(("select", row))

    def read_whole(self, item):
        self.read.append(item.email.uid)

    def missing_from(self, item):
        return ""

    def neighbour_row(self, row, by):
        live = [n for n, item in enumerate(self.model.items)
                if not getattr(item, "moved", False)]
        if row not in live:
            return None
        at = live.index(row) + by
        return live[at] if 0 <= at < len(live) else None

    def open_settings(self):
        self.calls.append(("settings",))

    def _about(self):
        self.calls.append(("about",))

    def quit_app(self):
        self.calls.append(("quit",))

    def _reveal(self):
        self.calls.append(("reveal",))

    def folder_choices(self):
        return ["Job Search/Interview", "Archive"]

    def open_link(self, url):
        self.calls.append(("link", url))

    def _open_attachments(self, row):
        self.calls.append(("attachments", row))

    def known_addresses(self):
        return ["dana@northwind.example", "Sam <sam@acme.example>"]

    def signature_html(self, replying=False):
        return "<p>-- <br>You</p>"

    def sender_name(self):
        return "You"

    def send_mail(self, account, draft, done, failed, answering=None):
        self.calls.append(("send", account.address if account else None, draft))
        self.sent = (done, failed)
        self.answering = answering

    def save_draft(self, account, draft, done, failed):
        self.calls.append(("draft", draft))
        done()


class TestTheMessageWindow:
    def _window(self, windows, row=0, count=3):
        from mail_window import MessageWindow

        owner = _Owner(count)
        window = MessageWindow(owner, row)
        windows.append(window)
        return owner, window

    def test_it_shows_the_message_and_what_can_be_done(self, windows):
        owner, window = self._window(windows)
        assert window.windowTitle() == "Subject 1"
        assert "Dana Reyes" in window.who.text() and "pat@acme.example" in window.who.text()
        assert "Body" in window.view.toPlainText()
        assert window.attachments_button.text() == "Attachments (1)"
        assert window.read_action.text() == "Mark as Unread"
        assert not window.flag_action.isChecked()
        assert window.reply_action.shortcut().toString() == "Ctrl+R"
        assert window.reply_all_action.shortcut().toString() == "Ctrl+Shift+R"
        assert window.forward_action.shortcut().toString() == "Ctrl+Shift+F"
        assert owner.read == ["1"], "a message cut short is read whole when shown"

    def test_reply_reply_all_and_forward_ask_the_main_window(self, windows):
        owner, window = self._window(windows, row=1)
        window.reply_action.trigger()
        window.reply_all_action.trigger()
        window.forward_action.trigger()
        assert owner.calls == [("compose", "reply", 1), ("compose", "reply_all", 1),
                               ("compose", "forward", 1)]

    def test_next_and_previous_walk_the_table(self, windows):
        owner, window = self._window(windows, row=0)
        assert not window.previous_action.isEnabled()
        window.next_action.trigger()
        assert window.row == 1 and window.windowTitle() == "Subject 2"
        assert ("select", 1) in owner.calls
        window.next_action.trigger()
        assert not window.next_action.isEnabled()
        window.previous_action.trigger()
        assert window.row == 1

    def test_read_and_flag_go_through_the_main_window(self, windows):
        owner, window = self._window(windows)
        window.read_action.trigger()
        assert ("act", "unread", [0], None) in owner.calls
        window.flag_action.trigger()
        assert ("act", "flag", [0], None) in owner.calls
        window.flag_action.trigger()
        assert ("act", "unflag", [0], None) in owner.calls
        owner.model.items[0].email.flags = ()
        window.refresh()
        assert window.read_action.text() == "Mark as Read"

    def test_archive_moves_on_to_the_next_message(self, windows):
        owner, window = self._window(windows, row=0)
        window.archive_action.trigger()
        assert ("act", "archive", [0], None) in owner.calls
        assert window.row == 1 and window.windowTitle() == "Subject 2"
        assert ("select", 1) in owner.calls

    def test_junk_on_the_last_row_steps_back(self, windows):
        owner, window = self._window(windows, row=2)
        window.junk_action.trigger()
        assert ("act", "junk", [2], None) in owner.calls
        assert window.row == 1

    def test_deleting_the_last_message_closes_the_window(self, windows):
        owner, window = self._window(windows, row=0, count=1)
        window.show()
        window.delete_action.trigger()
        assert ("act", "delete", [0], None) in owner.calls
        assert not window.isVisible()

    def test_a_scan_that_moves_the_message_is_followed(self, windows):
        owner, window = self._window(windows, row=0)
        window.show()
        items = owner.model.items
        owner.model.items = [items[2], items[0], items[1]]
        window.refresh()
        assert window.row == 1 and window.windowTitle() == "Subject 1"
        assert window.same_message(items[0]) and not window.same_message(items[1])
        owner.model.items = [items[2]]
        window.refresh()
        assert not window.isVisible(), "the message is gone, so is the window"

    def test_its_menus_carry_the_app_and_the_message(self, windows):
        owner, window = self._window(windows)
        titles = [m.title() for m in window.menuBar().findChildren(
            type(window.message_menu)) if m.title()]
        assert titles == ["&File", "&Window", "&Message"]
        assert "Esc" in [s.toString() for s in window.close_action.shortcuts()]
        roles = {a.menuRole() for a in window.app_menu.actions()}
        from PySide6.QtGui import QAction
        assert {QAction.MenuRole.PreferencesRole, QAction.MenuRole.AboutRole,
                QAction.MenuRole.QuitRole} <= roles
        window.window_menu.actions()[-1].trigger()
        assert ("reveal",) in owner.calls

    def test_move_to_offers_the_folders(self, windows):
        owner, window = self._window(windows)
        window.move_menu.aboutToShow.emit()
        names = [a.text() for a in window.move_menu.actions()]
        assert names == ["Job Search/Interview", "Archive"]
        window.move_menu.actions()[1].trigger()
        assert ("act", "move", [0], "Archive") in owner.calls

    def test_attachments_open_through_the_main_window(self, windows):
        owner, window = self._window(windows, row=2)
        window.attachments_button.click()
        assert ("attachments", 2) in owner.calls


class TestTheComposeWindow:
    def _window(self, windows, draft=None, **extra):
        from mail_window import ComposeWindow

        owner = _Owner()
        draft = draft or outgoing.Draft(from_address="you@icloud.example")
        window = ComposeWindow(owner, draft, owner.accounts, owner.accounts[0], **extra)
        windows.append(window)
        return owner, window

    def test_its_touch_bar_offers_words_for_the_letter(self, windows):
        import touchbar

        _owner, window = self._window(windows)
        bar = touchbar.of(window)
        assert bar.flat["suggest"].kind == "popover"
        words = bar.flat["words"]
        assert words.kind == "suggestions" and words.editor is window.editor

    def test_a_reply_opens_addressed_with_the_quote_under_the_signature(self, windows):
        draft = outgoing.Draft(from_address="you@icloud.example",
                               to=["Dana <dana@northwind.example>"], subject="Re: Offer",
                               in_reply_to="<1@northwind.example>")
        owner, window = self._window(windows, draft, quoted_html="<blockquote>Body</blockquote>",
                                     quoted_text="> Body")
        assert window.to_edit.text() == "Dana <dana@northwind.example>"
        assert window.subject_edit.text() == "Re: Offer"
        html = window.editor.html()
        assert html.index("You") < html.index("Body"), "the quote goes under the signature"
        made = window.draft()
        assert made.in_reply_to == "<1@northwind.example>"
        assert made.from_name == "You"

    def test_the_message_it_answers_goes_with_the_send(self, windows):
        owner, window = self._window(windows, answering=("7", "INBOX", "a1"))
        window.to_edit.setText("dana@northwind.example")
        window.subject_edit.setText("Re: Offer")
        window.send()
        assert owner.answering == ("7", "INBOX", "a1")

    def test_nothing_goes_without_somebody_to_send_it_to(self, windows, monkeypatch):
        from PySide6.QtWidgets import QMessageBox

        said = []
        monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a: said.append(a[2])))
        owner, window = self._window(windows)
        window.subject_edit.setText("Hello")
        window.send()
        assert said and "Nobody" in said[0]
        assert not any(c[0] == "send" for c in owner.calls)

    def test_sending_hands_the_draft_to_the_main_window_and_closes(self, windows):
        owner, window = self._window(windows)
        window.show()
        window.to_edit.setText("dana@northwind.example")
        window.subject_edit.setText("Hello")
        window.editor.setPlainText("Hi Dana")
        window.send()
        kind, address, draft = owner.calls[-1]
        assert kind == "send" and address == "you@icloud.example"
        assert draft.to == ["dana@northwind.example"] and draft.subject == "Hello"
        assert "Hi Dana" in draft.text and "Hi Dana" in draft.html
        assert not window.send_button.isEnabled()
        done, _failed = owner.sent
        done()
        assert not window.isVisible()

    def test_a_failed_send_leaves_the_message_to_try_again(self, windows, monkeypatch):
        from PySide6.QtWidgets import QMessageBox

        said = []
        monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a: said.append(a[2])))
        owner, window = self._window(windows)
        window.show()
        window.to_edit.setText("dana@northwind.example")
        window.subject_edit.setText("Hello")
        window.send()
        _done, failed = owner.sent
        failed("The server refused the password.")
        assert window.isVisible() and window.send_button.isEnabled()
        assert said[-1] == "The server refused the password."

    def test_formatting_reaches_the_message(self, windows):
        owner, window = self._window(windows)
        window.editor.setPlainText("plain bold")
        cursor = window.editor.textCursor()
        cursor.setPosition(6)
        cursor.setPosition(10, cursor.MoveMode.KeepAnchor)
        window.editor.setTextCursor(cursor)
        window.bold_action.trigger()
        window.editor.make_list(False)
        html = window.editor.html()
        assert "font-weight" in html and "bold" in html
        assert "<ul" in html or "list-style" in html
        window.editor.clear_formatting()
        assert "font-weight:700" not in window.editor.html()

    def test_attachments_can_be_added_and_taken_off(self, windows, tmp_path):
        owner, window = self._window(windows)
        note = tmp_path / "note.txt"
        note.write_text("hello")
        window.attach_paths([str(note), str(tmp_path / "missing.txt")])
        assert [a.name for a in window.draft().attachments] == ["note.txt"]
        assert window.attachment_holder.isVisibleTo(window)
        window.add_attachments([outgoing.Attachment("cv.pdf", "application/pdf", b"%PDF")])
        assert [a.name for a in window.draft().attachments] == ["note.txt", "cv.pdf"]
        window._drop_attachment(0)
        assert [a.name for a in window.draft().attachments] == ["cv.pdf"]

    def test_the_format_menu_and_the_edit_menu_reach_the_editor(self, windows):
        owner, window = self._window(windows)
        titles = [m.title() for m in window.menuBar().findChildren(
            type(window.message_menu)) if m.title()]
        assert titles[:5] == ["&File", "&Window", "&Message", "&Edit", "F&ormat"]
        window.editor.setPlainText("words")
        window.editor.setFocus()
        window.editor.selectAll()
        sizes = next(a.menu() for a in window.format_menu.actions()
                     if a.menu() is not None and a.text() == "Size")
        twenty_two = next(a for a in sizes.actions() if a.text() == "22")
        twenty_two.trigger()
        assert window.size_box.currentText() == "22"
        assert "22pt" in window.editor.html()
        window.note("Fetching 2 attachments…")
        assert window.statusBar().currentMessage() == "Fetching 2 attachments…"

    def test_closing_an_unsent_message_asks(self, windows, monkeypatch):
        owner, window = self._window(windows)
        window.show()
        window.to_edit.setText("dana@northwind.example")
        window.editor.setPlainText("Started writing")
        assert window.is_dirty()
        answers = iter(["cancel", "save"])
        asked = []
        monkeypatch.setattr(window, "_ask_to_keep",
                            lambda: (asked.append(1), next(answers))[1])
        window.close()
        assert asked and window.isVisible(), "cancel keeps the message open"
        window.close()
        assert len(asked) == 2 and not window.isVisible()
        assert any(c[0] == "draft" for c in owner.calls), "save puts it in Drafts first"

    def test_nothing_pressed_in_the_question_is_cancel(self, windows, dialog_calls):
        owner, window = self._window(windows)
        window.show()
        window.subject_edit.setText("Half done")
        window.close()
        assert dialog_calls[-1][2] == "This message has not been sent."
        assert window.isVisible()

    def test_an_untouched_message_closes_without_a_word(self, windows, dialog_calls):
        owner, window = self._window(windows)
        window.show()
        assert not window.is_dirty(), "a signature alone is not writing"
        window.close()
        assert not dialog_calls and not window.isVisible()

    def test_an_untouched_reply_closes_without_a_word_too(self, windows, dialog_calls):
        draft = outgoing.Draft(from_address="you@icloud.example",
                               to=["dana@northwind.example"], subject="Re: Offer")
        owner, window = self._window(windows, draft, quoted_html="<p>quoted</p>",
                                     quoted_text="> quoted")
        window.show()
        assert not window.is_dirty()
        window.editor.moveCursor(window.editor.textCursor().MoveOperation.Start)
        window.editor.insertPlainText("Thanks")
        assert window.is_dirty()
        window.close()
        assert dialog_calls and window.isVisible()

    def test_the_keys_are_the_usual_ones(self, windows):
        owner, window = self._window(windows)
        assert "Ctrl+Return" in [s.toString() for s in window.send_action.shortcuts()]
        assert window.draft_action.shortcut().toString() == "Ctrl+S"
        assert window.attach_action.shortcut().toString() == "Ctrl+Shift+A"
        assert window.bold_action.shortcut().toString() == "Ctrl+B"
        assert window.link_action.shortcut().toString() == "Ctrl+K"


def _page(editor, base: str, text: str) -> None:
    """``editor`` on a page of ``base`` with text of ``text``: a theme's."""
    from PySide6.QtGui import QColor, QPalette

    palette = editor.palette()
    palette.setColor(QPalette.ColorRole.Base, QColor(base))
    palette.setColor(QPalette.ColorRole.Text, QColor(text))
    editor.setPalette(palette)


def _stands_out(editor, words: str) -> float:
    """How far the line holding ``words`` stands from what it is shown on,
    as drawn: the lightness between the line's commonest colour and the
    pixel furthest from it. Text that cannot be seen stands nowhere."""
    from collections import Counter

    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QApplication

    editor.setCursorWidth(0)        # the caret is not text
    QApplication.processEvents()
    found = editor.document().find(words)
    assert not found.isNull(), words
    image = editor.viewport().grab().toImage()
    ratio = image.devicePixelRatio()
    box = editor.document().documentLayout().blockBoundingRect(found.block())
    seen = Counter(
        image.pixel(x, y)
        for y in range(int(box.top() * ratio) + 1, int(box.bottom() * ratio) - 1)
        for x in range(int(box.left() * ratio), min(int(box.right() * ratio), image.width())))
    behind = QColor.fromRgb(seen.most_common(1)[0][0]).lightnessF()
    return max(abs(QColor.fromRgb(pixel).lightnessF() - behind) for pixel in seen)


class TestTheLetterKeepsTheEditorsPage:
    """A reply to mail written on a white page opened on that page, with
    the signature and everything typed in a dark theme's white text on it.
    Whatever the message answered wears, the letter is written on the
    editor's own page, and the message is shown as it is, in a box."""

    WHITE_PAGE = ('<html><head><style>body{background-color:#ffffff;color:#000000}'
                  'p{color:#000000;font-family:Arial}</style></head>'
                  '<body bgcolor="#ffffff"><table width="100%" bgcolor="#ffffff"><tr><td>'
                  '<p>The picnic moves indoors this year.</p></td></tr></table>'
                  '</body></html>')
    DARK_PAGE = ('<html><body style="background-color:#101010;color:#eeeeee">'
                 '<p>The lanterns go up on Friday.</p></body></html>')
    #: Readable text stands this far from what is behind it; text the colour
    #: of its page stands nowhere.
    APART = 0.4

    def _reply(self, windows, body_html, base, text, forward=False):
        from PySide6.QtWidgets import QApplication

        from mail_window import ComposeWindow

        owner = _Owner()
        message = _message(body_html=body_html)
        quote = (outgoing.forward_html(message) if forward
                 else outgoing.quoted_html(message))
        window = ComposeWindow(owner, outgoing.Draft(
            from_address="you@icloud.example", to=["dana@northwind.example"]),
            owner.accounts, owner.accounts[0], quote,
            outgoing.quoted_text(message))
        windows.append(window)
        _page(window.editor, base, text)
        window.resize(760, 560)
        window.show()
        QApplication.processEvents()
        window.editor.insertPlainText("Count me in")
        return window

    def test_a_white_page_stays_in_the_quote(self, windows):
        window = self._reply(windows, self.WHITE_PAGE, "#1e1e1e", "#ffffff")
        from PySide6.QtCore import Qt

        root = window.editor.document().rootFrame().frameFormat()
        assert root.background().style() == Qt.BrushStyle.NoBrush, "no page of its own"
        editor = window.editor
        assert _stands_out(editor, "Count me in") > self.APART, "what is typed"
        assert _stands_out(editor, "You") > self.APART, "the signature"
        assert _stands_out(editor, "wrote:") > self.APART
        assert _stands_out(editor, "The picnic moves indoors") > self.APART, "the quote"

    def test_a_dark_page_stays_in_the_quote_too(self, windows):
        window = self._reply(windows, self.DARK_PAGE, "#ffffff", "#141414")
        editor = window.editor
        assert _stands_out(editor, "Count me in") > self.APART
        assert _stands_out(editor, "You") > self.APART
        assert _stands_out(editor, "wrote:") > self.APART, "pale on its dark page"
        assert _stands_out(editor, "The lanterns go up") > self.APART

    def test_a_forward_keeps_the_page_too(self, windows):
        window = self._reply(windows, self.WHITE_PAGE, "#1e1e1e", "#ffffff",
                             forward=True)
        assert _stands_out(window.editor, "Count me in") > self.APART
        assert _stands_out(window.editor, "You") > self.APART
        assert _stands_out(window.editor, "The picnic moves indoors") > self.APART

    def test_words_the_message_gave_no_colour_read_in_its_box(self, windows):
        window = self._reply(windows, "<p>Bring a folding chair.</p>",
                             "#1e1e1e", "#ffffff")
        assert _stands_out(window.editor, "Bring a folding chair") > self.APART

    def test_what_is_typed_in_an_empty_cell_of_the_quote_reads(self, windows):
        window = self._reply(
            windows, "<table><tr><td>Seats left</td><td></td></tr></table>",
            "#1e1e1e", "#ffffff")
        editor = window.editor
        seats = editor.document().find("Seats left")
        cell = seats.currentTable().cellAt(0, 1).firstCursorPosition()
        editor.setTextCursor(cell)
        editor.insertPlainText("Two of us")
        assert _stands_out(editor, "Two of us") > self.APART

    def test_what_is_sent_carries_no_page_but_the_quotes_own(self, windows):
        import re

        window = self._reply(windows, self.WHITE_PAGE, "#1e1e1e", "#ffffff")
        sent = window.draft()
        assert "bgcolor" not in re.search(r"<body[^>]*>", sent.html).group(0)
        assert re.search(r'<table[^>]*bgcolor="#ffffff"', sent.html)
        assert "Count me in" in sent.text and "The picnic moves indoors" in sent.text
        assert sent.text.index("Count me in") < sent.text.index("You") < (
            sent.text.index("The picnic"))


class TestPastedWordsReadOnThePage:
    """Rich text pasted in brought the page it was copied off: black words
    on a white page's colour, which on a dark page is black on dark."""

    BROWSER = ('<span style="color: rgb(0, 0, 0); background-color: rgb(255, 255, 255);">'
               'Warm wishes from the allotment</span><br>'
               '<span style="color: rgb(0, 0, 0); background-color: rgb(255, 255, 0);">'
               'marked</span> <span style="color: #d93025;">in red</span>'
               '<table bgcolor="#ffffff"><tr>'
               '<td bgcolor="#ffffff" style="color:#000000">Plot nine</td>'
               '<td bgcolor="#3366cc" style="color:#ffffff">Shed</td></tr></table>')

    def _paste(self, windows, html, base="#1e1e1e", text="#ffffff"):
        from PySide6.QtCore import QMimeData
        from PySide6.QtWidgets import QApplication

        from mail_window import RichEditor

        editor = RichEditor()
        windows.append(editor)
        _page(editor, base, text)
        editor.resize(640, 400)
        editor.show()
        copied = QMimeData()
        copied.setHtml(html)
        copied.setText("pasted")
        editor.insertFromMimeData(copied)
        QApplication.processEvents()
        return editor

    @staticmethod
    def _format(editor, words):
        found = editor.document().find(words)
        cursor = editor.textCursor()
        cursor.setPosition(found.selectionStart() + 1)
        return cursor.charFormat()

    def test_words_off_a_white_page_read_on_a_dark_one(self, windows):
        from PySide6.QtCore import Qt

        editor = self._paste(windows, self.BROWSER)
        assert _stands_out(editor, "Warm wishes") > 0.4
        assert _stands_out(editor, "Plot nine") > 0.4
        # On the editor's own page, not on the white one they came off.
        nothing = Qt.BrushStyle.NoBrush
        assert self._format(editor, "Warm wishes").background().style() == nothing
        plot = editor.document().find("Plot nine")
        assert plot.currentTable().cellAt(plot).format().background().style() == nothing

    def test_words_off_a_dark_page_read_on_a_light_one(self, windows):
        editor = self._paste(
            windows, '<span style="color:#f0f0f0; background-color:#121212">'
                     'Seedlings by the gate</span>', "#ffffff", "#141414")
        assert _stands_out(editor, "Seedlings") > 0.4

    def test_a_highlight_a_colour_and_a_tinted_cell_are_kept(self, windows):
        from PySide6.QtCore import Qt

        def painted(brush):
            # An empty brush says black too.
            return (brush.color().name()
                    if brush.style() != Qt.BrushStyle.NoBrush else None)

        editor = self._paste(windows, self.BROWSER)
        marked = self._format(editor, "marked")
        assert painted(marked.background()) == "#ffff00"
        assert painted(marked.foreground()) == "#000000", "black on the marker is kept"
        assert painted(self._format(editor, "in red").foreground()) == "#d93025"
        shed = editor.document().find("Shed")
        cell = shed.currentTable().cellAt(shed)
        assert painted(cell.format().background()) == "#3366cc"
        assert painted(self._format(editor, "Shed").foreground()) == "#ffffff"

    def test_plain_text_still_pastes(self, windows):
        from PySide6.QtCore import QMimeData

        from mail_window import RichEditor

        editor = RichEditor()
        windows.append(editor)
        copied = QMimeData()
        copied.setText("Just words")
        editor.insertFromMimeData(copied)
        assert editor.toPlainText() == "Just words"


class TestNothingIsCutOff:
    """A narrow window keeps every control: the formatting row wraps, and
    the message window's bar is sized for its buttons."""

    def test_the_formatting_row_wraps_rather_than_hides(self, windows):
        from PySide6.QtWidgets import QApplication

        from mail_window import ComposeWindow

        owner = _Owner()
        window = ComposeWindow(owner, outgoing.Draft(from_address="you@icloud.example"),
                               owner.accounts, owner.accounts[0])
        windows.append(window)
        holder = window.formatting
        # Narrower than the row wants, then wider: measured, so the test
        # holds whatever the buttons weigh on this platform.
        row = holder.holder.layout()
        wanted = (sum(row.itemAt(i).sizeHint().width() for i in range(row.count()))
                  + row.spacing() * (row.count() - 1))
        window.resize(int(wanted * 0.6), 600)
        window.show()
        QApplication.processEvents()
        buttons = [window.format_button(a) for a in (
            window.bold_action, window.bullets_action, window.plain_action)]
        assert all(b is not None and b.isVisibleTo(window) for b in buttons)
        for button in buttons:
            assert button.geometry().right() <= holder.width(), "nothing past the edge"
        assert holder.height() >= 2 * buttons[0].height(), "two rows when narrow"
        window.resize(wanted + 240, 600)
        QApplication.processEvents()
        assert holder.height() < 2 * buttons[0].height(), "one row when wide"
        assert window.format_button(window.plain_action).geometry().right() <= holder.width()

    def test_the_message_bar_fits_its_window(self, windows):
        from PySide6.QtWidgets import QApplication

        from mail_window import MessageWindow

        owner = _Owner()
        window = MessageWindow(owner, 0)
        windows.append(window)
        window.show()
        QApplication.processEvents()
        bar = window.toolbar
        assert window.previous_action.iconText() == "Previous"
        for action in (window.reply_action, window.junk_action, window.delete_action,
                       window.previous_action, window.next_action):
            button = bar.widgetForAction(action)
            assert button is not None and button.isVisibleTo(window), action.text()
            assert button.geometry().right() <= bar.width(), action.text()
        assert window.move_button.isVisibleTo(window)
        assert window.move_button.geometry().right() <= bar.width()

    def test_a_reply_opens_on_two_blank_lines_above_the_signature(self, windows):
        from mail_window import ComposeWindow

        owner = _Owner()
        draft = outgoing.Draft(from_address="you@icloud.example",
                               to=["dana@northwind.example"], subject="Re: Offer")
        window = ComposeWindow(owner, draft, owner.accounts, owner.accounts[0],
                               quoted_html="<blockquote>Body</blockquote>")
        windows.append(window)
        lines = window.editor.text().split("\n")
        assert lines[0] == "" and lines[1] == "", "room to write first"
        assert "You" in "\n".join(lines[2:4]), "then the signature"
        assert window.editor.textCursor().position() == 0
        unsigned = _Owner()
        unsigned.signature_html = lambda replying=False: ""
        blank = ComposeWindow(unsigned, outgoing.Draft(from_address="you@icloud.example"),
                              unsigned.accounts, unsigned.accounts[0])
        windows.append(blank)
        assert blank.editor.text() == "", "nothing to write above, so no blank lines"


class TestAMessageWithNothingToRead:
    def test_the_window_says_what_it_carries(self, windows):
        from mail_window import MessageWindow

        owner = _Owner()
        owner.model.items[0].email.body_html = "<html><body><div><br></div></body></html>"
        owner.model.items[0].email.body_text = ""
        owner.model.items[0].email.attachments = ("notes.md", "cv.pdf")
        window = MessageWindow(owner, 0)
        windows.append(window)
        shown = window.view.toPlainText()
        assert shown.startswith("This message has no text.")
        assert "2 attachments" in shown and "notes.md" in shown and "cv.pdf" in shown
        owner.model.items[0].email.attachments = ()
        window.refresh()
        assert window.view.toPlainText() == "This message has no text."

    def test_pictures_alone_are_still_drawn(self, windows):
        from mail_window import MessageWindow

        owner = _Owner()
        owner.model.items[0].email.body_html = '<p><img src="data:image/png;base64,iVBORw0KGgo=" alt="logo"></p>'
        owner.model.items[0].email.body_text = ""
        window = MessageWindow(owner, 0)
        windows.append(window)
        assert "no text" not in window.view.toPlainText()
