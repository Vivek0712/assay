"""The author-facing page: paste a link, get ranked recommendations.

Deliberately separate from the leaderboard. The leaderboard answers "what should
I read?" and is about other people's work; this answers "how do I make mine
better?" and is about yours. Mixing them would make the second feel like an
accusation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .config import READ_THRESHOLD, SKIM_THRESHOLD, WEIGHTS

WEB_DIR = Path(__file__).resolve().parents[2] / "web"

DEFAULT_API = "https://d1m5fcrjmmi9ue.cloudfront.net"


def render_review_page(*, api_base: str = DEFAULT_API) -> str:
    weights = "".join(
        f"<tr><td>{name.replace('_', ' ')}</td><td>{w}</td></tr>"
        for name, w in sorted(WEIGHTS.items(), key=lambda kv: -kv[1])
    )
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Assay - improve your Read-Quality Score</title>
<meta name="description" content="Paste an AWS Builder Center article and get ranked, quantified recommendations for improving its Read-Quality Score.">
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
  font:16px/1.6 ui-sans-serif,-apple-system,"Segoe UI",Inter,system-ui,sans-serif; }}
.wrap {{ max-width:800px; margin:0 auto; padding:0 20px 80px; }}
header {{ padding:40px 0 24px; }}
.brand {{ display:inline-flex; align-items:center; gap:10px; text-decoration:none; color:inherit;
  margin:0 0 22px; }}
.brand img {{ border-radius:8px; display:block; }}
.brand span {{ font-weight:600; letter-spacing:-.012em; font-size:17px; }}
h1 {{ font-size:clamp(26px,5vw,38px); margin:0 0 10px; letter-spacing:-.022em; }}
.sub {{ color:var(--muted); margin:0; max-width:62ch; }}
form {{ display:flex; gap:10px; margin:26px 0 6px; flex-wrap:wrap; }}
input[type=url] {{ flex:1; min-width:260px; font:inherit; font-size:14px; padding:11px 14px;
  border-radius:10px; border:1px solid var(--line); background:var(--card); color:var(--fg); }}
button {{ font:inherit; font-size:14px; font-weight:600; padding:11px 20px; border-radius:10px;
  border:1px solid var(--accent); background:var(--accent); color:var(--bg); cursor:pointer; }}
button:disabled {{ opacity:.55; cursor:progress; }}
.hint {{ color:var(--muted); font-size:12.5px; margin:0 0 30px; }}
#out:empty {{ display:none; }}
.result {{ border:1px solid var(--line); border-radius:12px; background:var(--card);
  padding:22px 24px; margin-top:22px; }}
.top {{ display:flex; align-items:baseline; gap:14px; flex-wrap:wrap; }}
.score {{ font-size:40px; font-weight:600; letter-spacing:-.03em; font-variant-numeric:tabular-nums; }}
.badge {{ font-size:11px; letter-spacing:.1em; font-weight:700; text-transform:uppercase; }}
.READ .score,.READ .badge {{ color:var(--read); }}
.SKIM .score,.SKIM .badge {{ color:var(--skim); }}
.SKIP .score,.SKIP .badge {{ color:var(--skip); }}
.title {{ font-weight:600; margin:2px 0 0; }}
.headline {{ color:var(--muted); margin:8px 0 0; }}
.next {{ margin:16px 0 0; padding:12px 14px; border-radius:9px; border:1px solid var(--line);
  font-size:14px; }}
.strengths {{ margin:18px 0 0; font-size:14px; }}
.strengths li {{ color:var(--read); }}
h3 {{ font-size:14px; text-transform:uppercase; letter-spacing:.09em; color:var(--muted);
  margin:26px 0 12px; font-weight:600; }}
ol.recs {{ list-style:none; padding:0; margin:0; display:grid; gap:14px; }}
li.rec {{ border:1px solid var(--line); border-radius:10px; padding:14px 16px; }}
.rechead {{ display:flex; justify-content:space-between; gap:14px; align-items:baseline; }}
.rectitle {{ font-weight:600; }}
.gain {{ font-variant-numeric:tabular-nums; font-weight:600; white-space:nowrap; color:var(--read); }}
.dim {{ color:var(--muted); font-size:12.5px; margin:2px 0 8px; }}
.detail {{ font-size:14.5px; margin:0; }}
.cert {{ font-size:11.5px; color:var(--muted); margin:8px 0 0; }}
table {{ border-collapse:collapse; font-size:13.5px; margin:8px 0 0; }}
td {{ padding:3px 16px 3px 0; }}
td:last-child {{ font-variant-numeric:tabular-nums; color:var(--muted); }}
details {{ margin-top:26px; }}
summary {{ cursor:pointer; color:var(--muted); font-size:13.5px; }}
.err {{ color:var(--skip); }}
footer {{ margin-top:44px; padding-top:20px; border-top:1px solid var(--line);
  color:var(--muted); font-size:13px; }}
a {{ color:inherit; }}
code {{ background:var(--card); border:1px solid var(--line); border-radius:4px;
  padding:1px 5px; font-size:12.5px; }}
</style>
</head>
<body>
<div class="wrap">
<header>
  <a class="brand" href="/"><img src="/assay-logo.svg" alt="" width="34" height="34"><span>Assay</span></a>
  <h1>Improve your Read-Quality Score</h1>
  <p class="sub">Paste one of your AWS Builder Center articles. You'll get its score and a ranked
  list of what would raise it &mdash; each with the number of points it is worth. Where a scoring
  cap is the thing holding you back, that number is exact arithmetic, not a guess.</p>
</header>

<form id="f">
  <input id="u" type="url" required placeholder="https://builder.aws.com/content/&hellip;"
         aria-label="Builder Center article URL">
  <button id="go" type="submit">Review</button>
</form>
<p class="hint">Nothing is stored and nothing is published. This page never adds an article to the
public leaderboard.</p>

<div id="out"></div>

<details>
  <summary>How the score is built</summary>
  <p style="font-size:14.5px">Eight deterministic tools measure the article first &mdash; whether the
  code parses, whether the AWS APIs exist, whether the links resolve, whether there is any pasted
  command output. Four judges then score five dimensions, and the final number is a weighted sum:</p>
  <table>{weights}</table>
  <p style="font-size:14.5px">Each dimension is <strong>capped</strong> by what was measured, so a
  judge cannot award points for code to an article with none. A cap is usually the biggest single
  thing standing between a draft and a better score, which is why the recommendations lead with
  them. <strong>{READ_THRESHOLD}+</strong> is READ, <strong>{SKIM_THRESHOLD}&ndash;{READ_THRESHOLD - 1}</strong>
  is SKIM.</p>
</details>

<footer>
  <p>Likes, views and comments are deliberately excluded &mdash; measured across a week of Builder
  Center they showed no correlation with quality (r&nbsp;=&nbsp;&minus;0.08, n&nbsp;=&nbsp;307).</p>
  <p>Also available as <code>assay review &lt;url&gt;</code> and as an MCP tool.
  &middot; <a href="/">the reading leaderboard</a> &middot; {generated}</p>
</footer>
</div>

<script>
const API = {api_base!r};
const out = document.getElementById('out');
const btn = document.getElementById('go');
const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));

document.getElementById('f').addEventListener('submit', async (e) => {{
  e.preventDefault();
  const url = document.getElementById('u').value.trim();
  if (!url) return;
  btn.disabled = true; btn.textContent = 'Reviewing\\u2026';
  out.innerHTML = `<div class="result"><p class="headline">Scoring the article &mdash; this runs
    four judges and takes up to a minute.</p></div>`;
  try {{
    const res = await fetch(`${{API}}/api/review?url=${{encodeURIComponent(url)}}`);
    const d = await res.json();
    if (d.error) {{
      out.innerHTML = `<div class="result"><p class="err">${{esc(d.error)}}</p></div>`;
    }} else {{
      out.innerHTML = render(d);
    }}
  }} catch (err) {{
    out.innerHTML = `<div class="result"><p class="err">Could not reach the scorer: ${{esc(err)}}</p></div>`;
  }} finally {{
    btn.disabled = false; btn.textContent = 'Review';
  }}
}});

function render(d) {{
  const next = d.points_to_next_band
    ? `<div class="next"><strong>${{d.points_to_next_band.points}} points</strong> from
       <strong>${{esc(d.points_to_next_band.band)}}</strong>. Working through everything below
       reaches about <strong>${{d.reachable_rqs}}</strong> (${{esc(d.reachable_verdict)}}).</div>`
    : (d.recommendations.length
        ? `<div class="next">Already at ${{esc(d.verdict)}}. The items below would take it to about
           <strong>${{d.reachable_rqs}}</strong>.</div>`
        : `<div class="next">Already at ${{esc(d.verdict)}} with nothing specific to fix.</div>`);

  const strengths = (d.strengths || []).length
    ? `<div class="strengths"><h3>Already working</h3><ul>${{
        d.strengths.map(s => `<li>${{esc(s)}}</li>`).join('')}}</ul></div>` : '';

  const recs = (d.recommendations || []).length
    ? `<h3>What would move it, highest impact first</h3>
       <ol class="recs">${{d.recommendations.map(r => `
         <li class="rec">
           <div class="rechead">
             <span class="rectitle">${{esc(r.title)}}</span>
             <span class="gain">+${{r.gain}} RQS</span>
           </div>
           <div class="dim">${{esc(r.dimension_label)}}</div>
           <p class="detail">${{esc(r.detail)}}</p>
           <p class="cert">${{r.certainty === 'exact'
             ? 'Exact &mdash; this cap is provably costing that much.'
             : 'Estimate &mdash; realistic headroom on this dimension.'}}</p>
         </li>`).join('')}}</ol>`
    : '';

  return `<div class="result ${{esc(d.verdict)}}">
    <div class="top">
      <span class="score">${{Math.round(d.rqs)}}</span>
      <span class="badge">${{esc(d.verdict)}}</span>
    </div>
    <p class="title">${{esc(d.title || '')}}</p>
    <p class="headline">${{esc(d.headline || '')}}</p>
    ${{next}}${{strengths}}${{recs}}
  </div>`;
}}
</script>
</body>
</html>
"""


def build_review_page(*, output: str | None = None, api_base: str = DEFAULT_API) -> Path:
    path = Path(output) if output else WEB_DIR / "review.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_review_page(api_base=api_base), encoding="utf-8")
    return path
