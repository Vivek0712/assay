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
    "execution_evidence": "evidence you actually ran it",
    "code_substance": "code substance",
    "source_integrity": "sources and citations",
    "depth": "depth beyond the docs",
    "originality": "originality",
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
        "no code blocks",
        "code_substance",
        "Add the code you actually ran",
        "This reads as a how-to but contains no code blocks, so the code score is "
        "capped at 20. Paste the actual commands, template or script - even a "
        "short complete one beats a described one.",
    ),
    (
        # The relaxed cap: code is not required here, so this is an opportunity
        # rather than a defect and the wording should not scold.
        "not the kind of article that needs it",
        "code_substance",
        "Optional: a small worked example would still help",
        "This is an opinion or news piece, so no code is expected and the score "
        "is not penalised for its absence - the code dimension is simply held at "
        "55. If there is a natural place for one concrete snippet or a "
        "configuration fragment, it would lift this without changing the piece.",
    ),
    (
        "most code blocks do not parse",
        "code_substance",
        "Fix the code that does not parse",
        "Some blocks fail a syntax check, which caps the code score at 40. Run each "
        "snippet through the interpreter before pasting it; a reader who copies "
        "broken code will not come back.",
    ),
    (
        "largely placeholders",
        "code_substance",
        "Replace placeholders with runnable values",
        "The code is mostly YOUR_BUCKET / <region> style placeholders, which caps "
        "the code score at 50. Show real (redacted) values so the snippet can be "
        "run and then adapted, rather than adapted before it can be run.",
    ),
    (
        "do not exist",
        "code_substance",
        "Correct the AWS API names that do not exist",
        "One or more APIs referenced are not in any AWS SDK. This caps both code "
        "and sources at 25, because a nonexistent operation means the code was "
        "never executed. Check each against botocore or the CLI reference.",
    ),
    (
        "no outbound sources at all",
        "source_integrity",
        "Cite your sources",
        "There are no outbound links, which caps the source score at 30. Link the "
        "AWS documentation pages, specs or repositories behind your non-obvious "
        "claims - quotas, limits, pricing and behaviour especially.",
    ),
    (
        "no primary sources",
        "source_integrity",
        "Link primary sources, not just commentary",
        "The links present go to blogs and community posts rather than AWS docs, "
        "specs or repositories, which caps the source score at 55. Point at the "
        "authoritative page for anything factual.",
    ),
    (
        "most links are dead",
        "source_integrity",
        "Fix the dead links",
        "A large share of outbound links do not resolve, capping sources at 40. "
        "Re-check them; AWS documentation URLs move more often than most.",
    ),
    (
        "no terminal output",
        "execution_evidence",
        "Paste what actually happened when you ran it",
        "There is no console output, screenshot or measurement anywhere, which "
        "caps execution evidence at 25 - the heaviest dimension in the score. "
        "Paste the real command output, including anything that went wrong.",
    ),
    (
        "numbers quoted but no output",
        "execution_evidence",
        "Show the run, not just the results",
        "You quote figures but never show them being produced, capping execution "
        "evidence at 45. A pasted session or a screenshot of your own console is "
        "the difference between reporting a result and demonstrating one.",
    ),
    (
        "near-duplicate",
        "originality",
        "This closely matches another author's article",
        "The body is a near-duplicate of a different author's published piece, "
        "capping originality at 10. If it is a legitimate reuse, set a canonical "
        "URL; if it is coincidental overlap, rewrite in your own framing.",
    ),
    (
        "bold-text pseudo-headings",
        "originality",
        "Use real markdown headings",
        "Sections are marked with bold text rather than ## headings, which caps "
        "originality at 55 - it is one of the most consistent markers of generated "
        "structure. Convert them; it also fixes your table of contents.",
    ),
    (
        "too short to develop",
        "depth",
        "Develop the idea further",
        "Under 300 words caps depth at 35. There is not room to establish a "
        "problem, show a solution and explain a tradeoff in that space.",
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
        "execution_evidence": (
            "Add an artefact only a real run produces",
            "Console output, a stack trace you hit and fixed, a timing, a bill, a "
            "screenshot of your own resources. This dimension carries the most "
            "weight of any in the score.",
        ),
        "code_substance": (
            "Make the code complete enough to run",
            "Include imports, versions and enough context that a reader can copy "
            "the block and get somewhere without reconstructing the rest.",
        ),
        "source_integrity": (
            "Anchor the factual claims",
            "Quotas, limits, pricing and behavioural claims read as assertions "
            "unless they point at the documentation that establishes them.",
        ),
        "depth": (
            "Say something the documentation does not",
            "A gotcha that is not written down, a tradeoff with your reasoning, a "
            "failure mode and its cause. Restating what a service does is the "
            "single most common reason an article scores mid-range.",
        ),
        "originality": (
            "Make it unmistakably yours",
            "Your specific problem, your constraints, your decision. Content that "
            "would read the same with the service name swapped scores low here.",
        ),
    }

    for dimension, weight in sorted(WEIGHTS.items(), key=lambda kv: -kv[1]):
        current = capped.get(dimension, {}).get("score")
        if current is None or dimension in already or current >= GOOD_ENOUGH:
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
