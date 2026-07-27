"""Does the code in the article actually parse?

Every check here is syntax-only. Nothing in a scored article is ever executed:
`ast.parse` builds a tree without running it, and `bash -n` / `node --check`
are the interpreters' own no-execute syntax modes. Snippets come from strangers
on the internet, so running them is off the table by construction.

A fragment failing to parse is not damning on its own - authors legitimately
show three lines out of context. What matters is the pattern: a long article
whose every "complete" snippet is malformed was not run by anybody.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .markdown_tools import PROMPT_LINE_RE, CodeBlock, extract_code_blocks

PY_LANGS = {"python", "py", "python3"}
SH_LANGS = {"bash", "sh", "shell", "zsh", "console", "terminal"}
JS_LANGS = {"javascript", "js", "node", "typescript", "ts"}
JSON_LANGS = {"json"}
YAML_LANGS = {"yaml", "yml", "cloudformation", "sam"}
HCL_LANGS = {"terraform", "tf", "hcl"}

_TIMEOUT = 8

# A block that only shows output, not code. Penalising these for "not parsing"
# would be backwards - they are execution evidence.
OUTPUT_ONLY_RE = re.compile(
    r"^\s*(?:Traceback|\w+Error|\w+Exception|HTTP/)", re.M
)


@dataclass
class BlockResult:
    index: int
    lang: str
    loc: int
    kind: str  # python | shell | js | json | yaml | hcl | text | output
    checked: bool = False
    ok: bool = False
    error: str = ""
    complete: bool = False  # looks like a whole file, not a fragment

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CodeReport:
    blocks: list[BlockResult] = field(default_factory=list)
    checked: int = 0
    passed: int = 0
    failed: int = 0
    output_blocks: int = 0
    complete_files: int = 0

    @property
    def pass_rate(self) -> float:
        return self.passed / self.checked if self.checked else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "passed": self.passed,
            "failed": self.failed,
            "pass_rate": round(self.pass_rate, 3),
            "output_blocks": self.output_blocks,
            "complete_files": self.complete_files,
            "failures": [
                b.to_dict() for b in self.blocks if b.checked and not b.ok
            ][:10],
        }


def _classify(block: CodeBlock) -> str:
    lang = block.lang
    if lang in PY_LANGS:
        return "python"
    if lang in JSON_LANGS:
        return "json"
    if lang in YAML_LANGS:
        return "yaml"
    if lang in HCL_LANGS:
        return "hcl"
    if lang in JS_LANGS:
        return "js"
    if lang in SH_LANGS:
        # A prompt-prefixed line means this is a terminal transcript, not a
        # script: nobody puts `$ ` in front of commands in a file they run. The
        # interleaved output would never parse, and shouldn't be asked to.
        if PROMPT_LINE_RE.search(block.body):
            return "output"
        if OUTPUT_ONLY_RE.match(block.body.strip()):
            return "output"
        return "shell"
    if not lang:
        # Unlabelled: infer, conservatively.
        body = block.body.strip()
        if not body:
            return "text"
        # Prompt detection must come first. A pasted session that opens with a
        # bracketed prompt - `[root@ip-172-31-44-77 ~]# pvs` - starts with "[",
        # so a JSON check placed ahead of this reads the best execution evidence
        # in the corpus as malformed JSON and counts it against the article.
        if PROMPT_LINE_RE.search(body):
            return "output"
        if body.startswith(("{", "[")):
            return "json"
        if re.search(r"^\s*(import|from|def|class)\s+\w", body, re.M):
            return "python"
        if OUTPUT_ONLY_RE.match(body):
            return "output"
        return "text"
    return "text"


def _run(cmd: list[str], src: str, suffix: str) -> tuple[bool, str]:
    """Syntax-check `src` with an external tool. Never executes the source."""
    with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False) as fh:
        fh.write(src)
        path = fh.name
    try:
        proc = subprocess.run(
            cmd + [path],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
        return proc.returncode == 0, (proc.stderr or proc.stdout).strip()[:300]
    except FileNotFoundError:
        return True, "__tool_missing__"
    except subprocess.TimeoutExpired:
        return False, "syntax check timed out"
    finally:
        Path(path).unlink(missing_ok=True)


def _check_python(src: str) -> tuple[bool, str]:
    try:
        ast.parse(src)
        return True, ""
    except SyntaxError as exc:
        return False, f"line {exc.lineno}: {exc.msg}"


def _check_json(src: str) -> tuple[bool, str]:
    try:
        json.loads(src)
        return True, ""
    except json.JSONDecodeError as exc:
        return False, str(exc)[:200]


def _check_yaml(src: str) -> tuple[bool, str]:
    # CloudFormation short tags (!Ref, !GetAtt) are not standard YAML.
    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_multi_constructor("!", lambda loader, suffix, node: None)
    try:
        list(yaml.load_all(src, Loader=_Loader))
        return True, ""
    except yaml.YAMLError as exc:
        return False, str(exc)[:200]


def _looks_complete(kind: str, src: str) -> bool:
    if kind == "python":
        return bool(re.search(r"^\s*(?:import|from)\s+\w", src, re.M))
    if kind == "hcl":
        return bool(re.search(r"^\s*(?:resource|provider|module|terraform)\s", src, re.M))
    if kind == "yaml":
        return bool(re.search(r"^\s*(?:AWSTemplateFormatVersion|Resources|apiVersion):", src, re.M))
    if kind == "js":
        return bool(re.search(r"^\s*(?:import|const|require\()", src, re.M))
    return False


def validate_code(md: str) -> CodeReport:
    """Syntax-check every code block. Reports pass rate and concrete failures."""
    report = CodeReport()

    for block in extract_code_blocks(md):
        kind = _classify(block)
        res = BlockResult(index=block.index, lang=block.lang, loc=block.loc, kind=kind)
        src = block.body

        if kind == "output":
            report.output_blocks += 1
            report.blocks.append(res)
            continue

        if not src.strip():
            report.blocks.append(res)
            continue

        res.complete = _looks_complete(kind, src)

        if kind == "python":
            res.checked, (res.ok, res.error) = True, _check_python(src)
        elif kind == "json":
            res.checked, (res.ok, res.error) = True, _check_json(src)
        elif kind == "yaml":
            res.checked, (res.ok, res.error) = True, _check_yaml(src)
        elif kind == "shell":
            ok, err = _run(["bash", "-n"], src, ".sh")
            if err != "__tool_missing__":
                res.checked, res.ok, res.error = True, ok, err
        elif kind == "js":
            if block.lang in {"typescript", "ts"}:
                pass  # no bundled tsc; skip rather than guess
            else:
                ok, err = _run(["node", "--check"], src, ".js")
                if err != "__tool_missing__":
                    res.checked, res.ok, res.error = True, ok, err

        if res.checked:
            report.checked += 1
            report.passed += int(res.ok)
            report.failed += int(not res.ok)
        if res.complete:
            report.complete_files += 1
        report.blocks.append(res)

    return report
