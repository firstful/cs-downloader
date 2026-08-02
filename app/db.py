"""SQLite job store."""
from __future__ import annotations

import sqlite3
import threading
import time
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
    progress REAL NOT NULL DEFAULT 0,
    bytes_downloaded INTEGER NOT NULL DEFAULT 0,
    total_bytes INTEGER,
    filename TEXT,
    file_size INTEGER,
    error TEXT,
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
               media_type="movie", season=None, episode=None, quality=None) -> int:
    with _cursor() as conn:
        cur = conn.execute(
            "INSERT INTO jobs (title, provider, tmdb_id, imdb_id, media_type, season, episode, quality, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (title, provider, tmdb_id, imdb_id, media_type, season, episode, quality, _now(), _now()),
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
