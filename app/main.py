"""StreamPlay Service — FastAPI app — CloudStream-style downloader

Emulates the CloudStream MainAPI model over HTTP:
  search -> load (episodes) -> resolve links -> enqueue download -> poll -> fetch file.
"""
from __future__ import annotations

import logging
import os
import time

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from . import config
from . import db
from . import reconciler
from . import worker
from .models import DownloadRequest, LinksRequest, LinksResponse, Job, SearchResult, WantedRequest
from .providers import registry
from .providers.hindmoviez import HindmoviezProvider
from .providers.tmdb import TMDBProvider

# Show INFO from the worker/reconciler loggers (adaptive concurrency decisions,
# provider failover, job launches) — the default root level hides them.
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="StreamPlay Service", version="1.0.0")

# Known children's shows: if the wanted title matches, force kid_friendly=True
# even when the caller (web UI checkbox default-off) didn't set it. Keeps kids
# downloads routed to the Kids library. Substring match on lowercased title.
KIDS_TITLES = (
    "peppa pig", "paw patrol", "bluey", "spongebob", "daniel tiger",
    "arthur", "wild kratts", "dora the explorer", "sesame street",
    "cocomelon", "mickey mouse", "pj masks", "octonauts",
    "blaze and the monster machines", "doc mcstuffins", "bubble guppies",
    "curious george", "thomas & friends", "my little pony", "teletubbies",
    "blue's clues", "super wings", "pocoyo", "masha and the bear",
    "winnie the pooh", "little einsteins", "handy manny", "mickey mouse clubhouse",
    "peppa", "strawberry shortcake", "care bears", "the wiggles", "barney",
    "pinkfong", "bebefinn", "morphle", "tayo",
)


def is_kids_title(title: str) -> bool:
    t = (title or "").lower()
    return any(k in t for k in KIDS_TITLES)

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.on_event("startup")
def _startup():
    os.makedirs(config.DATA_DIR, exist_ok=True)
    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
    db.init_db()
    registry.init_registry()
    worker.start()
    reconciler.start()


@app.on_event("shutdown")
def _shutdown():
    reconciler.stop()
    worker.stop()


# ---- health / meta ---------------------------------------------------------

@app.get("/")
def index():
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))


@app.get("/health")
def health():
    return {"status": "ok", "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


@app.get("/api/providers")
def providers():
    return {"providers": [p.describe() for p in registry.all_providers()]}


@app.get("/api/providers/stats")
def provider_stats_api():
    """Provider download reliability stats (for auto-resolve ordering)."""
    stats = db.provider_stats()
    for s in stats:
        a = s["attempts"] or 0
        s["success_rate"] = round((s["successes"] or 0) / a, 3) if a else None
    order = db.reliable_provider_order()
    return {"stats": stats, "order": order}


# ---- CloudStream-style search / load ---------------------------------------

@app.get("/api/search")
def search(q: str = Query(..., min_length=1), page: int = 1):
    tmdb: TMDBProvider = registry.metadata_provider()  # type: ignore
    return {"query": q, "page": page, "results": [r.model_dump() for r in tmdb.search(q, page)]}


@app.get("/api/load")
def load(url: str):
    tmdb: TMDBProvider = registry.metadata_provider()  # type: ignore
    data = tmdb.load(url)
    if not data:
        raise HTTPException(404, f"no metadata for {url}")
    return data.model_dump()


# ---- stream links ----------------------------------------------------------

@app.post("/api/links")
def links(req: LinksRequest):
    if not req.imdb_id and not req.tmdb_id:
        raise HTTPException(400, "imdb_id or tmdb_id required")

    # resolve imdb id via tmdb if only tmdb_id given
    imdb_id = req.imdb_id
    if not imdb_id and req.tmdb_id:
        tmdb: TMDBProvider = registry.metadata_provider()  # type: ignore
        loaded = tmdb.load(f"tmdb://{req.tmdb_id}/{req.media_type}")
        if loaded and loaded.imdb_id:
            imdb_id = loaded.imdb_id
    if not imdb_id:
        raise HTTPException(400, "could not resolve imdb_id (2embed needs it)")

    wanted = req.providers or [p.id for p in registry.source_providers()]
    out = []
    results = {}
    for pid in wanted:
        prov = registry.get(pid)
        if not prov or not prov.enabled:
            results[pid] = {"status": "skipped", "note": "disabled or unknown"}
            continue
        try:
            links_list = prov.resolve(req.model_copy(update={"imdb_id": imdb_id}))
            results[pid] = {"status": "ok", "count": len(links_list)}
            out.extend(l.model_dump() for l in links_list)
        except Exception as e:  # noqa: BLE001
            results[pid] = {"status": "error", "error": f"{type(e).__name__}: {e}"}
    return LinksResponse(links=out, provider_results=results).model_dump()


# ---- downloads -------------------------------------------------------------

@app.post("/api/downloads")
def enqueue(req: DownloadRequest):
    if req.media_type not in ("movie", "tv"):
        raise HTTPException(400, "media_type must be movie|tv")
    if req.media_type == "tv" and (not req.season or not req.episode):
        raise HTTPException(400, "tv downloads need season + episode")

    title = req.title or ""
    if not title:
        tmdb: TMDBProvider = registry.metadata_provider()  # type: ignore
        loaded = tmdb.load(f"tmdb://{req.tmdb_id}/{req.media_type}") if req.tmdb_id else None
        if loaded:
            suffix = ""
            if req.media_type == "tv":
                suffix = f" S{req.season:02d}E{req.episode:02d}"
            title = f"{loaded.title} ({loaded.year}){suffix}"
    if not title:
        title = f"{req.media_type}-{req.tmdb_id or req.imdb_id}"

    job_id = db.create_job(
        title=title,
        provider=req.provider,
        tmdb_id=req.tmdb_id,
        imdb_id=req.imdb_id,
        media_type=req.media_type,
        season=req.season,
        episode=req.episode,
        quality=req.quality,
        expected_aspect_ratio=req.expected_aspect_ratio,
    )
    worker.enqueue(job_id)
    return {"id": job_id, "status": "queued"}


@app.get("/api/downloads")
def downloads(limit: int = 50):
    return {"jobs": db.list_jobs(limit)}


@app.get("/api/downloads/{job_id}")
def job_status(job_id: int):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    return job


@app.get("/api/downloads/{job_id}/file")
def job_file(job_id: int):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    if job["status"] != "done" or not job["filename"]:
        raise HTTPException(409, f"file not ready (status={job['status']})")
    path = os.path.join(config.DOWNLOAD_DIR, job["filename"])
    if not os.path.exists(path):
        raise HTTPException(404, "file missing on disk")
    return FileResponse(path, filename=job["filename"], media_type="application/octet-stream")


@app.delete("/api/downloads/{job_id}")
def job_delete(job_id: int):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    if job["filename"]:
        try:
            os.remove(os.path.join(config.DOWNLOAD_DIR, job["filename"]))
        except OSError:
            pass
    db.delete_job(job_id)
    return {"deleted": job_id}


# ---- wanted library (persistent desired-items + reconciler) ---------------

@app.post("/api/wanted")
def wanted_add(req: WantedRequest):
    # ---- auto-resolve provider when not specified -----------------------------
    # Priority: videasy (streaming, broad catalog incl. English TV) ->
    # hindmoviez (season-pack download site) -> 2embed (imdb fallback).
    # Auto mode tries each in order and picks the first that resolves a source.
    source_url = req.source_url
    provider = req.provider
    tmdb_id = req.tmdb_id
    title = req.title
    candidates: list[dict] = []

    if provider and not source_url:
        # explicit provider with tmdb_id: resolve source for that provider only
        tmdb = registry.metadata_provider()  # type: ignore
        loaded = tmdb.load(f"tmdb://{tmdb_id}/{req.media_type}") if tmdb_id else None
        if loaded and not title:
            title = f"{loaded.title} ({loaded.year})" if loaded.year else loaded.title
        if provider == "hindmoviez":
            hm = registry.get("hindmoviez")  # type: ignore
            best = _hindmoviez_best(hm, loaded)
            if best:
                source_url = best.url
        elif provider in ("2embed", "videasy", "xpass"):
            if loaded and loaded.imdb_id and provider == "2embed":
                source_url = loaded.imdb_id
        # videasy/xpass/rivestream resolve from tmdb_id only — no source_url needed
        if not source_url and provider not in ("videasy", "xpass", "rivestream"):
            raise HTTPException(404, f"no source found for provider {provider}")
        candidates = [{"id": provider, "source_url": source_url}]

    elif not provider:
        # AUTO: try providers in reliability order (most-reliable first), then
        # fall back to the priority list until the stats table has data.
        tmdb = registry.metadata_provider()  # type: ignore
        loaded = tmdb.load(f"tmdb://{tmdb_id}/{req.media_type}") if tmdb_id else None
        if loaded and not title:
            title = f"{loaded.title} ({loaded.year})" if loaded.year else loaded.title
        if not tmdb_id:
            raise HTTPException(400, "tmdb_id required for auto provider resolution")

        # Build the candidate pool in reliability order, keeping the known-good
        # priority as a tiebreak for providers with no stats yet.
        priority = ["videasy", "xpass", "hindmoviez", "2embed"]
        reliable = db.reliable_provider_order()
        ordered: list[str] = []
        for p in reliable + priority:
            if p not in ordered and p in priority:
                ordered.append(p)
        for p in priority:
            if p not in ordered:
                ordered.append(p)

        candidates = []
        for pid in ordered:
            prov = registry.get(pid)
            if not prov or not prov.enabled or prov.kind != "source":
                continue
            if pid == "videasy" and tmdb_id:
                # seed probe confirms availability cheaply
                try:
                    seed = prov._fetch_seed("https://api.speedracelight.com", tmdb_id)  # type: ignore[attr-defined]
                except Exception:
                    seed = None
                if seed:
                    source_url = ""
                    provider = "videasy"
                    candidates = [
                        {"id": "videasy", "source_url": ""},
                        {"id": "xpass", "source_url": None},
                        {"id": "hindmoviez", "source_url": None},
                        {"id": "2embed", "source_url": None},
                    ]
                    break
            elif pid == "xpass":
                # Probe the player page cheaply (needs a concrete episode for tv).
                xp = registry.get("xpass")  # type: ignore
                probe_s, probe_e = None, None
                if req.media_type == "tv":
                    if req.episode is not None:
                        probe_s, probe_e = req.season, req.episode
                    elif req.episodes:
                        probe_s = req.episodes[0].get("season")
                        probe_e = req.episodes[0].get("episode")
                if xp and tmdb_id and (req.media_type == "movie" or (probe_s is not None and probe_e is not None)):
                    try:
                        links = xp.resolve(type("_R", (), {
                            "media_type": req.media_type, "tmdb_id": tmdb_id,
                            "season": probe_s, "episode": probe_e,
                        })())
                        if links:
                            provider = "xpass"
                            candidates = [
                                {"id": "xpass", "source_url": None},
                                {"id": "videasy", "source_url": ""},
                                {"id": "hindmoviez", "source_url": None},
                                {"id": "2embed", "source_url": None},
                            ]
                            break
                    except Exception:
                        pass
            elif pid == "hindmoviez":
                best = _hindmoviez_best(prov, loaded)
                if best:
                    source_url, provider = best.url, "hindmoviez"
                    candidates = [
                        {"id": "hindmoviez", "source_url": best.url},
                        {"id": "videasy", "source_url": ""},
                        {"id": "xpass", "source_url": None},
                        {"id": "2embed", "source_url": None},
                    ]
                    break
            elif pid == "2embed":
                if loaded and loaded.imdb_id:
                    source_url, provider = loaded.imdb_id, "2embed"
                    candidates = [
                        {"id": "2embed", "source_url": loaded.imdb_id},
                        {"id": "videasy", "source_url": ""},
                        {"id": "xpass", "source_url": None},
                        {"id": "hindmoviez", "source_url": None},
                    ]
                    break
        if not provider:
            raise HTTPException(404, "no source found on any provider")

    if not title:
        title = source_url or f"tmdb-{tmdb_id or '?'}"

    # ---- build the (season, episode) selection ---------------------------------
    pairs: list[tuple[int | None, int | None]] = []
    if req.media_type == "tv":
        if req.seasons or req.episodes or req.season is not None:
            # explicit per-episode picks
            for e in req.episodes:
                pairs.append((e.get("season"), e.get("episode")))
            # whole seasons -> enumerate episodes from TMDB
            if req.seasons:
                tmdb = registry.metadata_provider()  # type: ignore
                loaded = tmdb.load(f"tmdb://{tmdb_id}/{req.media_type}") if tmdb_id else None
                ep_by_season: dict[int, list[int]] = {}
                if loaded:
                    for ep in loaded.episodes:
                        ep_by_season.setdefault(ep.season, []).append(ep.episode)
                for sn in req.seasons:
                    for ep in ep_by_season.get(sn, []):
                        pairs.append((sn, ep))
            # legacy single season/episode
            if req.season is not None:
                if req.episode is not None:
                    pairs.append((req.season, req.episode))
                else:
                    # legacy "whole season" -> enumerate too
                    tmdb = registry.metadata_provider()  # type: ignore
                    loaded = tmdb.load(f"tmdb://{tmdb_id}/{req.media_type}") if tmdb_id else None
                    if loaded:
                        for ep in loaded.episodes:
                            if ep.season == req.season:
                                pairs.append((req.season, ep.episode))
                    else:
                        pairs.append((req.season, None))
        # dedupe + order
        seen: set[tuple[int | None, int | None]] = set()
        pairs = [p for p in pairs if not (p in seen or seen.add(p))]
        pairs.sort(key=lambda p: (p[0] or 0, p[1] or 0))
        if not pairs:
            raise HTTPException(400, "tv wanted needs season + episode (or seasons/episodes list)")
    else:
        pairs = [(None, None)]

    # ---- provider-aware granularity ------------------------------------------
    # hindmoviez posts are SEASON PACKS: a TV wanted item downloads the pack, so
    # one row per season (episode=None). Per-episode providers (2embed/videasy)
    # get one row per episode, queued one at a time by the reconciler/worker pool.
    if req.media_type == "tv" and provider == "hindmoviez":
        rows: list[tuple[int | None, int | None]] = sorted({(s, None) for s, _ in pairs if s is not None})
        if not rows:
            raise HTTPException(400, "no seasons to download")
    else:
        rows = pairs

    created = []
    kid_flag = req.kid_friendly or is_kids_title(title)
    for sn, ep in rows:
        row_title = title
        if req.media_type == "tv" and sn is not None:
            row_title = f"{title} S{sn:02d}" + (f"E{ep:02d}" if ep is not None else "")
        existing = db.find_wanted(source_url or f"tmdb://{tmdb_id}", req.media_type, sn, ep)
        if existing:
            created.append(existing)
            continue
        wid = db.create_wanted(row_title, provider, source_url or f"tmdb://{tmdb_id}",
                               req.media_type, req.quality, sn, ep,
                               tmdb_id=tmdb_id, providers=candidates,
                               kid_friendly=kid_flag,
                               priority=req.priority,
                               expected_aspect_ratio=req.expected_aspect_ratio)
        created.append(db.get_wanted(wid))

    # first sweep happens soon after startup; nudge the reconciler for fast kickoff
    reconciler.nudge()
    return {"created": created, "count": len(created)}


def _hindmoviez_best(hm, loaded) -> "SearchResult | None":
    """Year-matched hindmoviez post search hit, or the first match."""
    if not hm or not loaded:
        return None
    matches = hm.search(loaded.title)
    best = None
    for m in matches:
        if m.year and loaded.year and m.year == loaded.year:
            best = m
            break
    if best is None and matches:
        best = matches[0]
    return best


@app.get("/api/wanted")
def wanted_list(status: str | None = None):
    return {"wanted": db.list_wanted(status)}


@app.get("/api/wanted/{wanted_id}")
def wanted_get(wanted_id: int):
    w = db.get_wanted(wanted_id)
    if not w:
        raise HTTPException(404, "no such wanted item")
    return w


@app.delete("/api/wanted/{wanted_id}")
def wanted_delete(wanted_id: int):
    w = db.get_wanted(wanted_id)
    if not w:
        raise HTTPException(404, "no such wanted item")
    db.delete_wanted(wanted_id)
    return {"deleted": wanted_id}


@app.post("/api/wanted/{wanted_id}/retry")
def wanted_retry(wanted_id: int):
    w = db.get_wanted(wanted_id)
    if not w:
        raise HTTPException(404, "no such wanted item")
    db.update_wanted(wanted_id, status="wanted", job_id=None, next_attempt_at=0)
    reconciler.nudge()
    return db.get_wanted(wanted_id)
