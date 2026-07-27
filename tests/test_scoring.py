"""Tests for scoring arithmetic, caps, preferences and the serving surfaces.

No Bedrock calls here - these cover the deterministic half of the pipeline,
which is where a silent regression would be hardest to notice.
"""

from __future__ import annotations

import json
import re

import pytest

from shouldiread.agents.fleet import apply_caps, compute_rqs
from shouldiread.config import WEIGHTS, verdict_for
from shouldiread.preferences import Preferences
from shouldiread.publish import _public, _stats, render_feed, render_page


def signals(**over):
    base = {
        "structure": {
            "words": 1500, "code_blocks": 4, "code_loc": 80, "links": 6,
            "images": 1, "terminal_evidence": 0, "measurements": 4,
            "placeholder_density": 2.0, "atx_headings": 8, "bold_pseudo_headings": 0,
        },
        "code": {"checked": 4, "passed": 4, "failed": 0, "pass_rate": 1.0,
                 "output_blocks": 0, "complete_files": 1},
        "aws_apis": {"total_checked": 5, "total_invalid": 0, "invalid_calls": [],
                     "invalid_cli": [], "invalid_services": [], "suspect_api_names": []},
        "links": {"total": 6, "checked": 6, "dead": 0, "dead_ratio": 0.0,
                  "primary_sources": 3, "by_category": {"primary": 3, "code": 3}},
        "duplicates": {"is_cross_post": False, "has_foreign_duplicate": False,
                       "max_similarity": 0.0},
        "engagement": {"suspicious": False, "reason": ""},
        "artifacts": {"has_durable_artifact": False, "is_authors_own": False, "total": 0},
        "aws_footprint": {
            "services": ["s3", "lambda"], "services_in_code": ["s3"],
            "operations_invoked": 4, "cfn_resources": [], "terraform_resources": [],
            "uses_cdk": False, "arns": 2, "iam_statements": 1,
            "quota_mentions": 2, "operated": 7, "names_only": False,
        },
    }
    for section, values in over.items():
        base[section] = {**base[section], **values}
    return base


ALL_HIGH = {k: 90.0 for k in WEIGHTS}  # includes aws_depth


# ------------------------------------------------------------------ rqs ---
def test_rqs_is_weighted_mean():
    assert compute_rqs({k: 100.0 for k in WEIGHTS}) == pytest.approx(100.0)
    assert compute_rqs({k: 0.0 for k in WEIGHTS}) == pytest.approx(0.0)
    assert compute_rqs({k: 50.0 for k in WEIGHTS}) == pytest.approx(50.0)



def test_verdict_thresholds():
    assert verdict_for(70) == "READ"
    assert verdict_for(69.9) == "SKIM"
    assert verdict_for(40) == "SKIM"
    assert verdict_for(39.9) == "SKIP"


# ----------------------------------------------------------------- caps ---







def test_clean_article_is_not_capped():
    capped, notes = apply_caps(
        ALL_HIGH,
        signals(code={"output_blocks": 3}, structure={"terminal_evidence": 3}),
        "tutorial",
    )
    assert capped == ALL_HIGH
    assert notes == []


def test_caps_never_raise_a_score():
    low = {k: 5.0 for k in WEIGHTS}
    capped, _ = apply_caps(low, signals(structure={"code_blocks": 0, "links": 0}), "tutorial")
    assert all(capped[k] <= low[k] for k in WEIGHTS)


# ---------------------------------------------------------- preferences ---
def score_row(**over):
    row = {
        "article_id": "a1", "title": "T", "url": "u", "author_alias": "alice",
        "author_kind": "community_builder", "tags": ["bedrock", "serverless"],
        "published_at": None, "rqs": 80.0, "verdict": "READ", "headline": "h",
        "dimensions": [], "signals": {}, "scored_at": None, "model": "m",
    }
    row.update(over)
    return row


def test_min_rqs_filter():
    prefs = Preferences(min_rqs=70)
    assert prefs.matches(score_row(rqs=80))
    assert not prefs.matches(score_row(rqs=60))


def test_topic_allowlist_and_blocklist():
    assert Preferences(min_rqs=0, topics=["bedrock"]).matches(score_row())
    assert not Preferences(min_rqs=0, topics=["eks"]).matches(score_row())
    assert not Preferences(min_rqs=0, exclude_topics=["serverless"]).matches(score_row())


def test_author_kind_filter():
    assert Preferences(min_rqs=0, author_kinds=["community_builder"]).matches(score_row())
    assert not Preferences(min_rqs=0, author_kinds=["hero"]).matches(score_row())


def test_apply_sorts_by_rqs_descending_and_limits():
    rows = [score_row(article_id=str(i), rqs=float(i)) for i in range(10)]
    out = Preferences(min_rqs=0, limit=3).apply(rows)
    assert [r["rqs"] for r in out] == [9.0, 8.0, 7.0]


def test_preferences_validation_clamps_bad_input():
    p = Preferences(min_rqs=999, limit=99999, frequency="whenever", author_kinds=["wizard"])
    assert p.min_rqs == 100.0
    assert p.limit == 500
    assert p.frequency == "daily"
    assert p.author_kinds == []


# ------------------------------------------------------- attribution ------
def test_skip_articles_are_redacted():
    """Attribution policy: SKIP verdicts are never publicly attributed."""
    pub = _public(score_row(verdict="SKIP", rqs=20, title="A Bad Post", author_alias="bob"))
    assert pub["title"] == "[redacted]"
    assert pub["author_alias"] == ""
    assert pub["url"] == ""
    assert pub["redacted"] is True
    assert pub["rqs"] == 20  # the score and reasons still publish


def test_read_articles_keep_attribution():
    pub = _public(score_row(verdict="READ", title="A Good Post"))
    assert pub["title"] == "A Good Post"
    assert pub["author_alias"] == "alice"
    assert pub["redacted"] is False


def test_redacted_titles_never_leak_into_page_html():
    rows = [
        score_row(article_id="1", verdict="READ", title="Good Post", author_alias="alice"),
        score_row(article_id="2", verdict="SKIP", rqs=15, title="SECRETTITLE",
                  author_alias="SECRETAUTHOR"),
    ]
    html = render_page(rows)
    assert "SECRETTITLE" not in html
    assert "SECRETAUTHOR" not in html
    assert "Good Post" in html


def test_redacted_articles_are_excluded_from_the_feed():
    rows = [
        score_row(article_id="1", verdict="READ", title="Good Post"),
        score_row(article_id="2", verdict="SKIP", rqs=15, title="SECRETTITLE"),
    ]
    xml = render_feed(rows, prefs=Preferences(min_rqs=0))
    assert "SECRETTITLE" not in xml
    assert "Good Post" in xml


def test_feed_escapes_xml_metacharacters():
    xml = render_feed(
        [score_row(title="Tips & Tricks <script>", headline='He said "no" & left')],
        prefs=Preferences(min_rqs=0),
    )
    assert "<script>" not in xml
    assert "&amp;" in xml


# ------------------------------------------------------------- stats ------
def test_stats_counts_missing_signals_safely():
    """Rows that exited early have no signals; stats must not crash on them."""
    rows = [score_row(signals={}), score_row(article_id="b", verdict="SKIP", rqs=10)]
    st = _stats(rows)
    assert st["total"] == 2
    assert st["no_code"] == 2  # absent signals count as absent code


# --------------------------------------------------------- id normalisation ---
def test_article_id_is_normalised_to_the_bare_id():
    """The API returns "/content/<id>"; everything downstream keys on the bare id.

    The cache filename, the DynamoDB key, MCP's explain_score and the browser
    extension all derive ids from URLs, so a prefixed id silently breaks every
    one of those lookups.
    """
    from shouldiread.ingest import article_id_from_url
    from shouldiread.models import Article

    art = Article.from_api(
        {
            "articleId": "/content/3H40HYax3TFa9lIfgwEGhvfjrr2",
            "title": "T",
            "uri": "/content/3H40HYax3TFa9lIfgwEGhvfjrr2/slug",
            "markdownDescription": "body",
        }
    )
    assert art.article_id == "3H40HYax3TFa9lIfgwEGhvfjrr2"
    assert article_id_from_url(art.url) == art.article_id


def test_bare_article_id_passes_through_unchanged():
    from shouldiread.models import Article

    art = Article.from_api({"articleId": "ABC123", "markdownDescription": "x"})
    assert art.article_id == "ABC123"


# ------------------------------------------------------- author credibility ---
def test_hero_and_amazonian_outrank_community_at_equal_evidence():
    """Requested behaviour: accountable authors score higher, all else equal."""
    from shouldiread.config import author_bonus

    base = 60.0
    assert author_bonus("hero", base) > author_bonus("community_builder", base)
    assert author_bonus("amazonian", base) > author_bonus("community", base)
    assert author_bonus("community", base) == 0.0


def test_credibility_cannot_rescue_an_evidence_free_post():
    """The bonus breaks ties between articles that stand up; it is not a way to
    lift an empty post into the reading queue."""
    from shouldiread.config import AUTHOR_BONUS_FLOOR, author_bonus

    assert author_bonus("hero", AUTHOR_BONUS_FLOOR - 1) == 0.0
    assert author_bonus("amazonian", 5.0) == 0.0


def test_bonus_is_bounded_and_cannot_flip_skip_to_read_alone():
    from shouldiread.config import AUTHOR_BONUS, SKIM_THRESHOLD, READ_THRESHOLD

    # Even the largest bonus applied at the SKIM floor stays short of READ.
    assert SKIM_THRESHOLD + max(AUTHOR_BONUS.values()) < READ_THRESHOLD


def test_judges_never_see_author_status():
    """Credibility is applied once, visibly. If the judges also saw who wrote
    the piece it would be double-counted and invisible inside the rationales."""
    from shouldiread.agents.fleet import _signal_block
    from shouldiread.models import Article, Author

    art = Article(
        article_id="x", title="t", uri="/content/x/t", markdown="body",
        tags=["bedrock"],
        author=Author(alias="famousperson", is_aws_hero=True, is_amazon_employee=True),
    )
    block = _signal_block(art, signals())
    assert "hero" not in block.lower()
    assert "famousperson" not in block
    assert "amazon" not in block.lower()


def test_transient_classifier_covers_the_failures_seen_in_real_runs():
    """Each of these degraded a real article during a corpus run. Retrying works
    for all of them - a re-signed, re-issued request succeeds."""
    from shouldiread.agents.fleet import _is_transient

    class ClientError(Exception):
        pass

    transient = [
        ClientError("An error occurred (ThrottlingException): Too many requests"),
        ClientError("An error occurred (InvalidSignatureException): Signature expired"),
        ClientError("Read timeout on endpoint URL"),
        ClientError("Connection pool is full"),
        ClientError("An error occurred (ServiceUnavailableException)"),
    ]
    for exc in transient:
        assert _is_transient(exc), exc

    # A malformed request is not worth retrying - it will fail identically.
    assert not _is_transient(ClientError("ValidationException: input too long"))
    assert not _is_transient(ValueError("bad schema"))


def test_cohorts_report_size_and_flag_underpowered():
    """A bare median from an n=1 cohort read as a real separation and ended up
    in a draft as a '46-point' finding. Size travels with the number now."""
    from shouldiread.calibrate import MIN_COHORT, coherence_report, render_markdown

    rows = [
        {**score_row(article_id="a", rqs=90.0),
         "signals": {"code": {"output_blocks": 3}, "structure": {"code_blocks": 2, "links": 1}}},
    ] + [
        {**score_row(article_id=str(i), rqs=30.0, verdict="SKIP"),
         "signals": {"code": {"output_blocks": 0}, "structure": {"code_blocks": 0, "links": 1}}}
        for i in range(30)
    ]
    rep = coherence_report(rows)
    by_name = {c["cohort"]: c for c in rep["cohorts"]}

    assert by_name["shows real terminal output"]["n"] == 1
    assert by_name["shows real terminal output"]["underpowered"] is True
    assert by_name["no terminal output"]["n"] == 30
    assert by_name["no terminal output"]["underpowered"] is False
    assert MIN_COHORT > 1

    md = render_markdown({"matched": 0}, rep)
    assert "underpowered" in md
    assert "Do not quote them as separations" in md


# ------------------------------------------------------------- stability ----
def test_median_of_samples_ignores_an_outlier_judge():
    """One sample returned 0 for execution evidence on an article with five
    pasted terminal sessions. The median has to survive that."""
    from shouldiread.agents.fleet import _median_field, _representative, _spread

    class R:
        def __init__(self, score):
            self.score = score

    samples = [R(0), R(80), R(85)]
    assert _median_field(samples, "score", 40.0) == 80.0
    assert _spread(samples, "score") == 85.0          # disagreement is recorded
    assert _representative(samples, "score").score == 80.0


def test_median_field_falls_back_when_every_sample_failed():
    from shouldiread.agents.fleet import _median_field

    assert _median_field([], "score", 40.0) == 40.0



def test_sampling_is_configurable_and_at_least_one():
    from shouldiread.agents.fleet import JUDGE_SAMPLES

    assert JUDGE_SAMPLES >= 1


# --------------------------------------------------------------- advice ------
def scored(caps=None, raw=None, dims=None, rqs=40.0, verdict="SKIM", positives=None):
    return {
        "rqs": rqs, "verdict": verdict, "headline": "h",
        "dimensions": [{"name": k, "score": v, "rationale": "", "evidence": []}
                       for k, v in (dims or {}).items()],
        "signals": {
            "caps_applied": caps or [],
            "raw_dimension_scores": raw or {},
            "heuristic": {"positives": positives or []},
        },
    }




def test_every_cap_message_has_matching_advice():
    """Advice is keyed off the scorer's own cap text, so a new cap without a
    recommendation would silently produce a finding nobody can act on."""
    import inspect

    from shouldiread.advise import CAP_ADVICE
    from shouldiread.agents import fleet

    source = inspect.getsource(fleet.apply_caps)
    emitted = re.findall(r'cap\(\s*"[a-z_]+",\s*[\d.]+,\s*"([^"]+)"', source)
    assert emitted, "could not find cap messages to check against"
    for message in emitted:
        assert any(fragment in message for fragment, *_ in CAP_ADVICE), message




def test_articles_that_never_reached_a_judge_still_get_advice():
    """Triage-rejected and empty articles have no dimensions and no caps, so the
    cap/headroom paths produce nothing - and those authors need advice most."""
    from shouldiread.advise import recommend

    gated = {
        "rqs": 20.0, "verdict": "SKIP", "headline": "Not a technical article.",
        "dimensions": [], "signals": {
            "gate": "triage_rejected",
            "heuristic": {"negatives": ["long-form with zero code", "no outbound sources"],
                          "positives": []},
        },
    }
    out = recommend(gated)
    titles = [r["title"] for r in out["recommendations"]]
    assert titles, "a gated article must still receive recommendations"
    assert any("code" in t.lower() for t in titles)
    assert any("sources" in t.lower() for t in titles)
    # No dimension scores exist, so no gain may be invented.
    assert all(r["certainty"] == "structural" and r["gain"] == 0.0
               for r in out["recommendations"])


# ------------------------------------------------- MCP-backed publishing ----
def test_export_corpus_applies_the_attribution_policy():
    """The published site is built from this tool's output, so redaction has to
    happen server-side - not in the page renderer that consumes it."""
    import shouldiread.mcp_server as srv

    rows = [
        score_row(article_id="1", verdict="READ", title="Good Post", author_alias="alice"),
        score_row(article_id="2", verdict="SKIP", rqs=15.0, title="SECRETTITLE",
                  author_alias="SECRETAUTHOR"),
    ]
    original = srv._scores
    srv._scores = lambda: rows
    try:
        out = srv.export_corpus(min_rqs=0, limit=10)
    finally:
        srv._scores = original

    assert out["returned"] == 2
    dumped = json.dumps(out)
    assert "SECRETTITLE" not in dumped
    assert "SECRETAUTHOR" not in dumped
    assert "Good Post" in dumped


def test_export_corpus_returns_full_records_not_the_compact_form():
    """get_reading_queue returns a brief; the leaderboard needs dimensions and
    signals, so export_corpus must not be the same shape."""
    import shouldiread.mcp_server as srv

    row = score_row(article_id="1", verdict="READ")
    row["dimensions"] = [{"name": "depth", "score": 80.0, "rationale": "r", "evidence": []}]
    row["signals"] = {"structure": {"code_blocks": 3, "links": 2}, "author_bonus": 3.0}

    original = srv._scores
    srv._scores = lambda: [row]
    try:
        out = srv.export_corpus(min_rqs=0)
    finally:
        srv._scores = original

    exported = out["scores"][0]
    assert exported["dimensions"], "dimensions must survive the export"
    assert exported["signals"], "measured signals must survive the export"
    assert "stats" in out


def test_build_page_accepts_scores_from_mcp():
    """build_page must render from injected records so the publish step can feed
    it MCP output instead of reading the store directly."""

    rows = [
        score_row(article_id="1", verdict="READ", title="From MCP", author_alias="alice"),
        score_row(article_id="2", verdict="SKIP", rqs=12.0, title="LEAKME",
                  author_alias="LEAKAUTHOR"),
    ]
    out = tmp_path_page(rows)
    assert "From MCP" in out
    assert "LEAKME" not in out
    assert "LEAKAUTHOR" not in out


def tmp_path_page(rows):
    import tempfile
    from pathlib import Path

    from shouldiread.publish import build_page

    with tempfile.TemporaryDirectory() as d:
        target = Path(d) / "index.html"
        build_page(output=str(target), scores=rows)
        return target.read_text()



def test_adviser_survives_an_unknown_dimension():
    from shouldiread.advise import recommend

    out = recommend({
        "rqs": 50.0, "verdict": "SKIM", "headline": "h",
        "dimensions": [{"name": "some_future_pillar", "score": 10.0,
                        "rationale": "", "evidence": []}],
        "signals": {"caps_applied": [], "raw_dimension_scores": {}},
    })
    assert isinstance(out["recommendations"], list)


# =========================================================================
# The reworked rubric: "is this worth my time?", not "prove you ran it".
# =========================================================================









def test_prompts_tell_judges_to_calibrate_by_content_type():
    from shouldiread.agents import prompts

    flat = " ".join(prompts.SHARED_RULES.split())
    assert "what it is TRYING to be" in flat
    assert "no code, no terminal output and no" in flat




# =========================================================================
# Fifteen dimensions, four judges.
# =========================================================================
def test_fifteen_dimensions_grouped_into_four_families():
    from shouldiread.config import DIMENSION_FAMILIES, WEIGHTS

    assert len(WEIGHTS) == 15
    assert sum(WEIGHTS.values()) == 100
    assert len(DIMENSION_FAMILIES) == 4
    covered = {d for dims in DIMENSION_FAMILIES.values() for d in dims}
    assert covered == set(WEIGHTS), "every dimension must belong to exactly one family"
    assert sum(len(v) for v in DIMENSION_FAMILIES.values()) == 15, "no dimension twice"


def test_granularity_costs_no_extra_model_calls():
    """Fifteen dimensions, but one judge per family - the schemas carry the rest."""
    from shouldiread.agents import AwsResult, ContentResult, EvidenceResult, ReaderResult
    from shouldiread.config import DIMENSION_FAMILIES

    schemas = {
        "content": ContentResult, "aws": AwsResult,
        "evidence": EvidenceResult, "reader": ReaderResult,
    }
    for family, dimensions in DIMENSION_FAMILIES.items():
        fields = schemas[family].model_fields
        for dimension in dimensions:
            assert dimension in fields, f"{family} judge cannot return {dimension}"


def test_every_dimension_has_a_label_and_advice():
    import inspect

    from shouldiread.advise import DIMENSION_LABELS, _headroom_recommendations
    from shouldiread.config import WEIGHTS

    source = inspect.getsource(_headroom_recommendations)
    for dimension in WEIGHTS:
        assert dimension in DIMENSION_LABELS, f"no label for {dimension}"
        assert f'"{dimension}"' in source, f"no headroom advice for {dimension}"


def test_aws_family_carries_the_platform_specific_dimensions():
    """These are what make it an AWS rubric rather than a generic blog rubric."""
    from shouldiread.config import DIMENSION_FAMILIES

    aws = set(DIMENSION_FAMILIES["aws"])
    assert {"aws_service_depth", "architecture_quality", "well_architected",
            "currency", "cost_awareness"} == aws


def test_no_code_does_not_penalise_a_codeless_piece():
    capped, notes = apply_caps(
        ALL_HIGH,
        signals(structure={"code_blocks": 0, "code_loc": 0, "words": 900, "measurements": 4}),
        "explainer",
    )
    assert capped["substance"] == 90
    assert capped["insight"] == 90
    assert not any("code_quality" in n for n in notes)


def test_nonexistent_apis_hit_code_quality_and_accuracy():
    capped, _ = apply_caps(
        ALL_HIGH, signals(aws_apis={"total_invalid": 2}), "tutorial"
    )
    assert capped["code_quality"] <= 29
    assert capped["accuracy"] <= 35
    assert capped["currency"] <= 40


def test_sourcing_demanded_by_content_type_only():
    no_links = signals(structure={"links": 0})
    opinion, _ = apply_caps(ALL_HIGH, no_links, "opinion")
    reference, _ = apply_caps(ALL_HIGH, no_links, "reference")
    assert opinion["sources"] == 90
    assert reference["sources"] <= 50


def test_every_family_prompt_has_bands_for_each_of_its_dimensions():
    from shouldiread.agents import prompts
    from shouldiread.config import DIMENSION_FAMILIES

    prompt_for = {
        "content": prompts.CONTENT, "aws": prompts.AWS,
        "evidence": prompts.EVIDENCE, "reader": prompts.READER,
    }
    for family, dimensions in DIMENSION_FAMILIES.items():
        text = prompt_for[family]
        for dimension in dimensions:
            assert f"**{dimension}**" in text, f"{family} prompt does not define {dimension}"
        assert "85-100" in text and "0-9" in text, f"{family} prompt lacks anchored bands"
