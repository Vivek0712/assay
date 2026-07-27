"""Outbound link verification and classification.

Two things worth knowing about an article's links: whether they resolve, and
what kind of place they point at. Citing AWS documentation, a spec, or a GitHub
repo is different from citing your own three other posts, and an article with no
outbound links at all has cited nothing whatsoever.

Dead links are weak evidence on their own - the web rots - but a high dead-link
rate in a post published last week is not rot.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

from ..config import REQUEST_TIMEOUT_S, USER_AGENT
from .markdown_tools import IMAGE_RE, LINK_RE, strip_code

PRIMARY_HOSTS = (
    "docs.aws.amazon.com",
    "aws.amazon.com",
    "awslabs.github.io",
    "boto3.amazonaws.com",
    "registry.terraform.io",
    "kubernetes.io",
    "datatracker.ietf.org",
    "www.rfc-editor.org",
    "arxiv.org",
)
CODE_HOSTS = ("github.com", "gitlab.com", "bitbucket.org", "pypi.org", "npmjs.com")
COMMUNITY_HOSTS = ("builder.aws.com", "community.aws", "dev.to", "medium.com", "hashnode")
SOCIAL_HOSTS = ("twitter.com", "x.com", "linkedin.com", "facebook.com", "youtube.com", "youtu.be")
IMAGE_CDN_HOSTS = ("media2.dev.to", "media.dev.to", "res.cloudinary.com", "imgur.com")

_CONCURRENCY = 8


def classify_host(url: str) -> str:
    host = (urlparse(url).netloc or "").lower().removeprefix("www.")
    if any(h in host for h in IMAGE_CDN_HOSTS):
        return "image_cdn"
    if any(host == h or host.endswith("." + h) or h in host for h in PRIMARY_HOSTS):
        return "primary"
    if any(h in host for h in CODE_HOSTS):
        return "code"
    if any(h in host for h in COMMUNITY_HOSTS):
        return "community"
    if any(h in host for h in SOCIAL_HOSTS):
        return "social"
    return "other"


@dataclass
class LinkResult:
    url: str
    category: str
    status: int | None = None
    ok: bool = False
    error: str = ""

    @property
    def is_dead(self) -> bool:
        """Reached the network and came back bad. Unreachable != dead."""
        return self.status is not None and not self.ok

    def to_dict(self) -> dict[str, Any]:
        return {"url": self.url, "category": self.category, "status": self.status, "ok": self.ok}


@dataclass
class LinkReport:
    total: int = 0
    checked: int = 0
    dead: int = 0
    by_category: dict[str, int] = field(default_factory=dict)
    self_citations: int = 0
    results: list[LinkResult] = field(default_factory=list)

    @property
    def dead_ratio(self) -> float:
        return self.dead / self.checked if self.checked else 0.0

    @property
    def primary_sources(self) -> int:
        return self.by_category.get("primary", 0) + self.by_category.get("code", 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "checked": self.checked,
            "dead": self.dead,
            "dead_ratio": round(self.dead_ratio, 3),
            "by_category": self.by_category,
            "primary_sources": self.primary_sources,
            "self_citations": self.self_citations,
            "dead_urls": [r.url for r in self.results if r.is_dead][:10],
        }


def extract_links(md: str, *, include_images: bool = False) -> list[str]:
    """Outbound http(s) links from prose. Image embeds excluded by default."""
    prose = strip_code(md)
    urls = [u for _, u in LINK_RE.findall(prose)]
    if include_images:
        urls += [u for _, u in IMAGE_RE.findall(md) if u.startswith("http")]
    seen, out = set(), []
    for u in urls:
        u = u.rstrip(".,;)")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


async def _probe(client: httpx.AsyncClient, url: str) -> LinkResult:
    res = LinkResult(url=url, category=classify_host(url))
    try:
        r = await client.head(url, timeout=REQUEST_TIMEOUT_S)
        # Plenty of servers refuse HEAD; retry those with a ranged GET.
        if r.status_code in (403, 405, 501):
            r = await client.get(url, timeout=REQUEST_TIMEOUT_S, headers={"Range": "bytes=0-2048"})
        res.status = r.status_code
        res.ok = r.status_code < 400
    except (httpx.TimeoutException, httpx.TransportError, httpx.InvalidURL) as exc:
        res.error = type(exc).__name__
    return res


async def verify_links(
    md: str, *, author_alias: str = "", check_network: bool = True
) -> LinkReport:
    """Classify every outbound link, and optionally verify each one resolves."""
    urls = extract_links(md)
    report = LinkReport(total=len(urls))

    for u in urls:
        cat = classify_host(u)
        report.by_category[cat] = report.by_category.get(cat, 0) + 1
        if author_alias and re.search(rf"/{re.escape(author_alias)}\b", u, re.I):
            report.self_citations += 1

    if not check_network or not urls:
        report.results = [LinkResult(url=u, category=classify_host(u)) for u in urls]
        return report

    sem = asyncio.Semaphore(_CONCURRENCY)
    async with httpx.AsyncClient(
        follow_redirects=True, headers={"User-Agent": USER_AGENT}, verify=True
    ) as client:

        async def one(u: str) -> LinkResult:
            async with sem:
                return await _probe(client, u)

        report.results = list(await asyncio.gather(*(one(u) for u in urls)))

    report.checked = sum(1 for r in report.results if r.status is not None)
    report.dead = sum(1 for r in report.results if r.is_dead)
    return report
