"""HTML to plain text, tuned for marketing-heavy job email.

Recruiting mail is usually HTML, often table-based, with hidden preheaders
and walls of CSS; fed raw to a model it wastes tokens and degrades
classification. This produces the text a person would read and keeps the
*link targets*: the strongest interview signal in real mail is a Calendly,
Greenhouse or Ashby booking URL behind "pick a time".

Standard library only, so bundles stay small and no third-party parser
breaks on malformed email HTML.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from typing import Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import unquote, urlparse

#: Tags whose entire contents are noise.
_DROP_TAGS = {
    "script", "style", "head", "noscript", "template", "svg", "canvas",
    "iframe", "object", "embed", "map", "select", "textarea",
}

#: Tags that force a line break before and after their content.
_BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "body", "center", "dd", "div",
    "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form", "h1",
    "h2", "h3", "h4", "h5", "h6", "header", "hr", "html", "legend", "li",
    "main", "nav", "ol", "p", "pre", "section", "table", "tbody", "tfoot",
    "thead", "tr", "ul",
}

_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}

#: Domains that are strong, checkable evidence of a specific triage category.
SCHEDULING_DOMAINS = (
    "calendly.com", "cal.com", "savvycal.com", "hubspot.com/meetings",
    "meetings.hubspot.com", "youcanbook.me", "chilipiper.com", "goodtime.io",
    "calendarhero.com", "doodle.com", "when2meet.com", "acuityscheduling.com",
    "zoom.us", "teams.microsoft.com", "meet.google.com", "whereby.com",
    "loom.com", "spark.hire", "sparkhire.com", "hirevue.com", "willo.video",
    "modernhire.com", "vidcruiter.com",
)

ATS_DOMAINS = (
    "greenhouse.io", "lever.co", "ashbyhq.com", "workday.com", "myworkdayjobs.com",
    "smartrecruiters.com", "icims.com", "jobvite.com", "bamboohr.com",
    "breezy.hr", "workable.com", "teamtailor.com", "recruitee.com",
    "successfactors.com", "taleo.net", "hire.withgoogle.com", "rippling.com",
    "gem.com", "paradox.ai", "eightfold.ai", "dover.com", "wellfound.com",
)

ASSESSMENT_DOMAINS = (
    "hackerrank.com", "codesignal.com", "codility.com", "leetcode.com",
    "karat.com", "coderbyte.com", "devskiller.com", "testgorilla.com",
    "woven.teams", "triplebyte.com", "mettl.com", "imocha.io", "qualified.io",
    "coderpad.io", "byteboard.dev", "filtered.ai", "pymetrics.ai",
    "criteriacorp.com", "predictiveindex.com", "shl.com",
)

NOTABLE_DOMAINS: Tuple[str, ...] = SCHEDULING_DOMAINS + ATS_DOMAINS + ASSESSMENT_DOMAINS

#: Zero-width and formatting characters email designers use to pad preheaders.
_INVISIBLE_CHARS = re.compile(
    "[͏​‌‍‎‏  ‪-‮"
    "⁠-⁤⁪-⁯﻿­᠎]"
)

_URL_RE = re.compile(r"""(?xi)
    \b(?:https?://|www\.)
    [^\s<>"'\)\]\},;]+
""")

_TRACKING_PARAM_RE = re.compile(
    r"(?i)[?&](?:utm_[a-z_]+|mc_[a-z]+|_hs[a-z]*|trk|trkEmail|ck_subscriber_id|"
    r"mkt_tok|vero_id|hsCtaTracking|elqTrack|elqTrackId|s_cid|gclid|fbclid)=[^&]*"
)

_FILENAME_ALT_RE = re.compile(r"(?i)^[\w .%-]{1,64}\.(png|jpe?g|gif|webp|bmp|svg|tiff?)$")

_MAX_LINKS = 40


@dataclass(frozen=True)
class ExtractedText:
    """Plain text plus the links recovered from the original document."""

    text: str
    links: Tuple[str, ...] = ()
    notable_links: Tuple[str, ...] = ()

    @property
    def has_notable_links(self) -> bool:
        return bool(self.notable_links)

    def with_link_appendix(self, max_links: int = 12) -> str:
        """The text an LLM should see: body first, then the recovered links."""
        if not self.links:
            return self.text
        shown = list(self.notable_links)
        for link in self.links:
            if len(shown) >= max_links:
                break
            if link not in shown:
                shown.append(link)
        shown = shown[:max_links]
        appendix = "\n".join(f"- {link}" for link in shown)
        omitted = len(self.links) - len(shown)
        if omitted > 0:
            appendix += f"\n- (+{omitted} more link{'s' if omitted != 1 else ''} omitted)"
        return f"{self.text}\n\n--- LINKS FOUND IN MESSAGE ---\n{appendix}"


class _TextExtractor(HTMLParser):
    """Collect readable text, tracking block structure and hidden content."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: List[str] = []
        self._drop_tag: Optional[str] = None
        self._drop_depth = 0
        self._hidden_tag: Optional[str] = None
        self._hidden_depth = 0
        #: (tag, is_zero) for every element that set a font size, innermost
        #: last. Email HTML is not reliably balanced, so this is pruned by
        #: name on the way out rather than trusted to nest.
        self._font_stack: List[Tuple[str, bool]] = []
        self._list_stack: List[str] = []
        self.links: List[str] = []
        self._seen_links: Set[str] = set()

    @property
    def _suppressed(self) -> bool:
        if self._drop_tag is not None or self._hidden_tag is not None:
            return True
        # The nearest ancestor that said anything about size decides.
        return bool(self._font_stack) and self._font_stack[-1][1]

    def _emit(self, text: str) -> None:
        if text:
            self._chunks.append(text)

    def _break(self, hard: bool = False) -> None:
        self._chunks.append("\n\n" if hard else "\n")

    def _record_link(self, href: Optional[str]) -> None:
        cleaned = clean_url(href)
        if cleaned and cleaned not in self._seen_links and len(self.links) < _MAX_LINKS:
            self._seen_links.add(cleaned)
            self.links.append(cleaned)

    @staticmethod
    def _is_hidden(attrs: Sequence[Tuple[str, Optional[str]]]) -> bool:
        mapping = {name.lower(): (value or "") for name, value in attrs}
        if "hidden" in mapping:
            return True
        style = mapping.get("style", "").lower().replace(" ", "")
        if "display:none" in style or "visibility:hidden" in style:
            return True
        if "mso-hide:all" in style:
            return True
        if mapping.get("aria-hidden", "").lower() == "true":
            return True
        # Classic preheader trick: a zero-size, zero-opacity block.
        if "max-height:0" in style and "overflow:hidden" in style:
            return True
        if "opacity:0" in style and "height:0" in style:
            return True
        # font-size:0 alone is a layout idiom, not hiding: responsive builders
        # put it on the columns' container to close the gaps between
        # inline-blocks, and the children set their size back. Read as hidden,
        # it threw away whole message bodies, so it means hidden only alongside
        # a second cue.
        if _ZERO_FONT.search(style) and _ALSO_HIDDEN.search(style):
            return True
        return False

    @staticmethod
    def _font_size(attrs: Sequence[Tuple[str, Optional[str]]]) -> Optional[bool]:
        """Whether this element sets a font size, and whether it is zero. None
        means it inherits.

        A zero size hides the text the element holds itself; a real size on
        a descendant brings that descendant back, as a responsive layout's
        columns do.
        """
        mapping = {name.lower(): (value or "") for name, value in attrs}
        style = mapping.get("style", "").lower().replace(" ", "")
        if not style or "font-size:" not in style:
            return None
        return bool(_ZERO_FONT.search(style))

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: D102
        tag = tag.lower()

        if self._drop_tag is not None:
            if tag == self._drop_tag:
                self._drop_depth += 1
            return
        if self._hidden_tag is not None:
            if tag == self._hidden_tag:
                self._hidden_depth += 1
            return

        if tag in _DROP_TAGS:
            self._drop_tag, self._drop_depth = tag, 1
            return
        if tag not in _VOID_TAGS and self._is_hidden(attrs):
            self._hidden_tag, self._hidden_depth = tag, 1
            return
        if tag not in _VOID_TAGS:
            sets_zero = self._font_size(attrs)
            if sets_zero is not None:
                self._font_stack.append((tag, sets_zero))

        if tag == "a":
            for name, value in attrs:
                if name.lower() == "href":
                    self._record_link(value)
                    break
        elif tag == "img":
            mapping = {name.lower(): (value or "") for name, value in attrs}
            alt = mapping.get("alt", "").strip()
            if alt and len(alt) > 1 and not _FILENAME_ALT_RE.match(alt):
                self._emit(f"[image: {alt}] ")
        elif tag == "br":
            self._break()
        elif tag == "hr":
            self._break(hard=True)
        elif tag in ("ul", "ol"):
            self._list_stack.append(tag)
            self._break(hard=True)
        elif tag == "li":
            self._break()
            self._emit("• ")
        elif tag in ("td", "th"):
            self._emit("\t")
        elif tag in _BLOCK_TAGS:
            self._break(hard=tag in ("p", "div", "table", "section", "article", "blockquote"))

    def handle_startendtag(self, tag: str, attrs) -> None:  # noqa: D102
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:  # noqa: D102
        tag = tag.lower()
        if self._drop_tag is not None:
            if tag == self._drop_tag:
                self._drop_depth -= 1
                if self._drop_depth <= 0:
                    self._drop_tag = None
            return
        if self._hidden_tag is not None:
            if tag == self._hidden_tag:
                self._hidden_depth -= 1
                if self._hidden_depth <= 0:
                    self._hidden_tag = None
            return
        for index in range(len(self._font_stack) - 1, -1, -1):
            if self._font_stack[index][0] == tag:
                del self._font_stack[index:]
                break
        if tag in ("ul", "ol"):
            if self._list_stack:
                self._list_stack.pop()
            self._break(hard=True)
        elif tag == "li":
            # The next <li> supplies its own break; emitting one here would
            # double-space every bullet list.
            pass
        elif tag in _BLOCK_TAGS:
            self._break(hard=tag in ("p", "div", "table", "section", "article", "blockquote"))

    def handle_data(self, data: str) -> None:  # noqa: D102
        if self._suppressed or not data:
            return
        self._emit(data)

    def error(self, message: str) -> None:  # pragma: no cover - py3.8 compat hook
        return

    def result(self) -> str:
        return "".join(self._chunks)


def clean_url(href: Optional[str]) -> Optional[str]:
    """Normalise a link, unwrap common click-trackers, drop non-web schemes."""
    if not href:
        return None
    href = href.strip().replace("\n", "").replace("\r", "")
    if not href or href.startswith("#"):
        return None
    lowered = href.lower()
    if lowered.startswith(("mailto:", "tel:", "javascript:", "data:", "cid:", "sms:")):
        return None
    if lowered.startswith("//"):
        href = "https:" + href
    elif not lowered.startswith(("http://", "https://")):
        if lowered.startswith("www."):
            href = "https://" + href
        else:
            return None

    href = _unwrap_redirect(href)
    href = _TRACKING_PARAM_RE.sub("", href)
    href = re.sub(r"\?&", "?", href).rstrip("?&")
    return href[:400]


def _unwrap_redirect(url: str) -> str:
    """Pull the real destination out of a tracking redirector where possible."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    host = (parsed.netloc or "").lower()
    known_wrappers = (
        "click.", "email.", "links.", "link.", "url", "t.", "e.", "mandrillapp.com",
        "sendgrid.net", "list-manage.com", "hubspotlinks.com", "sparkpostmail.com",
    )
    if not any(marker in host for marker in known_wrappers):
        return url
    match = re.search(r"(?:[?&](?:url|u|redirect|target|dest|link|r)=)(https?%3A%2F%2F[^&]+|https?://[^&]+)", url, re.I)
    if match:
        candidate = unquote(match.group(1))
        if candidate.lower().startswith(("http://", "https://")):
            return candidate
    return url


def normalize_whitespace(text: str) -> str:
    """Collapse the runaway whitespace typical of table-based email HTML."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _INVISIBLE_CHARS.sub("", text)
    text = text.replace("\xa0", " ").replace(" ", " ").replace(" ", " ")
    # Tabs are used above as cell separators; keep one at most.
    text = re.sub(r"[ \t]*\t[ \t]*", "\t", text)
    text = re.sub(r"\t{2,}", "\t", text)
    text = re.sub(r"[ ]{2,}", " ", text)
    lines = [line.strip(" \t") for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def find_urls(text: str) -> Tuple[str, ...]:
    """Recover bare URLs from plain-text bodies."""
    found: List[str] = []
    seen: Set[str] = set()
    for match in _URL_RE.finditer(text or ""):
        cleaned = clean_url(match.group(0).rstrip(".,;:!?"))
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            found.append(cleaned)
        if len(found) >= _MAX_LINKS:
            break
    return tuple(found)


def notable_links(links: Iterable[str], domains: Sequence[str] = NOTABLE_DOMAINS) -> Tuple[str, ...]:
    """Subset of links whose host matches a known scheduling/ATS/assessment domain."""
    result: List[str] = []
    for link in links:
        lowered = link.lower()
        for domain in domains:
            if domain in lowered:
                result.append(link)
                break
    return tuple(result)


#: A zero font size: hidden only alongside a second cue (see _ALSO_HIDDEN).
_ZERO_FONT = re.compile(r"font-size:0(?:px|pt|em|rem)?(?:;|$)")

#: What a preheader does as well as shrinking the text. Matched separately so a
#: background colour is not read as a text colour: "background-color:#ffffff"
#: contains "color:#fff".
_ALSO_HIDDEN = re.compile(
    r"(?:^|;)(?:max-height:0|opacity:0|mso-hide:all|height:0(?:px)?)(?:;|$)"
)


#: How many unmatched "<" a document may carry before it is malformed rather
#: than untidy: far above anything in the six thousand messages of the
#: SpamAssassin corpus.
_STRAY_BRACKET_LIMIT = 200

#: How far after a "<" a ">" may be and still plausibly close a tag. Longer
#: than any real tag, short enough that the search stays cheap.
_TAG_WINDOW = 2048


def defuse_stray_brackets(html: str) -> str:
    """Escape "<" characters that no ">" ever closes.

    HTMLParser rescans the rest of the buffer at every "<" it cannot
    complete, which is quadratic: 40,000 of them in a 156 KB body took 22
    seconds, and anyone can send that. A "<" with no ">" after it is no tag
    by any reading, so the entity costs nothing; merely untidy documents are
    left exactly as they are.
    """
    opens = html.count("<")
    if opens - html.count(">") <= _STRAY_BRACKET_LIMIT:
        return html
    out = []
    index = 0
    length = len(html)
    while index < length:
        nxt = html.find("<", index)
        if nxt < 0:
            out.append(html[index:])
            break
        out.append(html[index:nxt])
        closing = html.find(">", nxt + 1, nxt + 1 + _TAG_WINDOW)
        following = html.find("<", nxt + 1)
        # A "<" opens a tag only if its ">" comes before the next "<":
        # otherwise the first of four hundred strays claimed a real anchor's
        # ">" a thousand characters later and swallowed the link.
        stray = closing < 0 or (0 <= following < closing)
        if stray:
            out.append("&lt;")
            index = nxt + 1
        else:
            out.append(html[nxt:closing + 1])
            index = closing + 1
    return "".join(out)


#: Elements dropped with everything inside them: nothing that runs, embeds
#: or takes input belongs in a preview.
_DROP_WHOLE = frozenset(
    "script iframe object applet svg math video audio template noscript "
    "frameset canvas map select textarea".split())
#: Void elements dropped on their own.
_DROP_TAG = frozenset("input link meta base embed source track area param "
                      "frame".split())
#: Elements whose tags go and whose text stays.
_UNWRAP = frozenset("form button label fieldset".split())
_VOID = frozenset("area base br col embed hr img input link meta param "
                  "source track wbr".split())
_SCHEME = re.compile(r"^\s*([a-z][a-z0-9+.-]*):", re.I)
_SAFE_SCHEMES = ("http", "https", "mailto", "tel")
_CSS_URL = re.compile(r"url\s*\([^)]*\)|expression\s*\(", re.I)


def _safe_link(value: str) -> bool:
    """A link a click may open: a web, mail or phone address, or a fragment."""
    found = _SCHEME.match(value.replace("\n", "").replace("\r", "")
                          .replace("\t", ""))
    return found is None or found.group(1).lower() in _SAFE_SCHEMES


_FONT_FAMILY = re.compile(r"font-family\s*:\s*([^;}]+)", re.I)
_GENERIC_FONTS = {"serif", "sans-serif", "monospace", "cursive", "fantasy",
                  "system-ui", "-apple-system", "inherit", "initial"}


def _known_fonts(css: str, families) -> str:
    """``font-family`` declarations kept to the families this machine has:
    asked for one it does not, Qt went looking through every family it
    knows, which took a while and said so."""
    if families is None:
        return css

    def keep(match):
        names = [name.strip().strip("'\"") for name in match.group(1).split(",")]
        kept = [name for name in names
                if name.lower() in _GENERIC_FONTS or name.lower() in families]
        return "font-family: " + ", ".join(kept) if kept else ""

    return _FONT_FAMILY.sub(keep, css)


def pictures_in(html: str) -> list:
    """The pictures a sanitised message asks the view to fetch, once each,
    in order: http and https only, since nothing else gets this far."""
    found = []
    for source in re.findall(r'<img src="([^"]+)"', html):
        source = unescape(source)
        if source.lower().startswith(("http://", "https://")) and source not in found:
            found.append(source)
    return found


class _Cleaner(HTMLParser):
    """Writes the document back out with the dangerous parts left out."""

    def __init__(self, pictures: bool = False, families=None) -> None:
        super().__init__(convert_charrefs=False)
        self.pictures = pictures
        self.families = families
        self.out: List[str] = []
        self._skipping = 0
        self._in_style = False

    def handle_startendtag(self, tag, attrs):
        self._start(tag, attrs, True)

    def handle_starttag(self, tag, attrs):
        self._start(tag, attrs, False)

    def _start(self, tag, attrs, closing: bool) -> None:
        tag = tag.lower()
        void = closing or tag in _VOID
        if self._skipping:
            if not void:
                self._skipping += 1
            return
        if tag in _DROP_WHOLE:
            if not void:
                self._skipping = 1
            return
        if tag in _DROP_TAG or tag in _UNWRAP:
            return
        if tag == "img":
            self.out.append(_image(attrs, self.pictures))
            return
        if tag == "style":
            self._in_style = True
        kept = []
        for name, value in attrs:
            name = name.lower()
            value = value or ""
            if name.startswith("on") or name in (
                    "background", "formaction", "ping", "srcset", "poster",
                    "usemap", "src", "action", "cite", "data", "xlink:href"):
                continue
            if name == "href" and not _safe_link(value):
                continue
            if name == "style":
                value = _known_fonts(_CSS_URL.sub("none", value), self.families)
            if name == "face" and self.families is not None and (
                    value.strip().lower() not in self.families):
                continue
            kept.append(f' {name}="{_attr(value)}"')
        self.out.append(f"<{tag}{''.join(kept)}{' /' if void else ''}>")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _VOID:
            return
        if self._skipping:
            self._skipping -= 1
            return
        if tag in _DROP_WHOLE or tag in _DROP_TAG or tag in _UNWRAP:
            return
        if tag == "style":
            self._in_style = False
        self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if self._skipping:
            return
        if self._in_style:
            data = _known_fonts(_CSS_URL.sub("none", data), self.families)
        self.out.append(data)

    def handle_entityref(self, name):
        if not self._skipping:
            self.out.append(f"&{name};")

    def handle_charref(self, name):
        if not self._skipping:
            self.out.append(f"&#{name};")


def _attr(value: str) -> str:
    return (value.replace("&", "&amp;").replace('"', "&quot;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def _size(found: dict, name: str) -> float:
    try:
        return float(re.sub(r"[^0-9.]", "", found.get(name, "")) or 0)
    except ValueError:
        return 0.0


def _image(attrs, pictures: bool = False) -> str:
    """An embedded image stays. A remote one is kept for the view to fetch
    when pictures are wanted, bar the pixels mail uses to tell its sender it
    was opened; otherwise it is a placeholder, or nothing for a pixel."""
    found = {name.lower(): (value or "") for name, value in attrs}
    source = found.get("src", "").strip()
    alt = found.get("alt", "").strip()
    if source.lower().startswith("data:image/"):
        return f'<img src="{_attr(source)}" alt="{_attr(alt)}" />'
    wide, tall = _size(found, "width"), _size(found, "height")
    if (pictures and source.lower().startswith(("http://", "https://"))
            and not (0.0 < wide <= 2.0 or 0.0 < tall <= 2.0)):
        size = "".join(f' {name}="{int(value)}"' for name, value
                       in (("width", wide), ("height", tall)) if value)
        return f'<img src="{_attr(source)}" alt="{_attr(alt)}"{size} />'
    if alt:
        return f"<span style=\"color:#8a8a8a\">[image: {_attr(alt)}]</span>"
    if wide >= 40.0:
        return "<span style=\"color:#8a8a8a\">[image]</span>"
    return ""


def sanitise_for_view(html: str, pictures: bool = False,
                      families=None) -> str:
    """The message's HTML with scripts, embeds, forms and every remote
    reference taken out, for Qt's document engine to draw, bar the pictures
    when they are wanted. Whatever is left can still not fetch anything of
    its own: the view refuses every resource, and fetches the pictures
    itself."""
    cleaner = _Cleaner(pictures, families)
    cleaner.feed(defuse_stray_brackets(html or ""))
    cleaner.close()
    return "".join(cleaner.out)


def html_to_text(html: str) -> ExtractedText:
    """Reduce an HTML email part to readable text plus its links."""
    if not html:
        return ExtractedText("")
    html = defuse_stray_brackets(html)
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # pragma: no cover - malformed markup must never crash a scan
        stripped = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
        stripped = re.sub(r"(?s)<[^>]+>", " ", stripped)
        text = normalize_whitespace(stripped)
        links = find_urls(html)
        return ExtractedText(text, links, notable_links(links))

    text = normalize_whitespace(parser.result())
    links = list(parser.links)
    seen = set(links)
    for url in find_urls(text):
        if url not in seen and len(links) < _MAX_LINKS:
            seen.add(url)
            links.append(url)
    return ExtractedText(text, tuple(links), notable_links(links))


def plain_to_text(body: str) -> ExtractedText:
    """Normalise a text/plain part and recover its URLs."""
    text = normalize_whitespace(body or "")
    links = find_urls(text)
    return ExtractedText(text, links, notable_links(links))


_HTML_MARKERS = re.compile(
    r"(?is)<!doctype\s+html|<html[\s>]|<body[\s>]|<table[\s>]|<div[\s>]|<meta\s"
)


def looks_like_html(text: str) -> bool:
    """True when a part declared text/plain is really HTML: several
    applicant-tracking systems send whole documents as text/plain.
    """
    if not text:
        return False
    head = text.lstrip()[:400]
    if _HTML_MARKERS.search(head):
        return True
    # Or a body that is mostly tags even without a recognisable opener.
    sample = text[:4000]
    tags = sample.count("<")
    return tags >= 12 and tags > len(sample) / 120


def clean_body(body: str, is_html: bool) -> ExtractedText:
    """Reduce a part to text, ignoring a content type that is plainly wrong."""
    return html_to_text(body) if (is_html or looks_like_html(body)) else plain_to_text(body)


def truncate_for_model(text: str, max_chars: int) -> Tuple[str, bool, int]:
    """Trim to ``max_chars`` at a paragraph or word boundary.

    Returns ``(text, was_truncated, original_length)``. The caller tells
    both the model and the user: silent truncation is how a rejection at the
    bottom of a digest gets misclassified.
    """
    original_length = len(text)
    if max_chars <= 0 or original_length <= max_chars:
        return text, False, original_length

    head = text[:max_chars]
    boundary = max(head.rfind("\n\n"), head.rfind("\n"), head.rfind(". "))
    if boundary > max_chars * 0.6:
        head = head[: boundary + 1]
    return head.rstrip(), True, original_length


#: A quoted reply begins at one of these.
_QUOTE_STARTERS = (
    re.compile(r"(?im)^\s*on .{4,120}\bwrote:\s*$"),
    re.compile(r"(?im)^\s*-{2,}\s*original message\s*-{2,}\s*$"),
    re.compile(r"(?im)^\s*-{2,}\s*forwarded message\s*-{2,}\s*$"),
    re.compile(r"(?im)^\s*from:\s*.{3,120}$\n^\s*sent:\s*", re.M),
    re.compile(r"(?im)^\s*_{5,}\s*$"),
    re.compile(r"(?im)^\s*le .{4,120}\ba ecrit\s*:\s*$"),
    re.compile(r"(?im)^\s*el .{4,120}\bescribio:\s*$"),
)

#: A signature block begins at one of these.
_SIGNATURE_STARTERS = (
    re.compile(r"(?m)^--\s*$"),
    re.compile(r"(?im)^\s*sent from my (iphone|ipad|android|mobile|samsung)\b"),
    re.compile(r"(?im)^\s*get outlook for \w+\s*$"),
)

#: Paragraphs containing any of these are boilerplate, not content.
_FOOTER_MARKERS = (
    "unsubscribe", "manage your preferences", "email preferences",
    "you are receiving this", "you're receiving this", "view this email in your browser",
    "view in browser", "privacy policy", "terms of service", "all rights reserved",
    "confidentiality notice", "intended recipient", "if you received this in error",
    "please do not reply to this", "this message and any attachments",
    "registered office", "company number", "vat number", "opt out",
)


def strip_quoted_replies(text: str) -> str:
    """Drop the quoted history below a reply marker.

    A long thread can be ten times the size of the new message on top of it,
    and the new message is the one being classified.
    """
    if not text:
        return ""
    cut = len(text)
    for pattern in _QUOTE_STARTERS:
        match = pattern.search(text)
        if match and match.start() < cut:
            cut = match.start()
    head = text[:cut]

    # Also drop a run of ">" quoted lines even without a marker.
    lines = head.split("\n")
    kept: List[str] = []
    quoted_run = 0
    for line in lines:
        if line.lstrip().startswith(">"):
            quoted_run += 1
            if quoted_run >= 2:
                continue
        else:
            quoted_run = 0
        kept.append(line)
    return "\n".join(kept).strip()


def strip_signature(text: str) -> str:
    """Remove a trailing signature block."""
    cut = len(text)
    for pattern in _SIGNATURE_STARTERS:
        match = pattern.search(text)
        # Only treat it as a signature if it is in the last third.
        if match and match.start() > len(text) * 0.4 and match.start() < cut:
            cut = match.start()
    return text[:cut].rstrip()


def strip_boilerplate(text: str) -> str:
    """Drop legal, unsubscribe and 'view in browser' paragraphs."""
    if not text:
        return ""
    blocks = re.split(r"\n\s*\n", text)
    kept = []
    for block in blocks:
        lowered = block.lower()
        if len(block) < 400 and any(marker in lowered for marker in _FOOTER_MARKERS):
            continue
        kept.append(block)
    return "\n\n".join(kept).strip() or text.strip()


def collapse_repeats(text: str) -> str:
    """Drop consecutive duplicate lines, common in table-built mail."""
    out: List[str] = []
    previous = None
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped and stripped == previous:
            continue
        previous = stripped
        out.append(line)
    return "\n".join(out)


def condense(text: str) -> str:
    """Reduce a body to the part worth paying tokens for."""
    if not text:
        return ""
    result = strip_quoted_replies(text)
    result = strip_boilerplate(result)
    result = strip_signature(result)
    result = collapse_repeats(result)
    return normalize_whitespace(result) or normalize_whitespace(text)


def head_and_tail(text: str, max_chars: int, head_share: float = 0.65) -> Tuple[str, bool, int]:
    """Keep the opening and the closing of a long body.

    Head-only truncation loses exactly the wrong thing: the call to action, the
    deadline and the sign-off all live at the end. Returns
    ``(text, was_truncated, original_length)``.
    """
    original_length = len(text)
    if max_chars <= 0 or original_length <= max_chars:
        return text, False, original_length

    marker = "\n\n[... middle of this message omitted ...]\n\n"
    budget = max(0, max_chars - len(marker))
    head_budget = int(budget * head_share)
    tail_budget = budget - head_budget

    head = text[:head_budget]
    boundary = max(head.rfind("\n\n"), head.rfind("\n"), head.rfind(". "))
    if boundary > head_budget * 0.6:
        head = head[: boundary + 1]

    tail = text[-tail_budget:] if tail_budget > 0 else ""
    boundary = min(
        (index for index in (tail.find("\n\n"), tail.find("\n"), tail.find(". "))
         if index != -1),
        default=-1,
    )
    if 0 <= boundary < tail_budget * 0.4:
        tail = tail[boundary + 1:]

    return (head.rstrip() + marker + tail.lstrip()), True, original_length
