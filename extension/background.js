/**
 * Service worker: owns all network access and caches the score index.
 *
 * Content scripts never fetch directly. Keeping the origin in one place means
 * the extension needs host permission for builder.aws.com only, and the score
 * API can move without touching the injected code.
 */

const DEFAULT_API = "https://d1m5fcrjmmi9ue.cloudfront.net";
const CACHE_TTL_MS = 15 * 60 * 1000;

let cache = { at: 0, scores: null, stats: null };

async function apiBase() {
  const { apiBase } = await chrome.storage.sync.get("apiBase");
  return (apiBase || DEFAULT_API).replace(/\/+$/, "");
}

async function loadIndex(force = false) {
  if (!force && cache.scores && Date.now() - cache.at < CACHE_TTL_MS) return cache;
  try {
    const res = await fetch(`${await apiBase()}/scores.json`, { cache: "no-cache" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    cache = { at: Date.now(), scores: data.scores || {}, stats: data.stats || null };
  } catch (err) {
    // Keep serving a stale index rather than blanking every badge on a blip.
    if (!cache.scores) cache = { at: Date.now(), scores: {}, stats: null, error: String(err) };
  }
  return cache;
}

chrome.runtime.onMessage.addListener((msg, _sender, respond) => {
  if (msg.type === "getScores") {
    loadIndex(msg.force).then((c) =>
      respond({ scores: c.scores, stats: c.stats, error: c.error })
    );
    return true; // async response
  }
  if (msg.type === "getSettings") {
    chrome.storage.sync
      .get({ apiBase: DEFAULT_API, minRqs: 0, hideSkips: false, enabled: true })
      .then(respond);
    return true;
  }
  return false;
});

chrome.runtime.onInstalled.addListener(() => loadIndex(true));
