#!/usr/bin/env python3
"""Fill the blog's statistics from a scored run.

The post argues that numbers in technical writing should be traceable to
something that was actually run. Hand-typing them would be the wrong way to
make that argument, so the placeholders are substituted from the corpus.

    python scripts/update_blog_stats.py --run week
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from shouldiread.corpus import load_scores  # noqa: E402
from shouldiread.publish import _stats  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", default="week")
    ap.add_argument("--post", default="blog/post.md")
    args = ap.parse_args()

    scores = load_scores(args.run)
    if not scores:
        print(f"no scores for run {args.run!r}", file=sys.stderr)
        return 1

    st = _stats(scores)
    with_output = [s for s in scores if (s.get("signals") or {}).get("code", {}).get("output_blocks", 0) > 0]
    without = [s for s in scores if (s.get("signals") or {}).get("code", {}).get("output_blocks", 0) == 0]

    import statistics

    def med(rows):
        return round(statistics.median([r["rqs"] for r in rows]), 1) if rows else None

    subs = {
        "PLACEHOLDER_TOTAL": str(st["total"]),
        "PLACEHOLDER_NO_CODE": f"{st['no_code_pct']}%",
        "PLACEHOLDER_NO_LINKS": f"{st['no_links_pct']}%",
        "PLACEHOLDER_OUTPUT": f"{st['with_output_pct']}%",
        "PLACEHOLDER_READ": f"{st['read_pct']}%",
        "PLACEHOLDER_MEDIAN": str(st["median_rqs"]),
        "PLACEHOLDER_MED_WITH_OUTPUT": str(med(with_output)),
        "PLACEHOLDER_MED_WITHOUT_OUTPUT": str(med(without)),
    }

    path = Path(args.post)
    text = path.read_text()
    replaced = 0
    for key, value in subs.items():
        if key in text:
            replaced += text.count(key)
            text = text.replace(key, value)
    path.write_text(text)

    print(f"run {args.run!r}: {st['total']} articles")
    for k, v in subs.items():
        print(f"  {k:32} {v}")
    print(f"\nsubstituted {replaced} placeholder(s) in {path}")
    if remaining := [k for k in subs if k in path.read_text()]:
        print(f"still unresolved: {remaining}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
