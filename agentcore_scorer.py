"""AgentCore Runtime entrypoint for the scoring fleet.

AgentCore's HTTP protocol expects an /invocations endpoint that takes a JSON
payload and a /ping health check. This wraps the same ScoringFleet the CLI uses -
there is no separate cloud implementation to drift out of sync.

    POST /invocations  {"url": "https://builder.aws.com/content/..."}
    POST /invocations  {"article_id": "3H40...", "check_links": false}
    POST /invocations  {"queue": true, "topics": ["bedrock"], "min_rqs": 70}
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from shouldiread.agents import ScoringFleet
from shouldiread.ingest import article_id_from_url, fetch
from shouldiread.preferences import Preferences
from shouldiread.store import all_scores, get_score, put_score

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("shouldiread.agentcore")

_fleet: ScoringFleet | None = None


def fleet() -> ScoringFleet:
    global _fleet
    if _fleet is None:
        _fleet = ScoringFleet()
    return _fleet


async def ping(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "healthy"})


async def invocations(request: Request) -> JSONResponse:
    try:
        payload: dict[str, Any] = await request.json()
    except Exception:
        return JSONResponse({"error": "body must be JSON"}, status_code=400)

    # Reading queue: filter what has already been scored.
    if payload.get("queue"):
        prefs = Preferences(
            topics=payload.get("topics") or [],
            min_rqs=float(payload.get("min_rqs", 70)),
            limit=int(payload.get("limit", 10)),
        )
        return JSONResponse({"articles": prefs.apply(all_scores())})

    target = payload.get("url") or payload.get("article_id") or ""
    article_id = article_id_from_url(target) or target.strip()
    if not article_id:
        return JSONResponse(
            {"error": "supply url, article_id, or queue=true"}, status_code=400
        )

    if not payload.get("force"):
        cached = get_score(article_id)
        if cached:
            return JSONResponse({**cached, "cached": True})

    async with httpx.AsyncClient(follow_redirects=True) as client:
        article = await fetch(client, article_id, use_cache=False)
    if article is None:
        return JSONResponse({"error": f"could not fetch {article_id}"}, status_code=404)

    score = await fleet().score(article, check_links=bool(payload.get("check_links", True)))
    result = score.to_dict()
    put_score(result)
    return JSONResponse(result)


app = Starlette(
    routes=[
        Route("/ping", ping, methods=["GET"]),
        Route("/invocations", invocations, methods=["POST"]),
    ]
)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
