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
    r"\b(?:candidates|applicants|those|anyone|everyone) who\b|"
    # "We only write back to people we'd like to meet": the meeting is for
    # them, not the reader.
    r"\b(?:people|candidates|applicants|the ones|those)(?: (?:that|whom))? "
    r"(?:we|i)(?:'d| would| will|'ll)?\b|"
    # "Shortlisted candidates will be contacted to arrange a call": what
    # happens to some of those who applied, the reader among them or not.
    # Not "as one of our shortlisted candidates, we'd like to meet you".
    r"\b(?:shortlisted|successful|selected|qualified|suitable|chosen|top|strongest) "
    r"(?:candidates|applicants)\b(?: \w+){0,3}? (?:will|would|may|might|are|get|receive)\b|"
    r"\b(?:candidates|applicants) (?:selected|shortlisted|chosen|invited)\b(?: \w+){0,3}? "
    r"(?:will|would|may|might|are|get|receive)\b")

#: A hedge in front of a step or a meeting: it may come, it has not. "May
#: be" rather than "may", which also grants: "you may now pick a slot".
_HEDGE = re.compile(r"\b(?:may be|might be|might|could be|possibly|potentially)\b")

#: A negation just before an offer, a step or a meeting turns it around:
#: "we cannot offer you the post", "we will not be arranging calls".
#: Just before: in "you have not yet finished your recorded interview" it is
#: the finishing that has not happened, not the interview.
_NEGATED = re.compile(
    r"(?:\bnot\b|n't\b|\bunable to\b|\bcannot\b|\bno longer\b|\bnever\b)"
    # "Can't wait to have you in" is eagerness, not a refusal.
    r"(?!\s+wait\b)(?:\s+[\w'-]+){0,2}\s*$")

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

#: Choosing, and somebody other than the reader chosen: the halves of a
#: decision told in the active voice.
_CHOSE = (r"(?:went|gone|going|go|chose|chosen|picked|hired|selected|"
          r"offer(?:ed|ing)? (?:it|the (?:role|position|job|post)))")
_ANOTHER = (r"(?:another (?:candidate|applicant|person|finalist)|a different (?:candidate|"
            r"applicant|person)|an internal (?:candidate|applicant|hire)|one of the other "
            r"(?:candidates|applicants|finalists))")

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
        r"[^.!?]{0,45}\b" + _YOU + r"\b")),
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
        + _NOT + r"[^.!?]{0,25}\b(?:been |be )?(?:selected|successful|chosen|"
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
    ("the search ended with a hire", re.compile(
        r"\b(?:completed|concluded|finished|closed|wrapped up)\b[^.!?]{0,15}"
        r"\b(?:our|the|this)\b[^.!?]{0,30}\b(?:search|selection process|recruitment|"
        r"recruiting process|hiring process)\b[^.!?]{0,40}\b(?:hire|hired|chosen|"
        r"selected|appointed|offered|another|someone|filled)\b|"
        r"\b(?:we|we've|we have|have)\b[^.!?]{0,10}\bmade (?:a|an|our|the) "
        r"(?:hire|hiring decision|appointment)\b|"
        r"\b(?:another|a different|an internal|one other) (?:candidate|applicant|person)"
        r"\b[^.!?]{0,25}\b(?:was|has been|were|have been) (?:chosen|selected|hired|"
        r"offered|appointed)\b")),
    ("an offer not made", re.compile(
        _NOT + r"(?: be)?(?: able to)? (?:extend\w*|mak\w+|offer\w*)\b[^.!?]{0,12}"
        r"\b(?:an? |the |you an? |you the )?(?:job |employment |formal )?offer\b")),
    ("an application unsuccessful", re.compile(
        r"\bregret\w*\b[^.!?]{0,60}\bunsuccessful\b|"
        r"\b(?:application|candidacy|submission)\b[^.!?]{0,40}\b(?:has been|was|is)"
        r"(?: deemed)? unsuccessful\b")),
    ("another direction taken", re.compile(
        r"\b(?:gone|go|going|went|chosen to go|decided to go)\b[^.!?]{0,10}\bin (?:a|another) "
        r"(?:different|new) direction\b")),
    ("no opening left", re.compile(
        r"\b(?:don'?t|do not|no longer) have (?:an?|the|that|any) (?:opening|position|"
        r"role|job|vacancy|spot)(?: for you)? (?:anymore|any more|available)\b|"
        r"\b(?:don'?t|do not|no longer) have (?:an?|the|that|any) (?:position|role|job|"
        r"vacancy) (?:open|available)\b")),
    # The prompt's own: confirmation that the application was withdrawn.
    # "Your score did not meet the threshold required to advance".
    ("a bar not met", re.compile(
        r"\b(?:did not|didn'?t|does not|doesn'?t|do not|don'?t) meet (?:the|our) (?:\w+ )?"
        r"(?:threshold|bar|minimum|requirements?|criteria|standard)\b[^.!?]{0,30}"
        r"\b(?:advance|progress|move forward|proceed|continue|next (?:stage|step|round))")),
    ("the application withdrawn", re.compile(
        r"\b(?:application|candidacy|submission)\b[^.!?]{0,60}\b(?:has been|have been|was|"
        r"is now) (?:successfully )?withdrawn\b|"
        r"\b(?:we'?ve|we have|has been) withdrawn your (?:application|candidacy)\b|"
        r"\byour withdrawal (?:request )?(?:has been|was|is) (?:processed|received|"
        r"confirmed|complete)\b")),
    # "We ended up going with someone local", "decided to offer the role to
    # another candidate", "they went with someone else": the decision told in
    # the active voice, by whoever makes it or passes it on. "I went with
    # someone else to the gig" is a night out.
    ("someone else chosen", re.compile(
        r"\b(?:we|we'?ve|we have|we'?d|the (?:team|panel|committee|client|hiring manager))"
        r"\b[^.!?]{0,25}\b" + _CHOSE + r"\b[^.!?]{0,12}\b(?:with|to) (?:someone|somebody|"
        + _ANOTHER + r")\b|"
        r"\bthey(?:'ve| have)?\b[^.!?]{0,25}\b" + _CHOSE + r"\b[^.!?]{0,12}\b(?:with|to) "
        r"(?:someone else|somebody else|" + _ANOTHER + r")\b|"
        r"\b(?:chose|chosen|picked|hired|selected|appointed)\b[^.!?]{0,10}\b" + _ANOTHER
        + r"\b")),
)

_ACKNOWLEDGEMENTS: Tuple[_Move, ...] = (
    ("the application arrived", re.compile(
        r"\b(?:we|we've|i|i've)\b[^.!?]{0,20}\b(?:received|receive|got)\b[^.!?]{0,30}"
        r"\b(?:your|the)\b[^.!?]{0,30}\b(?:application|resume|cv|submission|candidacy|"
        r"materials)\b|"
        r"\b(?:your|the)\b[^.!?]{0,40}\b(?:application|resume|cv|submission|candidacy)\b"
        r"[^.!?]{0,60}\b(?:has|have|was|were|is)\b[^.!?]{0,15}\b(?:been )?(?:successfully )?"
        r"(?:received|submitted|confirmed|recorded|completed)\b|"
        r"\bsuccessfully applied (?:for|to)\b|"
        r"\bgot your (?:resume|cv|application) for (?:the|a|an|your) (?:[\w-]+ ){0,3}"
        r"(?:spot|role|position|job|opening|post|vacancy)\b|"
        r"\b(?:the )?hiring (?:team|manager) has been notified\b|"
        r"\bthis (?:message|email) (?:verifies|confirms) that you have submitted\b|"
        r"\byou(?:'ve| have) (?:successfully )?(?:submitted|applied)\b|"
        r"\bapplication (?:is|has been) confirmed\b|"
        # The subject an applicant-tracking system writes.
        r"^(?:re: )?(?:your )?(?:job )?(?:application|resume) (?:received|submitted|"
        r"confirmation|confirmed)\b")),
    ("thanks for applying", re.compile(
        r"\bthank(?:s| you)\b[^.!?]{0,30}\b(?:for|on)\b[^.!?]{0,20}\b(?:applying|"
        r"your (?:recent )?application|submitting|your submission|your interest|"
        r"your resume|considering|taking the time to apply|beginning your "
        r"application|starting your application)\b")),
    # A careers account opened, which the labelled mail files with the
    # acknowledgements: the system has the candidate, and asks nothing.
    ("a candidate account opened", re.compile(
        r"\b(?:welcome to (?:the )?(?:\w+ ){0,3}careers?(?: portal| site)?\b|"
        r"(?:careers?|candidate) (?:account|profile) (?:has been|was|is) (?:created|set up|"
        r"activated)|thank you for creating (?:a|your) (?:candidate |careers? )?profile|"
        r"career site (?:login|log-in|sign-in) credentials)")),
    ("applications still being read", re.compile(
        r"\b(?:still|currently|is|are)\b[^.!?]{0,15}\b(?:reviewing|going through|looking "
        r"through|considering|evaluating|working through)\b[^.!?]{0,25}"
        r"\b(?:applications|candidates|applicants|resumes|cvs|submissions)\b")),
    ("the application landed", re.compile(
        r"\byour (?:application|resume|cv)\b[^.!?]{0,50}\b(?:just )?(?:landed|arrived|came "
        r"through|came in|is in|made it)\b")),
    ("no decision yet", re.compile(
        r"\b(?:haven'?t|have not|has not|hasn'?t) (?:yet )?made (?:a|any|our|the)\b"
        r"[^.!?]{0,10}\bdecisions?\b")),
    ("still in the running", re.compile(
        r"\b(?:still in the running|(?:remains?|is still) (?:active|under consideration|"
        r"in consideration|in the process))\b|"
        r"\b(?:yours|your (?:cv|resume|application)) is (?:in the pile|on (?:the|my|our) "
        r"(?:list|shortlist)|with the hiring (?:manager|team))\b")),
)

#: Acknowledgements that are one whole by themselves: news that nothing has
#: been decided is news that the application is being read.
_PROGRESS = frozenset({"applications still being read", "no decision yet",
                       "still in the running", "a candidate account opened",
                       "the application landed"})

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
    # Papers asked for: a degree, a licence, letters of recommendation.
    ("documents asked for", re.compile(
        r"\b(?:upload|send|submit|provide|attach|bring|email|forward)\b[^.!?]{0,30}"
        r"\b(?:a copy of |copies of |scans? of )?(?:your |two |three )?(?:degree|diploma|"
        r"certificat\w+|licen[cs]es?|transcripts?|letters? of (?:recommendation|reference)|"
        r"proof of (?:identity|eligibility|address)|work permit|photo id|identification)\b")),
    ("a sample asked for", re.compile(
        r"\b(?:send|share|provide|submit|forward|attach|have)\b[^.!?]{0,40}\b(?:a |an |your |"
        r"two |three |some )?(?:short |official )?(?:writing samples?|work samples?|"
        r"code samples?|transcripts?|portfolio)\b|"
        r"\b(?:writing samples?|work samples?|official transcript)\b[^.!?]{0,40}"
        r"\b(?:send|share|provide|submit|by|this week|request\w*|ask\w*)\b")),
    ("a test about to expire", re.compile(
        r"\b(?:test|assessment|challenge|exercise|questionnaire)\b[^.!?]{0,40}"
        r"\b(?:expires?|is due|closes|ends)\b[^.!?]{0,20}\b(?:today|tomorrow|tonight|"
        r"soon|in \d|on \w+day|at \d)")),
    # After an approach, the application itself: "I need you to formally
    # apply through our portal".
    ("a formal application asked for", re.compile(
        r"\b(?:need|ask|like|want|asking) you to (?:formally )?apply\b|"
        r"\bformally apply (?:through|via|on|using|in|for)\b|"
        r"\bapply (?:through|via) our (?:portal|website|careers? (?:site|page))\b")),
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
    ("a time being moved", re.compile(
        r"\b(?:move|moving|reschedule|rescheduling|push|pushing|shift|shifting)\b"
        r"(?: back)? (?:our|the|your|tomorrow'?s|today'?s) (?:\w+ )?(?:interview|call|phone "
        r"screen|chat|conversation|meeting)\b")),
    ("a time agreed", re.compile(
        r"\b(?:let'?s do|let'?s say|see(?:ing)? you(?: on)?|talk to you|speak to you|"
        r"(?:hav(?:e|ing)|meet(?:ing)?|host(?:ing)?|welcom(?:e|ing)) you(?: in| here)?(?: on)?|"
        r"i'?ll call you)\b[^.!?]{0,12}\b(?:monday|tuesday|wednesday|thursday|friday|"
        r"saturday|sunday|tomorrow|today|this afternoon|this morning)\b[^.!?]{0,15}"
        r"\bat \d|"
        r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow)"
        r"(?: \w+){0,2} at \d{1,2}(?:[:.]\d{2})?\s?(?:am|pm)?(?: \w+)? (?:works|"
        r"is perfect|is great|sounds good)\b")),
    # A day or a time, or it is advice: "prepare for your interview" is in
    # every assessment platform's footer.
    ("a meeting ahead", re.compile(
        r"\b(?:ahead of|before|prep(?:are|aring)? for|for) (?:monday|tuesday|wednesday|"
        r"thursday|friday|saturday|sunday|tomorrow|today)'?s? (?:\w+ ){0,2}(?:interview|"
        r"phone screen|panel|final round|onsite)\b|"
        r"\b(?:ahead of|before|for) (?:your|our|the) (?:\w+ ){0,2}(?:interview|phone screen|"
        r"panel|final round|onsite)\b[^.!?]{0,20}\b(?:on (?:monday|tuesday|wednesday|"
        r"thursday|friday|saturday|sunday)|tomorrow|today|this (?:afternoon|morning)|"
        r"at \d)")),
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
        r"\b(?:your|the) offer(?: letter| package| details| paperwork)?\b[^.!?]{0,50}"
        r"\b(?:is |are )?(?:attached|enclosed|below|ready|on its way)\b|"
        r"\b(?:attached|enclosed) (?:is|are|please find) (?:your|the) (?:offer|contract)\b|"
        r"\b(?:attached|enclosed|attaching|enclosing) (?:the|your|an?) (?:\w+ ){0,2}offer\b")),
    ("the offer to answer", re.compile(
        r"\b(?:accept|sign|countersign|return) (?:the|your|this) (?:offer|offer letter|"
        r"contract)\b|\boffer (?:expires|deadline)\b|"
        r"\b(?:decision|decide|answer)\b[^.!?]{0,20}\bon (?:our|the|your|this) offer\b|"
        r"\b(?:extend|extending|push|move|hold)\w*\b[^.!?]{0,15}\b(?:deadline|offer)\b"
        r"[^.!?]{0,12}\b(?:on|for|of) (?:your|the|our|this) offer\b|"
        r"\b(?:hold|keep) (?:the|your|our) offer open\b|"
        r"\b(?:your|the|our) offer deadline\b")),
    ("a detail of the offer", re.compile(
        r"\b(?:equity|stock options|rsus|bonus|salary|base|package|pto|benefits|"
        r"relocation|start date)\b[^.!?]{0,20}\bin (?:your|the|our) (?:job |written )?"
        r"offer\b")),
    ("a job offered in other words", re.compile(
        r"\b(?:like|love|want|delighted|thrilled|pleased|happy|excited)\b[^.!?]{0,25}"
        r"\b(?:you|to have you) (?:to )?join\b[^.!?]{0,40}\bas (?:a|an|our)\b[^.!?]{0,40}"
        r"\b(?:analyst|engineer|developer|technician|specialist|manager|consultant|"
        r"associate|coordinator|administrator|architect|designer|scientist|"
        r"representative|assistant|director|officer|lead|generalist|tech|technologist|"
        r"admin|support|operator|clerk|programmer|intern|apprentice|trainee|writer|"
        r"editor|accountant|nurse|teacher)\b")),
    # The prompt's offer includes what follows one: the paperwork of a job
    # accepted, addressed to whoever is starting it. "His first day is
    # Monday" welcomes a colleague.
    ("a start being prepared", re.compile(
        r"\b(?:your first day|your start date|your first week|welcome aboard|"
        r"your onboarding|onboarding (?:checklist|documents?|portal|paperwork|forms?|"
        r"tasks)|new hire (?:paperwork|forms?|orientation|portal))\b")),
    ("the offer accepted", re.compile(
        r"\b(?:you(?:'ve| have)? accepted|you(?:'ve| have)? signed|thank you for (?:accepting|"
        r"signing))\b[^.!?]{0,15}\b(?:our|the|your|this)? ?(?:offer|offer letter)\b|"
        r"\bnow that (?:your|the) offer (?:is|has been) (?:signed|accepted)\b|"
        r"\b(?:hr|they|recruiting|the team) (?:told me|tells me|said) (?:that )?you "
        r"accepted\b")),
)


#: Moves that say hiring by themselves, so need no other word for it: a
#: job title joined is a job, whatever else the message lacks.
SELF_EVIDENT = frozenset({"a job offered in other words"})
#: The business of an offer - its deadline, its terms, its letter - which a
#: person writing for an organisation means about a job far more often than
#: about a house. Read without a word of hiring, and looked at before it is
#: filed.
SOFT_EVIDENT = frozenset({"the offer to answer", "a detail of the offer", "the offer sent",
                          "the offer accepted", "an offer made"})


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
        if "the application arrived" in moves or moves & _PROGRESS:
            return True
        return ("a review promised" in moves and any(
            statement.move == "thanks for applying"
            and re.search(r"\bappl(?:y|ied|ying|ication)", statement.words)
            for statement in self.acknowledgements))

    @property
    def names_an_application(self) -> bool:
        """Whether the acknowledgement is of an application, by that name. A
        friend thanking you for sending your CV acknowledges it too."""
        return any(re.search(r"\bappl(?:y|ied|ying|ication)|\bfor (?:the|a|an|your) "
                             r"(?:[\w-]+ ){0,3}(?:spot|role|position|job|opening|post|vacancy)\b",
                             statement.words)
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
