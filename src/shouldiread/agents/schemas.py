"""Structured output contracts for each judge.

Every judge returns a bounded score plus the concrete observations behind it.
Free-text-only verdicts were the thing to avoid: a number without cited evidence
is exactly the unfalsifiable output this project exists to complain about.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class TriageResult(BaseModel):
    """Cheap first pass. Decides whether the deep judges are worth running."""

    worth_deep_review: bool = Field(
        description="True if this has enough substance that a detailed review is meaningful"
    )
    preliminary_score: int = Field(ge=0, le=100, description="Rough 0-100 quality estimate")
    content_type: str = Field(
        description="One of: tutorial, experience_report, news, opinion, reference, "
        "certification_journey, listicle, announcement, other"
    )
    one_line: str = Field(description="One sentence on what this article actually is")


class ProvenanceResult(BaseModel):
    """Did the author actually run this, or only describe running it?"""

    score: int = Field(ge=0, le=100, description="0-100 evidence that the author did the work")
    first_hand: bool = Field(description="True if there is real evidence of hands-on execution")
    evidence: list[str] = Field(
        default_factory=list,
        description="Concrete artefacts observed: error messages, timings, costs, "
        "console output, specific resource names. Quote them briefly.",
    )
    missing: list[str] = Field(
        default_factory=list,
        description="What a genuine hands-on write-up would have contained but this lacks",
    )
    rationale: str = Field(description="Two sentences maximum")


class CodeResult(BaseModel):
    """Is the code substantial and usable, or decorative?"""

    score: int = Field(ge=0, le=100)
    runnable: bool = Field(description="Could a reader copy this and get somewhere?")
    issues: list[str] = Field(default_factory=list, description="Concrete problems with the code")
    rationale: str = Field(description="Two sentences maximum")


class SourceResult(BaseModel):
    """Are claims anchored to anything checkable?"""

    score: int = Field(ge=0, le=100)
    unsupported_claims: list[str] = Field(
        default_factory=list,
        description="Specific factual or technical claims made with no source and no demonstration",
    )
    rationale: str = Field(description="Two sentences maximum")


class DepthResult(BaseModel):
    """Does this go beyond the documentation, and is it the author's own?"""

    depth_score: int = Field(ge=0, le=100, description="Insight beyond what AWS docs already say")
    originality_score: int = Field(ge=0, le=100, description="Distinct voice and own material")
    generic_markers: list[str] = Field(
        default_factory=list,
        description="Specific phrases or structural patterns indicating generated filler",
    )
    what_is_new: str = Field(
        description="The single most useful thing a reader learns here that they could not "
        "get from the service's documentation page. Say 'nothing' if that is the answer."
    )
    rationale: str = Field(description="Two sentences maximum")


class FinalVerdict(BaseModel):
    """Composed verdict. The RQS itself is computed in code, not here."""

    headline: str = Field(
        description="One blunt sentence telling a busy reader whether to open this and why. "
        "No hedging, no praise padding. Under 25 words."
    )
    best_for: str = Field(
        description="Who, if anyone, should read this. Say 'nobody' when that is true."
    )
    disagreement: str = Field(
        default="",
        description="If the measured signals contradict the dimension scores, say so here. "
        "Otherwise leave empty.",
    )
