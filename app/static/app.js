/* media.ffilt.us — cs-downloader SPA (vanilla JS, no build step) */
"use strict";

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const state = { results: [], selected: null, loaded: null, season: 1, episode: null, quality: null };

async function api(path, opts) {
  const r = await fetch(path, opts);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
  return body;
}

// ---- search (debounced type-ahead) ----------------------------------------
let debounceTimer = null;
$("#q").addEventListener("input", (e) => {
  clearTimeout(debounceTimer);
  const q = e.target.value.trim();
  if (q.length < 2) { $("#results").innerHTML = ""; return; }
  debounceTimer = setTimeout(() => runSearch(q), 400);
});

async function runSearch(q) {
  try {
    const d = await api(`/api/search?q=${encodeURIComponent(q)}`);
    state.results = d.results || [];
    renderResults();
  } catch (err) { $("#results").innerHTML = `<div class="msg err">${esc(err.message)}</div>`; }
}

function renderResults() {
  const box = $("#results");
  if (!state.results.length) { box.innerHTML = `<div class="msg">No matches.</div>`; return; }
  box.innerHTML = state.results.map((r, i) => `
    <div class="result" data-i="${i}">
      ${r.poster ? `<img src="${esc(r.poster)}" alt="">` : ""}
      <div>
        <div class="t">${esc(r.title)} <span class="badge">${esc(r.media_type)}</span></div>
        <div class="meta">${r.year || ""}${r.overview ? " — " + esc(r.overview.slice(0, 90)) : ""}</div>
      </div>
    </div>`).join("");
  box.querySelectorAll(".result").forEach((el) => el.addEventListener("click", () => selectResult(+el.dataset.i)));
}

// ---- detail ----------------------------------------------------------------
async function selectResult(i) {
  const r = state.results[i];
  $("#results").innerHTML = "";
  state.selected = r;
  const url = `tmdb://${r.id}/${r.media_type}`;
  try {
    const d = await api(`/api/load?url=${encodeURIComponent(url)}`);
    state.loaded = d;
    state.quality = r.media_type === "tv" ? "720p" : "1080p";
    state.season = 1; state.episode = null;
    renderDetail();
  } catch (err) { $("#detail-body").innerHTML = `<div class="msg err">${esc(err.message)}</div>`; }
}

function renderDetail() {
  const d = state.loaded, r = state.selected;
  const isTv = d.media_type === "tv";
  const seasons = (d.seasons || []).length ? d.seasons : [1];
  const eps = (d.episodes || []).filter((e) => e.season === state.season);
  const tvdb = d.tvdb_id ? `<span class="pill">TVDB ${d.tvdb_id}</span>` : "";
  $("#detail").classList.remove("hidden");
  $("#detail-body").innerHTML = `
    <div class="detail-top">
      ${d.poster ? `<img src="${esc(d.poster)}" alt="">` : ""}
      <div style="flex:1">
        <h3>${esc(d.title)} ${d.year ? `(${d.year})` : ""} ${tvdb}</h3>
        <div class="meta">${esc(d.imdb_id || "")} · provider: hindmoviez (pack search) → 2embed fallback</div>
        ${d.overview ? `<div class="over">${esc(d.overview)}</div>` : ""}
        <div class="controls">
          <label>Quality
            <select id="quality">
              ${(isTv ? ["720p", "1080p", "480p"] : ["1080p", "720p", "480p", "4K"]).map((q) =>
                `<option value="${q}" ${q === state.quality ? "selected" : ""}>${q}</option>`).join("")}
            </select>
          </label>
          ${isTv ? `
          <label>Season
            <select id="season">
              ${seasons.map((s) => `<option value="${s}" ${s === state.season ? "selected" : ""}>${s}</option>`).join("")}
            </select>
          </label>
          <label>Episode
            <select id="episode">
              <option value="">whole season (pack)</option>
              ${eps.map((e) => `<option value="${e.episode}">E${String(e.episode).padStart(2, "0")}${e.title ? " · " + esc(e.title) : ""}</option>`).join("")}
            </select>
          </label>` : ""}
          <button class="primary" id="add-wanted">Add to library</button>
        </div>
        <div id="wmsg"></div>
      </div>
    </div>`;
  if (isTv) {
    $("#season").addEventListener("change", (e) => { state.season = +e.target.value; state.episode = null; renderDetail(); });
    $("#episode").addEventListener("change", (e) => { state.episode = e.target.value ? +e.target.value : null; });
  }
  $("#quality").addEventListener("change", (e) => { state.quality = e.target.value; });
  $("#add-wanted").addEventListener("click", addWanted);
}

async function addWanted() {
  const d = state.loaded;
  const btn = $("#add-wanted"); btn.disabled = true;
  const payload = {
    tmdb_id: d.id, media_type: d.media_type, title: d.year ? `${d.title} (${d.year})` : d.title,
    quality: state.quality,
  };
  if (d.media_type === "tv") { payload.season = state.season; if (state.episode) payload.episode = state.episode; }
  try {
    const w = await api("/api/wanted", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    $("#wmsg").innerHTML = `<div class="msg ok">Queued #${w.id} — ${esc(w.provider)} · ${esc(w.quality || "auto")}${w.season ? ` S${w.season}` : ""}${w.episode ? `E${w.episode}` : ""}</div>`;
    loadWanted();
  } catch (err) { $("#wmsg").innerHTML = `<div class="msg err">${esc(err.message)}</div>`; }
  finally { btn.disabled = false; }
}

// ---- wanted library ---------------------------------------------------------
async function loadWanted() {
  try {
    const d = await api("/api/wanted");
    const list = d.wanted || [];
    $("#wanted-list").innerHTML = list.map((w) => `
      <div class="wcard">
        <span class="st st-${esc(w.status)}">${esc(w.status)}</span>
        <div class="t">${esc(w.title)}</div>
        <div class="s">${esc(w.provider)}${w.quality ? " · " + esc(w.quality) : ""}${w.season ? ` · S${w.season}` : ""}${w.episode ? `E${w.episode}` : ""}${w.attempt_count ? ` · attempt ${w.attempt_count}` : ""}</div>
        ${w.last_error ? `<div class="err">${esc(w.last_error)}</div>` : ""}
        <button data-act="retry" data-id="${w.id}" title="Retry now">↻</button>
        <button class="danger" data-act="del" data-id="${w.id}" title="Remove">✕</button>
      </div>`).join("") || `<div class="msg">Nothing desired yet — search and add above.</div>`;
    list.forEach((w) => {
      $("#wanted-list").querySelector(`[data-act="retry"][data-id="${w.id}"]`)?.addEventListener("click", () => retryWanted(w.id));
      $("#wanted-list").querySelector(`[data-act="del"][data-id="${w.id}"]`)?.addEventListener("click", () => delWanted(w.id));
    });
  } catch (_) { /* transient */ }
}

async function retryWanted(id) { try { await api(`/api/wanted/${id}/retry`, { method: "POST" }); loadWanted(); } catch (_) {} }
async function delWanted(id) { try { await api(`/api/wanted/${id}`, { method: "DELETE" }); loadWanted(); } catch (_) {} }

loadWanted();
setInterval(loadWanted, 10000);
