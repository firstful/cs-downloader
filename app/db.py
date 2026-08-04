"""SQLite job store."""
from __future__ import annotations

import sqlite3
import threading
import time
import json
from contextlib import contextmanager
from typing import Optional

from . import config

_LOCK = threading.Lock()
_DB = config.DATA_DIR.rstrip("/") + "/streamplay.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL DEFAULT 'queued',
    title TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    tmdb_id INTEGER,
    imdb_id TEXT,
    media_type TEXT NOT NULL DEFAULT 'movie',
    season INTEGER,
    episode INTEGER,
    quality TEXT,
    kid_friendly INTEGER NOT NULL DEFAULT 0,
    progress REAL NOT NULL DEFAULT 0,
    bytes_downloaded INTEGER NOT NULL DEFAULT 0,
    total_bytes INTEGER,
    filename TEXT,
    file_size INTEGER,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_stats (
    provider TEXT PRIMARY KEY,
    attempts INTEGER NOT NULL DEFAULT 0,
    successes INTEGER NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    bytes_downloaded INTEGER NOT NULL DEFAULT 0,
    last_success_at INTEGER,
    last_failure_at INTEGER,
    avg_quality TEXT
);

CREATE TABLE IF NOT EXISTS wanted (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL,
    tmdb_id INTEGER,
    providers TEXT,                       -- JSON array of candidate provider ids (auto-resolve order)
    media_type TEXT NOT NULL DEFAULT 'movie',
    quality TEXT,
    status TEXT NOT NULL DEFAULT 'wanted',      -- wanted | attempting | downloaded | failed
    job_id INTEGER,
    downloaded_job_id INTEGER,
    kid_friendly INTEGER NOT NULL DEFAULT 0,     -- route to the Kids library
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    last_attempt_at INTEGER,
    next_attempt_at INTEGER NOT NULL DEFAULT 0,  -- epoch; 0 = due immediately
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    import os
    os.makedirs(config.DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        # migration: wanted season/episode (older DBs lack the columns)
        for col in ("season INTEGER", "episode INTEGER", "tmdb_id INTEGER", "providers TEXT", "kid_friendly INTEGER NOT NULL DEFAULT 0"):
            try:
                conn.execute(f"ALTER TABLE wanted ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass  # already present
        for col in ("kid_friendly INTEGER NOT NULL DEFAULT 0",):
            try:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass  # already present


@contextmanager
def _cursor():
    with _LOCK:
        conn = _connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def create_job(title: str, provider: str, tmdb_id=None, imdb_id=None,
               media_type="movie", season=None, episode=None, quality=None,
               kid_friendly: bool = False) -> int:
    with _cursor() as conn:
        cur = conn.execute(
            "INSERT INTO jobs (title, provider, tmdb_id, imdb_id, media_type, season, episode, quality, kid_friendly, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (title, provider, tmdb_id, imdb_id, media_type, season, episode, quality,
             1 if kid_friendly else 0, _now(), _now()),
        )
        return int(cur.lastrowid)


def get_job(job_id: int) -> Optional[dict]:
    with _cursor() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None


def list_jobs(limit: int = 50) -> list[dict]:
    with _cursor() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def list_jobs_by_status(statuses: tuple[str, ...]) -> list[dict]:
    """All jobs in the given statuses, oldest first (FIFO recovery order)."""
    if not statuses:
        return []
    marks = ",".join("?" for _ in statuses)
    with _cursor() as conn:
        rows = conn.execute(
            f"SELECT * FROM jobs WHERE status IN ({marks}) ORDER BY id ASC",
            statuses,
        ).fetchall()
        return [dict(r) for r in rows]


def update_job(job_id: int, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [job_id]
    with _cursor() as conn:
        conn.execute(f"UPDATE jobs SET {cols} WHERE id=?", vals)


def delete_job(job_id: int) -> None:
    with _cursor() as conn:
        conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))


# ---- wanted library --------------------------------------------------------

def create_wanted(title: str, provider: str, source_url: str,
                  media_type: str = "movie", quality: str | None = None,
                  season: int | None = None, episode: int | None = None,
                  tmdb_id: int | None = None,
                  providers: list[dict] | None = None,
                  kid_friendly: bool = False) -> int:
    with _cursor() as conn:
        cur = conn.execute(
            "INSERT INTO wanted (title, provider, source_url, tmdb_id, providers, media_type, quality,"
            " season, episode, kid_friendly, status, next_attempt_at, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?, 'wanted', 0, ?, ?)",
            (title, provider, source_url, tmdb_id,
             json.dumps(providers) if providers else None,
             media_type, quality, season, episode,
             1 if kid_friendly else 0, _now(), _now()),
        )
        return int(cur.lastrowid)


def find_wanted(source_url: str, media_type: str = "movie",
                season: int | None = None, episode: int | None = None) -> Optional[dict]:
    """Return an existing non-downloaded wanted row matching the same source+episode (dedupe)."""
    with _cursor() as conn:
        row = conn.execute(
            "SELECT * FROM wanted WHERE source_url=? AND media_type=? AND season IS ? AND episode IS ?"
            " AND status != 'downloaded' ORDER BY id DESC LIMIT 1",
            (source_url, media_type, season, episode),
        ).fetchone()
        return dict(row) if row else None


def get_wanted(wanted_id: int) -> Optional[dict]:
    with _cursor() as conn:
        row = conn.execute("SELECT * FROM wanted WHERE id=?", (wanted_id,)).fetchone()
        return dict(row) if row else None


def list_wanted(status: str | None = None) -> list[dict]:
    with _cursor() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM wanted WHERE status=? ORDER BY id DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM wanted ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]


def delete_wanted(wanted_id: int) -> None:
    with _cursor() as conn:
        conn.execute("DELETE FROM wanted WHERE id=?", (wanted_id,))


def update_wanted(wanted_id: int, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [wanted_id]
    with _cursor() as conn:
        conn.execute(f"UPDATE wanted SET {cols} WHERE id=?", vals)


def due_wanted(now: int, limit: int = 2) -> list[dict]:
    """wanted/failed items whose retry window has elapsed (next_attempt_at <= now)."""
    with _cursor() as conn:
        rows = conn.execute(
            "SELECT * FROM wanted WHERE status IN ('wanted','failed')"
            " AND next_attempt_at <= ? ORDER BY next_attempt_at ASC, id ASC LIMIT ?",
            (now, limit),
        ).fetchall()
        return [dict(r) for r in rows]


# ---- provider reliability stats --------------------------------------------

def record_provider_attempt(provider: str, ok: bool, bytes_downloaded: int = 0,
                            quality: str | None = None) -> None:
    """Record one download outcome for a provider (for reliability ordering)."""
    now = int(time.time())
    with _cursor() as conn:
        row = conn.execute("SELECT * FROM provider_stats WHERE provider=?",
                           (provider,)).fetchone()
        if row:
            conn.execute(
                "UPDATE provider_stats SET attempts=attempts+1,"
                " successes=successes+?, failures=failures+?,"
                " bytes_downloaded=bytes_downloaded+?,"
                " last_success_at=CASE WHEN ? THEN ? ELSE last_success_at END,"
                " last_failure_at=CASE WHEN ? THEN ? ELSE last_failure_at END,"
                " avg_quality=? WHERE provider=?",
                (1 if ok else 0, 0 if ok else 1, bytes_downloaded,
                 ok, now, not ok, now, quality or row["avg_quality"], provider),
            )
        else:
            conn.execute(
                "INSERT INTO provider_stats (provider, attempts, successes, failures,"
                " bytes_downloaded, last_success_at, last_failure_at, avg_quality)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (provider, 1, 1 if ok else 0, 0 if ok else 1, bytes_downloaded,
                 now if ok else None, now if not ok else None, quality),
            )


def provider_stats() -> list[dict]:
    with _cursor() as conn:
        rows = conn.execute("SELECT * FROM provider_stats").fetchall()
        return [dict(r) for r in rows]


def reliable_provider_order() -> list[str]:
    """Provider ids sorted by reliability (best first).

    Score = success rate with a recency bonus and a minimum-attempts floor so a
    single lucky success doesn't dominate. Providers with no stats are ranked
    after proven ones but before consistently-failing ones.
    """
    import json as _json
    stats = provider_stats()
    scored = []
    for s in stats:
        attempts = s["attempts"] or 0
        if attempts == 0:
            continue
        rate = (s["successes"] or 0) / attempts
        # recency: recent success boosts, recent failure hurts (within 7 days)
        now = int(time.time())
        recency = 0.0
        if s["last_success_at"] and now - s["last_success_at"] < 7 * 86400:
            recency += 0.15
        if s["last_failure_at"] and now - s["last_failure_at"] < 7 * 86400:
            recency -= 0.1
        # min-attempts floor: <3 attempts get slight penalty (not proven yet)
        floor = -0.1 if attempts < 3 else 0.0
        scored.append((rate + recency + floor, s["provider"]))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in scored]
