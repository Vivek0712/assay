"""Artefacts that show the work exists.

The first rubric treated a pasted terminal session as the only credible proof
that an author had actually done the thing. A review council of four
constituencies - developer advocate, Hero, working practitioner, learner - all
independently flagged that as too narrow, and a real case made it concrete: a
substantial physical-AI write-up linked two working repositories, one the
author's own, and was scored as if nothing had been built.

A shipped repository is arguably *stronger* evidence than a transcript. A
transcript is a claim about what happened once on someone else's machine; a
repository is inspectable, runnable, and still there tomorrow.

So this counts the durable artefacts: repositories, live demos, published
packages, diagrams. It does not replace terminal output - a transcript still
shows a reader what the thing does when it runs, which a repo link does not.
Both count; neither alone is required.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlparse

from .markdown_tools import IMAGE_RE, LINK_RE, strip_code

REPO_HOSTS = ("github.com", "gitlab.com", "bitbucket.org", "codeberg.org")
PACKAGE_HOSTS = ("pypi.org", "npmjs.com", "crates.io", "hub.docker.com", "gallery.ecr.aws")
DEMO_HOSTS = ("youtube.com", "youtu.be", "vimeo.com", "loom.com", "asciinema.org")

# A repo URL carries an owner; matching it against the author's alias is what
# separates "I built this" from "here is somebody else's library".
_REPO_PATH_RE = re.compile(r"^/([\w.-]+)/([\w.-]+)")

# Architecture diagrams are usually named, or drawn in a fenced mermaid block.
DIAGRAM_HINT_RE = re.compile(
    r"(architecture|sequence|topology|data[- ]flow|state)\s+diagram|```mermaid", re.I
)


@dataclass
class ArtifactReport:
    repos: list[str] = field(default_factory=list)
    own_repos: list[str] = field(default_factory=list)
    packages: list[str] = field(default_factory=list)
    demos: list[str] = field(default_factory=list)
    hero_image: bool = False
    diagrams: int = 0

    @property
    def total(self) -> int:
        return (
            len(self.repos) + len(self.packages) + len(self.demos)
            + int(self.hero_image) + self.diagrams
        )

    @property
    def has_durable_artifact(self) -> bool:
        """A repository, package or live demo - something a reader can go and use."""
        return bool(self.repos or self.packages or self.demos)

    @property
    def is_authors_own(self) -> bool:
        """The artefact belongs to the author, so it evidences *their* work."""
        return bool(self.own_repos)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["total"] = self.total
        d["has_durable_artifact"] = self.has_durable_artifact
        d["is_authors_own"] = self.is_authors_own
        return d


def _norm(alias: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (alias or "").lower())


def find_artifacts(
    markdown: str,
    *,
    author_alias: str = "",
    hero_image_url: str | None = None,
) -> ArtifactReport:
    """Durable artefacts referenced by an article."""
    report = ArtifactReport(hero_image=bool(hero_image_url))
    prose = strip_code(markdown)

    urls = [u for _, u in LINK_RE.findall(prose)]
    urls += [u for _, u in IMAGE_RE.findall(markdown) if u.startswith("http")]
    # Repos are frequently named in prose without being linked.
    urls += [f"https://{m}" for m in re.findall(r"(?:github|gitlab)\.com/[\w.-]+/[\w.-]+", markdown)]

    alias = _norm(author_alias)
    seen: set[str] = set()
    for url in urls:
        try:
            parsed = urlparse(url)
        except ValueError:
            continue
        host = (parsed.netloc or "").lower().removeprefix("www.")
        key = f"{host}{parsed.path}".rstrip("/")
        if key in seen:
            continue
        seen.add(key)

        if any(h in host for h in REPO_HOSTS):
            match = _REPO_PATH_RE.match(parsed.path)
            if not match:
                continue
            report.repos.append(key)
            owner = _norm(match.group(1))
            # Owner matching is deliberately loose: Builder Center aliases and
            # GitHub handles are usually close but rarely identical.
            if alias and (alias in owner or owner in alias):
                report.own_repos.append(key)
        elif any(h in host for h in PACKAGE_HOSTS):
            report.packages.append(key)
        elif any(h in host for h in DEMO_HOSTS):
            report.demos.append(key)

    report.diagrams = len(DIAGRAM_HINT_RE.findall(markdown))
    return report
