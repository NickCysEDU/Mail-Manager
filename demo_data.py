"""A realistic sample inbox, used for offline development and demos.

One source of truth for three consumers:

* ``main.py --demo`` - the whole GUI, populated, with no credentials, no
  network and nothing that can move real mail.
* ``tools/devscan.py --fake`` - the whole pipeline end to end in a terminal.
* the test suite - the same messages the integration tests run against.

Every category and every non-job topic appears at least once, including the
awkward cases the routing rules exist for: a rejection that also opens another
role, a confirmation that hides a required action, and a digest the model is
deliberately unsure about.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from models import (
    Category,
    Classification,
    EmailMessage,
    FolderPlan,
    NonJobRouting,
    OtherCategory,
    TriageItem,
)

DEMO_MODEL = "claude-opus-5 (demo - no API call was made)"


@dataclass(frozen=True)
class DemoMessage:
    """One sample email plus the verdict a well-behaved model returns for it."""

    uid: str
    sender_name: str
    sender_email: str
    subject: str
    body: str
    hours_ago: float
    summary: str
    reasoning: str
    is_job_related: bool = True
    category: Category = Category.UNCLASSIFIED_OTHER
    other_category: OtherCategory = OtherCategory.NOT_APPLICABLE
    confidence: float = 0.97
    links: Tuple[str, ...] = ()
    list_unsubscribe: str = ""
    attachments: Tuple[str, ...] = ()

    @property
    def verdict(self) -> Dict[str, object]:
        """The JSON payload a scripted classifier should return."""
        return {
            "summary": self.summary,
            "is_job_related": self.is_job_related,
            "category": self.category.value,
            "other_category": self.other_category.value,
            "confidence_score": self.confidence,
            "reasoning": self.reasoning,
        }


DEMO_MESSAGES: Tuple[DemoMessage, ...] = (
    DemoMessage(
        uid="1001",
        sender_name="Dana Reyes",
        sender_email="dana@northwind.example",
        subject="Northwind Systems - technical interview, pick a time",
        body=(
            "Hi Alex,\n\n"
            "Thanks for your interest in the Senior Platform Engineer role at Northwind "
            "Systems. I really enjoyed reading through your background - the work on the "
            "ingestion pipeline stood out.\n\n"
            "I'd love to set up a 45-minute technical interview with two of our engineers "
            "this week. Please pick whichever slot works best for you:\n\n"
            "    Pick a time\n\n"
            "Looking forward to speaking,\nDana"
        ),
        hours_ago=2,
        category=Category.INTERVIEW,
        confidence=0.98,
        links=("https://calendly.com/northwind/tech-interview",),
        summary=(
            "Dana Reyes, a recruiter at Northwind Systems, is inviting you to book a "
            "45-minute technical interview. Choose a slot on the Calendly link this week."
        ),
        reasoning=(
            "The body says 'set up a 45-minute technical interview' and links to "
            "calendly.com/northwind, a scheduling domain. The sender is a named human at a "
            "company address with no List-Unsubscribe header. Runner-up was NEXT_STEPS, "
            "rejected because the requested action is booking an interview rather than "
            "completing an assessment."
        ),
    ),
    DemoMessage(
        uid="1002",
        sender_name="Vela Labs via Greenhouse",
        sender_email="no-reply@greenhouse.io",
        subject="Your CodeSignal assessment for Vela Labs",
        body=(
            "Hello Alex,\n\n"
            "As the next step in your application for Backend Engineer, please complete "
            "the CodeSignal assessment linked below. It takes about 70 minutes and must be "
            "finished within 5 days.\n\n"
            "    Start assessment\n\n"
            "Good luck!\nThe Vela Labs Talent Team"
        ),
        hours_ago=5,
        category=Category.NEXT_STEPS,
        confidence=0.97,
        links=("https://app.codesignal.com/t/abc123",),
        list_unsubscribe="<mailto:unsubscribe@greenhouse.io>",
        summary=(
            "Vela Labs, via Greenhouse, has sent a timed CodeSignal assessment for the "
            "Backend Engineer role. It must be completed within 5 days."
        ),
        reasoning=(
            "Links to app.codesignal.com, an assessment domain, and states 'must be "
            "finished within 5 days'. That is a required applicant action that is not an "
            "interview. Runner-up was INTERVIEW, rejected because no meeting with a person "
            "is offered or scheduled anywhere in the message."
        ),
    ),
    DemoMessage(
        uid="1003",
        sender_name="Acme Talent",
        sender_email="no-reply@acme.example",
        subject="We've received your application - Staff Engineer",
        body=(
            "Thank you for applying to Acme.\n\n"
            "We have received your application for Staff Engineer and our team will review "
            "it shortly. If your background is a match, a recruiter will be in touch.\n\n"
            "This is an automated message; please do not reply."
        ),
        hours_ago=9,
        category=Category.APPLICATION_RECEIVED,
        confidence=0.96,
        list_unsubscribe="<mailto:unsubscribe@acme.example>",
        summary=(
            "Acme's applicant tracking system confirms your Staff Engineer application was "
            "received. No action needed."
        ),
        reasoning=(
            "Templated no-reply text: 'Thank you for applying' and 'our team will review "
            "it shortly'. Nothing is requested of the applicant and no decision is stated. "
            "Runner-up was NEXT_STEPS, rejected because no task, form or document is asked "
            "for anywhere in the body."
        ),
    ),
    DemoMessage(
        uid="1004",
        sender_name="Northwind Careers",
        sender_email="careers@northwind.example",
        subject="Update on your application",
        body=(
            "Dear Alex,\n\n"
            "Thank you for taking the time to apply for the Platform Engineer position.\n\n"
            "After careful consideration we have decided to move forward with other "
            "candidates whose experience more closely matches our current needs. We wish "
            "you the very best in your search.\n\n"
            "Kind regards,\nThe Northwind Talent Team"
        ),
        hours_ago=14,
        category=Category.NOT_INTERESTED,
        confidence=0.99,
        summary=(
            "Northwind is declining to move forward with your Platform Engineer "
            "application. No action needed."
        ),
        reasoning=(
            "The decisive phrase is 'we have decided to move forward with other "
            "candidates', an unambiguous rejection with no forward action. Runner-up was "
            "APPLICATION_RECEIVED, rejected because a decision is stated rather than a "
            "receipt acknowledged."
        ),
    ),
    DemoMessage(
        uid="1005",
        sender_name="Tal Berger",
        sender_email="tal@apexstaffing.example",
        subject="Exciting opportunity - 6 month contract, immediate start",
        body=(
            "Hi,\n\n"
            "I came across your profile and wanted to reach out about an exciting "
            "opportunity with one of our clients. It's a 6 month contract with immediate "
            "start, competitive rates, hybrid in the Bay Area.\n\n"
            "Let me know if you'd like the full spec.\n\n"
            "Best,\nTal Berger, Apex Staffing"
        ),
        hours_ago=18,
        category=Category.UNSOLICITED,
        confidence=0.96,
        list_unsubscribe="<mailto:opt-out@apexstaffing.example>",
        summary=(
            "An unsolicited staffing-agency pitch for a six-month contract you did not "
            "apply for. No action needed."
        ),
        reasoning=(
            "Cold outreach with no prior thread, the generic opener 'I came across your "
            "profile', a staffing agency rather than the employer, and a bulk "
            "List-Unsubscribe header. Precedence puts UNSOLICITED first when the user "
            "never applied. Runner-up was NOT_INTERESTED, rejected because nothing was "
            "declined - there was no application to decline."
        ),
    ),
    DemoMessage(
        uid="1013",
        sender_name="Marcus Webb",
        sender_email="marcus.webb@vela.example",
        subject="Offer - Backend Engineer at Vela Labs",
        body=(
            "Hi Alex,\n\n"
            "We were all impressed and I am delighted to offer you the Backend Engineer "
            "role at Vela Labs.\n\n"
            "  Base salary: $195,000\n"
            "  Equity: 0.08% over four years\n"
            "  Signing bonus: $15,000\n"
            "  Proposed start date: 6 October\n\n"
            "The formal letter is attached. We would love an answer by Friday 12 "
            "September - happy to talk anything through before then.\n\n"
            "Congratulations,\nMarcus"
        ),
        hours_ago=7,
        category=Category.OFFER,
        confidence=0.99,
        attachments=("Vela-Labs-Offer-Letter.pdf",),
        summary=(
            "Vela Labs is offering you the Backend Engineer role at $195,000 with 0.08% "
            "equity and a 6 October start. They want an answer by Friday 12 September."
        ),
        reasoning=(
            "The phrase 'delighted to offer you the Backend Engineer role', a full "
            "compensation breakdown, an attached offer letter and a response deadline. "
            "Precedence puts OFFER above NEXT_STEPS even though a reply is required. "
            "Runner-up was NEXT_STEPS, rejected because the action is accepting an offer "
            "rather than completing a step in a process."
        ),
    ),
    DemoMessage(
        uid="1014",
        sender_name="Sam Okafor",
        sender_email="sam@okafor.example",
        subject="Happy to refer you at Meridian",
        body=(
            "Alex - good running into you last week.\n\n"
            "I mentioned you to our platform lead and she'd be glad to have your name in "
            "the system. No formal opening posted yet, but there will be one in a few "
            "weeks. Send me a CV whenever and I'll put the referral in.\n\n"
            "Also happy to grab twenty minutes if you want the inside view of the team "
            "before you decide.\n\nSam"
        ),
        hours_ago=12,
        category=Category.NETWORKING,
        confidence=0.96,
        summary=(
            "Sam is offering to refer you at Meridian once a role opens, and to talk you "
            "through the team. Send a CV when you are ready."
        ),
        reasoning=(
            "A referral offer from a personal contact with no posted role, no application "
            "and no scheduled step - 'no formal opening posted yet'. Runner-up was "
            "INTERVIEW, rejected because the offered twenty minutes is an informal chat "
            "about a team rather than an interview in a hiring process; NEXT_STEPS was "
            "rejected because sending a CV here is optional and open-ended."
        ),
    ),
    DemoMessage(
        uid="1006",
        sender_name="Imogen Clarke",
        sender_email="imogen@brightpath.example",
        subject="Re: Staff Engineer - not this role, but let's talk about another",
        body=(
            "Hi Alex,\n\n"
            "The team has decided not to move ahead with your application for the Staff "
            "Engineer role - the panel wanted deeper Kubernetes ownership.\n\n"
            "That said, we've just opened a Principal Infrastructure role that I think is a "
            "much better fit. Are you free for a 30-minute call on Thursday or Friday? "
            "Grab whichever slot suits you:\n\n"
            "    Book a call\n\n"
            "Imogen"
        ),
        hours_ago=21,
        category=Category.INTERVIEW,
        confidence=0.96,
        links=("https://cal.com/imogen-brightpath/30min",),
        summary=(
            "Imogen at BrightPath is declining your Staff Engineer application but "
            "proposing a call about a new Principal Infrastructure role. Book a slot for "
            "Thursday or Friday."
        ),
        reasoning=(
            "The message contains both a rejection ('decided not to move ahead') and a "
            "concrete invitation with a cal.com booking link. Precedence puts INTERVIEW "
            "first, because the actionable item is the call. Runner-up was NOT_INTERESTED, "
            "rejected because the thread is explicitly continuing."
        ),
    ),
    DemoMessage(
        uid="1007",
        sender_name="Careers Digest",
        sender_email="alerts@careersdigest.example",
        subject="23 new jobs matching 'Staff Engineer'",
        body=(
            "Your saved search has 23 new results this week, including roles at "
            "Northwind, Vela Labs and eleven other companies. View them all in one place.\n\n"
            "You are receiving this because you saved a job alert."
        ),
        hours_ago=26,
        is_job_related=False,
        other_category=OtherCategory.PROMOTION,
        confidence=0.72,
        list_unsubscribe="<mailto:unsubscribe@careersdigest.example>",
        summary=(
            "A job-board digest listing 23 roles matching a saved search. No action "
            "needed, though it may be worth skimming."
        ),
        reasoning=(
            "A bulk job-alert digest, which the rules place outside the user's own job "
            "search. Confidence is deliberately held below the threshold because one of "
            "the listed roles could be an application the user already has open, which the "
            "digest format hides. Routed to Needs Review so a human decides."
        ),
    ),
    DemoMessage(
        uid="1008",
        sender_name="Priya Shah",
        sender_email="priya@friends.example",
        subject="Re: coffee next week?",
        body=(
            "Hey! Are you around on Thursday afternoon for a coffee somewhere central? "
            "I want to hear how the search is going. Let me know what suits."
        ),
        hours_ago=30,
        is_job_related=False,
        other_category=OtherCategory.PERSONAL,
        confidence=0.97,
        summary="A friend is proposing coffee on Thursday. Reply with a time that suits you.",
        reasoning=(
            "One-to-one prose from a named individual, no marketing footer and no "
            "unsubscribe header. It mentions 'the search' but is not part of any hiring "
            "process. Runner-up was EVENT, rejected because this is informal personal "
            "correspondence rather than an organised event."
        ),
    ),
    DemoMessage(
        uid="1009",
        sender_name="Chase",
        sender_email="alerts@chase.example",
        subject="Your September statement is ready",
        body=(
            "Your September credit card statement is now available to view online. "
            "Sign in to your account to see your balance and payment due date."
        ),
        hours_ago=34,
        is_job_related=False,
        other_category=OtherCategory.FINANCE,
        confidence=0.99,
        summary=(
            "Chase is notifying you that your September card statement is available. "
            "No action needed."
        ),
        reasoning=(
            "Bank sender, statement-availability language and a secure sign-in prompt. "
            "Runner-up was RECEIPT, rejected because no purchase or order is involved."
        ),
    ),
    DemoMessage(
        uid="1010",
        sender_name="The Pragmatic Engineer",
        sender_email="newsletter@pragmatic.example",
        subject="This week: platform teams that scale",
        body=(
            "In this week's issue we look at how platform teams stay effective past fifty "
            "engineers, why internal tooling rots, and what three staff engineers changed "
            "after their first year.\n\n"
            "You are receiving this because you subscribed."
        ),
        hours_ago=39,
        is_job_related=False,
        other_category=OtherCategory.NEWSLETTER,
        confidence=0.98,
        list_unsubscribe="<mailto:unsubscribe@pragmatic.example>",
        summary=(
            "A subscribed engineering newsletter issue about scaling platform teams. "
            "No action needed."
        ),
        reasoning=(
            "Editorial digest format with a List-Unsubscribe header and a 'you subscribed' "
            "footer. Runner-up was PROMOTION, rejected because the content is editorial "
            "rather than a sales offer."
        ),
    ),
    DemoMessage(
        uid="1011",
        sender_name="Apple",
        sender_email="no-reply@apple.example",
        subject="Sign-in code for your Apple Account",
        body=(
            "Your verification code is 481 902. It expires in ten minutes.\n\n"
            "If you did not request this code, someone may be trying to sign in to your "
            "account. Do not share it with anyone."
        ),
        hours_ago=44,
        is_job_related=False,
        other_category=OtherCategory.SECURITY,
        confidence=0.99,
        summary=(
            "Apple sent a one-time sign-in code for your account. Use it within ten "
            "minutes or ignore it if you did not request it."
        ),
        reasoning=(
            "A one-time verification code with an expiry and a 'do not share' warning - "
            "the textbook SECURITY shape. Runner-up was OTHER, rejected because the "
            "account-security purpose is explicit."
        ),
    ),
    DemoMessage(
        uid="1012",
        sender_name="Unknown Sender",
        sender_email="hr-team@mail.unknown-domain.example",
        subject="Regarding your recent submission",
        body=(
            "Hello,\n\nWe are writing regarding your recent submission. Please review the "
            "attached document and respond at your earliest convenience.\n\nRegards,\nHR"
        ),
        hours_ago=47,
        category=Category.UNCLASSIFIED_OTHER,
        confidence=0.54,
        attachments=("document.pdf",),
        summary=(
            "An unnamed sender refers vaguely to a 'recent submission' and an attached "
            "document. It is unclear what this relates to."
        ),
        reasoning=(
            "The message never names a company, a role, or the nature of the submission, "
            "and the decisive content is in an attachment that was not read. It could be "
            "an ATS follow-up, a request for documents, or phishing. Nothing here supports "
            "0.95 confidence in any category, so it goes to Needs Review - I would need "
            "the sender's organisation or the attachment's contents to be sure."
        ),
    ),
)


def _ordered() -> Tuple[DemoMessage, ...]:
    """Newest first, so the fixtures match what a real scan returns."""
    return tuple(sorted(DEMO_MESSAGES, key=lambda message: message.hours_ago))


def demo_emails(now: Optional[datetime] = None) -> List[EmailMessage]:
    """The sample messages as :class:`~models.EmailMessage` objects."""
    now = now or datetime.now(timezone.utc)
    return [
        EmailMessage(
            uid=message.uid,
            subject=message.subject,
            sender_name=message.sender_name,
            sender_email=message.sender_email,
            date=now - timedelta(hours=message.hours_ago),
            body_text=message.body,
            message_id=f"<{message.uid}@demo.local>",
            to="you@icloud.example",
            list_unsubscribe=message.list_unsubscribe,
            size=len(message.body.encode("utf-8")),
            links=message.links,
            attachments=message.attachments,
            source_folder="INBOX",
        )
        for message in _ordered()
    ]


def demo_classifications() -> List[Classification]:
    """The matching verdicts, run through the real validation layer."""
    return [
        Classification.from_payload(
            {**message.verdict, "_usage": {"input_tokens": 1180, "output_tokens": 205}},
            model=DEMO_MODEL,
        )
        for message in _ordered()
    ]


def demo_items(
    folders: Optional[FolderPlan] = None,
    threshold: float = 0.95,
    non_job_routing: NonJobRouting = NonJobRouting.LEAVE,
    auto_approve_non_job: bool = False,
    now: Optional[datetime] = None,
) -> List[TriageItem]:
    """Ready-to-display rows, routed by the real routing rules."""
    plan = folders or FolderPlan()
    return [
        TriageItem(
            email=message,
            classification=classification,
            folders=plan,
            threshold=threshold,
            non_job_routing=non_job_routing,
            auto_approve_non_job=auto_approve_non_job,
        )
        for message, classification in zip(demo_emails(now), demo_classifications())
    ]


def demo_mime() -> Dict[str, bytes]:
    """The sample messages as raw RFC 822 bytes, for a fake IMAP server."""
    import email.message

    messages: Dict[str, bytes] = {}
    for demo in DEMO_MESSAGES:
        message = email.message.EmailMessage()
        message["Subject"] = demo.subject
        message["From"] = f"{demo.sender_name} <{demo.sender_email}>"
        message["To"] = "you@icloud.example"
        message["Message-ID"] = f"<{demo.uid}@demo.local>"
        if demo.list_unsubscribe:
            message["List-Unsubscribe"] = demo.list_unsubscribe
        body = demo.body
        if demo.links:
            body += "\n\n" + "\n".join(demo.links)
        message.set_content(body)
        messages[demo.uid] = message.as_bytes()
    return messages


def verdict_for_prompt(prompt: str) -> Optional[Dict[str, object]]:
    """Match a rendered prompt back to its scripted verdict by subject line."""
    lowered = prompt.lower()
    for demo in DEMO_MESSAGES:
        if demo.subject.lower()[:40] in lowered:
            return demo.verdict
    return None
