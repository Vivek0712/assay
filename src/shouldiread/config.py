"""Central configuration: endpoints, models, scoring weights, thresholds."""

from __future__ import annotations

import os
from pathlib import Path

# --- Builder Center endpoints -------------------------------------------------
# All public. The article API accepts the literal anonymous session token that
# builder.aws.com itself sends for logged-out readers; there is no login here.
SITE = "https://builder.aws.com"
API = "https://api.builder.aws.com"

SITEMAP_INDEX = f"{SITE}/sitemaps/sitemap.xml"
ATOM_FEED = f"{SITE}/rss"
ARTICLE_API = f"{API}/cs/v2/articles"

ANON_TOKEN = "dummy"
USER_AGENT = "shouldiread/0.1 (+https://builder.aws.com; personal reading-triage agent)"

REQUEST_HEADERS = {
    "builder-session-token": ANON_TOKEN,
    "Referer": f"{SITE}/",
    "User-Agent": USER_AGENT,
}

# Politeness. The site is a community resource, not a load test target.
MAX_CONCURRENCY = int(os.environ.get("SIR_MAX_CONCURRENCY", "6"))
REQUEST_DELAY_S = float(os.environ.get("SIR_REQUEST_DELAY", "0.15"))
REQUEST_TIMEOUT_S = 30.0
MAX_RETRIES = 4

# --- Bedrock ------------------------------------------------------------------
# Verified on profile `heisenberg` (643603452951, us-east-1): Nova invokes fine,
# every Anthropic inference profile returns ResourceNotFoundException. Nova only.
AWS_PROFILE = os.environ.get("SIR_AWS_PROFILE", "heisenberg")
AWS_REGION = os.environ.get("SIR_AWS_REGION", "us-east-1")

MODEL_TRIAGE = "global.amazon.nova-2-lite-v1:0"  # cheap gate, runs on everything
MODEL_JUDGE = "us.amazon.nova-pro-v1:0"  # deep judge, only past the gate

# --- Scoring ------------------------------------------------------------------
# Weights sum to 100. Execution evidence dominates on purpose: the whole premise
# is that the expensive-to-fake signal is having actually run the thing.
# Fifteen dimensions, grouped into four families. The question every one of them
# serves is the reader's - "is this worth my time?" - not "prove you ran it".
#
# Fifteen scored dimensions, but only FOUR judge calls: each judge returns its
# whole family in one structured response. Granularity for the author-facing
# advice, without paying for it in tokens.
DIMENSION_FAMILIES: dict[str, list[str]] = {
    "content": ["substance", "explanation_depth", "insight", "accuracy", "scope_discipline"],
    "aws": ["aws_service_depth", "architecture_quality", "well_architected",
            "currency", "cost_awareness"],
    "evidence": ["evidence", "sources", "code_quality"],
    "reader": ["clarity", "actionability"],
}

WEIGHTS = {
    # --- content: is there anything here? (35) ---
    "substance": 9,            # real technical content vs padding
    "explanation_depth": 7,    # mechanism, or surface description
    "insight": 10,             # what you could not get from the docs
    "accuracy": 6,             # is it correct
    "scope_discipline": 3,     # does it deliver what the title promises

    # --- AWS: is it useful to an AWS builder? (28) ---
    "aws_service_depth": 10,   # operating a service vs naming it
    "architecture_quality": 7, # is the design sound and reasoned
    "well_architected": 5,     # security, reliability, performance, ops
    "currency": 3,             # current with the service as it is today
    "cost_awareness": 3,       # does it engage with what this costs

    # --- evidence: can it be trusted? (22) ---
    "evidence": 9,             # verifiable artefacts, proportionate to claims
    "sources": 6,              # citations for factual claims
    "code_quality": 7,         # if code is present, is it usable and correct

    # --- reader: can it be used? (15) ---
    "clarity": 8,              # can a reader follow it
    "actionability": 7,        # can a reader do something with it
}

READ_THRESHOLD = 70
SKIM_THRESHOLD = 40

# --- Author credibility -------------------------------------------------------
# An explicit, auditable bonus added to the finished RQS for authors who carry
# accountability for what they publish: AWS Heroes are vetted and renewed
# annually, Amazon employees write under their employer's name.
#
# Deliberately applied *here* rather than inside the judges, and the author's
# status is deliberately withheld from the judges' evidence block. If a judge
# knew who wrote a piece it would quietly inflate five separate rationales and
# there would be no way to see how much of a score was credential and how much
# was content. As one number it is visible, tunable, and removable.
AUTHOR_BONUS = {
    "hero": float(os.environ.get("SIR_BONUS_HERO", "8")),
    "amazonian": float(os.environ.get("SIR_BONUS_AMAZONIAN", "6")),
    "community_builder": float(os.environ.get("SIR_BONUS_CB", "3")),
    "community": 0.0,
}

# The bonus is a tie-breaker between articles that already stand up, not a way
# to lift an evidence-free post into the reading queue. Below this base score no
# bonus applies. Set SIR_BONUS_FLOOR=0 to make it unconditional.
AUTHOR_BONUS_FLOOR = float(os.environ.get("SIR_BONUS_FLOOR", str(SKIM_THRESHOLD)))


def author_bonus(author_kind: str, base_rqs: float) -> float:
    """Credibility bonus for one author class, given the evidence-only score."""
    if base_rqs < AUTHOR_BONUS_FLOOR:
        return 0.0
    return AUTHOR_BONUS.get(author_kind, 0.0)

VERDICT_READ = "READ"
VERDICT_SKIM = "SKIM"
VERDICT_SKIP = "SKIP"


def verdict_for(rqs: float) -> str:
    if rqs >= READ_THRESHOLD:
        return VERDICT_READ
    if rqs >= SKIM_THRESHOLD:
        return VERDICT_SKIM
    return VERDICT_SKIP


# --- Local paths --------------------------------------------------------------
ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
CACHE_DIR = DATA / "cache"
CORPUS_DIR = DATA / "corpus"
SCORES_DIR = DATA / "scores"
FIXTURES_DIR = ROOT / "tests" / "fixtures"
