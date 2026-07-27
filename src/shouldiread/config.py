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
WEIGHTS = {
    "execution_evidence": 30,
    "code_substance": 25,
    "source_integrity": 20,
    "depth": 15,
    "originality": 10,
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
