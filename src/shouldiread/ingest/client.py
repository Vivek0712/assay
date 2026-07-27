"""Article body fetcher.

Article pages on builder.aws.com are a 2.5 KB SPA shell with no server-rendered
body, so HTML scraping yields nothing. The site's own web app loads content from
`/cs/v2/articles?articleId=/content/<id>` with a fixed anonymous session token.
Same request, same data the public sees, and it hands back raw markdown - which
is far better for scoring than rendered HTML.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from pathlib import Path
from urllib.parse import quote

import httpx

from ..config import (
    ARTICLE_API,
    CACHE_DIR,
    MAX_CONCURRENCY,
    MAX_RETRIES,
    REQUEST_DELAY_S,
    REQUEST_HEADERS,
    REQUEST_TIMEOUT_S,
)
from ..models import Article

log = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}


def _cache_path(article_id: str) -> Path:
    return CACHE_DIR / f"{article_id}.json"


def load_cached(article_id: str) -> dict | None:
    p = _cache_path(article_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _save(article_id: str, payload: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(article_id).write_text(json.dumps(payload, ensure_ascii=False))


async def fetch_raw(
    client: httpx.AsyncClient, article_id: str, *, use_cache: bool = True
) -> dict | None:
    """Raw API payload for one article, with retry/backoff. None if unavailable."""
    if use_cache:
        cached = load_cached(article_id)
        if cached is not None:
            return cached

    url = f"{ARTICLE_API}?articleId={quote('/content/' + article_id, safe='')}"

    for attempt in range(MAX_RETRIES):
        try:
            r = await client.get(
                url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT_S
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            log.debug("transport error on %s: %s", article_id, exc)
            await asyncio.sleep(2**attempt + random.random())
            continue

        if r.status_code == 200:
            payload = r.json()
            if use_cache:
                _save(article_id, payload)
            return payload

        if r.status_code in RETRY_STATUS:
            # Honour Retry-After when the service sends one.
            wait = float(r.headers.get("Retry-After") or 0) or 2**attempt
            log.debug("status %s on %s, backing off %.1fs", r.status_code, article_id, wait)
            await asyncio.sleep(wait + random.random())
            continue

        log.warning("giving up on %s: HTTP %s", article_id, r.status_code)
        return None

    log.warning("giving up on %s after %d attempts", article_id, MAX_RETRIES)
    return None


async def fetch(
    client: httpx.AsyncClient, article_id: str, *, use_cache: bool = True
) -> Article | None:
    payload = await fetch_raw(client, article_id, use_cache=use_cache)
    if not payload or not payload.get("markdownDescription"):
        return None
    return Article.from_api(payload)


async def fetch_many(
    article_ids: list[str],
    *,
    use_cache: bool = True,
    concurrency: int = MAX_CONCURRENCY,
    progress: bool = False,
) -> list[Article]:
    """Fetch many articles politely: bounded concurrency plus a small delay.

    The site is a community resource. Concurrency is capped low on purpose.
    """
    sem = asyncio.Semaphore(concurrency)
    done = 0
    total = len(article_ids)

    async with httpx.AsyncClient(follow_redirects=True) as client:

        async def one(aid: str) -> Article | None:
            nonlocal done
            async with sem:
                art = await fetch(client, aid, use_cache=use_cache)
                await asyncio.sleep(REQUEST_DELAY_S)
            done += 1
            if progress and done % 25 == 0:
                print(f"  fetched {done}/{total}", flush=True)
            return art

        results = await asyncio.gather(*(one(a) for a in article_ids))

    return [a for a in results if a is not None]
