"""MCP server exposing the triage as tools.

Runs two ways from one definition:

    python -m shouldiread.mcp_server                 stdio, for local clients
    python -m shouldiread.mcp_server --http          streamable HTTP on :8000/mcp,
                                                     the shape AgentCore Runtime wants

Tools are written for an assistant that is answering "what should I read?" - they
return compact structured results, not walls of JSON.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from .config import CORPUS_DIR, SITE
from .preferences import Preferences

mcp = FastMCP(
    name="shouldiread",
    instructions=(
        "Triage for AWS Builder Center articles. Every article carries a Read-Quality "
        "Score (RQS, 0-100) and a verdict of READ, SKIM or SKIP, derived from measurable "
        "evidence that the author actually ran what they wrote about - pasted terminal "
        "output, code that parses, AWS APIs that exist, sources that resolve. Likes, "
        "views and comments are deliberately excluded because they are trivially bought. "
        "Use get_reading_queue for recommendations, score_url to judge a specific article, "
        "and explain_score when the user asks why something scored the way it did."
    ),
)

PREFS_PATH = CORPUS_DIR / "preferences.json"
_RUN = os.environ.get("SIR_RUN", "latest")
_SUBSCRIBER = os.environ.get("SIR_SUBSCRIBER", "default")


def _scores() -> list[dict[str, Any]]:
    """The scored corpus.

    Reads DynamoDB when the table is configured (deployed on AgentCore or
    Lambda) and the local JSONL otherwise, so the same tool code serves both.
    """
    from . import store

    if store.SCORES_TABLE:
        return store.all_scores()
    from .corpus import load_scores

    return load_scores(_RUN)


# --------------------------------------------------------------------------
def _load_prefs() -> Preferences:
    from . import store

    if store.PREFS_TABLE:
        return store.get_preferences(_SUBSCRIBER)
    if PREFS_PATH.exists():
        try:
            return Preferences.from_dict(json.loads(PREFS_PATH.read_text()))
        except (json.JSONDecodeError, OSError, TypeError):
            pass
    return Preferences()


def _save_prefs(prefs: Preferences) -> None:
    from . import store

    if store.PREFS_TABLE:
        store.put_preferences(_SUBSCRIBER, prefs)
        return
    PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PREFS_PATH.write_text(json.dumps(prefs.to_dict(), indent=2))


def _brief(s: dict[str, Any]) -> dict[str, Any]:
    """Compact form: what a model needs to make a recommendation, nothing more."""
    redacted = s.get("verdict") == "SKIP"
    return {
        "rqs": round(s.get("rqs", 0), 1),
        "verdict": s.get("verdict"),
        "headline": s.get("headline"),
        "title": "[withheld: low-scoring articles are not named]" if redacted else s.get("title"),
        "url": "" if redacted else s.get("url"),
        "author": "" if redacted else s.get("author_alias"),
        "author_kind": s.get("author_kind"),
        "tags": (s.get("tags") or [])[:6],
        "published_at": s.get("published_at"),
    }


# --------------------------------------------------------------------------
@mcp.tool()
def get_reading_queue(
    topics: list[str] | None = None,
    min_rqs: float | None = None,
    author_kinds: list[str] | None = None,
    max_age_days: int | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Ranked list of AWS Builder Center articles actually worth reading.

    Args:
        topics: tag allowlist, e.g. ["bedrock", "serverless"]. Omit for all topics.
        min_rqs: minimum Read-Quality Score. Defaults to the saved preference (70).
        author_kinds: filter to any of "hero", "amazonian", "community_builder", "community".
        max_age_days: only articles published within this many days.
        limit: how many to return.
    """
    prefs = _load_prefs()
    if topics is not None:
        prefs.topics = [t.lower() for t in topics]
    if min_rqs is not None:
        prefs.min_rqs = min_rqs
    if author_kinds is not None:
        prefs.author_kinds = author_kinds
    if max_age_days is not None:
        prefs.max_age_days = max_age_days
    prefs.limit = max(1, min(50, limit))

    scores = _scores()
    if not scores:
        return {"error": "no scored corpus available yet", "articles": []}

    kept = prefs.apply(scores)
    return {
        "articles": [_brief(s) for s in kept],
        "returned": len(kept),
        "corpus_size": len(scores),
        "filters": {
            "topics": prefs.topics or "all",
            "min_rqs": prefs.min_rqs,
            "author_kinds": prefs.author_kinds or "all",
            "max_age_days": prefs.max_age_days,
        },
    }


@mcp.tool()
async def score_url(url: str, check_links: bool = True) -> dict[str, Any]:
    """Score any AWS Builder Center article on demand, live.

    Fetches the article and runs the full multi-agent review. Use this for an
    article that is not in the scored corpus yet.

    Args:
        url: a builder.aws.com article URL, or the bare article id.
        check_links: verify that outbound links resolve. Slower but more accurate.
    """
    from .agents import ScoringFleet
    from .ingest import article_id_from_url, fetch

    aid = article_id_from_url(url) or url.strip()
    if not aid:
        return {"error": f"could not parse an article id out of {url!r}"}

    async with httpx.AsyncClient(follow_redirects=True) as client:
        article = await fetch(client, aid)
    if article is None:
        return {"error": f"could not retrieve article {aid!r} from {SITE}"}

    score = await ScoringFleet().score(article, check_links=check_links)
    return {
        **_brief(score.to_dict()),
        "title": score.title,  # on-demand scoring is not anonymised: the user asked
        "url": score.url,
        "dimensions": [
            {"name": d.name, "score": round(d.score, 1), "rationale": d.rationale}
            for d in score.dimensions
        ],
        "caps_applied": score.signals.get("caps_applied", []),
    }


@mcp.tool()
async def review_article(
    url_or_markdown: str, author_kind: str = "community", check_links: bool = True
) -> dict[str, Any]:
    """Review an article and recommend what would raise its Read-Quality Score.

    Use this when someone wants feedback on their own writing rather than a
    verdict on someone else's. Accepts a published builder.aws.com URL or a
    complete draft pasted as markdown.

    Recommendations are ranked by how many RQS points each is worth. Where a cap
    is binding, the figure is exact arithmetic rather than an estimate - the caps
    and weights are deterministic, so the cost of a missing citation or a missing
    code block is computable.

    Args:
        url_or_markdown: a builder.aws.com URL, an article id, or raw markdown.
        author_kind: "hero", "amazonian", "community_builder" or "community".
        check_links: verify outbound links resolve. Slower, more accurate.
    """
    from .advise import recommend
    from .agents import ScoringFleet
    from .ingest import article_id_from_url, fetch
    from .models import Article, Author

    text = url_or_markdown.strip()
    looks_like_a_draft = "\n" in text or len(text) > 400

    if looks_like_a_draft:
        first = text.splitlines()[0] if text else ""
        article = Article(
            article_id="draft",
            title=first.lstrip("# ").strip()[:200] or "untitled draft",
            uri="/content/draft/local",
            markdown=text,
            author=Author(
                alias="",
                is_aws_hero=author_kind == "hero",
                is_amazon_employee=author_kind == "amazonian",
                is_community_builder=author_kind == "community_builder",
            ),
        )
    else:
        aid = article_id_from_url(text) or text
        async with httpx.AsyncClient(follow_redirects=True) as client:
            fetched = await fetch(client, aid)
        if fetched is None:
            return {"error": f"could not retrieve {text!r}; paste the markdown instead"}
        article = fetched

    score = await ScoringFleet().score(article, check_links=check_links)
    advice = recommend(score.to_dict())
    return {"title": article.title, "url": article.url if not looks_like_a_draft else "", **advice}


@mcp.tool()
def explain_score(article_id_or_url: str) -> dict[str, Any]:
    """Full evidence breakdown for an already-scored article.

    Returns every dimension, the concrete evidence behind it, the caps that were
    applied, and the raw measured signals. Use this when the user asks why an
    article scored the way it did.
    """
    from .ingest import article_id_from_url

    aid = article_id_from_url(article_id_or_url) or article_id_or_url.strip()
    for s in _scores():
        if s["article_id"] == aid:
            sig = s.get("signals", {})
            return {
                "title": s["title"],
                "url": s["url"],
                "rqs": s["rqs"],
                "verdict": s["verdict"],
                "headline": s["headline"],
                "dimensions": s.get("dimensions", []),
                "caps_applied": sig.get("caps_applied", []),
                # Split out so a caller can always tell how much of the score
                # the article earned and how much came from who wrote it.
                "base_rqs": sig.get("base_rqs"),
                "author_bonus": sig.get("author_bonus"),
                "measured": {
                    "words": sig.get("structure", {}).get("words"),
                    "code_blocks": sig.get("structure", {}).get("code_blocks"),
                    "code_parse_rate": sig.get("code", {}).get("pass_rate"),
                    "terminal_sessions": sig.get("code", {}).get("output_blocks"),
                    "outbound_links": sig.get("structure", {}).get("links"),
                    "dead_links": sig.get("links", {}).get("dead"),
                    "primary_sources": sig.get("links", {}).get("primary_sources"),
                    "nonexistent_aws_apis": sig.get("aws_apis", {}).get("total_invalid"),
                    "cross_posted": sig.get("duplicates", {}).get("is_cross_post"),
                },
            }
    return {"error": f"article {aid!r} is not in the scored corpus; try score_url instead"}


@mcp.tool()
def search_scored(
    query: str, min_rqs: float = 0.0, limit: int = 10
) -> dict[str, Any]:
    """Search the scored corpus by title, tag, author or headline text."""
    q = query.strip().lower()
    if not q:
        return {"error": "empty query", "articles": []}

    hits = []
    for s in _scores():
        if s.get("rqs", 0) < min_rqs:
            continue
        hay = " ".join(
            [s.get("title", ""), s.get("headline", ""), s.get("author_alias", "")]
            + (s.get("tags") or [])
        ).lower()
        if q in hay:
            hits.append(s)

    hits.sort(key=lambda s: s["rqs"], reverse=True)
    return {"articles": [_brief(s) for s in hits[:limit]], "matched": len(hits)}


@mcp.tool()
def set_preferences(
    topics: list[str] | None = None,
    exclude_topics: list[str] | None = None,
    min_rqs: float | None = None,
    author_kinds: list[str] | None = None,
    max_age_days: int | None = None,
    frequency: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Save reading preferences. Every surface - feed, page, extension - honours these.

    Args:
        topics: tags to include, e.g. ["bedrock", "eks"]. Empty list means all topics.
        exclude_topics: tags to always drop, e.g. ["certification"].
        min_rqs: minimum Read-Quality Score to surface. 70 is the READ threshold.
        author_kinds: any of "hero", "amazonian", "community_builder", "community".
        max_age_days: how far back to look.
        frequency: "realtime", "hourly", "daily" or "weekly".
        limit: how many articles per delivery.
    """
    prefs = _load_prefs()
    for field, value in (
        ("topics", topics),
        ("exclude_topics", exclude_topics),
        ("min_rqs", min_rqs),
        ("author_kinds", author_kinds),
        ("max_age_days", max_age_days),
        ("frequency", frequency),
        ("limit", limit),
    ):
        if value is not None:
            setattr(prefs, field, value)

    prefs = Preferences.from_dict(prefs.to_dict())  # re-run validation
    _save_prefs(prefs)
    return {"saved": True, "preferences": prefs.to_dict()}


@mcp.tool()
def get_preferences() -> dict[str, Any]:
    """Current saved reading preferences."""
    return _load_prefs().to_dict()


@mcp.tool()
def export_corpus(
    min_rqs: float = 0.0, topics: list[str] | None = None, limit: int = 2000
) -> dict[str, Any]:
    """Every scored article as a full record, for building your own view.

    Unlike `get_reading_queue`, which returns the compact form a model needs to
    make a recommendation, this returns the complete scored record including
    dimension breakdowns and the measured signals behind them.

    The attribution policy is applied before anything leaves: articles scoring
    READ or SKIM are named and linked, articles scoring SKIP carry their score
    and reasons but never their author or title.

    Args:
        min_rqs: minimum Read-Quality Score to include. 0 returns everything.
        topics: tag allowlist. Omit for all topics.
        limit: maximum records to return.
    """
    from .publish import _public, _stats

    scores = _scores()
    if not scores:
        return {"error": "no scored corpus available yet", "scores": []}

    wanted = {t.lower() for t in (topics or [])}
    kept = [
        s
        for s in scores
        if s.get("rqs", 0) >= min_rqs
        and (not wanted or wanted & {t.lower() for t in (s.get("tags") or [])})
    ]
    kept.sort(key=lambda s: s.get("rqs", 0), reverse=True)
    kept = kept[:limit]

    return {
        "scores": [_public(s) for s in kept],
        "returned": len(kept),
        "corpus_size": len(scores),
        "stats": _stats(kept),
    }


@mcp.tool()
def corpus_stats() -> dict[str, Any]:
    """Aggregate statistics for the scored corpus.

    Useful for questions like "how much of Builder Center is worth reading?".
    """
    from .publish import _stats

    scores = _scores()
    if not scores:
        return {"error": "no scored corpus available yet"}
    return _stats(scores)


# --------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="ShouldIRead MCP server")
    parser.add_argument(
        "--http",
        action="store_true",
        help="serve streamable HTTP on 0.0.0.0:8000/mcp instead of stdio",
    )
    args = parser.parse_args()

    if args.http:
        from mcp.server.transport_security import TransportSecuritySettings

        mcp.settings.host = "0.0.0.0"
        mcp.settings.port = int(os.environ.get("PORT", "8000"))
        mcp.settings.streamable_http_path = "/mcp"
        mcp.settings.stateless_http = True  # required by AgentCore Runtime
        # DNS-rebinding protection validates the Host header, which AgentCore
        # rewrites when it proxies to the container - the container answers 421
        # and every MCP call fails. That protection guards browser-origin
        # attacks; here the only route in is AgentCore's SigV4-authenticated
        # endpoint, so Host validation is the wrong control and simply breaks it.
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        )
        asyncio.run(mcp.run_streamable_http_async())
    else:
        mcp.run()


if __name__ == "__main__":
    main()
