"""Run the whole deterministic tool belt over one article.

This is the layer between raw markdown and the agents. Everything here is exact;
the agents receive these numbers as facts and are told not to contradict them.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .models import Article
from .tools import (
    aws_footprint,
    check_aws_api_names,
    find_artifacts,
    cross_post_check,
    engagement_ratio,
    structure_stats,
    validate_code,
    verify_links,
)
from .tools.dedup import DuplicateIndex


async def analyze(
    article: Article,
    *,
    index: DuplicateIndex | None = None,
    check_links: bool = True,
) -> dict[str, Any]:
    """All deterministic signals for one article, as a plain dict."""
    stats = structure_stats(article.markdown)
    code = validate_code(article.markdown)
    apis = check_aws_api_names(article.markdown)
    dupes = cross_post_check(
        article.markdown,
        article_id=article.article_id,
        author_alias=article.author.alias,
        external_canonical_url=article.external_canonical_url,
        index=index,
    )
    links = await verify_links(
        article.markdown,
        author_alias=article.author.alias,
        check_network=check_links,
    )
    artifacts = find_artifacts(
        article.markdown,
        author_alias=article.author.alias,
        hero_image_url=article.hero_image_url,
    )
    footprint = aws_footprint(article.markdown)
    engagement = engagement_ratio(
        likes=article.likes,
        comments=article.comments,
        words=stats.words,
        code_blocks=stats.code_blocks,
        code_loc=stats.code_loc,
        links=stats.links,
        images=stats.images,
    )

    return {
        "structure": stats.to_dict(),
        "code": code.to_dict(),
        "aws_apis": apis.to_dict(),
        "links": links.to_dict(),
        "duplicates": dupes.to_dict(),
        "artifacts": artifacts.to_dict(),
        "aws_footprint": footprint.to_dict(),
        "engagement": engagement.to_dict(),
    }


def analyze_sync(article: Article, **kw: Any) -> dict[str, Any]:
    return asyncio.run(analyze(article, **kw))


# --------------------------------------------------------------------------
# Cheap pre-verdict, used to gate expensive model calls and to sanity-check
# the model's own scoring. Structure only - no model involved.
# --------------------------------------------------------------------------
def heuristic_floor(signals: dict[str, Any]) -> dict[str, Any]:
    """A structural read on an article, independent of any model.

    Not the final score. It exists to (a) skip the deep judge on content that
    is obviously empty, and (b) catch a model that scores an article wildly
    out of line with what is measurably on the page.
    """
    s = signals["structure"]
    c = signals["code"]
    a = signals["aws_apis"]
    l = signals["links"]
    d = signals["duplicates"]

    # Screenshots are frequently written as plain links to an image CDN rather
    # than as markdown image embeds, so count both.
    effective_images = s["images"] + l["by_category"].get("image_cdn", 0)

    # Artefacts that are expensive to produce without having run something.
    # Deliberately excludes `measurements`: quoting "up to 99.99% availability"
    # from a docs page is not evidence of having done anything, and treating it
    # as such let prose-only posts masquerade as hands-on write-ups.
    art = signals.get("artifacts") or {}
    hard_evidence = (
        s["terminal_evidence"] > 0
        or c["output_blocks"] > 0
        or effective_images >= 2
        # A linked repository, package or demo is durable, inspectable proof the
        # work exists - the council's unanimous correction to the first rubric.
        or bool(art.get("has_durable_artifact"))
    )

    reasons: list[str] = []
    if s["words"] < 150:
        reasons.append("under 150 words")
    if s["code_blocks"] == 0 and s["words"] > 800:
        reasons.append("long-form with zero code")
    if s["links"] == 0:
        reasons.append("no outbound sources")
    if a["total_invalid"] > 0:
        reasons.append(f"{a['total_invalid']} nonexistent AWS API reference(s)")
    if c["checked"] and c["pass_rate"] < 0.5:
        reasons.append("most code blocks do not parse")
    if s["placeholder_density"] > 25:
        reasons.append("code is mostly placeholders")
    if d["has_foreign_duplicate"]:
        reasons.append("near-duplicate of another author's post")
    if l["checked"] and l["dead_ratio"] > 0.4:
        reasons.append("most links are dead")
    if s["bold_pseudo_headings"] > 3 and s["atx_headings"] == 0:
        reasons.append("bold-text pseudo-headings throughout")

    positives: list[str] = []
    if s["terminal_evidence"] > 0:
        positives.append(f"{s['terminal_evidence']} terminal/error artefact(s)")
    if c["output_blocks"] > 0:
        positives.append(f"{c['output_blocks']} pasted terminal session(s)")
    if effective_images >= 2:
        positives.append(f"{effective_images} screenshots/diagrams")
    if c["passed"] >= 3:
        positives.append(f"{c['passed']} code blocks parse cleanly")
    if c["complete_files"] >= 1:
        positives.append("includes complete runnable files")
    if l["primary_sources"] >= 3:
        positives.append(f"{l['primary_sources']} primary sources cited")
    if s["measurements"] >= 3:
        positives.append("reports concrete numbers")
    if art.get("is_authors_own"):
        positives.append("links the author's own repository")
    elif art.get("has_durable_artifact"):
        positives.append("links a working repository, package or demo")
    if a["total_checked"] >= 3 and a["total_invalid"] == 0:
        positives.append(f"{a['total_checked']} AWS API references all valid")

    # Narrow on purpose. This gate skips the model entirely, so it must only
    # catch content with nothing whatsoever to judge. A prose-only experience
    # report can still be worth reading - that call belongs to the model, not
    # to a word count.
    trivially_empty = s["words"] < 200 or (
        s["words"] < 400
        and s["code_blocks"] == 0
        and s["links"] == 0
        and not hard_evidence
    )

    return {
        "trivially_empty": trivially_empty,
        "hard_evidence": hard_evidence,
        "negatives": reasons,
        "positives": positives,
    }
