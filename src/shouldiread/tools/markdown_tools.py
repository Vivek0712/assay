"""Structural analysis of the raw markdown: code blocks, shape, placeholders.

None of this needs a model. Counting fenced blocks and headings is exact, and
the structural signals turn out to separate real write-ups from filler far more
reliably than any keyword list does.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})[ \t]*([A-Za-z0-9_+-]*)[ \t]*$", re.M)
INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
ATX_HEADING_RE = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]+(\S.*)$", re.M)
SETEXT_RE = re.compile(r"^(?!\s*$)(.+)\n[ \t]{0,3}(=+|-{2,})[ \t]*$", re.M)
# A whole line that is nothing but bold text: a heading typed by someone who
# does not know markdown, and a reliable tell for generated prose.
BOLD_LINE_RE = re.compile(r"^[ \t]{0,3}\*\*([^*\n]{2,120})\*\*:?[ \t]*$", re.M)
IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)")
LINK_RE = re.compile(r"(?<!!)\[([^\]]*)\]\((https?://[^)\s]+)")
LIST_ITEM_RE = re.compile(r"^[ \t]{0,4}([-*+]|\d+\.)[ \t]+\S", re.M)
TABLE_RE = re.compile(r"^\|.+\|[ \t]*$", re.M)

# Values a reader must replace before the snippet can run.
PLACEHOLDER_RE = re.compile(
    r"""(
        YOUR[_\- ][A-Z0-9_\- ]{2,}          # YOUR_BUCKET_NAME
      | <[a-z0-9][a-z0-9_\- ]{1,30}>        # <region>, <your-bucket>
      | \{\{[^}]{1,40}\}\}                  # {{account_id}}
      | \bACCOUNT[_\-]?ID\b
      | \b(?:123456789012|111122223333|000000000000)\b   # doc-style account ids
      | \bxxxx+\b
      | \bREPLACE[_\- ]?(?:ME|WITH)\b
      | \bTODO\b
      | \bexample\.com\b
      | \bmy-bucket\b
    )""",
    re.X | re.I,
)

# A line beginning with a shell prompt. Covers the four shapes that actually
# turn up in published transcripts, including `[root@ip-172-31-44-77 ~]#`,
# which is the form real EC2 sessions produce.
PROMPT_LINE_RE = re.compile(
    r"""^[ \t]*(?:
        \[[^\]\n]{1,60}\][ \t]*[#$]          # [root@ip-172-31-44-77 ~]#
      | [\w.\-]+@[\w.\-]+[^\n]{0,60}?[#$]    # ubuntu@host:~$
      | PS[ ][A-Za-z]:[^\n]*>                # PS C:\>
      | [$❯»]                                # bare $ prompt
      | >>>                                  # python REPL
    )[ \t]+\S""",
    re.X | re.M,
)

# Output that is expensive to fabricate convincingly: real consoles, real errors.
TERMINAL_MARKERS = re.compile(
    r"""(
        ^[ \t]*\[[^\]\n]{1,60}\][ \t]*[#$][ \t]+\S   # bracketed root/user prompt
      | ^[ \t]*[\w.\-]+@[\w.\-]+[^\n]{0,60}?[#$][ \t]+\S
      | ^[ \t]*[$❯»][ \t]+\S                 # bare shell prompt
      | ^[ \t]*>>>[ \t]+\S                   # python REPL
      | Traceback\ \(most\ recent\ call\ last\)
      | ^\s*at\ [A-Za-z0-9_.$]+\(            # java/js stack frame
      | botocore\.exceptions\.\w+
      | \b[A-Z][A-Za-z]*(?:Exception|Error)\b:
      | HTTP/\d\.\d\s+\d{3}
      | \b(?:AccessDenied|ResourceNotFoundException|ThrottlingException|ValidationException)\b
    )""",
    re.X | re.M,
)

# Numbers with units: timings, costs, sizes. Someone who ran it tends to have these.
MEASUREMENT_RE = re.compile(
    r"""(
        \$\s?\d+(?:[.,]\d+)?                       # $0.42
      | \b\d+(?:\.\d+)?\s?(?:ms|milliseconds|s|sec|secs|seconds|min|mins|minutes|hours?)\b
      | \b\d+(?:\.\d+)?\s?(?:KB|MB|GB|TB|KiB|MiB|GiB)\b
      | \b\d+(?:\.\d+)?\s?(?:rps|qps|tps|req/s|iops)\b
      # No trailing \b after % - it is already a non-word character, so requiring
      # a boundary meant "64%" only counted when followed by a letter or digit.
      # Inside a table cell or bold markers ("**64%**") it silently missed.
      | \b\d+(?:\.\d+)?\s?%
    )""",
    re.X | re.I,
)


@dataclass
class CodeBlock:
    lang: str
    body: str
    index: int

    @property
    def loc(self) -> int:
        return len([ln for ln in self.body.splitlines() if ln.strip()])


@dataclass
class MarkdownStats:
    words: int = 0
    code_blocks: int = 0
    code_loc: int = 0
    code_langs: dict[str, int] = field(default_factory=dict)
    inline_code: int = 0
    atx_headings: int = 0
    setext_headings: int = 0
    bold_pseudo_headings: int = 0
    images: int = 0
    links: int = 0
    list_items: int = 0
    tables: int = 0
    code_to_prose: float = 0.0
    terminal_evidence: int = 0
    measurements: int = 0
    placeholders: int = 0
    placeholder_density: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(self)


def strip_code(md: str) -> str:
    """Markdown with fenced blocks removed, for prose-only measurements."""
    out, in_fence, fence = [], False, ""
    for line in md.splitlines():
        m = FENCE_RE.match(line)
        if m:
            if not in_fence:
                in_fence, fence = True, m.group(1)[0]
            elif m.group(1)[0] == fence:
                in_fence = False
            continue
        if not in_fence:
            out.append(line)
    return "\n".join(out)


def extract_code_blocks(md: str) -> list[CodeBlock]:
    """Every fenced code block, in document order.

    Handles ``` and ~~~ fences of any length, and tolerates the unclosed final
    fence that shows up in a surprising amount of published content.
    """
    blocks: list[CodeBlock] = []
    lines = md.splitlines()
    i, idx = 0, 0
    while i < len(lines):
        m = FENCE_RE.match(lines[i])
        if not m:
            i += 1
            continue
        char, lang = m.group(1)[0], (m.group(2) or "").lower()
        body: list[str] = []
        i += 1
        while i < len(lines):
            close = FENCE_RE.match(lines[i])
            if close and close.group(1)[0] == char and not close.group(2):
                break
            body.append(lines[i])
            i += 1
        i += 1
        blocks.append(CodeBlock(lang=lang, body="\n".join(body), index=idx))
        idx += 1
    return blocks


def placeholder_density(md: str) -> tuple[int, float]:
    """(count, per-100-LOC) across code blocks only.

    High density means the snippet was written to be looked at, not run.
    """
    blocks = extract_code_blocks(md)
    loc = sum(b.loc for b in blocks)
    hits = sum(len(PLACEHOLDER_RE.findall(b.body)) for b in blocks)
    return hits, (hits * 100.0 / loc if loc else 0.0)


def structure_stats(md: str) -> MarkdownStats:
    """Every structural signal in one pass."""
    prose = strip_code(md)
    blocks = extract_code_blocks(md)

    langs: dict[str, int] = {}
    for b in blocks:
        langs[b.lang or "none"] = langs.get(b.lang or "none", 0) + 1

    code_loc = sum(b.loc for b in blocks)
    prose_words = len(prose.split())
    ph_count, ph_density = placeholder_density(md)
    code_text = "\n".join(b.body for b in blocks)

    return MarkdownStats(
        words=len(md.split()),
        code_blocks=len(blocks),
        code_loc=code_loc,
        code_langs=langs,
        inline_code=len(INLINE_CODE_RE.findall(prose)),
        atx_headings=len(ATX_HEADING_RE.findall(prose)),
        setext_headings=len(SETEXT_RE.findall(prose)),
        bold_pseudo_headings=len(BOLD_LINE_RE.findall(prose)),
        images=len(IMAGE_RE.findall(md)),
        links=len(LINK_RE.findall(prose)),
        list_items=len(LIST_ITEM_RE.findall(prose)),
        tables=len(TABLE_RE.findall(prose)),
        code_to_prose=round(code_loc / prose_words, 4) if prose_words else 0.0,
        # Terminal output usually sits inside fences; measurements usually don't.
        terminal_evidence=len(TERMINAL_MARKERS.findall(code_text)),
        measurements=len(MEASUREMENT_RE.findall(prose)),
        placeholders=ph_count,
        placeholder_density=round(ph_density, 2),
    )
