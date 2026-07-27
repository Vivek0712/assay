"""Cross-post and near-duplicate detection.

Three distinct things this catches:

1. Declared cross-posts - the API hands us `externalCanonicalUrl` outright.
2. Self-recycling - the same author reposting the same material with a new title.
3. Content laundering - the same body published under *different* authors, which
   is the one that actually matters and the one no engagement metric reveals.

MinHash over word shingles, implemented directly rather than pulled in as a
dependency: it is about forty lines and keeps the Lambda bundle small.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .markdown_tools import strip_code

SHINGLE_SIZE = 7
NUM_HASHES = 128
_MASK = (1 << 61) - 1

_WORD_RE = re.compile(r"[a-z0-9]+")


def _normalise(md: str) -> list[str]:
    """Prose words, lowercased. Code is excluded: shared boilerplate snippets
    would otherwise make unrelated tutorials look like copies of each other."""
    return _WORD_RE.findall(strip_code(md).lower())


def _shingles(words: list[str], k: int = SHINGLE_SIZE) -> set[str]:
    if len(words) < k:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i : i + k]) for i in range(len(words) - k + 1)}


def _hash_seeds(n: int = NUM_HASHES) -> list[tuple[int, int]]:
    # Deterministic (a, b) pairs for the universal hash family h(x) = a*x + b.
    seeds = []
    for i in range(n):
        d = hashlib.blake2b(str(i).encode(), digest_size=16).digest()
        a = int.from_bytes(d[:8], "big") | 1
        b = int.from_bytes(d[8:], "big")
        seeds.append((a & _MASK, b & _MASK))
    return seeds


_SEEDS = _hash_seeds()


def minhash(md: str) -> tuple[int, ...]:
    """MinHash signature of an article's prose."""
    sh = _shingles(_normalise(md))
    if not sh:
        return tuple([0] * NUM_HASHES)
    base = [int.from_bytes(hashlib.blake2b(s.encode(), digest_size=8).digest(), "big") for s in sh]
    return tuple(min(((a * h + b) & _MASK) for h in base) for a, b in _SEEDS)


def similarity(sig_a: Iterable[int], sig_b: Iterable[int]) -> float:
    a, b = tuple(sig_a), tuple(sig_b)
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(1 for x, y in zip(a, b) if x == y) / len(a)


@dataclass
class DuplicateMatch:
    article_id: str
    title: str
    author_alias: str
    similarity: float
    same_author: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "article_id": self.article_id,
            "title": self.title,
            "author_alias": self.author_alias,
            "similarity": round(self.similarity, 3),
            "same_author": self.same_author,
        }


@dataclass
class DuplicateReport:
    is_cross_post: bool = False
    canonical_url: str | None = None
    canonical_host: str | None = None
    near_duplicates: list[DuplicateMatch] = field(default_factory=list)

    @property
    def max_similarity(self) -> float:
        return max((m.similarity for m in self.near_duplicates), default=0.0)

    @property
    def has_foreign_duplicate(self) -> bool:
        """Same body, different author. The serious one."""
        return any(not m.same_author for m in self.near_duplicates)

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_cross_post": self.is_cross_post,
            "canonical_url": self.canonical_url,
            "canonical_host": self.canonical_host,
            "near_duplicates": [m.to_dict() for m in self.near_duplicates][:5],
            "max_similarity": round(self.max_similarity, 3),
            "has_foreign_duplicate": self.has_foreign_duplicate,
        }


class DuplicateIndex:
    """In-memory corpus index. Add every article, then query for duplicates."""

    def __init__(self, threshold: float = 0.55) -> None:
        self.threshold = threshold
        self._sigs: dict[str, tuple[int, ...]] = {}
        self._meta: dict[str, tuple[str, str]] = {}  # id -> (title, author)

    def add(self, article_id: str, md: str, title: str = "", author: str = "") -> None:
        self._sigs[article_id] = minhash(md)
        self._meta[article_id] = (title, author)

    def __len__(self) -> int:
        return len(self._sigs)

    def query(self, article_id: str, md: str, author: str = "") -> list[DuplicateMatch]:
        sig = self._sigs.get(article_id) or minhash(md)
        out: list[DuplicateMatch] = []
        for other_id, other_sig in self._sigs.items():
            if other_id == article_id:
                continue
            score = similarity(sig, other_sig)
            if score >= self.threshold:
                title, other_author = self._meta.get(other_id, ("", ""))
                out.append(
                    DuplicateMatch(
                        article_id=other_id,
                        title=title,
                        author_alias=other_author,
                        similarity=score,
                        same_author=bool(author) and other_author == author,
                    )
                )
        return sorted(out, key=lambda m: m.similarity, reverse=True)


def cross_post_check(
    md: str,
    *,
    article_id: str = "",
    author_alias: str = "",
    external_canonical_url: str | None = None,
    index: DuplicateIndex | None = None,
) -> DuplicateReport:
    """Declared cross-post status plus near-duplicate hits against the corpus."""
    from urllib.parse import urlparse

    report = DuplicateReport(
        is_cross_post=bool(external_canonical_url),
        canonical_url=external_canonical_url,
        canonical_host=(
            urlparse(external_canonical_url).netloc or None if external_canonical_url else None
        ),
    )
    if index is not None and article_id:
        report.near_duplicates = index.query(article_id, md, author_alias)
    return report
