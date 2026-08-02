"""Download worker — yt-dlp per job, progress into SQLite."""
from __future__ import annotations

import os
import queue
import json
import re
import threading
import time

from . import config
from . import db

# Use yt-dlp python API if available, else shell out.
try:
    import yt_dlp  # type: ignore
    _HAS_API = True
except Exception:
    _HAS_API = False

_jobs: queue.Queue = queue.Queue()
_stop = threading.Event()
_threads: list[threading.Thread] = []
_job_lock = threading.Lock()


def _safe_name(title: str) -> str:
    name = re.sub(r'[^\w\-. ()]+', "_", title)
    return name.strip(" _.") or "download"


def _write_sidecar(final: str, job: dict) -> None:
    """Write a .json metadata sidecar next to the finished file for the ingest watcher."""
    try:
        meta = {
            "media_type": job.get("media_type") or "movie",
            "title": job.get("title") or "",
            "season": job.get("season"),
            "episode": job.get("episode"),
            "quality": job.get("quality"),
            "provider": job.get("provider"),
            "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        sidecar = final + ".json"
        with open(sidecar, "w") as f:
            json.dump(meta, f)
    except Exception:  # noqa: BLE001
        pass  # sidecar is best-effort; ingest can fall back to filename parsing


def _progress_hook(job_id: int, d: dict):
    status = d.get("status")
    if status == "downloading":
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        done = d.get("downloaded_bytes") or 0
        pct = (done / total * 100.0) if total else 0.0
        db.update_job(
            job_id,
            status="downloading",
            progress=round(pct, 1),
            bytes_downloaded=int(done),
            total_bytes=int(total) if total else None,
        )
    elif status == "finished":
        db.update_job(job_id, status="downloading", progress=99.0)


def _remux_mp4(path: str) -> str:
    """Force an explicit ffmpeg remux to mp4 (yt-dlp remux_video skips HLS TS)."""
    if not path or not os.path.exists(path):
        return path
    import subprocess
    out = os.path.splitext(path)[0] + ".remux.mp4"
    r = subprocess.run(
        [config.FFMPEG_BIN, "-y", "-i", path, "-c", "copy", "-movflags", "+faststart", out],
        capture_output=True, text=True, timeout=1800,
    )
    if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
        os.replace(out, path)
        return path
    return path


def _run_ytdlp(job_id: int, url: str, out_dir: str, out_tmpl: str,
               referer: str | None = None, headers: dict | None = None) -> str:
    """Download url to out_dir. Returns final file path."""
    os.makedirs(out_dir, exist_ok=True)
    opts = {
        "outtmpl": os.path.join(out_dir, out_tmpl),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": config.MAX_RETRIES,
        "fragment_retries": config.MAX_RETRIES,
        "concurrent_fragment_downloads": 4,
        "remux_video": "mp4",
        "progress_hooks": [lambda d: _progress_hook(job_id, d)],
        "http_headers": {
            "User-Agent": config.USER_AGENT,
            **(headers or {}),
        },
        "ffmpeg_location": config.FFMPEG_BIN,
    }
    if referer:
        opts["http_headers"].setdefault("Referer", referer)

    if _HAS_API:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            # info may be a playlist dict; take requested entry path
            path = ""
            if info:
                req = info.get("requested_downloads") or []
                if req:
                    path = req[0].get("filepath") or ""
                if not path:
                    path = ydl.prepare_filename(info)
            return path
    else:
        # fallback: shell out
        import subprocess
        cmd = [config.YTDLP_BIN, "-o", os.path.join(out_dir, out_tmpl),
               "--no-playlist", "--quiet", "--no-warnings",
               "--retries", str(config.MAX_RETRIES), url]
        subprocess.run(cmd, check=True)
        # find the file
        for f in os.listdir(out_dir):
            if not f.endswith((".part", ".ytdl")):
                return os.path.join(out_dir, f)
        return ""


def _worker_loop():
    while not _stop.is_set():
        try:
            job_id = _jobs.get(timeout=2)
        except queue.Empty:
            continue
        try:
            _process(job_id)
        except Exception as e:  # noqa: BLE001
            db.update_job(job_id, status="error", error=f"{type(e).__name__}: {e}")
        finally:
            _jobs.task_done()


def _process(job_id: int):
    job = db.get_job(job_id)
    if not job or job["status"] == "deleted":
        return

    from . import models
    from .providers import registry

    db.update_job(job_id, status="resolving")
    prov = registry.get(job["provider"])
    if not prov:
        db.update_job(job_id, status="error", error=f"unknown provider {job['provider']}")
        return

    req = models.LinksRequest(
        tmdb_id=job["tmdb_id"],
        imdb_id=job["imdb_id"],
        media_type=job["media_type"],
        season=job["season"],
        episode=job["episode"],
        providers=[prov.id],
    )
    try:
        links = prov.resolve(req)
    except Exception as e:
        db.update_job(job_id, status="error", error=f"resolve failed: {e}")
        return
    if not links:
        db.update_job(job_id, status="error", error="no stream links found")
        return

    db.update_job(job_id, status="downloading", progress=0.0)
    title = job["title"] or "download"
    out_dir = os.path.join(config.DOWNLOAD_DIR, f"job-{job_id}")
    os.makedirs(out_dir, exist_ok=True)
    ext_hint = ".%(ext)s"
    out_tmpl = f"{_safe_name(title)}{ext_hint}"

    last_err = None
    for link in links:
        try:
            path = _run_ytdlp(
                job_id, link.url, out_dir, out_tmpl,
                referer=link.referer or link.headers.get("Referer"),
                headers=link.headers,
            )
            if path and os.path.exists(path) and os.path.getsize(path) > 0:
                path = _remux_mp4(path)
                size = os.path.getsize(path)
                # move into flat downloads dir with a stable name
                final = os.path.join(config.DOWNLOAD_DIR, f"{job_id}_{os.path.basename(path)}")
                os.replace(path, final)
                db.update_job(job_id, status="done", progress=100.0,
                              filename=os.path.basename(final), file_size=size)
                _write_sidecar(final, job)
                return
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
            continue

    db.update_job(job_id, status="error", error=f"all links failed: {last_err or 'unknown'}")


def enqueue(job_id: int):
    _jobs.put(job_id)


def start(workers: int | None = None):
    global _threads
    n = workers or config.MAX_CONCURRENT_DOWNLOADS
    for _ in range(n):
        t = threading.Thread(target=_worker_loop, daemon=True, name="dl-worker")
        t.start()
        _threads.append(t)


def stop():
    _stop.set()
    for t in _threads:
        t.join(timeout=5)
