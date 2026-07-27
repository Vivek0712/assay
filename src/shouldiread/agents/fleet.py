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
    DIMENSION_FAMILIES,
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
    AwsResult,
    ContentResult,
    EvidenceResult,
    FinalVerdict,
    ReaderResult,
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
# Three. Bundling several dimensions into one judge call splits the model's
# attention and reintroduces per-dimension disagreement the bands alone do not
# remove - up to 40 points on aws_service_depth before the median. Five samples
# were tried and abandoned: the across-run spread was already within 15 points
# at three, the whole-article RQS within ~3, and five put 100 calls in flight
# per batch without moving either number enough to justify the wait.
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
    fp = signals.get("aws_footprint") or {}
    if fp.get("services"):
        lines.append(
            f"  AWS services referenced: {fp['services']} "
            f"(in code: {fp.get('services_in_code') or 'none'})"
        )
        lines.append(
            f"  AWS operations invoked: {fp.get('operations_invoked', 0)}; "
            f"CloudFormation resources: {len(fp.get('cfn_resources') or [])}; "
            f"Terraform resources: {len(fp.get('terraform_resources') or [])}; "
            f"CDK: {fp.get('uses_cdk')}; ARNs: {fp.get('arns', 0)}; "
            f"IAM statements: {fp.get('iam_statements', 0)}; "
            f"quota/limit mentions: {fp.get('quota_mentions', 0)}"
        )
        if fp.get("names_only"):
            lines.append("  !! services are NAMED but nothing is configured or invoked")
    else:
        lines.append("  AWS services referenced: none")

    art = signals.get("artifacts") or {}
    if art.get("total"):
        bits = []
        if art.get("repos"):
            own = " (the author's own)" if art.get("is_authors_own") else ""
            bits.append(f"{len(art['repos'])} linked repository/ies{own}")
        if art.get("packages"):
            bits.append(f"{len(art['packages'])} published package(s)")
        if art.get("demos"):
            bits.append(f"{len(art['demos'])} demo/video link(s)")
        if art.get("diagrams"):
            bits.append(f"{art['diagrams']} diagram(s)")
        if art.get("hero_image"):
            bits.append("hero image")
        lines.append(f"  durable artefacts: {', '.join(bits)}")
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
def apply_caps(
    dims: dict[str, float], signals: dict[str, Any], content_type: str
) -> tuple[dict[str, float], list[str]]:
    """Clamp dimensions the measurements cannot support.

    Deliberately light. Caps exist to stop a judge awarding points for something
    demonstrably absent - not to enforce a house style. An article with no code
    is not automatically worse; one claiming APIs that do not exist is.
    """
    s_, c, a = signals["structure"], signals["code"], signals["aws_apis"]
    l = signals["links"]
    fp = signals.get("aws_footprint") or {}
    capped = dict(dims)
    notes: list[str] = []

    def cap(dim: str, ceiling: float, why: str) -> None:
        if dim in capped and capped[dim] > ceiling:
            capped[dim] = ceiling
            notes.append(f"{dim} capped at {ceiling:.0f}: {why}")

    # --- content ---------------------------------------------------------
    if s_["words"] < 250:
        cap("substance", 40, "too short to carry much")
        cap("insight", 40, "too short to develop anything")
        cap("explanation_depth", 45, "too short to explain a mechanism")
    if s_["words"] > 1200 and s_["code_blocks"] == 0 and s_["measurements"] < 2:
        cap("substance", 55, "long-form with no code, no numbers and no specifics")

    # --- AWS -------------------------------------------------------------
    if not fp.get("services"):
        cap("aws_service_depth", 10, "no AWS service referenced at all")
    elif fp.get("names_only") and s_["measurements"] < 3:
        cap("aws_service_depth", 49, "services named, but nothing configured, invoked or measured")
    if a["total_invalid"] > 0:
        cap("currency", 40, "references AWS APIs that do not exist")

    # --- evidence: the one hard disqualifier -----------------------------
    if a["total_invalid"] > 0:
        cap("code_quality", 29,
            f"{a['total_invalid']} AWS API reference(s) do not exist in any SDK")
        cap("accuracy", 35, "cites AWS operations that do not exist")
    if c["checked"] and c["pass_rate"] < 0.5:
        cap("code_quality", 45, "most code blocks do not parse")
    if s_["placeholder_density"] > 30:
        cap("code_quality", 55, "code is largely placeholders")
    if l["checked"] and l["dead_ratio"] > 0.4:
        cap("sources", 45, "most outbound links are dead")
    # Only demand sourcing where the piece makes factual claims to support.
    if s_["links"] == 0 and content_type in {"tutorial", "deep_dive", "reference", "news"}:
        cap("sources", 50, "no outbound sources in a piece making factual claims")

    # --- reader ----------------------------------------------------------
    if s_["bold_pseudo_headings"] > 3 and s_["atx_headings"] == 0:
        cap("clarity", 65, "bold-text pseudo-headings instead of real structure")

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
        content_s, aws_s, evidence_s, reader_s = await asyncio.gather(
            self._ask_many(
                prompts.CONTENT, ContentResult,
                f"{signal_text}\n\n{article_text}", "content",
            ),
            self._ask_many(
                prompts.AWS, AwsResult,
                f"{signal_text}\n\n{article_text}", "aws",
            ),
            self._ask_many(
                prompts.EVIDENCE, EvidenceResult,
                f"{signal_text}\n\n{article_text}\n\n"
                f"CODE BLOCKS:\n{_code_block_text(article)}",
                "evidence",
            ),
            self._ask_many(
                prompts.READER, ReaderResult,
                f"{signal_text}\n\n{article_text}", "reader",
            ),
        )

        # Each family judge returns several dimensions in one response; take the
        # median per dimension across samples, exactly as before.
        families = {
            "content": content_s, "aws": aws_s,
            "evidence": evidence_s, "reader": reader_s,
        }
        raw: dict[str, float] = {}
        sample_spread: dict[str, float] = {}
        for family, dimensions in DIMENSION_FAMILIES.items():
            samples = families[family]
            for dimension in dimensions:
                raw[dimension] = _median_field(samples, dimension, 40.0)
                sample_spread[dimension] = _spread(samples, dimension)

        content = _representative(content_s, "substance")
        aws = _representative(aws_s, "aws_service_depth")
        evidence = _representative(evidence_s, "evidence")
        reader = _representative(reader_s, "clarity")
        capped, cap_notes = apply_caps(raw, signals, content_type)
        base_rqs = compute_rqs(capped)
        bonus = author_bonus(article.author.kind, base_rqs)
        rqs = min(100.0, base_rqs + bonus)

        def _ev(tool: str, items, hint: str, prefix: str = "") -> list[Evidence]:
            return [
                Evidence(tool, f"{prefix}{x}", weight_hint=hint) for x in (items or [])[:4]
            ]

        # Family-level observations hang off the dimension they most speak to, so
        # the breakdown stays readable rather than repeating the same notes five
        # times.
        family_evidence: dict[str, list[Evidence]] = {
            "substance": (
                _ev("content", content.specifics, "positive")
                + _ev("content", content.padding, "negative", "padding: ")
            ) if content else [],
            "insight": (
                [Evidence("content", f"new to reader: {content.what_is_new}")]
            ) if content else [],
            "accuracy": _ev("content", content.errors, "negative", "error: ") if content else [],
            "aws_service_depth": (
                _ev("aws", aws.operating_detail, "positive", "operates: ")
                + _ev("aws", aws.named_only, "negative", "named only: ")
            ) if aws else [],
            "currency": _ev("aws", aws.dated_or_wrong, "negative", "dated: ") if aws else [],
            "evidence": (
                _ev("evidence", evidence.supported_by, "positive")
                + _ev("evidence", evidence.unsupported_claims, "negative", "unsupported: ")
            ) if evidence else [],
            "clarity": (
                _ev("reader", reader.strengths, "positive")
                + _ev("reader", reader.obstacles, "negative", "obstacle: ")
            ) if reader else [],
            "actionability": (
                [Evidence("reader", f"next step: {reader.next_step}")]
            ) if reader else [],
        }
        family_rationale = {
            "content": content.rationale if content else "judge unavailable",
            "aws": aws.rationale if aws else "judge unavailable",
            "evidence": evidence.rationale if evidence else "judge unavailable",
            "reader": reader.rationale if reader else "judge unavailable",
        }
        dimension_family = {
            d: f for f, dims in DIMENSION_FAMILIES.items() for d in dims
        }

        dimensions = [
            Dimension(
                name=name,
                score=capped[name],
                rationale=family_rationale[dimension_family[name]],
                evidence=family_evidence.get(name, []),
            )
            for name in WEIGHTS
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
