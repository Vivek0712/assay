"""Serving surfaces built from a scored run: the leaderboard page and the feed.

Attribution policy, applied here and nowhere else so it cannot drift between
surfaces: articles that earn READ or SKIM are named and linked, because naming
good work is the point. Articles that score SKIP are shown with their score and
their reasons but without author or title. The aggregate problem is worth being
blunt about; individual community members are not the target.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

from .config import ROOT, SITE
from .corpus import load_scores
from .preferences import Preferences

WEB_DIR = ROOT / "web"

# SKIP verdicts are published anonymously.
REDACT_VERDICTS = {"SKIP"}


def _redacted(score: dict[str, Any]) -> bool:
    return score.get("verdict") in REDACT_VERDICTS


def _public(score: dict[str, Any]) -> dict[str, Any]:
    """One score, with the attribution policy applied."""
    out = dict(score)
    if _redacted(score):
        out["title"] = "[redacted]"
        out["url"] = ""
        out["author_alias"] = ""
        out["redacted"] = True
    else:
        out["redacted"] = False
    return out


def _stats(scores: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(scores) or 1
    counts: dict[str, int] = {}
    for s in scores:
        counts[s["verdict"]] = counts.get(s["verdict"], 0) + 1

    def sig(s: dict, *path: str, default: Any = 0) -> Any:
        cur: Any = s.get("signals") or {}
        for p in path:
            cur = (cur or {}).get(p) if isinstance(cur, dict) else None
        return default if cur is None else cur

    no_code = sum(1 for s in scores if sig(s, "structure", "code_blocks") == 0)
    no_links = sum(1 for s in scores if sig(s, "structure", "links") == 0)
    with_output = sum(1 for s in scores if sig(s, "code", "output_blocks") > 0)
    bad_apis = sum(1 for s in scores if sig(s, "aws_apis", "total_invalid") > 0)
    cross = sum(1 for s in scores if sig(s, "duplicates", "is_cross_post"))
    rqs = sorted(s["rqs"] for s in scores)

    return {
        "total": len(scores),
        "read": counts.get("READ", 0),
        "skim": counts.get("SKIM", 0),
        "skip": counts.get("SKIP", 0),
        "read_pct": round(counts.get("READ", 0) / n * 100, 1),
        "skip_pct": round(counts.get("SKIP", 0) / n * 100, 1),
        "median_rqs": rqs[len(rqs) // 2] if rqs else 0,
        "no_code": no_code,
        "no_code_pct": round(no_code / n * 100, 1),
        "no_links": no_links,
        "no_links_pct": round(no_links / n * 100, 1),
        "with_output": with_output,
        "with_output_pct": round(with_output / n * 100, 1),
        "invalid_apis": bad_apis,
        "cross_posts": cross,
    }


# --------------------------------------------------------------------------
# HTML leaderboard
# --------------------------------------------------------------------------
def render_page(scores: list[dict[str, Any]], *, generated: str | None = None) -> str:
    scores = sorted(scores, key=lambda s: s["rqs"], reverse=True)
    stats = _stats(scores)
    generated = generated or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    payload = json.dumps([_public(s) for s in scores], ensure_ascii=False)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" type="image/svg+xml" href="/assay-mark.svg">
<title>Assay - read-quality scoring for AWS Builder Center</title>
<meta name="description" content="Assay reads AWS Builder Center and scores every article on measurable evidence rather than likes.">
<style>
:root {{
  color-scheme: light dark;
  --bg:#fbfbfa; --fg:#16150f; --muted:#6b6a63; --line:#e3e2dc; --card:#fff;
  --read:#1a7f5a; --skim:#9a6b00; --skip:#a33b30; --accent:#16150f;
}}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#14140f; --fg:#ecebe4; --muted:#96958c; --line:#2c2b24; --card:#1c1b16;
           --read:#5fd1a4; --skim:#e3b34d; --skip:#f2887a; --accent:#ecebe4; }}
}}
:root[data-theme=dark] {{ --bg:#14140f; --fg:#ecebe4; --muted:#96958c; --line:#2c2b24; --card:#1c1b16;
  --read:#5fd1a4; --skim:#e3b34d; --skip:#f2887a; --accent:#ecebe4; }}
:root[data-theme=light] {{ --bg:#fbfbfa; --fg:#16150f; --muted:#6b6a63; --line:#e3e2dc; --card:#fff;
  --read:#1a7f5a; --skim:#9a6b00; --skip:#a33b30; --accent:#16150f; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--fg);
  font:16px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Inter,system-ui,sans-serif; }}
.wrap {{ max-width:940px; margin:0 auto; padding:0 20px 80px; }}
header {{ padding:40px 0 28px; border-bottom:1px solid var(--line); }}
.brand {{ display:inline-flex; align-items:center; gap:10px; text-decoration:none; color:inherit;
  margin:0 0 22px; }}
.brand img {{ display:block; }}
h1 {{ font-size:clamp(28px,5vw,42px); margin:0 0 10px; letter-spacing:-.022em; }}
.sub {{ color:var(--muted); max-width:60ch; margin:0; }}
.stats {{ display:grid; gap:12px; grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
  margin:28px 0 0; }}
.stat {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; }}
.stat b {{ display:block; font-size:26px; letter-spacing:-.02em; font-variant-numeric:tabular-nums; }}
.stat span {{ color:var(--muted); font-size:12.5px; }}
.controls {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin:26px 0 6px; }}
button.f {{ font:inherit; font-size:13px; padding:6px 13px; border-radius:999px; cursor:pointer;
  border:1px solid var(--line); background:var(--card); color:var(--fg); }}
button.f[aria-pressed=true] {{ background:var(--accent); color:var(--bg); border-color:var(--accent); }}
input#q {{ font:inherit; font-size:13px; padding:6px 12px; border-radius:999px;
  border:1px solid var(--line); background:var(--card); color:var(--fg); min-width:190px; flex:1; }}
.count {{ color:var(--muted); font-size:13px; margin:14px 0 0; }}
ol {{ list-style:none; padding:0; margin:8px 0 0; }}
li.row {{ border-bottom:1px solid var(--line); padding:18px 0; display:grid;
  grid-template-columns:64px 1fr; gap:16px; align-items:start; }}
.score {{ font-size:23px; font-weight:600; font-variant-numeric:tabular-nums; letter-spacing:-.02em; }}
.badge {{ font-size:10.5px; letter-spacing:.09em; font-weight:600; text-transform:uppercase; }}
.READ .score,.READ .badge {{ color:var(--read); }}
.SKIM .score,.SKIM .badge {{ color:var(--skim); }}
.SKIP .score,.SKIP .badge {{ color:var(--skip); }}
.title {{ font-weight:600; letter-spacing:-.011em; }}
.title a {{ color:inherit; text-decoration:none; }}
.title a:hover {{ text-decoration:underline; }}
.redacted {{ color:var(--muted); font-style:italic; font-weight:500; }}
.head {{ margin:5px 0 8px; }}
.meta {{ color:var(--muted); font-size:12.5px; display:flex; flex-wrap:wrap; gap:5px 12px; }}
.tag {{ font-size:11px; border:1px solid var(--line); border-radius:5px; padding:1px 6px; }}
details {{ margin-top:10px; }}
summary {{ cursor:pointer; color:var(--muted); font-size:12.5px; }}
.dims {{ margin-top:10px; display:grid; gap:7px; }}
.dim {{ display:grid; grid-template-columns:150px 90px 1fr; gap:10px; align-items:center; font-size:12.5px; }}
.bar {{ height:5px; background:var(--line); border-radius:3px; overflow:hidden; }}
.bar i {{ display:block; height:100%; background:currentColor; }}
.dimname {{ color:var(--muted); }}
footer {{ margin-top:52px; padding-top:22px; border-top:1px solid var(--line);
  color:var(--muted); font-size:13px; }}
code {{ background:var(--card); border:1px solid var(--line); border-radius:4px; padding:1px 5px; font-size:12.5px; }}
@media (max-width:560px) {{
  li.row {{ grid-template-columns:52px 1fr; gap:12px; }}
  .dim {{ grid-template-columns:110px 60px 1fr; }}
}}
</style>
</head>
<body>
<div class="wrap">
<header>
  <a class="brand" href="/"><img src="/assay-logo.svg" alt="Assay" width="187" height="52"></a>
  <h1>Should I read this?</h1>
  <p class="sub">An agent reads AWS Builder Center so you don't have to. Every article gets a
  <strong>Read-Quality Score</strong> built from what can be measured &mdash; pasted terminal output,
  code that actually parses, AWS APIs that actually exist, sources that actually resolve.
  Likes and views are deliberately ignored; they are the easiest thing on the page to buy.</p>
  <div class="stats">
    <div class="stat"><b>{stats['total']}</b><span>articles scored</span></div>
    <div class="stat"><b>{stats['read']}</b><span>worth reading ({stats['read_pct']}%)</span></div>
    <div class="stat"><b>{stats['no_code_pct']}%</b><span>contain no code at all</span></div>
    <div class="stat"><b>{stats['no_links_pct']}%</b><span>cite no sources at all</span></div>
    <div class="stat"><b>{stats['with_output_pct']}%</b><span>show real command output</span></div>
    <div class="stat"><b>{stats['median_rqs']:.0f}</b><span>median score</span></div>
  </div>
</header>

<div class="controls">
  <button class="f" data-v="ALL" aria-pressed="true">All</button>
  <button class="f" data-v="READ" aria-pressed="false">Read</button>
  <button class="f" data-v="SKIM" aria-pressed="false">Skim</button>
  <button class="f" data-v="SKIP" aria-pressed="false">Skip</button>
  <input id="q" type="search" placeholder="filter by topic, title or author&hellip;" aria-label="Filter">
</div>
<p class="count" id="count"></p>
<ol id="list"></ol>

<footer>
  <p><strong>Assay</strong> scores are produced by a Strands multi-agent fleet on Amazon Bedrock. Four judges score
  execution evidence, code substance, source integrity and depth; the final number is weighted
  arithmetic, not a model's opinion, and each dimension is capped by what the deterministic
  tools actually measured &mdash; a judge cannot award points for code to an article with none.</p>
  <p><strong>Attribution:</strong> articles scoring READ or SKIM are named and linked, because
  naming good work is the point. Articles scoring SKIP are shown anonymously. The aggregate
  problem is worth being blunt about; individual authors are not the target.</p>
  <p><strong>Wrote one of these?</strong> <a href="/review.html">Get recommendations for improving
  its score</a> &mdash; ranked by how many points each change is worth.</p>
  <p>Generated {generated} &middot; source data from <a href="{SITE}">builder.aws.com</a>.
  Score one yourself with <code>shouldiread score &lt;url&gt;</code>.</p>
</footer>
</div>

<script>
const DATA = {payload};
const list = document.getElementById('list');
const count = document.getElementById('count');
const q = document.getElementById('q');
let verdict = 'ALL';

const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));

function dimRow(d) {{
  return `<div class="dim"><span class="dimname">${{esc(d.name.replace(/_/g,' '))}}</span>
    <span class="bar"><i style="width:${{Math.max(0, Math.min(100, d.score))}}%"></i></span>
    <span>${{d.score.toFixed(0)}} &mdash; ${{esc(d.rationale)}}</span></div>`;
}}

function render() {{
  const term = q.value.trim().toLowerCase();
  const rows = DATA.filter(s => {{
    if (verdict !== 'ALL' && s.verdict !== verdict) return false;
    if (!term) return true;
    return (s.title + ' ' + (s.author_alias||'') + ' ' + (s.tags||[]).join(' ') + ' ' + s.headline)
      .toLowerCase().includes(term);
  }});
  count.textContent = `${{rows.length}} of ${{DATA.length}} articles`;
  list.innerHTML = rows.map(s => {{
    const title = s.redacted
      ? `<span class="redacted">[author and title withheld]</span>`
      : (s.url ? `<a href="${{esc(s.url)}}" rel="noopener">${{esc(s.title)}}</a>` : esc(s.title));
    const who = s.redacted ? '' : `<span>@${{esc(s.author_alias)}}</span>`;
    const tags = (s.tags||[]).slice(0,5).map(t => `<span class="tag">${{esc(t)}}</span>`).join('');
    const bonus = (s.signals && s.signals.author_bonus) || 0;
    const bonusRow = bonus
      ? `<div class="dim"><span class="dimname">author credibility</span>
           <span></span><span>+${{bonus}} on top of ${{s.signals.base_rqs}} earned by the article
           (${{esc(s.author_kind||'').replace(/_/g,' ')}})</span></div>`
      : '';
    const dims = (s.dimensions||[]).length
      ? `<details><summary>score breakdown</summary><div class="dims">
           ${{s.dimensions.map(dimRow).join('')}}${{bonusRow}}</div></details>`
      : '';
    return `<li class="row ${{s.verdict}}">
      <div><div class="score">${{s.rqs.toFixed(0)}}</div><div class="badge">${{s.verdict}}</div></div>
      <div>
        <div class="title">${{title}}</div>
        <div class="head">${{esc(s.headline)}}</div>
        <div class="meta">${{who}}<span>${{esc(s.author_kind||'')}}</span>${{tags}}</div>
        ${{dims}}
      </div></li>`;
  }}).join('');
}}

document.querySelectorAll('button.f').forEach(b => b.addEventListener('click', () => {{
  verdict = b.dataset.v;
  document.querySelectorAll('button.f').forEach(x =>
    x.setAttribute('aria-pressed', String(x === b)));
  render();
}}));
q.addEventListener('input', render);
render();
</script>
</body>
</html>
"""


def build_page(*, run: str = "latest", output: str | None = None) -> Path:
    scores = load_scores(run)
    if not scores:
        raise SystemExit(f"no scores found for run {run!r}")
    path = Path(output) if output else WEB_DIR / "index.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_page(scores), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# RSS / Atom feed
# --------------------------------------------------------------------------
def render_feed(scores: list[dict[str, Any]], *, prefs: Preferences) -> str:
    kept = prefs.apply(scores)
    now = datetime.now(timezone.utc).isoformat()

    entries = []
    for s in kept:
        if _redacted(s):
            continue  # a feed of anonymous links is useless
        dims = "".join(
            f"<li>{xml_escape(d['name'].replace('_', ' '))}: {d['score']:.0f}/100</li>"
            for d in s.get("dimensions", [])
        )
        body = (
            f"<p><strong>RQS {s['rqs']:.0f}/100 &mdash; {xml_escape(s['verdict'])}</strong></p>"
            f"<p>{xml_escape(s['headline'])}</p>"
            + (f"<ul>{dims}</ul>" if dims else "")
        )
        entries.append(
            f"""  <entry>
    <id>{xml_escape(s['url'] or s['article_id'])}</id>
    <title type="html">{xml_escape(s['title'])}</title>
    <link rel="alternate" href="{xml_escape(s['url'])}"/>
    <updated>{xml_escape(s.get('published_at') or now)}</updated>
    <author><name>{xml_escape(s.get('author_alias') or 'unknown')}</name></author>
    {''.join(f'<category term="{xml_escape(t)}"/>' for t in (s.get('tags') or [])[:6])}
    <summary type="html">{xml_escape(body)}</summary>
  </entry>"""
        )

    topics = ", ".join(prefs.topics) if prefs.topics else "all topics"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <id>urn:shouldiread:feed</id>
  <title>ShouldIRead - AWS Builder Center, worth-reading only</title>
  <subtitle>RQS &gt;= {prefs.min_rqs:.0f}, {xml_escape(topics)}. Scored on execution evidence, not engagement.</subtitle>
  <updated>{now}</updated>
  <link rel="alternate" href="{SITE}"/>
  <generator>shouldiread</generator>
{chr(10).join(entries)}
</feed>
"""


def build_feed(
    *,
    run: str = "latest",
    output: str | None = None,
    min_rqs: float = 70.0,
    topics: list[str] | None = None,
) -> Path:
    scores = load_scores(run)
    if not scores:
        raise SystemExit(f"no scores found for run {run!r}")
    prefs = Preferences(min_rqs=min_rqs, topics=topics or [], limit=100)
    path = Path(output) if output else WEB_DIR / "feed.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_feed(scores, prefs=prefs), encoding="utf-8")
    return path


def build_api_json(*, run: str = "latest", output: str | None = None) -> Path:
    """Static JSON the browser extension and MCP server read."""
    scores = load_scores(run)
    by_id = {s["article_id"]: _public(s) for s in scores}
    path = Path(output) if output else WEB_DIR / "scores.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "generated": datetime.now(timezone.utc).isoformat(),
                "stats": _stats(scores),
                "scores": by_id,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path
