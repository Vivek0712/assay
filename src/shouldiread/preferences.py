"""Reader preferences.

Filtering is applied identically by every serving surface - the HTML page, the
RSS feed, the MCP tools and the browser extension all call `apply`. One
implementation means the extension can never disagree with the feed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .config import READ_THRESHOLD

VALID_AUTHOR_KINDS = {"hero", "amazonian", "community_builder", "community"}
VALID_FREQUENCIES = {"realtime", "hourly", "daily", "weekly"}


@dataclass
class Preferences:
    """What a given reader wants surfaced."""

    topics: list[str] = field(default_factory=list)
    """Tag allowlist. Empty means every topic."""

    exclude_topics: list[str] = field(default_factory=list)
    min_rqs: float = float(READ_THRESHOLD)
    author_kinds: list[str] = field(default_factory=list)
    """Empty means every kind of author."""

    max_age_days: int = 3
    include_cross_posts: bool = True
    locales: list[str] = field(default_factory=list)
    """Empty means every language."""

    frequency: str = "daily"
    limit: int = 25

    def __post_init__(self) -> None:
        self.topics = [t.strip().lower() for t in self.topics if t.strip()]
        self.exclude_topics = [t.strip().lower() for t in self.exclude_topics if t.strip()]
        self.author_kinds = [k for k in self.author_kinds if k in VALID_AUTHOR_KINDS]
        if self.frequency not in VALID_FREQUENCIES:
            self.frequency = "daily"
        self.min_rqs = max(0.0, min(100.0, float(self.min_rqs)))
        self.limit = max(1, min(500, int(self.limit)))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Preferences":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (d or {}).items() if k in known})

    # ------------------------------------------------------------------
    def matches(self, score: dict[str, Any]) -> bool:
        """Does one scored article satisfy these preferences?"""
        if score.get("rqs", 0) < self.min_rqs:
            return False

        tags = {t.lower() for t in score.get("tags") or []}
        if self.topics and not (tags & set(self.topics)):
            return False
        if self.exclude_topics and (tags & set(self.exclude_topics)):
            return False

        if self.author_kinds and score.get("author_kind") not in self.author_kinds:
            return False

        if not self.include_cross_posts:
            if (score.get("signals", {}).get("duplicates") or {}).get("is_cross_post"):
                return False

        if self.locales:
            locale = (score.get("signals", {}).get("locale") or "en").lower()
            if locale not in {l.lower() for l in self.locales}:
                return False

        if self.max_age_days:
            published = score.get("published_at")
            if published and _age_days(published) > self.max_age_days:
                return False

        return True

    def apply(self, scores: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Filter and rank. Highest RQS first, newest breaking ties."""
        kept = [s for s in scores if self.matches(s)]
        kept.sort(key=lambda s: (s.get("rqs", 0), s.get("published_at") or ""), reverse=True)
        return kept[: self.limit]


def _age_days(iso: str) -> float:
    from datetime import datetime, timezone

    try:
        ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() / 86400
