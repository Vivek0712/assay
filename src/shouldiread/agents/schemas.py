"""Structured output contracts for each judge.

Fifteen scored dimensions, four judges. Each judge owns one family and returns
every dimension in that family in a single structured response, so granularity
costs no extra model calls.

Every score arrives with the observations behind it. A number with no quotable
evidence is exactly the unfalsifiable output this project exists to complain
about, so the schema makes returning one impossible.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class TriageResult(BaseModel):
    """Cheap first pass. Decides whether the deep judges are worth running."""

    worth_deep_review: bool = Field(
        description="True if there is anything real here to evaluate"
    )
    preliminary_score: int = Field(ge=0, le=100, description="Rough 0-100 estimate")
    content_type: str = Field(
        description="One of: tutorial, deep_dive, project_writeup, explainer, opinion, "
        "news, experience_report, reference, listicle, announcement, other"
    )
    one_line: str = Field(description="One sentence on what this article actually is")


class ContentResult(BaseModel):
    """Is there anything here? Five dimensions, 35 of the 100 weight."""

    substance: int = Field(
        ge=0, le=100, description="Density of real technical content versus padding"
    )
    explanation_depth: int = Field(
        ge=0,
        le=100,
        description="Does it explain the mechanism, or only describe the surface",
    )
    insight: int = Field(
        ge=0, le=100, description="What a reader gets that the documentation does not give"
    )
    accuracy: int = Field(
        ge=0, le=100, description="Is what it says correct, as far as you can tell"
    )
    scope_discipline: int = Field(
        ge=0,
        le=100,
        description="Does it deliver what the title and introduction promise, without "
        "drifting or overclaiming",
    )

    what_is_new: str = Field(
        description="The single most useful takeaway a reader could not get from the "
        "docs. Write 'nothing' if that is the honest answer."
    )
    specifics: list[str] = Field(
        default_factory=list, description="Concrete technical content observed"
    )
    padding: list[str] = Field(
        default_factory=list, description="Passages carrying no information"
    )
    errors: list[str] = Field(
        default_factory=list, description="Anything stated that appears incorrect"
    )
    rationale: str = Field(description="Three sentences maximum, covering the family")


class AwsResult(BaseModel):
    """Is it useful to an AWS builder? Five dimensions, 28 of the weight."""

    aws_service_depth: int = Field(
        ge=0, le=100, description="Operating services versus naming them"
    )
    architecture_quality: int = Field(
        ge=0,
        le=100,
        description="Is the design sound, and are the choices reasoned rather than asserted",
    )
    well_architected: int = Field(
        ge=0,
        le=100,
        description="Engagement with security, reliability, performance or operations - "
        "not a checklist, just whether the hard parts are acknowledged",
    )
    currency: int = Field(
        ge=0,
        le=100,
        description="Current with the service as it is today: no deprecated patterns, "
        "superseded APIs or advice overtaken by a launch",
    )
    cost_awareness: int = Field(
        ge=0, le=100, description="Does it engage with what this actually costs to run"
    )

    services_engaged: list[str] = Field(default_factory=list)
    operating_detail: list[str] = Field(
        default_factory=list,
        description="Specifics only someone who used the service would write",
    )
    named_only: list[str] = Field(
        default_factory=list, description="Services named without accompanying detail"
    )
    dated_or_wrong: list[str] = Field(
        default_factory=list,
        description="Advice that is out of date or superseded, if any",
    )
    rationale: str = Field(description="Three sentences maximum, covering the family")


class EvidenceResult(BaseModel):
    """Can it be trusted? Three dimensions, 22 of the weight."""

    evidence: int = Field(
        ge=0,
        le=100,
        description="Verifiable artefacts, weighted to the strength of the claims made. "
        "An opinion piece needs none; a claimed 10x speedup needs the measurement.",
    )
    sources: int = Field(
        ge=0, le=100, description="Citations for factual claims, weighted by content type"
    )
    code_quality: int = Field(
        ge=0,
        le=100,
        description="If code is present: usable, complete, correct. If code is not "
        "expected for this content type, score 50 as not-applicable rather than 0.",
    )

    supported_by: list[str] = Field(
        default_factory=list, description="What makes it trustworthy"
    )
    unsupported_claims: list[str] = Field(
        default_factory=list, description="Claims asserted with nothing behind them"
    )
    evidence_proportionate: bool = Field(
        description="True if evidence matches the strength of the claims"
    )
    rationale: str = Field(description="Three sentences maximum, covering the family")


class ReaderResult(BaseModel):
    """Can it be used? Two dimensions, 15 of the weight."""

    clarity: int = Field(ge=0, le=100, description="Can a reader follow it")
    actionability: int = Field(
        ge=0,
        le=100,
        description="Could a reader do something with this - reproduce it, apply the "
        "idea, or make a decision differently",
    )

    strengths: list[str] = Field(default_factory=list)
    obstacles: list[str] = Field(
        default_factory=list, description="Where a reader would get stuck"
    )
    next_step: str = Field(
        description="What a reader can concretely do after reading. 'nothing' if true."
    )
    rationale: str = Field(description="Two sentences maximum")


class FinalVerdict(BaseModel):
    """Composed verdict. The RQS itself is computed in code, not here."""

    headline: str = Field(
        description="One blunt sentence telling a busy reader whether to open this and "
        "why. No hedging, no praise padding. Under 25 words."
    )
    best_for: str = Field(
        description="Who, if anyone, should read this. Say 'nobody' when that is true."
    )
    disagreement: str = Field(
        default="",
        description="If the measured signals contradict the dimension scores, say so. "
        "Otherwise leave empty.",
    )
