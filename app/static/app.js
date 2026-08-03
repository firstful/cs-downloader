/* media.ffilt.us — cs-downloader SPA (vanilla JS, no build step) */
"use strict";

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pad = (n) => String(n).padStart(2, "0");

const state = { results: [], selected: null, loaded: null, quality: null, picked: new Set() };

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
  state.picked = new Set();
  const url = `tmdb://${r.id}/${r.media_type}`;
  try {
    const d = await api(`/api/load?url=${encodeURIComponent(url)}`);
    state.loaded = d;
    state.quality = r.media_type === "tv" ? "720p" : "1080p";
    renderDetail();
  } catch (err) { $("#detail-body").innerHTML = `<div class="msg err">${esc(err.message)}</div>`; }
}

function renderDetail() {
  const d = state.loaded, r = state.selected;
  const isTv = d.media_type === "tv";
  const seasons = (d.seasons || []).length ? d.seasons : [1];
  const epsBySeason = {};
  (d.episodes || []).forEach((e) => { (epsBySeason[e.season] = epsBySeason[e.season] || []).push(e); });
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
          <label class="pick-all-wrap">
            <input type="checkbox" id="pick-all">
            <b>Select all</b> <span class="muted">(every season &amp; episode)</span>
          </label>` : ""}
        </div>
        ${isTv ? `<div class="ep-tree" id="ep-tree"></div>` : ""}
        <div class="controls">
          <button class="primary" id="add-wanted">${isTv ? "Add selected to library" : "Add to library"}</button>
          <span id="pick-count" class="muted"></span>
        </div>
        <div id="wmsg"></div>
      </div>
    </div>`;
  if (isTv) {
    buildEpTree(seasons, epsBySeason);
    $("#pick-all").addEventListener("change", (e) => {
      const on = e.target.checked;
      seasons.forEach((sn) => (epsBySeason[sn] || []).forEach((ep) => {
        const key = `s${sn}e${ep.episode}`;
        if (on) state.picked.add(key); else state.picked.delete(key);
      }));
      syncTree(seasons, epsBySeason);
    });
  }
  $("#quality").addEventListener("change", (e) => { state.quality = e.target.value; });
  $("#add-wanted").addEventListener("click", addWanted);
  updatePickCount();
}

function buildEpTree(seasons, epsBySeason) {
  const tree = $("#ep-tree");
  tree.innerHTML = seasons.map((sn) => {
    const eps = epsBySeason[sn] || [];
    return `
      <div class="tree-season">
        <label class="s-head"><input type="checkbox" class="pick-season" data-season="${sn}">
          <b>Season ${sn}</b> <span class="muted">(${eps.length} eps)</span></label>
        <div class="tree-eps">
          ${eps.map((e) => `
            <label class="tree-ep"><input type="checkbox" class="pick-ep" data-season="${sn}" data-episode="${e.episode}">
              S${pad(sn)}E${pad(e.episode)}${e.title ? " · " + esc(e.title) : ""}</label>`).join("")}
        </div>
      </div>`;
  }).join("");
  tree.querySelectorAll(".pick-season").forEach((el) => el.addEventListener("change", (e) => {
    const sn = +e.target.dataset.season;
    const on = e.target.checked;
    (epsBySeason[sn] || []).forEach((ep) => {
      const key = `s${sn}e${ep.episode}`;
      if (on) state.picked.add(key); else state.picked.delete(key);
    });
    syncTree(seasons, epsBySeason);
  }));
  tree.querySelectorAll(".pick-ep").forEach((el) => el.addEventListener("change", (e) => {
    const key = `s${e.target.dataset.season}e${e.target.dataset.episode}`;
    if (e.target.checked) state.picked.add(key); else state.picked.delete(key);
    syncTree(seasons, epsBySeason);
  }));
}

function syncTree(seasons, epsBySeason) {
  const tree = $("#ep-tree");
  if (!tree) return;
  tree.querySelectorAll(".pick-ep").forEach((el) => {
    el.checked = state.picked.has(`s${el.dataset.season}e${el.dataset.episode}`);
  });
  tree.querySelectorAll(".pick-season").forEach((el) => {
    const sn = +el.dataset.season;
    const eps = epsBySeason[sn] || [];
    const n = eps.filter((e) => state.picked.has(`s${sn}e${e.episode}`)).length;
    el.checked = eps.length > 0 && n === eps.length;
    el.indeterminate = n > 0 && n < eps.length;
  });
  const all = $("#pick-all");
  if (all) {
    let total = 0, picked = 0;
    seasons.forEach((sn) => (epsBySeason[sn] || []).forEach((e) => { total++; if (state.picked.has(`s${sn}e${e.episode}`)) picked++; }));
    all.checked = total > 0 && picked === total;
    all.indeterminate = picked > 0 && picked < total;
  }
  updatePickCount();
}

function updatePickCount() {
  const el = $("#pick-count");
  if (!el) return;
  const n = state.picked.size;
  el.textContent = n ? `${n} episode${n === 1 ? "" : "s"} selected` : "";
}

async function addWanted() {
  const d = state.loaded;
  const btn = $("#add-wanted"); btn.disabled = true;
  const base = {
    tmdb_id: d.id, media_type: d.media_type, title: d.year ? `${d.title} (${d.year})` : d.title,
    quality: state.quality,
  };
  try {
    let res;
    if (d.media_type === "tv") {
      if (!state.picked.size) throw new Error("select at least one episode (or use Select all)");
      const seasons = [];
      const episodes = [];
      const bySeason = {};
      state.picked.forEach((k) => { const m = k.match(/^s(\d+)e(\d+)$/); if (m) (bySeason[m[1]] = bySeason[m[1]] || []).push(+m[2]); });
      // whole seasons -> seasons list; partial -> per-episode list
      const allEp = {};
      (d.episodes || []).forEach((e) => { (allEp[e.season] = allEp[e.season] || []).push(e.episode); });
      Object.keys(bySeason).map(Number).sort((a, b) => a - b).forEach((sn) => {
        const pickedEps = bySeason[sn].sort((a, b) => a - b);
        const allEps = (allEp[sn] || []).sort((a, b) => a - b);
        if (pickedEps.length === allEps.length && pickedEps.every((v, i) => v === allEps[i])) {
          seasons.push(sn);
        } else {
          pickedEps.forEach((ep) => episodes.push({ season: sn, episode: ep }));
        }
      });
      res = await api("/api/wanted", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...base, seasons, episodes }) });
    } else {
      res = await api("/api/wanted", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(base) });
    }
    const n = res.count || (Array.isArray(res.created) ? res.created.length : 1);
    const first = (res.created || [])[0];
    $("#wmsg").innerHTML = `<div class="msg ok">Queued ${n} item${n === 1 ? "" : "s"} — ${first ? esc(first.provider) : "auto"} · ${esc(state.quality || "auto")}</div>`;
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
