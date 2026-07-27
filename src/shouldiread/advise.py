"""Turn a score into advice.

The scorer answers "is this worth reading?". This answers the more useful
question an author has: **what would move it?**

The important property is that recommendations are *quantified*. Because the
caps and the weights are deterministic, the exact cost of a binding cap is
computable: a cap holding `source_integrity` at 30 when the judge wanted to give
it 85 is costing precisely (85 - 30) x 20 / 100 = 11.0 RQS. That is not an
estimate, it is arithmetic, and it lets recommendations be ranked by what they
are actually worth rather than by how important they sound.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from .config import READ_THRESHOLD, SKIM_THRESHOLD, WEIGHTS, verdict_for

# A dimension at or above this is doing its job; below it there is headroom.
GOOD_ENOUGH = 75.0

# Human-readable names for the machinery.
DIMENSION_LABELS = {
    "substance": "substance — how much is actually here",
    "explanation_depth": "explanation — mechanism or surface",
    "insight": "insight — beyond the docs",
    "accuracy": "accuracy",
    "scope_discipline": "scope — delivers what it promises",
    "aws_service_depth": "AWS depth — operating or naming",
    "architecture_quality": "architecture — sound and reasoned",
    "well_architected": "the hard parts — security, reliability, ops",
    "currency": "currency — current with the service today",
    "cost_awareness": "cost awareness",
    "evidence": "evidence — proportionate to the claims",
    "sources": "sources and citations",
    "code_quality": "code quality",
    "clarity": "clarity — can a reader follow it",
    "actionability": "actionability — can a reader use it",
}


@dataclass
class Recommendation:
    """One concrete change, with what it is worth."""

    dimension: str
    title: str
    detail: str
    action: str
    gain: float = 0.0
    """RQS points this is currently costing, or could realistically add."""

    certainty: str = "exact"
    """`exact` when a cap is provably costing this much; `estimate` otherwise."""

    measured: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["gain"] = round(self.gain, 1)
        d["dimension_label"] = DIMENSION_LABELS.get(self.dimension, self.dimension)
        return d


def _weight(dimension: str) -> float:
    return WEIGHTS.get(dimension, 0) / sum(WEIGHTS.values())


def _points(dimension: str, delta: float) -> float:
    """What `delta` points on one dimension is worth on the final RQS."""
    return max(0.0, delta) * _weight(dimension) * 100 / 100


# --------------------------------------------------------------------------
# The specific fix behind each cap. Keyed by a fragment of the cap message the
# scorer emits, so advice and enforcement cannot drift apart.
# --------------------------------------------------------------------------
CAP_ADVICE: list[tuple[str, str, str, str]] = [
    (
        "too short to carry much",
        "substance",
        "There is not enough here yet",
        "Under 250 words caps substance at 40. Not a style note - there is simply "
        "not room to establish a problem, show a solution and say what you learned.",
    ),
    (
        "too short to explain a mechanism",
        "explanation_depth",
        "There is not room to explain anything yet",
        "Under 250 words caps explanation depth at 45. Explaining why something "
        "behaves the way it does takes more space than describing that it does.",
    ),
    (
        "references AWS APIs that do not exist",
        "currency",
        "Check the API names against the current SDK",
        "An operation that is not in any AWS SDK is either invented or long "
        "removed. Either way a reader following it will fail.",
    ),
    (
        "cites AWS operations that do not exist",
        "accuracy",
        "Correct the operations that do not exist",
        "Accuracy is capped at 35 while the article references AWS operations "
        "that are not in any SDK - the clearest possible signal that a claim was "
        "never checked.",
    ),
    (
        "code is largely placeholders",
        "code_quality",
        "Replace placeholders with runnable values",
        "Code that is mostly YOUR_BUCKET and <region> caps code quality at 55. "
        "Show real (redacted) values so a reader can run it and then adapt it, "
        "rather than adapt it before they can run it.",
    ),
    (
        "long-form with no code, no numbers and no specifics",
        "substance",
        "Long, but thin on specifics",
        "A long piece with no code, no measurements and no concrete detail caps "
        "substance at 55. Length is not the problem; the absence of anything "
        "specific enough to be checked is.",
    ),
    (
        "no AWS service referenced at all",
        "aws_depth",
        "Connect it to AWS",
        "Builder Center readers come for AWS. Nothing here references a service, so "
        "this is capped at 10. If AWS is part of the work, name what you used and "
        "show a little of how.",
    ),
    (
        "services named, but nothing configured",
        "aws_depth",
        "Show the service being operated, not just named",
        "Services are mentioned but nothing is configured, invoked or measured, "
        "capping AWS depth at 49. One parameter that mattered, a quota you hit or a "
        "cost figure moves this more than another paragraph of description.",
    ),
    (
        "do not exist in any SDK",
        "credibility",
        "Correct the AWS APIs that do not exist",
        "One or more APIs referenced are not in any AWS SDK, which caps credibility "
        "at 29. This is the one hard disqualifier: an operation that does not exist "
        "means the content was never checked against reality.",
    ),
    (
        "most code blocks do not parse",
        "credibility",
        "Fix the code that does not parse",
        "Blocks failing a syntax check cap credibility at 55. Run each snippet "
        "through the interpreter before pasting it.",
    ),
    (
        "most outbound links are dead",
        "credibility",
        "Fix the dead links",
        "A large share of outbound links do not resolve, capping credibility at 50.",
    ),
    (
        "no outbound sources in a piece making factual claims",
        "credibility",
        "Cite sources for the factual claims",
        "A tutorial, deep dive, reference or news piece asserting facts with no "
        "outbound links is capped at 60. Opinion and experience pieces are exempt - "
        "this applies because of what the article claims to be.",
    ),
    (
        "too short to develop anything",
        "insight",
        "Develop the idea further",
        "Under 300 words caps insight at 40 - not enough room to establish a point "
        "and support it.",
    ),
    (
        "pseudo-headings instead of real structure",
        "clarity",
        "Use real markdown headings",
        "Sections marked with bold text rather than ## headings cap clarity at 65. "
        "Converting them also fixes navigation and the table of contents.",
    ),
]


def _cap_recommendations(score: dict[str, Any]) -> list[Recommendation]:
    """One recommendation per binding cap, priced exactly."""
    signals = score.get("signals") or {}
    raw = signals.get("raw_dimension_scores") or {}
    capped = {d["name"]: d["score"] for d in score.get("dimensions", [])}
    out: list[Recommendation] = []

    for note in signals.get("caps_applied", []):
        for fragment, dimension, title, detail in CAP_ADVICE:
            if fragment not in note:
                continue
            wanted = float(raw.get(dimension, 0.0))
            actual = float(capped.get(dimension, wanted))
            out.append(
                Recommendation(
                    dimension=dimension,
                    title=title,
                    detail=detail,
                    action=note,
                    # Exactly what the cap is costing right now.
                    gain=_points(dimension, wanted - actual),
                    certainty="exact",
                    measured={"judge_wanted": wanted, "capped_to": actual},
                )
            )
            break
    return out


def _headroom_recommendations(score: dict[str, Any]) -> list[Recommendation]:
    """Dimensions that are simply low, with no cap involved."""
    capped = {d["name"]: d for d in score.get("dimensions", [])}
    already = {r.dimension for r in _cap_recommendations(score)}
    out: list[Recommendation] = []

    hints = {
        "substance": (
            "Cut the padding, add specifics",
            "Replace the generic passages with concrete detail only you can supply - "
            "a parameter, a number, a constraint you hit.",
        ),
        "explanation_depth": (
            "Explain the mechanism, not just the behaviour",
            "Say why it works the way it does, not only what it does. That is the "
            "difference between a description and an explanation.",
        ),
        "insight": (
            "Say something the documentation does not",
            "A gotcha that is not written down, a tradeoff with your reasoning, why "
            "the obvious approach fails.",
        ),
        "accuracy": (
            "Tighten the claims that are loose",
            "Check the statements a knowledgeable reader would query, and qualify "
            "anything you are not certain of rather than stating it flat.",
        ),
        "scope_discipline": (
            "Deliver what the title promises",
            "Either narrow the title to what the article actually covers, or cover "
            "what the title claims. A deep-dive title on a quickstart costs trust.",
        ),
        "aws_service_depth": (
            "Operate the services, do not just name them",
            "One parameter that mattered, a quota you hit, an IAM condition or a "
            "cost figure is worth more than another paragraph describing a service.",
        ),
        "architecture_quality": (
            "Show the reasoning behind the design",
            "A diagram is not architecture. Say what you considered and rejected, "
            "and what constraint drove the shape of it.",
        ),
        "well_architected": (
            "Acknowledge what would bite in production",
            "Security, failure modes, scaling limits, operations. Not a checklist - "
            "just the parts a reader would hit that the happy path hides.",
        ),
        "currency": (
            "Check it against the service as it is today",
            "Patterns and APIs move. Anything superseded by a recent launch will "
            "mislead a reader who finds this in six months.",
        ),
        "cost_awareness": (
            "Say what this costs to run",
            "A real figure, or the pricing dimension that shapes the design. Readers "
            "have to pay for what they build from your article.",
        ),
        "evidence": (
            "Support the claims you are making",
            "Not every article needs terminal output - but every claim needs "
            "something behind it. Show the measurement for results, and say plainly "
            "what you did not test.",
        ),
        "sources": (
            "Cite the factual claims",
            "Quotas, limits, prices and behaviours read as assertions unless they "
            "point at the documentation that establishes them.",
        ),
        "code_quality": (
            "Make the code complete enough to run",
            "Include imports, versions and enough context that a reader can copy the "
            "block and get somewhere without reconstructing the rest.",
        ),
        "clarity": (
            "Make it easier to follow",
            "State the problem before the solution, use real headings, and give code "
            "enough surrounding context that a reader knows where it goes.",
        ),
        "actionability": (
            "Give the reader something to do",
            "End with the concrete next step - what to run, what to change, or what "
            "decision to make differently.",
        ),
    }

    for dimension, weight in sorted(WEIGHTS.items(), key=lambda kv: -kv[1]):
        current = capped.get(dimension, {}).get("score")
        if current is None or dimension in already or current >= GOOD_ENOUGH:
            continue
        if dimension not in hints:
            # A pillar added without a hint should degrade, not crash.
            continue
        title, detail = hints[dimension]
        rationale = capped.get(dimension, {}).get("rationale", "")
        out.append(
            Recommendation(
                dimension=dimension,
                title=title,
                detail=detail,
                action=rationale or "",
                gain=_points(dimension, GOOD_ENOUGH - float(current)),
                certainty="estimate",
                measured={"current": current, "target": GOOD_ENOUGH},
            )
        )
    return out


# Articles that exit through the cheap gates never reach a judge, so they have
# no dimensions and no caps to derive advice from - and they are precisely the
# authors who most need some. These map the structural findings the deterministic
# pass produced onto the same advice vocabulary.
FLOOR_ADVICE: list[tuple[str, str, str, str]] = [
    (
        "under 150 words",
        "depth",
        "There is not enough here to evaluate",
        "The article is too short to establish a problem, show a solution and say "
        "what you learned. Nothing below matters until there is more of it.",
    ),
    (
        "long-form with zero code",
        "code_substance",
        "Add the code you actually ran",
        "A long technical piece with no code blocks reads as a description of work "
        "rather than a record of it. Paste the commands, template or script.",
    ),
    (
        "no outbound sources",
        "source_integrity",
        "Cite your sources",
        "There are no outbound links at all. Link the AWS documentation, specs or "
        "repositories behind your non-obvious claims.",
    ),
    (
        "nonexistent AWS API",
        "code_substance",
        "Correct the AWS API names that do not exist",
        "One or more APIs referenced are not in any AWS SDK, which is strong "
        "evidence the code was never executed. Check each against botocore.",
    ),
    (
        "most code blocks do not parse",
        "code_substance",
        "Fix the code that does not parse",
        "Run each snippet through the interpreter before pasting it.",
    ),
    (
        "code is mostly placeholders",
        "code_substance",
        "Replace placeholders with runnable values",
        "Show real (redacted) values so a reader can run the snippet and then "
        "adapt it, rather than adapt it before they can run it.",
    ),
    (
        "near-duplicate",
        "originality",
        "This closely matches another author's article",
        "If it is a legitimate reuse, set a canonical URL; otherwise rewrite it in "
        "your own framing.",
    ),
    (
        "most links are dead",
        "source_integrity",
        "Fix the dead links",
        "A large share of the outbound links do not resolve.",
    ),
    (
        "bold-text pseudo-headings",
        "originality",
        "Use real markdown headings",
        "Sections marked with bold text rather than ## headings are one of the "
        "most consistent markers of generated structure.",
    ),
]


def _floor_recommendations(score: dict[str, Any]) -> list[Recommendation]:
    """Advice for articles that never reached a judge.

    Gain is deliberately left unquantified: without dimension scores there is
    nothing to compute a delta against, and inventing a number here would
    undermine the point of the exact ones elsewhere.
    """
    floor = (score.get("signals") or {}).get("heuristic") or {}
    out: list[Recommendation] = []
    seen: set[str] = set()

    for negative in floor.get("negatives", []):
        for fragment, dimension, title, detail in FLOOR_ADVICE:
            if fragment not in negative or title in seen:
                continue
            seen.add(title)
            out.append(
                Recommendation(
                    dimension=dimension,
                    title=title,
                    detail=detail,
                    action=negative,
                    gain=0.0,
                    certainty="structural",
                    measured={"finding": negative},
                )
            )
            break
    return out


def _judge_notes(score: dict[str, Any]) -> list[str]:
    """Concrete gaps the judges named, worth surfacing verbatim."""
    notes: list[str] = []
    for dim in score.get("dimensions", []):
        for ev in dim.get("evidence", []):
            if ev.get("weight_hint") == "negative" and ev.get("label"):
                notes.append(ev["label"])
    return notes[:8]


# --------------------------------------------------------------------------
def recommend(score: dict[str, Any]) -> dict[str, Any]:
    """Ranked, quantified recommendations for one scored article."""
    caps = _cap_recommendations(score)
    headroom = _headroom_recommendations(score)
    recommendations = sorted(caps + headroom, key=lambda r: -r.gain)

    # Articles that exited through a cheap gate have neither caps nor dimensions.
    # Fall back to the structural findings so the author still gets something.
    if not recommendations:
        recommendations = _floor_recommendations(score)

    rqs = float(score.get("rqs", 0.0))
    reachable = min(100.0, rqs + sum(r.gain for r in recommendations))

    to_next = None
    if rqs < SKIM_THRESHOLD:
        to_next = ("SKIM", round(SKIM_THRESHOLD - rqs, 1))
    elif rqs < READ_THRESHOLD:
        to_next = ("READ", round(READ_THRESHOLD - rqs, 1))

    return {
        "rqs": round(rqs, 1),
        "verdict": score.get("verdict"),
        "headline": score.get("headline"),
        "reachable_rqs": round(reachable, 1),
        "reachable_verdict": verdict_for(reachable),
        "points_to_next_band": (
            {"band": to_next[0], "points": to_next[1]} if to_next else None
        ),
        "recommendations": [r.to_dict() for r in recommendations],
        "judge_observations": _judge_notes(score),
        "strengths": (score.get("signals") or {}).get("heuristic", {}).get("positives", []),
    }


def render_text(advice: dict[str, Any], *, title: str = "") -> str:
    """Terminal-friendly rendering."""
    lines: list[str] = []
    if title:
        lines.append(f"\n  {title[:78]}")
    lines.append(
        f"  RQS {advice['rqs']:.0f}/100 -> {advice['verdict']}"
        + (
            f"   ({advice['points_to_next_band']['points']:.0f} points from "
            f"{advice['points_to_next_band']['band']})"
            if advice.get("points_to_next_band")
            else ""
        )
    )
    if advice["recommendations"]:
        lines.append(
            f"  addressing everything below reaches about "
            f"{advice['reachable_rqs']:.0f} ({advice['reachable_verdict']})\n"
        )
    if advice["strengths"]:
        lines.append("  already working:")
        for s in advice["strengths"][:5]:
            lines.append(f"    + {s}")
        lines.append("")

    if not advice["recommendations"]:
        lines.append("  no specific improvements identified - this one is in good shape.")
        return "\n".join(lines) + "\n"

    lines.append("  recommendations, highest impact first:\n")
    for i, r in enumerate(advice["recommendations"], 1):
        if r["certainty"] == "structural":
            lines.append(f"  {i}. {r['title']}")
        else:
            marker = "=" if r["certainty"] == "exact" else "~"
            lines.append(f"  {i}. {r['title']}   [{marker}{r['gain']:.1f} RQS]")
        lines.append(f"     {r['dimension_label']}")
        for chunk in _wrap(r["detail"], 74):
            lines.append(f"     {chunk}")
        lines.append("")

    if advice["judge_observations"]:
        lines.append("  specific gaps noted while reviewing:")
        for note in advice["judge_observations"][:5]:
            lines.append(f"    - {note[:90]}")
    lines.append("")
    if any(r["certainty"] != "structural" for r in advice["recommendations"]):
        lines.append("  [= exact: this cap is provably costing that much]")
        lines.append("  [~ estimate: realistic headroom on that dimension]")
    return "\n".join(lines) + "\n"


def _wrap(text: str, width: int) -> list[str]:
    words, line, out = re.split(r"\s+", text.strip()), "", []
    for word in words:
        if len(line) + len(word) + 1 > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return out
