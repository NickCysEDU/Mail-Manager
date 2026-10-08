"""What a message states, read a sentence at a time.

Hiring mail says what it is in one sentence: "we have decided to go ahead
with other candidates", "we have received your application", "please
complete the test below", "book a time that suits you". A phrase table finds
words; this finds the statement, with enough grammar that one pattern covers
the ways people put it ("we will not be taking you forward", "we won't be
moving forward with further rounds at this time", "we have now filled the
places on this team"), and enough context to see what undoes it:

- a condition: "if we decide not to go ahead, we will let you know";
- a hedge: "you may be asked to sit a test";
- somebody else's step: "applicants who reach the last round give references";
- a quoted reply, which is the last message rather than this one;
- a feedback survey after a decision, which asks nothing of the application.

Five kinds of statement are read: a decline, an offer, an acknowledgement,
a request for a step, and an invitation to talk. Each is a short list of moves, each
move a pattern over one normalised sentence. Nothing here decides a
category: the classifier weighs what was stated against who sent it and
what else the message holds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import html_utils

#: Sentences end at their punctuation or at a paragraph, never at a wrapped
#: line: plain-text mail is hard-wrapped at 72 columns, and "we've decided to
#: move forward with / other candidates" is one sentence.
_SENTENCE_END = re.compile(
    r"(?<=[.!?])\s+|\n\s*\n+|\n\s*[•·*-]\s|\s[•·]\s")

#: What makes the rest of a sentence a condition rather than a statement.
_CONDITION = re.compile(
    r"\b(?:if|should|unless|whether|in the event|in case|depending on)\b")

#: Somebody else's step: "applicants who reach the last round give
#: references" describes the process, and asks nothing of the reader. Not a
#: decision's business: "having read everyone who applied, we decided".
_SOMEBODY_ELSE = re.compile(
    r"\b(?:candidates|applicants|those|anyone|everyone) who\b")

#: A hedge in front of a step or a meeting: it may come, it has not. "May
#: be" rather than "may", which also grants: "you may now pick a slot".
_HEDGE = re.compile(r"\b(?:may be|might be|might|could be|possibly|potentially)\b")

#: A negation just before an offer, a step or a meeting turns it around:
#: "we cannot offer you the post", "we will not be arranging calls".
#: Just before: in "you have not yet finished your recorded interview" it is
#: the finishing that has not happened, not the interview.
_NEGATED = re.compile(
    r"(?:\bnot\b|n't\b|\bunable to\b|\bcannot\b|\bno longer\b|\bnever\b)"
    r"(?:\s+[\w'-]+){0,2}\s*$")

#: A survey about how the process felt asks nothing of the application, and
#: says so in the sentence that asks or in the few before it.
_FEEDBACK = re.compile(
    r"\b(?:feedback|your experience|the experience|candidate experience|"
    r"how (?:we|did we) do|help us improve|where we can improve|your input|"
    r"anonymous\w*)\b")
#: How many sentences before a request are read for that.
_FEEDBACK_REACH = 3

#: Who a decision is about. Hiring mail names the application, the
#: candidacy or the reader, and a decision about anything else ("we will not
#: be moving our offices") is not one about them.
_YOU = (r"(?:you|your (?:application|candidacy|candidature|profile|submission|"
        r"resume|cv|interest))")
#: What can be filled or closed, and the words that make it this one.
_ROLE = r"(?:position|role|opening|vacancy|requisition|req|job|posting|opportunity)"
_THIS = r"(?:the|this|that|our|your|these|those|all|available)"
#: A negation, including the contracted kind that has no word boundary in
#: front of it ("won't", "can't").
_NOT = r"(?:\bnot\b|n't\b|\bno longer\b|\bunable to\b|\bcannot\b)"

_Move = Tuple[str, "re.Pattern"]

_DECLINES: Tuple[_Move, ...] = (
    ("a decision not to go on", re.compile(
        r"\b(?:decided|decision|elected|chosen|opted|determined)\b[^.!?]{0,40}"
        r"\b(?:not to|to not)\b[^.!?]{0,30}\b(?:move|moving|proceed|progress|advance|"
        r"continue|pursue|offer|extend|consider|go|take)\b")),
    ("somebody else chosen", re.compile(
        r"\b(?:move|moving|moved|proceed|proceeding|proceeded|progress|progressing|"
        r"go|going|gone|went|continue|continuing|pursue|pursuing|advance|advancing|"
        r"selected|chosen|chose|select|hired?|offered|focus\w*|concentrat\w*)\b"
        r"(?: forward| ahead| on)?[^.!?]{0,25}\b(?:with|in favou?r of|to|for|on)?\b"
        r"[^.!?]{0,25}\b(?:other|another|different|stronger|alternative|additional|"
        r"more qualified)\b[^.!?]{0,30}\b(?:candidates?|applicants?|individuals?|"
        r"people|profiles?|finalists?|person)\b")),
    ("closer matches chosen", re.compile(
        r"\b(?:forward|ahead|proceed\w*) with (?:the |those )?(?:candidates?|"
        r"applicants?|individuals?) (?:whose|who|that)\b[^.!?]{0,80}"
        r"\b(?:closely|closer|better|more)\b")),
    ("not going on with you", re.compile(
        _NOT + r"[^.!?]{0,25}\b(?:be )?(?:able to )?(?:moving|move|offer\w*|progress\w*|"
        r"advanc\w+|proceed\w*|tak\w+|consider\w*|pursu\w+|select\w*|continu\w+)\b"
        r"[^.!?]{0,30}\b" + _YOU + r"\b")),
    ("not going on now", re.compile(
        _NOT + r" (?:be )?(?:able to )?(?:moving|move|proceeding|proceed|progressing|"
        r"progress)(?: forward| further| ahead)?\b(?:[^.!?]{0,40}\b(?:at this "
        r"(?:time|point|stage)|this time)\b|\s*(?:[,.;]|$))")),
    ("the role filled or closed", re.compile(
        r"\b" + _THIS + r"\b(?: [\w&/-]+){0,4} " + _ROLE + r"\b[^.!?]{0,40}"
        r"\b(?:has|have|had|is|was|were|are)(?: now| since| already| officially| just)?"
        r"(?: been)?(?: successfully)? (?:filled|closed|cancel+ed|withdrawn|put on hold|"
        r"on hold|no longer (?:open|available|active|being filled|accepting))\b"
        # "positions are filled on a rolling basis": how hiring works, not news.
        r"(?! (?:on a|continuously|as|until|quickly|in the order))")),
    ("the role filled by them", re.compile(
        r"\b(?:we|we've|we have|have|had|just)\b[^.!?]{0,15}\b(?:filled|closed|"
        r"cancel+ed)\b[^.!?]{0,50}\b(?:position|role|opening|vacancy|requisition|"
        r"seats?|spots?|job|search|process|hiring)\b")),
    ("the search closed", re.compile(
        r"\b(?:decided|decision) to close\b[^.!?]{0,30}\b(?:process|position|role|"
        r"search|requisition)\b")),
    ("not selected", re.compile(
        r"\b" + _YOU + r"\b[^.!?]{0,30}\b(?:has|have|was|were|is|are|will)\b[^.!?]{0,10}"
        + _NOT + r"[^.!?]{0,15}\b(?:been |be )?(?:selected|successful|chosen|"
        r"shortlisted|progressed|moved forward|advanced|considered|being considered|"
        r"under consideration|moving forward|move forward|proceed\w*|progress\w*)\b")),
    ("not chosen by them", re.compile(
        r"(?:\bdid not\b|\bdidn't\b|\bdo not\b|\bdon't\b|\bwill not\b|\bwon't\b) "
        r"(?:select|choose|shortlist|advance|progress|move) you\b")),
    # "Does not", never "may not": a screen that "may not fully match" and
    # promises another look has not decided anything.
    ("requirements not met", re.compile(
        r"\b(?:you|your (?:application|submission|profile|experience|background))\b"
        r"[^.!?]{0,30}(?:\bdo not\b|\bdon't\b|\bdid not\b|\bdidn't\b|\bdoes not\b|"
        r"\bdoesn't\b|\bno longer\b)[^.!?]{0,20}\b(?:meet|match|satisfy|"
        r"fulfil+|align)\b[^.!?]{0,60}\b(?:qualifications|requirements|criteria)\b")),
    ("regretfully not a fit", re.compile(
        r"\b(?:unfortunately|regret\w*|sadly)\b[^.!?]{0,60}(?:\bis not\b|\bisn't\b|"
        r"\bwas not\b|\bwasn't\b|\bnot\b)(?: quite)?(?: the| a)?(?: best| right| good|"
        r" strong)? (?:match|fit)\b")),
    ("a withdrawal confirmed", re.compile(
        r"\b(?:decision|request) to withdraw\b|\b(?:application|candidacy) "
        r"(?:has been|was|is) withdrawn\b|\byou (?:have )?withdrawn\b")),
    ("no longer considered", re.compile(
        r"\b" + _YOU + r"\b[^.!?]{0,30}\b(?:no longer|not) (?:be )?(?:being )?"
        r"(?:considered|under consideration)\b")),
)

_ACKNOWLEDGEMENTS: Tuple[_Move, ...] = (
    ("the application arrived", re.compile(
        r"\b(?:we|we've|i|i've)\b[^.!?]{0,20}\b(?:received|receive|got)\b[^.!?]{0,30}"
        r"\b(?:your|the)\b[^.!?]{0,30}\b(?:application|resume|cv|submission|candidacy|"
        r"materials)\b|"
        r"\b(?:your|the)\b[^.!?]{0,40}\b(?:application|resume|cv|submission|candidacy)\b"
        r"[^.!?]{0,60}\b(?:has|have|was|were|is)\b[^.!?]{0,15}\b(?:been )?(?:successfully )?"
        r"(?:received|submitted|confirmed|recorded|completed)\b|"
        r"\bthis (?:message|email) (?:verifies|confirms) that you have submitted\b|"
        r"\byou have (?:successfully )?(?:submitted|applied)\b|"
        r"\bapplication (?:is|has been) confirmed\b|"
        # The subject an applicant-tracking system writes.
        r"^(?:re: )?(?:your )?(?:job )?(?:application|resume) (?:received|submitted|"
        r"confirmation|confirmed)\b")),
    ("thanks for applying", re.compile(
        r"\bthank(?:s| you)\b[^.!?]{0,30}\b(?:for|on)\b[^.!?]{0,20}\b(?:applying|"
        r"your (?:recent )?application|submitting|your submission|your interest|"
        r"your resume|considering|taking the time to apply|beginning your "
        r"application|starting your application)\b")),
)

#: The promise that ends an acknowledgement is conditional by nature ("if
#: your experience matches, we will be in touch"), so it is read apart from
#: the condition rule the other moves keep.
_PROMISES: Tuple[_Move, ...] = (
    ("a review promised", re.compile(
        r"\b(?:will|'ll|shall|are|is|be|currently)\b[^.!?]{0,30}\b(?:review|reviewing|"
        r"reviewed|consider|evaluat\w+|assess\w*|look at|go through|be in touch|contact|"
        r"reach out|get back|follow up|hear)\b|\b(?:under|pending) review\b")),
)

#: What a step is, when one is asked for.
_STEP = (r"(?:assessments?|tests?|questionnaires?|surveys?|forms?|(?:job |employment )?"
         r"application|exercises?|challenges?|tasks?|assignments?|references?|"
         r"documents?|transcripts?|portfolio|background check|consent|evaluations?|"
         r"quiz|steps?)")

_REQUESTS: Tuple[_Move, ...] = (
    ("a step asked for", re.compile(
        r"\b(?:please|kindly|you(?:'ll| will)? need to|you are (?:required|requested|"
        r"invited|asked) to|(?:ask|asks|invite|invites|need|needs|require|requires|request|"
        r"requests|encourage|encourages)(?: that)? you(?: to)?|you must|be sure to|"
        r"(?:the|your) next step(?: in (?:the|our|your) (?:\w+ )?process)? (?:is|will be)"
        r"(?: to| for you to)?|complete the following|click (?:here|below|"
        r"the (?:link|button)(?: below)?) to|invited to|invit\w* you to|"
        r"(?:your )?availability to)\b[^.!?]{0,110}\b(?:complet\w*|tak(?:e|es|ing)|"
        r"submit\w*|fill(?:ing)? (?:out|in)|finish\w*|record\w*|upload\w*|provid\w*|"
        r"send\w*|sign(?:ing)?|answer\w*|respond\w* to|start\w*|begin\w*|access\w*)\b"
        r"[^.!?]{0,60}\b" + _STEP + r"\b")),
    ("a step waiting", re.compile(
        r"\b(?:assessment|test|questionnaire|challenge|evaluation|exercise)s? "
        r"(?:is|are)(?: now| still)? (?:ready|waiting|pending|available|due|expir\w+)\b|"
        r"\b(?:assessment|test) invit(?:e|ation)\b")),
    ("a step sent over", re.compile(
        r"\b(?:sent|sending|send|forwarded|emailed)(?: you| over)? (?:the|an|your|a) "
        r"(?:\w+ ){0,2}(?:assessments?|tests?|exercises?|challenges?|questionnaires?|"
        r"assignments?|take-?home|coding (?:test|challenge))\b")),
)

_INVITATIONS: Tuple[_Move, ...] = (
    # The verb with its object, so that "the new rota means more time off"
    # arranges nothing.
    ("a time being arranged", re.compile(
        r"\b(?:schedule|set up|book|arrange|line up|organi[sz]e|find)\s+(?:a|an|the|"
        r"your|some|our|another)\s+(?:[\w-]+\s+){0,3}?(?:time|call|interview|phone "
        r"screen|screen|chat|conversation|meeting|zoom|teams|video call|phone call|"
        r"discussion)\b")),
    # An invitation to talk, not to join: "we would love you to join our
    # talent network" asks for an email address.
    ("a wish to talk", re.compile(
        r"\b(?:would|'d) (?:like|love) to\b[^.!?]{0,20}\b(?:schedule|set up|speak|talk|"
        r"chat|connect|meet|discuss|learn more about (?:you|your background)|invite you "
        r"(?:to|for) (?:an? )?(?:interview|call|chat|conversation|meeting|meet|speak|talk|"
        r"come in))\b")),
    ("availability asked", re.compile(
        r"\b(?:let me know|please (?:provide|send|share)|what is|what are|could you "
        r"(?:share|send|let me know))\b[^.!?]{0,30}\b(?:your )?(?:availability|"
        r"available times|times that work|a time that works|when you(?:'re| are) "
        r"available)\b")),
    ("a time fixed", re.compile(
        r"\b(?:interview|phone screen|screening|call|meeting)\b[^.!?]{0,40}\b(?:is |was |"
        r"has been |have been )(?:scheduled|confirmed|booked|rescheduled|moved|"
        r"cancel+ed)\b|\b(?:have|has|i've|we've) (?:been )?(?:scheduled|booked|"
        r"rescheduled|pushed)\b[^.!?]{0,40}\b(?:interview|call|screen|time|meeting)\b|"
        r"\bget you rescheduled\b")),
    ("an invitation sent", re.compile(
        r"\b(?:sent|sending|send|resent|forwarded)(?: you| over)? (?:a |an |the )?"
        r"(?:calendar |meeting |teams |zoom )?(?:invite|invitation)\b")),
    ("a call promised", re.compile(
        r"\b(?:give you a call|call you|ring you)\b[^.!?]{0,30}\b(?:monday|tuesday|"
        r"wednesday|thursday|friday|saturday|sunday|tomorrow|today|this afternoon|"
        r"this morning|at \d)")),
    ("a recorded interview", re.compile(
        r"\b(?:on-?demand|one-?way|recorded|video)\b[^.!?]{0,20}\b(?:interview|"
        r"screening)\b|\binterview link\b")),
    ("a booking made", re.compile(
        r"\bevent name\b[^.!?]{0,60}\b(?:interview|screen\w*)\b|\bcalendar invitation\b")),
)


_OFFERS: Tuple[_Move, ...] = (
    # What is offered is the job: "happy to offer you early access to our
    # jobs newsletter" is a mailing list.
    ("an offer made", re.compile(
        r"\b(?:pleased|delighted|happy|excited|thrilled|glad) to (?:offer|extend)(?: you)?"
        r"(?: (?:the|a|an|this|our))?(?: [\w-]+){0,3}? (?:offer|position|role|job|"
        r"employment)\b|"
        r"\b(?:extend|extending|make|making|made) (?:you )?(?:a |an |the |this )?(?:formal |"
        r"verbal |written |conditional |contingent )?(?:job |employment )?offer\b|"
        r"\boffer you (?:the|a|this) (?:position|role|job)\b|"
        r"\bwould like to offer you (?:the|a|this) (?:position|role|job)\b")),
    ("the offer sent", re.compile(
        r"\b(?:your|the) offer(?: letter| package| details| paperwork)?\b[^.!?]{0,30}"
        r"\b(?:is |are )?(?:attached|enclosed|below|ready|on its way)\b|"
        r"\b(?:attached|enclosed) (?:is|are|please find) (?:your|the) (?:offer|contract)\b")),
    ("the offer to answer", re.compile(
        r"\b(?:accept|sign|countersign|return) (?:the|your|this) (?:offer|offer letter|"
        r"contract)\b|\boffer (?:expires|deadline)\b")),
)


@dataclass(frozen=True)
class Statement:
    """One move found: which, and the words it was found in."""

    move: str
    words: str


@dataclass(frozen=True)
class Reading:
    """Everything a message was found to state."""

    declines: Tuple[Statement, ...] = ()
    offers: Tuple[Statement, ...] = ()
    acknowledgements: Tuple[Statement, ...] = ()
    requests: Tuple[Statement, ...] = ()
    invitations: Tuple[Statement, ...] = ()

    @property
    def acknowledged(self) -> bool:
        """An application acknowledged in so many words: it arrived, or the
        message thanks you for applying and promises to read it. Thanks
        alone opens every rejection, and thanks for your interest any
        letter at all."""
        moves = {statement.move for statement in self.acknowledgements}
        if "the application arrived" in moves:
            return True
        return ("a review promised" in moves and any(
            statement.move == "thanks for applying"
            and re.search(r"\bappl(?:y|ied|ying|ication)", statement.words)
            for statement in self.acknowledgements))

    @property
    def names_an_application(self) -> bool:
        """Whether the acknowledgement is of an application, by that name. A
        friend thanking you for sending your CV acknowledges it too."""
        return any(re.search(r"\bappl(?:y|ied|ying|ication)", statement.words)
                   for statement in self.acknowledgements
                   if statement.move != "a review promised")

    @property
    def thanked(self) -> bool:
        """Thanks for applying, which an acknowledgement and a rejection
        both open with."""
        return "thanks for applying" in self.moves("acknowledgements")

    def moves(self, kind: str) -> List[str]:
        return [statement.move for statement in getattr(self, kind)]


def sentences(subject: str, body: str) -> List[str]:
    """The subject and the new part of the body, one normalised sentence
    each. The quoted history of a reply is somebody else's statement."""
    from rules_engine import normalize

    out = []
    if subject and subject.strip():
        out.append(normalize(subject))
    for piece in _SENTENCE_END.split(html_utils.strip_quoted_replies(body or "")):
        text = normalize(piece.replace("\n", " "))
        if len(text) >= 8:
            out.append(text)
    return out


def _find(moves: Sequence[_Move], said: Sequence[str], hedged: bool = False,
          skip: Optional["re.Pattern"] = None,
          conditional: bool = False) -> Tuple[Statement, ...]:
    found = []
    for move, pattern in moves:
        for index, sentence in enumerate(said):
            match = pattern.search(sentence)
            if match is None:
                continue
            before = sentence[:match.start()]
            if not conditional and _CONDITION.search(before):
                continue
            if hedged and (_HEDGE.search(before) or _NEGATED.search(before)
                           or _SOMEBODY_ELSE.search(before)):
                continue
            if skip is not None and any(
                    skip.search(earlier)
                    for earlier in said[max(0, index - _FEEDBACK_REACH):index + 1]):
                continue
            start = max(0, match.start() - 20)
            found.append(Statement(move, sentence[start:match.end() + 20].strip()))
            break
    return tuple(found)


def unconditional(subject: str, body: str) -> str:
    """The body without its conditions and hedges, as one normalised text:
    what it says will happen, not what might. "If another opening comes up we
    will be in touch to find a time" is how a rejection ends, and read as
    words it is an interview."""
    said = sentences("", body)
    return " ".join(sentence for sentence in said
                    if not _CONDITION.search(sentence) and not _HEDGE.search(sentence)
                    and not _SOMEBODY_ELSE.search(sentence))


def read(subject: str, body: str) -> Reading:
    """What the message states, by kind."""
    said = sentences(subject, body)
    return Reading(
        declines=_find(_DECLINES, said),
        offers=_find(_OFFERS, said, hedged=True),
        acknowledgements=(_find(_ACKNOWLEDGEMENTS, said)
                          + _find(_PROMISES, said, conditional=True)),
        requests=_find(_REQUESTS, said, hedged=True, skip=_FEEDBACK),
        invitations=_find(_INVITATIONS, said, hedged=True),
    )
