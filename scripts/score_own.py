#!/usr/bin/env python3
"""Score our own writing with our own scorer.

The self-test gate: a post arguing that Builder Center content is unverified has
to survive its own check before it ships.

    python scripts/score_own.py blog/post.md
    python scripts/score_own.py blog/post.md --signals-only   # no Bedrock calls
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from shouldiread.agents import ScoringFleet  # noqa: E402
from shouldiread.analyze import analyze, heuristic_floor  # noqa: E402
from shouldiread.models import Article, Author  # noqa: E402


def load(path: Path) -> Article:
    md = path.read_text()
    first = md.splitlines()[0] if md else ""
    title = first.lstrip("# ").strip() if first.startswith("#") else path.stem
    return Article(
        article_id="self",
        title=title,
        uri="/content/self/own-post",
        markdown=md,
        tags=["generative-ai", "aws", "agents"],
        author=Author(alias="self", preferred_name="self", is_community_builder=True),
    )


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path", nargs="?", default="blog/post.md")
    ap.add_argument("--threshold", type=float, default=85.0)
    ap.add_argument("--signals-only", action="store_true", help="skip the model calls")
    args = ap.parse_args()

    article = load(Path(args.path))
    signals = await analyze(article, check_links=True)
    floor = heuristic_floor(signals)

    s, c, l, a = signals["structure"], signals["code"], signals["links"], signals["aws_apis"]
    print(f"\n  {article.title[:78]}")
    print(
        f"  words={s['words']}  code_blocks={s['code_blocks']} ({s['code_loc']} loc)  "
        f"parse={c['passed']}/{c['checked']}  terminal_sessions={c['output_blocks']}"
    )
    print(
        f"  links={s['links']} (primary={l['primary_sources']}, "
        f"dead={l['dead']}/{l['checked']})  measurements={s['measurements']}  "
        f"headings={s['atx_headings']}  pseudo={s['bold_pseudo_headings']}"
    )
    print(
        f"  aws_apis checked={a['total_checked']} invalid={a['total_invalid']} "
        f"suspect={a['suspect_api_names']}"
    )
    if c["failures"]:
        print("  CODE THAT DOES NOT PARSE:")
        for f in c["failures"]:
            print(f"    block {f['index']} [{f['lang']}]: {f['error'][:110]}")
    if l["dead_urls"]:
        print(f"  DEAD LINKS: {l['dead_urls']}")
    print(f"  + {', '.join(floor['positives']) or 'none'}")
    print(f"  - {', '.join(floor['negatives']) or 'none'}")

    if args.signals_only:
        return 0

    score = await ScoringFleet().score(article, check_links=True)
    print(f"\n  RQS {score.rqs:.1f}/100  ->  {score.verdict}")
    print(f"  {score.headline}\n")
    for d in score.dimensions:
        print(f"    {d.name:<20} {d.score:>5.0f}   {d.rationale}")
        for e in d.evidence[:3]:
            mark = {"positive": "+", "negative": "-"}.get(e.weight_hint, "*")
            print(f"       {mark} {e.label[:92]}")
    for cap in score.signals.get("caps_applied", []):
        print(f"    [cap] {cap}")

    ok = score.rqs >= args.threshold
    print(
        f"\n  gate: RQS {score.rqs:.1f} {'>=' if ok else '<'} {args.threshold} -> "
        f"{'PASS' if ok else 'FAIL - revise'}\n"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
