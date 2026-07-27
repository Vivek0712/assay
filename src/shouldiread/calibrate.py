"""Calibration: does the fleet agree with a human, and with itself?

Three checks, because each catches a different failure:

* agreement   - fleet verdict vs hand labels on a golden set. Catches miscalibration.
* consistency - the same article scored repeatedly. Catches an unstable judge.
* coherence   - scores vs the deterministic signals across the whole corpus.
                Catches a judge that has quietly stopped reading the evidence,
                which no single-article test would reveal.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .config import ROOT, verdict_for

GOLDEN_PATH = ROOT / "tests" / "golden_set.yaml"
VERDICTS = ("READ", "SKIM", "SKIP")

# Cohorts smaller than this are reported but flagged: too few articles to carry
# a claim about separation.
MIN_COHORT = 20


@dataclass
class GoldenLabel:
    article_id: str
    title: str
    verdict: str
    note: str = ""


def load_golden(path: Path = GOLDEN_PATH) -> list[GoldenLabel]:
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text()) or {}
    return [
        GoldenLabel(
            article_id=item["article_id"],
            title=item.get("title", ""),
            verdict=item["verdict"].upper(),
            note=item.get("note", ""),
        )
        for item in raw.get("labels", [])
    ]


def confusion(pairs: list[tuple[str, str]]) -> dict[str, dict[str, int]]:
    """{expected: {predicted: count}}"""
    m = {e: {p: 0 for p in VERDICTS} for e in VERDICTS}
    for expected, predicted in pairs:
        if expected in m and predicted in m[expected]:
            m[expected][predicted] += 1
    return m


def _ordinal(v: str) -> int:
    return {"SKIP": 0, "SKIM": 1, "READ": 2}[v]


def agreement_report(scores: list[dict[str, Any]], golden: list[GoldenLabel]) -> dict[str, Any]:
    """Fleet verdicts vs hand labels."""
    by_id = {s["article_id"]: s for s in scores}
    pairs: list[tuple[str, str]] = []
    misses: list[dict[str, Any]] = []

    for label in golden:
        got = by_id.get(label.article_id)
        if not got:
            continue
        pairs.append((label.verdict, got["verdict"]))
        if label.verdict != got["verdict"]:
            misses.append(
                {
                    "title": label.title or got["title"],
                    "expected": label.verdict,
                    "got": got["verdict"],
                    "rqs": got["rqs"],
                    "headline": got["headline"],
                    "note": label.note,
                    # A SKIP labelled READ is a far worse error than SKIM/READ.
                    "severity": abs(_ordinal(label.verdict) - _ordinal(got["verdict"])),
                }
            )

    n = len(pairs)
    exact = sum(1 for e, p in pairs if e == p)
    adjacent = sum(1 for e, p in pairs if abs(_ordinal(e) - _ordinal(p)) <= 1)
    severe = sum(1 for m in misses if m["severity"] >= 2)

    return {
        "labelled": len(golden),
        "matched": n,
        "exact_agreement": round(exact / n, 3) if n else None,
        "within_one_band": round(adjacent / n, 3) if n else None,
        "severe_disagreements": severe,
        "confusion": confusion(pairs),
        "disagreements": sorted(misses, key=lambda m: -m["severity"]),
    }


def coherence_report(scores: list[dict[str, Any]]) -> dict[str, Any]:
    """Do the scores track the measured evidence across the corpus?

    Every check here is a property that must hold by construction. A violation
    means a cap failed or a judge overrode the evidence, and is a bug.
    """
    violations: list[dict[str, Any]] = []

    def sig(s: dict, *path: str, default: Any = 0) -> Any:
        cur: Any = s.get("signals") or {}
        for p in path:
            cur = (cur or {}).get(p) if isinstance(cur, dict) else None
        return default if cur is None else cur

    def dim(s: dict, name: str) -> float | None:
        for d in s.get("dimensions", []):
            if d["name"] == name:
                return d["score"]
        return None

    for s in scores:
        code_dim = dim(s, "code_substance")
        ctype = sig(s, "content_type", default="other")
        code_optional = ctype in {"news", "opinion", "announcement", "certification_journey"}

        if sig(s, "structure", "code_blocks") == 0 and code_dim is not None:
            ceiling = 55 if code_optional else 20
            if code_dim > ceiling:
                violations.append(
                    {"article_id": s["article_id"], "rule": "code score without code",
                     "value": code_dim, "ceiling": ceiling}
                )

        exec_dim = dim(s, "execution_evidence")
        if (
            exec_dim is not None
            and exec_dim > 45
            and sig(s, "structure", "terminal_evidence") == 0
            and sig(s, "code", "output_blocks") == 0
            and sig(s, "structure", "images") == 0
        ):
            violations.append(
                {"article_id": s["article_id"], "rule": "execution score without artefacts",
                 "value": exec_dim, "ceiling": 45}
            )

        src_dim = dim(s, "source_integrity")
        if src_dim is not None and src_dim > 30 and sig(s, "structure", "links") == 0:
            violations.append(
                {"article_id": s["article_id"], "rule": "source score with zero links",
                 "value": src_dim, "ceiling": 30}
            )

        if s["verdict"] != verdict_for(s["rqs"]):
            violations.append(
                {"article_id": s["article_id"], "rule": "verdict does not match RQS",
                 "value": s["verdict"], "ceiling": verdict_for(s["rqs"])}
            )

    # Does having real evidence actually move the score?
    with_output = [s["rqs"] for s in scores if sig(s, "code", "output_blocks") > 0]
    without = [s["rqs"] for s in scores if sig(s, "code", "output_blocks") == 0]
    with_code = [s["rqs"] for s in scores if sig(s, "structure", "code_blocks") > 0]
    no_code = [s["rqs"] for s in scores if sig(s, "structure", "code_blocks") == 0]

    def cohort(name: str, xs: list[float]) -> dict[str, Any]:
        """A cohort always travels with its size.

        Reporting a bare median let an n=1 cohort look like a finding, and that
        number made it into a draft as a '46-point separation'. The size is not
        optional context.
        """
        return {
            "cohort": name,
            "n": len(xs),
            "median_rqs": round(statistics.median(xs), 1) if xs else None,
            "mean_rqs": round(statistics.mean(xs), 1) if xs else None,
            # Below this, quote it as an observation, never as a separation.
            "underpowered": len(xs) < MIN_COHORT,
        }

    return {
        "checked": len(scores),
        "violations": violations,
        "violation_count": len(violations),
        "cohorts": [
            cohort("shows real terminal output", with_output),
            cohort("no terminal output", without),
            cohort("contains code", with_code),
            cohort("contains no code", no_code),
        ],
    }


def consistency_report(runs: list[list[dict[str, Any]]]) -> dict[str, Any]:
    """Spread of RQS for the same articles scored across repeated runs."""
    by_id: dict[str, list[float]] = {}
    for run in runs:
        for s in run:
            by_id.setdefault(s["article_id"], []).append(s["rqs"])

    repeated = {k: v for k, v in by_id.items() if len(v) > 1}
    if not repeated:
        return {"repeated_articles": 0}

    spreads = [max(v) - min(v) for v in repeated.values()]
    flips = sum(
        1 for v in repeated.values() if len({verdict_for(x) for x in v}) > 1
    )
    return {
        "repeated_articles": len(repeated),
        "mean_spread": round(statistics.mean(spreads), 2),
        "max_spread": round(max(spreads), 2),
        "verdict_flips": flips,
        "flip_rate": round(flips / len(repeated), 3),
    }


def render_markdown(agreement: dict[str, Any], coherence: dict[str, Any]) -> str:
    lines = ["# Calibration report", ""]

    lines += ["## Agreement with hand labels", ""]
    if agreement.get("matched"):
        lines += [
            f"- golden set: **{agreement['matched']}** articles labelled by hand",
            f"- exact verdict agreement: **{agreement['exact_agreement']:.0%}**",
            f"- within one band: **{agreement['within_one_band']:.0%}**",
            f"- severe disagreements (READ vs SKIP): **{agreement['severe_disagreements']}**",
            "",
            "| expected \\ predicted | READ | SKIM | SKIP |",
            "|---|---|---|---|",
        ]
        for exp in VERDICTS:
            row = agreement["confusion"][exp]
            lines.append(f"| **{exp}** | {row['READ']} | {row['SKIM']} | {row['SKIP']} |")
        if agreement["disagreements"]:
            lines += ["", "### Disagreements", ""]
            for d in agreement["disagreements"]:
                lines.append(
                    f"- `{d['expected']}` -> `{d['got']}` ({d['rqs']:.0f}) "
                    f"**{d['title'][:70]}** - {d['note'] or d['headline'][:90]}"
                )
    else:
        lines.append("_No golden set matched this run._")

    lines += ["", "## Coherence with measured signals", ""]
    lines.append(f"- articles checked: **{coherence['checked']}**")
    lines.append(f"- cap violations: **{coherence['violation_count']}**")
    lines += ["", "| cohort | n | median RQS | mean RQS | |", "|---|---|---|---|---|"]
    for c in coherence["cohorts"]:
        flag = f"underpowered (n < {MIN_COHORT})" if c["underpowered"] else ""
        lines.append(
            f"| {c['cohort']} | {c['n']} | {c['median_rqs']} | {c['mean_rqs']} | {flag} |"
        )
    if any(c["underpowered"] for c in coherence["cohorts"]):
        lines += [
            "",
            "> Underpowered cohorts are shown for completeness. Do not quote them as "
            "separations - there are not enough articles behind the number.",
        ]
    if coherence["violations"]:
        lines += ["", "### Violations", ""]
        for v in coherence["violations"][:20]:
            lines.append(
                f"- `{v['article_id']}` {v['rule']}: {v['value']} > {v['ceiling']}"
            )
    return "\n".join(lines) + "\n"
