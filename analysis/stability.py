#!/usr/bin/env python3
"""How much does the score move when nothing about the article does?

An LLM judge whose variance you have not measured is a judge whose scores you
cannot interpret. This scores the same article repeatedly and reports the
spread.

    python analysis/stability.py --samples 5
    python analysis/stability.py --samples 5 --judge-samples 1   # no median
    python analysis/stability.py https://builder.aws.com/content/<id>/<slug>
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from shouldiread.config import verdict_for  # noqa: E402
from shouldiread.models import Article, Author  # noqa: E402


def local_article(path: Path) -> Article:
    md = path.read_text()
    first = md.splitlines()[0] if md else ""
    return Article(
        article_id="self",
        title=first.lstrip("# ").strip() or path.stem,
        uri="/content/self/own-post",
        markdown=md,
        tags=["generative-ai"],
        author=Author(alias="self"),
    )


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("target", nargs="?", default="blog/post.md",
                    help="a local markdown file, or a builder.aws.com URL / id")
    ap.add_argument("--samples", type=int, default=5, help="how many times to score it")
    ap.add_argument("--judge-samples", type=int, default=None,
                    help="override SIR_JUDGE_SAMPLES for this run")
    args = ap.parse_args()

    if args.judge_samples is not None:
        os.environ["SIR_JUDGE_SAMPLES"] = str(args.judge_samples)

    # Imported after the env override so JUDGE_SAMPLES picks it up.
    from shouldiread.agents import ScoringFleet

    path = Path(args.target)
    if path.exists():
        article = local_article(path)
    else:
        import httpx

        from shouldiread.ingest import article_id_from_url, fetch

        aid = article_id_from_url(args.target) or args.target
        async with httpx.AsyncClient(follow_redirects=True) as client:
            fetched = await fetch(client, aid)
        if fetched is None:
            print(f"could not retrieve {args.target!r}", file=sys.stderr)
            return 1
        article = fetched

    fleet = ScoringFleet()
    scores: list[float] = []
    per_dimension: dict[str, list[float]] = {}

    for i in range(args.samples):
        score = await fleet.score(article, check_links=False)
        scores.append(score.rqs)
        dims = {d.name: d.score for d in score.dimensions}
        for name, value in dims.items():
            per_dimension.setdefault(name, []).append(value)
        print(
            f"  run {i + 1}: RQS {score.rqs:5.1f} {score.verdict:5} "
            + " ".join(f"{k[:4]}={v:.0f}" for k, v in dims.items())
        )

    print(f"\n  n={len(scores)}  mean={statistics.mean(scores):.1f}", end="")
    if len(scores) > 1:
        print(
            f"  stdev={statistics.stdev(scores):.1f}"
            f"  spread={max(scores) - min(scores):.1f}"
        )
    else:
        print()
    print(f"  verdicts: {sorted({verdict_for(s) for s in scores})}")

    if per_dimension:
        print("\n  per-dimension spread across runs:")
        for name, values in per_dimension.items():
            print(f"    {name:20} min={min(values):3.0f} max={max(values):3.0f} "
                  f"spread={max(values) - min(values):3.0f}")

    # A verdict that changes between identical runs is the failure that matters.
    if len({verdict_for(s) for s in scores}) > 1:
        print("\n  WARNING: the verdict is not stable on identical input")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
