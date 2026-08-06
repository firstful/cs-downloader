"""Duration validation filter — catches wrong-content downloads.

Motivating case (2026-08-06): hindmoviez answered "Furious (2026) S01E03/E05/E06"
requests with the SAME ~113-minute movie rip (tag "Visit - JoshMovies.com") while
the real Hulu episodes run 50-55 minutes. The duration gate would have rejected
all three links and let the job fail → reconciler retry until real content exists.

Expected runtime resolution (high → low confidence):
  1. job["expected_runtime_minutes"] — caller pinned (future API field; wins)
  2. TMDB exact:
       TV:    /tv/{id}/season/{s}/episode/{e} -> runtime
       movie: /movie/{id} -> runtime
  3. TV season-median of known per-episode runtimes — for episodes TMDB has not
     populated yet (usually un-aired). E.g. Furious E06 has runtime None on TMDB
     (air date 2026-08-17) but E01-E04 carry ~50-55 min → median catches the junk.
  4. Nothing known → PASS with note. Never sink a job we could not evaluate.

Tolerance (SP_DUR_TOLERANCE, default 0.6): actual must be within ±60% of
expected, i.e. ratio in [1/(1+tol), 1+tol]. A 113-min file for a ~52-min ep
(ratio 2.17) fails; a 22-min broadcast cut vs 45-min listing (0.49) passes.

Action (SP_DUR_ACTION, default hard): wrong content must NOT land in the
library — hard discards the file and the worker tries the next link; if every
link is junk the job errors and the reconciler retries later (correct for
un-aired episodes). Flip to soft if TMDB metadata ever causes false rejects.
"""
from __future__ import annotations

import re
import statistics
from typing import Optional

from .. import config
from ..providers.tmdb import TMDBProvider
from . import FilterResult, register
from .aspect_ratio import _probe

_TMDB_URL_RE = re.compile(r"tmdb://(\d+)")


def _tmdb_id(job: dict) -> Optional[int]:
    """Job rows carry tmdb_id when enqueued via API; wanted-driven jobs stuff
    the source_url ("tmdb://287238") into imdb_id instead."""
    tid = job.get("tmdb_id")
    if tid:
        try:
            return int(tid)
        except (TypeError, ValueError):
            pass
    m = _TMDB_URL_RE.search(str(job.get("imdb_id") or ""))
    if m:
        return int(m.group(1))
    return None


def _episode_runtime(job: dict, tid: int) -> tuple[Optional[float], str]:
    """Exact per-episode runtime from TMDB, or season-median fallback.

    Returns (runtime_minutes, source_label); runtime None when unknown.
    """
    season, episode = job.get("season"), job.get("episode")
    if not season:
        return None, "no-season"
    tmdb = TMDBProvider()
    if episode:
        d = tmdb._get(f"/tv/{tid}/season/{season}/episode/{episode}")
        if d and d.get("runtime"):
            return float(d["runtime"]), "tmdb-episode"
    sdata = tmdb._get(f"/tv/{tid}/season/{season}")
    if not sdata:
        return None, "no-runtime"
    rts = [float(e["runtime"]) for e in sdata.get("episodes", []) if e.get("runtime")]
    if not rts:
        return None, "no-runtime"
    return float(statistics.median(rts)), "season-median"


def _expected_runtime(job: dict) -> tuple[Optional[float], str]:
    """(expected_minutes, source_label). None minutes => inconclusive."""
    pinned = job.get("expected_runtime_minutes")
    if pinned:
        try:
            return float(pinned), "explicit"
        except (TypeError, ValueError):
            pass

    tid = _tmdb_id(job)
    if not tid:
        return None, "no-tmdb-id"

    tmdb = TMDBProvider()
    if job.get("media_type") == "movie":
        d = tmdb._get(f"/movie/{tid}")
        rt = d.get("runtime") if d else None
        if rt:
            return float(rt), "tmdb-movie"
        return None, "no-runtime"

    rt, src = _episode_runtime(job, tid)
    if rt is not None:
        return rt, src
    return None, "no-runtime"


@register("duration")
def check(path: str, job: dict) -> FilterResult:
    probe = _probe(path)
    if not probe["has_video"] or probe["duration"] <= 0:
        return FilterResult(
            filter_id="duration",
            passed=True,
            detail="inconclusive (no video stream or unknown duration)",
            metrics={"note": "probe failed"},
            action=config.DUR_ACTION,
        )

    actual_min = probe["duration"] / 60.0
    expected_min, src = _expected_runtime(job)
    if expected_min is None:
        return FilterResult(
            filter_id="duration",
            passed=True,
            detail=f"inconclusive (no expected runtime: {src})",
            metrics={"actual_minutes": round(actual_min, 1), "source": src},
            action=config.DUR_ACTION,
        )

    tol = config.DUR_TOLERANCE
    ratio = actual_min / expected_min
    lo, hi = 1.0 / (1.0 + tol), 1.0 + tol
    passed = lo <= ratio <= hi
    detail = (
        f"expected {expected_min:.0f} min ({src}) got {actual_min:.0f} min "
        f"(ratio {ratio:.2f}, tol {tol * 100:.0f}%)"
    )
    metrics = {
        "expected_minutes": round(expected_min, 1),
        "actual_minutes": round(actual_min, 1),
        "ratio": round(ratio, 3),
        "tolerance": tol,
        "source": src,
        "bounds": [round(lo, 3), round(hi, 3)],
    }
    return FilterResult(
        filter_id="duration",
        passed=passed,
        detail=detail,
        metrics=metrics,
        action=config.DUR_ACTION,
    )
