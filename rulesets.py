"""Field-specific overlays for the offline rules engine.

The base signal tables in :mod:`rules_engine` cover hiring language that is the
same everywhere - "we have decided to move forward with other candidates" reads
the same to a nurse and to a bricklayer. What differs is the vocabulary around
it: a software candidate gets a *system design round*, a nurse gets
*credentialing*, an academic gets a *campus visit*, and a tradesperson gets a
*ticket* and a *shift differential*.

Each ruleset below adds signals on top of the base set. They are additive, so
picking the wrong one costs recall, never correctness - and "General" is always
a reasonable answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Tuple

from models import Category
from rules_engine import Signal


@dataclass(frozen=True)
class RuleSet:
    """A named overlay of extra signals."""

    name: str
    label: str
    blurb: str
    #: Extra signals per category, added to the base tables.
    extra: Dict[Category, Tuple[Signal, ...]] = field(default_factory=dict)
    #: Extra phrases that establish "this is about employment at all".
    context: Tuple[Signal, ...] = ()

    def signals_for(self, category: Category) -> Tuple[Signal, ...]:
        return self.extra.get(category, ())

    @property
    def signal_count(self) -> int:
        return sum(len(table) for table in self.extra.values()) + len(self.context)


def _s(phrase: str, weight: float = 2.4, label: str = "") -> Signal:
    return Signal(phrase, weight, label=label)


GENERAL = RuleSet(
    name="general",
    label="General",
    blurb="Hiring language common to every field. A safe default.",
)

SOFTWARE = RuleSet(
    name="software",
    label="Software & Data",
    blurb="Engineering, data and IT: assessments, system design, on-call, stacks.",
    extra={
        Category.NEXT_STEPS: (
            _s("system design exercise", 2.8), _s("pair programming exercise", 2.8),
            _s("live coding", 2.8), _s("technical screen", 2.6),
            _s("github repository", 2.0), _s("pull request", 1.8),
            _s("submit your solution", 2.6), _s("starter repo", 2.6),
            _s("leetcode", 2.4), _s("algorithms round", 2.6),
            _s("architecture exercise", 2.6), _s("data challenge", 2.6),
            _s("sql exercise", 2.6), _s("debugging exercise", 2.6),
        ),
        Category.INTERVIEW: (
            _s("system design interview", 3.0), _s("system design round", 3.0),
            _s("virtual onsite", 2.8), _s("interview loop", 2.8),
            _s("behavioural round", 2.4), _s("behavioral round", 2.4),
            _s("meet the engineering team", 2.4), _s("with the engineering manager", 2.4),
            _s("chat with our cto", 2.4), _s("bar raiser", 2.6),
        ),
        Category.OFFER: (
            _s("equity refresh", 2.4), _s("vesting schedule", 2.4),
            _s("levels", 1.2), _s("l4", 1.0), _s("senior swe", 1.4),
        ),
    },
    context=(
        _s("software engineer", 1.8), _s("backend", 1.2), _s("frontend", 1.2),
        _s("full stack", 1.4), _s("devops", 1.4), _s("site reliability", 1.6),
        _s("data engineer", 1.8), _s("data scientist", 1.8), _s("platform engineer", 1.8),
        _s("machine learning", 1.4), _s("kubernetes", 1.0), _s("python", 0.8),
        _s("javascript", 0.8), _s("tech stack", 1.2), _s("on call", 1.0),
    ),
)

HEALTHCARE = RuleSet(
    name="healthcare",
    label="Healthcare & Clinical",
    blurb="Nursing, allied health and medicine: licensure, credentialing, shifts.",
    extra={
        Category.NEXT_STEPS: (
            _s("credentialing packet", 3.0), _s("credentialing process", 3.0),
            _s("licensure verification", 3.0), _s("license verification", 2.8),
            _s("immunization records", 2.8), _s("tb test", 2.6),
            _s("drug screen", 2.8), _s("fit for duty", 2.6),
            _s("bls certification", 2.6), _s("acls certification", 2.6),
            _s("malpractice history", 2.6), _s("privileging", 2.8),
            _s("occupational health", 2.4),
        ),
        Category.INTERVIEW: (
            _s("shadow shift", 2.8), _s("unit tour", 2.6),
            _s("meet the nurse manager", 2.6), _s("panel with the care team", 2.6),
            _s("clinical interview", 2.8),
        ),
        Category.OFFER: (
            _s("shift differential", 2.6), _s("sign on bonus", 2.4),
            _s("relocation assistance", 2.0), _s("per diem rate", 2.4),
            _s("fte", 1.4),
        ),
    },
    context=(
        _s("registered nurse", 2.0), _s("rn", 1.0), _s("np", 0.8),
        _s("physician", 1.8), _s("clinician", 1.8), _s("patient care", 1.6),
        _s("bedside", 1.4), _s("acute care", 1.6), _s("med surg", 1.6),
        _s("charge nurse", 1.8), _s("travel assignment", 1.8), _s("locum", 1.8),
    ),
)

FINANCE = RuleSet(
    name="finance",
    label="Finance & Accounting",
    blurb="Banking, accounting and analysis: licensing, modelling tests, series exams.",
    extra={
        Category.NEXT_STEPS: (
            _s("modelling test", 2.8), _s("modeling test", 2.8),
            _s("case study exercise", 2.8), _s("excel assessment", 2.8),
            _s("series 7", 2.6), _s("series 63", 2.6), _s("cfa", 1.8),
            _s("cpa license", 2.2), _s("finra registration", 2.8),
            _s("compliance questionnaire", 2.6), _s("credit check", 2.4),
        ),
        Category.INTERVIEW: (
            _s("superday", 3.0), _s("case interview", 2.8),
            _s("with the managing director", 2.6), _s("partner interview", 2.6),
            _s("technical finance round", 2.6),
        ),
        Category.OFFER: (
            _s("bonus target", 2.4), _s("carried interest", 2.4),
            _s("deferred compensation", 2.4), _s("total comp", 2.0),
        ),
    },
    context=(
        _s("financial analyst", 1.8), _s("investment banking", 1.8),
        _s("private equity", 1.8), _s("accountant", 1.8), _s("controller", 1.4),
        _s("audit", 1.2), _s("underwriting", 1.6), _s("portfolio", 1.0),
    ),
)

ACADEMIA = RuleSet(
    name="academia",
    label="Academia & Research",
    blurb="Faculty, postdoc and research posts: search committees, job talks, tenure.",
    extra={
        Category.NEXT_STEPS: (
            _s("teaching statement", 3.0), _s("research statement", 3.0),
            _s("diversity statement", 2.8), _s("letters of recommendation", 2.8),
            _s("writing sample", 2.8), _s("official transcripts", 2.6),
            _s("submit your cv", 2.4), _s("job talk slides", 2.8),
        ),
        Category.INTERVIEW: (
            _s("campus visit", 3.0), _s("job talk", 3.0),
            _s("search committee", 2.8), _s("conference interview", 2.8),
            _s("chalk talk", 3.0), _s("meet the department", 2.6),
        ),
        Category.OFFER: (
            _s("tenure track", 2.6), _s("start up package", 2.8),
            _s("startup package", 2.8), _s("course load", 2.2),
            _s("lab space", 2.4), _s("nine month appointment", 2.4),
        ),
        Category.APPLICATION_RECEIVED: (
            _s("your application materials", 2.4), _s("interfolio", 2.6),
        ),
    },
    context=(
        _s("postdoc", 2.0), _s("faculty position", 2.2), _s("assistant professor", 2.2),
        _s("principal investigator", 1.8), _s("department chair", 1.8),
        _s("dissertation", 1.6), _s("publication record", 1.6), _s("grant funding", 1.4),
    ),
)

LEGAL = RuleSet(
    name="legal",
    label="Legal",
    blurb="Law firms and in-house: bar admission, conflicts checks, callbacks.",
    extra={
        Category.NEXT_STEPS: (
            _s("conflicts check", 3.0), _s("bar admission", 2.8),
            _s("character and fitness", 2.8), _s("writing sample", 2.6),
            _s("law school transcript", 2.6), _s("class rank", 2.2),
        ),
        Category.INTERVIEW: (
            _s("callback interview", 3.0), _s("screening interview at oci", 2.8),
            _s("on campus interview", 2.8), _s("meet the partners", 2.6),
        ),
        Category.OFFER: (
            _s("clerkship", 2.2), _s("associate salary", 2.4), _s("of counsel", 2.0),
        ),
    },
    context=(
        _s("attorney", 2.0), _s("paralegal", 1.8), _s("counsel", 1.4),
        _s("litigation", 1.6), _s("general counsel", 1.8), _s("law firm", 1.6),
    ),
)

SALES = RuleSet(
    name="sales",
    label="Sales & Marketing",
    blurb="Quota-carrying and marketing roles: mock pitches, territory, OTE.",
    extra={
        Category.NEXT_STEPS: (
            _s("mock pitch", 3.0), _s("mock demo", 3.0),
            _s("sales presentation exercise", 2.8), _s("role play exercise", 2.8),
            _s("30 60 90 plan", 3.0), _s("portfolio of campaigns", 2.6),
            _s("writing exercise", 2.4),
        ),
        Category.INTERVIEW: (
            _s("meet the vp of sales", 2.6), _s("panel with the revenue team", 2.6),
            _s("discovery call", 2.0),
        ),
        Category.OFFER: (
            _s("on target earnings", 3.0), _s("ote", 2.4),
            _s("commission plan", 2.8), _s("quota", 2.2), _s("territory", 2.0),
            _s("accelerators", 2.2),
        ),
    },
    context=(
        _s("account executive", 2.0), _s("sdr", 1.6), _s("bdr", 1.6),
        _s("demand generation", 1.8), _s("growth marketing", 1.8),
        _s("pipeline", 1.0), _s("customer success", 1.6),
    ),
)

TRADES = RuleSet(
    name="trades",
    label="Trades & Operations",
    blurb="Skilled trades, logistics and field work: tickets, certifications, shifts.",
    extra={
        Category.NEXT_STEPS: (
            _s("safety orientation", 2.8), _s("osha certification", 2.8),
            _s("cdl", 2.6), _s("dot physical", 2.8), _s("drug screen", 2.6),
            _s("apprenticeship paperwork", 2.8), _s("journeyman ticket", 2.8),
            _s("forklift certification", 2.6), _s("union card", 2.4),
        ),
        Category.INTERVIEW: (
            _s("site walk", 2.8), _s("shop visit", 2.6),
            _s("working interview", 2.8), _s("meet the foreman", 2.6),
            _s("skills demonstration", 2.6),
        ),
        Category.OFFER: (
            _s("hourly rate", 1.8), _s("per diem", 2.2), _s("prevailing wage", 2.8),
            _s("shift schedule", 2.0), _s("boot allowance", 2.4),
        ),
    },
    context=(
        _s("electrician", 2.0), _s("welder", 2.0), _s("hvac", 1.8),
        _s("technician", 1.4), _s("machinist", 2.0), _s("warehouse", 1.4),
        _s("dispatcher", 1.6), _s("foreman", 1.8), _s("journeyman", 2.0),
    ),
)

GOVERNMENT = RuleSet(
    name="government",
    label="Government & Public Sector",
    blurb="Civil service and contracting: clearances, GS grades, referral lists.",
    extra={
        Category.NEXT_STEPS: (
            _s("security clearance", 3.0), _s("sf 86", 3.0),
            _s("background investigation", 2.8), _s("e verify", 2.4),
            _s("veterans preference", 2.4), _s("occupational questionnaire", 2.8),
            _s("supporting documents", 2.2),
        ),
        Category.INTERVIEW: (
            _s("structured interview", 2.6), _s("panel rating", 2.6),
            _s("referred to the hiring manager", 2.8),
        ),
        Category.APPLICATION_RECEIVED: (
            _s("usajobs", 2.8), _s("your application status is", 2.6),
            _s("referred to the selecting official", 2.6),
            _s("certificate of eligibles", 2.6),
        ),
        Category.NOT_INTERESTED: (
            _s("not referred", 2.8), _s("not among the best qualified", 3.0),
        ),
    },
    context=(
        _s("gs 12", 1.8), _s("gs 13", 1.8), _s("federal position", 2.0),
        _s("civil service", 2.0), _s("public sector", 1.6), _s("contracting officer", 1.8),
    ),
)

CREATIVE = RuleSet(
    name="creative",
    label="Design & Creative",
    blurb="Design, writing and media: portfolio reviews, briefs, spec work.",
    extra={
        Category.NEXT_STEPS: (
            _s("portfolio review", 3.0), _s("design exercise", 3.0),
            _s("design challenge", 3.0), _s("whiteboard challenge", 2.8),
            _s("spec work", 2.6), _s("share your portfolio", 2.8),
            _s("case study walkthrough", 2.8), _s("writing test", 2.8),
            _s("edit test", 2.8), _s("show reel", 2.6),
        ),
        Category.INTERVIEW: (
            _s("portfolio presentation", 3.0), _s("meet the design team", 2.6),
            _s("critique session", 2.6),
        ),
        Category.OFFER: (
            _s("day rate", 2.2), _s("kill fee", 2.4), _s("usage rights", 2.2),
        ),
    },
    context=(
        _s("product designer", 2.0), _s("ux", 1.6), _s("ui designer", 1.8),
        _s("art director", 2.0), _s("copywriter", 2.0), _s("figma", 1.4),
        _s("brand", 1.0), _s("editorial", 1.4),
    ),
)

EDUCATION = RuleSet(
    name="education",
    label="Teaching & Education",
    blurb="Schools and districts: certification, demo lessons, district paperwork.",
    extra={
        Category.NEXT_STEPS: (
            _s("teaching certificate", 3.0), _s("teaching license", 3.0),
            _s("fingerprint clearance", 2.8), _s("child abuse clearance", 2.8),
            _s("praxis", 2.6), _s("lesson plan submission", 2.8),
            _s("official transcripts", 2.4),
        ),
        Category.INTERVIEW: (
            _s("demo lesson", 3.0), _s("model lesson", 3.0),
            _s("classroom observation", 2.8), _s("meet the principal", 2.6),
            _s("interview with the district", 2.6),
        ),
        Category.OFFER: (
            _s("salary schedule", 2.6), _s("step and lane", 2.8),
            _s("contract year", 2.2), _s("stipend", 2.0),
        ),
    },
    context=(
        _s("classroom", 1.6), _s("school district", 2.0), _s("principal", 1.4),
        _s("curriculum", 1.4), _s("students", 1.0), _s("paraprofessional", 1.8),
    ),
)

ALL_RULESETS: Tuple[RuleSet, ...] = (
    GENERAL, SOFTWARE, HEALTHCARE, FINANCE, ACADEMIA, LEGAL,
    SALES, TRADES, GOVERNMENT, CREATIVE, EDUCATION,
)
BY_NAME: Dict[str, RuleSet] = {ruleset.name: ruleset for ruleset in ALL_RULESETS}
DEFAULT_RULESET = GENERAL.name


def get(name: str) -> RuleSet:
    return BY_NAME.get((name or "").strip().lower(), GENERAL)


def choices() -> List[Tuple[str, str, str]]:
    """``(name, label, blurb)`` for the menu."""
    return [(r.name, r.label, r.blurb) for r in ALL_RULESETS]
