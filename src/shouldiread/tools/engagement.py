"""Engagement analysis - deliberately asymmetric.

Likes, views and comments are the metrics Builder Center shows you, and they are
the metrics that are trivially bought. So this tool is wired one way only:
engagement can never raise an article's score. It exists solely to flag the
inverse pattern - a thin post carrying implausible engagement - which is exactly
what boosted content looks like and exactly what the visible UI rewards.

If you take one thing from this module: high likes is not evidence of quality.
High likes on a 400-word post with no code is evidence of something else.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class EngagementReport:
    likes: int = 0
    comments: int = 0
    substance_score: float = 0.0  # 0-1, from structural signals only
    engagement_per_100_words: float = 0.0
    suspicious: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["substance_score"] = round(self.substance_score, 3)
        d["engagement_per_100_words"] = round(self.engagement_per_100_words, 3)
        return d


def _substance(words: int, code_blocks: int, code_loc: int, links: int, images: int) -> float:
    """Crude 0-1 substance proxy built only from things that are hard to fake."""
    score = 0.0
    score += min(words / 1500, 1.0) * 0.25
    score += min(code_blocks / 5, 1.0) * 0.30
    score += min(code_loc / 120, 1.0) * 0.20
    score += min(links / 6, 1.0) * 0.15
    score += min(images / 4, 1.0) * 0.10
    return min(score, 1.0)


def engagement_ratio(
    *,
    likes: int,
    comments: int,
    words: int,
    code_blocks: int = 0,
    code_loc: int = 0,
    links: int = 0,
    images: int = 0,
) -> EngagementReport:
    """Flag engagement that is out of proportion to substance.

    Never returns a positive signal - only `suspicious` or not.
    """
    substance = _substance(words, code_blocks, code_loc, links, images)
    total = likes + comments * 2  # a comment costs more to fake than a like
    per_100 = (total * 100.0 / words) if words else 0.0

    report = EngagementReport(
        likes=likes,
        comments=comments,
        substance_score=substance,
        engagement_per_100_words=per_100,
    )

    # Thresholds are intentionally forgiving: this flag accuses someone of being
    # boosted, so it should fire only on the clear cases.
    if total >= 15 and substance < 0.25:
        report.suspicious = True
        report.reason = (
            f"{likes} likes / {comments} comments on a post with very little "
            f"substance (score {substance:.2f})"
        )
    elif total >= 40 and per_100 > 4.0 and substance < 0.4:
        report.suspicious = True
        report.reason = (
            f"engagement density {per_100:.1f} per 100 words is far above what "
            f"the content supports"
        )

    return report
