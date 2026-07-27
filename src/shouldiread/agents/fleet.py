"""The scoring fleet.

Shape of one scoring run:

    deterministic tools  ->  TRIAGE (cheap)  ->  4 judges in parallel (deep)
                                              ->  caps  ->  RQS  ->  VERDICT

Two deliberate choices:

* The RQS is arithmetic, not a model output. Judges score their own dimension;
  the weighted sum happens in Python. Models are unreliable at arithmetic and
  reproducibility matters more than nuance for the final number.
* Caps clamp a dimension against what was actually measured. A judge cannot
  award 80 for code substance to an article containing no code, however
  persuasive the prose. This is the guardrail that makes the score hard to talk
  your way past.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import statistics
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from strands import Agent
from strands.models import BedrockModel

from ..analyze import analyze, heuristic_floor
from ..config import (
    AWS_PROFILE,
    AWS_REGION,
    MODEL_JUDGE,
    MODEL_TRIAGE,
    WEIGHTS,
    author_bonus,
    verdict_for,
)
from ..models import Article, Dimension, Evidence, Score
from ..tools.dedup import DuplicateIndex
from . import prompts
from .schemas import (
    CodeResult,
    DepthResult,
    FinalVerdict,
    ProvenanceResult,
    SourceResult,
    TriageResult,
)

log = logging.getLogger(__name__)

# How much article text each judge sees. Nova Pro has plenty of context, but
# trimming keeps cost down and keeps judges focused on the substantive part.
EXCERPT_CHARS = 14000
CODE_EXCERPT_CHARS = 9000

JUDGE_RETRIES = 3

# How many times each deep judge is sampled; the per-dimension median is used.
#
# Not optional polish. Scoring one article five times produced a 32-point spread
# in the final RQS and an 80-point spread on execution evidence - one sample
# returned 0 for a post containing five pasted terminal sessions - and the
# verdict flipped between SKIM and READ. Temperature is already 0; Bedrock does
# not promise determinism, and a single sample is simply not a measurement.
# Median of three costs roughly 2.5x and removes most of it.
JUDGE_SAMPLES = int(os.environ.get("SIR_JUDGE_SAMPLES", "3"))

# Failures caused by load rather than by the request. Everything else is a real
# error and retrying it just wastes tokens.
_TRANSIENT = (
    "throttl",
    "timeout",
    "timed out",
    "too many requests",
    "serviceunavailable",
    "service unavailable",
    "internalserver",
    "connection",
    "eventstream",
    "incomplete read",
    # A request signed, then queued behind a backlog for longer than the five
    # minutes SigV4 allows. Retrying re-signs it, so this is transient - but
    # seeing it means requests are sitting in the pool and concurrency is too
    # high for the machine.
    "signature expired",
    "invalidsignature",
)


def _is_transient(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(marker in text for marker in _TRANSIENT)


# --------------------------------------------------------------------------
# model plumbing
# --------------------------------------------------------------------------
def _session() -> boto3.Session:
    try:
        return boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)
    except Exception:  # running in Lambda/AgentCore: use the task role
        return boto3.Session(region_name=AWS_REGION)


def _model(model_id: str, temperature: float = 0.0) -> BedrockModel:
    # A corpus run fans out to several hundred concurrent Bedrock calls, so
    # adaptive retry is doing real work here, not defensive decoration.
    return BedrockModel(
        boto_session=_session(),
        boto_client_config=BotoConfig(
            retries={"max_attempts": 6, "mode": "adaptive"},
            # botocore pools 10 connections by default. A corpus run issues
            # roughly four times the article concurrency in parallel requests,
            # so the default silently discards and re-establishes connections -
            # it shows up as throughput collapse, not as an error.
            max_pool_connections=int(os.environ.get("SIR_MAX_POOL", "128")),
            connect_timeout=10,
            read_timeout=120,
        ),
        model_id=model_id,
        temperature=temperature,
        max_tokens=2048,
    )


def _agent(system_prompt: str, schema: type, *, model: BedrockModel) -> Agent:
    """Build a fresh agent for a single invocation.

    Deliberately not cached. A Strands Agent owns a conversation, so reusing one
    across articles would both trip its concurrency guard and accumulate every
    previously scored article in the context window - each judgement silently
    conditioned on the last. The model object is shared; the agent never is.
    """
    return Agent(
        model=model,
        system_prompt=system_prompt,
        structured_output_model=schema,
        callback_handler=None,
    )


# --------------------------------------------------------------------------
# prompt assembly
# --------------------------------------------------------------------------
def _signal_block(article: Article, signals: dict[str, Any]) -> str:
    """The measured facts, rendered for the model. Judges may not contradict these."""
    s, c, a = signals["structure"], signals["code"], signals["aws_apis"]
    l, d = signals["links"], signals["duplicates"]

    lines = [
        "MEASURED SIGNALS (exact, tool-derived - treat as ground truth):",
        f"  words: {s['words']}",
        f"  code blocks: {s['code_blocks']} ({s['code_loc']} lines of code)",
        f"  code blocks syntax-checked: {c['checked']}, passed: {c['passed']}, failed: {c['failed']}",
        f"  complete runnable files: {c['complete_files']}",
        f"  pasted terminal/console sessions: {c['output_blocks']}",
        f"  terminal or error artefacts in text: {s['terminal_evidence']}",
        f"  measurements with units (times, costs, sizes): {s['measurements']}",
        f"  placeholder density in code: {s['placeholder_density']} per 100 lines",
        f"  outbound links: {s['links']} (by kind: {l['by_category']})",
        f"  links verified dead: {l['dead']} of {l['checked']} checked",
        f"  primary sources (AWS docs / specs / repos): {l['primary_sources']}",
        f"  images or diagrams: {s['images']}",
        f"  real markdown headings: {s['atx_headings']}, bold-text pseudo-headings: {s['bold_pseudo_headings']}",
        f"  AWS API references checked: {a['total_checked']}, nonexistent: {a['total_invalid']}",
    ]
    if a["invalid_calls"] or a["invalid_cli"] or a["invalid_services"]:
        bad = a["invalid_calls"] + a["invalid_cli"] + a["invalid_services"]
        lines.append(f"  !! APIs that DO NOT EXIST in any AWS SDK: {bad}")
    if a["suspect_api_names"]:
        lines.append(
            f"  ?  unrecognised API-looking names (low confidence, verify yourself): "
            f"{a['suspect_api_names'][:8]}"
        )
    if d["is_cross_post"]:
        lines.append(f"  cross-posted from: {d['canonical_host']} (declared, not a problem in itself)")
    if d["has_foreign_duplicate"]:
        lines.append(
            f"  !! near-duplicate of a DIFFERENT author's article (similarity {d['max_similarity']})"
        )
    if signals["engagement"]["suspicious"]:
        lines.append(f"  !! engagement looks inflated: {signals['engagement']['reason']}")

    # The author's status (Hero / Amazon employee / Community Builder) is
    # withheld on purpose. Credibility is applied once, visibly, as a bonus on
    # the finished score; letting the judges see it too would double-count it
    # and bury it inside five separate rationales.
    lines.append(f"  tags: {', '.join(article.tags) or 'none'}")
    return "\n".join(lines)


def _article_block(article: Article, limit: int = EXCERPT_CHARS) -> str:
    body = article.markdown
    truncated = ""
    if len(body) > limit:
        body = body[:limit]
        truncated = f"\n\n[... truncated, {article.word_count} words total ...]"
    return f"TITLE: {article.title}\n\nARTICLE:\n{body}{truncated}"


def _code_block_text(article: Article) -> str:
    from ..tools import extract_code_blocks

    blocks = extract_code_blocks(article.markdown)
    if not blocks:
        return "(this article contains no code blocks at all)"
    out, used = [], 0
    for b in blocks:
        chunk = f"--- block {b.index} [{b.lang or 'unlabelled'}] ---\n{b.body}\n"
        if used + len(chunk) > CODE_EXCERPT_CHARS:
            out.append(f"[... {len(blocks) - b.index} further blocks omitted ...]")
            break
        out.append(chunk)
        used += len(chunk)
    return "\n".join(out)


# --------------------------------------------------------------------------
# caps: measured reality beats model opinion
# --------------------------------------------------------------------------
def apply_caps(dims: dict[str, float], signals: dict[str, Any], content_type: str) -> tuple[dict[str, float], list[str]]:
    """Clamp dimension scores that the measurements cannot support."""
    s, c, a = signals["structure"], signals["code"], signals["aws_apis"]
    l, d = signals["links"], signals["duplicates"]
    capped = dict(dims)
    notes: list[str] = []

    def cap(dim: str, ceiling: float, why: str) -> None:
        if capped[dim] > ceiling:
            capped[dim] = ceiling
            notes.append(f"{dim} capped at {ceiling:.0f}: {why}")

    # Execution evidence must be evidenced.
    if s["terminal_evidence"] == 0 and c["output_blocks"] == 0 and s["images"] == 0:
        if s["measurements"] < 3:
            cap("execution_evidence", 25, "no terminal output, screenshots or measurements")
        else:
            cap("execution_evidence", 45, "numbers quoted but no output, screenshots or transcripts")

    # Code substance requires code. Content types where code is not expected are
    # exempt: an opinion piece is not a failed tutorial.
    code_optional = content_type in {"news", "opinion", "announcement", "certification_journey"}
    if s["code_blocks"] == 0 and not code_optional:
        cap("code_substance", 20, "no code blocks in a piece that calls for them")
    elif s["code_blocks"] == 0 and code_optional:
        cap("code_substance", 55, "no code, but not the kind of article that needs it")
    if c["checked"] and c["pass_rate"] < 0.5:
        cap("code_substance", 40, "most code blocks do not parse")
    if s["placeholder_density"] > 30:
        cap("code_substance", 50, "code is largely placeholders")

    # An invented API is disqualifying for both code and sources.
    if a["total_invalid"] > 0:
        cap("code_substance", 25, f"{a['total_invalid']} AWS API reference(s) do not exist")
        cap("source_integrity", 25, "cites AWS APIs that do not exist")

    # Sources.
    if s["links"] == 0:
        cap("source_integrity", 30, "no outbound sources at all")
    elif l["primary_sources"] == 0:
        cap("source_integrity", 55, "no primary sources cited")
    if l["checked"] and l["dead_ratio"] > 0.4:
        cap("source_integrity", 40, "most links are dead")

    # Originality.
    if d["has_foreign_duplicate"]:
        cap("originality", 10, "near-duplicate of another author's article")
    if s["bold_pseudo_headings"] > 3 and s["atx_headings"] == 0:
        cap("originality", 55, "bold-text pseudo-headings throughout")

    # Depth has a length floor: you cannot go deep in 300 words.
    if s["words"] < 300:
        cap("depth", 35, "too short to develop anything")

    return capped, notes


def compute_rqs(dims: dict[str, float]) -> float:
    total_weight = sum(WEIGHTS.values())
    return sum(dims[k] * w for k, w in WEIGHTS.items()) / total_weight


# --------------------------------------------------------------------------
# the fleet
# --------------------------------------------------------------------------
def _median_field(results: list[Any], field: str, default: float) -> float:
    """Median of one numeric field across samples of the same judge."""
    values = [float(getattr(r, field)) for r in results if getattr(r, field, None) is not None]
    return statistics.median(values) if values else default


def _spread(results: list[Any], field: str) -> float:
    """Max minus min across samples. Large values mean the judges disagreed and
    the dimension should be read as uncertain, not precise."""
    values = [float(getattr(r, field)) for r in results if getattr(r, field, None) is not None]
    return round(max(values) - min(values), 1) if len(values) > 1 else 0.0


def _representative(results: list[Any], field: str) -> Any | None:
    """The sample whose score sits closest to the median, for its prose.

    Rationales should belong to a real sample rather than be stitched together
    from several - the quoted evidence has to match the score it explains.
    """
    if not results:
        return None
    target = _median_field(results, field, 0.0)
    return min(results, key=lambda r: abs(float(getattr(r, field, 0)) - target))


class ScoringFleet:
    """Multi-agent scorer. Reuse one instance across a corpus run."""

    def __init__(self, *, triage_model: str = MODEL_TRIAGE, judge_model: str = MODEL_JUDGE):
        self.triage_model = triage_model
        self.judge_model = judge_model
        # Models are shared and reused; agents are built per invocation below.
        self._triage_m = _model(triage_model)
        self._judge_m = _model(judge_model)
        # Judges that gave up on the article currently being scored. Recorded on
        # the result so a degraded score is visible rather than indistinguishable
        # from a genuine mediocre one.
        self.failed_judges: list[str] = []

    async def _ask(
        self, system_prompt: str, schema: type, prompt: str, what: str, *, triage: bool = False
    ) -> Any | None:
        """Run one judge, retrying transient failures.

        Falling straight back to a neutral 40 on a throttle would let load, not
        content, decide a score - and it does so silently. Transient failures get
        retried; only a persistent one degrades the article.
        """
        model = self._triage_m if triage else self._judge_m
        last: Exception | None = None

        for attempt in range(JUDGE_RETRIES):
            # A fresh agent each attempt: a failed invocation can leave the
            # previous one holding a half-written conversation.
            agent = _agent(system_prompt, schema, model=model)
            try:
                result = await agent.invoke_async(prompt)
                return result.structured_output
            except Exception as exc:
                last = exc
                if not _is_transient(exc) or attempt == JUDGE_RETRIES - 1:
                    break
                delay = 2**attempt + random.random()
                log.debug(
                    "%s judge transient failure (%s), retrying in %.1fs",
                    what, type(exc).__name__, delay,
                )
                await asyncio.sleep(delay)

        log.warning("%s judge failed: %s: %s", what, type(last).__name__, last)
        self.failed_judges.append(what)
        return None

    async def _ask_many(
        self,
        system_prompt: str,
        schema: type,
        prompt: str,
        what: str,
        *,
        samples: int = JUDGE_SAMPLES,
    ) -> list[Any]:
        """Sample one judge `samples` times concurrently. Returns what came back."""
        results = await asyncio.gather(
            *(self._ask(system_prompt, schema, prompt, what) for _ in range(samples))
        )
        return [r for r in results if r is not None]


    async def score(
        self,
        article: Article,
        *,
        index: DuplicateIndex | None = None,
        check_links: bool = True,
    ) -> Score:
        self.failed_judges = []
        signals = await analyze(article, index=index, check_links=check_links)
        floor = heuristic_floor(signals)
        signal_text = _signal_block(article, signals)

        # --- gate 1: measurably empty. No model call at all. ---
        if floor["trivially_empty"]:
            return self._empty_verdict(article, signals, floor)

        # --- gate 2: cheap triage ---
        triage: TriageResult | None = await self._ask(
            prompts.TRIAGE,
            TriageResult,
            f"{signal_text}\n\n{_article_block(article, 6000)}",
            "triage",
            triage=True,
        )
        content_type = triage.content_type if triage else "other"

        if triage and not triage.worth_deep_review:
            return self._shallow_verdict(article, signals, triage)

        # --- deep review: four judges, concurrently ---
        article_text = _article_block(article)
        prov_s, code_s, source_s, depth_s = await asyncio.gather(
            self._ask_many(
                prompts.PROVENANCE, ProvenanceResult,
                f"{signal_text}\n\n{article_text}", "provenance",
            ),
            self._ask_many(
                prompts.CODE, CodeResult,
                f"{signal_text}\n\nTITLE: {article.title}\n\n"
                f"CODE BLOCKS:\n{_code_block_text(article)}",
                "code",
            ),
            self._ask_many(
                prompts.SOURCE, SourceResult,
                f"{signal_text}\n\n{article_text}", "source",
            ),
            self._ask_many(
                prompts.DEPTH, DepthResult,
                f"{signal_text}\n\n{article_text}", "depth",
            ),
        )

        # Scores are per-dimension medians; the prose comes from whichever
        # sample landed closest to that median.
        prov = _representative(prov_s, "score")
        code = _representative(code_s, "score")
        source = _representative(source_s, "score")
        depth = _representative(depth_s, "depth_score")

        raw = {
            "execution_evidence": _median_field(prov_s, "score", 40.0),
            "code_substance": _median_field(code_s, "score", 40.0),
            "source_integrity": _median_field(source_s, "score", 40.0),
            "depth": _median_field(depth_s, "depth_score", 40.0),
            "originality": _median_field(depth_s, "originality_score", 40.0),
        }
        sample_spread = {
            "execution_evidence": _spread(prov_s, "score"),
            "code_substance": _spread(code_s, "score"),
            "source_integrity": _spread(source_s, "score"),
            "depth": _spread(depth_s, "depth_score"),
            "originality": _spread(depth_s, "originality_score"),
        }
        capped, cap_notes = apply_caps(raw, signals, content_type)
        base_rqs = compute_rqs(capped)
        bonus = author_bonus(article.author.kind, base_rqs)
        rqs = min(100.0, base_rqs + bonus)

        dimensions = [
            Dimension(
                name="execution_evidence",
                score=capped["execution_evidence"],
                rationale=prov.rationale if prov else "judge unavailable",
                evidence=(
                    [Evidence("provenance", e, weight_hint="positive") for e in prov.evidence[:5]]
                    + [Evidence("provenance", m, weight_hint="negative") for m in prov.missing[:5]]
                )
                if prov
                else [],
            ),
            Dimension(
                name="code_substance",
                score=capped["code_substance"],
                rationale=code.rationale if code else "judge unavailable",
                evidence=[Evidence("code", i, weight_hint="negative") for i in code.issues[:5]]
                if code
                else [],
            ),
            Dimension(
                name="source_integrity",
                score=capped["source_integrity"],
                rationale=source.rationale if source else "judge unavailable",
                evidence=[
                    Evidence("source", c, weight_hint="negative")
                    for c in source.unsupported_claims[:5]
                ]
                if source
                else [],
            ),
            Dimension(
                name="depth",
                score=capped["depth"],
                rationale=depth.rationale if depth else "judge unavailable",
                evidence=[Evidence("depth", f"new to reader: {depth.what_is_new}")] if depth else [],
            ),
            Dimension(
                name="originality",
                score=capped["originality"],
                rationale=depth.rationale if depth else "judge unavailable",
                evidence=[
                    Evidence("depth", g, weight_hint="negative") for g in depth.generic_markers[:5]
                ]
                if depth
                else [],
            ),
        ]

        verdict_text = verdict_for(rqs)
        summary = "\n".join(
            f"  {d.name}: {d.score:.0f}/100 - {d.rationale}" for d in dimensions
        )
        final: FinalVerdict | None = await self._ask(
            prompts.VERDICT,
            FinalVerdict,
            f"{signal_text}\n\nTITLE: {article.title}\n"
            f"CONTENT TYPE: {content_type}\n\n"
            f"DIMENSION SCORES:\n{summary}\n\n"
            f"COMPUTED RQS: {rqs:.0f}/100 -> {verdict_text}\n"
            + ("CAPS APPLIED:\n  " + "\n  ".join(cap_notes) if cap_notes else ""),
            "verdict",
        )

        return Score(
            article_id=article.article_id,
            title=article.title,
            url=article.url,
            author_alias=article.author.alias,
            author_kind=article.author.kind,
            tags=article.tags,
            published_at=article.published_at,
            rqs=rqs,
            verdict=verdict_text,
            headline=final.headline if final else (floor["negatives"] or ["scored"])[0],
            dimensions=dimensions,
            signals={
                **signals,
                "heuristic": floor,
                "caps_applied": cap_notes,
                "content_type": content_type,
                "best_for": final.best_for if final else "",
                "disagreement": final.disagreement if final else "",
                "raw_dimension_scores": raw,
                # Evidence-only score, and the credibility added on top of it.
                # Kept separate so anyone can see how much of an RQS was earned
                # by the article and how much by who wrote it.
                "base_rqs": round(base_rqs, 1),
                "author_bonus": round(bonus, 1),
                "judge_samples": JUDGE_SAMPLES,
                # Per-dimension disagreement across samples. High spread means
                # the number is soft, however precise it looks.
                "sample_spread": sample_spread,
                # Non-empty means this score is degraded and worth re-running.
                "failed_judges": list(self.failed_judges),
            },
            scored_at=datetime.now(timezone.utc).isoformat(),
            model=f"{self.triage_model} + {self.judge_model}",
        )

    # -- cheap exits ------------------------------------------------------
    def _base(
        self,
        article: Article,
        signals: dict,
        rqs: float,
        headline: str,
        extra: dict,
        model: str = "deterministic",
    ) -> Score:
        # Same credibility rule as the deep path, so a verdict never depends on
        # which branch produced it.
        bonus = author_bonus(article.author.kind, rqs)
        base, rqs = rqs, min(100.0, rqs + bonus)
        extra = {**extra, "base_rqs": round(base, 1), "author_bonus": round(bonus, 1)}
        return Score(
            article_id=article.article_id,
            title=article.title,
            url=article.url,
            author_alias=article.author.alias,
            author_kind=article.author.kind,
            tags=article.tags,
            published_at=article.published_at,
            rqs=rqs,
            verdict=verdict_for(rqs),
            headline=headline,
            dimensions=[],
            signals={**signals, **extra},
            scored_at=datetime.now(timezone.utc).isoformat(),
            model=model,
        )

    def _empty_verdict(self, article: Article, signals: dict, floor: dict) -> Score:
        words = signals["structure"]["words"]
        why = ", ".join(floor["negatives"][:3]) or "nothing substantive to evaluate"
        return self._base(
            article,
            signals,
            rqs=min(20.0, words / 20.0),
            headline=f"Not a technical article: {why}.",
            extra={"heuristic": floor, "content_type": "empty", "gate": "trivially_empty"},
        )

    def _shallow_verdict(self, article: Article, signals: dict, triage: TriageResult) -> Score:
        # Triage rejected it, so the deep judges never ran. Cap the score at the
        # SKIM boundary: without a full review we have not earned a READ.
        return self._base(
            article,
            signals,
            rqs=min(float(triage.preliminary_score), 45.0),
            headline=triage.one_line,
            extra={
                "heuristic": heuristic_floor(signals),
                "content_type": triage.content_type,
                "gate": "triage_rejected",
            },
            model=self.triage_model,
        )
