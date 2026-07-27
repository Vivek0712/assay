"""Data model shared by ingest, tools, agents and the serving layer."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _ms_to_iso(ms: int | None) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


@dataclass
class Author:
    alias: str = ""
    preferred_name: str = ""
    is_aws_hero: bool = False
    is_community_builder: bool = False
    is_amazon_employee: bool = False
    bio: str = ""

    @property
    def kind(self) -> str:
        """Coarse author class, used for preference filtering."""
        if self.is_aws_hero:
            return "hero"
        if self.is_amazon_employee:
            return "amazonian"
        if self.is_community_builder:
            return "community_builder"
        return "community"

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> "Author":
        return cls(
            alias=d.get("alias", ""),
            preferred_name=d.get("preferredName", ""),
            is_aws_hero=bool(d.get("isAwsHero")),
            is_community_builder=bool(d.get("isCommunityBuilder")),
            is_amazon_employee=bool(d.get("isAmazonEmployee")),
            bio=d.get("bio", "") or "",
        )


@dataclass
class Article:
    """One Builder Center article, as ingested. `markdown` is the raw source."""

    article_id: str
    title: str
    uri: str
    markdown: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    locale: str = "en"
    author: Author = field(default_factory=Author)
    external_canonical_url: str | None = None
    likes: int = 0
    comments: int = 0
    created_at: str | None = None
    published_at: str | None = None

    @property
    def url(self) -> str:
        from .config import SITE

        return f"{SITE}{self.uri}" if self.uri.startswith("/") else self.uri

    @property
    def word_count(self) -> int:
        return len(self.markdown.split())

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> "Article":
        # The API returns articleId as "/content/<id>", but every consumer - the
        # cache filename, the DynamoDB key, the MCP lookup, the URLs the browser
        # extension parses out of hrefs - works with the bare id. Normalise here
        # so the two forms never diverge downstream.
        return cls(
            article_id=(d.get("articleId") or "").removeprefix("/content/"),
            title=d.get("title", "") or "",
            uri=d.get("uri", "") or "",
            markdown=d.get("markdownDescription", "") or "",
            description=d.get("description", "") or "",
            tags=list(d.get("tags") or []),
            locale=d.get("locale", "en") or "en",
            author=Author.from_api(d.get("author") or {}),
            external_canonical_url=d.get("externalCanonicalUrl") or None,
            likes=int(d.get("likesCount") or 0),
            comments=int(d.get("commentsCount") or 0),
            created_at=_ms_to_iso(d.get("createdAt")),
            published_at=_ms_to_iso(d.get("lastPublishedAt") or d.get("createdAt")),
        )

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["url"] = self.url
        return d


@dataclass
class Evidence:
    """One concrete, tool-derived fact backing a score. Never model prose."""

    tool: str
    label: str
    detail: str = ""
    weight_hint: str = "neutral"  # "positive" | "negative" | "neutral"

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class Dimension:
    """A scored dimension: 0-100 within the dimension, weighted into the RQS."""

    name: str
    score: float
    rationale: str = ""
    evidence: list[Evidence] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "score": round(self.score, 1),
            "rationale": self.rationale,
            "evidence": [e.to_dict() for e in self.evidence],
        }


@dataclass
class Score:
    """Final verdict for one article."""

    article_id: str
    title: str
    url: str
    author_alias: str
    author_kind: str
    tags: list[str]
    published_at: str | None
    rqs: float
    verdict: str
    headline: str  # the one-line brutal reason
    dimensions: list[Dimension] = field(default_factory=list)
    signals: dict[str, Any] = field(default_factory=dict)  # raw tool output
    scored_at: str | None = None
    model: str = ""

    def to_dict(self, redact: bool = False) -> dict[str, Any]:
        """`redact=True` strips author + title, per the attribution policy:
        name authors on READ, redact them on SKIP."""
        d = {
            "article_id": self.article_id,
            "title": self.title,
            "url": self.url,
            "author_alias": self.author_alias,
            "author_kind": self.author_kind,
            "tags": self.tags,
            "published_at": self.published_at,
            "rqs": round(self.rqs, 1),
            "verdict": self.verdict,
            "headline": self.headline,
            "dimensions": [x.to_dict() for x in self.dimensions],
            "signals": self.signals,
            "scored_at": self.scored_at,
            "model": self.model,
        }
        if redact:
            d["title"] = "[redacted]"
            d["url"] = ""
            d["author_alias"] = "[redacted]"
            d["redacted"] = True
        return d
