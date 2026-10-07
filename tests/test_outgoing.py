"""Mail that leaves: the message built, who a reply goes to, what is
quoted, and the send itself against a stand-in for the server."""

from __future__ import annotations

import email
from datetime import datetime, timezone
from email import policy

import pytest

import outgoing
from models import EmailMessage


def _message(**fields):
    base = dict(uid="7", subject="Re: Re: Offer", sender_name="Dana Reyes",
                sender_email="dana@northwind.example",
                to="you@icloud.example, Sam <sam@acme.example>",
                cc="pat@acme.example", message_id="<abc@northwind.example>",
                references="<first@northwind.example>",
                date=datetime(2026, 10, 6, 15, 30, tzinfo=timezone.utc),
                body_text="Line one\n\nLine two", body_html="<p>Line <b>one</b></p>")
    base.update(fields)
    return EmailMessage(**base)


class TestBuilding:
    def _parsed(self, draft):
        return email.message_from_bytes(outgoing.build(draft), policy=policy.default)

    def test_text_and_html_side_by_side_with_the_thread_headers(self):
        draft = outgoing.Draft(from_address="you@icloud.example", from_name="You",
                               to=["Dana <dana@northwind.example>"], cc=["pat@acme.example"],
                               bcc=["secret@acme.example"], subject="Re: Offer",
                               text="Thanks.", html="<p>Thanks.</p>",
                               in_reply_to="<abc@northwind.example>",
                               references="<first@northwind.example>")
        parsed = self._parsed(draft)
        assert parsed["From"] == "You <you@icloud.example>"
        assert parsed["To"] == "Dana <dana@northwind.example>"
        assert parsed["Cc"] == "pat@acme.example"
        assert parsed["Bcc"] is None, "Bcc must never be written into the message"
        assert parsed["In-Reply-To"] == "<abc@northwind.example>"
        assert parsed["References"] == "<first@northwind.example> <abc@northwind.example>"
        assert parsed["Message-ID"].endswith("@icloud.example>")
        assert parsed.get_content_type() == "multipart/alternative"
        assert parsed.get_body(("plain",)).get_content().strip() == "Thanks."
        assert "<p>Thanks.</p>" in parsed.get_body(("html",)).get_content()
        assert draft.recipients == ["Dana <dana@northwind.example>", "pat@acme.example",
                                    "secret@acme.example"]

    def test_an_attachment_rides_along(self):
        draft = outgoing.Draft(from_address="you@icloud.example", to=["a@b.example"],
                               subject="cv", text="attached",
                               attachments=[outgoing.Attachment("cv.pdf", "application/pdf",
                                                                b"%PDF-1.4 x")])
        parsed = self._parsed(draft)
        assert parsed.get_content_type() == "multipart/mixed"
        found = list(parsed.iter_attachments())
        assert len(found) == 1 and found[0].get_filename() == "cv.pdf"
        assert found[0].get_content() == b"%PDF-1.4 x"

    def test_a_picture_in_the_message_travels_as_a_part_of_its_own(self):
        """Gmail shows nothing for a picture written into the page as data;
        a related part with a content id is shown everywhere."""
        import base64

        png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + bytes(24)).decode()
        draft = outgoing.Draft(
            from_address="you@icloud.example", to=["a@b.example"], subject="Logo",
            html=f'<p>Hi</p><p><img src="data:image/png;base64,{png}" width="40"></p>')
        parsed = self._parsed(draft)
        page = parsed.get_body(("html",))
        html = page.get_content()
        assert "data:image" not in html and 'src="cid:' in html
        assert 'width="40"' in html, "the rest of the tag is kept"
        related = next(part for part in parsed.walk()
                       if part.get_content_type() == "multipart/related")
        picture = next(part for part in related.walk()
                       if part.get_content_type() == "image/png")
        cid = picture["Content-ID"].strip("<>")
        assert f"cid:{cid}" in html
        assert picture.get_content().startswith(b"\x89PNG")
        assert not list(parsed.iter_attachments()), "not an attachment"

    def test_html_alone_still_has_a_plain_half(self):
        draft = outgoing.Draft(from_address="you@icloud.example", to=["a@b.example"],
                               subject="hi", html="<p>Hello <b>there</b></p>")
        parsed = self._parsed(draft)
        assert "Hello there" in parsed.get_body(("plain",)).get_content()

    def test_what_stops_a_message_going(self):
        empty = outgoing.Draft(from_address="you@icloud.example")
        assert any("Nobody" in p for p in empty.problems())
        assert any("no subject" in p for p in empty.problems())
        bad = outgoing.Draft(from_address="you@icloud.example", to=["not an address"],
                             subject="x")
        assert any("not an address" in p for p in bad.problems())
        heavy = outgoing.Draft(from_address="you@icloud.example", to=["a@b.example"],
                               subject="x",
                               attachments=[outgoing.Attachment(
                                   "big", "application/octet-stream",
                                   b"0" * (outgoing.MOST_BYTES + 1))])
        assert any("weigh" in p for p in heavy.problems())
        fine = outgoing.Draft(from_address="you@icloud.example", to=["a@b.example"],
                              subject="x")
        assert fine.problems() == []


class TestAddresses:
    def test_typed_addresses_are_read_as_people_write_them(self):
        assert outgoing.parse_addresses("a@x.example, Bea <b@y.example>, a@x.example") == [
            "a@x.example", "Bea <b@y.example>"]
        assert outgoing.parse_addresses("") == []

    def test_a_reply_goes_to_the_sender_or_whoever_asked(self):
        to, cc = outgoing.reply_addresses(_message(), own=["you@icloud.example"])
        assert to == ["Dana Reyes <dana@northwind.example>"] and cc == []
        to, _cc = outgoing.reply_addresses(_message(reply_to="replies@northwind.example"),
                                           own=["you@icloud.example"])
        assert to == ["replies@northwind.example"]

    def test_reply_all_takes_everyone_but_us(self):
        to, cc = outgoing.reply_addresses(_message(), own=["You@icloud.example"],
                                          everyone=True)
        assert to == ["Dana Reyes <dana@northwind.example>"]
        assert cc == ["Sam <sam@acme.example>", "pat@acme.example"]

    def test_a_reply_to_our_own_message_still_has_somebody(self):
        mine = _message(sender_name="You", sender_email="you@icloud.example", reply_to="")
        to, _cc = outgoing.reply_addresses(mine, own=["you@icloud.example"])
        assert to == ["You <you@icloud.example>"]

    def test_subjects_are_prefixed_once(self):
        assert outgoing.reply_subject("Re: Re: Offer") == "Re: Offer"
        assert outgoing.reply_subject("Offer", forward=True) == "Fwd: Offer"
        assert outgoing.reply_subject("Fwd: Offer", forward=True) == "Fwd: Offer"


class TestQuoting:
    def test_the_text_is_quoted_under_a_line_saying_who_wrote_it(self):
        quoted = outgoing.quoted_text(_message())
        assert quoted.startswith("On ")
        assert "Dana Reyes <dana@northwind.example> wrote:" in quoted
        assert "> Line one\n>\n> Line two" in quoted

    def test_the_html_is_the_message_as_sent_cleaned_in_a_blockquote(self):
        quoted = outgoing.quoted_html(_message(body_html="<p>Hi</p><script>x()</script>"))
        assert "<blockquote" in quoted and "<p>Hi</p>" in quoted
        assert "script" not in quoted

    def test_a_forward_carries_the_original_headers(self):
        text = outgoing.forward_text(_message())
        assert "---------- Forwarded message ----------" in text
        assert "From: Dana Reyes <dana@northwind.example>" in text
        assert "Subject: Re: Re: Offer" in text and "Line two" in text
        html = outgoing.forward_html(_message())
        assert "<b>From:</b> Dana Reyes" in html and "Line <b>one</b>" in html

    def test_thread_headers_come_from_the_message(self):
        assert outgoing.thread_headers(_message()) == ("<abc@northwind.example>",
                                                       "<first@northwind.example>")
        assert outgoing.thread_headers(_message(message_id="")) == (
            "", "<first@northwind.example>")


class TestTheOutgoingServer:
    def test_known_providers_and_custom_hosts(self):
        from accounts import Account

        assert outgoing.smtp_for(Account(address="a@icloud.example", preset="icloud")) == (
            outgoing.SmtpHost("smtp.mail.me.com", 587, True))
        assert outgoing.smtp_for(Account(address="a@gmail.example", preset="gmail")).port == 465
        custom = Account(address="a@corp.example", preset="custom", host="imap.corp.example")
        assert outgoing.smtp_for(custom) == outgoing.SmtpHost("smtp.corp.example", 587, True)
        named = Account(address="a@corp.example", preset="custom", host="mail.corp.example",
                        smtp_host="out.corp.example", smtp_port=465)
        assert outgoing.smtp_for(named) == outgoing.SmtpHost("out.corp.example", 465, False)


class _Server:
    """A stand-in for smtplib's client: remembers what was asked of it."""

    def __init__(self, refuse_login=False, refuse=None):
        self.calls = []
        self.refuse_login = refuse_login
        self.refuse = refuse or {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.calls.append("quit")

    def ehlo(self):
        self.calls.append("ehlo")

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        import smtplib

        self.calls.append(("login", user, password))
        if self.refuse_login:
            raise smtplib.SMTPAuthenticationError(535, b"no")

    def sendmail(self, sender, recipients, raw):
        self.calls.append(("sendmail", sender, list(recipients), raw))
        return self.refuse


class TestSending:
    HOST = outgoing.SmtpHost("smtp.example", 587, True)

    def test_it_logs_in_and_hands_the_message_over(self):
        server = _Server()
        outgoing.send(b"raw", self.HOST, "you@icloud.example", "app-pass",
                      "You <you@icloud.example>",
                      ["Dana <dana@northwind.example>", "pat@acme.example"],
                      factory=lambda host: server)
        assert server.calls[0] == "ehlo"
        assert ("login", "you@icloud.example", "app-pass") in server.calls
        sent = next(c for c in server.calls if c[0] == "sendmail")
        assert sent[1] == "you@icloud.example"
        assert sent[2] == ["dana@northwind.example", "pat@acme.example"]
        assert sent[3] == b"raw"
        assert server.calls[-1] == "quit"

    def test_a_refused_password_says_so_in_words(self):
        with pytest.raises(outgoing.SendError, match="refused the password"):
            outgoing.send(b"raw", self.HOST, "you@icloud.example", "wrong",
                          "you@icloud.example", ["a@b.example"],
                          factory=lambda host: _Server(refuse_login=True))

    def test_a_refused_recipient_is_named(self):
        with pytest.raises(outgoing.SendError, match="a@b.example"):
            outgoing.send(b"raw", self.HOST, "you@icloud.example", "p", "you@icloud.example",
                          ["a@b.example"],
                          factory=lambda host: _Server(refuse={"a@b.example": (550, b"no")}))

    def test_no_server_is_said_plainly(self):
        with pytest.raises(outgoing.SendError, match="no outgoing server"):
            outgoing.send(b"raw", outgoing.SmtpHost("", 0), "you@icloud.example", "p",
                          "you@icloud.example", ["a@b.example"])
