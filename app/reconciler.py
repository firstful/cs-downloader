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

from . import config
from . import db
from . import worker

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
