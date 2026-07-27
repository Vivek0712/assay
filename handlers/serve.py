"""Public read API behind CloudFront.

    GET /api/queue?topics=bedrock,eks&min_rqs=70&limit=20   ranked reading queue
    GET /api/score/{article_id}                             one full breakdown
    GET /api/stats                                          corpus statistics
    GET /scores.json                                        index for the extension
    GET /feed.xml                                           filtered Atom feed

Read-only by construction: nothing here writes, so a fully public endpoint has
no state to corrupt. Responses carry cache headers because CloudFront in front
of this is what keeps an open endpoint from becoming an open Lambda bill.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from shouldiread.preferences import Preferences
from shouldiread.publish import _public, _stats, render_feed
from shouldiread.store import all_scores, get_score

log = logging.getLogger()
log.setLevel(logging.INFO)

CACHE = "public, max-age=300, stale-while-revalidate=3600"
JSON_HEADERS = {"content-type": "application/json; charset=utf-8", "cache-control": CACHE}


def _respond(status: int, body: Any, headers: dict[str, str] | None = None) -> dict[str, Any]:
    is_text = isinstance(body, str)
    return {
        "statusCode": status,
        "headers": {
            **(headers or JSON_HEADERS),
            "access-control-allow-origin": "*",
        },
        "body": body if is_text else json.dumps(body, ensure_ascii=False),
    }


def _csv(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _prefs_from_query(q: dict[str, str]) -> Preferences:
    return Preferences(
        topics=_csv(q.get("topics")),
        exclude_topics=_csv(q.get("exclude")),
        min_rqs=float(q.get("min_rqs", 70)),
        author_kinds=_csv(q.get("author_kinds")),
        max_age_days=int(q.get("max_age_days", 7)),
        limit=int(q.get("limit", 25)),
    )


def _review(event: dict[str, Any], query: dict[str, str]) -> dict[str, Any]:
    """Score a submitted article and return recommendations.

    This is the one endpoint that costs money per request - it runs the full
    fleet - so it only accepts an already-published builder.aws.com article,
    never arbitrary pasted text. That bounds the work per call, lets the result
    be cached by article, and keeps a public endpoint from becoming a way to run
    a language model on anything at somebody else's expense.
    """
    import asyncio

    import httpx

    from shouldiread.advise import recommend
    from shouldiread.agents import ScoringFleet
    from shouldiread.ingest import article_id_from_url, fetch

    target = (query.get("url") or query.get("id") or "").strip()
    if not target:
        return _respond(400, {"error": "supply ?url= a builder.aws.com article"})

    article_id = article_id_from_url(target) or target
    if not re.fullmatch(r"[0-9A-Za-z]{10,40}", article_id):
        return _respond(400, {"error": "not a builder.aws.com article URL or id"})

    # Already scored: advise from the stored score, no model calls at all.
    existing = get_score(article_id)
    if existing and not query.get("fresh"):
        advice = recommend(existing)
        return _respond(200, {"title": existing.get("title"), "url": existing.get("url"),
                              "cached": True, **advice})

    async def run() -> dict[str, Any]:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            article = await fetch(client, article_id, use_cache=False)
        if article is None:
            return {"error": f"could not retrieve {article_id}"}
        score = await ScoringFleet().score(article, check_links=False)
        return {"title": article.title, "url": article.url, "cached": False,
                **recommend(score.to_dict())}

    result = asyncio.run(run())
    status = 404 if "error" in result else 200
    return _respond(status, result)


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    path = (event.get("rawPath") or event.get("path") or "/").rstrip("/") or "/"
    query = event.get("queryStringParameters") or {}

    try:
        if path in ("/api/review", "/review"):
            return _review(event, query)

        if path in ("/api/stats", "/stats"):
            return _respond(200, _stats(all_scores()))

        if path in ("/scores.json", "/api/scores"):
            scores = all_scores()
            return _respond(
                200,
                {
                    "stats": _stats(scores),
                    "scores": {s["article_id"]: _public(s) for s in scores},
                },
            )

        if path.startswith("/api/score/"):
            article_id = path.rsplit("/", 1)[-1]
            score = get_score(article_id)
            if not score:
                return _respond(404, {"error": f"{article_id} has not been scored"})
            return _respond(200, _public(score))

        if path in ("/api/queue", "/queue"):
            prefs = _prefs_from_query(query)
            kept = prefs.apply(all_scores())
            return _respond(
                200,
                {
                    "articles": [_public(s) for s in kept],
                    "returned": len(kept),
                    "filters": prefs.to_dict(),
                },
            )

        if path in ("/feed.xml", "/api/feed", "/rss"):
            prefs = _prefs_from_query(query)
            prefs.limit = min(prefs.limit, 100)
            return _respond(
                200,
                render_feed(all_scores(), prefs=prefs),
                {"content-type": "application/atom+xml; charset=utf-8", "cache-control": CACHE},
            )

        return _respond(
            404,
            {
                "error": "not found",
                "routes": ["/api/queue", "/api/score/{id}", "/api/review?url=", "/api/stats",
                           "/scores.json", "/feed.xml"],
            },
        )
    except Exception as exc:  # never leak a stack trace on a public endpoint
        log.exception("request failed for %s", path)
        return _respond(500, {"error": "internal error", "type": type(exc).__name__})
