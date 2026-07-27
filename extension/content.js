/**
 * Injects Read-Quality Scores into builder.aws.com.
 *
 * The site is a single-page app that re-renders its feed constantly, so this
 * watches for DOM changes and re-badges rather than running once. Everything is
 * additive: badges are appended, nothing existing is modified or removed, and
 * turning the extension off leaves the page exactly as it shipped.
 */

const CONTENT_HREF = /\/content\/([0-9A-Za-z]+)(?:\/|$)/;
const BADGED = "data-sir-badged";

let SCORES = {};
let SETTINGS = { minRqs: 0, hideSkips: false, enabled: true };

const send = (msg) =>
  new Promise((resolve) => chrome.runtime.sendMessage(msg, resolve));

function articleIdFrom(href) {
  try {
    const m = CONTENT_HREF.exec(new URL(href, location.origin).pathname);
    return m ? m[1] : null;
  } catch {
    return null;
  }
}

function badge(score, { large = false } = {}) {
  const el = document.createElement("span");
  el.className = `sir-badge sir-${score.verdict}${large ? " sir-large" : ""}`;
  el.title = score.headline || "";
  el.innerHTML =
    `<b>${Math.round(score.rqs)}</b><i>${score.verdict}</i>` +
    (large && score.headline ? `<em>${escapeHtml(score.headline)}</em>` : "");
  return el;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

/** Nearest sensible card container, for hiding a whole entry. */
function cardFor(anchor) {
  let node = anchor;
  for (let i = 0; i < 6 && node?.parentElement; i++) {
    node = node.parentElement;
    if (node.matches("li, article, [class*='card'], [class*='Card']")) return node;
  }
  return null;
}

function decorateLinks() {
  for (const a of document.querySelectorAll(`a[href*="/content/"]:not([${BADGED}])`)) {
    const id = articleIdFrom(a.getAttribute("href") || "");
    if (!id) continue;
    a.setAttribute(BADGED, "1");

    const score = SCORES[id];
    if (!score) continue;
    if (score.rqs < SETTINGS.minRqs) continue;

    if (SETTINGS.hideSkips && score.verdict === "SKIP") {
      const card = cardFor(a);
      if (card) {
        card.classList.add("sir-hidden");
        continue;
      }
    }
    a.appendChild(badge(score));
  }
}

function decorateArticlePage() {
  const id = articleIdFrom(location.pathname);
  if (!id) return;
  const score = SCORES[id];
  if (!score || document.querySelector(".sir-banner")) return;

  const heading = document.querySelector("h1");
  if (!heading) return;

  const banner = document.createElement("div");
  banner.className = `sir-banner sir-${score.verdict}`;
  banner.appendChild(badge(score, { large: true }));
  heading.insertAdjacentElement("afterend", banner);
}

let pending = null;
function refresh() {
  if (pending) return;
  pending = requestAnimationFrame(() => {
    pending = null;
    if (!SETTINGS.enabled) return;
    decorateLinks();
    decorateArticlePage();
  });
}

async function init() {
  SETTINGS = (await send({ type: "getSettings" })) || SETTINGS;
  if (!SETTINGS.enabled) return;

  const res = (await send({ type: "getScores" })) || {};
  SCORES = res.scores || {};
  refresh();

  new MutationObserver(refresh).observe(document.body, {
    childList: true,
    subtree: true,
  });

  // The SPA swaps routes without a page load; re-badge when the path changes.
  let path = location.pathname;
  setInterval(() => {
    if (location.pathname !== path) {
      path = location.pathname;
      refresh();
    }
  }, 700);
}

init();
