"""Hourly ingest: find newly published articles and hand them to the scorer.

Runs off the Atom feed rather than the sitemaps. The feed is the site's own
delta channel, refreshed hourly, and 30 entries comfortably covers an hour of
publishing - the daily rate across the whole platform is around 40.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import boto3

from shouldiread.ingest import fetch_many, from_atom
from shouldiread.store import put_raw, scored_ids

log = logging.getLogger()
log.setLevel(logging.INFO)

SCORE_FUNCTION = os.environ.get("SCORE_FUNCTION")
BATCH = 10  # articles per score invocation


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    force = bool(event.get("force"))
    discovered = from_atom()
    log.info("atom feed returned %d entries", len(discovered))

    known = set() if force else scored_ids()
    fresh = [d for d in discovered if d.article_id not in known]
    log.info("%d are new", len(fresh))

    if not fresh:
        return {"discovered": len(discovered), "new": 0, "dispatched": 0}

    articles = asyncio.run(fetch_many([d.article_id for d in fresh], use_cache=False))
    for a in articles:
        put_raw(a.article_id, a.to_dict())

    dispatched = 0
    if SCORE_FUNCTION:
        lam = boto3.client("lambda")
        ids = [a.article_id for a in articles]
        for i in range(0, len(ids), BATCH):
            lam.invoke(
                FunctionName=SCORE_FUNCTION,
                InvocationType="Event",  # fire and forget; scoring is slow
                Payload=json.dumps({"article_ids": ids[i : i + BATCH]}).encode(),
            )
            dispatched += 1

    return {
        "discovered": len(discovered),
        "new": len(fresh),
        "fetched": len(articles),
        "dispatched": dispatched,
    }
