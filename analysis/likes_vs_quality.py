#!/usr/bin/env python3
"""Do likes predict measured quality?

The assumption behind this whole project is that engagement is a bad proxy for
whether an article is worth reading. That is an assumption, so it gets tested
rather than asserted.

    python analysis/likes_vs_quality.py --run week
"""

from __future__ import annotations

import argparse
import math
import statistics
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from shouldiread.corpus import load_scores  # noqa: E402


def signal(row: dict[str, Any], *path: str, default: Any = 0) -> Any:
    cur: Any = row.get("signals") or {}
    for key in path:
        cur = (cur or {}).get(key) if isinstance(cur, dict) else None
    return default if cur is None else cur


def pearson(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 3:
        return None
    mean_x = statistics.mean(x for x, _ in pairs)
    mean_y = statistics.mean(y for _, y in pairs)
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in pairs)
    denominator = math.sqrt(
        sum((x - mean_x) ** 2 for x, _ in pairs)
        * sum((y - mean_y) ** 2 for _, y in pairs)
    )
    return numerator / denominator if denominator else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", default="week")
    ap.add_argument("--top", type=int, default=8)
    args = ap.parse_args()

    scores = load_scores(args.run)
    if not scores:
        print(f"no scores for run {args.run!r}", file=sys.stderr)
        return 1

    pairs = [(float(signal(s, "engagement", "likes")), float(s["rqs"])) for s in scores]
    r = pearson(pairs)
    print(f"Pearson r(likes, RQS) = {r:.3f}  (n={len(pairs)})")

    print("\nmost-liked articles and what they scored:")
    for s in sorted(scores, key=lambda s: -signal(s, "engagement", "likes"))[: args.top]:
        print(
            f"  {signal(s, 'engagement', 'likes'):4} likes -> "
            f"RQS {s['rqs']:5.1f} {s['verdict']}"
        )

    flagged = [s for s in scores if signal(s, "engagement", "suspicious")]
    print(
        f"\nengagement disproportionate to substance: {len(flagged)} of {len(scores)}"
    )

    # The comparison that matters: does the top of the like ranking look
    # anything like the top of the quality ranking?
    by_likes = {s["article_id"] for s in sorted(scores, key=lambda s: -signal(s, "engagement", "likes"))[:20]}
    by_rqs = {s["article_id"] for s in sorted(scores, key=lambda s: -s["rqs"])[:20]}
    print(f"overlap between top-20 by likes and top-20 by RQS: {len(by_likes & by_rqs)} articles")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
