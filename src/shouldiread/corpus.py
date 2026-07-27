"""Corpus-scale scoring: discover, fetch, score, persist.

Scoring is bounded-concurrent because every article costs several Bedrock calls.
Results are written incrementally so a long run can be interrupted and resumed
without losing what it already paid for.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .agents import ScoringFleet
from .config import SCORES_DIR
from .ingest import fetch_many, recent
from .models import Article, Score
from .tools.dedup import DuplicateIndex

log = logging.getLogger(__name__)

# Each article fans out to up to six Bedrock calls, four of them concurrent, so
# the real request concurrency is roughly 4x this. Adaptive retry absorbs the
# throttling that follows; raise it for a backfill, lower it if you start seeing
# sustained ThrottlingException in the logs.
SCORE_CONCURRENCY = int(os.environ.get("SIR_SCORE_CONCURRENCY", "6"))


def _score_path(run: str) -> Path:
    return SCORES_DIR / f"{run}.jsonl"


def load_scores(run: str = "latest") -> list[dict[str, Any]]:
    path = _score_path(run)
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def already_scored(run: str = "latest") -> set[str]:
    return {s["article_id"] for s in load_scores(run)}


def build_index(articles: Iterable[Article]) -> DuplicateIndex:
    """Index the whole corpus first so duplicate detection sees every article."""
    idx = DuplicateIndex()
    for a in articles:
        idx.add(a.article_id, a.markdown, a.title, a.author.alias)
    return idx


async def score_articles(
    articles: list[Article],
    *,
    run: str = "latest",
    resume: bool = True,
    check_links: bool = True,
    concurrency: int = SCORE_CONCURRENCY,
    fleet: ScoringFleet | None = None,
    progress: bool = True,
) -> list[Score]:
    """Score a list of articles, appending each result as it completes."""
    SCORES_DIR.mkdir(parents=True, exist_ok=True)
    path = _score_path(run)

    done = already_scored(run) if resume else set()
    if resume and done:
        log.info("resuming run %r: %d already scored", run, len(done))
    todo = [a for a in articles if a.article_id not in done]

    index = build_index(articles)
    fleet = fleet or ScoringFleet()
    sem = asyncio.Semaphore(concurrency)
    write_lock = asyncio.Lock()
    results: list[Score] = []
    completed = 0

    async def one(article: Article) -> Score | None:
        nonlocal completed
        async with sem:
            try:
                score = await fleet.score(article, index=index, check_links=check_links)
            except Exception as exc:
                log.error("scoring failed for %s: %s", article.article_id, exc)
                return None
        async with write_lock:
            with path.open("a") as fh:
                fh.write(json.dumps(score.to_dict(), ensure_ascii=False) + "\n")
            completed += 1
            if progress and completed % 10 == 0:
                print(f"  scored {completed}/{len(todo)}", flush=True)
        return score

    for score in await asyncio.gather(*(one(a) for a in todo)):
        if score is not None:
            results.append(score)

    return results


async def run_corpus(
    *,
    days: int = 3,
    limit: int | None = None,
    run: str = "latest",
    check_links: bool = True,
    resume: bool = True,
) -> dict[str, Any]:
    """Discover -> fetch -> score everything published in the last `days`."""
    started = datetime.now(timezone.utc)
    print(f"discovering articles from the last {days} days ...", flush=True)
    discovered = recent(days=days)
    if limit:
        discovered = discovered[:limit]
    print(f"  {len(discovered)} articles discovered", flush=True)

    print("fetching bodies ...", flush=True)
    articles = await fetch_many([d.article_id for d in discovered], progress=True)
    print(f"  {len(articles)} bodies retrieved", flush=True)

    print("scoring ...", flush=True)
    scores = await score_articles(
        articles, run=run, resume=resume, check_links=check_links
    )

    all_scores = load_scores(run)
    verdicts: dict[str, int] = {}
    for s in all_scores:
        verdicts[s["verdict"]] = verdicts.get(s["verdict"], 0) + 1

    return {
        "run": run,
        "days": days,
        "discovered": len(discovered),
        "fetched": len(articles),
        "scored_this_pass": len(scores),
        "scored_total": len(all_scores),
        "verdicts": verdicts,
        "started": started.isoformat(),
        "finished": datetime.now(timezone.utc).isoformat(),
    }
