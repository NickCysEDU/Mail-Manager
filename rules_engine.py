"""A local classifier for job-search email, with no model.

A hand-built rule set, not a trained model: the phrases, sender shapes, link
domains and structural tells that separate a rejection from a receipt, as
weighted signals that can be read and tested. It is the fallback when a
model backend is unavailable, the private and free default, and a second
opinion beside a model.

Mail arrives with mojibake ("weâ€™ve"), smart quotes, accents, zero-width
padding, homoglyphs, broken hyphenation and spaced-out words ("i n t e r v i
e w"), so every pattern is matched against a readable normalisation and a
"tight" one with all punctuation and spacing removed. Confidence is capped
below the auto-file threshold except on overwhelming evidence, so routing
still does the safety work.
"""

from __future__ import annotations

import html
import math
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import lexicon
import statements
from models import Category, OtherCategory

#: The most confidence this engine gives: even a textbook rejection could be
#: quoted inside another message.
MAX_CONFIDENCE = 0.96
#: Below this total score nothing is claimed at all.
MIN_SCORE = 1.6
#: Score at which "more evidence" stops increasing confidence.
SATURATION = 5.0
#: A signal at or above this weight is decisive on its own.
DECISIVE_WEIGHT = 3.0
#: Evidence a category needs before it enters the precedence contest.
QUALIFY_SCORE = 2.5
#: UNSOLICITED comes first in precedence, so it must clear a higher bar, or one
#: eager phrase would outrank a real interview invitation.
QUALIFY_SCORE_UNSOLICITED = 3.5
#: The share of the strongest category's score a category needs for the
#: precedence order to apply between them.
DWARFED = 0.4
#: Multiplier applied when a phrase matched only with words inserted into it.
GAPPED_PENALTY = 0.75
#: What a decision, step, meeting or acknowledgement stated in so many words
#: is worth: decisive, since one sentence is how hiring mail says what it is.
STATED_WEIGHT = 3.2

#: What "next steps" is worth, and so what comes off when every mention is a
#: promise rather than a request.
PROMISED_STEPS_WEIGHT = 1.6

#: The most a reading resting mainly on shape (the mailbox, a flight number,
#: how two people write) can be trusted: below the filing threshold, so it
#: sorts but never moves mail unasked.
SOFT_EVIDENCE_CEILING = 0.90

#: How much of a message the rules read: what a message is gets settled early,
#: and beyond this is quoted threads, footers and boilerplate.
MAX_SCANNED_CHARS = 20000

#: How much to discount transactional topics on mail with an unsubscribe
#: header: lightly when the sender vouches for the topic (a courier, an
#: airline, a bank on its own domain), more when nothing corroborates the
#: wording, as with marketing that borrows travel or shopping language.
TRANSACTIONAL_IN_BULK = 0.72
TRANSACTIONAL_IN_BULK_UNVOUCHED = 0.45

#: Topics that describe a transaction rather than a broadcast.
TRANSACTIONAL_TOPICS: Tuple["OtherCategory", ...] = ()


# UTF-8 read as Latin-1, the commonest corruption in forwarded mail.
_MOJIBAKE = {
    "â€™": "'", "â€˜": "'", "â€œ": '"', "â€\x9d": '"', "â€“": "-", "â€”": "-",
    "â€¦": "...", "â€¢": "-", "Â ": " ", "Ã©": "e", "Ã¨": "e", "Ã¡": "a",
    "Ã­": "i", "Ã³": "o", "Ãº": "u", "Ã±": "n", "Ã§": "c", "â€": '"',
}

#: Letters from other scripts that look identical in a Latin word, used to slip
#: past keyword filters.
_HOMOGLYPHS = {
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c",
    "у": "y", "х": "x", "і": "i", "ј": "j", "һ": "h",
    "ο": "o", "α": "a", "ε": "e", "ρ": "p", "υ": "u",
    "ԁ": "d", "ԛ": "q", "ɡ": "g", "ᴏ": "o", "ⁱ": "i",
}

#: Letters NFKD leaves alone. "Grüße" folds to "gruße" without this.
_TRANSLITERATE = {
    "ß": "ss", "æ": "ae", "œ": "oe", "ø": "o", "å": "a", "đ": "d",
    "ð": "d", "þ": "th", "ł": "l", "ı": "i", "ħ": "h", "ŋ": "ng",
}

_ZERO_WIDTH = re.compile("[​-‏ - ⁠-⁤﻿­᠎]")
_PUNCT_RUN = re.compile(r"[^\w\s]{3,}")
#: Any run of six or more characters that are not letters or digits: a rule of
#: underscores, a row of dashes. The gapped matcher allows separators between a
#: phrase's words, and a long run can be divided between the gaps in
#: exponentially many ways (seventy underscores took forty-three seconds), so
#: runs are collapsed. Six, because ordinary punctuation between words reaches
#: four, and decoration is longer.
_GAP_RUN = re.compile(r"[^a-z0-9]{6,}")
_SPACED_OUT = re.compile(r"(?:(?<=\s)|^)(?:[a-z]\s){3,}[a-z](?=\s|$)")


def normalize(text: str) -> str:
    """Fold real-world mail into something patterns can match: mojibake,
    homoglyphs, accents, zero-width padding, words hyphenated across lines,
    and spaced-out words.
    """
    if not text:
        return ""
    result = text
    for broken, fixed in _MOJIBAKE.items():
        if broken in result:
            result = result.replace(broken, fixed)

    # Some senders double-encode, so "&amp;nbsp;" arrives and decodes to the
    # literal "&nbsp;". Unescaping twice clears both layers.
    if "&" in result:
        result = html.unescape(html.unescape(result))

    result = unicodedata.normalize("NFKC", result)
    if any(char in _HOMOGLYPHS for char in result):
        result = "".join(_HOMOGLYPHS.get(char, char) for char in result)

    # Strip accents so "résumé"/"resume" and "Grüße"/"Grusse" both match.
    result = "".join(
        char for char in unicodedata.normalize("NFKD", result)
        if not unicodedata.combining(char)
    )
    if any(char in _TRANSLITERATE for char in result.lower()):
        result = "".join(
            _TRANSLITERATE.get(char.lower(), char) for char in result
        )

    result = _ZERO_WIDTH.sub("", result)
    result = (
        result.replace("‘", "'").replace("’", "'")
        .replace("“", '"').replace("”", '"')
        # Escaped so the dash characters survive a source-wide tidy-up.
        .replace("\u2013", "-").replace("\u2014", "-")
        .replace(" ", " ")
    )
    result = result.lower()

    # "for-\nward" -> "forward"
    result = re.sub(r"-\s*\n\s*", "", result)
    result = _PUNCT_RUN.sub(" ", result)
    result = _GAP_RUN.sub(" ", result)
    result = _SPACED_OUT.sub(lambda m: m.group(0).replace(" ", ""), result)
    return re.sub(r"\s+", " ", result).strip()


#: Where the footer of a long message is taken to begin, as a share of it,
#: and how long a body must be to have one.
FOOTER_STARTS = 0.6
FOOTER_MIN_BODY = 800
#: What a topic phrase found only in the footer is worth.
FOOTER_SHARE = 0.5


def _head_and_footer(body: str) -> Tuple[str, str]:
    """A long normalised body split where its footer is taken to begin, at a
    word; a short one is all head."""
    if len(body) < FOOTER_MIN_BODY:
        return body, ""
    cut = body.rfind(" ", 0, int(len(body) * FOOTER_STARTS))
    if cut <= 0:
        return body, ""
    return body[:cut], body[cut:]


#: Where an applicant-tracking system starts echoing back the form you filled
#: in: what follows is your own answers and its questions ("When are you
#: available to start?"), not anything it asks of you now.
_ECHO = re.compile(
    r"(?i)\b(?:here(?:'|\u2019)?s|here is|below is|attached is|we(?:'|\u2019)?ve "
    r"included|you(?:'|\u2019)?ll find) a copy of (?:your|the) (?:application|answers|"
    r"responses|submission)|\b(?:a )?copy of your application(?: data)? for "
    r"(?:safekeeping|your records)")


def _without_the_echo(body: str) -> str:
    """The body up to and including the sentence that introduces a copy of
    your own application, without the copy."""
    found = _ECHO.search(body or "")
    if not found:
        return body
    end = re.search(r"[.!?\n]", body[found.end():])
    return body[:found.end() + (end.end() if end else 0)]


def tighten(text: str) -> str:
    """Everything but letters and digits removed, for evasion-proof matching."""
    return re.sub(r"[^a-z0-9]+", "", normalize(text))


def _loose(phrase: str) -> str:
    """A regex matching ``phrase`` with any punctuation or spacing between
    words. The joiner is an atomic group: otherwise a long run of separators
    can be divided between the gaps in exponentially many ways, all tried.
    """
    words = [re.escape(word) for word in phrase.split()]
    return r"\b" + r"[\W_]{0,4}".join(words)


def _gapped(phrase: str, max_inserted: int = 2) -> Optional[str]:
    """A regex allowing a couple of extra words inside the phrase, since
    sentences interleave ("your **September** statement is ready"). Only for
    phrases of three or more words; two-word phrases with gaps match too
    eagerly.
    """
    words = [re.escape(word) for word in phrase.split()]
    if len(words) < 3:
        return None
    # A repetition inside a repetition, which can backtrack catastrophically;
    # it is safe because normalize() leaves no long run of separators. Bounding
    # it here would cost accuracy.
    gap = r"(?:[\W_]+\w+){0,%d}[\W_]+" % max_inserted
    return r"\b" + gap.join(words) + r"\b"


@dataclass(frozen=True)
class Signal:
    """One piece of evidence for a category."""

    phrase: str
    weight: float
    #: Where to look: "body", "subject", "any" (subject + body), "sender".
    field: str = "any"
    #: Human-readable name used in the reasoning text.
    label: str = ""

    def describe(self) -> str:
        return self.label or f"“{self.phrase}”"


class _Matcher:
    """Compiled exact, gapped and tight forms of a signal's phrase."""

    __slots__ = ("signal", "loose", "gapped", "tight", "anchor")

    #: An anchor shorter than this is in almost every message, so testing for
    #: it saves nothing.
    MIN_ANCHOR = 5

    def __init__(self, signal: Signal) -> None:
        self.signal = signal
        self.loose = re.compile(_loose(signal.phrase))
        pattern = _gapped(signal.phrase)
        self.gapped = re.compile(pattern) if pattern else None
        tight = re.sub(r"[^a-z0-9]+", "", signal.phrase.lower())
        self.tight = tight if len(tight) >= 8 else ""
        self.anchor = self._anchor(signal.phrase)

    @staticmethod
    def _anchor(phrase: str) -> str:
        """The longest word of the phrase as it appears in a tightened blob.
        Both pattern kinds join whole words with separators, so a matching
        phrase leaves every word intact in the tightened text: if the
        longest is absent, nothing can match, and a substring test settles
        it cheaply. It only ever says "definitely not".
        """
        words = [re.sub(r"[^a-z0-9]+", "", word.lower())
                 for word in phrase.split()]
        longest = max(words, key=len, default="")
        return longest if len(longest) >= _Matcher.MIN_ANCHOR else ""

    def hit(self, normalized: str, tightened: str) -> float:
        """Return a weight multiplier: 1.0 exact, 0.75 gapped, 0.0 no match."""
        if self.anchor and self.anchor not in tightened:
            return 0.0
        return self._hit(normalized, tightened)

    def sender_hit(self, sender: str) -> float:
        """Match against a sender, which arrives untightened, so the prefilter
        is skipped: an address is short, and the tightened-text test would
        reject real matches.
        """
        return self._hit(sender, sender)

    def _hit(self, normalized: str, tightened: str) -> float:
        if self.loose.search(normalized):
            return 1.0
        if self.tight and self.tight in tightened:
            return 1.0
        if self.gapped is not None and self.gapped.search(normalized):
            return GAPPED_PENALTY
        return 0.0


# Weights: 3.0 decisive, 2.0 strong, 1.2 moderate, 0.6 supporting. Phrases
# match loosely, so "move forward" also matches "move-forward" and
# "moveforward".

REJECTION_SIGNALS: Tuple[Signal, ...] = (
    Signal("move forward with other candidates", 3.0),
    Signal("moving forward with other candidates", 3.0),
    Signal("proceed with other candidates", 3.0),
    Signal("pursue other candidates", 3.0),
    Signal("pursuing other applicants", 3.0),
    Signal("decided not to move forward", 3.0),
    Signal("not be moving forward", 3.0),
    Signal("will not be moving ahead", 3.0),
    Signal("not moving ahead with your application", 3.0),
    Signal("decided not to proceed", 3.0),
    Signal("not to proceed with your application", 3.0),
    Signal("no longer under consideration", 3.0),
    Signal("not be progressing", 2.6),
    Signal("not progressing your application", 3.0),
    Signal("we regret to inform", 2.6),
    Signal("regret to inform you", 3.0),
    Signal("unsuccessful on this occasion", 3.0),
    Signal("were not successful", 2.2),
    Signal("you have not been selected", 3.0),
    Signal("not been shortlisted", 2.8),
    Signal("chosen another candidate", 2.8),
    Signal("selected another candidate", 2.8),
    Signal("gone with another candidate", 2.6),
    Signal("position has been filled", 2.6),
    Signal("role has been filled", 2.6),
    # The same news in the active voice; a literal list does not see past word
    # order.
    Signal("filled the position", 2.6),
    Signal("filled the role", 2.6),
    Signal("filled this position", 2.6),
    Signal("we have filled", 2.4),
    Signal("with another candidate", 2.6),
    Signal("with a different candidate", 2.6),
    Signal("another candidate was selected", 3.0),
    Signal("another candidate has been selected", 3.0),
    Signal("offer to another candidate", 3.0),
    Signal("hired another candidate", 2.8),
    Signal("someone whose experience", 2.0),
    Signal("candidates whose qualifications more closely", 3.0),
    Signal("decided to move forward with other", 3.0),
    Signal("moving forward with another", 3.0),
    Signal("progressing with other candidates", 3.0),
    Signal("not able to offer you", 2.6),
    Signal("unable to consider your application", 3.0),
    Signal("not able to consider your application", 3.0),
    Signal("cannot consider your application", 3.0),
    Signal("no longer able to consider", 3.0),
    Signal("unable to offer you a position", 3.0),
    Signal("will not be extending an offer", 3.0),
    Signal("we have decided not to", 2.4),
    Signal("this position is now closed", 2.2),
    Signal("we have closed this role", 2.2),
    Signal("keep your resume on file", 1.8),
    Signal("keep your details on file", 1.8),
    Signal("wish you the best in your search", 1.6),
    Signal("wish you every success", 1.4),
    Signal("we will not be pursuing", 2.6),
    Signal("your application was not successful", 3.0),
    Signal("after careful consideration", 1.4),
    Signal("more closely matched", 1.4),
    Signal("better aligned with our needs", 1.4),
    Signal("withdraw your application", 2.0),
    Signal("application has been withdrawn", 2.4),
    # The polite opener "thank you for your interest" is shared with
    # acknowledgements, so the decisive phrase is always further in.
    Signal("we have moved forward with other candidates", 3.0),
    Signal("moved forward with other candidates", 3.0),
    Signal("more closely match the listed requirements", 3.0),
    Signal("more closely match our current needs", 3.0),
    Signal("closely match the requirements", 2.4),
    Signal("candidates that were further along", 3.0),
    Signal("further along in the process", 2.8),
    Signal("we just filled this position", 3.0),
    Signal("this position has been filled", 3.0),
    Signal("the position here at", 1.2),
    Signal("has been filled", 2.6),
    Signal("we are not moving forward with your application", 3.0),
    Signal("not moving forward with your application", 3.0),
    Signal("we do not have a match", 3.0),
    Signal("don t have a match for your", 3.0),
    Signal("do not have a match for your", 3.0),
    Signal("keep your resume on hand", 2.8),
    Signal("keep your resume on file", 2.4),
    Signal("we are unable to offer you", 3.0),
    Signal("unable to offer you this position", 3.0),
    Signal("unable to move forward", 3.0),
    Signal("we hope to stay connected", 2.2),
    Signal("you do not meet the minimum qualifications", 3.0),
    Signal("do not meet the minimum qualifications", 3.0),
    Signal("does not meet the requirements", 2.8),
    Signal("after careful review of your resume", 2.4),
    Signal("please continue to visit our careers", 2.4),
    Signal("new positions are posted daily", 2.4),
    Signal("encourage you to apply for other", 2.4),
    Signal("apply to other roles", 2.0),
    Signal("we have selected other applicants", 3.0),
    Signal("other applicants whose", 2.6),
    Signal("we will not be progressing", 3.0),
    Signal("your application will not be", 2.6),
    Signal("decided to pursue other", 3.0),
    Signal("we have filled the role", 3.0),
    Signal("role is no longer available", 2.8),
    Signal("no longer being considered", 3.0),
    Signal("not be considered further", 2.8),
    Signal("we appreciate your interest but", 2.6),
    Signal("although we are not", 2.4),
    Signal("while we are unable", 2.6),
    Signal("at this time we have", 1.6),
    Signal("best of luck in your search", 2.0),
    Signal("best of luck with your job search", 2.4),
    Signal("wish you well in your search", 2.2),
    # Other languages, for the highest-value verdict.
    Signal("no continuaremos con tu candidatura", 2.8, label="Spanish rejection"),
    Signal("hemos decidido continuar con otros candidatos", 2.8, label="Spanish rejection"),
    Signal("ne donnerons pas suite", 2.8, label="French rejection"),
    Signal("votre candidature n a pas ete retenue", 2.8, label="French rejection"),
    Signal("leider absagen", 2.8, label="German rejection"),
    Signal("wir haben uns fur einen anderen", 2.8, label="German rejection"),
    Signal("nao seguiremos com sua candidatura", 2.8, label="Portuguese rejection"),
)

OFFER_SIGNALS: Tuple[Signal, ...] = (
    # Supporting, not decisive: "happy to offer you a place in our talent
    # network" is a mailing list. What is offered being the job is read by
    # statements.py, which makes a real offer decisive.
    Signal("pleased to offer you", 1.6),
    Signal("delighted to offer you", 1.6),
    Signal("happy to offer you", 1.6),
    Signal("we would like to offer you", 1.6),
    Signal("extend an offer", 3.0),
    Signal("extending an offer", 3.0),
    # Decisive as a subject; in a body it is as often "any offer of employment
    # is contingent on", in the footer of everything an employer sends.
    Signal("offer of employment", 3.0, field="subject"),
    Signal("offer of employment", 1.4, field="body"),
    Signal("offer letter", 2.8),
    Signal("your offer", 1.6),
    Signal("employment agreement", 2.0),
    Signal("compensation package", 2.4),
    Signal("total compensation", 2.0),
    Signal("base salary", 2.0),
    Signal("signing bonus", 2.2),
    Signal("equity grant", 2.2),
    Signal("stock options", 1.8),
    Signal("rsus", 1.8),
    Signal("start date", 1.2),
    Signal("proposed start date", 2.0),
    Signal("accept the offer", 2.4),
    Signal("accepting this offer", 2.4),
    Signal("countersign", 2.0),
    Signal("offer expires", 2.4),
    Signal("respond by", 0.8),
    Signal("welcome to the team", 1.8),
    Signal("congratulations", 1.0),
    Signal("oferta de empleo", 2.6, label="Spanish offer"),
    Signal("proposition d embauche", 2.6, label="French offer"),
)

INTERVIEW_SIGNALS: Tuple[Signal, ...] = (
    Signal("schedule an interview", 3.0),
    Signal("schedule a call", 2.6),
    Signal("set up a call", 2.4),
    Signal("set up some time", 2.2),
    Signal("book a time", 2.6),
    Signal("pick a time", 2.6),
    Signal("choose a time", 2.4),
    Signal("find a time", 2.0),
    Signal("grab some time", 2.0),
    Signal("your availability", 2.4),
    Signal("let me know your availability", 2.8),
    Signal("when are you available", 2.6),
    Signal("times that work for you", 2.6),
    Signal("interview invitation", 3.0),
    Signal("invitation to interview", 3.0),
    Signal("invite you to interview", 3.0),
    Signal("like to interview you", 3.0),
    Signal("phone screen", 2.8),
    Signal("initial screen", 2.2),
    Signal("recruiter screen", 2.4),
    Signal("technical interview", 2.8),
    Signal("onsite interview", 2.8),
    Signal("on site interview", 2.8),
    Signal("panel interview", 2.8),
    Signal("final round", 2.4),
    Signal("next round", 2.0),
    Signal("hiring manager chat", 2.2),
    Signal("meet the team", 1.8),
    Signal("video interview", 2.6),
    Signal("one way interview", 2.4),
    Signal("hiring event", 2.6),
    Signal("recruiting event", 2.4),
    Signal("career fair", 2.0),
    Signal("open house", 1.6),
    Signal("invite you to our", 1.2),
    Signal("interview confirmed", 2.8),
    Signal("interview scheduled", 2.8),
    Signal("reschedule your interview", 2.8),
    Signal("your interview is", 2.4),
    Signal("looking forward to speaking", 1.4),
    Signal("speak with you about the role", 2.2),
    Signal("30 minutes", 0.8),
    Signal("45 minutes", 0.8),
    Signal("entrevista", 2.2, label="Spanish interview"),
    Signal("entretien", 2.2, label="French interview"),
    Signal("vorstellungsgesprach", 2.2, label="German interview"),
)

#: Interview phrases any meeting could use: a dentist asks for your
#: availability, a sales team books a slot. Everything else names a hiring
#: process outright ("phone screen") and needs no corroboration. Discounting
#: all interview evidence without other job wording threw away the clearest
#: signals there are.
GENERIC_SCHEDULING: frozenset = frozenset({
    "schedule a call", "set up a call", "set up some time", "book a time",
    "pick a time", "choose a time", "find a time", "grab some time",
    "your availability", "let me know your availability",
    "when are you available", "times that work for you",
    "looking forward to speaking", "meet the team", "30 minutes", "45 minutes",
    "open house", "invite you to our",
})

#: Interview phrases that name a hiring process and so speak for themselves.
HIRING_SPECIFIC_SIGNALS: Tuple[Signal, ...] = tuple(
    signal for signal in INTERVIEW_SIGNALS
    if signal.phrase not in GENERIC_SCHEDULING
)

NEXT_STEPS_SIGNALS: Tuple[Signal, ...] = (
    Signal("coding assessment", 3.0),
    Signal("technical assessment", 3.0),
    Signal("online assessment", 3.0),
    Signal("take home", 2.8),
    Signal("take home assignment", 3.0),
    Signal("coding challenge", 3.0),
    Signal("coding exercise", 2.8),
    Signal("skills test", 2.4),
    Signal("please complete", 2.4),
    Signal("complete the following", 2.4),
    Signal("complete this assessment", 3.0),
    Signal("complete within", 2.0),
    Signal("questionnaire", 2.2),
    Signal("pre screening questions", 2.6),
    Signal("screening questions", 2.4),
    Signal("a few questions", 1.4),
    Signal("provide references", 2.8),
    Signal("professional references", 2.8),
    Signal("reference check", 2.6),
    Signal("background check", 2.6),
    Signal("right to work", 2.2),
    Signal("work authorization", 2.2),
    Signal("visa status", 2.0),
    Signal("upload your", 2.0),
    Signal("fill out the form", 1.8),
    Signal("complete your profile", 2.2),
    Signal("submit your application", 2.0),
    Signal("finish your application", 2.4),
    Signal("action required", 1.8),
    Signal("next steps", 1.6),
    Signal("expires in", 1.4),
    Signal("due by", 1.2),
    Signal("within 5 days", 1.6),
    Signal("within 48 hours", 1.6),
    Signal("send us your", 1.8),
    Signal("attach your resume", 2.0),
    Signal("share your portfolio", 2.2),
    # Learned from real applicant-tracking mail.
    Signal("you have not yet submitted", 3.0),
    Signal("this is a reminder that you have not", 3.0),
    Signal("reminder that you have not yet", 3.0),
    Signal("ondemand interview", 3.0),
    Signal("on demand interview", 3.0),
    Signal("get started", 1.2),
    Signal("verify your candidate account", 3.0),
    Signal("candidate account", 2.0),
    Signal("confirm your email address and complete", 3.0),
    Signal("complete setup for your", 2.8),
    Signal("the link will expire", 2.4),
    Signal("link expires in", 2.4),
    Signal("finish setting up your account", 2.8),
    Signal("activate your account", 2.4),
    Signal("your application is incomplete", 3.0),
    Signal("incomplete application", 2.8),
    Signal("partially completed application", 2.8),
    Signal("return to the job application", 2.6),
    Signal("return to your application", 2.6),
    Signal("resume your application", 2.6),
    Signal("complete your application", 2.4),
    Signal("started your application", 1.6),
    Signal("additional information is needed", 2.8),
    Signal("please respond by", 2.4),
)

APPLICATION_RECEIVED_SIGNALS: Tuple[Signal, ...] = (
    Signal("thank you for applying", 3.0),
    Signal("thanks for applying", 3.0),
    Signal("we have received your application", 3.0),
    Signal("we received your application", 3.0),
    Signal("your application has been received", 3.0),
    Signal("application received", 2.6),
    Signal("received your resume", 2.8),
    Signal("received your cv", 2.8),
    Signal("successfully submitted", 2.6),
    Signal("application was submitted", 2.6),
    # Shared with rejections, so it is context rather than evidence.
    Signal("thank you for your interest in", 0.6),
    Signal("thanks for your interest in", 0.6),
    Signal("thank you for applying to", 2.4),
    Signal("thank you for taking the time to apply", 2.4),
    Signal("we appreciate you applying", 2.6),
    Signal("your application for", 1.2),
    Signal("has been received", 2.6),
    Signal("we have your application", 2.6),
    Signal("application confirmation", 2.8),
    Signal("confirming receipt of your", 3.0),
    Signal("receipt of your application", 3.0),
    Signal("this confirms", 1.8),
    Signal("your submission has been", 2.4),
    Signal("has been successfully submitted", 3.0),
    Signal("we will review your qualifications", 2.6),
    Signal("our recruiting team will review", 2.6),
    Signal("if there is a match", 2.0),
    Signal("should your qualifications", 2.2),
    Signal("we keep every application", 2.0),
    Signal("no further action is needed", 2.6),
    Signal("no action is required", 2.6),
    Signal("application submitted for", 3.0),
    Signal("profile submitted to", 3.0),
    Signal("we have received the profile you submitted", 3.0),
    Signal("the profile you submitted", 2.8),
    Signal("thank you for taking the time to submit", 2.8),
    Signal("submit your application for", 1.6),
    Signal("we will contact you to discuss", 2.4),
    Signal("if your profile matches", 2.6),
    Signal("if your qualifications match", 2.6),
    Signal("your application is in our system", 2.8),
    Signal("we are reviewing applications", 2.4),
    Signal("talent acquisition team", 1.4),
    Signal("we are reviewing your application", 2.8),
    Signal("your application is under review", 2.8),
    Signal("currently reviewing applications", 2.4),
    Signal("our team will review", 2.2),
    Signal("if your background is a match", 2.2),
    Signal("we will be in touch", 1.4),
    Signal("do not reply to this", 1.0),
    Signal("this is an automated", 1.2),
    Signal("application status", 1.4),
    Signal("gracias por postular", 2.4, label="Spanish acknowledgement"),
    Signal("merci pour votre candidature", 2.4, label="French acknowledgement"),
    Signal("vielen dank fur ihre bewerbung", 2.4, label="German acknowledgement"),
)

#: Phrases a form, a lender, a shop, a club or a friend writes as readily as
#: an employer. Once a message is about hiring they help say which stage it
#: is; they never make it about hiring. Alone, "please complete your annual
#: review", "upload your two most recent pay stubs" and "this exclusive offer
#: expires Sunday" each read as job mail.
GENERIC_STAGE_PHRASES: frozenset = GENERIC_SCHEDULING | frozenset({
    "please complete", "complete the following", "complete within",
    "questionnaire", "a few questions", "visa status", "upload your",
    "fill out the form", "complete your profile", "submit your application",
    "finish your application", "action required", "next steps", "expires in",
    "due by", "within 5 days", "within 48 hours", "send us your", "get started",
    "the link will expire", "link expires in", "finish setting up your account",
    "activate your account", "complete setup for your",
    "additional information is needed", "please respond by",
    "complete your application", "started your application",
    "your application is incomplete", "incomplete application",
    "partially completed application", "return to your application",
    "resume your application", "background check", "reference check",
    "congratulations", "start date", "respond by", "offer expires",
    "welcome to the team", "your offer", "accept the offer", "accepting this offer",
    # A friend's ordinary kindnesses, which are networking only when the
    # message is about work: "let me introduce you to free software".
    "introduce you to", "make an introduction", "connect you with", "grab a coffee",
    "grab coffee", "catch up", "thought of you", "passing along",
    "let me know if you want me to", "might be a good fit for",
    # What every service notice says of a payment, a return or an outage.
    "no action is required", "no further action is needed", "has been received",
    "this confirms",
})

NETWORKING_SIGNALS: Tuple[Signal, ...] = (
    Signal("happy to refer you", 3.0),
    Signal("refer you internally", 3.0),
    Signal("put in a referral", 3.0),
    Signal("submit a referral", 2.8),
    Signal("referral for you", 2.6),
    Signal("introduce you to", 2.6),
    Signal("make an introduction", 2.6),
    Signal("connect you with", 2.4),
    Signal("put in a good word", 2.8),
    Signal("informational interview", 2.8),
    Signal("informational chat", 2.8),
    Signal("pick your brain", 2.6),
    Signal("grab a coffee", 2.0),
    Signal("grab coffee", 2.0),
    Signal("catch up", 1.2),
    Signal("inside view of the team", 2.2),
    Signal("no formal opening", 2.4),
    Signal("not posted yet", 2.2),
    Signal("thought of you", 1.8),
    Signal("passing along", 1.6),
    Signal("might be a good fit for", 1.6),
    Signal("let me know if you want me to", 1.6),
)

UNSOLICITED_SIGNALS: Tuple[Signal, ...] = (
    Signal("came across your profile", 3.0),
    Signal("came across your resume", 3.0),
    Signal("found your profile", 3.0),
    Signal("stumbled upon your profile", 3.0),
    Signal("your profile caught my eye", 3.0),
    Signal("i am reaching out because", 1.8),
    Signal("reaching out to see if", 1.8),
    Signal("exciting opportunity", 2.4),
    Signal("great opportunity", 2.0),
    Signal("urgent requirement", 2.8),
    Signal("immediate joiner", 3.0),
    Signal("immediate start", 2.0),
    Signal("hot requirement", 2.8),
    Signal("our client is looking", 2.8),
    Signal("one of our clients", 2.4),
    Signal("my client is", 2.4),
    Signal("c2c", 2.6),
    Signal("corp to corp", 2.8),
    Signal("w2 only", 2.6),
    Signal("rate is", 1.4),
    Signal("hourly rate", 1.6),
    Signal("would you be open to", 2.0),
    Signal("are you open to new opportunities", 2.6),
    Signal("not sure if you are looking", 2.4),
    Signal("if you are not interested", 1.8),
    Signal("please share your updated resume", 2.6),
    Signal("send me your updated cv", 2.6),
    Signal("kindly revert", 2.2),
    Signal("do the needful", 2.2),
    Signal("staffing", 1.4),
    Signal("consultancy", 1.2),
    Signal("recruitment agency", 1.8),
)

#: Sender-address fragments that make unsolicited outreach more likely.
AGENCY_SENDER_HINTS: Tuple[str, ...] = (
    "staffing", "recruit", "talentacquisition", "consultanc", "resourcing",
    "manpower", "placements", "headhunt", "techjobs", "itjobs", "hiring",
)

#: Link domains that are decisive evidence for a specific category.
SCHEDULING_LINK_DOMAINS: Tuple[str, ...] = (
    "calendly.com", "cal.com", "savvycal.com", "meetings.hubspot.com",
    "hubspot.com/meetings", "chilipiper.com", "goodtime.io", "youcanbook.me",
    "acuityscheduling.com", "doodle.com", "when2meet.com", "calendarhero.com",
    # Google's and Microsoft's own booking pages.
    "calendar.app.google", "calendar.google.com", "bookings.microsoft.com",
    "outlook.office.com/bookwithme", "outlook.office365.com/book",
    "koalendar.com", "tidycal.com", "zcal.co", "usemotion.com", "clara.com",
    "appointlet.com", "setmore.com", "vcita.com", "book.morgen.so",
    "zoom.us", "teams.microsoft.com", "meet.google.com", "whereby.com",
    "hirevue.com", "sparkhire.com", "spark.hire", "willo.video",
    "modernhire.com", "vidcruiter.com", "loom.com",
)
#: The words a hiring process cannot be written without. A meeting proposed
#: with none of them is about work, but not a step of anybody's application.
_HIRING_ANCHOR = re.compile(
    r"\b(?:interview\w*|positions?|roles?|openings?|vacanc\w+|jobs?|applications?|"
    r"apply|applied|applying|candidate\w*|candidacy|recruit\w*|hiring|hire|resume|cv|"
    r"requisition|req|talent acquisition|phone screen|screening call|offer)\b")

#: The same, less the words a university or a lender uses too.
_HIRING_WORDS = re.compile(
    r"\b(?:interview\w*|positions?|roles?|openings?|vacanc\w+|jobs?|candidate\w*|"
    r"candidacy|recruit\w*|hiring|hire|resume|cv|requisition|req|talent acquisition|"
    r"employment|employer|career\w*|hr|human resources|salary|stock options|"
    r"offer letter|onboarding)\b")

#: A job applied for, by its title: "thanks for applying for the IT
#: generalist spot" says hiring with none of the words above.
_APPLIED_FOR_A_TITLE = re.compile(
    r"\b(?:apply|applying|applied|application)\s+(?:for|to)\s+(?:the|a|an|our|your|this)\s+"
    r"(?:[\w/+-]+\s+){0,3}?(?:technician|engineer|analyst|developer|administrator|admin|"
    r"specialist|generalist|manager|coordinator|assistant|intern|internship|associate|"
    r"consultant|designer|architect|lead|officer|representative|clerk|operator|"
    r"programmer|tech)\b")
#: The part of a school, a hospital or a court that hires: its own people
#: team writes about its own vacancies.
_EMPLOYER_SIDE = re.compile(
    r"\b(?:hr|human resources|careers?|recruit\w*|talent|employment|staffing|personnel)\b")

#: One-way video interview platforms: an employer's questions, recorded
#: answers. What they ask for is an interview, however the reminder words it.
VIDEO_INTERVIEW_DOMAINS: Tuple[str, ...] = (
    "hirevue.com", "modernhire.com", "sparkhire.com", "spark.hire", "willo.video",
    "vidcruiter.com", "myinterview.com", "hireflix.com",
)
ASSESSMENT_LINK_DOMAINS: Tuple[str, ...] = (
    "hackerrank.com", "codesignal.com", "codility.com", "karat.com",
    "coderbyte.com", "devskiller.com", "testgorilla.com", "woven.teams",
    "triplebyte.com", "mettl.com", "imocha.io", "qualified.io", "coderpad.io",
    "byteboard.dev", "filtered.ai", "pymetrics.ai", "criteriacorp.com",
    "shl.com", "predictiveindex.com", "leetcode.com",
)
#: Subject lines follow a few shapes across applicant-tracking systems, and the
#: shape alone says this is about an application the reader made.
SUBJECT_PATTERNS: Tuple[Tuple[re.Pattern, float, str], ...] = tuple(
    (re.compile(pattern), weight, label)
    for pattern, weight, label in (
        (r"^\s*(?:re:\s*)?your application (?:for|to|with)\b", 3.0,
         "a subject of the form 'Your application for ...'"),
        (r"^\s*(?:re:\s*)?application (?:for|to|update|status|received|submitted|"
         r"confirmation)\b",
         3.0, "an application-status subject"),
        (r"\bupdate (?:on|regarding) your application\b", 3.0,
         "a subject announcing an application update"),
        (r"\bthank you for (?:applying|your application)\b", 2.6,
         "a thank-you-for-applying subject"),
        (r"\bthank you for your interest in\b", 1.6, "a thank-you-for-interest subject"),
        (r"\binterview (?:invitation|request|confirmation|scheduled)\b", 3.0,
         "an interview subject"),
        (r"\b(?:job|position|role|opening|vacancy|opportunity)\b", 1.2,
         "a subject naming a role"),
        (r"\b(?:candidate|applicant|recruit\w*|talent|careers?|hiring)\b", 1.8,
         "a subject naming the hiring process"),
        (r"\b(?:req|requisition|jr)\s?\d{4,}\b", 2.4, "a requisition number"),
    )
)

ATS_LINK_DOMAINS: Tuple[str, ...] = (
    "greenhouse.io", "lever.co", "ashbyhq.com", "workday.com",
    "myworkdayjobs.com", "smartrecruiters.com", "icims.com", "jobvite.com",
    "bamboohr.com", "breezy.hr", "workable.com", "teamtailor.com",
    "recruitee.com", "successfactors.com", "taleo.net", "hire.withgoogle.com",
    "rippling.com", "gem.com", "paradox.ai", "eightfold.ai", "dover.com",
    "wellfound.com", "jazzhr.com", "pinpointhq.com",
    # Seen on real mail, and the list every job seeker accumulates.
    "myworkday.com", "myworkdaysite.com", "brassring.com", "kenexa.com",
    "adp.com", "workforcenow.adp.com", "ultipro.com", "paylocity.com",
    "dayforcehcm.com", "cornerstoneondemand.com", "csod.com", "avature.net",
    "phenompeople.com", "radancy.com", "symphonytalent.com", "applytojob.com",
    "clearcompany.com", "hirebridge.com", "silkroad.com", "oraclecloud.com",
    "peoplefluent.com", "isolvedhire.com", "trakstar.com", "hiringthing.com",
    "smrtr.io", "ripplematch.com", "handshake.com", "ziprecruiter.com",
    "indeed.com", "linkedin.com/jobs", "monster.com", "dice.com",
)

#: The applicant-tracking systems among those, whose mail is an employer's
#: about an application: not the job boards, and not the tools recruiters
#: send cold outreach through.
TRACKING_SYSTEMS: Tuple[str, ...] = tuple(
    domain for domain in ATS_LINK_DOMAINS
    if domain not in {"indeed.com", "linkedin.com/jobs", "monster.com", "dice.com",
                      "ziprecruiter.com", "handshake.com", "ripplematch.com",
                      "wellfound.com", "gem.com", "dover.com", "eightfold.ai",
                      "paradox.ai", "smrtr.io"})

#: Phrases that mean nothing on their own and a great deal from a careers
#: mailbox: "your details are with us" is an acknowledgement only because of
#: who sent it. Scored only when the sender is a hiring mailbox or a named
#: hiring process is established; elsewhere "the paperwork is attached" is a
#: solicitor or a letting agent. Weights are modest even then.
CONDITIONAL_SIGNALS: Dict[Category, Tuple[Signal, ...]] = {
    Category.NOT_INTERESTED: (
        Signal("chosen someone", 2.4), Signal("chosen another", 2.4),
        Signal("gone with another", 2.4), Signal("went with another", 2.4),
        Signal("offered the role to", 2.4), Signal("offered it to", 2.0),
        Signal("closer to what", 2.0),
        Signal("a closer match", 2.2),
        Signal("on this occasion", 1.8), 
        Signal("strong field", 1.6), Signal("difficult decision", 1.6),
        Signal("hear from you again", 1.2),
        Signal("keep your details", 1.6), Signal("keep you in mind", 1.6),
    ),
    Category.APPLICATION_RECEIVED: (
        Signal("your details are", 1.8),
        Signal("no need to do anything", 2.2),
        Signal("nothing you need to do", 2.2),
        Signal("nothing for you to do", 2.2),
        Signal("sit tight", 1.8),
        Signal("under review", 1.6), Signal("being reviewed", 1.6),
        Signal("with the team", 1.2), 
        Signal("shortlisting", 2.0),
    ),
    Category.NEXT_STEPS: (
        Signal("the exercise", 2.0), 
        Signal("spend some time on", 2.0),
        Signal("back to us by", 2.0),
        Signal("no time limit", 1.8), Signal("linked below", 1.6),
    ),
    Category.INTERVIEW: (
        Signal("the panel", 2.4), 
        Signal("see you again", 2.0),
        Signal("meet the team", 2.0), Signal("are you around", 2.0),
        Signal("speak again", 1.6), Signal("another conversation", 1.6),
    ),
    Category.OFFER: (
        Signal("paperwork is attached", 2.4),
        Signal("as we discussed", 1.6), 
        Signal("starting the", 1.2),
    ),
}


JOB_CONTEXT_SIGNALS: Tuple[Signal, ...] = (
    Signal("your application", 2.0),
    Signal("the position", 1.4),
    Signal("the role", 1.2),
    Signal("this role", 1.4),
    Signal("job title", 1.2),
    Signal("hiring team", 2.0),
    Signal("hiring manager", 2.0),
    Signal("talent acquisition", 2.0),
    Signal("recruiter", 1.8),
    Signal("recruiting team", 2.0),
    Signal("candidate", 1.6),
    Signal("candidacy", 2.0),
    Signal("resume", 1.4),
    Signal("cv", 0.8),
    Signal("cover letter", 2.0),
    Signal("job opening", 2.0),
    Signal("job posting", 2.0),
    Signal("vacancy", 1.8),
    Signal("interview", 1.6),
    Signal("employment", 1.4),
    Signal("careers", 1.0),
    Signal("engineer at", 0.8),
    Signal("developer at", 0.8),
    Signal("the panel", 1.2),
    Signal("take home", 1.0),
)

#: Things that mean "not this person's job search".
NON_JOB_SIGNALS: Tuple[Signal, ...] = (
    Signal("new jobs matching", 2.6, label="job-board digest"),
    Signal("jobs matching your search", 2.8, label="job-board digest"),
    Signal("job alert", 2.4, label="job alert"),
    Signal("saved search", 2.2),
    Signal("recommended jobs", 2.4),
    Signal("jobs you may be interested in", 2.6),
    Signal("viewed your profile", 2.6, label="social notification"),
    Signal("people you may know", 2.6),
    Signal("your post reached", 2.4),
    Signal("connection request", 2.2),
    Signal("premium trial", 2.0),
    Signal("unsubscribe from these alerts", 1.2),
    Signal("your order", 2.4),
    Signal("your receipt", 2.6),
    Signal("your statement", 2.6),
    Signal("verification code", 2.8),
    Signal("security alert", 2.4),
    Signal("password reset", 2.6),
    Signal("your subscription", 2.0),
    Signal("in this week s issue", 2.4),
    Signal("view this email in your browser", 1.2),
)


# Everyday topics, weighted and scored like the job tables. Sender signals are
# the sharpest: a courier's own domain settles what prose rarely does.
TOPIC_SIGNALS: Dict[OtherCategory, Tuple[Signal, ...]] = {
    OtherCategory.SECURITY: (
        Signal("app specific password", 3.0),
        Signal("password was generated for your", 3.0),
        Signal("was used to sign in to", 3.0),
        Signal("if you did not make this change", 3.0),
        Signal("unauthorized person has accessed", 3.0),
        Signal("if the information above looks familiar", 3.0),
        Signal("you can ignore this message", 2.0),
        Signal("apple account", 2.0), Signal("sign in attempt", 2.8),
        Signal("verification code", 3.0), Signal("one time code", 3.0),
        Signal("one time password", 3.0), Signal("security code", 2.8),
        Signal("two factor", 2.6), Signal("password reset", 3.0),
        Signal("reset your password", 3.0), Signal("new sign in", 2.8),
        Signal("reset the password", 2.8), Signal("choose a new password", 2.8),
        Signal("suspicious activity", 2.8), Signal("security alert", 2.8),
        Signal("was signed in to", 2.6), Signal("do not share this code", 3.0),
        Signal("confirm your email address", 2.2), Signal("verify your account", 2.4),
        # A code that expires is a code, whatever the surrounding wording.
        Signal("sign in code", 3.0), Signal("login code", 3.0),
        Signal("authentication code", 3.0), Signal("your code is", 2.8),
        Signal("code expires", 2.8), Signal("expires in 10 minutes", 2.6),
        Signal("expires in 15 minutes", 2.6), Signal("valid for 10 minutes", 2.6),
        Signal("sign in from a new device", 3.0), Signal("new device signed in", 3.0),
        Signal("we noticed a new sign in", 3.0), Signal("unusual sign in", 3.0),
        Signal("log in attempt", 2.8), Signal("unusual activity", 2.6),
        Signal("your password has been changed", 3.0),
        Signal("your password was changed", 3.0),
        Signal("your account has been locked", 3.0),
        Signal("temporarily locked", 2.6), Signal("confirm it s you", 2.8),
        Signal("two step verification", 2.8), Signal("authenticator app", 2.6),
        Signal("backup codes", 2.6), Signal("passkey", 2.6),
        Signal("trusted device", 2.4), Signal("recovery email", 2.4),
        Signal("recovery phone", 2.4), Signal("revoke access", 2.4),
        Signal("third party access", 2.2), Signal("we will never ask", 2.4),
        Signal("data breach", 2.8), Signal("involved in a breach", 3.0),
        Signal("okta.com", 2.4, field="sender", label="an identity provider"),
        Signal("duosecurity.com", 2.4, field="sender", label="an identity provider"),
        Signal("authy.com", 2.4, field="sender", label="an identity provider"),
    ),
    OtherCategory.FINANCE: (
        Signal("your statement is ready", 3.0), Signal("statement is available", 3.0),
        Signal("account statement", 2.6), Signal("payment due", 2.8),
        Signal("minimum payment", 2.8), Signal("your balance", 2.4),
        Signal("direct debit", 2.4), Signal("invoice", 2.2),
        Signal("tax", 1.6), Signal("credit card", 2.0),
        Signal("transaction", 1.8), Signal("interest rate", 1.8),
        Signal("overdraft", 2.4), Signal("investment", 1.8),
        # Money owed or moved, as opposed to money already spent on something.
        Signal("your bill is ready", 3.0), Signal("your invoice is ready", 3.0),
        Signal("payment is due", 2.8), Signal("payment scheduled", 2.6),
        Signal("autopay", 2.6), Signal("automatic payment", 2.6),
        Signal("account balance", 2.6), Signal("available balance", 2.8),
        Signal("low balance", 2.8), Signal("insufficient funds", 3.0),
        Signal("deposit posted", 2.8), Signal("direct deposit", 2.8),
        Signal("withdrawal", 2.4), Signal("your card ending in", 3.0),
        Signal("card ending in", 2.8), Signal("credit score", 1.4),
        Signal("annual percentage rate", 1.2), Signal("loan payment", 2.0),
        Signal("student loan", 1.6), Signal("mortgage", 1.0),
        Signal("loan application", 2.6), Signal("auto loan", 2.4),
        Signal("loan officer", 2.4), Signal("pay stubs", 2.2),
        Signal("pre approval", 2.4), Signal("pre approved for a mortgage", 3.0),
        Signal("escrow", 1.8), Signal("tax return", 2.6),
        Signal("form 1099", 2.8), Signal("your 1099", 2.6),
        Signal("tax document", 2.8), Signal("premium is due", 2.6),
        Signal("insurance policy", 1.4), Signal("policy renewal", 2.6),
        Signal("e statement", 2.8), Signal("paperless statement", 2.8),
        Signal("chase.com", 2.6, field="sender", label="a bank's own domain"),
        Signal("bankofamerica.com", 2.6, field="sender", label="a bank's own domain"),
        Signal("wellsfargo.com", 2.6, field="sender", label="a bank's own domain"),
        Signal("capitalone.com", 2.6, field="sender", label="a bank's own domain"),
        Signal("irs.gov", 2.8, field="sender", label="the tax authority"),
        Signal("intuit.com", 2.2, field="sender", label="a tax or accounting service"),
    ),
    OtherCategory.RECEIPT: (
        Signal("your receipt", 3.0), Signal("order confirmation", 3.0),
        Signal("thanks for your order", 3.0), Signal("your order", 2.2),
        Signal("purchase confirmation", 3.0), Signal("subscription renewed", 2.8),
        Signal("your refund", 2.6), Signal("payment received", 2.4),
        Signal("order number", 2.4), Signal("total charged", 2.6),
        # Money already spent. The tense is what separates this from Finance.
        Signal("thank you for your purchase", 3.0),
        Signal("thanks for your purchase", 3.0),
        Signal("your order is confirmed", 3.0), Signal("order placed", 2.8),
        Signal("we have received your order", 3.0),
        Signal("here is your receipt", 3.0), Signal("transaction receipt", 3.0),
        Signal("amount charged", 2.8), Signal("you were charged", 2.8),
        Signal("charged to your", 2.6), Signal("payment successful", 2.8),
        Signal("order summary", 2.6), Signal("order id", 2.4),
        Signal("invoice number", 2.4), Signal("paid in full", 2.4),
        Signal("itemized", 2.2), Signal("subscription confirmation", 2.8),
        Signal("your subscription will renew", 2.8), Signal("renewal notice", 2.4),
        Signal("auto renew", 2.4), Signal("refund has been issued", 3.0),
        Signal("refund processed", 2.8), Signal("return has been received", 2.6),
        # A payment already made to a merchant, as payment services word it.
        Signal("you sent a payment", 3.0), Signal("your payment was processed", 2.8),
        Signal("payment has been processed", 2.6), Signal("you paid", 2.6),
        Signal("you authorized", 2.2),
        Signal("your payment to", 1.8), Signal("payment details", 1.2),
        Signal("merchant", 1.0),
        Signal("stripe.com", 2.4, field="sender", label="a payment processor"),
        Signal("squareup.com", 2.2, field="sender", label="a payment processor"),
    ),
    OtherCategory.SHIPPING: (
        # Collecting a parcel, in the words the notices use.
        Signal("collection point", 2.8),
        Signal("pick up point", 2.4),
        Signal("ready to collect", 2.8),
        Signal("out for delivery", 3.0),
        Signal("we will hold it", 2.0),
        Signal("bring the qr code", 2.4),
        Signal("your parcel", 2.8),
        Signal("parcel is", 2.4),
        Signal("has shipped", 3.0), Signal("out for delivery", 3.0),
        Signal("your package", 2.8), Signal("tracking number", 3.0),
        Signal("track your", 2.4), Signal("delivered today", 2.6),
        Signal("delivery attempt", 2.6), Signal("return label", 2.4),
        Signal("your return has", 2.6), Signal("received your return", 2.8),
        # Where the parcel is, rather than what it cost.
        Signal("your shipment", 2.8), Signal("shipment update", 3.0),
        Signal("estimated delivery", 3.0), Signal("expected delivery", 2.8),
        Signal("arriving today", 3.0), Signal("arriving tomorrow", 3.0),
        Signal("has been delivered", 3.0), Signal("was delivered", 2.8),
        Signal("delivery scheduled", 2.8), Signal("in transit", 2.8),
        Signal("on its way", 2.6), Signal("label created", 2.6),
        Signal("ready for pickup", 2.8), Signal("available for pickup", 2.8),
        Signal("track your package", 3.0), Signal("track your order", 2.8),
        Signal("tracking information", 2.8), Signal("signature required", 2.6),
        Signal("missed delivery", 2.8), Signal("we could not deliver", 3.0),
        Signal("courier", 2.2),
        Signal("ups.com", 3.0, field="sender", label="a courier's own domain"),
        Signal("fedex.com", 3.0, field="sender", label="a courier's own domain"),
        Signal("usps.com", 3.0, field="sender", label="a courier's own domain"),
        Signal("dhl.com", 3.0, field="sender", label="a courier's own domain"),
        Signal("shipment-tracking", 2.8, field="sender", label="a shipping notifier"),
    ),
    OtherCategory.NEWSLETTER: (
        Signal("enews", 3.0), Signal("e news from", 3.0),
        Signal("newsletter", 2.6), Signal("bulletin", 2.4),
        Signal("greetings in the name", 2.8), Signal("worship", 2.6),
        Signal("this week at", 2.2), Signal("view this issue", 2.6),
        Signal("in this week s issue", 3.0), Signal("this week s newsletter", 3.0),
        Signal("latest issue", 2.6), Signal("you subscribed", 2.4),
        Signal("you are receiving this because you subscribed", 3.0),
        Signal("read online", 1.4), Signal("weekly digest", 2.8),
        Signal("daily briefing", 2.6), Signal("changelog", 2.0),
        Signal("release notes", 2.2), Signal("blog post", 1.6),
        # Editorial bulk mail, as opposed to bulk mail that is selling something.
        Signal("view this email in your browser", 2.8), Signal("view in browser", 2.4),
        Signal("you are receiving this email because", 2.4),
        Signal("manage your preferences", 2.2), Signal("email preferences", 2.2),
        Signal("unsubscribe from this list", 2.4), Signal("forward to a friend", 2.4),
        Signal("top stories", 2.8), Signal("today s headlines", 3.0),
        Signal("morning brief", 2.8), Signal("this week in", 2.6),
        Signal("issue no", 2.4), Signal("roundup", 2.4),
        Signal("digest", 2.4), Signal("curated", 2.2),
        Signal("our latest post", 2.4), Signal("new article", 2.2),
        Signal("edition", 1.8),
        Signal("substack.com", 3.0, field="sender", label="a newsletter platform"),
        Signal("beehiiv.com", 2.8, field="sender", label="a newsletter platform"),
    ),
    OtherCategory.PROMOTION: (
        # Job-board blasts: marketing that happens to be about jobs.
        Signal("jobs tailored for you", 3.0), Signal("fitting roles", 3.0),
        Signal("roles for you", 2.8), Signal("match your previous application", 2.8),
        Signal("new today", 2.4), Signal("recommended for you", 2.4),
        Signal("jobs matching", 2.8), Signal("we found", 1.4),
        Signal("off your next", 3.0), Signal("limited time offer", 3.0),
        Signal("save up to", 2.8), Signal("discount code", 3.0),
        Signal("flash sale", 3.0), Signal("black friday", 2.8),
        Signal("shop now", 2.6), Signal("upgrade to pro", 2.4),
        Signal("free trial", 2.2), Signal("book a demo", 2.4),
        Signal("last chance", 2.4), Signal("dont miss out", 2.2),
        # Bulk mail with something to sell.
        Signal("sale ends", 3.0), Signal("ends tonight", 2.8),
        Signal("today only", 2.8), Signal("deal of the day", 3.0),
        Signal("exclusive offer", 2.8), Signal("special offer", 2.6),
        Signal("for a limited time", 2.6), Signal("percent off", 2.8),
        Signal("buy one get one", 3.0), Signal("free shipping", 2.6),
        Signal("clearance", 2.8), Signal("new arrivals", 2.6),
        Signal("back in stock", 2.6), Signal("your cart", 2.6),
        Signal("you left something", 3.0), Signal("still thinking about", 2.4),
        Signal("cyber monday", 2.8), Signal("early access", 2.2),
        Signal("members only", 2.2), Signal("reward points", 2.4),
        Signal("gift card", 2.2), Signal("coupon", 2.8),
    ),
    OtherCategory.SOCIAL: (
        Signal("viewed your profile", 3.0), Signal("people you may know", 3.0),
        Signal("connection request", 2.8), Signal("mentioned you", 2.8),
        Signal("commented on your", 2.8), Signal("liked your", 2.6),
        Signal("new follower", 2.8), Signal("your post reached", 2.8),
        Signal("invited you to join", 2.2), Signal("community digest", 2.4),
        Signal("friend request", 3.0), Signal("started following you", 3.0),
        Signal("tagged you", 2.8), Signal("endorsed you", 2.8),
        Signal("replied to your", 2.6), Signal("shared a post", 2.6),
        Signal("new message from", 2.4), Signal("upvoted", 2.6),
        Signal("trending in your network", 2.6), Signal("your weekly stats", 2.2),
        Signal("accepted your invitation", 2.8), Signal("sent you a message", 2.8),
        Signal("new comments on", 2.6), Signal("you were mentioned", 2.8),
        Signal("profiles like yours", 2.6), Signal("reacted to your", 2.6),
        Signal("linkedin.com", 2.4, field="sender", label="a social network"),
        Signal("facebookmail.com", 3.0, field="sender", label="a social network"),
        Signal("instagram.com", 2.8, field="sender", label="a social network"),
        Signal("reddit.com", 2.6, field="sender", label="a social network"),
        Signal("discord.com", 2.6, field="sender", label="a social network"),
        Signal("nextdoor.com", 2.8, field="sender", label="a social network"),
    ),
    OtherCategory.EVENT: (
        Signal("you are registered", 2.8), Signal("register now", 2.2),
        Signal("webinar", 2.8), Signal("meetup", 2.6),
        Signal("conference", 2.2), Signal("save the date", 2.6),
        Signal("agenda for", 2.0), Signal("doors open", 2.4),
        Signal("your ticket", 2.6), Signal("rsvp", 2.6),
        Signal("registration confirmed", 3.0), Signal("event reminder", 3.0),
        Signal("here is your ticket", 3.0), Signal("tickets are ready", 2.8),
        Signal("add to calendar", 2.6), Signal("calendar invite", 2.8),
        Signal("has invited you to", 2.4), Signal("join the event", 2.4),
        Signal("starts tomorrow", 2.4), Signal("session recording", 2.2),
        Signal("your seat", 2.2), Signal("venue", 2.0),
        Signal("eventbrite.com", 3.0, field="sender", label="a ticketing service"),
        Signal("meetup.com", 3.0, field="sender", label="a ticketing service"),
        Signal("ticketmaster.com", 2.8, field="sender", label="a ticketing service"),
    ),
    OtherCategory.TRAVEL: (
        Signal("your itinerary", 3.0), Signal("booking confirmation", 2.8),
        Signal("check in for your flight", 3.0), Signal("boarding pass", 3.0),
        Signal("flight", 2.0), Signal("hotel reservation", 2.8),
        Signal("your reservation", 2.4), Signal("departure", 1.8),
        Signal("rental car", 2.4),
        Signal("trip confirmation", 3.0), Signal("flight confirmation", 3.0),
        Signal("travel itinerary", 3.0), Signal("hotel confirmation", 3.0),
        Signal("your trip", 2.6), Signal("upcoming trip", 2.8),
        Signal("your flight", 2.6), Signal("flight status", 2.8),
        Signal("gate change", 3.0), Signal("flight delayed", 3.0),
        Signal("flight cancelled", 3.0), Signal("check in is now open", 3.0),
        Signal("online check in", 2.8), Signal("seat assignment", 2.8),
        Signal("booking reference", 2.8), Signal("reservation confirmed", 2.8),
        Signal("check in date", 2.6), Signal("check out date", 2.6),
        Signal("car rental", 2.6), Signal("your stay", 2.4),
        Signal("baggage", 2.4), Signal("passport", 2.0),
        Signal("united.com", 2.6, field="sender", label="an airline"),
        Signal("delta.com", 2.6, field="sender", label="an airline"),
        Signal("southwest.com", 2.6, field="sender", label="an airline"),
        Signal("airbnb.com", 2.6, field="sender", label="a travel booking site"),
        Signal("booking.com", 2.6, field="sender", label="a travel booking site"),
        Signal("expedia.com", 2.6, field="sender", label="a travel booking site"),
        Signal("marriott.com", 2.4, field="sender", label="a hotel chain"),
        Signal("hilton.com", 2.4, field="sender", label="a hotel chain"),
    ),
    OtherCategory.CHURCH: (
        # A church writes about a handful of things and names them; little of
        # this vocabulary appears elsewhere in an inbox.
        Signal("sunday service", 3.0), Signal("sunday services", 3.0),
        Signal("morning service", 2.6), Signal("evening service", 2.6),
        Signal("this sunday", 2.4), Signal("next sunday", 2.4),
        Signal("order of service", 2.8), Signal("service times", 2.8),
        Signal("worship service", 3.0), Signal("worship team", 2.8),
        Signal("praise and worship", 2.8), Signal("worship night", 2.8),
        Signal("holy communion", 3.0), Signal("the eucharist", 3.0),
        Signal("mass times", 3.0), Signal("sunday mass", 3.0),
        Signal("evensong", 3.0), Signal("matins", 2.6),
        Signal("liturgy", 2.8), Signal("benediction", 2.6),
        Signal("the sermon", 2.8), Signal("this week s sermon", 3.0),
        Signal("sermon series", 3.0), Signal("preaching", 2.6),
        Signal("the preacher", 2.6), Signal("scripture reading", 2.8),
        Signal("bible study", 3.0), Signal("bible studies", 3.0),
        Signal("small group", 2.2), Signal("small groups", 2.2),
        Signal("home group", 2.4), Signal("life group", 2.4),
        Signal("cell group", 2.2), Signal("sunday school", 3.0),
        Signal("youth group", 2.4), Signal("youth ministry", 2.8),
        Signal("childrens ministry", 2.8), Signal("kids ministry", 2.8),
        Signal("prayer meeting", 3.0), Signal("prayer request", 3.0),
        Signal("prayer requests", 3.0), Signal("prayer chain", 3.0),
        Signal("praying for", 2.2), Signal("in our prayers", 2.6),
        Signal("please pray", 2.8), Signal("prayer list", 2.8),
        Signal("intercession", 2.6), Signal("devotional", 2.4),
        Signal("daily devotion", 2.6),
        Signal("the congregation", 3.0), Signal("our congregation", 3.0),
        Signal("church family", 3.0), Signal("the parish", 2.6),
        Signal("parish council", 3.0), Signal("parish newsletter", 3.0),
        Signal("church newsletter", 3.0), Signal("pew sheet", 3.0),
        Signal("the fellowship", 2.4), Signal("fellowship hall", 2.8),
        Signal("church council", 3.0), Signal("elders meeting", 2.8),
        Signal("deacons", 2.6), Signal("the ministry team", 2.4),
        Signal("serving rota", 3.0), Signal("the rota", 2.4),
        Signal("welcome team", 2.4), Signal("coffee rota", 2.8),
        Signal("volunteer rota", 2.6), Signal("sign up to serve", 2.6),
        Signal("tithes", 3.0), Signal("tithing", 3.0),
        Signal("your giving", 2.4), Signal("giving statement", 2.8),
        Signal("offering", 1.6), Signal("gift aid", 2.0),
        Signal("stewardship", 2.2),
        Signal("baptism", 2.8), Signal("christening", 2.8),
        Signal("confirmation service", 2.8), Signal("harvest festival", 3.0),
        Signal("carol service", 3.0), Signal("nativity", 2.6),
        Signal("midnight mass", 3.0), Signal("ash wednesday", 3.0),
        Signal("good friday", 2.6), Signal("easter sunday", 2.6),
        Signal("palm sunday", 3.0), Signal("advent", 2.2),
        Signal("lent", 2.0), Signal("pentecost", 2.8),
        Signal("vacation bible school", 3.0), Signal("alpha course", 2.8),
        Signal("mission trip", 2.6), Signal("outreach", 1.8),
        Signal("food bank", 1.8), Signal("church office", 3.0),
        Signal("our pastor", 3.0), Signal("pastor", 2.2),
        Signal("the vicar", 3.0), Signal("the rector", 2.8),
        Signal("the curate", 2.8), Signal("the chaplain", 2.4),
        Signal("the minister", 2.0), Signal("the priest", 2.4),
        Signal("the bishop", 2.4), Signal("the diocese", 3.0),
        Signal("the presbytery", 2.8), Signal("the synod", 2.6),
        Signal("god bless", 2.0), Signal("blessings", 1.6),
        Signal("in christ", 2.4), Signal("grace and peace", 2.6),
        Signal("the lord", 1.8), Signal("the gospel", 2.4),
        Signal("faith community", 2.6),
        # Denominations: the word in a church's masthead, footer and signature,
        # which identifies one anywhere.
        Signal("lutheran", 2.8), Signal("baptist", 2.6),
        Signal("methodist", 2.8), Signal("presbyterian", 2.8),
        Signal("episcopal", 2.8), Signal("anglican", 2.8),
        Signal("pentecostal", 2.8), Signal("evangelical", 2.2),
        Signal("adventist", 2.8), Signal("mennonite", 2.8),
        Signal("unitarian", 2.6), Signal("congregational church", 3.0),
        Signal("roman catholic", 2.8), Signal("catholic church", 3.0),
        Signal("orthodox church", 3.0), Signal("assemblies of god", 3.0),
        Signal("church of england", 3.0), Signal("church of scotland", 3.0),
        Signal("united methodist", 3.0), Signal("southern baptist", 3.0),
        # Denominational acronyms, which are near-unique strings.
        Signal("lcms", 2.8), Signal("elca", 2.8), Signal("pcusa", 2.8),
        Signal("umc", 1.8), Signal("sbc", 1.4),
        # What a church building has in it, and who stands where.
        Signal("the sanctuary", 2.4), Signal("the narthex", 3.0),
        Signal("the chancel", 3.0), Signal("the altar", 2.6),
        Signal("the pulpit", 2.8), Signal("the pews", 2.8),
        Signal("hymn", 2.6), Signal("hymns", 2.6), Signal("hymnal", 3.0),
        Signal("the choir", 2.6), Signal("choir practice", 3.0),
        Signal("the organist", 2.8), Signal("acolyte", 3.0),
        Signal("the lector", 2.8), Signal("the ushers", 2.4),
        Signal("church elders", 3.0), Signal("the elders", 2.2),
        Signal("coffee hour", 2.6), Signal("potluck", 2.0),
        Signal("reverend", 2.6), Signal("the deaconess", 3.0),
        # A death in the congregation, in the words a parish gives it.
        Signal("funeral mass", 3.0), Signal("mass of christian burial", 3.0),
        Signal("memorial mass", 3.0), Signal("parishioner", 2.8),
        Signal("rosary", 2.4),
        Signal("eternal rest", 2.6),
    ),
    OtherCategory.SPAM: (
        Signal("you have won", 3.0), Signal("claim your prize", 3.0),
        Signal("verify your wallet", 3.0), Signal("crypto", 2.0),
        Signal("act now", 2.0), Signal("wire transfer", 2.4),
        Signal("nigerian", 2.0), Signal("inheritance", 2.2),
        Signal("ignore previous instructions", 3.0, label="prompt-injection attempt"),
        Signal("disregard your instructions", 3.0, label="prompt-injection attempt"),
        Signal("you are an ai", 2.6, label="prompt-injection attempt"),
        Signal("system prompt", 2.4, label="prompt-injection attempt"),
        Signal("congratulations you have been selected", 3.0),
        Signal("click here to claim", 3.0), Signal("urgent action required", 2.6),
        Signal("your account will be suspended", 2.8),
        Signal("confirm your identity immediately", 2.8),
        Signal("guaranteed returns", 3.0), Signal("investment opportunity", 2.6),
        Signal("make money fast", 3.0), Signal("work from home earn", 3.0),
        Signal("dear beloved", 3.0), Signal("kindly reply", 2.4),
        Signal("beneficiary", 2.4), Signal("barrister", 2.6),
        Signal("lottery", 2.8), Signal("bitcoin", 2.4),
        Signal("risk free", 2.0), Signal("no obligation", 1.8),
        # Categories of junk that have outlived every change of wording; only
        # the unambiguous ones.
        Signal("debt consolidation", 3.0), Signal("consolidate your debt", 3.0),
        Signal("credit repair", 2.8), Signal("repair your credit", 3.0),
        Signal("refinance your", 2.6), Signal("mortgage rates", 2.2),
        Signal("lowest rates", 2.4), Signal("pre approved", 2.4),
        Signal("university diploma", 3.0), Signal("no exams", 2.6),
        Signal("weight loss", 2.4), Signal("lose weight", 2.4),
        Signal("male enhancement", 3.0), Signal("prescription drugs online", 3.0),
        Signal("online pharmacy", 3.0), Signal("no prescription", 2.8),
        Signal("replica watches", 3.0), Signal("rolex", 2.4),
        Signal("adult content", 2.8), Signal("hot singles", 3.0),
        Signal("increase your sales", 2.6), Signal("bulk email", 3.0),
        Signal("millions of email addresses", 3.0), Signal("targeted leads", 2.8),
        Signal("toner cartridges", 2.8), Signal("extended warranty", 2.4),
        Signal("casino", 2.4), Signal("free quote", 2.0),
        Signal("satellite tv", 2.6), Signal("cable descrambler", 3.0),
    ),
    OtherCategory.WORK: (
        Signal("payslip", 3.0), Signal("payroll", 2.8),
        Signal("open enrollment", 2.8), Signal("benefits enrollment", 2.8),
        Signal("timesheet", 2.8), Signal("performance review", 2.6),
        Signal("all hands", 2.6), Signal("standup", 2.2),
        Signal("sprint", 2.0), Signal("pull request", 2.4),
        Signal("expense report", 2.6),
        Signal("your paystub", 3.0), Signal("pay stub", 3.0),
        Signal("pto request", 2.8), Signal("time off request", 2.8),
        Signal("vacation request", 2.6), Signal("holiday schedule", 2.4),
        Signal("company holiday", 2.4), Signal("onboarding", 2.2),
        Signal("offboarding", 2.4), Signal("help desk ticket", 2.4),
        Signal("ticket assigned", 2.4), Signal("code review", 2.4),
        Signal("merge request", 2.4), Signal("build failed", 2.6),
        Signal("on call", 2.4), Signal("postmortem", 2.6),
        Signal("quarterly review", 2.4), Signal("one on one", 2.2),
        Signal("team meeting", 2.2),
        # The reader interviewing for their own team.
        Signal("scorecard", 2.6), Signal("interview panel", 2.4),
        Signal("atlassian.net", 2.6, field="sender", label="a work tool"),
        Signal("slack.com", 2.4, field="sender", label="a work tool"),
        Signal("asana.com", 2.4, field="sender", label="a work tool"),
        Signal("notion.so", 2.2, field="sender", label="a work tool"),
    ),
    OtherCategory.PERSONAL: (
        Signal("funeral arrangements", 3.0), Signal("memorial arrangements", 3.0),
        Signal("obituary", 3.0), Signal("passed away", 2.8),
        Signal("in loving memory", 3.0), Signal("visitation will be", 2.8),
        Signal("celebration of life", 2.8), Signal("our condolences", 2.8),
        Signal("survived by", 2.6), Signal("interment", 2.6),
        Signal("congratulations on your", 1.8), Signal("hope you had a", 1.8),
        Signal("see you then", 1.8), Signal("how are you", 1.8),
        Signal("let me know what suits", 2.2), Signal("miss you", 2.2),
        Signal("happy birthday", 2.6), Signal("thanks again for", 1.6),
        Signal("hope you are well", 1.4),
        # Written by a person to a person. Bulk mail almost never says these.
        Signal("love mom", 3.0), Signal("love dad", 3.0),
        Signal("love you", 2.8), Signal("let s catch up", 2.6),
        Signal("catch up soon", 2.4), Signal("thinking of you", 2.0),
        Signal("just checking in", 1.4), Signal("give me a call", 2.4),
        Signal("call me when", 2.4), Signal("talk soon", 1.8),
        Signal("are you free", 1.6), Signal("see you at", 1.6),
        Signal("grandma", 2.4), Signal("grandpa", 2.4),
        Signal("baby shower", 2.6), Signal("new baby", 2.4),
        Signal("get well", 2.4), Signal("happy anniversary", 2.6),
        Signal("merry christmas", 2.2), Signal("happy holidays", 1.8),
    ),
}


#: Filled in here rather than at the top, where OtherCategory is not yet used.
TRANSACTIONAL_TOPICS = (
    OtherCategory.TRAVEL,
    OtherCategory.RECEIPT,
    OtherCategory.SHIPPING,
    OtherCategory.FINANCE,
    OtherCategory.SECURITY,
)


#: Where application, interview, offer and candidate mean something else: a
#: tenancy application, a radio interview, an offer on a house.
_OTHER_WORLD = re.compile(
    r"\b(?:tenanc\w+|landlord|lettings?|letting agent|rent(?:al)?|deposit "
    r"protection|estate agent|vendor|conveyanc\w+|mortgage adviser|viewing|"
    r"property|freehold|leasehold|"
    r"radio|podcast|broadcast|documentary|on air|our listeners|newsroom|"
    r"journalist|reporter|"
    r"university place|ucas|admissions|visa application|passport application|"
    r"planning application|insurance claim|grant application|"
    r"dentist|hygienist|surgery|clinic|consultant appointment|"
    r"jury service|jury duty|deferral|tribunal|summons|magistrates|"
    r"placement year|industrial placement|the department|semester|module|"
    r"dissertation|tutor|faculty|be interviewed|running a piece|a piece on|"
    r"our (?:programme|show|listeners|viewers)|the (?:flat|house)|offer on the|"
    r"completion date|exchange(?:d)? contracts|the landlord|"
    r"your appointment|hygienist appointment|"
    r"adopt|adopting|adoption|rescue|foster care|fostering|animal shelter|home visit|"
    r"scholarship|bursary|funding (?:this|cycle|round)|awarded funding|"
    r"to volunteer|volunteer (?:application|coordinator|orientation)|"
    r"architectural review|homeowners association|"
    r"working on a story|for a story|our readers)\b"
)


#: Families of unsolicited-commercial-mail language: one turns up in ordinary
#: mail, two or three together almost never. Families rather than phrases,
#: because the wording moves and the shape does not.
_SOLICITATION = (
    ("a claim about money you could make", re.compile(
        r"\b(?:earn (?:up to )?\$?\d|\$\d[\d,]*(?:\.\d+)? (?:a|per) "
        r"(?:day|week|month|hour|year)|make money|extra income|second income|"
        r"financial freedom|be your own boss|work from home and earn|"
        r"earn money|potential income)\b")),
    ("pressure to act at once", re.compile(
        r"\b(?:act now|order now|call now|apply now|don'?t delay|"
        r"limited time|today only|offer expires|while supplies last|"
        r"urgent(?:ly)? (?:reply|respond)|immediate attention)\b")),
    ("a promise that it costs nothing", re.compile(
        r"\b(?:no obligation|risk[\W_]?free|100%\s*free|absolutely free|"
        r"free of charge|no cost to you|no experience (?:is )?(?:necessary|"
        r"required)|no credit check|money back guarantee)\b")),
    ("an opt-out footer written into the body", re.compile(
        r"\b(?:to be removed from (?:this|our) (?:list|mailing)|"
        r"click (?:here )?to (?:be )?(?:removed|unsubscribe)|"
        r"remove me from (?:this|your) list|"
        r"if you (?:wish|would like) to be removed|"
        r"this is not spam|you are receiving this because you (?:signed|opted))\b")),
    ("a guarantee no honest sender makes", re.compile(
        r"\b(?:guaranteed (?:results|income|approval|acceptance)|"
        r"lowest (?:rates?|prices?) (?:in|on|anywhere)|"
        r"best (?:rates?|prices?) (?:guaranteed|anywhere)|"
        r"you have been (?:specially )?selected|"
        r"congratulations,? you)\b")),
)
#: The day of interviews at an employer's office, by its trade name; not a
#: technician's visit.
_YOUR_ONSITE = re.compile(
    r"\byour on-?sites?\b(?! (?:support|parking|visit|service|team|staff|repair))")
#: The reader's pay, in an employer's words.
_PAYROLL = re.compile(
    r"\b(?:pay statement|pay ?stubs?|payslip|employee portal|"
    r"employee self[- ]service)\b")
#: A mailbox in a person's own name, first and last: what a hiring manager
#: writes from, and a marketing list does not ("offers@").
_NAMED_MAILBOX = re.compile(r"<?[a-z]{2,20}[._-][a-z]{2,20}\d{0,4}@")
#: Somebody's own mailbox at a mail provider: no courier or shop writes from one.
_MAIL_PROVIDERS = re.compile(
    r"@(?:gmail|googlemail|yahoo|ymail|outlook|hotmail|live|msn|icloud|me|mac|aol|"
    r"comcast|verizon|att|sbcglobal|proton|protonmail|pm|fastmail|gmx|zoho|hey)\.")
#: An account's password being reset.
_PASSWORD_RESET = re.compile(
    r"\b(?:reset (?:the |your )?password|password reset|choose a new password|"
    r"set a new password)\b")
#: A price cut held out: what a shop says, never an employer.
_SALE = re.compile(
    r"\b(?:save \d{1,3}%|\d{1,3}% off|save \$\d+|\$\d+ off|discount code|promo code|"
    r"coupon code|use code)")
#: Shouting, which is a shape rather than a vocabulary.
_SHOUTED = re.compile(r"\b[A-Z]{4,}\b")


def solicitation_score(subject: str, body: str, raw_subject: str = "") -> Tuple[float, List[str]]:
    """How strongly this reads as unsolicited commercial mail: by how many
    families of solicitation language appear, so repeating one phrase does
    not outscore doing three things.
    """
    blob = f"{subject} {body}"
    reasons = []
    for describes, pattern in _SOLICITATION:
        if pattern.search(blob):
            reasons.append(describes)
    score = {0: 0.0, 1: 1.0, 2: 2.6}.get(len(reasons), 3.6)

    shouted = _SHOUTED.findall(raw_subject or "")
    if len(shouted) >= 3:
        score += 0.8
        reasons.append("a subject line in capitals")
    if (raw_subject or "").count("!") >= 2:
        score += 0.5
        reasons.append("a subject line of exclamation marks")
    return score, reasons


#: A meeting being proposed: the verb and the noun may be forty characters
#: apart within a sentence, so one entry covers "schedule a call" and "set up a
#: quick half-hour video chat"; fixed phrases break when someone says how long
#: it will take.
_MEETING_VERB = (r"(?:schedule|set ?up|book|arrange|organi[sz]e|line up|find|"
                 r"pick|choose|grab|hop on|jump on|get on|put in|coordinate)")
_MEETING_NOUN = (r"(?:call|chat|meeting|conversation|sync|catch ?up|zoom|"
                 r"hangout|huddle|time|slot|session|screen(?:ing)?|interview|"
                 r"appointment)")
_MEETING_REQUEST = (
    ("a proposal to meet", re.compile(
        rf"\b{_MEETING_VERB}\b[^.!?\n]{{0,40}}?\b{_MEETING_NOUN}\b")),
    ("a span of time offered", re.compile(
        r"\b(?:grab|find|spare|block|carve out|put aside|have)\b"
        r"[^.!?\n]{0,20}?\b\d{1,3}\s*(?:-|\s)?\s*(?:min(?:ute)?s?|hours?)\b")),
    ("a stated length for it", re.compile(
        r"\b\d{1,3}\s*(?:-|\s)?\s*(?:min(?:ute)?s?|hours?|hrs?)\b"
        r"[^.!?\n]{0,30}?\b(?:call|chat|meeting|conversation|zoom|meet|"
        r"session|slot|interview|screen)\b")),
    ("an offer of times", re.compile(
        r"\b(?:your availability|when (?:are|would) you (?:be )?(?:free|available)|"
        r"what times? (?:work|suits?)|times? that work|let me know (?:a|what|when|"
        r"your)|does .{0,20}work for you|are you (?:free|available)|"
        r"whatever slot|any slot|slot that suits|pick a (?:time|slot))\b")),
    ("a wish to speak", re.compile(
        r"\b(?:would like to (?:meet|speak|talk|chat|connect)|"
        r"like to (?:meet|speak|talk|chat|connect) (?:with )?you|"
        r"love to (?:meet|speak|talk|chat|connect)|happy to (?:meet|speak|talk|"
        r"chat|connect)|free to (?:meet|speak|talk|chat|connect)|"
        r"keen to (?:meet|speak|talk|chat)|good time to (?:speak|talk|chat))\b")),
)


def meeting_request_score(subject: str, body: str) -> Tuple[float, List[str]]:
    """How strongly this message proposes a meeting, blind to why: a dentist
    and a hiring manager book calls in the same words. What it is for is
    professional_context_score's question.
    """
    blob = f"{subject} {body}"
    reasons = [describes for describes, pattern in _MEETING_REQUEST
               if pattern.search(blob)]
    score = {0: 0.0, 1: 1.6, 2: 2.6}.get(len(reasons), 3.2)
    return score, reasons


#: Language that places a conversation in a working life. None of it is
#: decisive alone, so it is counted in families and only qualifies other
#: evidence.
_PROFESSIONAL_CONTEXT = (
    ("an interest in your background", re.compile(
        r"\b(?:learn more about you|hear about (?:your|you)|about your "
        r"(?:background|experience|career|work|profile)|your background|"
        r"tell (?:me|us) about (?:your|you)|walk (?:me|us) through your|"
        r"more about your (?:background|experience|career)|"
        r"your (?:details|profile|cv|resume|r\xe9sum\xe9|portfolio|"
        r"credentials|qualifications))\b")),
    ("a role or an opening", re.compile(
        r"\b(?:(?:the|this|that|our|a) (?:\w+ ){0,3}"
        r"(?:role|position|opening|vacancy|headcount|req)\b|"
        r"(?:the|our|that|my) (?:\w+ ){0,3}team\b|"
        r"the opportunity|requisition|"
        r"where you (?:might|would|could) fit|what we(?:'re| are) building)\b")),
    ("hiring vocabulary", re.compile(
        r"\b(?:recruit\w*|hiring|hire|talent|candidate|candidacy|sourc(?:er|ing)|"
        r"staffing|placement|résumé|resume|cv|cover letter|portfolio|"
        r"employer|employment|career)\b")),
    ("pay and terms", re.compile(
        r"\b(?:salary|starting pay|base pay|pay rate|hourly rate|sign(?:ing|-on| on) bonus|"
        r"notice period|visa sponsorship|relocation package)\b")),
    ("news of a job", re.compile(
        r"\b(?:offer letter|your offer (?:letter|package)|your start date|your first day|"
        r"new hire (?:paperwork|orientation|forms?)|accepted (?:our|the|your) (?:job |"
        r"employment )?offer|join (?:us|our team|the team) as|(?:list|use|put) me (?:down )?"
        r"as a reference|referral bonus)\b")),
    ("a professional introduction", re.compile(
        r"\b(?:i (?:am|'m) .{0,40}(?:assistant|recruiter|partner|manager|"
        r"director|founder|lead|facilitator)|my name is .{0,30} and i"
        r"|i (?:run|lead|head up|manage) (?:the|our|that|this)|on behalf of)\b")),
)


def professional_context_score(subject: str, body: str,
                               sender: str = "") -> Tuple[float, List[str]]:
    """Whether a conversation is a working one. Qualifies, never decides."""
    blob = f"{subject} {body}"
    reasons = [describes for describes, pattern in _PROFESSIONAL_CONTEXT
               if pattern.search(blob)]
    score = {0: 0.0, 1: 1.0, 2: 2.0}.get(len(reasons), 2.8)
    return score, reasons


#: The sections a job description is built from: a posting carries several,
#: ordinary mail at most one.
_POSTING_SECTIONS = (
    ("a role summary", re.compile(
        r"\b(?:job summary|position summary|role summary|job description|"
        r"position description|about (?:the|this) (?:role|position|job|"
        r"opportunity)|the opportunity)\b")),
    ("a list of responsibilities", re.compile(
        r"\b(?:job responsibilities|key responsibilities|responsibilities|"
        r"essential (?:duties|functions)|duties and responsibilities|"
        r"what you(?: wi)?'?ll do|in this role you will|day to day|"
        r"primary duties)\b")),
    ("a list of requirements", re.compile(
        r"\b(?:qualifications|requirements|what we(?:'re| are) looking for|"
        r"what you(?: wi)?'?ll bring|skills and experience|"
        r"required skills|minimum (?:qualifications|requirements)|"
        r"preferred (?:qualifications|skills))\b")),
    ("terms of employment", re.compile(
        r"\b(?:equal opportunity employer|eeo|affirmative action|"
        r"salary range|compensation range|pay range|base salary|"
        r"benefits package|reports to|full[- ]time|part[- ]time|"
        r"exempt|non[- ]exempt|work authorization)\b")),
    ("a posting reference", re.compile(
        r"\b(?:requisition(?: id| number)?|req(?: id| #|#)|job (?:id|code|"
        r"number|req)|position id|posting (?:id|date))\b")),
    ("an experience demand", re.compile(
        r"\b(?:\d{1,2}\+? years? of experience|\d{1,2}\s*-\s*\d{1,2} years|"
        r"bachelor'?s degree|master'?s degree|degree in [a-z ]{3,30}|"
        r"equivalent experience)\b")),
)


def job_posting_score(subject: str, body: str,
                      list_unsubscribe: str = "") -> Tuple[float, List[str]]:
    """How strongly the message is a job description rather than about one. A
    posting mailed to yourself is part of a job search but contains no
    hiring process words; it is all headings. A digest quoting one heading
    is not a posting, and bulk mail is discounted.
    """
    blob = f"{subject} {body}"
    reasons = [describes for describes, pattern in _POSTING_SECTIONS
               if pattern.search(blob)]
    # One heading is not a description; a posting carries several at once.
    score = {0: 0.0, 1: 0.0, 2: 2.6, 3: 3.4}.get(len(reasons), 4.0)
    if list_unsubscribe.strip() and score:
        # A posting from a mailing list is a job board writing to everybody.
        score *= 0.45
        reasons.append("(discounted: it arrived on a mailing list)")
    return score, reasons


#: What a job board's broadcast does that a posting does not: offers many roles
#: and asks you to go and look.
_JOB_BOARD_BLAST = (
    ("more than one opening at once", re.compile(
        r"\b(?:\d{1,4}\+? (?:new )?(?:jobs|roles|openings|positions|vacancies)|"
        r"hundreds of (?:jobs|roles|openings|positions)|"
        r"(?:jobs|roles|openings|positions) (?:matching|for you|near you)|"
        r"(?:new|top|featured|recommended) (?:jobs|roles|openings|picks)|"
        r"see all \d|more (?:jobs|roles|openings))\b")),
    ("an invitation to go and browse", re.compile(
        r"\b(?:browse|explore|view|see) (?:all |more |hundreds |our |the )?"
        r"(?:jobs|roles|openings|positions|listings|opportunities)\b|"
        r"\bapply (?:in one click|with one click|now)\b|"
        r"\b(?:job alert|saved search|job digest|daily digest)\b")),
    ("a board writing on its own schedule", re.compile(
        r"\b(?:new today|this week'?s? (?:jobs|roles|picks)|"
        r"today'?s? (?:jobs|matches|picks)|your (?:weekly|daily) )\b")),
)


def job_board_blast(subject: str, body: str,
                    list_unsubscribe: str = "") -> Tuple[float, str]:
    """How strongly this is a job board broadcasting, not a job offered: the
    vocabulary matches a posting; the shape does not.
    """
    if not list_unsubscribe.strip():
        return 0.0, ""
    blob = f"{subject} {body}"
    reasons = [describes for describes, pattern in _JOB_BOARD_BLAST
               if pattern.search(blob)]
    if not reasons:
        return 0.0, ""
    score = {1: 1.4, 2: 2.8}.get(len(reasons), 3.6)
    return score, "a job board writing to a list (" + ", ".join(reasons[:2]) + ")"


#: How many times to unwrap a redirect: trackers nest, but never deeply.
MAX_LINK_UNWRAPS = 3


def unwrap_links(links: Sequence[str]) -> str:
    """One lower-case blob of link text, with redirects opened out. Click
    trackers keep the real destination inside the URL, percent-encoded
    (``tracker.example/x/https%3A%2F%2Fcalendar.example%2F…``), so matching
    domains against it finds only the tracker. Decoding makes it readable.
    """
    seen: List[str] = []
    for link in links:
        text = (link or "").lower()
        seen.append(text)
        for _ in range(MAX_LINK_UNWRAPS):
            opened = urllib.parse.unquote(text)
            if opened == text:
                break
            seen.append(opened)
            text = opened
    return " ".join(seen)


#: What an application acknowledgement does: say it arrived, promise to read
#: it, promise to be in touch if it fits. Counting those moves catches the
#: whole family; listing phrases catches only the vendors listed.
_ACKNOWLEDGEMENT = (
    ("it says the application arrived", re.compile(
        r"\b(?:receiv\w+ your (?:recent )?(?:application|resume|cv|submission)|"
        r"your (?:recent )?application (?:to|for|has been)|"
        r"thank you (?:very much )?for (?:your |applying|submitting|taking the time)"
        r"[^.!?]{0,40}(?:application|apply|interest|submission|resume)|"
        r"applied (?:to|for) (?:the |our )|"
        r"you have submitted an (?:employment )?application|"
        r"application (?:has been |was )?(?:received|submitted)|"
        r"thanks for (?:applying|submitting))\b")),
    ("it promises to read it", re.compile(
        r"\b(?:will be reviewed|reviewing (?:your |applications|candidates)|"
        r"under review|being reviewed|"
        r"(?:recruiting|talent acquisition|hiring) (?:staff|team|group)"
        r"[^.!?]{0,30}review|"
        r"review(?:ing)? your (?:application|experience|qualifications|"
        r"background|resume|profile|submission))\b")),
    ("it promises to be in touch if it fits", re.compile(
        r"\b(?:we will (?:contact|reach out to|be in touch with|get in touch)|"
        r"will be in (?:touch|contact)|you will (?:hear|be contacted)|"
        r"someone will (?:contact|reach)|"
        r"(?:if|should) (?:your|we|there|you|selected)"
        r"[^.!?]{0,60}(?:match|align|need|fit|qualif|interest|progress|"
        r"selected|suitable|contact|touch|reach))\b")),
    ("it says nothing is needed from you", re.compile(
        r"\b(?:no (?:further )?action (?:is )?(?:required|needed)|"
        r"you do not need to (?:do|take) any|"
        r"this is an automated (?:email|message|response|reply)|"
        r"please do not reply|no reply is (?:required|needed))\b")),
)


def acknowledgement_score(subject: str, body: str) -> Tuple[float, List[str]]:
    """How strongly this is "we got your application, we will be in touch": the
    commonest and least interesting job mail. It takes two moves, since
    "thank you for applying" opens a rejection too.
    """
    blob = f"{subject} {body}"
    reasons = [describes for describes, pattern in _ACKNOWLEDGEMENT
               if pattern.search(blob)]
    # Two moves score 2.6: at 3.0 one held-out message was filed wrongly.
    score = {0: 0.0, 1: 0.0, 2: 2.6, 3: 3.2}.get(len(reasons), 3.6)
    return score, reasons




# -- Reading, not matching ---------------------------------------------------
#
# A person reads a message by what it does, not by the phrases in it: an
# offer states pay, leave and a start; a rejection thanks you, names
# somebody else and leaves a door open; a second interview proposes days
# and says who will be there. Each reader below is a family of such moves.
# One move is a coincidence; two or three are the message. They score the
# oblique, typed mail that phrase tables never see, and nothing else.

_OFFER_TERMS = (
    ("pay", re.compile(
        r"\b(?:\d{2,3}k\b|[$£€]\s?\d{2,3}[,.]\d{3}\b|\d{2,3}[,.]\d{3}\s?(?:per annum|pa|"
        r"a year|p\.a\.|gbp|usd|eur)|salary|base (?:is|of|salary|pay)|per annum|"
        r"day rate|pro rata|remuneration|package of)\b")),
    ("leave", re.compile(
        r"\b(?:\d{1,2} days(?:'|’)? (?:holiday|leave|annual leave|pto|vacation)|"
        r"\d{1,2} days plus|bank holidays|annual leave|holiday allowance|pto)\b")),
    ("a start", re.compile(
        r"\b(?:start(?:ing)? (?:date|on|the|from|with us)|first day|notice period|"
        r"probation(?:ary)?|join(?:ing)? (?:us )?on the)\b")),
    ("the paperwork", re.compile(
        r"\b(?:contract|paperwork|offer letter|(?:the|our|your) offer|agreement|"
        r"sign(?:ing)? (?:when|it|the|and return|element|bonus)|countersign|"
        r"return (?:it )?signed|the terms|terms attached)\b")),
    ("the extras", re.compile(
        r"\b(?:pension|benefits|equity|share options|stock|bonus|signing element|"
        r"private medical|healthcare|relocation|car allowance)\b")),
)

_LET_DOWN = (
    ("a decision for somebody else", re.compile(
        r"\b(?:(?:go|gone|went|proceed|proceeding|move|moving|moved|progress|"
        r"progressing) (?:forward |ahead )?with (?:someone|somebody|another|"
        r"a different|other|an)\b|chose(?:n)? (?:someone|somebody|another|a candidate)|"
        r"(?:someone|somebody) (?:else|whose|who|with)\b|(?:closer|better|stronger) "
        r"(?:fit|match|aligned)|sits closer|more closely|not (?:quite )?(?:the right|a) fit|"
        r"other candidates?|done more of)\b")),
    ("a door left open", re.compile(
        r"\b(?:keep in touch|stay in touch|hear from you again|keep your (?:details|cv|"
        r"resume|name)|on file|future (?:roles?|openings?|opportunit\w+|positions?)|"
        r"(?:another|a second|other) roles?|next time|in the (?:spring|summer|autumn|"
        r"new year|future|coming months))\b")),
    ("thanks for the time", re.compile(
        r"\b(?:thank(?:s| you) (?:so much |very much |again )?for (?:coming in|your time|"
        r"the time|taking the time|meeting|talking|the effort|your interest|"
        r"your patience)|enjoyed (?:meeting|talking|speaking|the conversation|it)|"
        r"the team enjoyed|the time you put in|appreciate your interest)\b")),
    ("regret", re.compile(
        r"\b(?:unfortunately|sadly|regret|on this occasion|not (?:this time|right now)|"
        r"(?:i'?m|we'?re|i am|we are) (?:sorry|afraid)|disappointing|bad news|"
        r"(?:won'?t|will not|can'?t|cannot|unable to|not able to) (?:be )?(?:offer|"
        r"progress|proceed|consider (?:you|your application)|take (?:it|this|you) "
        r"(?:further|forward)))\b")),
)

_SEE_YOU = (
    ("a wish to meet", re.compile(
        r"\b(?:(?:would|'?d|will) (?:both |all )?(?:like|love) to (?:meet|see|speak|talk|"
        r"chat)(?: (?:with|to))? you|like to (?:meet|see) you|see you again|"
        r"meet (?:the|with the) (?:team|panel|founders?)|come (?:in|back in)|"
        r"bring you (?:in|back)|next (?:conversation|round|stage) (?:is|would be|"
        r"will be) (?:a|an|to))\b")),
    ("days or times offered", re.compile(
        r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow)\b"
        r"[^.!?\n]{0,30}\b(?:or|/)\b[^.!?\n]{0,30}\b(?:monday|tuesday|wednesday|thursday|"
        r"friday|saturday|sunday|afternoon|morning)\b|"
        r"\b(?:are you (?:around|free|available)|(?:does|would) [^.!?\n]{0,20}(?:work|suit)|"
        r"what works|an hour|half an hour|\d{2} minutes|\d{1,2} ?(?:am|pm)\b|"
        r"(?:morning|afternoon) (?:of|on) )")),
    ("who will be there", re.compile(
        r"\b(?:the panel|\w+ and i (?:would|'?d|will|both)|with \w+,? who (?:runs|leads|"
        r"heads|looks after|manages)|the (?:team|hiring manager|founders?|cto|ceo|"
        r"head of)|this time with|join(?:ed|ing) by)\b")),
    ("a second meeting", re.compile(
        r"\b(?:see you again|again, this time|next (?:round|stage|conversation)|"
        r"follow[- ]?up (?:conversation|chat|call|interview)|come back in|second "
        r"(?:round|interview|conversation|stage)|final (?:round|stage|interview)|"
        r"walk through (?:the|your) (?:take[- ]?home|exercise|task|assignment|test|"
        r"work)|rather than set another|it was good to talk)\b")),
)

_STEP_ASKED = (
    ("a request", re.compile(
        r"\b(?:could you|can you|would you (?:mind|be able)|"
        r"please (?!note|do not|don'?t|be aware|find|see|read|disregard|ignore)|"
        r"we(?:'d| would) need|we need you to|if you could|are you able to|"
        r"we(?:'d| would) like you to)\b")),
    ("the thing asked for", re.compile(
        r"\b(?:(?:send|share|provide|give|pass on|forward|supply|upload|need|two|three|"
        r"\d) [^.!?]{0,25}\breferences?|referees?|(?:the|an?|this) (?:exercise|task|assignment|"
        r"case study|presentation|test|take[- ]?home|questionnaire|form)|right to work|"
        r"passport|id documents?|proof of (?:address|identity)|portfolio|work samples?|"
        r"your availability for)\b")),
    ("by when", re.compile(
        r"\b(?:by (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|the end of|"
        r"end of|close of|\d{1,2}(?:st|nd|rd|th)?\b)|before (?:friday|monday|the end|we)|"
        r"get it back|send it back|within (?:a|\d+|the)|no time limit|time limit)\b")),
    ("what comes after", re.compile(
        r"\b(?:once (?:those|that|they|these|it)(?:'s| is| are|'re)? (?:in|back|done|"
        r"through|with us)|(?:last|final|next) stage|move (?:you )?to the|before we "
        r"(?:go|take|move)|then we can|so we can (?:move|progress|get))\b")),
)

_WE_HAVE_IT = (
    ("it says they have it", re.compile(
        r"\b(?:we have your (?:cv|resume|details|application|profile)|your (?:details|cv|"
        r"resume|application|profile) (?:are|is) (?:with us|safe|in|on file|in our "
        r"system)|just to (?:say|confirm|let you know)|got it|landed safely|"
        r"safely received|has arrived|is with the team)\b")),
    ("a wait", re.compile(
        r"\b(?:(?:within|inside|over|in) the next (?:\w+ )?(?:weeks?|days|fortnight|"
        r"month)|(?:within|inside) (?:\w+ )?(?:weeks?|days)|next fortnight|"
        r"(?:will|'ll) (?:look|review|go through|read|come back|be in touch)|only "
        r"contact|in touch if|hear from us|because of the volume|the volume of)\b")),
    ("nothing to do", re.compile(
        r"\b(?:no need to do anything|nothing (?:else |more |further )?(?:to do|needed|"
        r"required|for you to do)|no (?:further )?action|sit tight|you don'?t need to do)\b")),
)

_HEADHUNT = (
    ("your profile noticed", re.compile(
        r"\b(?:(?:came across|found|saw|noticed|spotted) your (?:profile|name|cv|resume|"
        r"details|background)|your profile (?:came up|caught)|keep a (?:short |long )?list|"
        r"(?:came across|found|saw|noticed|spotted) your (?:\w+ )?(?:project|repo|repository|"
        r"github|portfolio|blog|post|article|talk|work|code|scripts|playbooks|write-?ups?|"
        r"videos?|answers?|contributions?)|"
        r"you(?:'d| would) be a (?:great|strong|perfect|good) (?:fit|match)|"
        r"your (?:experience|background|profile|resume) (?:really )?(?:stood out|caught my "
        r"(?:eye|attention))|"
        r"on my radar|wondered whether to add you|add you to)\b")),
    ("a sounding out", re.compile(
        r"\b(?:open to a move|open to (?:a |new )?(?:conversation|chat|opportunit\w+|"
        r"roles?|hearing)|worth a (?:quick |short )?(?:chat|conversation|call)|might (?:suit|be of "
        r"interest|interest you)|would you be (?:interested|open)|if you(?:'re| are) "
        r"(?:not )?(?:looking|interested|open|curious)|not (?:a role|something) i'?m (?:filling|"
        r"hiring for|working on) right now)\b")),
    ("the role sketched", re.compile(
        r"\b(?:staff level|senior|lead|principal|head of|director|hybrid|remote|"
        r"well funded|funded|series [abc]|scale[- ]up|start[- ]up|fast[- ]growing|equity|day rate|"
        r"contract role|perm(?:anent)? role|platform work|contract to hire|w2|c2c|"
        r"corp to corp|long term|day 1 onsite|on-?site|full time|duration|"
        r"\d+\+? months|pay rate|[$]\d{2,3}(?:[-/]| per | an )(?:hr|hour|\$?\d))")),
)

#: More of a stranger's approach, from agencies and recruiters' templates.
#: Weaker words, so they count only beside a word of hiring.
_HEADHUNT_MORE = (
    ("a recruiter's own words", re.compile(
        r"\b(?:i'?m (?:a|an|the) (?:\w+ ){0,2}recruiter|(?:i'?m|i am|we'?re|we are) "
        r"recruiting (?:for|a)|recruiter (?:at|with)|talent partner|"
        r"i specialize in placing|we (?:recently )?placed|our recruiters? (?:are|is)|"
        r"greetings from|this is \w+ from|\w+ here from|i (?:lead|head) (?:talent|recruiting|"
        r"hiring))\b")),
    ("a client's role", re.compile(
        r"\b(?:(?:our|my|one of our|one of my|multiple|several|a few|a couple of|some) "
        r"(?:\w+ )?clients?|direct client|with a client|for a client|"
        r"clients? (?:who are |that are )?hiring)\b")),
    ("a reply asked for", re.compile(
        r"\b(?:reply (?:with (?:your|an updated|a) (?:\w+ )?(?:resume|cv|rate|phone|"
        r"number|contact)|yes|to any)|(?:send|share|forward) (?:me|us) (?:your|an "
        r"updated|the updated) (?:updated )?(?:resume|cv)|(?:share|send) your updated "
        r"(?:resume|cv)|(?:revert|respond) with your (?:updated )?(?:resume|cv)|"
        r"let me know (?:if|your) interest|interested(?=\s*\?)|if you or someone you know|"
        r"send me (?:a )?(?:good )?times? to (?:talk|chat|connect|call)|"
        r"want (?:me to send|the details)|would you (?:consider|be (?:interested|open))|"
        r"happy to share the (?:job )?description|happy where you are|"
        r"consider a move|kindly (?:reply|share|go through)|"
        r"your (?:experience|background|profile|skills)\b[^.!?]{0,60}\b(?:looks like|"
        r"would be|is|seems like) a (?:strong|great|good) (?:match|fit))\b")),
)


#: What a person does for somebody else's search: a referral, an
#: introduction, a reference, a lead, a chat about how a team works. No
#: application is behind any of it yet, which is what makes it networking.
_NETWORKING_MOVES = (
    ("a referral", 2.6, re.compile(
        r"\b(?:refer(?:red)? you|refer me|(?:a|the|your) referral|referral (?:bonus|for)|"
        r"put (?:in )?a good word|put your name (?:in|forward)|submitted you|"
        r"pass(?:ed)? (?:along |on )?your (?:resume|cv|name)|walk it over|"
        r"(?:send|forward|share) your (?:resume|cv) (?:to|with) (?:\w+ ){0,3}(?:people|"
        r"friends?|contacts?|someone|him|her|them)|"
        r"(?:send|forward|give|pass) (?:him|her|them) your (?:resume|cv)|"
        r"flag you to)\b")),
    ("an introduction", 2.6, re.compile(
        r"\b(?:\w+, meet \w+|introduc(?:e|ing) (?:you|two)|intro(?:duction)?:|intro to|"
        r"connect you (?:with|to)|put you in touch|i'?ll let you (?:two|both)|"
        r"over to you (?:both|two)|let you two connect|"
        r"(?:shall|should|can|may|could) i (?:connect|introduce) you)\b")),
    ("a reference", 2.6, re.compile(
        r"\b(?:(?:list|use|put) me (?:down )?as a reference|be (?:a|your) reference|"
        r"happy to vouch|give you a reference)\b")),
    ("a lead", 2.0, re.compile(
        r"\b(?:(?:my|his|her|their|our|\w+'s) (?:team|company|firm|employer|office|group|"
        r"department)(?: is| are|'s| just)? (?:hiring|building|backfilling|looking for)|"
        r"is quietly (?:hiring|building)|just (?:posted|opened) (?:a|an)\b[^.!?]{0,30}"
        r"\b(?:role|position|opening|job)|thought of you|right up your alley|"
        r"sounds like you|keep an eye out for (?:openings|roles|jobs)|"
        r"openings? on (?:their|his|her|my|our) teams?|lead for you|"
        r"(?:asked|wants) if i knew anyone)\b")),
    ("a chat about the work", 2.0, re.compile(
        r"\b(?:(?:happy|glad|always happy) to (?:chat|help|tell you|share|talk|look over|"
        r"have a (?:quick |short )?(?:call|chat)|grab (?:a )?coffee|meet)"
        r"\b[^.!?]{0,60}\b(?:team|day to day|what (?:the|we)|how (?:we|people|the|they)|"
        r"values|look for|hire|resume)|informational (?:interview|chat)|"
        r"(?:grab |a )?coffee\b[^.!?]{0,60}\b(?:team|work|hire|role)|pick your brain|"
        r"(?:offered|agreed|willing) to (?:talk|chat|meet|speak) (?:with|to) you\b[^.!?]{0,60}"
        r"\b(?:field|team|work|role|career|industry|resume|cv)|"
        r"(?:look|go) (?:at|over|through) your (?:resume|cv))\b")),
    ("the search talked about", 1.0, re.compile(
        r"\b(?:your (?:job )?(?:search|hunt)|looking for (?:work|a (?:new )?(?:job|role))|"
        r"breaking into|your resume|your next role|job hunting|on the job front|"
        r"you(?:'re| are) (?:job )?(?:hunting|looking))\b")),
)

#: What makes a referral or an introduction about a career: a word of
#: hiring, a team growing, or the search itself. "I'll put you in touch with
#: Ryan" sells a computer, and "let me introduce you to free software" is an
#: essay.
_CAREER_CUE = re.compile(
    _HIRING_ANCHOR.pattern
    + r"|\b(?:(?:growing|grow|expanding|building out|building) (?:the|their|our|his|her|"
    r"a) (?:\w+ )?team|your (?:job )?(?:search|hunt)|looking for (?:work|a (?:new )?"
    r"(?:job|role))|breaking into|next role|(?:anything|nothing) (?:posted|open)|"
    r"opens up|(?:move|moving|get|getting|switch|switching|transition|transitioning|break) "
    r"into (?:security|cyber\w*|networking|network \w+|cloud|devops|data|software|tech|"
    r"the (?:industry|field))|career change|chang(?:e|ing) careers|switch(?:ing)? careers)\b")

#: A programme being sold, which a favour never is: "shall I connect you
#: with an advisor" from a bootcamp is a sale.
_PROGRAMME_SOLD = re.compile(
    r"\b(?:our (?:free |online |live |upcoming |next )?(?:webinar|bootcamp|masterclass)|"
    r"free (?:webinar|workshop|class|trial|consultation|info session)|"
    r"enrol(?:l|ls|led|ling|ment)?|tuition)\b")

#: An application for something other than a job: a loan, a place on a
#: course, a flat, a dog. "Sadly we cannot offer you admission" is a decline
#: in every word but the last.
_OTHER_APPLICATION = re.compile(
    r"\b(?:loan|mortgage|credit card|account|lease|leasing|rental|tenancy|adopt\w*|"
    r"admissions?|scholarship|bursary|grant|visa|passport|permit|membership|"
    r"volunteer\w*|insurance|programme|program|degree|course)\b[^.!?]{0,40}"
    r"\bapplication\b|"
    r"\bapplication\b[^.!?]{0,40}\b(?:for|to) (?:an? |the |your |our )?(?:\w+ ){0,4}?"
    r"(?:loan|mortgage|credit card|account|lease|apartment|unit|adopt\w*|admission|"
    r"program|programme|degree|course|scholarship|grant|membership|volunteer\w*|"
    r"visa|card)\b|"
    r"\b(?:offer you|grant you|granted) admission\b|\badmission decision\b|"
    r"\bapplying (?:for|to) (?:an? )?(?:auto |car |home |personal )?(?:loan|mortgage|"
    r"adopt\w*|volunteer|lease)\b")

#: The reader on the other side of the table, at the job they have: on a
#: panel, writing up candidates.
_INTERVIEWER = re.compile(
    r"\b(?:(?:sit|be|join) on (?:the|an|our) (?:\w+ )?interview panel|"
    r"(?:candidate )?scorecards?|interview(?:ing)? (?:the|a|our|some) candidates?|"
    r"thanks for interviewing (?:the |our |some |a |\d+ )?candidates?|"
    r"run the technical (?:part|portion) of the interview|"
    r"feedback on (?:the )?candidates?|(?:the|this) candidate'?s? (?:resume|interview))\b")

#: An interview the newspaper, the podcast or the column wants: another
#: world's.
_MEDIA_INTERVIEW = re.compile(
    r"\binterview(?:ing)? you\b[^.!?]{0,40}\b(?:podcast|episode|show|story|article|"
    r"piece|column|newsletter|documentary|blog|magazine|segment|spotlight)\b|"
    r"\b(?:podcast|column|reporter|journalist|newsletter|magazine)\b[^.!?]{0,80}"
    r"\binterview(?:ing)? you\b")
#: A guest for a programme, or a programme at work on a piece: with an
#: interview anywhere in the message, it is the programme's. Not a job
#: title, which a station hires for too ("the Video Editor role on our show").
_ON_A_PROGRAMME = re.compile(
    r"\b(?:have you|be|come|appear|join us|guest) on (?:our|the|my) (?:show|podcast|"
    r"programme|program|radio show|segment|livestream)\b|"
    r"\b(?:our|the|my) (?:podcast|show|magazine|newspaper|paper|publication|programme|"
    r"program|newsletter|channel|station) (?:is|are|will be|has been) (?:running|doing|"
    r"working on|putting together|preparing|planning|writing|making|producing|featuring)\b")

#: An interview held out, rather than the word: "ace your next interview" is
#: a coaching advert.
_INTERVIEW_OFFERED = re.compile(
    r"\b(?:invit\w* (?:you )?to (?:an )?interview|interview (?:slot|request)s?|"
    r"to interview (?:you|for)|interviews? (?:for|with) (?:multiple|several|\d+|our)|"
    r"interviews are (?:filling|happening)|pre-?selected to interview|"
    r"invited to interview)\b")

#: Something only an applicant is told.
_APPLIED = re.compile(
    r"\b(?:your (?:application|candidacy)|you applied|applied (?:for|to)|"
    r"thanks for applying|thank you for applying)\b")

#: An interview offered to everyone on a list.
_MASS_INVITATION = re.compile(
    r"\b(?:multiple|several|\d+) (?:remote )?(?:\w+ )?(?:positions|roles|companies|"
    r"employers|openings)\b[^.!?]{0,60}\b(?:interview|partner|hiring)|"
    r"\bpartner companies\b|\bthousands of (?:resumes|candidates|profiles)\b|"
    r"\b(?:spots|slots|seats) (?:are )?(?:limited|filling)|\blimited (?:spots|slots)\b|"
    r"\b(?:pre-?selected|hand-?picked) to interview\b|"
    r"\binterviews are (?:filling|happening)\b|\bunlock interview requests\b")

#: A school's, a board's or an alumni office's list about careers: fairs,
#: workshops, advice, for everyone on it.
_CAREER_INSTITUTION = re.compile(
    r"career (?:services|center|centre|office|development)|alumni|university|college|"
    r"workforce|chamber of commerce|community college|school district")

#: The reader asked to find candidates for somebody else, which is their work.
_FIND_SOMEBODY = re.compile(
    r"\b(?:if you know (?:anyone|someone|somebody)|know anyone (?:good|who)|"
    r"please (?:share|forward|pass on) (?:it|this|the posting))\b")

#: The user's own application or interview named, which makes a meeting part
#: of a process rather than networking.
_IN_A_PROCESS = re.compile(
    r"\b(?:your (?:application|candidacy|interview)|you applied|applied (?:for|to)|"
    r"the interview|interview(?:ing)? with|thanks for applying|"
    r"(?:assessment|interview|hiring|selection|recruiting|recruitment) process|"
    r"withdraw\w* from (?:our|the|this) process)\b")


_QUOTE_HEADER = re.compile(
    r"^\s*>?\s*on [^\n]{0,60}?,?\s+([^\n<,]{2,60}?)\s*(?:<[^>\n]*>)?\s*wrote:\s*$",
    re.I | re.M)


def _follows_up_itself(body: str, sender: str) -> bool:
    """Whether a reply quotes nobody but its own sender: a recruiter's
    second note on top of the first. Such a thread is not the reader's."""
    quoted = [match.group(1).strip().lower() for match in _QUOTE_HEADER.finditer(body or "")]
    if not quoted:
        return False
    name = (sender or "").split("<")[0].strip().strip('"').lower()
    if not name:
        return False
    return all(author == name or author.split()[-1:] == name.split()[-1:]
               for author in quoted)


#: Several applications reported at once, by a tracker or a careers site:
#: job mail, and no one stage of anything.
_DIGEST_PHRASE = re.compile(
    r"\b(?:updates? on|status of|changes? (?:to|in)|summary of|news on) (?:\d+|two|"
    r"three|four|five|several|multiple) (?:of )?(?:your )?(?:job )?"
    r"(?:applications|candidacies)\b|\byour applications this week\b|"
    r"\byou applied to \d+ (?:roles|jobs|positions)\b|"
    r"\bwhat changed in your applications\b")
_STATUS_LINE = re.compile(
    r"(?:\||:|-|\u2013)\s*(?:applied|interviewing|phone screen|final round|rejected|"
    r"closed|under review|in review|assessment(?: requested)?|offer(?: made)?|"
    r"position closed|not selected|withdrawn)\s*$", re.M)


def application_digest(subject: str, body: str) -> str:
    """Why this reports several applications at once, or nothing: it says
    so, or it is a list of them with a status each."""
    if _DIGEST_PHRASE.search(f"{subject} {body}"):
        return "it reports on several of your applications"
    if len(_STATUS_LINE.findall(body)) >= 3:
        return "a list of applications, each with its status"
    return ""


def networking_score(subject: str, body: str) -> Tuple[float, List[str]]:
    """How much a message reads as networking for the reader's search, and
    the moves it makes. Nothing where the reader is asked to find somebody."""
    blob = f"{subject} {body}"
    if _FIND_SOMEBODY.search(blob) or not _CAREER_CUE.search(blob):
        return 0.0, []
    score, reasons = 0.0, []
    for describes, weight, pattern in _NETWORKING_MOVES:
        if pattern.search(blob):
            score += weight
            reasons.append(describes)
    if score < 2.0:
        return 0.0, []
    return min(4.2, score), reasons


def _family_score(families, blob: str, counts: dict) -> Tuple[float, List[str]]:
    reasons = [describes for describes, pattern in families if pattern.search(blob)]
    return counts.get(len(reasons), counts[max(counts)]), reasons


def offer_terms_score(subject: str, body: str) -> Tuple[float, List[str]]:
    """How strongly this states the terms of a job: pay, leave, a start, the
    paperwork, the extras. Two of them together is an offer, whatever it
    is called."""
    return _family_score(_OFFER_TERMS, f"{subject} {body}",
                         {0: 0.0, 1: 0.0, 2: 2.6, 3: 3.2, 4: 3.6})


def let_down_score(subject: str, body: str) -> Tuple[float, List[str]]:
    """How strongly this turns somebody down without saying so: thanks for
    the time, somebody else chosen, a door left open. The decision or the
    regret has to be there; thanks and an open door alone are a friendly
    note."""
    score, reasons = _family_score(_LET_DOWN, f"{subject} {body}",
                                   {0: 0.0, 1: 0.0, 2: 2.6, 3: 3.2, 4: 3.6})
    if score and not any(why in ("a decision for somebody else", "regret")
                         for why in reasons):
        return 0.0, []
    return score, reasons


def see_you_score(subject: str, body: str) -> Tuple[float, List[str]]:
    """How strongly a conversation in person is being arranged: a wish to
    meet, days offered, who will be there, a second time."""
    return _family_score(_SEE_YOU, f"{subject} {body}",
                         {0: 0.0, 1: 0.0, 2: 2.6, 3: 3.2, 4: 3.6})


def step_asked_score(subject: str, body: str) -> Tuple[float, List[str]]:
    """How strongly something is being asked for before a process can go on:
    a request, the thing itself, by when, and what follows."""
    score, reasons = _family_score(_STEP_ASKED, f"{subject} {body}",
                                   {0: 0.0, 1: 0.0, 2: 2.6, 3: 3.2, 4: 3.6})
    if score and "the thing asked for" not in reasons:
        return 0.0, []
    return score, reasons


def we_have_it_score(subject: str, body: str) -> Tuple[float, List[str]]:
    """How strongly this says an application arrived and nothing is wanted
    yet, in words that never say "application"."""
    return _family_score(_WE_HAVE_IT, f"{subject} {body}",
                         {0: 0.0, 1: 0.0, 2: 2.6, 3: 3.2})


def headhunt_score(subject: str, body: str) -> Tuple[float, List[str]]:
    """How strongly a stranger is sounding you out about a role you did not
    ask about. An agency's template words count only where hiring is
    named."""
    blob = f"{subject} {body}"
    score, reasons = _family_score(_HEADHUNT, blob, {0: 0.0, 1: 0.0, 2: 3.6, 3: 3.8})
    if not _HIRING_ANCHOR.search(blob):
        return score, reasons
    more = [describes for describes, pattern in _HEADHUNT_MORE if pattern.search(blob)]
    found = len(reasons) + len(more)
    if found < 2:
        return 0.0, []
    return max(score, {2: 3.6, 3: 3.8}.get(found, 4.2)), reasons + more


#: A call being arranged: the subject names a role and a call, or the body
#: is the boilerplate of a meeting link. Together with a person or a hiring
#: mailbox writing, an interview.
_TITLE_WORDS = re.compile(
    r"\b(?:analyst|engineer|developer|technician|specialist|manager|consultant|"
    r"intern|internship|associate|coordinator|administrator|architect|designer|"
    r"scientist|representative|assistant|director|officer|apprentice|graduate|"
    r"trainee|programmer|support|operations|it)\b")
_CALL_SUBJECT = re.compile(
    r"\b(?:teams call|zoom call|phone call|video call|call\s*\(|phone screen|"
    r"interview|screening call|intro call|meeting|conversation with)\b")
_MEETING_LINK = re.compile(
    r"\b(?:teams\.microsoft\.com|zoom\.us/j|meet\.google\.com|webex\.com|"
    r"meeting id|passcode|dial in by phone|join the meeting|join microsoft teams|"
    r"microsoft teams meeting|google meet|zoom meeting)\b")
_CALENDAR_PART = re.compile(r"\.ics$|^invite|calendar", re.I)


def call_score(subject: str, body: str, attachments: Sequence[str] = ()) -> Tuple[float, List[str]]:
    """How strongly a call about a role is being arranged: a role and a call
    in the subject, a meeting link or a calendar part, a reschedule."""
    reasons = []
    if _TITLE_WORDS.search(subject) and _CALL_SUBJECT.search(subject):
        reasons.append("a role and a call in the subject")
    if _MEETING_LINK.search(body):
        reasons.append("a meeting link")
    if any(_CALENDAR_PART.search(name or "") for name in attachments):
        reasons.append("a calendar invitation attached")
    if re.search(r"\b(?:rescheduled|reschedule|pushed your time|moved your|time slot|"
                 r"double booked|event accepted|accepted your invitation)\b",
                 f"{subject} {body}"):
        reasons.append("a time being moved or accepted")
    if not reasons or "a role and a call in the subject" not in reasons and len(reasons) < 2:
        return 0.0, []
    return {1: 2.6, 2: 3.2}.get(len(reasons), 3.6), reasons


#: A job board passing a recruiter's message on: the board's own mailbox, a
#: subject saying somebody is interested, and the message inside.
_RELAY_SUBJECT = re.compile(
    r"\b(?:is interested in (?:talking to you|you|your profile|your resume|your "
    r"background)|wants to (?:connect|talk|chat)|sent you a message|"
    r"new message from|has a job for you|invited you to apply)\b")
_RELAY_BODY = re.compile(
    r"\b(?:new message in your \w+ inbox|interested in your (?:resume|profile|"
    r"background)|would like to offer|is interested in|message from \w+ at)\b")
_RELAY_SENDER = re.compile(
    r"\b(?:monster|indeed|linkedin|ziprecruiter|glassdoor|dice|handshake|"
    r"wellfound|hired|otta|reed|totaljobs|cwjobs|seek|stepstone)\b")


def relay_score(subject: str, body: str, sender: str) -> Tuple[float, List[str]]:
    """How strongly this is a recruiter's approach passed on by a job board:
    the board's mailbox, and the subject or the body saying so."""
    if not _RELAY_SENDER.search(sender or ""):
        return 0.0, []
    reasons = []
    if _RELAY_SUBJECT.search(subject):
        reasons.append("a board saying somebody is interested")
    if _RELAY_BODY.search(body):
        reasons.append("a recruiter's message passed on")
    if not reasons:
        return 0.0, []
    return 3.6 if len(reasons) == 2 else 2.8, reasons


#: Senders whose "application", "interview" and "offer" are somebody else's:
#: a university, a court, a surgery, a letting agent, a newsroom.
_OTHER_WORLD_SENDER = re.compile(
    r"\b(?:admissions|registry|student|faculty|school|college|university|academy|"
    r"court|courts|hmcts|tribunal|jury|nhs|dental|dentist|dentists|clinic|surgery|"
    r"practice|lettings|estates?|property|properties|homes|conveyanc\w*|solicitors?|"
    r"legal|radio|press|newsroom|editorial|producer|bbc|itv|rescue|shelter|humane|"
    r"pantry|hoa|financial aid|finaid)\b")


def other_world_sender(sender: str) -> Tuple[float, str]:
    """Whether the sender itself belongs to another world."""
    found = _OTHER_WORLD_SENDER.search(sender or "")
    if not found:
        return 0.0, ""
    return 3.6, f"from {found.group(0)}, where an application is not a job application"


#: Family and friends, who write "role" and "congratulations" too.
_KITH = re.compile(
    r"\b(?:mum|mom|dad|nan|gran|grandma|grandad|grandpa|aunt|auntie|uncle|cousin|"
    r"sis|bro|love you|miss you|xx+|hugs|proud of you|drinks on|the pub|a pint|"
    r"birthday|wedding|the baby|congrats!!|see you at christmas)\b")


def kith_score(subject: str, body: str, sender: str) -> Tuple[float, str]:
    """Whether this is family or a friend writing: the words only they use,
    from a person's own address."""
    found = _KITH.search(f"{subject} {body}")
    if not found or not looks_like_a_person(sender):
        return 0.0, ""
    return 2.4, f"family or a friend ({found.group(0)!r})"


# A parcel notice that never says "delivery", a flight confirmation that never
# says "flight", a friend moving a plan: a person reads their shape (a flight
# number beside an airport pair, a booking reference, a bookings@ mailbox, two
# people talking), and a phrase list has nothing to list.

#: Who the sender is, from the part before the @: a company uses a mailbox per
#: kind of mail, and the name says which (offers@, billing@, bookings@,
#: security@). Nearly free to read, and right far more often than not.
_SENDER_PURPOSE: Tuple[Tuple[str, "OtherCategory", float], ...] = (
    (r"offers?|deals?|promo\w*|marketing|savings?|voucher|sale", OtherCategory.PROMOTION, 1.8),
    (r"news(letter)?|digest|weekly|monthly|bulletin|update[sz]?", OtherCategory.NEWSLETTER, 1.6),
    (r"billing|invoices?|payments?|accounts?|statements?|finance|creditcontrol",
     OtherCategory.FINANCE, 1.8),
    (r"receipts?|orders?|purchase\w*", OtherCategory.RECEIPT, 1.6),
    (r"shipping|delivery|deliveries|dispatch|tracking|parcels?|courier",
     OtherCategory.SHIPPING, 2.0),
    (r"security|auth\w*|verify|verification|2fa|otp|login|signin",
     OtherCategory.SECURITY, 2.0),
    (r"bookings?|reservations?|tickets?|events?|rsvp", OtherCategory.EVENT, 1.8),
    (r"travel|flights?|trips?|itinerary|checkin|check-in", OtherCategory.TRAVEL, 1.8),
    (r"social|notifications?|friends?|community", OtherCategory.SOCIAL, 1.2),
)
_SENDER_PURPOSE_COMPILED = tuple(
    (re.compile(rf"(?:^|[._-])(?:{pattern})(?:$|[._-])", re.I), topic, weight)
    for pattern, topic, weight in _SENDER_PURPOSE)

#: A local part shaped like a name rather than a department ("jane.doe",
#: "a_smith"): two name-ish pieces, few digits, and none of the role words
#: above.
_PERSON_LOCAL = re.compile(
    r"^[a-z]{1,20}(?:[._-][a-z]{1,20}){1,2}\d{0,4}$|^[a-z]{2,20}\d{0,4}$", re.I)
_ROLE_WORDS = re.compile(
    r"no.?reply|do.?not.?reply|donotreply|mailer|bounce|postmaster|admin|"
    r"info|hello|hi|contact|support|help|service|team|care|customer|"
    r"notification|alerts?|news|mail|robot|auto|system|daemon|"
    r"reception|bookings?|appointments?|clinic|surgery|practice|admissions|"
    r"registry|lettings|sales|enquiries|office|careers|jobs|recruit\w*|talent|"
    r"people|hr|payroll|billing|accounts?|orders?|sec|secretary|committee", re.I)
#: A whole piece of an address that is a role, plural or not.
_ROLE_PIECE = re.compile(rf"(?:{_ROLE_WORDS.pattern})s?", re.I)


def sender_sector(sender: str) -> Tuple[Dict["OtherCategory", float], List[str]]:
    """What the company sending this does for a living. A phrase table cannot
    know which domains are airlines or shops; tens of thousands of company
    domains can. A sector describes the sender, never the message (a bank
    sends codes and marketing alike), so it is a hint, capped with the other
    soft evidence.
    """
    sector, matched = lexicon.sector_of(sender)
    if not sector:
        return {}, []
    topic_name, weight = lexicon.SECTOR_TOPICS.get(sector, ("", 0.0))
    if not topic_name:
        return {}, []
    try:
        topic = OtherCategory(topic_name)
    except ValueError:                      # pragma: no cover - data mismatch
        return {}, []
    said = {"airline": "an airline", "bank": "a bank or insurer",
            "telecom": "a phone or broadband company", "utility": "a utility",
            "retail": "a shop", "courier": "a courier",
            "social": "a social network", "news": "a news publisher",
            "hotel": "a hotel or restaurant"}.get(sector, f"a {sector} company")
    return {topic: weight}, [f"“{matched}” is {said}"]


def sender_purpose(sender: str) -> Tuple[Dict["OtherCategory", float], List[str]]:
    """What the mailbox this came from is for: "offers@" is a promotion before
    a word of the body is read.
    """
    local = (sender or "").split("@")[0]
    local = local.split("<")[-1].strip().lower()
    if not local:
        return {}, []
    found: Dict[OtherCategory, float] = {}
    why: List[str] = []
    for pattern, topic, weight in _SENDER_PURPOSE_COMPILED:
        if pattern.search(local):
            found[topic] = max(found.get(topic, 0.0), weight)
            why.append(f"it came from “{local}@”")
            break
    return found, why


#: Mailboxes that exist to talk to candidates, checked against the display name
#: as well as the address ("Careers <no-reply@company>").
_HIRING_MAILBOX = re.compile(
    r"(?:^|[\W_])(?:careers?|recruit(?:ing|ment|er|ers)?|talent"
    r"(?:[\W_]?acquisition)?|hiring|jobs?|vacanc(?:y|ies)|"
    r"people[\W_]?(?:team|ops|operations)|human[\W_]?resources|"
    r"campus[\W_]?recruit\w*|graduate[\W_]?(?:recruit\w*|scheme))"
    r"(?:$|[\W_])", re.I)


def agency_sender(sender: str) -> bool:
    """Whether the address is a recruitment agency's."""
    return any(hint in (sender or "").replace(" ", "") for hint in AGENCY_SENDER_HINTS)


#: Platforms that write to candidates for employers and to nobody else: an
#: assessment or a one-way interview. LeetCode is left out; most of what it
#: sends is its own marketing.
PLATFORM_SENDER_DOMAINS: Tuple[str, ...] = tuple(
    domain for domain in ASSESSMENT_LINK_DOMAINS + VIDEO_INTERVIEW_DOMAINS
    if domain != "leetcode.com")


def hiring_mailbox(sender: str) -> str:
    """The word that says this mailbox exists to talk about hiring, if any.
    "Your details are with us" could come from anywhere; from a recruitment
    mailbox it is an application acknowledgement, and only the From line
    says so. Both halves are read: a company sending from no-reply@ still
    puts "Careers" or "Talent" in the display name. An assessment or
    interviewing platform's own domain says it too.
    """
    sender = sender or ""
    domain = sender.split("@")[-1].strip(" >").lower() if "@" in sender else ""
    if domain and any(domain == known or domain.endswith("." + known)
                      for known in PLATFORM_SENDER_DOMAINS):
        return "candidate-platform"
    name = sender.split("<")[0]
    local = sender.split("@")[0].split("<")[-1]
    for part in (name, local):
        found = _HIRING_MAILBOX.search(part or "")
        if found:
            return re.sub(r"^[\W_]+|[\W_]+$", "",
                          found.group(0)).lower()
    return ""


#: Words that make a sender a place of worship, checked against the display
#: name and the domain: a church writes from its own domain under its own name,
#: while the wording inside varies with whoever writes it. "churchill" is
#: excluded by name.
_CHURCH_SENDER = re.compile(
    r"church(?!ill)|chapel|parish|cathedral|congregation|diocese|"
    r"tabernacle|synagogue|mosque|ministries|"
    r"lutheran|baptist|methodist|anglican|episcopal|presbyterian|"
    r"pentecostal|evangelical|adventist|orthodox\w*church|"
    r"catholicchurch|uniting\w*church|christchurch",
    re.I)
#: The same words standing on their own in a display name, where "Fenchurch"
#: is a shop and "Chapelford" a town.
_CHURCH_WORD = re.compile(r"\b(?:" + _CHURCH_SENDER.pattern + r")(?:e?s)?\b", re.I)
#: A denomination names churches, and also hospitals that carry nothing
#: else in their names but a city's.
_DENOMINATION = re.compile(
    r"lutheran|baptist|methodist|anglican|episcopal|presbyterian|pentecostal|"
    r"evangelical|adventist|mennonite|unitarian|lcms|elca|pcusa|umc|sbc", re.I)


#: A parish's own domain, named for its saint and often its town: stmarysparish,
#: stjosephs and the town's name. Only on a parish's kind of ending.
_SAINT_DOMAIN = re.compile(
    r"^(?:st|saint)(?:john|mary|paul|peter|joseph|michael|patrick|james|francis|anne?|"
    r"luke|mark|matthew|andrew|thomas|brendan|catherine|theresa|therese|teresa|jude|"
    r"anthony|agnes|gabriel|raphael|elizabeth|margaret|bridget|brigid|benedict|ignatius|"
    r"vincent|monica|rita|cecilia|clare|dominic|isidore|pius|stephen|timothy|barnabas|"
    r"lawrence|alphonsus|charles|edward|george|joan|columba|aidan|david|giles|martin|"
    r"nicholas|philip|bartholomew|simon|helen|lucy|ambrose|augustine|gregory|leo)"
    r"s?[a-z]*\.(?:org|church|parish)$")
#: What else takes a saint's or a denomination's name: a Lutheran hospital,
#: an Adventist health system, a parish school, a Baptist university, a
#: Methodist retirement village, a credit union.
_NOT_A_PARISH = re.compile(
    r"hospital|health|medical|clinic|school|academy|college|universit|seminary|"
    r"prep|high(?!land|way|ridge|point|view)|social services|senior|village|"
    r"credit union|\bf?cu\b|insurance|mutual", re.I)
#: Where a church word is a town's, a street's or a firm's name.
_NOT_A_CHURCH = re.compile(
    r"chapel\s*hill|cathedral\s*city|christchurch\s*(?:city|airport|council)|"
    r"church\s*(?:street|st\b|road|rd\b|lane|hill)|church\s*(?:&|and)?\s*dwight|"
    r"parish\s*(?:council|government|sheriff|assessor|clerk|president|librar|utilit)",
    re.I)
#: The church words that are surnames too, and how a congregation's name
#: opens where a person's would have a first name.
_SURNAME_CHURCH_WORDS = frozenset({"church", "chapel", "parish"})
_CONGREGATION_OPENS = re.compile(
    r"^(?:st\b|saint|the|first|second|third|holy|grace|trinity|christ|calvary|"
    r"bethel|faith|hope|new|our|community|united|old|sacred|immaculate|blessed|"
    r"ascension|resurrection|epiphany|annunciation|transfiguration|visitation|"
    r"nativity|assumption|incarnation|good shepherd|precious blood|corpus christi)",
    re.I)


def _a_surname(name: str, word: str, sender: str) -> bool:
    """Whether the church word in a display name is somebody's surname:
    "Sarah Church" or "Parish, Tom" writing from their own address, rather
    than "Grace Church" or "Elevation Church" writing from the office's."""
    if word not in _SURNAME_CHURCH_WORDS or len(_CHURCH_WORD.findall(name)) > 1:
        return False
    words = re.findall(r"[A-Za-z][\w'.-]*", name)
    if not 2 <= len(words) <= 3 or not looks_like_a_person(sender):
        return False
    if "," in name and words[0].lower() == word:
        return True
    return words[-1].lower() == word and not _CONGREGATION_OPENS.match(name.strip())


def _settles(name: str, word: str) -> bool:
    """Whether a church word in a congregation's display name says outright
    that it is one. A denomination alone does not (a city's name and
    "Methodist" is a hospital's), and neither does "parish" after a place's
    name, which is also
    a county in Louisiana: "Jefferson Parish", against "St. Mary's Parish"
    and "Parish of the Ascension"."""
    if _DENOMINATION.fullmatch(word):
        return False
    if word == "parish":
        return bool(_CONGREGATION_OPENS.match(name.strip())
                    or re.match(r"\s*parish\b", name, re.I)
                    or re.search(r"\bparish\s+of\b", name, re.I))
    return True


#: A death in a congregation, in the words its office gives it: the prompt's
#: own example of mail written personally that is still parish business.
_A_DEATH_NOTICE = re.compile(
    r"\b(?:funeral (?:arrangements|service|mass|home)|memorial (?:arrangements|service|"
    r"mass)|visitation (?:will be|is|at|for)|wake (?:will be|is)|passed away|"
    r"past member|longtime member|entered (?:into )?eternal rest)\b")


def church_sender(sender: str) -> Tuple[str, bool]:
    """What says this sender is a place of worship, if anything, and whether
    that settles it. A notice about a bereavement reads the same from anyone;
    the From line is what makes it a church notice.

    A congregation's own name settles it: "St. Brendan Parish", "Grace
    Church". The rest only suggest it and count beside what a church writes
    about: a church word run into a domain (church domains run their words
    together, and so does ``fenchurch``, a shop), a denomination on its own
    (a hospital's name, often) and a saint's name (a children's hospital's
    too).
    A hospital, a school, a government, a firm the lexicon knows and a person
    called Church are never one.
    """
    sender = sender or ""
    name = sender.split("<")[0].strip().strip("\"'")
    domain = sender.rsplit("@", 1)[-1].strip("<>() \t").lower() if "@" in sender else ""
    if (domain.endswith((".gov", ".mil")) or ".gov." in domain or ".govt." in domain
            or any(p.search(name) or p.search(domain) for p in (_NOT_A_PARISH, _NOT_A_CHURCH))
            or lexicon.sector_of(sender)[0]):
        return "", False
    named = [w for w in (m.group(0).lower() for m in _CHURCH_WORD.finditer(name))
             if not _a_surname(name, w, sender)]
    if named:
        settling = [w for w in named if _settles(name, w)]
        return (settling or named)[0], bool(settling)
    found = _CHURCH_SENDER.search(domain)
    if found:
        return found.group(0).lower(), False
    if _SAINT_DOMAIN.match(domain):
        return "a saint's name", False
    return "", False


def looks_like_a_person(sender: str) -> bool:
    """Whether the address belongs to a person rather than a department. A
    role word has to be a whole piece of the address: "hi" is a mailbox,
    and also two letters of a surname."""
    local = (sender or "").split("@")[0].split("<")[-1].strip().lower()
    if not local:
        return False
    pieces = [local] + re.split(r"[._-]", local)
    if any(_ROLE_PIECE.fullmatch(piece) for piece in pieces if piece):
        return False
    return bool(_PERSON_LOCAL.match(local))


#: Structured things a person recognises on sight: shapes, not words.
_ENTITIES: Tuple[Tuple[str, "re.Pattern", "OtherCategory", float], ...] = (
    ("a flight number", re.compile(
        r"\b(?:[A-Z]{2}|[A-Z]\d|\d[A-Z])\s?\d{2,4}\b(?!\s*(?:%|mb|gb|kb))"), 
     OtherCategory.TRAVEL, 1.4),
    # The airport pair is handled separately, against the real list: three
    # capitals either side of "to" is also "PDF to DOC".
    ("a seat or gate", re.compile(
        r"\bseat\s*\d{1,3}[A-K]\b|\bgate\s*[A-Z]?\d{1,3}\b|"
        r"\bboarding\b|\bbags? close\b|\bcheck.?in closes\b", re.I),
     OtherCategory.TRAVEL, 2.2),
    ("a booking reference", re.compile(
        r"\b(?:reference|ref|booking|confirmation|pnr|record locator)\s*"
        r"(?:is|:|number|no\.?|#)?\s*([A-Z0-9]{5,8})\b", re.I),
     OtherCategory.TRAVEL, 1.2),

    ("a tracking number", re.compile(
        r"\b(?:tracking|consignment|waybill)\s*(?:number|no\.?|#|:)?\s*"
        r"[A-Z0-9]{8,}\b", re.I), OtherCategory.SHIPPING, 2.6),
    ("an amount of money", re.compile(
        r"[£$€]\s?\d[\d,]*(?:\.\d{2})?\b|\b\d[\d,]*\.\d{2}\b"),
     OtherCategory.FINANCE, 0.8),
    ("a direct debit or standing order", re.compile(
        r"\bdirect debit\b|\bstanding order\b|\bdd\b(?= *(?:of|for))|"
        r"\bcould not be collected\b|\bwe'?ll try again\b", re.I),
     OtherCategory.FINANCE, 2.6),
    ("a meter or usage reading", re.compile(
        r"\bmeter reading\b|\bkwh\b|\bunits used\b|\bestimated? bills?\b|"
        r"\bactual reading\b|\btrue it up\b", re.I), OtherCategory.FINANCE, 2.6),
    ("a table or covers", re.compile(
        r"\btable for \d\b|\bcovers?\b(?= *(?:at|for)? *\d)|"
        r"\bwe hold tables\b|\bconfirmed under\b|\bparty of \d\b|"
        r"\bdoors (?:open|at)\b", re.I), OtherCategory.EVENT, 2.4),
)


def entity_scores(subject: str, body: str,
                  raw_subject: str = "", raw_body: str = "") -> Tuple[
                      Dict["OtherCategory", float], List[str]]:
    """Topics implied by the shapes in a message rather than its words, on the
    raw text, since case is half of what makes a flight number look like
    one.
    """
    blob = f"{raw_subject or subject}\n{raw_body or body}"[:MAX_SCANNED_CHARS]
    found: Dict[OtherCategory, float] = {}
    why: List[str] = []
    for describes, pattern, topic, weight in _ENTITIES:
        if pattern.search(blob):
            found[topic] = found.get(topic, 0.0) + weight
            why.append(describes)
    # Two real airports either side of "to" is a flight; two arbitrary capitals
    # are "PDF to DOC", hence the list of four and a half thousand codes.
    pair = lexicon.airport_pair(blob)
    if pair:
        found[OtherCategory.TRAVEL] = found.get(OtherCategory.TRAVEL, 0.0) + 2.4
        why.append(f"a flight between {pair[0]} and {pair[1]}")
    # Several shapes agreeing count for more than their sum; one alone never
    # decides.
    for topic in list(found):
        found[topic] = min(3.4, found[topic])
    return found, why


#: How two people write to each other rather than how a company writes to a
#: customer: none decisive, together unmistakable.
_CONVERSATIONAL = (
    ("a question", re.compile(r"\?")),
    ("first and second person", re.compile(
        r"\b(?:i|i'?m|i'?ll|i'?ve|we|you|your|you'?re|me|my|us)\b", re.I)),
    ("contractions", re.compile(
        r"\b\w+'(?:s|t|re|ll|ve|d|m)\b", re.I)),
    ("an apology or thanks", re.compile(
        r"\b(?:sorry|thanks|thank you|cheers|no worries|apolog\w+)\b", re.I)),
    ("arranging something between two people", re.compile(
        r"\b(?:can we|shall we|are you|could you|do you|let me know|"
        r"push it|move it|swap|instead|either way|works for me)\b", re.I)),
)


def personal_register(subject: str, body: str, sender: str,
                      list_unsubscribe: str = "", links: Sequence[str] = ()) -> Tuple[
                          float, List[str]]:
    """How strongly this reads as one person writing to another. A note moving
    a plan may have no topic word at all; what marks it is a person's
    address, no unsubscribe, no links, a short body and two people arranging
    something.
    """
    if list_unsubscribe.strip():
        return 0.0, []                      # a list is not a person
    # Sounding like a friend is the oldest trick in unsolicited mail ("Re: our
    # conversation"), so warmth counts only when nothing is being sold.
    selling, _why = solicitation_score(normalize(subject), normalize(body), subject)
    if selling >= 2.6:
        return 0.0, []
    fake, _fake_why = impersonation_score(sender, normalize(subject), normalize(body))
    if fake:
        return 0.0, []
    # A careers mailbox is not a person writing to you, however warm: a
    # friendly rejection is still a rejection.
    if hiring_mailbox(sender):
        return 0.0, []
    text = f"{subject}\n{body}"
    reasons = [describes for describes, pattern in _CONVERSATIONAL
               if pattern.search(text)]
    score = {0: 0.0, 1: 0.0, 2: 0.8, 3: 1.6}.get(len(reasons), 2.2)
    if not score:
        return 0.0, []
    if looks_like_a_person(sender):
        score += 1.2
        reasons.insert(0, "a person's own address")
    if len(body) <= 320:
        score += 0.6
        reasons.append("short, the way a note is")
    if len(links) > 2:
        score = max(0.0, score - 1.4)
        reasons.append("(discounted: it is full of links)")
    return min(3.4, score), reasons


def other_world_context(subject: str, body: str) -> Tuple[float, str]:
    """How strongly the message is about something other than a job search."""
    hits = set(_OTHER_WORLD.findall(f"{subject} {body}"))
    if not hits:
        return 0.0, ""
    return min(4.4, 1.8 * len(hits)), f"about {sorted(hits)[0]} rather than a job search"


# Phrase tables only recognise mail written the way they expect, which
# templated mail mostly is and typed mail is not. These read form rather than
# vocabulary, so paraphrase does not defeat them: whether a person or a machine
# sent it, money and which way it went, a code, a flight, a delivery window, a
# date.

#: Local parts that mean nobody is reading replies.
_ROBOT_SENDER = re.compile(
    r"\b(?:no[\W_]?reply|do[\W_]?not[\W_]?reply|noreply|donotreply|notifications?|"
    r"alerts?|automated|auto[\W_]?notify|mailer|bounces?|postmaster|support|"
    r"info|hello|team|news|updates?|billing|accounts?|care|service)\b"
)
#: A run of 4 to 8 digits standing on its own: a one-time code, usually.
_BARE_CODE = re.compile(r"(?<![\w.])(\d{4,8})(?![\w.])")
#: Money, in the three symbols this is likely to meet, or written out.
_MONEY = re.compile(r"(?:[$£€]\s?\d[\d,]*(?:\.\d{2})?)|(?:\b\d[\d,]*\.\d{2}\s?(?:usd|gbp|eur)\b)")
#: Money that has already gone.
_SPENT = re.compile(
    r"\b(?:we[\W_]?ve|we have|weve)?\s*(?:taken|charged|debited|deducted|"
    r"refunded|credited|put\s+\S+\s+back|paid)\b")
_MONEY_IN = re.compile(
    r"\b(?:sent you|paid you|you(?:'ve| have)? received|requested) "
    r"(?:[$\u00a3\u20ac]\s?)?\d")
_SPENT_AT = re.compile(
    r"\byou spent (?:[$\u00a3\u20ac]\s?)?\d[\d,]*(?:\.\d{2})?(?:\s?(?:usd|gbp|eur))? at\b")
#: Money that has not gone yet.
_OWED = re.compile(
    r"\b(?:due|owing|outstanding|payable|will (?:be )?(?:taken|collected|leave)|"
    r"leaves your account|collection is|balance of|chasing|overdue)\b")
#: A delivery window, which almost nothing but a courier writes.
_DELIVERY_WINDOW = re.compile(
    r"\bbetween\s+\d{1,2}\s?(?:am|pm|:\d{2})[^.]{0,20}\band\s+\d{1,2}\s?(?:am|pm|:\d{2})")
#: Things only parcels do.
_PARCEL = re.compile(
    r"\b(?:parcel|package|courier|driver|doorstep|delivery office|"
    r"left (?:it )?with your neighbour|no answer|needs a signature|stops before yours)\b")
#: A parcel's journey: what makes "the package" one on its way.
_DELIVERED = re.compile(
    r"\b(?:deliver\w*|tracking|track (?:it|your)|dispatch\w*|shipped|out for|"
    r"on (?:its|the) way|arriv\w+|collect\w*|pick ?up|drop(?:ped)? off|left (?:it )?"
    r"(?:with|at|in|by)|signed for|depot|sorting)\b")
#: A flight number: two letters then three or four digits.
_FLIGHT = re.compile(r"\b[A-Za-z]{2}\d{3,4}\b")
#: A booking or confirmation reference: mixed letters and digits, 5 to 8 long.
_BOOKING_REF = re.compile(r"\b(?=[A-Z0-9]{5,8}\b)(?=[^\s]*\d)(?=[^\s]*[A-Z])[A-Z0-9]{5,8}\b")
#: A clock time, which dated things have and broadcasts rarely do.
_CLOCK = re.compile(r"\b\d{1,2}[:.]\d{2}\s?(?:am|pm)?\b|\b\d{1,2}\s?(?:am|pm)\b")
_WEEKDAY = re.compile(
    r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|tonight)\b")
#: First and second person, which is how people write and marketing imitates.
_PERSONAL_VOICE = re.compile(r"\b(?:i|i[\W_]?ll|i[\W_]?ve|we[\W_]?ve|my|me|us)\b")
#: Selling, as opposed to telling.
_SELLING = re.compile(
    r"\b(?:off|discount|voucher|deal|sale|save|free|offer|shop|buy|order now|"
    r"basket|cart|restock|outlet|%)\b")
#: Editorial furniture.
_EDITORIAL = re.compile(
    r"\b(?:issue|edition|no\.\s?\d+|this (?:week|fortnight|month)|read (?:it|more) on|"
    r"long read|links?|subscrib\w+|unsubscribe|in this)\b")


#: A figure to two places with no currency sign, money when the words
#: around it are about money.
_BARE_AMOUNT = re.compile(r"\b\d{1,4}(?:,\d{3})*\.\d{2}\b")
_MONEY_WORDS = re.compile(
    r"\b(?:card|refund\w*|bill\w*|payment|direct debit|debit|renew\w*|premium|"
    r"collect\w*|charged|fee|invoice|balance|policy|quote|paid|pay)\b")
#: Things said about a journey or a stay.
_JOURNEY = re.compile(
    r"\b(?:gate|bag drop|leaves at|departs|nights? in|you arrive|arrive on|"
    r"check ?in (?:shuts|closes|opens|is)|door code|your stay|your trip|your host|"
    r"itinerary|boarding|departure|the airport|hotel)\b")
#: The sounds of a scam: a fortune, a dead relative, a confidence, and the
#: grammar of somebody writing in a language not their own.
_SWINDLE = (
    re.compile(r"\b(?:million (?:dollars|usd|euros|pounds)|late (?:husband|wife|father|"
               r"client)|next of kin|in confidence|trustworthy partner|business "
               r"proposal|transfer of funds|god bless|beneficiary|unclaimed)\b"),
    re.compile(r"\b(?:we has|you parcel|is hold|is require|will be return|"
               r"dear (?:customer|user|valued|sir/madam)|kindly (?:verify|confirm|"
               r"update)|good day)\b"),
    re.compile(r"\b(?:a fee of|pay (?:the |a )?(?:fee|charge) (?:now|within)|"
               r"click here to pay|reply for details|release the funds|"
               r"unpaid (?:fee|charge|customs|duty)|(?:redelivery|customs) fee|"
               r"pay (?:now|online) to (?:schedule|release|receive|reschedule))\b"),
    # Money moved for a stranger: the cheque deposited and the rest wired on,
    # the gift cards bought, the bank details sent for a job never applied for.
    re.compile(r"\b(?:deposit (?:it|the check|the cheque)|keep \$?\d+ as your|"
               r"wire the (?:rest|remaining|balance)|(?:purchase|buy) (?:\w+ )?gift cards?|"
               r"send (?:us|me) the (?:codes|card numbers)|gift card codes|"
               r"(?:reply|respond) with your (?:full name|name)?,? ?(?:address,? )?(?:and )?"
               r"(?:bank|ssn|social security))\b"),
)

#: The favour asked in a pastor's, a manager's or a friend's name: gift cards
#: bought, and their codes or photographs of them sent on, pressed for in a
#: hurry or in confidence. Nobody who knows the reader asks for the codes.
_CODES_SENT_ON = (
    r"(?:(?:send|text|email|forward) (?:me|us) (?:the |their |all the |all |both )?"
    r"(?:codes?|pins?|card numbers|numbers|pictures?|photos?|pics?)|"
    r"reply with (?:the |their |all the )?(?:codes?|pins?|card numbers)|"
    r"(?:pictures?|photos?|pics?|snaps?) of (?:the |each |both |all )?(?:cards?|codes?|backs?))")
_GIFT_CARD_CODES = re.compile(
    r"\bgift ?cards?\b[\s\S]{0,300}?\b" + _CODES_SENT_ON
    + r"|\b" + _CODES_SENT_ON + r"[\s\S]{0,300}?\bgift ?cards?\b")
_FAVOUR_PRESSED = re.compile(
    r"\b(?:are you (?:available|free|around|busy)|quick (?:favou?r|request)|(?:a|small|"
    r"urgent) favou?r|in a meeting|can'?t (?:talk|call|take calls)|between us|"
    r"confidential|a surprise|(?:pay|reimburse) you back|asap|urgent\w*|how many can "
    r"you get|\$\d+ each)")


def favour_scam(blob: str) -> bool:
    """Whether a message asks for gift cards and their codes the way the
    favour scam does, pressing for them."""
    return bool(_GIFT_CARD_CODES.search(blob) and _FAVOUR_PRESSED.search(blob))


def job_swindle(blob: str) -> bool:
    """Whether a message is a job that pays out through the reader: gift
    cards for the equipment, a cheque to deposit and wire on, bank details
    sent by reply."""
    return bool(_SWINDLE[3].search(blob) and _HIRING_WORDS.search(blob))


#: Words written for a machine reading the mail, not for its reader: the
#: model prompt calls them strong evidence of spam, and so are they here.
_TO_A_MACHINE = re.compile(
    r"\b(?:ignore (?:all |any )?(?:the )?(?:previous|prior|above|other) (?:instructions|"
    r"rules|prompts?)|(?:classify|categori[sz]e|label) (?:this|it|the message|this message|"
    r"this email) as|(?:set|with) (?:the )?confidence (?:to|of|at)|note to (?:any|the) "
    r"(?:ai|automated|model|assistant)|system note|(?:any|the) automated reader|"
    r"(?:to|for) (?:any|the) ai (?:reading|reader))\b")
#: A social network telling you what others did.
_SOCIAL_NOTICE = re.compile(
    r"\b(?:appeared in \d+ searches|viewed your profile|looked at your profile|"
    r"\d+ reactions|people you (?:may know|worked with)|have new roles|were busy|"
    r"commented on|mentioned you|tagged you|connection request|wrote something|"
    r"new followers?|liked your)\b")
#: The systems of a workplace, and the people team's business.
_WORK_TOOLING = re.compile(
    r"\b(?:deploy\w*|staging|production|prod|rollback|migrations?|pipeline|runbook|"
    r"on[- ]?call|on rota|rota|p99|latency|incident|outage|pager|threshold|sprint|"
    r"stand-?up|retro|jira|pull request|merge|is red|build (?:failed|broke)|"
    r"the gate|checkout-api|api|q[1-4]|forecast|board deck|the deck|quarterly|"
    r"budget|okrs?|kpis?|roadmap|stakeholders?|sync on|quick sync|the numbers|"
    r"(?:inc|ritm|chg)\d{5,}|1:1|one[- ]on[- ]ones?|backlog)\b")
#: A conference selling its stage.
_CONFERENCE = re.compile(
    r"\b(?:main stage|keynote|speaker series|speakers?|agenda|register (?:now|today|for)|"
    r"save your (?:seat|spot)|conference|summit|expo|workshop|webinar|live at|"
    r"join(?:s|ing)? (?:the|our) (?:main stage|stage|line-?up|panel)|join us (?:at|on|for))\b")
#: A talent network keeping its list warm.
_TALENT_NETWORK = re.compile(
    r"\b(?:talent (?:community|network|pool)|careers? community|stay connected|"
    r"continue receiving|keep receiving|remain active in our system|email preferences|"
    r"opt[- ]in|re-?subscribe|confirm you(?:'d| would) like to continue|"
    r"career insights|recruiting events)\b")
#: What a subject line alone says, when there is no body under it.
_SUBJECT_ALONE = {
    "a thank-you-for-applying subject": (Category.APPLICATION_RECEIVED, 2.6),
    "a thank-you-for-interest subject": (Category.APPLICATION_RECEIVED, 2.0),
    "a subject announcing an application update": (Category.APPLICATION_RECEIVED, 1.8),
    "a subject of the form 'Your application for ...'": (Category.APPLICATION_RECEIVED, 1.8),
}
#: What a one-time code is called, and how long the message around one runs.
_ONE_TIME_CODE = re.compile(
    r"\b(?:one[\W_]?time (?:pass ?code|passcode|password|code|pin)|verification code|"
    r"security code|sign[\W_]?in code|log[\W_]?in code|confirmation code|"
    r"(?:confirm|verify) your identity|(?:use|enter) (?:this|the following) code)\b")
ONE_TIME_CODE_BODY = 900
#: The code itself, introduced as one: "pass code: 731904." A bare number
#: would also be a year at the end of a sentence.
_NAMED_CODE = re.compile(r"\b(?:code|passcode|pin|otp)\b[^.!?\d]{0,20}\d{4,8}\b|"
                         r"\b(?:code|passcode|pin|otp)\b[^.!?\d]{0,80}:\s*\d{4,8}\b")
_VERIFY_SUBJECT = re.compile(
    r"\b(?:account verification|verify your (?:account|email)|activate your account|"
    r"complete your (?:profile|registration))\b")
_WORK_ADMIN = re.compile(
    r"\b(?:benefits? (?:enrol\w*|elections?|window|choices)|open enrol\w+|"
    r"medical and dental|dental and medical|roll over onto|your plan for next year|"
    r"payroll|payslip|expenses claim|timesheet|annual review|performance review|"
    r"people team|hr team|all[- ]hands|town hall|line manager|your line manager|"
    r"self[- ]?(?:evaluation|assessment|appraisal)|appraisal|"
    r"(?:pto|time[- ]off|leave) request|(?:mandatory|compliance) training)\b")
#: The job the reader already has, in a people team's words: what makes its
#: mail work rather than hiring.
_CURRENT_JOB = re.compile(
    r"\b(?:self[- ]?(?:evaluation|assessment|appraisal|review)|appraisal|"
    r"performance (?:review|evaluation|conversation|cycle|check[- ]?in)|"
    r"(?:annual|yearly|year[- ]end|mid[- ]year) (?:review|check[- ]?in)|"
    r"(?:pto|time[- ]off|leave|holiday) (?:request|balance)|open enrol\w+|"
    r"benefits? (?:enrol\w*|elections?|window)|payroll|payslip|pay ?stubs?|"
    r"timesheets?|all[- ]hands|town hall|(?:mandatory|compliance) training|"
    r"all (?:employees|staff)|staff portal|employee handbook|your (?:line )?manager)\b")
#: Being hired, which a people team writes about too.
_BEING_HIRED = re.compile(
    r"\b(?:candidate\w*|candidacy|interview\w*|applications?|applied|applying|"
    r"apply|offer letter|your offer|start date|first day|onboarding|new hire|"
    r"new starter|background check|references?|requisition|vacanc\w+|"
    r"openings?|positions?)\b")
#: A night out: doors, a table, a ticket.
_OUTING = re.compile(
    r"\b(?:doors (?:at|open)|on at \d|standing|seated|tickets?|box office|line[- ]?up|"
    r"support act|set times|table for \d|we hold tables|your booking|see you there|"
    r"rsvp|you'?re coming to|the bar takes|on stage)\b")
#: A group writing to its members.
_COMMUNITY = re.compile(
    r"\b(?:turned up|well done|thanks to everyone|everyone who|next (?:work day|"
    r"meeting|session|rehearsal|practice)|same time|bring (?:gloves|a chair|wellies|"
    r"food|a plate)|volunteers?|members|the committee|agm|subs are due|work day)\b")

#: Brands worth impersonating in a From line, checked against the domain the
#: mail came from.
_IMPERSONATED = (
    "apple", "icloud", "google", "gmail", "microsoft", "outlook", "office365",
    "amazon", "paypal", "netflix", "meta", "facebook", "instagram", "linkedin",
    "chase", "barclays", "hsbc", "natwest", "lloyds", "santander", "revolut",
    "monzo", "wise", "coinbase", "binance", "dhl", "fedex", "ups", "usps",
    "evri", "royalmail", "hmrc", "irs", "dvla",
)

#: The words a phishing domain is built from. Two of them hyphenated
#: together, or one on a throwaway ending, is a domain nobody would register
#: to send honest mail: parcel-redelivery-fee, invoice-secure-pay.
_SCAM_DOMAIN_WORDS = frozenset({
    "verify", "verification", "secure", "security", "account", "accounts", "login",
    "signin", "update", "support", "billing", "payment", "pay", "fee", "fees",
    "redelivery", "delivery", "parcel", "quota", "mailbox", "alert", "alerts",
    "claim", "reward", "rewards", "winner", "prize", "refund", "invoice", "income",
    "cash", "assignments", "survey",
})
_THROWAWAY_ENDINGS = frozenset({"top", "xyz", "click", "biz", "loan", "work", "gq",
                                "cf", "ml", "tk", "buzz", "rest"})
#: Pressure plus a threat plus a link: the shape of phishing, whatever brand it
#: wears.
_URGENCY = re.compile(
    r"\b(?:within \d+ hours?|immediately|urgent(?:ly)?|right away|act now|"
    r"as soon as possible|before it is too late|final (?:notice|warning))\b")
_THREAT = re.compile(
    r"\b(?:suspend\w*|clos\w+ permanently|permanently clos\w+|terminat\w+|"
    r"delet\w+|restrict\w+|lose access|locked out|legal action)\b")
_CLICK_THROUGH = re.compile(
    r"\b(?:click here|verify your account|confirm you(?:r)? identity|"
    r"update your details|log ?in below|follow this link)\b")


def _registrable(domain: str) -> str:
    """The part of a host a brand would actually own."""
    parts = [p for p in domain.split(".") if p]
    if len(parts) < 2:
        return domain
    # Good enough: two labels, or three where the middle is a known
    # second-level suffix. It only has to notice that
    # apple-account-verify.example is not apple.com.
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "gov", "ac", "net"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def scam_domain(sender: str) -> str:
    """Why the sending domain is one nobody would register to send honest
    mail, or nothing: scam words hyphenated together (parcel-redelivery-fee),
    or one on a throwaway ending."""
    address = sender.rpartition("<")[2].rstrip(">").strip() or sender
    root = _registrable(address.split("@")[-1].lower())
    stem = root.split(".")[0]
    scammy = len(set(re.split(r"[-.]", stem)) & _SCAM_DOMAIN_WORDS)
    if ("-" in stem and scammy >= 2) or (
            scammy and root.rsplit(".", 1)[-1] in _THROWAWAY_ENDINGS):
        return f"a domain made of a scam's words ({root})"
    return ""


def impersonation_score(sender: str, subject: str, body: str) -> Tuple[float, str]:
    """Whether the From line claims to be somebody the domain says it is not: a
    display name is free text and the domain is not, so the gap between them
    is the most reliable thing about the message.
    """
    display, _, address = sender.rpartition("<")
    address = address.rstrip(">").strip() or sender
    display = (display or "").strip().strip('"').lower()
    domain = address.split("@")[-1].lower()
    root = _registrable(domain)
    stem = root.split(".")[0]

    claimed = next((brand for brand in _IMPERSONATED if brand in display), "")
    if claimed and claimed != stem and not stem.endswith(claimed):
        return 3.0, f"the name says {claimed} but the mail came from {root}"

    # A brand buried in a longer hyphenated domain is the same trick:
    # apple-account-verify, paypal-secure-login.
    if "-" in stem and any(brand in stem for brand in _IMPERSONATED):
        return 2.6, f"a brand name inside a longer domain ({root})"
    scam = scam_domain(sender)
    if scam:
        return 2.6, scam

    blob = f"{subject} {body}"
    pressure = sum(bool(pattern.search(blob))
                   for pattern in (_URGENCY, _THREAT, _CLICK_THROUGH))
    if pressure >= 3:
        return 2.4, "pressure, a threat and a link to follow"
    if pressure == 2:
        return 1.2, "pressure and a threat"
    return 0.0, ""


def _robot_sender(sender: str) -> bool:
    local = sender.split("@")[0] if "@" in sender else sender
    return bool(_ROBOT_SENDER.search(local))


_TERMS_CHANGE = re.compile(
    r"\b(?:changes? to|updat\w+(?: to)?|revis\w+(?: to)?)\s+(?:our|the|its)\s+(?:legal "
    r"agreements?|user agreement|terms(?: of (?:service|use))?|privacy (?:policy|statement|"
    r"notice)|customer agreement|account agreement)\b")
_BOUNCER = re.compile(r"\b(?:mailer[\W_]?daemon|postmaster|mail delivery (?:system|subsystem))\b")
_BOUNCED = re.compile(
    r"\b(?:undeliver\w+|delivery status notification|returned to sender|could not be "
    r"delivered|delivery (?:has )?failed|message not delivered|address not found)\b")
#: A tenant of a shared service whose name is a jumble: digits run into
#: letters, or five consonants in a row.
_BORROWED_TENANT = re.compile(
    r"@(?=[a-z0-9-]*(?:\d{3}|[bcdfghjklmnpqrstvwxz]{5}))[a-z0-9-]+\."
    r"(?:atlassian\.net|zendesk\.com|freshdesk\.com|myshopify\.com|wixsite\.com|"
    r"weebly\.com|webflow\.io|notion\.site|sharepoint\.com|helpscoutdocs\.com)\b")
_LURE = re.compile(
    r"\b(?:bonus|gifts?|prizes?|rewards?|winner|you(?:'ve| have)? won|claim|free spins|"
    r"jackpot|cash ?out|picked up)\b")


#: A library's hold or loan: waiting at the desk or due back there.
_LOAN_WORDS = re.compile(
    r"\b(?:on hold|placed (?:a |on )?hold|ready for pick ?up|due back|due date|"
    r"checked out|overdue|renew\w*)\b")


def library_loan(blob: str, sender: str = "") -> bool:
    """Whether a message is a library's hold or loan. "Ready for pickup" and
    "placed on hold" are a shop's words too, so they count where a library
    is named, in the message or the From line."""
    if re.search(r"\b(?:public library|your holds? (?:is|are) ready)\b", blob):
        return True
    library = re.search(r"\blibrar(?:y|ies)\b", blob) or "librar" in (sender or "").lower()
    return bool(library and _LOAN_WORDS.search(blob))


def structural_topic_scores(
    subject: str, body: str, sender: str, list_unsubscribe: str = "",
    links: Sequence[str] = (),
) -> Tuple[Dict["OtherCategory", float], Dict["OtherCategory", List[str]]]:
    """Evidence from the shape of a message rather than its phrasing."""
    scores: Dict[OtherCategory, float] = {}
    notes: Dict[OtherCategory, List[str]] = {}

    def add(topic: "OtherCategory", weight: float, why: str) -> None:
        scores[topic] = scores.get(topic, 0.0) + weight
        notes.setdefault(topic, []).append(why)


    blob = f"{subject} {body}"
    bulk = bool(list_unsubscribe)

    fake, why = impersonation_score(sender, subject, body)
    if fake:
        add(OtherCategory.SPAM, fake, why)
    if _TO_A_MACHINE.search(blob):
        add(OtherCategory.SPAM, 3.4, "words written to a machine reading the mail")

    selling, selling_why = solicitation_score(subject, body, subject)
    if selling >= 2.6:
        # Three or more separate solicitation moves, or two with shouting:
        # junk, not a shop you know.
        add(OtherCategory.SPAM, min(3.4, selling), selling_why[0])
        # A bank reporting a statement and a stranger selling a mortgage use
        # the same words; one reports something that happened, the other sells.
        for topic in (OtherCategory.FINANCE, OtherCategory.RECEIPT,
                      OtherCategory.SHIPPING, OtherCategory.TRAVEL):
            if topic in scores:
                scores[topic] *= 0.45
                notes.setdefault(topic, []).append("but it is soliciting, not reporting")
    elif selling >= 1.0:
        add(OtherCategory.PROMOTION, selling, selling_why[0])
    robot = _robot_sender(sender)
    short = len(body) < 700

    # The scams a person spots at once: a stranger's fortune, or a
    # courier's notice written by nobody who speaks the language.
    swindle = len({pattern.pattern for pattern in _SWINDLE if pattern.search(blob)})
    if swindle >= 2:
        add(OtherCategory.SPAM, 3.0, "the shape of a scam")
    elif swindle == 1 and (fake or selling >= 1.0):
        add(OtherCategory.SPAM, 1.6, "a scam's turn of phrase")

    # The favour asked in somebody's name. Asked for without the pressing,
    # gift card codes are only a hint: a friend may want back the one they
    # gave.
    if favour_scam(blob):
        add(OtherCategory.SPAM, 3.4, "gift cards and their codes asked for, in a hurry")
    elif job_swindle(blob):
        # A job that pays out through the reader's gift cards, a cheque to
        # deposit or bank details sent by reply: a swindle wearing a job.
        add(OtherCategory.SPAM, 3.0, "a job that asks for money")
    elif _GIFT_CARD_CODES.search(blob):
        add(OtherCategory.SPAM, 1.6, "gift card codes asked for")

    # The reader's pay, from the employer that pays it.
    if _PAYROLL.search(blob):
        add(OtherCategory.WORK, 3.4, "the reader's pay, from their employer")

    # A forum's digest of what its members wrote, which the prompt files as
    # social. Not an economic forum's newsletter: threads are a forum's own.
    if (re.search(r"\bforum\b", f"{blob} {sender}")
            and re.search(r"\b(?:threads|replies)\b", blob)):
        add(OtherCategory.SOCIAL, 4.6, "a forum's digest")

    # The reader interviewing somebody for their employer: work, not a search.
    if _INTERVIEWER.search(blob):
        add(OtherCategory.WORK, 3.0, "the reader interviewing for their employer")

    # A social network telling you what happened without you.
    if _SOCIAL_NOTICE.search(blob):
        add(OtherCategory.SOCIAL, 2.4, "a social network's notification")

    # The machinery of a workplace: the systems, or the people team.
    tooling = len(set(_WORK_TOOLING.findall(blob)))
    if tooling >= 2:
        add(OtherCategory.WORK, 2.6 if tooling == 2 else 3.2, "the business of a workplace")
    if _WORK_ADMIN.search(blob) and not (bulk and _SELLING.search(blob)):
        add(OtherCategory.WORK, 2.4, "a workplace's own administration")

    # A library's hold, which no courier delivers.
    if library_loan(blob, sender):
        add(OtherCategory.OTHER, 3.4, "a library's hold or loan")

    # A stranger selling to the reader's team: a product, a demo, a calendar.
    if (re.search(r"\b(?:demo|our (?:\w+ )?(?:platform|product|software|tool|solution))\b", blob)
            and re.search(r"\b(?:teams? like yours|companies like yours|for teams|your (?:it )?"
                          r"(?:team|stack))\b", blob)):
        add(OtherCategory.PROMOTION, 3.0, "a stranger selling to the reader's team")

    # A conference selling its days.
    if re.search(r"\b(?:summit|symposium|expo)\b", blob) and re.search(
            r"\b(?:early[- ]bird|talks|speakers|sessions|labs|keynote|register)\b", blob):
        add(OtherCategory.EVENT, 2.6, "a conference and its programme")

    # A night out, a table, a ticket.
    if len(set(_OUTING.findall(blob))) >= 2:
        add(OtherCategory.EVENT, 2.4, "an outing with a time and a place")
    # A conference selling its stage.
    stage = len(set(_CONFERENCE.findall(blob)))
    if stage >= 2:
        add(OtherCategory.EVENT, {2: 2.4, 3: 3.2}.get(stage, 4.2), "a conference and its speakers")

    # A group writing to its members: a club, an allotment, a choir.
    if len(set(_COMMUNITY.findall(blob))) >= 2 and not bulk:
        add(OtherCategory.PERSONAL, 2.0, "a group writing to its members")

    # Money without a symbol: a figure to two places, where the words say
    # it is money.
    plain_money = bool(_BARE_AMOUNT.search(blob)) and bool(_MONEY_WORDS.search(blob))

    # A one-time code
    codes = _BARE_CODE.findall(blob)
    if codes and short and not bulk:
        # Codes come with a deadline and an instruction, never with a pitch.
        expiring = re.search(r"\b(?:expires?|stops working|valid for|within)\b", blob)
        add(OtherCategory.SECURITY, 3.0 if expiring else 1.6,
            "a short code in a short message")

    # The terms of an account changing: a notice to every customer, and
    # still the account's business rather than an advert.
    if _TERMS_CHANGE.search(blob):
        add(OtherCategory.SECURITY, 2.4, "a change to the terms of an account")

    # Mail that never arrived, reported by the system that tried to send it.
    if _BOUNCER.search(sender) and _BOUNCED.search(blob):
        add(OtherCategory.OTHER, 3.4, "a delivery failure notice")

    # A shared service's tenant with a name nobody chose, offering a prize:
    # a helpdesk or tracker borrowed to send spam past the filters.
    if _BORROWED_TENANT.search(sender) and _LURE.search(blob):
        add(OtherCategory.SPAM, 3.2, "a prize offered from a borrowed service")

    # An account changed: a new primary email, a password, a sign-in method.
    if re.search(r"\b(?:your (?:\w+ )?(?:email|email address|address|password|details|"
                 r"phone number) "
                 r"(?:has been|have been|was|were) (?:updated|changed)|primary email|"
                 r"new sign-?in method|recovery (?:email|phone)|security settings|"
                 r"made [^.!?]{0,60}? your primary)\b", blob):
        add(OtherCategory.SECURITY, 2.0, "a change to the account")

    # Account safety, without the word security
    if re.search(r"\b(?:password|sign(?:ed|ing)? in|signin|log(?:ged|ging)? in|"
                 r"account was|recovery|two[\W_]?factor|device we (?:don[\W_]?t )?recognise|"
                 r"recognize|end every other session|lock you out)\b", blob):
        if re.search(r"\b(?:wasn[\W_]?t you|was this you|didn[\W_]?t do|"
                     r"not you|change your password|act quickly|straight away)\b", blob):
            add(OtherCategory.SECURITY, 2.6, "asks you to check a sign-in")
        else:
            add(OtherCategory.SECURITY, 1.4, "about account access")

    # Money, and which way it went
    if _MONEY.search(blob) or plain_money:
        if _SPENT.search(blob):
            add(OtherCategory.RECEIPT, 2.6, "an amount already taken")
        # A purchase names where; a month's summary names when.
        if _SPENT_AT.search(blob):
            add(OtherCategory.RECEIPT, 2.4, "an amount spent at a merchant")
        if _OWED.search(blob):
            add(OtherCategory.FINANCE, 2.6, "an amount still owed")
        # Money coming in is the account's news, not a purchase.
        if _MONEY_IN.search(blob):
            add(OtherCategory.FINANCE, 2.8, "money sent to you")
        if not _SPENT.search(blob) and not _OWED.search(blob):
            add(OtherCategory.FINANCE, 0.8, "an amount of money")

    # One word is not a parcel: an npm package, a printer driver and a
    # newspaper called the Courier are not on a van. Two are, or one with its
    # delivery.
    if _PARCEL.search(blob) and (len(set(_PARCEL.findall(blob))) >= 2
                                 or _DELIVERED.search(blob)):
        add(OtherCategory.SHIPPING, 2.4, "describes a parcel")
    if _DELIVERY_WINDOW.search(blob):
        add(OtherCategory.SHIPPING, 2.2, "gives a delivery window")

    travel_hits = 0
    if _FLIGHT.search(f"{subject} {body}"):
        travel_hits += 1
    if _BOOKING_REF.search(f"{subject} {body}"):
        travel_hits += 1
    # Each thing said about a journey counts once: a stay has nights, an
    # arrival and a door code, and no flight number at all.
    travel_hits += min(2, len(set(_JOURNEY.findall(blob))))
    if travel_hits >= 2:
        add(OtherCategory.TRAVEL, 2.8, "a journey with a reference and a time")
    elif travel_hits == 1:
        add(OtherCategory.TRAVEL, 1.0, "something that reads like a journey")

    # Something happening at a time and a place
    if _CLOCK.search(blob) and _WEEKDAY.search(blob) and not bulk:
        add(OtherCategory.EVENT, 1.2, "a day and a time")

    # A person, rather than a system
    if not bulk and not robot and short and len(links) <= 1:
        voice = len(_PERSONAL_VOICE.findall(blob))
        if voice >= 2:
            add(OtherCategory.PERSONAL, 2.4, "written by a person, to a person")
        elif voice == 1:
            add(OtherCategory.PERSONAL, 1.2, "reads as written by hand")

    # Bulk mail: selling, or telling?
    if bulk:
        selling = len(_SELLING.findall(blob))
        editorial = len(_EDITORIAL.findall(blob))
        if selling > editorial:
            add(OtherCategory.PROMOTION, 1.4 + min(1.2, 0.4 * selling), "bulk mail with a pitch")
        elif editorial:
            add(OtherCategory.NEWSLETTER, 1.4 + min(1.2, 0.4 * editorial), "bulk mail with an editorial shape")
        elif len(_PERSONAL_VOICE.findall(blob)) >= 2 and not _SELLING.search(blob):
            # A letter to a list, from somebody writing as themselves.
            add(OtherCategory.NEWSLETTER, 1.0, "bulk mail written in the first person")
        else:
            add(OtherCategory.PROMOTION, 0.8, "bulk mail")

    return scores, notes

# Sentences that tell the reader to do something. Applicant-tracking mail
# buries the request in a cheerful acknowledgement, which phrase tables alone
# misfile.
_ACTION_PATTERNS: Tuple[Tuple[re.Pattern, float, str], ...] = tuple(
    (re.compile(pattern), weight, label)
    for pattern, weight, label in (
        (r"\bthe next step (?:in|of) the (?:application|hiring|interview) process\b",
         3.0, "names an explicit next step"),
        (r"\bthe next steps? (?:is|are) to\b", 3.0, "names an explicit next step"),
        # A step after it, not a portal note: "you must sign in with the
        # address you applied from" asks nothing of the application.
        (r"\byou (?:must|need to|will need to|are required to) (?:complete|submit|provide|"
         r"upload|fill|schedule|book|sign(?! (?:in|up|on|into)\b)|take|finish|send|return|"
         r"confirm|respond|reply|record|answer|attend|register)\b", 2.6, "tells you to act"),
        (r"\bwe (?:ask|require|request) that you\b", 2.6, "tells you to act"),
        (r"\bwe recommend that you complete\b", 2.6, "tells you to act"),
        (r"\bplease (?:complete|submit|provide|upload|fill|confirm|schedule|book|sign(?! (?:in|up|on|into)\b)|review|respond|reply|verify|click)\b",
         2.4, "asks you to do something"),
        (r"\bplease (?:complete|submit|provide|upload|fill in|sign|schedule|book)\b",
         2.2, "asks you to do something"),
        (r"\bkindly (?:complete|submit|provide|share|revert)\b", 2.2, "asks you to do something"),
        (r"\b(?:complete|submit) (?:the|this|your) (?:assessment|questionnaire|form|survey|test|application|profile)\b",
         2.8, "asks for a specific task"),
        (r"\baction (?:is )?required\b", 2.6, "is marked action required"),
        (r"\bas soon as possible\b", 1.2, "is time-bounded"),
        (r"\bwithin \d+ (?:hours|days|business days)\b", 1.8, "carries a deadline"),
        (r"\bby (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|\d{1,2}\s)", 1.2,
         "carries a deadline"),
        (r"\bexpires? (?:in|on|after)\b", 1.8, "carries an expiry"),
    )
)

#: An instruction in one of these is hypothetical ("if you need to reset your
#: password") or already done ("thank you for submitting your application"):
#: not a request.
_NOT_A_REQUEST = re.compile(
    r"(?:\bif\b|\bin case\b|\bshould you\b|\bunless\b|\bwhen you\b|"
    r"\bmay have been\b|\bin the event\b|\bwhenever\b|"
    r"\bthank(?:s| you) for\b|\btaking the time to\b|\byou have already\b|"
    r"\bthe time you (?:took|spent)\b|\byou took the time\b|"
    r"\bwe have received\b)[^.!?]{0,70}$"
    # "No action is required": the request negated where it stands.
    r"|\b(?:no|no further|not)\s*$"
)


#: "Next steps" as something that will happen later rather than something asked
#: now: acknowledgements end this way, and reading it as a request makes them
#: action items.
_PROMISED_STEPS = re.compile(
    r"(?:\bwe(?:'| a|'?ll| will)?\b[^.!?]{0,40}(?:contact|reach out|be in touch|"
    r"share|discuss|send|let you know|follow up|advise|update)|"
    r"\byou will (?:receive|hear|be (?:contacted|notified|informed))|"
    r"\b(?:if|should|when|once)\b[^.!?]{0,70}|"
    r"\bsomeone (?:will|from)\b[^.!?]{0,40}|"
    r"\bto discuss\b|\babout\b|\bwith\b|\bregarding\b|\bon\b)"
    r"[^.!?]{0,30}$"
)


def steps_are_only_promised(body: str) -> bool:
    """Whether every mention of next steps is a promise rather than a request.
    True means someone else will act later; False means at least one mention
    is addressed to the reader, or there is none.
    """
    mentions = list(re.finditer(r"\bnext steps?\b", body))
    if not mentions:
        return False
    for mention in mentions:
        lead = body[max(0, mention.start() - 100):mention.start()]
        if not _PROMISED_STEPS.search(lead):
            return False
    return True


def _is_a_real_request(body: str, start: int) -> bool:
    """Is the instruction at `start` addressed to the reader, right now?"""
    lead = body[max(0, start - 90):start]
    return not _NOT_A_REQUEST.search(lead)


@dataclass
class RuleVerdict:
    """What the rules engine concluded, and why."""

    is_job_related: bool
    category: Category
    other_category: OtherCategory
    confidence: float
    summary: str
    reasoning: str
    scores: Dict[str, float] = field(default_factory=dict)
    matched: Tuple[str, ...] = ()

    def to_payload(self) -> Dict[str, object]:
        """The same JSON shape the model backends produce, plus the phrases
        that fired and each category's score, which a model could only
        invent after the fact.
        """
        return {
            "summary": self.summary,
            "is_job_related": self.is_job_related,
            "category": self.category.value,
            "other_category": self.other_category.value,
            "confidence_score": round(self.confidence, 3),
            "reasoning": self.reasoning,
            "signals": list(self.matched),
            "scores": {name: round(value, 3)
                       for name, value in self.scores.items()},
        }


_CATEGORY_TABLES: Tuple[Tuple[Category, Tuple[Signal, ...]], ...] = (
    (Category.NOT_INTERESTED, REJECTION_SIGNALS),
    (Category.OFFER, OFFER_SIGNALS),
    (Category.INTERVIEW, INTERVIEW_SIGNALS),
    (Category.NEXT_STEPS, NEXT_STEPS_SIGNALS),
    (Category.APPLICATION_RECEIVED, APPLICATION_RECEIVED_SIGNALS),
    (Category.NETWORKING, NETWORKING_SIGNALS),
    (Category.UNSOLICITED, UNSOLICITED_SIGNALS),
)

#: How to settle a tie between two topics with the same score, matching the
#: system prompt; ties break toward the earlier entry. From specific to
#: generic, with the two catch-alls last: dict order would have made the answer
#: depend on how the tables happen to be written.
_TOPIC_PRECEDENCE: Tuple[OtherCategory, ...] = (
    OtherCategory.SECURITY,     # a code or a sign-in alert is unmistakable
    OtherCategory.TRAVEL,       # a flight number is not a metaphor
    OtherCategory.SHIPPING,     # nor is a tracking number
    OtherCategory.RECEIPT,      # money that already moved
    OtherCategory.FINANCE,      # money that has not
    # More specific than the three it is often mistaken for: a carol service is
    # an event, arrives as a newsletter and reads like a note from a friend.
    OtherCategory.CHURCH,
    OtherCategory.EVENT,
    OtherCategory.SOCIAL,
    OtherCategory.WORK,
    OtherCategory.NEWSLETTER,
    OtherCategory.PROMOTION,
    OtherCategory.SPAM,
    OtherCategory.PERSONAL,     # a register, not a subject
    OtherCategory.OTHER,        # the absence of an answer
)


#: Topics that describe how a message is packaged rather than what it is about.
#: A church bulletin is a newsletter in form and church mail in substance, and
#: belongs with the church mail. So these lose to a subject topic with real
#: evidence behind it, not a stray word.
FORM_TOPICS = frozenset({
    OtherCategory.NEWSLETTER,
    OtherCategory.PROMOTION,
    OtherCategory.PERSONAL,
    OtherCategory.OTHER,
})

#: What a subject topic must score to overrule the form: about two solid
#: phrases, so a stray word does not reclassify newsletters.
SUBJECT_BEATS_FORM = 4.0

#: And at least this share of the winning form score, so mostly-newsletter mail
#: stays a newsletter.
SUBJECT_BEATS_FORM_SHARE = 0.35


def _topic_rank(topic: "OtherCategory") -> int:
    """Lower is more specific. Anything unlisted sorts last."""
    try:
        return _TOPIC_PRECEDENCE.index(topic)
    except ValueError:
        return len(_TOPIC_PRECEDENCE)


#: The stages whose message opens with the thanks an acknowledgement is.
_ABSORB_THE_THANKS = frozenset({
    Category.OFFER, Category.INTERVIEW, Category.NEXT_STEPS, Category.NOT_INTERESTED,
})

_PRECEDENCE: Tuple[Category, ...] = (
    Category.UNSOLICITED,
    Category.OFFER,
    Category.INTERVIEW,
    Category.NEXT_STEPS,
    Category.NOT_INTERESTED,
    Category.NETWORKING,
    Category.APPLICATION_RECEIVED,
)


class RuleClassifier:
    """Scores an email against the signal tables, deterministically and
    offline. ``ruleset`` names a field overlay from :mod:`rulesets`, added
    to the shared hiring language; overlays are additive, so the wrong one
    costs recall, never correctness.
    """

    def __init__(self, threshold: float = 0.95, ruleset: str = "general") -> None:
        self.threshold = threshold
        import rulesets as _rulesets

        self.ruleset = _rulesets.get(ruleset)
        self._compiled: Dict[int, List[_Matcher]] = {}
        self._tables: Dict[Category, Tuple[Signal, ...]] = {}
        for category, table in _CATEGORY_TABLES:
            merged = table + self.ruleset.signals_for(category)
            self._tables[category] = merged
            self._compiled[id(merged)] = [_Matcher(signal) for signal in merged]

        #: Each table's phrases that say nothing about hiring by themselves.
        self._generic: Dict[Category, Tuple[Signal, ...]] = {}
        for category, merged in self._tables.items():
            generic = tuple(signal for signal in merged
                            if signal.phrase in GENERIC_STAGE_PHRASES)
            self._generic[category] = generic
            self._compiled[id(generic)] = [_Matcher(signal) for signal in generic]

        self._context = JOB_CONTEXT_SIGNALS + self.ruleset.context
        self._compiled[id(self._context)] = [_Matcher(s) for s in self._context]
        self._compiled[id(NON_JOB_SIGNALS)] = [_Matcher(s) for s in NON_JOB_SIGNALS]
        self._compiled[id(HIRING_SPECIFIC_SIGNALS)] = [
            _Matcher(s) for s in HIRING_SPECIFIC_SIGNALS]
        for table in TOPIC_SIGNALS.values():
            self._compiled[id(table)] = [_Matcher(signal) for signal in table]
        for table in CONDITIONAL_SIGNALS.values():
            self._compiled[id(table)] = [_Matcher(signal) for signal in table]

    @property
    def signal_count(self) -> int:
        return (
            sum(len(t) for t in self._tables.values())
            + len(self._context) + len(NON_JOB_SIGNALS)
            + sum(len(t) for t in TOPIC_SIGNALS.values())
        )

    def _score(
        self, table: Tuple[Signal, ...], subject: str, subject_tight: str,
        body: str, body_tight: str,
    ) -> Tuple[float, List[str]]:
        total, matched, _ = self._score_detail(table, subject, subject_tight, body, body_tight)
        return total, matched

    def _sender_backs(self, table: Tuple[Signal, ...], sender: str) -> bool:
        """Whether the sender's own address supports this topic."""
        if not sender:
            return False
        return any(
            matcher.signal.field == "sender" and matcher.sender_hit(sender)
            for matcher in self._compiled[id(table)]
        )

    def _score_detail(
        self, table: Tuple[Signal, ...], subject: str, subject_tight: str,
        body: str, body_tight: str, sender: str = "",
    ) -> Tuple[float, List[str], float]:
        """``(total, matched labels, strongest single signal weight)``."""
        total = 0.0
        strongest = 0.0
        matched: List[str] = []
        for matcher in self._compiled[id(table)]:
            signal = matcher.signal
            subject_hit = (
                matcher.hit(subject, subject_tight)
                if signal.field in ("any", "subject") else 0.0
            )
            body_hit = (
                matcher.hit(body, body_tight)
                if signal.field in ("any", "body") else 0.0
            )
            if signal.field == "sender":
                # Who sent it is worth more than what it says: a courier's own
                # domain settles the topic.
                if sender and matcher.sender_hit(sender):
                    total += signal.weight
                    strongest = max(strongest, signal.weight)
                    matched.append(signal.describe())
                continue
            if not (subject_hit or body_hit):
                continue
            # A phrase in the subject line is stated, not buried.
            quality = max(subject_hit, body_hit)
            contribution = signal.weight * quality * (1.5 if subject_hit else 1.0)
            total += contribution
            strongest = max(strongest, signal.weight * quality)
            label = signal.describe()
            matched.append(label if quality == 1.0 else f"{label} (loosely)")
        return total, matched, strongest

    def classify(
        self,
        subject: str = "",
        body: str = "",
        sender: str = "",
        links: Sequence[str] = (),
        list_unsubscribe: str = "",
        truncated: bool = False,
        attachments: Sequence[str] = (),
        _rereading: bool = False,
    ) -> RuleVerdict:
        subject = (subject or "")[:2000]
        body = _without_the_echo((body or "")[:MAX_SCANNED_CHARS])
        subject_n, subject_t = normalize(subject), tighten(subject)
        body_n, body_t = normalize(body), tighten(body)
        sender_n = normalize(sender)
        link_blob = unwrap_links(links)

        scores: Dict[Category, float] = {}
        matches: Dict[Category, List[str]] = {}
        strongest: Dict[Category, float] = {}
        for category, _base in _CATEGORY_TABLES:
            score, matched, peak = self._score_detail(
                self._tables[category], subject_n, subject_t, body_n, body_t
            )
            scores[category] = score
            matches[category] = matched
            strongest[category] = peak

        # Asked before the link evidence: a booking link says a meeting is
        # being arranged and nothing about what for.
        professional, professional_why = professional_context_score(
            subject_n, body_n, sender_n)
        meeting, meeting_why = meeting_request_score(subject_n, body_n)
        posting, posting_why = job_posting_score(subject_n, body_n, list_unsubscribe)
        # Job-search vocabulary counts as working context too: a bare invite
        # titled "Interview - Acme" says what it is.
        context_now, _context_why = self._score(
            self._context, subject_n, subject_t, body_n, body_t)
        named_process, _named_why = self._score(
            HIRING_SPECIFIC_SIGNALS, subject_n, subject_t, body_n, body_t)
        if _YOUR_ONSITE.search(f"{subject_n} {body_n}"):
            # "Your onsite" is the day of interviews itself.
            named_process = max(named_process, 1.0)
        # Who it came from is context too: a careers mailbox has one job, and
        # its mail is about that job even at four words, where no phrase table
        # reaches.
        from_hiring = hiring_mailbox(sender) or (
            "tracking-system" if any(domain in sender_n for domain in TRACKING_SYSTEMS)
            else "")
        # A people team also writes about the job the reader already has - a
        # review, leave, pay, training - and that is work mail, not hiring.
        if (re.search(r"people|human", from_hiring)
                and _CURRENT_JOB.search(f"{subject_n} {body_n}")
                and not _BEING_HIRED.search(f"{subject_n} {body_n}")):
            from_hiring = ""
        if from_hiring:
            context_now += 1.6
        working = (professional > 0.0 or context_now >= 1.0
                   or named_process > 0.0 or bool(from_hiring))

        # What a person reads that no phrase says: the terms of an offer, a
        # gentle no, a second meeting being arranged, a step asked for, an
        # application acknowledged in plain words, a stranger sounding you
        # out. Each needs a person or a hiring mailbox writing, and most of
        # them some working context, or a friend arranging a drink and a
        # landlord stating the rent would read as hiring.
        person = looks_like_a_person(sender) and not _robot_sender(sender_n)
        in_process = working or any(
            scores[c] >= 2.0 for c in (Category.INTERVIEW, Category.NEXT_STEPS,
                                       Category.OFFER, Category.APPLICATION_RECEIVED))
        read: List[Tuple[Category, float, List[str], float]] = []
        terms, terms_why = offer_terms_score(subject_n, body_n)
        # A letting agent's "paperwork attached, starting the 1st" has two of
        # these moves, and an applicant-tracking system's footer mentions
        # salary and benefits under anything: a person writing with working
        # context may have two, anybody else needs three.
        if terms and ((person and working and len(terms_why) >= 2)
                      or len(terms_why) >= 3):
            read.append((Category.OFFER, terms, terms_why, 2.0))
        let_down, let_down_why = let_down_score(subject_n, body_n)
        if let_down and (person or from_hiring or working):
            read.append((Category.NOT_INTERESTED, let_down, let_down_why, 2.0))
        see_you, see_you_why = see_you_score(subject_n, body_n)
        if see_you and (person or from_hiring) and in_process:
            read.append((Category.INTERVIEW, see_you, see_you_why, 2.4))
        asked, asked_why = step_asked_score(subject_n, body_n)
        if asked and (from_hiring or (person and in_process)):
            read.append((Category.NEXT_STEPS, asked, asked_why, 2.0))
        have_it, have_it_why = we_have_it_score(subject_n, body_n)
        if have_it and (from_hiring or working):
            read.append((Category.APPLICATION_RECEIVED, have_it, have_it_why, 2.0))
        headhunt, headhunt_why = headhunt_score(subject_n, body_n)
        # Not the reader's own process: a recruiter writing about it is in it.
        # A list footer on a recruiter's own note is a sequence tool; three
        # of the moves together are a pitch whatever sent it.
        if (headhunt and not _IN_A_PROCESS.search(f"{subject_n} {body_n}")
                and ((person and (not list_unsubscribe.strip() or len(headhunt_why) >= 3))
                     or agency_sender(sender_n))):
            read.append((Category.UNSOLICITED, headhunt, headhunt_why, 2.0))
        else:
            headhunt_why = []
        # An invitation to interview sent to a list: "multiple remote roles
        # with our partner companies", "spots are limited". Nobody's process.
        mass_moves = {found.group(0) for found in
                      _MASS_INVITATION.finditer(f"{subject_n} {body_n}")}
        # "Book your interview slot" is the pitch; only an application the
        # reader made says otherwise.
        massed = bool(list_unsubscribe.strip() and mass_moves
                      and (not from_hiring or len(mass_moves) >= 3)
                      and not _APPLIED.search(f"{subject_n} {body_n}")
                      and _INTERVIEW_OFFERED.search(f"{subject_n} {body_n}"))
        if massed:
            read.append((Category.UNSOLICITED, 3.6, ["an invitation sent to a list"], 2.0))
        networked, networked_why = networking_score(subject_n, body_n)
        # No application behind it, or it is that process's mail.
        if (networked and person and not list_unsubscribe.strip() and not from_hiring
                and not named_process
                and not _IN_A_PROCESS.search(f"{subject_n} {body_n}")
                and not _PROGRAMME_SOLD.search(f"{subject_n} {body_n}")):
            read.append((Category.NETWORKING, networked, networked_why, 2.0))
        relayed, relayed_why = relay_score(subject_n, body_n, sender_n)
        if relayed:
            read.append((Category.UNSOLICITED, relayed, relayed_why, 2.4))
        call, call_why = call_score(subject_n, body_n, attachments)
        if call and (person or from_hiring):
            read.append((Category.INTERVIEW, call, call_why, 2.4))
        # What the message states in so many words: a decision, a step asked
        # for, a meeting arranged, an application acknowledged. Stated plainly
        # each is decisive, and each still needs the sender or the process
        # behind it: a bank also receives applications.
        stated = statements.read(subject, body)
        said: Dict[Category, List[str]] = {}
        # A university, a lender and a landlord all receive applications and
        # turn them down; a statement is a hiring one when something besides
        # the word "application" says hiring, and nothing says another world.
        # A school's, a hospital's or a court's own people team is an
        # employer, writing about its own vacancies, not another world.
        elsewhere_sender = ((0.0, "") if (from_hiring or _EMPLOYER_SIDE.search(sender_n))
                            else other_world_sender(sender_n))
        hiring_said = bool(
            from_hiring or named_process
            or any(domain in link_blob or domain in sender_n for domain in ATS_LINK_DOMAINS)
            or _APPLIED_FOR_A_TITLE.search(f"{subject_n} {body_n}")
            or agency_sender(sender_n)
            or (_HIRING_WORDS.search(f"{subject_n} {body_n}") and not elsewhere_sender[0]))
        offer_unsaid = False
        if not hiring_said:
            # Except a move that says hiring itself, a title joined; and the
            # business of an offer from a person writing for an organisation,
            # read but looked at before it is filed.
            kept = [found for found in stated.offers if found.move in statements.SELF_EVIDENT]
            if (not kept and _NAMED_MAILBOX.search(sender_n)
                    and not _MAIL_PROVIDERS.search(sender_n)
                    and not list_unsubscribe.strip()
                    and not _SALE.search(f"{subject_n} {body_n}")):
                kept = [found for found in stated.offers
                        if found.move in statements.SOFT_EVIDENT]
                offer_unsaid = bool(kept)
            stated = statements.Reading(offers=tuple(kept))
        if stated.declines and (person or from_hiring or working):
            said[Category.NOT_INTERESTED] = stated.moves("declines")
        if stated.offers and (person or from_hiring or working):
            said[Category.OFFER] = stated.moves("offers")
        if stated.requests and (from_hiring or working or (person and in_process)):
            said[Category.NEXT_STEPS] = stated.moves("requests")
        # A meeting stated with nothing about hiring in it - no role, no
        # application, no interview, nobody whose job is hiring - may be a
        # career conversation or a facilitator's coffee. What else the message
        # holds may still read it as an interview; the statement alone does
        # not make it one.
        anchored = bool(from_hiring or named_process
                        or _HIRING_ANCHOR.search(f"{subject_n} {body_n}")
                        # A job title in the subject of a call is the role.
                        or _TITLE_WORDS.search(subject_n)
                        or any(domain in sender_n for domain in VIDEO_INTERVIEW_DOMAINS))
        if stated.invitations and anchored and (
                working or (in_process and (person or from_hiring))):
            said[Category.INTERVIEW] = stated.moves("invitations")
        progress = bool(set(stated.moves("acknowledgements")) & statements._PROGRESS)
        if stated.acknowledged and (from_hiring or (working and (
                not person or stated.names_an_application or progress))):
            said[Category.APPLICATION_RECEIVED] = stated.moves("acknowledgements")
        if "an offer not made" in stated.moves("declines"):
            # "We will not be extending an offer" is a decision, and the offer
            # in it is the thing not done.
            scores[Category.OFFER] = 0.0
            strongest[Category.OFFER] = 0.0
            matches[Category.OFFER].append("(discounted: the offer is one not made)")
        for category, moves in said.items():
            read.append((category, STATED_WEIGHT, ["it states " + move for move in moves],
                         2.0))
        # Readings by shape or by link, rather than by phrase.
        shaped = {category for category, _w, _why, _c in read}
        read_context = 0.0
        read_notes: List[str] = []
        for category, weight, why, carries in read:
            scores[category] += weight
            strongest[category] = max(strongest[category], weight)
            matches[category].append("read as " + " + ".join(why[:3]))
            read_context = max(read_context, min(carries, weight))
            read_notes.append(f"{category.label.lower()} by its shape")
        if read:
            working = True
            context_now = max(context_now, 1.0)

        # Only with the sender and the process established may the
        # conditional phrases count; without that they would file a
        # solicitor's letter under Offer.
        licensed = bool(from_hiring) or named_process > 0.0 or bool(read)
        if licensed:
            for category, table in CONDITIONAL_SIGNALS.items():
                extra, extra_matched, extra_peak = self._score_detail(
                    table, subject_n, subject_t, body_n, body_t)
                if not extra:
                    continue
                scores[category] += extra
                strongest[category] = max(strongest[category], extra_peak)
                matches[category].extend(extra_matched)

        # Link evidence, which outweighs prose
        if any(domain in link_blob for domain in SCHEDULING_LINK_DOMAINS):
            if working:
                scores[Category.INTERVIEW] += 3.0
                strongest[Category.INTERVIEW] = max(strongest[Category.INTERVIEW], 3.0)
                matches[Category.INTERVIEW].append("a scheduling link")
                shaped.add(Category.INTERVIEW)
            else:
                # Kept as a weak hint: still a meeting, just nobody's job
                # search.
                scores[Category.INTERVIEW] += 0.6
                matches[Category.INTERVIEW].append(
                    "a scheduling link, with nothing to say it is about work")
        if any(domain in link_blob for domain in ASSESSMENT_LINK_DOMAINS):
            scores[Category.NEXT_STEPS] += 3.0
            strongest[Category.NEXT_STEPS] = max(strongest[Category.NEXT_STEPS], 3.0)
            matches[Category.NEXT_STEPS].append("an assessment-platform link")
            shaped.add(Category.NEXT_STEPS)
        # On a one-way video platform the step asked for is the interview:
        # recording answers to an employer's questions is how it interviews,
        # and the reminder to finish it is a reminder of an interview.
        on_the_platform = any(domain in sender_n for domain in VIDEO_INTERVIEW_DOMAINS)
        if on_the_platform or ((working or from_hiring) and any(
                domain in link_blob for domain in VIDEO_INTERVIEW_DOMAINS)):
            scores[Category.INTERVIEW] += scores[Category.NEXT_STEPS]
            strongest[Category.INTERVIEW] = max(strongest[Category.INTERVIEW],
                                                strongest[Category.NEXT_STEPS])
            matches[Category.INTERVIEW].extend(
                ["a one-way video interview"] + matches[Category.NEXT_STEPS][:2])
            scores[Category.NEXT_STEPS] = 0.0
            strongest[Category.NEXT_STEPS] = 0.0
            shaped.add(Category.INTERVIEW)
            if Category.NEXT_STEPS in said:
                said.setdefault(Category.INTERVIEW, []).extend(said.pop(Category.NEXT_STEPS))
        if meeting and working:
            # Neither half is worth much alone ("let's find 20 minutes", "the
            # team"); together they are someone proposing to talk about your
            # working life.
            weight = min(3.0, meeting * min(1.0, max(professional, context_now) / 2.0))
            scores[Category.INTERVIEW] += weight
            strongest[Category.INTERVIEW] = max(strongest[Category.INTERVIEW], weight)
            because = (professional_why[0] if professional_why
                       else "job-search wording elsewhere in the message")
            matches[Category.INTERVIEW].append(meeting_why[0] + ", and " + because)
        elif meeting and not working:
            # A meeting with nothing tying it to work: "pick a time" and a
            # calendar link are how a dentist, a school and a sales team write.
            if scores[Category.INTERVIEW]:
                scores[Category.INTERVIEW] *= 0.3
                strongest[Category.INTERVIEW] *= 0.3
                matches[Category.INTERVIEW].append(
                    "(discounted: a meeting, but nothing says it is about work)")
        # In bulk mail an interview nobody is offered is a word in the copy:
        # "our clients get 3x more interview invitations". An interview held
        # out to the reader, an application of theirs, a hiring system or a
        # booking link keeps it.
        if (list_unsubscribe.strip() and scores[Category.INTERVIEW]
                and not from_hiring and Category.INTERVIEW not in said
                and not any(domain in link_blob or domain in sender_n
                            for domain in ATS_LINK_DOMAINS)
                and not _INTERVIEW_OFFERED.search(f"{subject_n} {body_n}")
                and not _APPLIED.search(f"{subject_n} {body_n}")
                and not any(domain in link_blob for domain in SCHEDULING_LINK_DOMAINS)):
            scores[Category.INTERVIEW] *= 0.3
            strongest[Category.INTERVIEW] *= 0.3
            matches[Category.INTERVIEW].append(
                "(discounted: an interview in bulk mail, offered to nobody)")

        # An acknowledgement is the absence of a decision. A rejection
        # acknowledges the application too, and there the decision is the
        # point, so this never outweighs one.
        acknowledged, acknowledged_why = acknowledgement_score(subject_n, body_n)
        if acknowledged:
            decided = max(scores[Category.NOT_INTERESTED], scores[Category.OFFER])
            if decided >= QUALIFY_SCORE:
                acknowledged = 0.0
                acknowledged_why = []
            else:
                scores[Category.APPLICATION_RECEIVED] += acknowledged
                strongest[Category.APPLICATION_RECEIVED] = max(
                    strongest[Category.APPLICATION_RECEIVED], acknowledged)
                matches[Category.APPLICATION_RECEIVED].extend(acknowledged_why[:2])

        ats_present = any(domain in link_blob or domain in sender_n for domain in ATS_LINK_DOMAINS)

        job_bonus = 0.0
        structure_notes: List[str] = []

        # A subject line is short, deliberate and written last: the most
        # reliable single feature in applicant-tracking mail.
        for pattern, weight, label in SUBJECT_PATTERNS:
            if pattern.search(subject_n):
                job_bonus += weight
                if label not in structure_notes:
                    structure_notes.append(label)

        if len(body_n.strip()) < 40:
            # Nothing but a subject line, which an applicant-tracking system
            # writes to a formula: read it the way the formula means it. A
            # modest score, so it is filed as what it is and still looked at.
            for label in structure_notes:
                seed = _SUBJECT_ALONE.get(label)
                if seed is None:
                    continue
                category, weight = seed
                scores[category] += weight
                strongest[category] = max(strongest[category], weight)
                matches[category].append(label + ", with no body to say more")
            if _VERIFY_SUBJECT.search(subject_n):
                scores[Category.NEXT_STEPS] += 2.6
                strongest[Category.NEXT_STEPS] = max(strongest[Category.NEXT_STEPS], 2.6)
                matches[Category.NEXT_STEPS].append("a subject asking you to verify an account")

        # Sender and structure
        agency = any(hint in sender_n.replace(" ", "") for hint in AGENCY_SENDER_HINTS)
        if agency:
            scores[Category.UNSOLICITED] += 1.2
            matches[Category.UNSOLICITED].append("an agency-style sender address")

        raw_scores = dict(scores)
        replying = bool(re.match(r"^\s*(re|fw|fwd)\s*:", subject or "", re.I))
        if replying and not _follows_up_itself(body, sender):
            # An existing thread is strong evidence the user started it.
            scores[Category.UNSOLICITED] *= 0.35
            matches[Category.UNSOLICITED].append("(discounted: this is a reply in an existing thread)")

        job_score, job_matches = self._score(
            self._context, subject_n, subject_t, body_n, body_t
        )
        job_score += job_bonus
        job_matches.extend(structure_notes)
        if from_hiring:
            job_score += 1.6
            job_matches.append(f"a {from_hiring} mailbox wrote it")
        if read_context:
            job_score += read_context
            job_matches.extend(read_notes[:2])
        digest = application_digest(subject_n, (body or "").lower())
        if digest:
            job_score += 3.0
            job_matches.append(digest)
        if posting:
            # A description is job-search material without one hiring-process
            # word: it is all headings.
            job_score += posting
            job_matches.append("it reads as a job description (" +
                               ", ".join(posting_why[:2]) + ")")
        if meeting and working:
            job_score += min(2.4, meeting * min(1.0,
                                                max(professional, context_now) / 2.0))
            job_matches.append("a working conversation is being proposed")
        if acknowledged:
            job_score += min(2.0, acknowledged)
            job_matches.append("it acknowledges an application ("
                               + ", ".join(acknowledged_why[:2]) + ")")
        non_job_score, non_job_matches = self._score(
            NON_JOB_SIGNALS, subject_n, subject_t, body_n, body_t
        )
        if ats_present:
            job_score += 2.5
            job_matches.append("an applicant-tracking-system address")

        # Only once the message is established as job mail: "please confirm
        # your email address" is a request in any inbox. And not in a job
        # description, whose "apply by Friday" is the posting's, not a step
        # of any application.
        if (job_score >= 2.0 or scores[Category.APPLICATION_RECEIVED] >= 2.5) and posting < 2.6:
            action_score = 0.0
            action_notes: List[str] = []
            peak_action = 0.0
            for pattern, weight, label in _ACTION_PATTERNS:
                match = next(
                    (m for m in pattern.finditer(body_n)
                     if _is_a_real_request(body_n, m.start())),
                    None,
                )
                if match is None:
                    continue
                # A request in the closing half is what the reader is left
                # with, where applicant-tracking mail puts it.
                position = match.start() / max(1, len(body_n))
                action_score += weight * (1.0 + 0.35 * position)
                peak_action = max(peak_action, weight)
                if label not in action_notes:
                    action_notes.append(label)
            if action_score:
                scores[Category.NEXT_STEPS] += action_score
                strongest[Category.NEXT_STEPS] = max(
                    strongest[Category.NEXT_STEPS], peak_action
                )
                matches[Category.NEXT_STEPS].extend(action_notes[:3])

        # "We will reach out to discuss next steps" is a promise, not a step
        # for the reader.
        if scores[Category.NEXT_STEPS] and steps_are_only_promised(body_n):
            scores[Category.NEXT_STEPS] = max(
                0.0, scores[Category.NEXT_STEPS] - PROMISED_STEPS_WEIGHT)
            matches[Category.NEXT_STEPS].append(
                "(discounted: next steps are promised, not asked for)")

        # Precedence is an order, not a tiebreak: an offer outranks the
        # paperwork attached to it even when the paperwork says more.
        def qualifies(category: Category) -> bool:
            bar = (
                QUALIFY_SCORE_UNSOLICITED
                if category is Category.UNSOLICITED else QUALIFY_SCORE
            )
            return scores[category] >= bar

        # The order settles it among categories in the same league. One
        # dwarfed by another - an acknowledgement fourteen points strong with
        # one incidental instruction in it - is not what the message is.
        top = max(scores.values(), default=0.0)
        qualified = [category for category in _PRECEDENCE
                     if qualifies(category)
                     and (scores[category] >= top * DWARFED
                          or strongest[category] >= DECISIVE_WEIGHT)]
        # What the message states outranks what its phrases suggest: "any offer
        # of employment is contingent on" is in the footer of a rejection, and
        # "please sign in to see your status" in an acknowledgement. Not
        # unsolicited mail, which is told by where it came from, not by what
        # it says.
        if any(category in said for category in qualified):
            qualified = [category for category in qualified
                         if category in said or category in shaped
                         or category is Category.UNSOLICITED]
        if (Category.NETWORKING in shaped
                and set(networked_why) & {"a referral", "an introduction", "a reference"}
                and not set(headhunt_why) & {"a recruiter's own words", "a client's role"}):
            # Help offered by somebody the reader knows, not a recruiter's
            # pitch: a referral is networking.
            qualified = [category for category in qualified
                         if category is not Category.UNSOLICITED]
        if massed:
            qualified = [category for category in qualified
                         if category is not Category.INTERVIEW]
        if (Category.APPLICATION_RECEIVED in said and Category.INTERVIEW not in said
                and not any(domain in link_blob for domain in SCHEDULING_LINK_DOMAINS)):
            # An acknowledgement that sketches the process to come ("a call
            # with a recruiter, then a final round") arranges none of it.
            qualified = [category for category in qualified
                         if category is not Category.INTERVIEW]
        if (Category.NETWORKING in shaped and Category.INTERVIEW not in said
                and not _IN_A_PROCESS.search(f"{subject_n} {body_n}")):
            # A chat proposed with no application behind it is networking:
            # an interview is a step of a process the reader is in.
            qualified = [category for category in qualified
                         if category is not Category.INTERVIEW]
        if (Category.NOT_INTERESTED in said and Category.INTERVIEW not in said
                and not any(domain in link_blob for domain in SCHEDULING_LINK_DOMAINS)):
            # Only a concrete invitation outranks a decision: "we won't be
            # moving you forward to the final round" names the round it
            # takes away.
            qualified = [category for category in qualified
                         if category is not Category.INTERVIEW]
        if qualified:
            best_category = qualified[0]
        else:
            best_category = max(_PRECEDENCE, key=lambda c: (scores[c], -_PRECEDENCE.index(c)))
        best_score = scores[best_category]
        # Every later stage opens by thanking you for applying, so where the
        # message states its decision, step or meeting, the acknowledgement
        # in it is part of that and not a rival reading of the message.
        absorbed = (Category.APPLICATION_RECEIVED
                    if best_category in said and best_category in _ABSORB_THE_THANKS
                    else None)
        runner_up = max(
            (score for category, score in scores.items()
             if category is not best_category and category is not absorbed),
            default=0.0,
        )

        # Measured before the reply discount: a reply in a thread is more
        # clearly part of a job search, not less.
        job_evidence = job_score + max(max(raw_scores.values(), default=0.0), best_score)
        if not (working or ats_present or posting):
            # Nothing says hiring: the stage phrases any form uses count for
            # nothing as evidence that this is job mail at all.
            generic = {category: self._score(self._generic[category], subject_n,
                                             subject_t, body_n, body_t)[0]
                       for category in raw_scores if self._generic[category]}
            job_evidence = job_score + max(
                (max(0.0, score - generic.get(category, 0.0))
                 for category, score in raw_scores.items()), default=0.0)

        # "Application", "interview" and "offer" belong to other parts of life
        # too; where a message is plainly about one of those, it counts against
        # the job reading.
        selling, selling_why = solicitation_score(subject_n, body_n, subject)
        if selling >= 2.6:
            # Unsolicited mail borrows every vocabulary, job mail's included:
            # "fill out the form below" reads as a next step until you see what
            # is being asked.
            job_evidence = max(0.0, job_evidence - selling)
            non_job_score += selling
            non_job_matches.append(
                "reads as unsolicited commercial mail (" + ", ".join(selling_why[:2]) + ")")

        # A sale to job seekers - a resume rewritten, a course, coaching - in
        # bulk: the job words describe its audience, not the reader's search.
        if (list_unsubscribe.strip() and _SALE.search(f"{subject_n} {body_n}")
                and not from_hiring and not _APPLIED.search(f"{subject_n} {body_n}")):
            job_evidence = 0.0
            non_job_score += 2.4
            non_job_matches.append("a sale to people looking for work")

        blast, blast_why = job_board_blast(subject_n, body_n, list_unsubscribe)
        if blast:
            job_evidence = max(0.0, job_evidence - blast)
            non_job_score += blast
            non_job_matches.append(blast_why)
        # A talent network keeping its mailing list is a list, whatever it
        # asks you to click.
        network = len(set(_TALENT_NETWORK.findall(f"{subject_n} {body_n}")))
        # Judged on the words alone: a list inviting you to "join our talent
        # community" states an invitation of a kind, and is still a list.
        worded = max((score - (STATED_WEIGHT if category in said else 0.0)
                      for category, score in raw_scores.items()), default=0.0)
        if ((network >= 2 or (network and list_unsubscribe.strip()))
                and worded < QUALIFY_SCORE):
            job_evidence = 0.0
            non_job_score += 3.0
            non_job_matches.append("a talent network keeping its mailing list")

        # A one-time code is the account's business whoever sends it: a
        # careers site confirming who is signing in has asked nothing of the
        # application, and the code is wanted in the inbox, now.
        if (len(body_n) < ONE_TIME_CODE_BODY
                and _ONE_TIME_CODE.search(f"{subject_n} {body_n}")
                and _NAMED_CODE.search(f"{subject_n} {body_n}")):
            job_evidence = 0.0
            non_job_score += 3.0
            non_job_matches.append("a one-time code, which is the account's business")
        elif (len(body_n) < ONE_TIME_CODE_BODY
                and _PASSWORD_RESET.search(f"{subject_n} {body_n}")):
            job_evidence = 0.0
            non_job_score += 3.0
            non_job_matches.append("a password reset, which is the account's business")

        # A sender pretending to be a service, or text written to the machine
        # reading it: whatever it says about hiring is bait.
        # An application for a loan, a course or a dog, with no word of a job
        # in it; the reader interviewing somebody else; an interview for a
        # podcast.
        blob_n = f"{subject_n} {body_n}"
        elsewhere_now = ""
        if _OTHER_APPLICATION.search(blob_n) and not _HIRING_WORDS.search(blob_n):
            elsewhere_now = "an application for something other than a job"
        elif _INTERVIEWER.search(blob_n):
            elsewhere_now = "the reader is the one interviewing"
        elif _MEDIA_INTERVIEW.search(blob_n) or (
                _ON_A_PROGRAMME.search(blob_n) and re.search(r"\binterview", blob_n)):
            elsewhere_now = "an interview for a programme or a publication"
        elif _SWINDLE[3].search(blob_n):
            # Gift cards bought, a cheque deposited and the rest wired on: no
            # employer asks for money like that.
            elsewhere_now = "money asked for the way a swindle asks"
        if (not elsewhere_now and list_unsubscribe.strip()
                and _CAREER_INSTITUTION.search(sender_n)
                and not _IN_A_PROCESS.search(blob_n)):
            elsewhere_now = "a school's or a board's list about careers"
        if elsewhere_now:
            job_evidence = 0.0
            non_job_score += 3.0
            non_job_matches.append(elsewhere_now)
            if (elsewhere_now.startswith("an interview for") or (
                    elsewhere_now.startswith("an application")
                    and not re.search(r"\b(?:loan|mortgage|credit|account|insurance)\b",
                                      blob_n))):
                # Another world's business, which is the Other topic.
                elsewhere_extra = 3.6
            else:
                elsewhere_extra = 0.0
        else:
            elsewhere_extra = 0.0
        bait = scam_domain(sender_n) or (
            "words written to a machine reading the mail"
            if _TO_A_MACHINE.search(f"{subject_n} {body_n}") else "")
        if bait:
            job_evidence = 0.0
            non_job_score += 3.0
            non_job_matches.append(bait)
        elsewhere, elsewhere_why = other_world_context(subject_n, body_n)
        if elsewhere_extra:
            elsewhere = max(elsewhere, elsewhere_extra)
            elsewhere_why = elsewhere_why or elsewhere_now
        if elsewhere:
            job_evidence = max(0.0, job_evidence - elsewhere)
            non_job_score += elsewhere
            non_job_matches.append(elsewhere_why)
        # A court, a surgery, a university or a letting agent writing about
        # an application: the sender settles it, and the job reading needs
        # twice the usual evidence to stand.
        bar = 2.4
        far, far_why = elsewhere_sender
        if far:
            job_evidence = max(0.0, job_evidence - far)
            non_job_score += far
            non_job_matches.append(far_why)
            bar = 4.8
            if elsewhere:
                # A word of that world from a sender of it: two things
                # agreeing, which is what settles a topic.
                elsewhere += far
        kith, kith_why = kith_score(subject_n, body_n, sender)
        if kith and Category.NETWORKING not in shaped:
            job_evidence = max(0.0, job_evidence - kith)
            non_job_score += kith
            non_job_matches.append(kith_why)

        looks_job_related = job_evidence >= max(bar, non_job_score * 0.9)
        # A list writing about hiring is a newsletter: nothing in it is a
        # step of anybody's application.
        if (looks_job_related and list_unsubscribe.strip() and best_score < MIN_SCORE
                and not digest and _EDITORIAL.search(f"{subject_n} {body_n}")):
            looks_job_related = False
            non_job_matches.append("a mailing list talking about hiring, with no step in it")

        if call and looks_job_related:
            pass
        if not looks_job_related:
            return self._non_job_verdict(
                subject_n, subject_t, body_n, body_t, sender_n,
                non_job_score, non_job_matches, job_evidence, truncated,
                list_unsubscribe, links,
                # The raw text as well: case is half of what makes "BA1442 LHR
                # to EDI" a flight.
                raw_subject=subject, raw_body=body, raw_sender=sender,
                elsewhere=elsewhere, elsewhere_why=elsewhere_why,
                kith=kith, kith_why=kith_why,
            )

        if best_score < MIN_SCORE or digest:
            return RuleVerdict(
                is_job_related=True,
                category=Category.UNCLASSIFIED_OTHER,
                other_category=OtherCategory.NOT_APPLICABLE,
                confidence=min(0.55, 0.25 + job_evidence / 20.0),
                summary=_summarise(subject, sender, "This looks job related, but no category fits it."),
                reasoning=(
                    "The local rules engine found job-search context "
                    f"({', '.join(job_matches[:4]) or 'weak signals'}) but no category "
                    "reached its evidence threshold. Routed to Needs Review."
                ),
                scores={c.value: round(v, 2) for c, v in scores.items()},
                matched=tuple(job_matches[:6]),
            )

        # A meeting with nothing about hiring in it - no role, no
        # application, no interview, nobody whose job is hiring - may be a
        # career conversation or a facilitator's coffee: read as an
        # interview, and looked at before it is filed as one.
        unanchored = best_category is Category.INTERVIEW and not anchored
        rivals_stated = set(said) - {best_category, absorbed}
        if best_category in said and not rivals_stated:
            # One thing stated in so many words, and nothing else stated: as
            # sure as the rules get, unless a rival reading is strong enough
            # to be one. A category below its qualifying score is not a
            # reading of the message, only words that appear in it; nor is
            # one whose words are all in conditions ("if a role arises, we
            # will be in touch to arrange a call").
            plain: Optional[Dict[str, float]] = None
            rival = 0.0
            for category, score in scores.items():
                if category in (best_category, absorbed) or score < QUALIFY_SCORE:
                    continue
                if category not in shaped and not _rereading:
                    # Read again without the sentences that only might
                    # happen: whatever the rival still scores is what the
                    # message means by it.
                    if plain is None:
                        again = self.classify(
                            subject=subject,
                            body=statements.unconditional(subject, body),
                            sender=sender, list_unsubscribe=list_unsubscribe,
                            truncated=truncated, attachments=attachments,
                            _rereading=True)
                        plain = dict(again.scores) if again.is_job_related else {}
                    score = min(score, plain.get(category.value, 0.0))
                    if score < QUALIFY_SCORE:
                        continue
                rival = max(rival, score)
            confidence = self._confidence(
                max(best_score, SATURATION), rival,
                truncated, max(strongest[best_category], STATED_WEIGHT))
        else:
            confidence = self._confidence(
                best_score, runner_up, truncated, strongest[best_category]
            )
        if unanchored or (offer_unsaid and best_category is Category.OFFER):
            confidence = min(confidence, SOFT_EVIDENCE_CEILING)
        matched = matches[best_category]
        return RuleVerdict(
            is_job_related=True,
            category=best_category,
            other_category=OtherCategory.NOT_APPLICABLE,
            confidence=confidence,
            summary=_summarise(subject, sender, _CATEGORY_BLURB[best_category]),
            reasoning=_explain(best_category, matched, scores, truncated),
            scores={c.value: round(v, 2) for c, v in scores.items()},
            matched=tuple(matched[:8]),
        )

    def _non_job_verdict(
        self, subject_n, subject_t, body_n, body_t, sender_n,
        non_job_score, non_job_matches, job_evidence, truncated, list_unsubscribe,
        links: Sequence[str] = (),
        raw_subject: str = "", raw_body: str = "", raw_sender: str = "",
        elsewhere: float = 0.0, elsewhere_why: str = "",
        kith: float = 0.0, kith_why: str = "",
    ) -> RuleVerdict:
        topic_scores: Dict[OtherCategory, float] = {}
        topic_matches: Dict[OtherCategory, List[str]] = {}
        topic_peak: Dict[OtherCategory, float] = {}
        # What a long transactional message is about is said at the top; the
        # bottom is the service notice every message from that sender carries
        # ("your statement can be viewed any time" under each receipt,
        # "we will never ask for your password" under each statement). A
        # transactional phrase found only there counts for half. Not other
        # topics: a parish bulletin is about the parish to its last line.
        head_n, tail_n = _head_and_footer(body_n)
        head_t = tighten(head_n) if tail_n else body_t
        for topic, table in TOPIC_SIGNALS.items():
            footed = bool(tail_n) and topic in TRANSACTIONAL_TOPICS
            score, matched, peak = self._score_detail(
                table, subject_n, subject_t, head_n if footed else body_n,
                head_t if footed else body_t, sender_n
            )
            if footed:
                whole, whole_matched, whole_peak = self._score_detail(
                    table, subject_n, subject_t, body_n, body_t, sender_n)
                if whole > score:
                    score += (whole - score) * FOOTER_SHARE
                    matched = matched + [why + " (in the footer)" for why in whole_matched
                                         if why not in matched]
                    peak = max(peak, whole_peak * FOOTER_SHARE)
            topic_scores[topic] = score
            topic_matches[topic] = matched
            topic_peak[topic] = peak

        # Shape as opposed to wording, which phrase tables cannot see in typed
        # mail.
        shape, shape_notes = structural_topic_scores(
            subject_n, body_n, sender_n, list_unsubscribe, links)
        for topic, weight in shape.items():
            topic_scores[topic] = topic_scores.get(topic, 0.0) + weight
            topic_matches.setdefault(topic, []).extend(shape_notes.get(topic, ()))
            topic_peak[topic] = max(topic_peak.get(topic, 0.0), weight)
        # A scam wears a courier's or a bank's words: once the shape says
        # scam, those words count for little.
        scam_shaped = shape.get(OtherCategory.SPAM, 0.0) >= 2.4
        if scam_shaped:
            for topic in TRANSACTIONAL_TOPICS:
                if topic_scores.get(topic):
                    topic_scores[topic] *= 0.35
                    topic_matches.setdefault(topic, []).append(
                        "(discounted: the words of a courier or a bank in a scam's shape)")
        # The reader's pay is their employer's business, whatever money words
        # it uses.
        if _PAYROLL.search(f"{subject_n} {body_n}") and topic_scores.get(OtherCategory.FINANCE):
            topic_scores[OtherCategory.FINANCE] *= 0.5
            topic_matches.setdefault(OtherCategory.FINANCE, []).append(
                "(discounted: the reader's pay, which is their employer's business)")
        # A courier or a shop never writes from somebody's own mailbox: their
        # words from a friend or a neighbour are news, not a notice.
        if looks_like_a_person(raw_sender or sender_n) and _MAIL_PROVIDERS.search(sender_n):
            for topic in (OtherCategory.SHIPPING, OtherCategory.RECEIPT):
                if topic_scores.get(topic):
                    topic_scores[topic] *= 0.25
                    topic_matches.setdefault(topic, []).append(
                        "(discounted: somebody's own mailbox, which no courier or shop uses)")
        # A library's hold waits at its desk, not on a courier's van.
        if library_loan(f"{subject_n} {body_n}", sender_n) and topic_scores.get(
                OtherCategory.SHIPPING):
            topic_scores[OtherCategory.SHIPPING] *= 0.35
            topic_matches.setdefault(OtherCategory.SHIPPING, []).append(
                "(discounted: a library's hold, which no courier brings)")
        # Another world - a surgery, a court, a landlord - is an answer in
        # itself, and outranks the register it was written in. One word of
        # it is not: a bank's code naming a letting agent is still a code.
        if elsewhere >= 3.6:
            topic_scores[OtherCategory.OTHER] = (
                topic_scores.get(OtherCategory.OTHER, 0.0) + elsewhere)
            topic_peak[OtherCategory.OTHER] = max(
                topic_peak.get(OtherCategory.OTHER, 0.0), elsewhere)
            topic_matches.setdefault(OtherCategory.OTHER, []).append(elsewhere_why)
        if kith:
            topic_scores[OtherCategory.PERSONAL] = (
                topic_scores.get(OtherCategory.PERSONAL, 0.0) + kith)
            topic_peak[OtherCategory.PERSONAL] = max(
                topic_peak.get(OtherCategory.PERSONAL, 0.0), kith)
            topic_matches.setdefault(OtherCategory.PERSONAL, []).append(kith_why)
        # A swindle borrows a trusted voice - a pastor's, a manager's, a
        # mother's, a payroll team's - and those words, family's included,
        # count for little in it.
        favour = favour_scam(f"{subject_n} {body_n}")
        if favour or job_swindle(f"{subject_n} {body_n}"):
            voices = (OtherCategory.PERSONAL, OtherCategory.WORK, OtherCategory.CHURCH)
            for topic in voices:
                if topic_scores.get(topic):
                    topic_scores[topic] *= 0.35
                    topic_matches.setdefault(topic, []).append(
                        "(discounted: a trusted voice borrowed to ask for money)")
        if favour:
            # However warmly it is written: nobody who knows the reader asks
            # for the codes.
            topic_scores[OtherCategory.SPAM] = max(
                topic_scores.get(OtherCategory.SPAM, 0.0),
                max(topic_scores.get(topic, 0.0) for topic in voices) + 1.0)
        worship, settled = church_sender(raw_sender or sender_n)
        # What a church writes about, beyond the denomination's own name,
        # which a Methodist hospital's footer carries too.
        churchly = topic_scores.get(OtherCategory.CHURCH, 0.0) >= 2.4 and any(
            not _DENOMINATION.search(found)
            for found in topic_matches.get(OtherCategory.CHURCH, []))
        church_said = bool(worship and (settled or churchly) and not scam_shaped)
        # A death notice from a sender that may be a parish is most likely
        # the parish's. Most likely is all: a club mourns its members too.
        mourned = bool(worship and not church_said and not scam_shaped
                       and _A_DEATH_NOTICE.search(f"{subject_n} {body_n}"))
        if church_said or mourned:
            # The prompt's own rule: a place of worship's mail is church mail,
            # its giving included, and preferred over a newsletter, an event
            # or a personal note - a bulletin is a newsletter in form, and a
            # death in the congregation is written personally. A security
            # code from one is still a code, and a swindle in a pastor's name
            # is still a swindle.
            rivals = max((score for topic, score in topic_scores.items() if topic not in (
                OtherCategory.CHURCH, OtherCategory.SECURITY, OtherCategory.SPAM)),
                default=0.0)
            topic_scores[OtherCategory.CHURCH] = max(
                topic_scores.get(OtherCategory.CHURCH, 0.0) + 3.0, rivals + 1.0)
            topic_peak[OtherCategory.CHURCH] = max(
                topic_peak.get(OtherCategory.CHURCH, 0.0), 3.0)
            topic_matches.setdefault(OtherCategory.CHURCH, []).append(
                f"a place of worship wrote it ({worship})" if church_said
                else f"a death notice from what may be a parish ({worship})")

        # Three more ways to recognise a message with no word saying what it
        # is: the mailbox, the shapes in it, and whether it reads as one person
        # to another. How much of each topic's score came from these: they may
        # decide which topic wins, never how certain that is, so they never
        # move mail unasked.
        soft: Dict[OtherCategory, float] = {}
        for source, notes in (
            sender_purpose(raw_sender or sender_n),
            sender_sector(raw_sender or sender_n),
            entity_scores(subject_n, body_n, raw_subject, raw_body),
        ):
            for topic, weight in source.items():
                topic_scores[topic] = topic_scores.get(topic, 0.0) + weight
                soft[topic] = soft.get(topic, 0.0) + weight
                topic_matches.setdefault(topic, []).extend(notes[:2])

        chatty, chatty_why = personal_register(
            raw_subject or subject_n, raw_body or body_n,
            raw_sender or sender_n, list_unsubscribe, links)
        # A recruiter is a person too: the register says who wrote it, not what
        # it is about, so it only speaks where nothing else does, or friendly
        # interviews and offers become "personal".
        hard_elsewhere = max(
            (score - soft.get(topic, 0.0)
             for topic, score in topic_scores.items()
             if topic is not OtherCategory.PERSONAL),
            default=0.0)
        if chatty and hard_elsewhere >= MIN_SCORE:
            chatty, chatty_why = 0.0, []
        if chatty:
            topic_scores[OtherCategory.PERSONAL] = (
                topic_scores.get(OtherCategory.PERSONAL, 0.0) + chatty)
            soft[OtherCategory.PERSONAL] = (
                soft.get(OtherCategory.PERSONAL, 0.0) + chatty)
            topic_matches.setdefault(OtherCategory.PERSONAL, []).extend(
                chatty_why[:3])

        if list_unsubscribe:
            for topic in (OtherCategory.NEWSLETTER, OtherCategory.PROMOTION, OtherCategory.SOCIAL):
                topic_scores[topic] += 0.8
            topic_scores[OtherCategory.PERSONAL] = max(0.0, topic_scores[OtherCategory.PERSONAL] - 1.5)
            # Transactional mail rarely carries an unsubscribe header; where
            # one is present, travel and shopping words are usually metaphor
            # ("check-in is now open" selling a course). A discount, not a
            # veto, so a genuine confirmation with the header still wins on its
            # evidence.
            for topic in TRANSACTIONAL_TOPICS:
                vouched = self._sender_backs(TOPIC_SIGNALS[topic], sender_n)
                topic_scores[topic] *= (
                    TRANSACTIONAL_IN_BULK if vouched
                    else TRANSACTIONAL_IN_BULK_UNVOUCHED
                )

        # Words decide when there are words; shape decides when there are none.
        # A bank's one-time code is a security notice: the sender being a bank
        # plus an amount of money once outscored the code itself.
        hard = {topic: score - soft.get(topic, 0.0)
                for topic, score in topic_scores.items()}

        def strength(topic) -> tuple:
            """What settles a tie, in order: the score, then the strongest
            single phrase, then how much of the score was words rather than
            shape, then the written precedence. Each step is a reason; dict
            order was not.
            """
            return (round(topic_scores[topic], 6),
                    round(topic_peak.get(topic, 0.0), 6),
                    round(hard.get(topic, 0.0), 6),
                    -_topic_rank(topic))

        spoken_for = [topic for topic, score in hard.items() if score >= MIN_SCORE]
        if spoken_for:
            best_topic = max(spoken_for, key=strength)
        else:
            best_topic = max(topic_scores, key=strength)

        # What it is about beats how it is written: Newsletter, Promotion and
        # Personal describe form, every other topic a subject, and a subject
        # with real evidence is the more useful answer.
        if best_topic in FORM_TOPICS:
            form_score = topic_scores.get(best_topic, 0.0)
            floor = max(SUBJECT_BEATS_FORM,
                        form_score * SUBJECT_BEATS_FORM_SHARE)
            subjects = [topic for topic, score in topic_scores.items()
                        if topic not in FORM_TOPICS
                        and hard.get(topic, 0.0) >= floor]
            if subjects:
                best_topic = max(subjects, key=strength)
                topic_matches.setdefault(best_topic, []).append(
                    "about this rather than merely written like a "
                    f"{OtherCategory(best_topic).label.lower()}"
                    if False else "what it is about, not how it is written")
        best = topic_scores[best_topic]
        ranked = sorted(topic_scores.values(), reverse=True)
        runner_up = ranked[1] if len(ranked) > 1 else 0.0

        if (best < MIN_SCORE and len(body_n.strip()) < 40 and not list_unsubscribe.strip()
                and looks_like_a_person(raw_sender or sender_n)):
            # A line from a person's own address with nothing under it: a
            # test, a nudge, a "call me". Theirs, whatever it is.
            best_topic, best, runner_up = OtherCategory.PERSONAL, MIN_SCORE, 0.0
            topic_matches[OtherCategory.PERSONAL] = ["a person's own address, and nothing else"]
        if best < MIN_SCORE:
            best_topic, best, runner_up = OtherCategory.OTHER, max(best, non_job_score), 0.0
            topic_matches[OtherCategory.OTHER] = non_job_matches

        confidence = self._confidence(
            best, runner_up, truncated, topic_peak.get(best_topic, 0.0)
        )
        if best_topic is OtherCategory.OTHER:
            confidence = min(confidence, 0.70)
        # A reading held up mostly by shape is a good guess, not a certainty;
        # without this, wrong answers passed the filing threshold.
        gentle = soft.get(best_topic, 0.0)
        if best > 0 and gentle >= best * 0.5:
            confidence = min(confidence, SOFT_EVIDENCE_CEILING)
        # A sender that may be a place of worship, with nothing in the message
        # to say so: if it is one, this is church mail, which the prompt
        # prefers over a newsletter, an event or a personal note.
        if worship and not church_said and best_topic in (
                OtherCategory.NEWSLETTER, OtherCategory.EVENT, OtherCategory.PERSONAL,
                OtherCategory.CHURCH):
            confidence = min(confidence, SOFT_EVIDENCE_CEILING)

        matched = topic_matches.get(best_topic, [])
        return RuleVerdict(
            is_job_related=False,
            category=Category.UNCLASSIFIED_OTHER,
            other_category=best_topic,
            confidence=confidence,
            summary=_summarise(
                "", "",
                f"This looks like {best_topic.label.lower()}."
            ),
            reasoning=(
                f"The local rules engine found no job-search context "
                f"(score {job_evidence:.1f}) and matched "
                f"{', '.join(matched[:4]) or 'general non-job signals'} for "
                f"{best_topic.label}."
                + (" The body was truncated, so confidence is held down." if truncated else "")
            ),
            scores={t.value: round(v, 2) for t, v in topic_scores.items() if v},
            matched=tuple(matched[:8]),
        )

    def _confidence(
        self, best: float, runner_up: float, truncated: bool, strongest: float = 0.0
    ) -> float:
        """Map evidence to a calibrated, deliberately humble probability.

        Total evidence, the winner's lead and any decisive phrase raise it;
        a competing second category suppresses it hard, keeping ambiguous
        mail out of category folders. The lead is damped by the square root
        of the evidence, so a lone weak signal loses most of its credit
        while an unambiguous message keeps nearly all of it.
        """
        if best <= 0:
            return 0.0
        strength = min(1.0, best / SATURATION)
        separation = max(0.0, (best - runner_up) / best) * math.sqrt(strength)
        decisive = 0.06 if strongest >= DECISIVE_WEIGHT else 0.0
        confidence = 0.50 + 0.30 * strength + 0.14 * separation + decisive
        if truncated:
            confidence = min(confidence, 0.88)
        return round(min(MAX_CONFIDENCE, confidence), 3)


_CATEGORY_BLURB: Dict[Category, str] = {
    Category.INTERVIEW: "Someone wants to speak with you about a role.",
    Category.NEXT_STEPS: "You have been asked to complete a step in an application.",
    Category.OFFER: "This looks like a job offer or its paperwork.",
    Category.APPLICATION_RECEIVED: "An application of yours was acknowledged. No action needed.",
    Category.NETWORKING: "A work conversation or referral, not a formal hiring step.",
    Category.NOT_INTERESTED: "An application of yours was declined. No action needed.",
    Category.UNSOLICITED: "Unrequested recruiter outreach. No action needed.",
    Category.UNCLASSIFIED_OTHER: "Job related, but the category is unclear.",
}


def _summarise(subject: str, sender: str, blurb: str) -> str:
    who = (sender or "").split("<")[0].strip() or "The sender"
    what = (subject or "").strip()
    first = f"{who} sent “{what}”." if what else f"{who} sent this message."
    return f"{first} {blurb}"


def _explain(
    category: Category, matched: Sequence[str], scores: Dict[Category, float], truncated: bool
) -> str:
    evidence = ", ".join(matched[:5]) or "weak signals"
    ordered = sorted(scores.items(), key=lambda pair: pair[1], reverse=True)
    runner_up = next((c for c, v in ordered if c is not category and v > 0), None)
    parts = [
        f"Classified locally, without a model. Matched {evidence} for {category.label}."
    ]
    if runner_up is not None:
        parts.append(
            f"The nearest alternative was {runner_up.label} "
            f"(score {scores[runner_up]:.1f} against {scores[category]:.1f})."
        )
    if truncated:
        parts.append("The body was truncated, so confidence is capped.")
    parts.append(
        "This engine is a rule set rather than a reader: treat a borderline "
        "verdict as a prompt to look, not as a decision."
    )
    return " ".join(parts)
