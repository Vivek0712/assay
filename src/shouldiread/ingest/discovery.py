"""Article discovery.

Two complementary sources, both public and both cheap:

* the Atom feed at /rss  -> the 30 newest articles, refreshed hourly (delta)
* /sitemaps/sitemap.xml  -> 58 monthly article sitemaps back to 2022 (backfill)

robots.txt disallows only /profile/, so both are fair game.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from ..config import ATOM_FEED, REQUEST_HEADERS, REQUEST_TIMEOUT_S, SITEMAP_INDEX

ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}
SITEMAP_NS = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}

# /content/<articleId>/<slug>
_CONTENT_RE = re.compile(r"/content/([0-9A-Za-z]+)(?:/|$)")

_MONTH_SITEMAP_RE = re.compile(r"/sitemaps/articles/(\d{4})-(\d{1,2})\.xml$")


@dataclass(frozen=True)
class Discovered:
    """A discovered article, before its body is fetched."""

    article_id: str
    url: str
    last_modified: str | None = None
    title: str | None = None
    summary: str | None = None


def article_id_from_url(url: str) -> str | None:
    m = _CONTENT_RE.search(url)
    return m.group(1) if m else None


def _get(client: httpx.Client, url: str) -> str:
    r = client.get(url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT_S)
    r.raise_for_status()
    return r.text


def from_atom(client: httpx.Client | None = None) -> list[Discovered]:
    """The 30 newest published articles. This is the hourly delta source."""
    own = client is None
    client = client or httpx.Client(follow_redirects=True)
    try:
        root = ET.fromstring(_get(client, ATOM_FEED))
    finally:
        if own:
            client.close()

    out: list[Discovered] = []
    for entry in root.findall("a:entry", ATOM_NS):
        link = entry.find("a:link", ATOM_NS)
        url = (link.get("href") if link is not None else "") or ""
        aid = article_id_from_url(url) or article_id_from_url(
            (entry.findtext("a:id", "", ATOM_NS) or "")
        )
        if not aid:
            continue
        out.append(
            Discovered(
                article_id=aid,
                url=url,
                last_modified=entry.findtext("a:updated", None, ATOM_NS),
                title=entry.findtext("a:title", None, ATOM_NS),
                summary=entry.findtext("a:summary", None, ATOM_NS),
            )
        )
    return out


def monthly_sitemaps(client: httpx.Client | None = None) -> list[tuple[int, int, str]]:
    """Every per-month article sitemap as (year, month, url), newest last."""
    own = client is None
    client = client or httpx.Client(follow_redirects=True)
    try:
        root = ET.fromstring(_get(client, SITEMAP_INDEX))
    finally:
        if own:
            client.close()

    found: list[tuple[int, int, str]] = []
    for loc in root.findall(".//s:loc", SITEMAP_NS):
        url = (loc.text or "").strip()
        m = _MONTH_SITEMAP_RE.search(url)
        if m:
            found.append((int(m.group(1)), int(m.group(2)), url))
    return sorted(found)


def from_sitemap(url: str, client: httpx.Client | None = None) -> list[Discovered]:
    """All articles listed in one monthly sitemap."""
    own = client is None
    client = client or httpx.Client(follow_redirects=True)
    try:
        root = ET.fromstring(_get(client, url))
    finally:
        if own:
            client.close()

    out: list[Discovered] = []
    for node in root.findall("s:url", SITEMAP_NS):
        loc = (node.findtext("s:loc", "", SITEMAP_NS) or "").strip()
        aid = article_id_from_url(loc)
        if not aid:
            continue
        out.append(
            Discovered(
                article_id=aid,
                url=loc,
                last_modified=(node.findtext("s:lastmod", None, SITEMAP_NS) or None),
            )
        )
    return out


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def recent(days: int = 30, client: httpx.Client | None = None) -> list[Discovered]:
    """Everything modified in the last `days`, deduped, newest first.

    Walks only the monthly sitemaps that can overlap the window, then filters on
    lastmod. Fetching 2-3 sitemaps beats fetching all 58.
    """
    own = client is None
    client = client or httpx.Client(follow_redirects=True)
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        wanted = {
            (d.year, d.month)
            for d in (cutoff + timedelta(days=i) for i in range(days + 1))
        }
        seen: dict[str, Discovered] = {}
        for year, month, url in monthly_sitemaps(client):
            if (year, month) not in wanted:
                continue
            for d in from_sitemap(url, client):
                ts = _parse_iso(d.last_modified)
                if ts and ts < cutoff:
                    continue
                seen[d.article_id] = d
        # The Atom feed can carry items the sitemap has not picked up yet.
        for d in from_atom(client):
            seen.setdefault(d.article_id, d)
    finally:
        if own:
            client.close()

    return sorted(
        seen.values(),
        key=lambda d: d.last_modified or "",
        reverse=True,
    )
