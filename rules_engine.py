"""A local, LLM-free classifier for job-search email.

This is a hand-built expert system, not a trained model: there is no corpus and
no weights file, and it makes no pretence of being one. What it encodes is the
same domain knowledge the system prompt describes - the phrases, sender shapes,
link domains and structural tells that separate a rejection from a receipt -
expressed as weighted signals that can be read, argued with, and unit tested.

It exists for three reasons:

* **Fallback.** When the model backend is unreachable, out of quota, or has no
  key, a scan still produces something better than a wall of "Needs Review".
* **Privacy and cost.** It runs on this Mac, instantly, for nothing, and no
  message text leaves the machine.
* **A second opinion.** Its verdict is computed for every message even when a
  model is in use, so a disagreement can be surfaced rather than hidden.

Robustness to messy text is a first-class concern. Real mail arrives with
mojibake ("weâ€™ve"), smart quotes, accents, zero-width padding, Cyrillic
homoglyphs, hyphenation across line breaks, and deliberate obfuscation
("i n t e r v i e w"). Every pattern is matched against two normalisations of
the text: a readable one, and a "tight" one with all punctuation and spacing
removed, which defeats most character-level evasion.

Its confidence is deliberately capped below the auto-file threshold for
anything but overwhelming evidence, so the routing layer keeps doing the safety
work.
"""

from __future__ import annotations

import html
import math
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import lexicon
from models import Category, OtherCategory

#: Confidence this engine will never exceed. It is a rule set, not a reader:
#: even a textbook rejection could be quoted inside a different message.
MAX_CONFIDENCE = 0.96
#: Below this total score nothing is claimed at all.
MIN_SCORE = 1.6
#: Score at which "more evidence" stops increasing confidence.
SATURATION = 5.0
#: A signal at or above this weight is decisive on its own.
DECISIVE_WEIGHT = 3.0
#: Evidence a category needs before it enters the precedence contest.
QUALIFY_SCORE = 2.5
#: UNSOLICITED sits first in precedence, so it has to clear a higher bar -
#: otherwise one enthusiastic phrase would outrank a real interview invitation.
QUALIFY_SCORE_UNSOLICITED = 3.5
#: Multiplier applied when a phrase matched only with words inserted into it.
GAPPED_PENALTY = 0.75

#: What the phrase "next steps" is worth, and so what to take back off when
#: every mention of it turns out to be a promise rather than a request.
PROMISED_STEPS_WEIGHT = 1.6

#: The most a reading can be trusted when it rests mainly on shape - the
#: mailbox it came from, a flight number, the way two people write - rather
#: than on words that say what the message is. Deliberately below the filing
#: threshold: these signals are right often enough to sort by and not often
#: enough to move somebody's mail unasked.
SOFT_EVIDENCE_CEILING = 0.90

#: How much of a message the rules look at. What a message is gets settled in
#: its opening; past this it is quoted threads, footers and legal boilerplate.
#: Five hundred signals against a hundred and sixty thousand characters costs
#: seconds and changes nothing.
MAX_SCANNED_CHARS = 20000

#: How much to discount transactional topics on mail that carries an
#: unsubscribe header. The lighter figure applies when the sender vouches for
#: the topic - a courier, an airline, a bank writing from its own domain. The
#: heavier one applies when nothing corroborates the wording, which is the
#: case a marketing email borrowing travel or shopping language falls into.
TRANSACTIONAL_IN_BULK = 0.72
TRANSACTIONAL_IN_BULK_UNVOUCHED = 0.45

#: Topics that describe a transaction rather than a broadcast.
TRANSACTIONAL_TOPICS: Tuple["OtherCategory", ...] = ()


# ==========================================================================
# Normalisation - the part that makes everything else work on real mail
# ==========================================================================
#: UTF-8 read as Latin-1, the most common corruption in forwarded mail.
_MOJIBAKE = {
    "â€™": "'", "â€˜": "'", "â€œ": '"', "â€\x9d": '"', "â€“": "-", "â€”": "-",
    "â€¦": "...", "â€¢": "-", "Â ": " ", "Ã©": "e", "Ã¨": "e", "Ã¡": "a",
    "Ã­": "i", "Ã³": "o", "Ãº": "u", "Ã±": "n", "Ã§": "c", "â€": '"',
}

#: Letters from other scripts that render identically in a Latin word. Spam
#: uses these to slip past naive keyword filters.
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
#: Any run of three or more characters that are not letters or digits: a rule
#: of underscores, a row of dashes, a line of equals signs.
#:
#: This is the reason phrase matching cannot be made to hang. The gapped
#: matcher allows separator characters between the words of a phrase, and a
#: long run of them can be divided between those gaps in exponentially many
#: ways - a real message beginning with seventy underscores took forty-three
#: seconds to classify. Collapsing the run removes the ambiguity at the source
#: and costs nothing: no phrase this app looks for contains one.
#: Six, not three. Three catches ordinary punctuation between words - an em
#: dash with a quote either side is four characters - and flattening that
#: loses meaning for no benefit. What has to go is decoration, and decoration
#: is never four characters long.
_GAP_RUN = re.compile(r"[^a-z0-9]{6,}")
_SPACED_OUT = re.compile(r"(?:(?<=\s)|^)(?:[a-z]\s){3,}[a-z](?=\s|$)")


def normalize(text: str) -> str:
    """Fold real-world mail into something patterns can match.

    Fixes mojibake, maps homoglyphs back to Latin, strips accents and
    zero-width padding, repairs words hyphenated across a line break, and
    un-spaces deliberately spread-out words.
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


def tighten(text: str) -> str:
    """Everything but letters and digits removed, for evasion-proof matching."""
    return re.sub(r"[^a-z0-9]+", "", normalize(text))


def _loose(phrase: str) -> str:
    """A regex matching ``phrase`` with any punctuation or spacing between words.

    The joiner is an atomic group. Without it, a phrase of several words tried
    against a long run of separator characters gives the engine an exponential
    number of ways to divide that run between the gaps, and it tries them all.
    Nothing is lost by refusing to reconsider: the gap either fits or it does
    not.
    """
    words = [re.escape(word) for word in phrase.split()]
    return r"\b" + r"[\W_]{0,4}".join(words)


def _gapped(phrase: str, max_inserted: int = 2) -> Optional[str]:
    """A regex allowing a couple of extra words inside the phrase.

    Real sentences interleave: "your **September** statement is ready",
    "we have **now** received your application". Requiring an exact adjacency
    misses those, and they are the same statement. Only phrases of three or
    more words get this treatment - two-word phrases with gaps match far too
    eagerly to be worth the recall.
    """
    words = [re.escape(word) for word in phrase.split()]
    if len(words) < 3:
        return None
    # A repetition inside a repetition, which is the shape that backtracks
    # catastrophically. What keeps it safe is normalize(), which leaves no run
    # of separator characters long enough to divide up in many ways; the guard
    # is upstream rather than here, because bounding it here costs accuracy.
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

    #: An anchor shorter than this appears in almost every message, so testing
    #: for it costs a scan and saves nothing.
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
        """The longest word of the phrase, as it appears in a tightened blob.

        Both the loose and the gapped patterns join whole words with separator
        classes, so every word of a matching phrase survives into the text
        with its own letters adjacent - and ``tighten`` only removes what is
        between them. So if the longest word is not in the tightened text, no
        form of the phrase can match, and a substring test settles it in a
        fraction of what running the regex would cost.

        This is a filter, never a decision: it can only say "definitely not".
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
        """Match against a sender, which arrives untightened.

        The prefilter is skipped rather than applied to the wrong blob: an
        address is a hundred characters at most, so nothing is saved by
        filtering it, and applying the test to text that was never tightened
        would reject matches the loose pattern would have found.
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


# ==========================================================================
# Signal tables
# ==========================================================================
# Weights: 3.0 decisive · 2.0 strong · 1.2 moderate · 0.6 supporting.
# Phrases are matched loosely, so one entry covers a family of spellings:
# "move forward" also matches "move  forward", "move-forward", "moveforward".

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
    # The same news in the active voice, which half of them use. Word order
    # is the only difference and a literal list does not see past it.
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
    # Everything below was taken from real rejection mail. The polite opener
    # "thank you for your interest" is shared with acknowledgements, so the
    # decisive phrase is always further in.
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
    Signal("pleased to offer you", 3.0),
    Signal("delighted to offer you", 3.0),
    Signal("happy to offer you", 3.0),
    Signal("we would like to offer you", 3.0),
    Signal("extend an offer", 3.0),
    Signal("extending an offer", 3.0),
    Signal("offer of employment", 3.0),
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

#: The interview phrases that any meeting could use. A dentist asks for your
#: availability; a school asks you to pick a time; a sales team books a slot.
#: Everything NOT in this set names a hiring process outright - "phone screen",
#: "technical interview" - and needs no corroboration to be about a job.
#:
#: The split exists because discounting all interview evidence when a message
#: lacks other job wording threw away the clearest signals there are: "please
#: let me know your availability for a phone screen" scored eight and was cut
#: to two and a half.
GENERIC_SCHEDULING: frozenset = frozenset({
    "schedule a call", "set up a call", "set up some time", "book a time",
    "pick a time", "choose a time", "find a time", "grab some time",
    "your availability", "let me know your availability",
    "when are you available", "times that work for you",
    "looking forward to speaking", "meet the team", "30 minutes", "45 minutes",
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
    Signal("1099", 1.6),
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
    # Google's and Microsoft's own booking pages, which are what somebody
    # without a scheduling product reaches for.
    "calendar.app.google", "calendar.google.com", "bookings.microsoft.com",
    "outlook.office.com/bookwithme", "outlook.office365.com/book",
    "koalendar.com", "tidycal.com", "zcal.co", "usemotion.com", "clara.com",
    "appointlet.com", "setmore.com", "vcita.com", "book.morgen.so",
    "zoom.us", "teams.microsoft.com", "meet.google.com", "whereby.com",
    "hirevue.com", "sparkhire.com", "spark.hire", "willo.video",
    "modernhire.com", "vidcruiter.com", "loom.com",
)
ASSESSMENT_LINK_DOMAINS: Tuple[str, ...] = (
    "hackerrank.com", "codesignal.com", "codility.com", "karat.com",
    "coderbyte.com", "devskiller.com", "testgorilla.com", "woven.teams",
    "triplebyte.com", "mettl.com", "imocha.io", "qualified.io", "coderpad.io",
    "byteboard.dev", "filtered.ai", "pymetrics.ai", "criteriacorp.com",
    "shl.com", "predictiveindex.com", "leetcode.com",
)
#: Subject lines follow a handful of shapes across every applicant-tracking
#: system, and the shape alone establishes that this is about a real
#: application the reader submitted.
SUBJECT_PATTERNS: Tuple[Tuple[re.Pattern, float, str], ...] = tuple(
    (re.compile(pattern), weight, label)
    for pattern, weight, label in (
        (r"^\s*(?:re:\s*)?your application (?:for|to|with)\b", 3.0,
         "a subject of the form 'Your application for ...'"),
        (r"^\s*application (?:for|to|update|status|received|submitted|confirmation)\b",
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

#: Signals that a message is about employment at all.
#: Phrases that mean nothing on their own and a great deal from a careers
#: mailbox.
#:
#: This is the thing a language model does that a phrase table does not: read
#: the same words differently depending on who said them. "Got it - your
#: details are with us, the team will look at them over the next fortnight"
#: contains no hiring vocabulary whatever. It is an application
#: acknowledgement, and the only thing that makes it one is the From line
#: saying Recruitment.
#:
#: So these are scored only when the sender is a hiring mailbox or the message
#: has already established a named hiring process. Off that leash they would
#: be a disaster - "the paperwork is attached" is a sentence from a solicitor,
#: an accountant and a letting agent - which is exactly why they live here
#: rather than in the tables proper, and why their weights are modest even
#: when they do fire.
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
)

#: Things that mean "definitely not this person's job search".
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


# ==========================================================================
# Non-job topic signals
# ==========================================================================
#: Everyday topics, for mail that is not part of a job search. These carry the
#: same weights as the job tables (3.0 decisive, 2.0 strong, 1.2 moderate) and
#: are scored the same way. Sender-field signals are the sharpest of the lot: a
#: courier's own domain settles the question in a way prose rarely does.
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
        Signal("escrow", 1.8), Signal("tax return", 2.6),
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
        Signal("stripe.com", 2.4, field="sender", label="a payment processor"),
        Signal("squareup.com", 2.2, field="sender", label="a payment processor"),
    ),
    OtherCategory.SHIPPING: (
        # Collecting a parcel, in the words the notice actually uses. These
        # were briefly filed as "shapes"; they are nothing of the sort, they
        # are phrases, and as evidence they are worth as much as any other.
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
        # Categories of junk that have outlived every change of wording around
        # them. Kept to what is unambiguous: none of these turns up in mail
        # somebody actually wanted.
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


#: Places the words application, interview, offer and candidate turn up
#: meaning something else entirely. A tenancy application, a radio interview
#: and an offer on a house are all ordinary mail, and the job tables have no
#: way of telling on vocabulary alone.
_OTHER_WORLD = re.compile(
    r"\b(?:tenanc\w+|landlord|lettings?|letting agent|rent(?:al)?|deposit "
    r"protection|estate agent|vendor|conveyanc\w+|mortgage adviser|viewing|"
    r"property|freehold|leasehold|"
    r"radio|podcast|broadcast|documentary|on air|our listeners|newsroom|"
    r"journalist|reporter|"
    r"university place|ucas|admissions|visa application|passport application|"
    r"planning application|insurance claim|grant application|"
    r"dentist|hygienist|surgery|clinic|consultant appointment)\b"
)


#: Families of unsolicited-commercial-mail language. Any one of these turns up
#: in ordinary mail; two or three together almost never do. Counting families
#: rather than phrases is what keeps this from being a list of the spam that
#: happened to be in one corpus - the wording moves, the shape does not.
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
#: Shouting, which is a shape rather than a vocabulary.
_SHOUTED = re.compile(r"\b[A-Z]{4,}\b")


def solicitation_score(subject: str, body: str, raw_subject: str = "") -> Tuple[float, List[str]]:
    """How strongly this reads as unsolicited commercial mail.

    Scored by how many different families of solicitation language appear, not
    by how many phrases match, so a message repeating one phrase does not out-
    score one doing three separate things.
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


#: A meeting being proposed, in the shapes real mail uses. The verb and the
#: noun are allowed up to forty characters between them and may not cross a
#: sentence, which is what lets one entry cover "schedule a call", "schedule a
#: 20-minute Google Meet call" and "set up a quick intro chat" alike. Fixed
#: phrases cannot: they break the moment somebody says how long it will take.
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
    """How strongly this message proposes a meeting. Says nothing about why.

    A dentist, a sales team and a hiring manager all book calls in the same
    words, so this is deliberately blind to the reason. What the meeting is
    *for* is a separate question, answered by professional_context_score, and
    keeping the two apart is what stops a reminder about a cleaning being
    filed as an interview.
    """
    blob = f"{subject} {body}"
    reasons = [describes for describes, pattern in _MEETING_REQUEST
               if pattern.search(blob)]
    score = {0: 0.0, 1: 1.6, 2: 2.6}.get(len(reasons), 3.2)
    return score, reasons


#: Language that places a conversation in somebody's working life rather than
#: their dentist's diary. None of it is decisive alone - "the team" is a phrase
#: every workplace uses - which is why it is counted in families and only ever
#: qualifies other evidence.
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
    ("a professional introduction", re.compile(
        r"\b(?:i (?:am|'m) .{0,40}(?:assistant|recruiter|partner|manager|"
        r"director|founder|lead|facilitator)|my name is .{0,30} and i"
        r"|i (?:run|lead|head up) (?:the|our)|on behalf of)\b")),
)


def professional_context_score(subject: str, body: str,
                               sender: str = "") -> Tuple[float, List[str]]:
    """Whether a conversation is a working one. Qualifies, never decides."""
    blob = f"{subject} {body}"
    reasons = [describes for describes, pattern in _PROFESSIONAL_CONTEXT
               if pattern.search(blob)]
    score = {0: 0.0, 1: 1.0, 2: 2.0}.get(len(reasons), 2.8)
    return score, reasons


#: The sections a job description is built out of. A posting almost always
#: carries several; ordinary mail that happens to use one of these headings
#: carries exactly one. Counting sections rather than phrases is what tells a
#: description apart from a digest that quotes one line of it.
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
    """How strongly the message *is* a job description, rather than about one.

    Somebody mailing a posting to themselves is doing their job search, and
    the sorter used to score that at zero because a description contains none
    of the words a hiring process uses - no "your application", no "we would
    like to", no "recruiter". It is all headings.

    A digest quoting one heading is not a posting, which is why this counts
    sections and discounts bulk mail: a description arrives from a person, or
    from you, and a blast about "hundreds of openings" arrives from a list.
    """
    blob = f"{subject} {body}"
    reasons = [describes for describes, pattern in _POSTING_SECTIONS
               if pattern.search(blob)]
    # One heading is not a description. Ordinary mail uses "requirements" and
    # "about the role" in passing; a posting carries several sections at once.
    score = {0: 0.0, 1: 0.0, 2: 2.6, 3: 3.4}.get(len(reasons), 4.0)
    if list_unsubscribe.strip() and score:
        # A posting you were sent by a mailing list is a job board writing to
        # everybody, not a description you kept.
        score *= 0.45
        reasons.append("(discounted: it arrived on a mailing list)")
    return score, reasons


#: What a job board's broadcast does that a single posting does not: it offers
#: many roles, and it asks you to go and look at them.
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
    """How strongly this is a job board broadcasting, not a job being offered.

    The vocabulary is identical to a real posting - that is the whole problem.
    What differs is the shape: a board offers many roles at once and sends you
    somewhere to look at them, and it arrives on a mailing list.
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


#: How many times to unwrap a redirect. Trackers nest - a mail platform wraps
#: a link the sender had already wrapped - but never deeply.
MAX_LINK_UNWRAPS = 3


def unwrap_links(links: Sequence[str]) -> str:
    """One lower-case blob of link text, with redirects opened out.

    Click trackers keep the real destination inside the URL, percent-encoded:
    ``streak-link.com/DBv8/https%3A%2F%2Fcalendar.app.google%2F…``. Matching a
    domain list against that finds the tracker and never the destination, so a
    scheduling link sent through any mail platform - which is most of them -
    was invisible. Decoding costs nothing and makes the evidence readable.
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


#: What an acknowledgement of an application actually does. Every vendor
#: writes it differently and no two share a phrase, but all of them do the
#: same three things: say the application arrived, promise to read it, and
#: promise to be in touch if it is a match. Counting those moves catches the
#: whole family; listing phrases catches whichever vendor was in the corpus.
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
    """How strongly this is "we got your application, we will be in touch".

    The commonest thing in a job-search inbox and the least interesting, which
    is exactly why it should be filed without being read. One move is not
    enough - "thank you for applying" opens a rejection too - so it takes two.
    """
    blob = f"{subject} {body}"
    reasons = [describes for describes, pattern in _ACKNOWLEDGEMENT
               if pattern.search(blob)]
    # Two moves is worth 2.6 rather than 3.0, which was tried and rejected:
    # 3.0 filed eight more messages on the labelled set and one wrong one on
    # the held-out set, which is the only set whose opinion counts here.
    score = {0: 0.0, 1: 0.0, 2: 2.6, 3: 3.2}.get(len(reasons), 3.6)
    return score, reasons


# ==========================================================================
# What a message *is*, when it contains no word that says so
# ==========================================================================
# The three layers below exist because of mail like this, taken from a
# held-out set the sorter scored 16.7% on:
#
#   "It's here" - Collection point 4, Stockport. Bring the QR code or the
#   order number. We'll hold it for seven days.        -> a parcel
#
#   "Seat 14C" - FR7712 STN to DUB, Tuesday. Bags close 40 minutes before.
#   Your reference is J4KP2W.                          -> a flight
#
#   "that thing on Thursday" - Can we push it to half four? School run has
#   moved.                                             -> a friend
#
# None contains "delivery", "flight" or any other keyword. A person reads the
# shape of the thing: a flight number, an airport pair, a booking reference; a
# mailbox called bookings@; two people talking. A phrase list cannot, however
# long it gets, because there is no phrase to list.

#: Who the sender is, from the part before the @. A company that sends several
#: kinds of mail uses a different mailbox for each, and the name says which:
#: offers@, billing@, bookings@, security@. It is nearly free to read and it
#: is right far more often than it is wrong.
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

#: A local part shaped like somebody's name rather than a department:
#: "r.mccarthy", "jane.doe", "sam_hale". Two name-ish pieces, no digits worth
#: speaking of, and not one of the role words above.
_PERSON_LOCAL = re.compile(
    r"^[a-z]{1,20}[._-][a-z]{2,20}\d{0,2}$|^[a-z]{2,20}\d{0,2}$", re.I)
_ROLE_WORDS = re.compile(
    r"no.?reply|do.?not.?reply|donotreply|mailer|bounce|postmaster|admin|"
    r"info|hello|hi|contact|support|help|service|team|care|customer|"
    r"notification|alerts?|news|mail|robot|auto|system|daemon", re.I)


def sender_sector(sender: str) -> Tuple[Dict["OtherCategory", float], List[str]]:
    """What the company sending this does for a living.

    A phrase table cannot know that ryanair.com is an airline or that
    argos.co.uk sells things. Forty-four thousand company domains can, and
    that is most of what is left between a rule set and a reader on mail that
    never says what it is.

    A sector describes the sender, never the message - a bank sends security
    codes and marketing alike - so it is weighed as a hint and capped with
    the rest of the soft evidence.
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
    """What the mailbox this came from is *for*.

    Reading the local part is the cheapest useful signal there is and nothing
    was using it. "offers@boots" is a promotion before a single word of the
    body has been read.
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


#: Mailboxes that exist to talk to candidates. Checked against the display
#: name as well as the address, because "Careers <no-reply@brightpath>" tells
#: you what it is in the half a phrase table would never read.
_HIRING_MAILBOX = re.compile(
    r"(?:^|[\W_])(?:careers?|recruit(?:ing|ment|er|ers)?|talent"
    r"(?:[\W_]?acquisition)?|hiring|jobs?|vacanc(?:y|ies)|"
    r"people[\W_]?(?:team|ops|operations)|human[\W_]?resources|"
    r"campus[\W_]?recruit\w*|graduate[\W_]?(?:recruit\w*|scheme))"
    r"(?:$|[\W_])", re.I)


def hiring_mailbox(sender: str) -> str:
    """The word that says this mailbox exists to talk about hiring, if any.

    "Got it - your details are with us" is a sentence from every part of
    life. From ``Recruitment <careers@stanfield>`` it is an application
    acknowledgement and nothing else, and no amount of reading the body was
    going to establish that: the evidence is in the From line.

    Both halves are read. A company that sends candidate mail from
    ``no-reply@`` still puts "Careers" or "Talent" in the display name,
    because a person has to know who it is from.
    """
    sender = sender or ""
    name = sender.split("<")[0]
    local = sender.split("@")[0].split("<")[-1]
    for part in (name, local):
        found = _HIRING_MAILBOX.search(part or "")
        if found:
            return re.sub(r"^[\W_]+|[\W_]+$", "",
                          found.group(0)).lower()
    return ""


def looks_like_a_person(sender: str) -> bool:
    """Whether the address belongs to a person rather than a department."""
    local = (sender or "").split("@")[0].split("<")[-1].strip().lower()
    if not local or _ROLE_WORDS.search(local):
        return False
    return bool(_PERSON_LOCAL.match(local))


#: Structured things a person recognises on sight. Each is a shape, not a
#: word, which is exactly what a phrase list cannot hold.
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
    """Topics implied by the shapes in a message rather than its words.

    Runs on the *raw* text, because normalising folds case, and case is half
    of what makes a flight number look like a flight number.
    """
    blob = f"{raw_subject or subject}\n{raw_body or body}"[:MAX_SCANNED_CHARS]
    found: Dict[OtherCategory, float] = {}
    why: List[str] = []
    for describes, pattern, topic, weight in _ENTITIES:
        if pattern.search(blob):
            found[topic] = found.get(topic, 0.0) + weight
            why.append(describes)
    # Two real airports either side of "to" is a flight. Two arbitrary
    # capitals are "PDF to DOC", which is why this is checked against four
    # and a half thousand codes rather than a regular expression.
    pair = lexicon.airport_pair(blob)
    if pair:
        found[OtherCategory.TRAVEL] = found.get(OtherCategory.TRAVEL, 0.0) + 2.4
        why.append(f"a flight between {pair[0]} and {pair[1]}")
    # Several shapes agreeing is worth more than the sum suggests, but one on
    # its own should never decide anything.
    for topic in list(found):
        found[topic] = min(3.4, found[topic])
    return found, why


#: How two people write to each other, as opposed to how a company writes to
#: a customer. None of it is decisive; together it is unmistakable.
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
    """How strongly this reads as one person writing to another.

    "that thing on Thursday - can we push it to half four? School run has
    moved." has no topic word in it at all. What marks it out is everything
    around the words: a person's address, no unsubscribe, no links, a short
    body, and two people arranging something.
    """
    if list_unsubscribe.strip():
        return 0.0, []                      # a list is not a person
    # Sounding like a friend is the oldest trick in unsolicited mail - "Re:
    # our conversation", "sorry for the delay, here is that link". Warmth is
    # evidence of a person only when nothing is being sold.
    selling, _why = solicitation_score(normalize(subject), normalize(body), subject)
    if selling >= 2.6:
        return 0.0, []
    fake, _fake_why = impersonation_score(sender, normalize(subject), normalize(body))
    if fake:
        return 0.0, []
    # A careers mailbox is not a person writing to you, however warmly it is
    # written. "Thank you for the time you put into this" from Careers@ is a
    # rejection in a friendly register, not a note from a friend - and the
    # warmth is exactly what made this fire on it.
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
    return min(3.6, 1.8 * len(hits)), f"about {sorted(hits)[0]} rather than a job search"


# ==========================================================================
# Structural features
# ==========================================================================
# Phrase tables only recognise mail that is written the way the table expects.
# Real transactional mail mostly is - it comes out of templates - which is why
# a phrase-only sorter scores well on a collected inbox and then falls over on
# anything a person actually typed.
#
# These look at the shape of a message instead of its wording: whether a human
# or a machine sent it, whether there is money in it and which direction it
# went, whether there is a code, a flight, a delivery window, a date. They are
# deliberately about form rather than vocabulary, so paraphrasing does not
# defeat them.

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


#: Brands whose name in a From line is worth impersonating. Only used to check
#: the name against the domain it actually came from.
_IMPERSONATED = (
    "apple", "icloud", "google", "gmail", "microsoft", "outlook", "office365",
    "amazon", "paypal", "netflix", "meta", "facebook", "instagram", "linkedin",
    "chase", "barclays", "hsbc", "natwest", "lloyds", "santander", "revolut",
    "monzo", "wise", "coinbase", "binance", "dhl", "fedex", "ups", "usps",
    "evri", "royalmail", "hmrc", "irs", "dvla",
)
#: Pressure plus a threat plus a link: the shape of a phishing message,
#: whatever brand it happens to be wearing this week.
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
    # Good enough here: two labels, or three where the middle is a known
    # second-level suffix. This never needs to be exactly right, only to
    # notice that apple-account-verify.example is not apple.com.
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "gov", "ac", "net"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def impersonation_score(sender: str, subject: str, body: str) -> Tuple[float, str]:
    """Whether the From line claims to be somebody the domain says it is not.

    A display name is free text; the domain is not. When the name says Apple
    and the mail came from apple-account-verify.example, that gap is the single
    most reliable thing about the message, and it does not depend on the
    wording at all.
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

    # A brand buried in a longer hyphenated domain is the other half of the
    # same trick: apple-account-verify, paypal-secure-login.
    if "-" in stem and any(brand in stem for brand in _IMPERSONATED):
        return 2.6, f"a brand name inside a longer domain ({root})"

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

    selling, selling_why = solicitation_score(subject, body, subject)
    if selling >= 2.6:
        # Three or more separate solicitation moves, or two with shouting, is
        # junk rather than a shop you have heard of.
        add(OtherCategory.SPAM, min(3.4, selling), selling_why[0])
        # A bank telling you your statement is ready and a stranger offering
        # you a mortgage use the same words. What separates them is that one
        # reports something that happened and the other is selling.
        for topic in (OtherCategory.FINANCE, OtherCategory.RECEIPT,
                      OtherCategory.SHIPPING, OtherCategory.TRAVEL):
            if topic in scores:
                scores[topic] *= 0.45
                notes.setdefault(topic, []).append("but it is soliciting, not reporting")
    elif selling >= 1.0:
        add(OtherCategory.PROMOTION, selling, selling_why[0])
    robot = _robot_sender(sender)
    short = len(body) < 700

    # -- a one-time code ------------------------------------------------
    codes = _BARE_CODE.findall(blob)
    if codes and short and not bulk:
        # Codes come with a deadline and an instruction, never with a pitch.
        expiring = re.search(r"\b(?:expires?|stops working|valid for|within)\b", blob)
        add(OtherCategory.SECURITY, 3.0 if expiring else 1.6,
            "a short code in a short message")

    # -- account safety, without the word security ----------------------
    if re.search(r"\b(?:password|sign(?:ed|ing)? in|signin|log(?:ged|ging)? in|"
                 r"account was|recovery|two[\W_]?factor|device we (?:don[\W_]?t )?recognise|"
                 r"recognize|end every other session|lock you out)\b", blob):
        if re.search(r"\b(?:wasn[\W_]?t you|was this you|didn[\W_]?t do|"
                     r"not you|change your password|act quickly|straight away)\b", blob):
            add(OtherCategory.SECURITY, 2.6, "asks you to check a sign-in")
        else:
            add(OtherCategory.SECURITY, 1.4, "about account access")

    # -- money, and which way it went -----------------------------------
    if _MONEY.search(blob):
        if _SPENT.search(blob):
            add(OtherCategory.RECEIPT, 2.6, "an amount already taken")
        if _OWED.search(blob):
            add(OtherCategory.FINANCE, 2.6, "an amount still owed")
        if not _SPENT.search(blob) and not _OWED.search(blob):
            add(OtherCategory.FINANCE, 0.8, "an amount of money")

    # -- parcels ---------------------------------------------------------
    if _PARCEL.search(blob):
        add(OtherCategory.SHIPPING, 2.4, "describes a parcel")
    if _DELIVERY_WINDOW.search(blob):
        add(OtherCategory.SHIPPING, 2.2, "gives a delivery window")

    # -- travel ----------------------------------------------------------
    travel_hits = 0
    if _FLIGHT.search(f"{subject} {body}"):
        travel_hits += 1
    if _BOOKING_REF.search(f"{subject} {body}"):
        travel_hits += 1
    if re.search(r"\b(?:gate|bag drop|leaves at|departs|nights? in|"
                 r"you arrive on|check ?in shuts|door code)\b", blob):
        travel_hits += 1
    if travel_hits >= 2:
        add(OtherCategory.TRAVEL, 2.8, "a journey with a reference and a time")
    elif travel_hits == 1:
        add(OtherCategory.TRAVEL, 1.0, "something that reads like a journey")

    # -- something happening at a time and a place -----------------------
    if _CLOCK.search(blob) and _WEEKDAY.search(blob) and not bulk:
        add(OtherCategory.EVENT, 1.2, "a day and a time")

    # -- a person, rather than a system ----------------------------------
    if not bulk and not robot and short and len(links) <= 1:
        voice = len(_PERSONAL_VOICE.findall(blob))
        if voice >= 2:
            add(OtherCategory.PERSONAL, 2.4, "written by a person, to a person")
        elif voice == 1:
            add(OtherCategory.PERSONAL, 1.2, "reads as written by hand")

    # -- bulk mail: selling, or telling? ---------------------------------
    if bulk:
        selling = len(_SELLING.findall(blob))
        editorial = len(_EDITORIAL.findall(blob))
        if selling > editorial:
            add(OtherCategory.PROMOTION, 1.4 + min(1.2, 0.4 * selling), "bulk mail with a pitch")
        elif editorial:
            add(OtherCategory.NEWSLETTER, 1.4 + min(1.2, 0.4 * editorial), "bulk mail with an editorial shape")
        else:
            add(OtherCategory.PROMOTION, 0.8, "bulk mail")

    return scores, notes

# ==========================================================================
# Classifier
# ==========================================================================
#: Sentences that tell the reader to do something. Real applicant-tracking mail
#: buries the request in the middle of an otherwise cheerful acknowledgement,
#: so the phrase tables alone put it in the wrong folder.
_ACTION_PATTERNS: Tuple[Tuple[re.Pattern, float, str], ...] = tuple(
    (re.compile(pattern), weight, label)
    for pattern, weight, label in (
        (r"\bthe next step (?:in|of) the (?:application|hiring|interview) process\b",
         3.0, "names an explicit next step"),
        (r"\bthe next steps? (?:is|are) to\b", 3.0, "names an explicit next step"),
        (r"\byou (?:must|need to|will need to|are required to)\b", 2.6, "tells you to act"),
        (r"\bwe (?:ask|require|request) that you\b", 2.6, "tells you to act"),
        (r"\bwe recommend that you complete\b", 2.6, "tells you to act"),
        (r"\bplease (?:complete|submit|provide|upload|fill|confirm|schedule|book|sign|review|respond|reply|verify|click)\b",
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

#: An instruction inside one of these is hypothetical ("if you need to reset
#: your password") or describes something already done ("thank you for taking
#: the time to submit your application"). Neither is a request.
_NOT_A_REQUEST = re.compile(
    r"(?:\bif\b|\bin case\b|\bshould you\b|\bunless\b|\bwhen you\b|"
    r"\bmay have been\b|\bin the event\b|\bwhenever\b|"
    r"\bthank(?:s| you) for\b|\btaking the time to\b|\byou have already\b|"
    r"\bwe have received\b)[^.!?]{0,70}$"
)


#: "Next steps" as something that will happen to you later, rather than
#: something being asked of you now. Every acknowledgement ends this way -
#: "if your experience aligns, we will reach out to discuss next steps" - and
#: reading it as a request turns the commonest mail in a job search into an
#: action item.
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

    True means the message says somebody else will do something later. False
    means at least one mention is addressed to the reader, or that the phrase
    does not appear at all.
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
        """The same JSON shape the model backends produce.

        Plus two keys they do not fill in: the phrases that fired and what
        each category scored. A model cannot produce these honestly - it
        would be describing its own reasoning after the fact - so it leaves
        them out and the panel that shows them stays empty.
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

#: Precedence, matching the system prompt exactly. Ties break toward the
#: earlier entry.
#: How to settle a tie between two topics with the same score.
#:
#: ``max`` on a dict returns whichever key happened to be inserted first,
#: which means the answer to "is this a receipt or a promotion" was decided by
#: the order the tables are written in - invisible, arbitrary, and liable to
#: change the moment somebody reorders a literal. This is the order instead,
#: and it runs from specific to generic: a topic that describes one kind of
#: message beats one that describes a category of them, and the two catch-alls
#: come last because "something else" should never win a tie against a real
#: answer.
_TOPIC_PRECEDENCE: Tuple[OtherCategory, ...] = (
    OtherCategory.SECURITY,     # a code or a sign-in alert is unmistakable
    OtherCategory.TRAVEL,       # a flight number is not a metaphor
    OtherCategory.SHIPPING,     # nor is a tracking number
    OtherCategory.RECEIPT,      # money that already moved
    OtherCategory.FINANCE,      # money that has not
    OtherCategory.EVENT,
    OtherCategory.SOCIAL,
    OtherCategory.WORK,
    OtherCategory.NEWSLETTER,
    OtherCategory.PROMOTION,
    OtherCategory.SPAM,
    OtherCategory.PERSONAL,     # a register, not a subject
    OtherCategory.OTHER,        # the absence of an answer
)


def _topic_rank(topic: "OtherCategory") -> int:
    """Lower is more specific. Anything unlisted sorts last."""
    try:
        return _TOPIC_PRECEDENCE.index(topic)
    except ValueError:
        return len(_TOPIC_PRECEDENCE)


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
    """Scores an email against the signal tables. Deterministic and offline.

    ``ruleset`` names a field-specific overlay from :mod:`rulesets`, which adds
    vocabulary on top of the shared hiring language. Overlays are additive, so
    choosing the wrong one costs recall, never correctness.
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

    # -- scoring ---------------------------------------------------------
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
                # domain settles the topic in a way prose never quite does.
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
    ) -> RuleVerdict:
        subject = (subject or "")[:2000]
        body = (body or "")[:MAX_SCANNED_CHARS]
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

        # ---- is this a working conversation? ---------------------------
        # Asked before the link evidence, because a booking link says a
        # meeting is being arranged and nothing whatever about what for. A
        # dentist, a sales team and a hiring manager all send the same link.
        professional, professional_why = professional_context_score(
            subject_n, body_n, sender_n)
        meeting, meeting_why = meeting_request_score(subject_n, body_n)
        posting, posting_why = job_posting_score(subject_n, body_n, list_unsubscribe)
        # The job-search vocabulary counts as working context too. A bare
        # calendar invite whose subject is "Interview - Roadrunner" says what
        # it is without any of the phrasings above.
        context_now, _context_why = self._score(
            self._context, subject_n, subject_t, body_n, body_t)
        named_process, _named_why = self._score(
            HIRING_SPECIFIC_SIGNALS, subject_n, subject_t, body_n, body_t)
        # Who it came from is context in its own right. A careers mailbox has
        # one job, and mail from it is about that job even when the body is
        # four words long - which is exactly the case a phrase table cannot
        # reach, because there are no phrases in it.
        from_hiring = hiring_mailbox(sender)
        if from_hiring:
            context_now += 1.6
        working = (professional > 0.0 or context_now >= 1.0
                   or named_process > 0.0 or bool(from_hiring))

        # ---- weak evidence the context has licensed --------------------
        # Only now, with the sender and the process established, are the
        # conditional phrases allowed to count. Read without that licence
        # they would file a solicitor's letter under Offer.
        licensed = bool(from_hiring) or named_process > 0.0
        if licensed:
            for category, table in CONDITIONAL_SIGNALS.items():
                extra, extra_matched, extra_peak = self._score_detail(
                    table, subject_n, subject_t, body_n, body_t)
                if not extra:
                    continue
                scores[category] += extra
                strongest[category] = max(strongest[category], extra_peak)
                matches[category].extend(extra_matched)

        # ---- link evidence, which outweighs prose ----------------------
        if any(domain in link_blob for domain in SCHEDULING_LINK_DOMAINS):
            if working:
                scores[Category.INTERVIEW] += 3.0
                strongest[Category.INTERVIEW] = max(strongest[Category.INTERVIEW], 3.0)
                matches[Category.INTERVIEW].append("a scheduling link")
            else:
                # Kept as a weak hint rather than dropped: it is still a
                # meeting, it is just nobody's job search.
                scores[Category.INTERVIEW] += 0.6
                matches[Category.INTERVIEW].append(
                    "a scheduling link, with nothing to say it is about work")
        if any(domain in link_blob for domain in ASSESSMENT_LINK_DOMAINS):
            scores[Category.NEXT_STEPS] += 3.0
            strongest[Category.NEXT_STEPS] = max(strongest[Category.NEXT_STEPS], 3.0)
            matches[Category.NEXT_STEPS].append("an assessment-platform link")
        if meeting and working:
            # Neither half is worth much alone. "Let's find 20 minutes" is a
            # sentence from every part of life, and "the team" is a phrase
            # every workplace uses; together they are somebody proposing to
            # talk to you about your working life.
            weight = min(3.0, meeting * min(1.0, max(professional, context_now) / 2.0))
            scores[Category.INTERVIEW] += weight
            strongest[Category.INTERVIEW] = max(strongest[Category.INTERVIEW], weight)
            because = (professional_why[0] if professional_why
                       else "job-search wording elsewhere in the message")
            matches[Category.INTERVIEW].append(meeting_why[0] + ", and " + because)
        elif meeting and not working:
            # Somebody is arranging a meeting and nothing in the message says
            # it has anything to do with work. "Pick a time", "book a slot"
            # and a calendar link are how a dentist, a school and a sales team
            # all write, and reading them as an interview is how a reminder
            # about a cleaning ends up in the job-search folder.
            if scores[Category.INTERVIEW]:
                scores[Category.INTERVIEW] *= 0.3
                strongest[Category.INTERVIEW] *= 0.3
                matches[Category.INTERVIEW].append(
                    "(discounted: a meeting, but nothing says it is about work)")

        # An acknowledgement is the commonest thing in a job-search inbox and
        # the least interesting: it is the absence of a decision. A rejection
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

        # ---- subject shape ---------------------------------------------
        # A subject line is short, deliberate, and written last: it is the most
        # reliable single feature in applicant-tracking mail.
        for pattern, weight, label in SUBJECT_PATTERNS:
            if pattern.search(subject_n):
                job_bonus += weight
                if label not in structure_notes:
                    structure_notes.append(label)

        # ---- sender and structure --------------------------------------
        agency = any(hint in sender_n.replace(" ", "") for hint in AGENCY_SENDER_HINTS)
        if agency:
            scores[Category.UNSOLICITED] += 1.2
            matches[Category.UNSOLICITED].append("an agency-style sender address")

        raw_scores = dict(scores)
        replying = bool(re.match(r"^\s*(re|fw|fwd)\s*:", subject or "", re.I))
        if replying:
            # An existing thread is strong evidence the user started it.
            scores[Category.UNSOLICITED] *= 0.35
            matches[Category.UNSOLICITED].append("(discounted: this is a reply in an existing thread)")

        job_score, job_matches = self._score(
            self._context, subject_n, subject_t, body_n, body_t
        )
        job_score += job_bonus
        job_matches.extend(structure_notes)
        if posting:
            # A description is job-search material even though it contains not
            # one word a hiring process uses. It is all headings.
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

        # ---- explicit requests to act ----------------------------------
        # Only once the message is established as job mail: "please confirm
        # your email address" is a request in any inbox, and on its own it says
        # nothing about a hiring process.
        if job_score >= 2.0 or scores[Category.APPLICATION_RECEIVED] >= 2.5:
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
                # with, and applicant-tracking mail puts it there.
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

        # "We will reach out to discuss next steps" is a promise about the
        # future, not a step for the reader. Every acknowledgement ends that
        # way, so reading it as a request turned the commonest mail in a job
        # search into an action item.
        if scores[Category.NEXT_STEPS] and steps_are_only_promised(body_n):
            scores[Category.NEXT_STEPS] = max(
                0.0, scores[Category.NEXT_STEPS] - PROMISED_STEPS_WEIGHT)
            matches[Category.NEXT_STEPS].append(
                "(discounted: next steps are promised, not asked for)")

        # ---- pick a category ------------------------------------------
        # Precedence is an order, not a tiebreak: an offer outranks the
        # paperwork attached to it even when the paperwork says more.
        def qualifies(category: Category) -> bool:
            bar = (
                QUALIFY_SCORE_UNSOLICITED
                if category is Category.UNSOLICITED else QUALIFY_SCORE
            )
            return scores[category] >= bar

        qualified = [category for category in _PRECEDENCE if qualifies(category)]
        if qualified:
            best_category = qualified[0]
        else:
            best_category = max(_PRECEDENCE, key=lambda c: (scores[c], -_PRECEDENCE.index(c)))
        best_score = scores[best_category]
        runner_up = max(
            (score for category, score in scores.items() if category is not best_category),
            default=0.0,
        )

        # ---- job related? ----------------------------------------------
        # Measured before the reply discount: a reply in an existing thread is
        # *more* clearly part of a job search, not less.
        job_evidence = job_score + max(max(raw_scores.values(), default=0.0), best_score)

        # "Application", "interview" and "offer" belong to plenty of other
        # parts of life. Where the message is plainly about one of those, that
        # counts against the job reading rather than for it.
        selling, selling_why = solicitation_score(subject_n, body_n, subject)
        if selling >= 2.6:
            # Unsolicited commercial mail borrows the vocabulary of everything
            # else, job mail included: "fill out the form below" reads as a
            # next step until you notice what it is asking you to fill in.
            job_evidence = max(0.0, job_evidence - selling)
            non_job_score += selling
            non_job_matches.append(
                "reads as unsolicited commercial mail (" + ", ".join(selling_why[:2]) + ")")

        blast, blast_why = job_board_blast(subject_n, body_n, list_unsubscribe)
        if blast:
            job_evidence = max(0.0, job_evidence - blast)
            non_job_score += blast
            non_job_matches.append(blast_why)

        elsewhere, elsewhere_why = other_world_context(subject_n, body_n)
        if elsewhere:
            job_evidence = max(0.0, job_evidence - elsewhere)
            non_job_score += elsewhere
            non_job_matches.append(elsewhere_why)

        looks_job_related = job_evidence >= max(2.4, non_job_score * 0.9)

        if not looks_job_related:
            return self._non_job_verdict(
                subject_n, subject_t, body_n, body_t, sender_n,
                non_job_score, non_job_matches, job_evidence, truncated,
                list_unsubscribe, links,
                # The raw text as well: normalising folds case, and case is
                # half of what makes "FR7712 STN to DUB" a flight.
                raw_subject=subject, raw_body=body, raw_sender=sender,
            )

        if best_score < MIN_SCORE:
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

        confidence = self._confidence(
            best_score, runner_up, truncated, strongest[best_category]
        )
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

    # -- non-job ---------------------------------------------------------
    def _non_job_verdict(
        self, subject_n, subject_t, body_n, body_t, sender_n,
        non_job_score, non_job_matches, job_evidence, truncated, list_unsubscribe,
        links: Sequence[str] = (),
        raw_subject: str = "", raw_body: str = "", raw_sender: str = "",
    ) -> RuleVerdict:
        topic_scores: Dict[OtherCategory, float] = {}
        topic_matches: Dict[OtherCategory, List[str]] = {}
        topic_peak: Dict[OtherCategory, float] = {}
        for topic, table in TOPIC_SIGNALS.items():
            score, matched, peak = self._score_detail(
                table, subject_n, subject_t, body_n, body_t, sender_n
            )
            topic_scores[topic] = score
            topic_matches[topic] = matched
            topic_peak[topic] = peak

        # Shape, as opposed to wording. Phrase tables are blind to anything a
        # person typed themselves; these are not.
        shape, shape_notes = structural_topic_scores(
            subject_n, body_n, sender_n, list_unsubscribe, links)
        for topic, weight in shape.items():
            topic_scores[topic] = topic_scores.get(topic, 0.0) + weight
            topic_matches.setdefault(topic, []).extend(shape_notes.get(topic, ()))
            topic_peak[topic] = max(topic_peak.get(topic, 0.0), weight)

        # Three more ways to recognise a message that contains no word saying
        # what it is: which mailbox it came from, the shapes in it, and
        # whether it reads like one person writing to another.
        #: How much of each topic's score came from shape rather than words.
        #: These layers are allowed to decide *which* topic wins and never how
        #: certain that is: a mailbox called offers@ and an amount of money
        #: are good reasons to rank promotions first, and no reason at all to
        #: move somebody's mail without asking.
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
        # A recruiter is a human too. The register tells you a person wrote
        # this, not what it is about, so it only speaks where nothing else
        # has anything to say. Without this gate it pulled interviews, offers
        # and rejections into "personal" purely because they were friendly.
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
            # A boarding pass, a receipt or a login code is not marketing, and
            # transactional mail almost never carries an unsubscribe header.
            # Where one is present, travel and shopping vocabulary is usually
            # metaphor - "check-in is now open" selling a training course. This
            # is a discount rather than a veto, so a genuine confirmation that
            # happens to carry the header still wins on its own evidence.
            for topic in TRANSACTIONAL_TOPICS:
                vouched = self._sender_backs(TOPIC_SIGNALS[topic], sender_n)
                topic_scores[topic] *= (
                    TRANSACTIONAL_IN_BULK if vouched
                    else TRANSACTIONAL_IN_BULK_UNVOUCHED
                )

        # Words decide when there are words; shape decides when there are
        # none. A one-time code from a bank is a security notice, and the
        # sender being a bank is not a reason to call it a bank statement -
        # but that is exactly what happened, because "halifax is a bank" plus
        # an amount of money outscored the code itself.
        hard = {topic: score - soft.get(topic, 0.0)
                for topic, score in topic_scores.items()}

        def strength(topic) -> tuple:
            """What settles it, in order, when two topics score the same.

            The score first, then the strongest single phrase behind it - one
            decisive sentence beats three vague ones - then how much of the
            score was words rather than shape, and only then the written
            precedence. Every step of that is a reason; dict order was not.
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
        best = topic_scores[best_topic]
        ranked = sorted(topic_scores.values(), reverse=True)
        runner_up = ranked[1] if len(ranked) > 1 else 0.0

        if best < MIN_SCORE:
            best_topic, best, runner_up = OtherCategory.OTHER, max(best, non_job_score), 0.0
            topic_matches[OtherCategory.OTHER] = non_job_matches

        confidence = self._confidence(
            best, runner_up, truncated, topic_peak.get(best_topic, 0.0)
        )
        if best_topic is OtherCategory.OTHER:
            confidence = min(confidence, 0.70)
        # A reading held up mostly by shape is a good guess, not a certainty.
        # Without this the new layers pushed wrong answers past the filing
        # threshold, which costs far more than an extra row to look at.
        gentle = soft.get(best_topic, 0.0)
        if best > 0 and gentle >= best * 0.5:
            confidence = min(confidence, SOFT_EVIDENCE_CEILING)

        matched = topic_matches.get(best_topic, [])
        return RuleVerdict(
            is_job_related=False,
            category=Category.UNCLASSIFIED_OTHER,
            other_category=best_topic,
            confidence=confidence,
            summary=_summarise(
                "", "", f"Not part of your job search - this looks like {best_topic.label.lower()}."
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

    # -- calibration -----------------------------------------------------
    def _confidence(
        self, best: float, runner_up: float, truncated: bool, strongest: float = 0.0
    ) -> float:
        """Map evidence to a calibrated, deliberately humble probability.

        Three things raise it: total weight of evidence, how far ahead the
        winner is, and whether any single decisive phrase fired. A competing
        second category suppresses it hard, which is the behaviour that keeps
        ambiguous mail out of category folders.

        Being far ahead of the field only counts for as much as the evidence
        behind it. One weak phrase that nothing happens to contradict is not a
        confident reading, it is a thin one, and undamped it scored the same
        separation as an overwhelming case. The damping is by the square root
        of the strength rather than the strength itself: a lone weak signal
        should lose most of that credit, but an unambiguous message that
        simply has nothing to argue with should keep nearly all of it.
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
