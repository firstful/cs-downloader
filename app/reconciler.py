"""Reconciler — watches the wanted-library and drives downloads.

Sweeps every RECONCILE_INTERVAL seconds:
  1. Resolve in-flight attempts: wanted rows in status `attempting` whose
     download job finished -> mark downloaded, or failed with a retry window.
  2. Launch due items: rows in `wanted`/`failed` whose next_attempt_at has
     elapsed -> enqueue a download job (max MAX_RECONCILE_PER_SWEEP per sweep).

Retry policy: a failed attempt bumps next_attempt_at by RETRY_SECONDS
(default 4h — providers tend to be more available at certain hours), so the
reconciler naturally retries every ~4h.
"""
from __future__ import annotations

import logging
import threading
import time
import json

from . import config
from . import db
from . import worker
from .providers import registry

log = logging.getLogger("reconciler")

_stop = threading.Event()
_wake = threading.Event()   # set() to nudge the loop into an early sweep
_thread: threading.Thread | None = None


def _sweep() -> None:
    now = int(time.time())

    # 1) resolve in-flight attempts against finished jobs
    for w in db.list_wanted(status="attempting"):
        job = db.get_job(w["job_id"]) if w.get("job_id") else None
        if job is None:
            # job vanished (deleted manually) -> back to wanted, due now
            db.update_wanted(w["id"], status="wanted", job_id=None, next_attempt_at=now)
            continue
        if job["status"] == "done":
            db.update_wanted(
                w["id"], status="downloaded", job_id=None,
                downloaded_job_id=job["id"], last_error=None,
            )
            log.info("wanted %s downloaded (job %s)", w["id"], job["id"])
        elif job["status"] == "error":
            attempts = (w["attempt_count"] or 0) + 1
            # provider failover: if this wanted row has more candidate providers,
            # advance to the next one and retry soon instead of the 4h backoff.
            providers = _candidates(w)
            nxt = _next_provider(w, providers)
            if nxt:
                # Some candidates (videasy-failover list) don't carry a source_url
                # yet — resolve it on the fly from tmdb metadata.
                src = nxt.get("source_url")
                if not src and nxt.get("id") == "2embed" and w.get("tmdb_id"):
                    tmdb = registry.metadata_provider()  # type: ignore
                    loaded = tmdb.load(f"tmdb://{w['tmdb_id']}/{w['media_type']}")
                    src = loaded.imdb_id if loaded and loaded.imdb_id else w["source_url"]
                elif not src and nxt.get("id") == "hindmoviez" and w.get("tmdb_id"):
                    tmdb = registry.metadata_provider()  # type: ignore
                    loaded = tmdb.load(f"tmdb://{w['tmdb_id']}/{w['media_type']}")
                    hm = registry.get("hindmoviez")  # type: ignore
                    best = None
                    if loaded and hm:
                        matches = hm.search(loaded.title)
                        for m in matches:
                            if m.year and loaded.year and m.year == loaded.year:
                                best = m
                                break
                        if best is None and matches:
                            best = matches[0]
                    src = best.url if best else w["source_url"]
                db.update_wanted(
                    w["id"], status="wanted", job_id=None,
                    attempt_count=attempts, last_error=job.get("error"),
                    provider=nxt["id"], source_url=src or w["source_url"],
                    next_attempt_at=now + 30,   # quick retry on the next candidate
                )
                log.info("wanted %s failed on %s (job %s): %s — trying next provider %s",
                         w["id"], w["provider"], job["id"], job.get("error"), nxt["id"])
            else:
                db.update_wanted(
                    w["id"], status="failed", job_id=None,
                    attempt_count=attempts, last_error=job.get("error"),
                    next_attempt_at=now + config.RETRY_SECONDS,
                )
                log.info("wanted %s failed (job %s): %s — retry in %ss",
                         w["id"], job["id"], job.get("error"), config.RETRY_SECONDS)
        # else: still queued/downloading — leave it

    # 2) launch due items
    for w in db.due_wanted(now, config.MAX_RECONCILE_PER_SWEEP):
        try:
            job_id = db.create_job(
                title=w["title"],
                provider=w["provider"],
                tmdb_id=w.get("tmdb_id"),
                imdb_id=w["source_url"],      # post URL / imdb id — what resolve() expects
                media_type=w["media_type"],
                quality=w.get("quality"),
                season=w.get("season"),
                episode=w.get("episode"),
            )
        except Exception as e:  # noqa: BLE001
            log.exception("wanted %s job create failed", w["id"])
            db.update_wanted(w["id"], status="failed", last_error=f"create: {e}",
                             next_attempt_at=now + config.RETRY_SECONDS)
            continue
        attempts = (w["attempt_count"] or 0) + 1
        db.update_wanted(
            w["id"], status="attempting", job_id=job_id,
            attempt_count=attempts, last_attempt_at=now,
        )
        worker.enqueue(job_id)
        log.info("wanted %s -> job %s (%s)", w["id"], job_id, w["title"])


def _candidates(w: dict) -> list[dict]:
    """Parse the stored candidate provider list. Each entry: {id, source_url?}."""
    raw = w.get("providers")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return [{"id": w["provider"], "source_url": w["source_url"]}]


def _next_provider(w: dict, providers: list[dict]) -> dict | None:
    """Return the first candidate after the currently-active provider."""
    cur = w.get("provider")
    started = False
    for p in providers:
        if p.get("id") == cur:
            started = True
            continue
        if started:
            return p
    return None


def _loop() -> None:
    # first sweep soon after start so a fresh wanted item kicks off quickly
    first = True
    while not _stop.is_set():
        wait = 5 if first else config.RECONCILE_INTERVAL
        first = False
        _wake.wait(wait)
        _wake.clear()
        if _stop.is_set():
            break
        try:
            _sweep()
        except Exception:  # noqa: BLE001
            log.exception("reconcile sweep failed")


def nudge() -> None:
    """Wake the loop for an early sweep (e.g. after a new wanted item / manual retry)."""
    _wake.set()


def start() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _thread = threading.Thread(target=_loop, daemon=True, name="reconciler")
    _thread.start()


def stop() -> None:
    _stop.set()
    if _thread:
        _thread.join(timeout=5)
