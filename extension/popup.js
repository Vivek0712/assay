/** Popup: corpus stats plus the four settings the content script reads. */

const FIELDS = {
  enabled: "checkbox",
  hideSkips: "checkbox",
  minRqs: "number",
  apiBase: "url",
};

const DEFAULTS = {
  enabled: true,
  hideSkips: false,
  minRqs: 0,
  apiBase: "https://d1m5fcrjmmi9ue.cloudfront.net",
};

function flash(message) {
  const el = document.getElementById("saved");
  el.textContent = message;
  setTimeout(() => (el.textContent = ""), 1400);
}

async function restore() {
  const stored = await chrome.storage.sync.get(DEFAULTS);
  for (const [id, kind] of Object.entries(FIELDS)) {
    const el = document.getElementById(id);
    if (kind === "checkbox") el.checked = Boolean(stored[id]);
    else el.value = stored[id];
  }
}

function wire() {
  for (const [id, kind] of Object.entries(FIELDS)) {
    const el = document.getElementById(id);
    el.addEventListener("change", async () => {
      const value =
        kind === "checkbox"
          ? el.checked
          : kind === "number"
          ? Number(el.value) || 0
          : el.value.trim();
      await chrome.storage.sync.set({ [id]: value });
      flash("Saved — reload builder.aws.com to apply.");
    });
  }
}

function renderStats(stats) {
  const box = document.getElementById("stats");
  if (!stats) {
    box.innerHTML = `<div class="stat"><b>—</b><span>scores unavailable</span></div>`;
    return;
  }
  const cells = [
    [stats.total, "articles scored"],
    [`${stats.read_pct}%`, "worth reading"],
    [`${stats.no_code_pct}%`, "have no code"],
    [`${stats.with_output_pct}%`, "show real output"],
  ];
  box.innerHTML = cells
    .map(([v, l]) => `<div class="stat"><b>${v}</b><span>${l}</span></div>`)
    .join("");
}

chrome.runtime.sendMessage({ type: "getScores" }, (res) =>
  renderStats(res && res.stats)
);

restore().then(wire);
