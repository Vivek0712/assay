"""Command line entry point.

    assay run --days 7                        score the last 7 days (default)
    assay score <url-or-id>              score one article
    assay review <url-or-file>           score it, then say what would improve it
    assay report --run latest            summary of a scored run
    assay page --run latest              build the HTML leaderboard
    assay review-page                    build the author-facing improvement page
    assay feed --run latest              build the filtered RSS feed
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from .config import SCORES_DIR
from .corpus import load_scores, run_corpus


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )


async def _cmd_run(args: argparse.Namespace) -> int:
    summary = await run_corpus(
        days=args.days,
        limit=args.limit,
        run=args.run,
        check_links=not args.no_links,
        resume=not args.no_resume,
    )
    print(json.dumps(summary, indent=2))
    return 0


async def _cmd_score(args: argparse.Namespace) -> int:
    import httpx

    from .agents import ScoringFleet
    from .ingest import article_id_from_url, fetch

    aid = article_id_from_url(args.target) or args.target
    async with httpx.AsyncClient(follow_redirects=True) as client:
        article = await fetch(client, aid, use_cache=not args.no_cache)
    if article is None:
        print(f"could not retrieve article {aid!r}", file=sys.stderr)
        return 1

    score = await ScoringFleet().score(article, check_links=not args.no_links)
    if args.json:
        print(json.dumps(score.to_dict(), indent=2, ensure_ascii=False))
        return 0

    print(f"\n  {score.verdict}  ({score.rqs:.0f}/100)   {article.title}")
    print(f"  {article.url}\n")
    print(f"  {score.headline}\n")
    for d in score.dimensions:
        print(f"    {d.name:<20} {d.score:>5.0f}/100   {d.rationale}")
        for e in d.evidence[:3]:
            mark = {"positive": "+", "negative": "-"}.get(e.weight_hint, "*")
            print(f"       {mark} {e.label[:96]}")
    for cap in score.signals.get("caps_applied", []):
        print(f"    [cap] {cap}")
    if score.signals.get("best_for"):
        print(f"\n  best for: {score.signals['best_for']}")
    print()
    return 0


async def _cmd_review(args: argparse.Namespace) -> int:
    """Score a draft or a published article, then say what would move it."""
    from pathlib import Path

    import httpx

    from .advise import recommend, render_text
    from .agents import ScoringFleet
    from .ingest import article_id_from_url, fetch
    from .models import Article, Author

    path = Path(args.target)
    if path.exists():
        md = path.read_text()
        first = md.splitlines()[0] if md else ""
        article = Article(
            article_id="draft",
            title=(first.lstrip("# ").strip() if first.startswith("#") else path.stem),
            uri="/content/draft/local",
            markdown=md,
            author=Author(alias=args.author or ""),
        )
    else:
        aid = article_id_from_url(args.target) or args.target
        async with httpx.AsyncClient(follow_redirects=True) as client:
            fetched = await fetch(client, aid, use_cache=not args.no_cache)
        if fetched is None:
            print(f"could not retrieve {args.target!r}", file=sys.stderr)
            return 1
        article = fetched

    score = await ScoringFleet().score(article, check_links=not args.no_links)
    advice = recommend(score.to_dict())

    if args.json:
        print(json.dumps({**advice, "title": article.title, "url": article.url}, indent=2))
    else:
        print(render_text(advice, title=article.title))
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    scores = load_scores(args.run)
    if not scores:
        print(f"no scores for run {args.run!r} (looked in {SCORES_DIR})", file=sys.stderr)
        return 1

    scores.sort(key=lambda s: s["rqs"], reverse=True)
    counts: dict[str, int] = {}
    for s in scores:
        counts[s["verdict"]] = counts.get(s["verdict"], 0) + 1
    n = len(scores)

    print(f"\nrun {args.run!r}: {n} articles scored\n")
    for verdict in ("READ", "SKIM", "SKIP"):
        c = counts.get(verdict, 0)
        print(f"  {verdict:5} {c:5}  {c / n * 100:5.1f}%  {'#' * int(c / n * 40)}")

    print(f"\n  median RQS: {scores[n // 2]['rqs']:.1f}")
    print(f"\ntop {min(args.top, n)}:\n")
    for s in scores[: args.top]:
        print(f"  {s['rqs']:5.1f}  {s['verdict']:5}  {s['title'][:60]}")
        print(f"         {s['headline'][:100]}")
    return 0


def _cmd_calibrate(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .calibrate import (
        agreement_report,
        coherence_report,
        load_golden,
        render_markdown,
    )

    scores = load_scores(args.run)
    if not scores:
        print(f"no scores for run {args.run!r}", file=sys.stderr)
        return 1

    agreement = agreement_report(scores, load_golden())
    coherence = coherence_report(scores)
    report = render_markdown(agreement, coherence)

    out = Path(args.output) if args.output else None
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        print(f"wrote {out}")
    else:
        print(report)

    # A cap violation means a judge overrode measured evidence: that is a bug.
    return 1 if coherence["violation_count"] else 0


def _cmd_page(args: argparse.Namespace) -> int:
    from .publish import build_page

    out = build_page(run=args.run, output=args.output)
    print(f"wrote {out}")
    return 0


def _cmd_review_page(args: argparse.Namespace) -> int:
    from .review_page import build_review_page

    out = build_review_page(output=args.output, api_base=args.api_base)
    print(f"wrote {out}")
    return 0


def _cmd_feed(args: argparse.Namespace) -> int:
    from .publish import build_feed

    out = build_feed(run=args.run, output=args.output, min_rqs=args.min_rqs, topics=args.topics)
    print(f"wrote {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="assay", description=__doc__)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="discover, fetch and score a corpus slice")
    r.add_argument("--days", type=int, default=7)
    r.add_argument("--limit", type=int, default=None)
    r.add_argument("--run", default="latest")
    r.add_argument("--no-links", action="store_true", help="skip network link checks")
    r.add_argument("--no-resume", action="store_true")
    r.set_defaults(func=_cmd_run, is_async=True)

    s = sub.add_parser("score", help="score a single article by URL or id")
    s.add_argument("target")
    s.add_argument("--json", action="store_true")
    s.add_argument("--no-links", action="store_true")
    s.add_argument("--no-cache", action="store_true")
    s.set_defaults(func=_cmd_score, is_async=True)

    rv = sub.add_parser(
        "review", help="score a draft or URL and recommend what would improve it"
    )
    rv.add_argument("target", help="a local markdown file, or a builder.aws.com URL / id")
    rv.add_argument("--json", action="store_true")
    rv.add_argument("--no-links", action="store_true")
    rv.add_argument("--no-cache", action="store_true")
    rv.add_argument("--author", default=None, help="author kind for credibility, e.g. hero")
    rv.set_defaults(func=_cmd_review, is_async=True)

    rep = sub.add_parser("report", help="summarise a scored run")
    rep.add_argument("--run", default="latest")
    rep.add_argument("--top", type=int, default=15)
    rep.set_defaults(func=_cmd_report, is_async=False)

    cal = sub.add_parser("calibrate", help="agreement with hand labels + coherence checks")
    cal.add_argument("--run", default="latest")
    cal.add_argument("--output", default=None)
    cal.set_defaults(func=_cmd_calibrate, is_async=False)

    pg = sub.add_parser("page", help="build the HTML leaderboard")
    pg.add_argument("--run", default="latest")
    pg.add_argument("--output", default=None)
    pg.set_defaults(func=_cmd_page, is_async=False)

    rp = sub.add_parser("review-page", help="build the author-facing improvement page")
    rp.add_argument("--output", default=None)
    rp.add_argument("--api-base", default="https://d1m5fcrjmmi9ue.cloudfront.net")
    rp.set_defaults(func=_cmd_review_page, is_async=False)

    fd = sub.add_parser("feed", help="build the filtered RSS feed")
    fd.add_argument("--run", default="latest")
    fd.add_argument("--output", default=None)
    fd.add_argument("--min-rqs", type=float, default=70.0)
    fd.add_argument("--topics", nargs="*", default=None)
    fd.set_defaults(func=_cmd_feed, is_async=False)

    args = p.parse_args(argv)
    _setup_logging(args.verbose)
    return asyncio.run(args.func(args)) if args.is_async else args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
