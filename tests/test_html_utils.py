"""HTML reduction, link recovery and truncation."""

from __future__ import annotations

import pytest

from html_utils import (
    ExtractedText,
    clean_body,
    clean_url,
    find_urls,
    html_to_text,
    normalize_whitespace,
    notable_links,
    plain_to_text,
    truncate_for_model,
)


class TestHtmlToText:
    def test_extracts_readable_prose(self):
        result = html_to_text("<p>Hi Alex,</p><p>Great to meet you.</p>")
        assert result.text == "Hi Alex,\n\nGreat to meet you."

    def test_drops_script_and_style(self):
        html = "<style>.a{color:red}</style><script>alert('x')</script><p>Real text</p>"
        assert html_to_text(html).text == "Real text"

    def test_drops_head_metadata(self):
        html = "<html><head><title>Ignore me</title></head><body><p>Body</p></body></html>"
        assert html_to_text(html).text == "Body"

    @pytest.mark.parametrize(
        "attrs",
        [
            'style="display:none"',
            'style="display: none;"',
            'style="visibility:hidden"',
            'style="max-height:0;overflow:hidden"',
            'style="font-size:0px"',
            'style="mso-hide:all"',
            "hidden",
            'aria-hidden="true"',
        ],
    )
    def test_hidden_preheader_text_is_dropped(self, attrs):
        """Marketing preheaders are invisible to humans and must not reach the model."""
        html = f"<div {attrs}>Secret preheader spam</div><p>Visible content</p>"
        result = html_to_text(html)
        assert "Secret preheader" not in result.text
        assert "Visible content" in result.text

    def test_nested_hidden_blocks_unwind_correctly(self):
        html = (
            '<div style="display:none"><div>inner hidden</div>still hidden</div>'
            "<p>after</p>"
        )
        result = html_to_text(html)
        assert "hidden" not in result.text
        assert result.text == "after"

    def test_entities_are_decoded(self):
        assert "we'd" in html_to_text("<p>we&rsquo;d</p>").text.replace("’", "'")
        assert html_to_text("<p>a &amp; b &lt;c&gt;</p>").text == "a & b <c>"

    def test_lists_become_bullets_without_double_spacing(self):
        result = html_to_text("<ul><li>One</li><li>Two</li></ul><p>Then</p>")
        assert result.text == "• One\n• Two\n\nThen"

    def test_tables_become_tab_separated_rows(self):
        html = "<table><tr><td>Role</td><td>Engineer</td></tr><tr><td>Stage</td><td>Onsite</td></tr></table>"
        assert html_to_text(html).text == "Role\tEngineer\n\nStage\tOnsite"

    def test_br_creates_line_breaks(self):
        assert html_to_text("a<br>b<br/>c").text == "a\nb\nc"

    def test_meaningful_image_alt_text_is_kept(self):
        assert "Acme Corp" in html_to_text('<img src="x.png" alt="Acme Corp">').text

    def test_filename_alt_text_is_noise_and_dropped(self):
        assert html_to_text('<img src="x" alt="image001.png">').text == ""

    def test_zero_width_padding_is_stripped(self):
        result = html_to_text("<p>Hello​‌­﻿world</p>")
        assert result.text == "Helloworld"

    def test_malformed_markup_still_yields_text(self):
        result = html_to_text("<p>unclosed <b>bold <div>weird</p>")
        assert "unclosed" in result.text and "weird" in result.text

    def test_empty_input(self):
        assert html_to_text("").text == ""
        assert html_to_text(None).text == ""


class TestLinkRecovery:
    def test_href_is_captured_even_when_anchor_text_hides_it(self):
        """The whole point: 'Pick a time' is not evidence; the calendly URL is."""
        result = html_to_text('<a href="https://calendly.com/acme/30min">Pick a time</a>')
        assert result.links == ("https://calendly.com/acme/30min",)
        assert result.notable_links == ("https://calendly.com/acme/30min",)

    def test_tracking_parameters_are_stripped(self):
        result = html_to_text(
            '<a href="https://calendly.com/a?utm_source=x&utm_campaign=y">go</a>'
        )
        assert result.links == ("https://calendly.com/a",)

    def test_click_wrappers_are_unwrapped(self):
        wrapped = "https://click.mandrillapp.com/track?url=https%3A%2F%2Fgreenhouse.io%2Fapply"
        assert clean_url(wrapped) == "https://greenhouse.io/apply"

    @pytest.mark.parametrize(
        "href", ["mailto:a@b.com", "tel:+1555", "javascript:void(0)", "#anchor", "data:text/html,x", ""]
    )
    def test_non_web_schemes_are_ignored(self, href):
        assert clean_url(href) is None

    def test_protocol_relative_and_bare_www(self):
        assert clean_url("//example.com/a") == "https://example.com/a"
        assert clean_url("www.example.com/a") == "https://www.example.com/a"

    def test_duplicate_links_are_deduplicated_in_order(self):
        html = '<a href="https://a.com">1</a><a href="https://b.com">2</a><a href="https://a.com">3</a>'
        assert html_to_text(html).links == ("https://a.com", "https://b.com")

    def test_bare_urls_in_plain_text_are_found(self):
        result = plain_to_text("Book here: https://calendly.com/x/30 and reply.")
        assert result.links == ("https://calendly.com/x/30",)

    def test_trailing_punctuation_is_not_part_of_the_url(self):
        assert find_urls("See https://example.com/page.") == ("https://example.com/page",)

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://calendly.com/x", True),
            ("https://app.codesignal.com/t/abc", True),
            ("https://boards.greenhouse.io/x", True),
            ("https://hire.withgoogle.com/x", True),
            ("https://news.ycombinator.com", False),
        ],
    )
    def test_notable_domain_detection(self, url, expected):
        assert bool(notable_links([url])) is expected

    def test_link_appendix_puts_notable_links_first(self):
        extracted = ExtractedText(
            "body",
            ("https://example.com/a", "https://calendly.com/x"),
            ("https://calendly.com/x",),
        )
        appendix = extracted.with_link_appendix()
        assert "LINKS FOUND IN MESSAGE" in appendix
        assert appendix.index("calendly.com") < appendix.index("example.com/a")

    def test_link_appendix_reports_omissions(self):
        links = tuple(f"https://example.com/{i}" for i in range(20))
        text = ExtractedText("body", links, ()).with_link_appendix(max_links=5)
        assert "+15 more links omitted" in text

    def test_no_appendix_when_there_are_no_links(self):
        assert ExtractedText("body").with_link_appendix() == "body"


class TestNormalizeWhitespace:
    def test_collapses_blank_lines(self):
        assert normalize_whitespace("a\n\n\n\n\nb") == "a\n\nb"

    def test_collapses_runs_of_spaces(self):
        assert normalize_whitespace("a     b") == "a b"

    def test_normalises_line_endings(self):
        assert normalize_whitespace("a\r\nb\rc") == "a\nb\nc"

    def test_replaces_nbsp(self):
        assert normalize_whitespace("a\xa0b") == "a b"


class TestTruncation:
    def test_short_text_is_untouched(self):
        text, truncated, original = truncate_for_model("short", 100)
        assert (text, truncated, original) == ("short", False, 5)

    def test_long_text_is_trimmed_and_flagged(self):
        body = "x" * 500
        text, truncated, original = truncate_for_model(body, 100)
        assert truncated is True
        assert original == 500
        assert len(text) <= 100

    def test_prefers_a_paragraph_boundary(self):
        body = "First paragraph. It carries on for a good while here.\n\n" + "y" * 200
        text, truncated, _ = truncate_for_model(body, 60)
        assert truncated is True
        assert text.endswith("here.")

    def test_ignores_a_boundary_that_would_discard_most_of_the_budget(self):
        """A break at 35% of the limit is not worth throwing away 65% of the text."""
        body = "Short.\n\n" + "y" * 200
        text, truncated, _ = truncate_for_model(body, 60)
        assert truncated is True
        assert len(text) > 40

    def test_falls_back_to_a_sentence_boundary(self):
        body = "Alpha beta gamma delta epsilon zeta eta theta. " + "z" * 200
        text, _, _ = truncate_for_model(body, 60)
        assert text.endswith("theta.")

    def test_zero_limit_disables_truncation(self):
        body = "x" * 500
        text, truncated, _ = truncate_for_model(body, 0)
        assert text == body and truncated is False


class TestCleanBody:
    def test_dispatches_on_content_type(self):
        assert clean_body("<p>hi</p>", is_html=True).text == "hi"
        assert clean_body("<p>hi</p>", is_html=False).text == "<p>hi</p>"


class TestRealisticEmails:
    """End-to-end shapes drawn from the kinds of mail this app actually sees."""

    def test_recruiter_email_with_hidden_preheader_and_tracked_link(self):
        html = """
        <html><head><style>.btn{background:#06c}</style></head><body>
          <div style="display:none;max-height:0;overflow:hidden">
            Northwind Systems &middot; your interview awaits &#8203;&#8203;&#8203;
          </div>
          <table><tr><td>
            <p>Hi Alex,</p>
            <p>We&rsquo;d love to schedule a <b>45-minute technical interview</b>.</p>
            <p><a class="btn" href="https://calendly.com/northwind/tech?utm_source=greenhouse">
               Pick a time</a></p>
            <p>Best,<br>Dana</p>
          </td></tr></table>
          <img src="https://track.example/o.gif" alt="" width="1" height="1">
        </body></html>
        """
        result = html_to_text(html)
        assert "your interview awaits" not in result.text
        assert "45-minute technical interview" in result.text
        assert "Pick a time" in result.text
        assert result.notable_links == ("https://calendly.com/northwind/tech",)

    def test_rejection_email(self):
        html = "<p>Thank you for your interest.</p><p>We have decided to move forward with other candidates.</p>"
        assert "move forward with other candidates" in html_to_text(html).text

    def test_assessment_email_link_is_recognised(self):
        html = '<p>Complete your assessment:</p><a href="https://app.codesignal.com/t/abc123">Start</a>'
        result = html_to_text(html)
        assert result.notable_links == ("https://app.codesignal.com/t/abc123",)

    def test_newsletter_with_many_links_stays_bounded(self):
        html = "".join(f'<a href="https://example.com/{i}">Story {i}</a>' for i in range(120))
        result = html_to_text(html)
        assert len(result.links) <= 40


# ==========================================================================
# Condensing for the model — the token-efficiency layer
# ==========================================================================
from html_utils import (  # noqa: E402
    collapse_repeats,
    condense,
    head_and_tail,
    strip_boilerplate,
    strip_quoted_replies,
    strip_signature,
)


class TestStripQuotedReplies:
    @pytest.mark.parametrize(
        "marker",
        [
            "On Tue, 2 Sep 2026 at 09:14, Alex <n@x.com> wrote:",
            "-----Original Message-----",
            "---------- Forwarded message ----------",
            "Le 2 septembre 2026, Alex a ecrit :",
        ],
    )
    def test_history_below_a_reply_marker_is_dropped(self, marker):
        text = f"New content here.\n\n{marker}\nOld quoted content."
        result = strip_quoted_replies(text)
        assert "New content here." in result
        assert "Old quoted content" not in result

    def test_a_run_of_quoted_lines_is_dropped(self):
        text = "New reply.\n> old line one\n> old line two\n> old line three"
        assert "old line three" not in strip_quoted_replies(text)

    def test_a_single_quoted_line_is_kept(self):
        """One '>' line is often a deliberate inline quote, not history."""
        assert "> the important bit" in strip_quoted_replies("Reply.\n> the important bit")

    def test_text_without_quoting_is_untouched(self):
        text = "Just a plain message."
        assert strip_quoted_replies(text) == text


class TestStripSignature:
    def test_a_dash_dash_signature_is_removed(self):
        text = "Body of the message goes here and is reasonably long.\n\n--\nDana Reyes\nTalent"
        assert "Dana Reyes" not in strip_signature(text)

    @pytest.mark.parametrize("footer", ["Sent from my iPhone", "Get Outlook for iOS"])
    def test_device_footers_are_removed(self, footer):
        text = "The actual content of this message, which is long enough to matter.\n\n" + footer
        assert footer not in strip_signature(text)

    def test_a_marker_near_the_top_is_not_a_signature(self):
        text = "--\nThis is actually the whole message and it carries the content."
        assert "whole message" in strip_signature(text)


class TestStripBoilerplate:
    @pytest.mark.parametrize(
        "footer",
        [
            "Unsubscribe | Manage your preferences",
            "View this email in your browser",
            "This message and any attachments are confidential.",
            "You are receiving this because you subscribed.",
            "© 2026 Acme. All rights reserved.",
        ],
    )
    def test_boilerplate_paragraphs_are_dropped(self, footer):
        text = f"Real content that matters.\n\n{footer}"
        assert footer.split()[0] not in strip_boilerplate(text)

    def test_the_real_content_survives(self):
        text = "Please complete the assessment.\n\nUnsubscribe here."
        assert "Please complete the assessment." in strip_boilerplate(text)

    def test_a_message_that_is_entirely_boilerplate_is_not_emptied(self):
        text = "Unsubscribe here."
        assert strip_boilerplate(text)


class TestCollapseRepeats:
    def test_consecutive_duplicate_lines_are_dropped(self):
        assert collapse_repeats("a\na\na\nb") == "a\nb"

    def test_non_consecutive_duplicates_are_kept(self):
        assert collapse_repeats("a\nb\na") == "a\nb\na"


class TestCondense:
    def test_a_realistic_thread_shrinks_dramatically(self):
        text = (
            "Hi Alex,\n\nPlease complete the assessment by Friday.\n\nBest,\nDana\n\n"
            "--\nDana Reyes | Talent | Northwind\n\n"
            "On Tue, 2 Sep 2026 at 09:14, Alex <n@x.com> wrote:\n"
            "> Thanks for getting back to me\n> I am very interested\n\n"
            "This email and any attachments are confidential and intended solely "
            "for the intended recipient.\n\n"
            "Unsubscribe | View this email in your browser"
        )
        result = condense(text)
        assert "complete the assessment by Friday" in result
        assert len(result) < len(text) * 0.4
        for gone in ("Unsubscribe", "confidential", "interested"):
            assert gone not in result

    def test_a_short_clean_message_is_left_alone(self):
        text = "We would like to schedule an interview. Please pick a time."
        assert condense(text) == text

    def test_it_never_returns_nothing(self):
        assert condense("Unsubscribe")
        assert condense("") == ""


class TestHeadAndTail:
    def test_short_text_is_untouched(self):
        assert head_and_tail("short", 100) == ("short", False, 5)

    def test_the_ending_is_preserved(self):
        """Deadlines and calls to action live at the bottom of an email."""
        text = "START. " + ("filler sentence here. " * 400) + "DEADLINE IS FRIDAY."
        result, truncated, original = head_and_tail(text, 400)
        assert truncated is True
        assert original == len(text)
        assert "START." in result
        assert "DEADLINE IS FRIDAY." in result
        assert len(result) <= 400

    def test_the_omission_is_marked(self):
        text = "x" * 5000
        result, _, _ = head_and_tail(text, 300)
        assert "middle of this message omitted" in result

    def test_the_split_favours_the_opening(self):
        text = "A" * 2000 + "B" * 2000
        result, _, _ = head_and_tail(text, 1000, head_share=0.65)
        assert result.count("A") > result.count("B")

    def test_zero_limit_disables_truncation(self):
        text = "x" * 500
        assert head_and_tail(text, 0) == (text, False, 500)
