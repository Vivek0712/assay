#!/usr/bin/env python3
"""A review council: what should Builder Center content actually be judged on?

The first rubric was written by one person with one use case - "which of today's
forty articles do I open?" - and it scored a substantial project write-up as
SKIM because the author linked a working repository instead of pasting a
terminal session. That is a rubric problem, not a judgement problem.

So this asks four constituencies who read Builder Center for different reasons
what evidence *they* would want, then synthesises their answers into pillars.
Each seat argues its own corner; the synthesis has to reconcile them. Run it and
you get the reasoning, not just the conclusion.

    python analysis/council.py
    python analysis/council.py --article <builder.aws.com url>   # ground it in a case
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pydantic import BaseModel, Field  # noqa: E402
from strands import Agent  # noqa: E402

from shouldiread.agents.fleet import _model  # noqa: E402
from shouldiread.config import MODEL_JUDGE, WEIGHTS  # noqa: E402

# --------------------------------------------------------------------------
# The seats. Chosen because each has a different reason to open an article, and
# therefore a different idea of what "worth reading" means.
# --------------------------------------------------------------------------
SEATS = {
    "aws_advocate": """You are an AWS developer advocate. You read Builder Center to find
community work worth amplifying, and you are accountable for what you put in front of
customers. You care whether the services are used correctly, whether the content would
still be right in six months, and whether it makes a reader more capable. You are wary of
content that is technically fine but teaches nothing, and equally wary of rubrics that
reward performative detail over genuine usefulness.""",

    "aws_hero": """You are an AWS Hero who builds substantial systems and writes them up. You
read to find people doing genuinely hard things. You care about the ambition and novelty of
what was built, whether the author hit real constraints, and whether they are honest about
what did not work. You are the person most likely to notice when a rubric rewards
ceremony - pasted logs, ritual citations - over the harder question of whether anything
interesting was actually built.""",

    "practitioner": """You are a working cloud engineer with a problem to solve today. You read
to get unstuck. You care about whether you can reproduce this, whether the code runs,
whether the gotchas are documented, and whether the author tells you what it costs. You do
not care how novel it is. A boring article that saves you four hours beats a brilliant one
you cannot act on.""",

    "learner": """You are eighteen months into cloud, using Builder Center to learn. You care
whether an article is followable, whether it explains *why* and not just *what*, and
whether the author shows their reasoning. You are the reader most damaged by confident,
polished, subtly wrong content, because you cannot yet tell. You also resent rubrics that
dismiss beginner-facing writing as shallow when it is doing something genuinely hard.""",
}


class SeatOpinion(BaseModel):
    """One constituency's view of what should be measured."""

    must_measure: list[str] = Field(
        description="What this reader needs measured to decide whether to open an article. "
        "Concrete and observable, 3-5 items."
    )
    evidence_that_counts: list[str] = Field(
        description="Artefacts that credibly show the author did the work, from THIS "
        "reader's point of view. Be specific and think beyond pasted terminal output."
    )
    current_rubric_gaps: list[str] = Field(
        description="Where the current pillars would misjudge content this reader values"
    )
    would_overweight: str = Field(
        description="One thing the current rubric weights too heavily for this reader"
    )
    rationale: str = Field(description="Three sentences maximum")


class Pillar(BaseModel):
    name: str = Field(description="Short snake_case identifier")
    label: str = Field(description="Human-readable name")
    weight: int = Field(ge=0, le=40, description="Weight out of 100")
    measures: str = Field(description="What it measures, one sentence")
    evidence: list[str] = Field(description="Observable artefacts that satisfy it")


class CouncilVerdict(BaseModel):
    """The synthesis. Must reconcile the seats, not average them."""

    pillars: list[Pillar] = Field(description="Five or six pillars, weights summing to 100")
    key_change: str = Field(
        description="The single most important change from the current rubric, and why"
    )
    disagreement: str = Field(
        description="Where the seats genuinely conflicted and how you resolved it"
    )
    keep_unchanged: list[str] = Field(
        description="What the current rubric gets right and must not be diluted"
    )


CURRENT = "\n".join(f"  {k}: {v}" for k, v in WEIGHTS.items())

SYNTHESIS = """You are chairing a review council for AWS Builder Center.

The current rubric weights five pillars:
{current}

It has one known failure. An AWS Hero published a substantial write-up of a physical-AI
system driving a simulated NASA Mars rover: 8 code blocks, 3 complete runnable files, two
linked GitHub repositories including the author's own implementation, and a hero image.
Depth scored 85 and originality 90. But `execution_evidence` was capped at 25 because the
article pasted no terminal output, and since that pillar carries the most weight the
article landed at SKIM.

The author shipped working repositories. The rubric could not see them.

Below are four constituencies' views. Reconcile them into pillars. Constraints:

- Five or six pillars, weights summing to exactly 100.
- Do not simply average the seats. Where they conflict, choose and justify.
- Keep what works: measurability, and the principle that a claim of having done
  something must be backed by an artefact.
- The failure to fix is that "evidence of doing the work" was collapsed into a single
  narrow proxy. Broaden what counts without letting it become unfalsifiable.
- Resist adding a pillar per grievance. A rubric with nine pillars measures nothing.
"""


async def ask_seat(name: str, persona: str, case: str) -> SeatOpinion | None:
    agent = Agent(
        model=_model(MODEL_JUDGE, temperature=0.3),
        system_prompt=f"{persona}\n\nAnswer only from this seat's point of view. Do not "
        f"try to be balanced - the chair will reconcile.",
        structured_output_model=SeatOpinion,
        callback_handler=None,
    )
    prompt = (
        "AWS Builder Center publishes ~300 articles a week. A scoring system decides which "
        "are worth opening.\n\nThe current pillars and weights:\n" + CURRENT + "\n\n" + case +
        "\n\nWhat should be measured, from your seat?"
    )
    try:
        return (await agent.invoke_async(prompt)).structured_output
    except Exception as exc:
        print(f"  {name} seat failed: {exc}", file=sys.stderr)
        return None


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--article", default=None, help="ground the council in a real article")
    ap.add_argument("--output", default="docs/council.md")
    args = ap.parse_args()

    case = (
        "A concrete case the current rubric got wrong: an AWS Hero wrote up a physical-AI "
        "system driving a simulated NASA Mars rover. Eight code blocks, three complete "
        "runnable files, two linked GitHub repositories including their own implementation. "
        "Depth 85, originality 90. But it pasted no terminal output, so evidence-of-execution "
        "was capped at 25, and the article scored SKIM."
    )
    if args.article:
        import httpx

        from shouldiread.ingest import article_id_from_url, fetch

        async with httpx.AsyncClient(follow_redirects=True) as client:
            article = await fetch(client, article_id_from_url(args.article) or args.article)
        if article:
            case += f"\n\nThe article in question:\n{article.markdown[:6000]}"

    print("convening the council ...")
    opinions = dict(
        zip(
            SEATS,
            await asyncio.gather(*(ask_seat(n, p, case) for n, p in SEATS.items())),
        )
    )

    lines = ["# Review council", "", "## Seats", ""]
    for name, op in opinions.items():
        if not op:
            continue
        print(f"\n{name}")
        print(f"  overweighted today: {op.would_overweight}")
        lines += [
            f"### {name.replace('_', ' ')}", "",
            f"_{op.rationale}_", "",
            "**Must measure:** " + "; ".join(op.must_measure), "",
            "**Evidence that counts:** " + "; ".join(op.evidence_that_counts), "",
            "**Gaps in the current rubric:** " + "; ".join(op.current_rubric_gaps), "",
            f"**Overweighted today:** {op.would_overweight}", "",
        ]
        for gap in op.current_rubric_gaps[:2]:
            print(f"    gap: {gap[:100]}")

    seat_text = json.dumps(
        {k: v.model_dump() for k, v in opinions.items() if v}, indent=2
    )
    chair = Agent(
        model=_model(MODEL_JUDGE, temperature=0.2),
        system_prompt=SYNTHESIS.format(current=CURRENT),
        structured_output_model=CouncilVerdict,
        callback_handler=None,
    )
    verdict = (await chair.invoke_async(f"The seats said:\n\n{seat_text}")).structured_output

    print(f"\n{'=' * 70}\nPROPOSED PILLARS\n")
    total = 0
    for p in verdict.pillars:
        total += p.weight
        print(f"  {p.weight:3}  {p.label}")
        print(f"       {p.measures}")
    print(f"  ---  total {total}")
    print(f"\nkey change: {verdict.key_change}")
    print(f"\ndisagreement: {verdict.disagreement}")

    lines += ["## Proposed pillars", "", "| weight | pillar | measures |", "|---|---|---|"]
    lines += [f"| {p.weight} | {p.label} | {p.measures} |" for p in verdict.pillars]
    lines += [
        "", f"**Total: {total}**", "",
        "### Evidence each pillar accepts", "",
    ]
    for p in verdict.pillars:
        lines += [f"- **{p.label}** — " + "; ".join(p.evidence)]
    lines += [
        "", "### Key change", "", verdict.key_change,
        "", "### Where the seats disagreed", "", verdict.disagreement,
        "", "### Kept unchanged", "",
    ] + [f"- {k}" for k in verdict.keep_unchanged]

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")
    return 0 if total == 100 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
