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
    }
    for section, values in over.items():
        base[section] = {**base[section], **values}
    return base


ALL_HIGH = {k: 90.0 for k in WEIGHTS}


# ------------------------------------------------------------------ rqs ---
def test_rqs_is_weighted_mean():
    assert compute_rqs({k: 100.0 for k in WEIGHTS}) == pytest.approx(100.0)
    assert compute_rqs({k: 0.0 for k in WEIGHTS}) == pytest.approx(0.0)
    assert compute_rqs({k: 50.0 for k in WEIGHTS}) == pytest.approx(50.0)


def test_execution_evidence_carries_the_most_weight():
    """The premise of the whole project: doing the work matters most."""
    assert WEIGHTS["execution_evidence"] == max(WEIGHTS.values())
    only_exec = {k: (100.0 if k == "execution_evidence" else 0.0) for k in WEIGHTS}
    only_orig = {k: (100.0 if k == "originality" else 0.0) for k in WEIGHTS}
    assert compute_rqs(only_exec) > compute_rqs(only_orig)


def test_verdict_thresholds():
    assert verdict_for(70) == "READ"
    assert verdict_for(69.9) == "SKIM"
    assert verdict_for(40) == "SKIM"
    assert verdict_for(39.9) == "SKIP"


# ----------------------------------------------------------------- caps ---
def test_code_score_capped_without_code():
    capped, notes = apply_caps(ALL_HIGH, signals(structure={"code_blocks": 0}), "tutorial")
    assert capped["code_substance"] <= 20
    assert any("no code blocks" in n for n in notes)


def test_code_cap_relaxed_for_opinion_pieces():
    """An opinion piece is not a failed tutorial."""
    capped, _ = apply_caps(ALL_HIGH, signals(structure={"code_blocks": 0}), "opinion")
    assert capped["code_substance"] == 55


def test_execution_score_capped_without_artefacts():
    s = signals(structure={"terminal_evidence": 0, "images": 0, "measurements": 0},
                code={"output_blocks": 0})
    capped, _ = apply_caps(ALL_HIGH, s, "tutorial")
    assert capped["execution_evidence"] <= 25


def test_execution_score_survives_with_terminal_output():
    s = signals(code={"output_blocks": 5}, structure={"terminal_evidence": 5})
    capped, _ = apply_caps(ALL_HIGH, s, "tutorial")
    assert capped["execution_evidence"] == 90


def test_invented_api_caps_code_and_sources():
    """An API that does not exist is disqualifying for both dimensions."""
    s = signals(aws_apis={"total_invalid": 2, "invalid_calls": ["s3.turbo_upload_object"]})
    capped, notes = apply_caps(ALL_HIGH, s, "tutorial")
    assert capped["code_substance"] <= 25
    assert capped["source_integrity"] <= 25
    assert any("do not exist" in n for n in notes)


def test_zero_links_caps_sources():
    capped, _ = apply_caps(ALL_HIGH, signals(structure={"links": 0}), "tutorial")
    assert capped["source_integrity"] <= 30


def test_foreign_duplicate_destroys_originality():
    s = signals(duplicates={"has_foreign_duplicate": True, "max_similarity": 0.95})
    capped, _ = apply_caps(ALL_HIGH, s, "tutorial")
    assert capped["originality"] <= 10


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


def test_provenance_prompt_has_anchored_score_bands():
    """Unanchored, the provenance judge disagreed with itself by 40-80 points on
    identical input. Explicit bands took the whole-article spread from 32 to 1.5."""
    from shouldiread.agents import prompts

    text = prompts.PROVENANCE
    assert "SCORE BANDS" in text
    for band in ("85-100", "70-84", "50-69", "30-49", "10-29", "0-9"):
        assert band in text, band
    # The bands must be tied to the measured counts, not to impressions.
    assert "MEASURED SIGNALS" in text
    assert "cannot be above 29" in text


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


def test_cap_cost_is_exact_arithmetic():
    """A cap holding sources at 30 when the judge wanted 85 costs exactly
    (85-30) * 20/100 = 11.0 RQS. Not an estimate."""
    from shouldiread.advise import recommend

    out = recommend(scored(
        caps=["source_integrity capped at 30: no outbound sources at all"],
        raw={"source_integrity": 85.0},
        dims={"source_integrity": 30.0},
    ))
    rec = next(r for r in out["recommendations"] if r["dimension"] == "source_integrity")
    assert rec["certainty"] == "exact"
    assert rec["gain"] == 11.0


def test_recommendations_are_ranked_by_value():
    from shouldiread.advise import recommend

    out = recommend(scored(
        caps=[
            "source_integrity capped at 30: no outbound sources at all",
            "execution_evidence capped at 25: no terminal output, screenshots or measurements",
        ],
        raw={"source_integrity": 50.0, "execution_evidence": 90.0},
        dims={"source_integrity": 30.0, "execution_evidence": 25.0},
    ))
    gains = [r["gain"] for r in out["recommendations"]]
    assert gains == sorted(gains, reverse=True)
    # execution evidence is weighted 30 and is 65 points below where the judge
    # put it, so it must outrank the sources fix.
    assert out["recommendations"][0]["dimension"] == "execution_evidence"


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


def test_reachable_score_and_next_band_are_reported():
    from shouldiread.advise import recommend

    out = recommend(scored(
        rqs=32.0, verdict="SKIP",
        caps=["source_integrity capped at 30: no outbound sources at all"],
        raw={"source_integrity": 80.0},
        dims={"source_integrity": 30.0},
    ))
    assert out["points_to_next_band"]["band"] == "SKIM"
    assert out["points_to_next_band"]["points"] == 8.0
    assert out["reachable_rqs"] > out["rqs"]


def test_a_strong_article_gets_no_busywork():
    from shouldiread.advise import recommend

    out = recommend(scored(
        rqs=88.0, verdict="READ",
        dims={k: 90.0 for k in ("execution_evidence", "code_substance",
                                "source_integrity", "depth", "originality")},
        positives=["8 pasted terminal session(s)"],
    ))
    assert out["recommendations"] == []
    assert "good shape" in __import__("shouldiread.advise", fromlist=["render_text"]).render_text(out)


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
    from shouldiread.publish import build_page

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
