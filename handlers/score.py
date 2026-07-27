"""Score a batch of articles with the Strands fleet and persist the results."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from shouldiread.agents import ScoringFleet
from shouldiread.ingest import fetch
from shouldiread.store import all_scores, put_score
from shouldiread.tools.dedup import DuplicateIndex

log = logging.getLogger()
log.setLevel(logging.INFO)

CONCURRENCY = 4


async def _run(article_ids: list[str], check_links: bool) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(follow_redirects=True) as client:
        articles = await asyncio.gather(
            *(fetch(client, aid, use_cache=False) for aid in article_ids)
        )
    articles = [a for a in articles if a is not None]
    if not articles:
        return []

    # Seed the duplicate index from what is already scored, so content lifted
    # from an older article is still caught in an incremental run.
    index = DuplicateIndex()
    for prior in all_scores():
        md = (prior.get("signals") or {}).get("markdown_sample")
        if md:
            index.add(prior["article_id"], md, prior.get("title", ""), prior.get("author_alias", ""))
    for a in articles:
        index.add(a.article_id, a.markdown, a.title, a.author.alias)

    fleet = ScoringFleet()
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(article):
        async with sem:
            try:
                score = await fleet.score(article, index=index, check_links=check_links)
            except Exception as exc:
                log.exception("scoring failed for %s: %s", article.article_id, exc)
                return None
        payload = score.to_dict()
        put_score(payload)
        return payload

    return [s for s in await asyncio.gather(*(one(a) for a in articles)) if s]


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    article_ids = event.get("article_ids") or []
    if isinstance(article_ids, str):
        article_ids = [article_ids]
    if not article_ids:
        return {"scored": 0, "error": "no article_ids supplied"}

    scored = asyncio.run(_run(article_ids, bool(event.get("check_links", True))))
    log.info("scored %d of %d requested", len(scored), len(article_ids))
    return {
        "scored": len(scored),
        "requested": len(article_ids),
        "verdicts": {s["verdict"]: 1 for s in scored},
    }
