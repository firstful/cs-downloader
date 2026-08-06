"""Aspect-ratio validation filter.

Rejects downloads whose on-screen content ratio (after removing letterbox /
pillarbox black bars) does not roughly match the expected ratio.

TWO COMPARISON MODES (config SP_AR_MODE_TV / SP_AR_MODE_MOVIE):

  strict — content AR must be within SP_AR_TOLERANCE of the expected value:
      1. job["expected_aspect_ratio"] if the caller supplied it (wins)
      2. TV: SP_AR_TV_DEFAULT (16:9) · movie: SP_AR_MOVIE_FALLBACK
      NOTE: per-title AR metadata does NOT exist on TMDB (verified 2026-08-05;
      no aspect_ratio field in v3 movie details), so a strict default of 16:9
      FALSE-REJECTS legit scope-letterboxed movies (2.39 content in a 16:9
      container) and 2.39 TV dramas. Only use strict without an explicit
      expected value on content you know is 16:9.

  lenient — accept if the content AR is within tolerance of ANY standard
      cinema/broadcast ratio; reject only non-standard garbage (stretched /
      squeezed / cut rips). Zero false positives on legit content, but cannot
      catch pan-and-scan (P&S removes the bars, so content looks like 16:9).

Expected resolution: an explicit job["expected_aspect_ratio"] always forces
strict comparison, regardless of the mode knobs. Nothing on TMDB provides AR,
so callers that know the title's ratio (IMDb lookup, manual pin) should pass
it. Until then, lenient for movies / strict 16:9 for TV are the defaults.

Detection: ffmpeg cropdetect on AR_SAMPLES frames spread across the file
(skipping intro/credits). The dominant crop (mode, largest area on tie) is the
content area; content AR = crop W / crop H. Samples whose crop is smaller than
AR_MIN_CROP_FRACTION of the frame in either dimension are discarded (fade-to-
black / mostly-black frames produce junk tiny crops).
"""
from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from typing import Optional

from .. import config
from . import FilterResult, register

_CROP_RE = re.compile(r"crop=(\d+):(\d+):\d+:\d+")

# Standard cinema/broadcast aspect ratios (content AR after bars removed).
STANDARD_RATIOS: tuple[float, ...] = (
    1.333,   # 4:3
    1.375,   # academy
    1.43,    # IMAX 70mm
    1.5,     # 3:2
    1.66,    # european flat
    1.778,   # 16:9
    1.85,    # US flat
    2.0,     # 2.00:1 (Univisium)
    2.2,     # 70mm
    2.35,    # anamorphic
    2.39,    # scope (current)
    2.55,    # cinemascope
    2.76,    # ultra-panavision
)


def _probe(path: str) -> dict:
    """ffprobe: duration, video width/height, has_video."""
    cmd = [
        config.AR_FFPROBE_BIN,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height:format=duration",
        "-of", "json",
        path,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        data = json.loads(r.stdout or "{}")
    except Exception:  # noqa: BLE001
        return {"has_video": False, "duration": 0.0, "width": 0, "height": 0}
    streams = data.get("streams") or []
    if not streams:
        return {"has_video": False, "duration": 0.0, "width": 0, "height": 0}
    s = streams[0]
    try:
        duration = float(data.get("format", {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    return {
        "has_video": True,
        "duration": duration,
        "width": int(s.get("width") or 0),
        "height": int(s.get("height") or 0),
    }


def _sample_crop(path: str, ts: float) -> Optional[tuple[int, int]]:
    """Run cropdetect on ~2 frames near ts. Returns (w, h) content area or None.

    Notes:
      - Uses -filter_complex: this host's ffmpeg (4.2.7 ESM) silently drops
        simple -vf filtergraphs when combined with input seeking.
      - -frames:v 2 (not 1): input seek + frames:v 1 also drops the filter on
        that build; 2 frames is deterministic and still ~1-2 decodes.
    """
    cmd = [
        config.FFMPEG_BIN,
        "-ss", f"{ts:.3f}",
        "-i", path,
        "-filter_complex", "[0:v]cropdetect=limit=24:round=2:reset=0[v]",
        "-map", "[v]",
        "-frames:v", "2",
        "-an",
        "-f", "null", "-",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except Exception:  # noqa: BLE001
        return None
    matches = _CROP_RE.findall(r.stderr)
    if not matches:
        return None
    w, h = int(matches[-1][0]), int(matches[-1][1])
    if w <= 0 or h <= 0:
        return None
    return w, h


def detect_content_aspect(path: str) -> tuple[Optional[float], dict]:
    """Detect the content aspect ratio with black bars removed.

    Returns (content_ar, metrics). content_ar is None when the detector could
    not measure anything (no video stream, or no valid crop samples).
    """
    probe = _probe(path)
    if not probe["has_video"]:
        return None, {"note": "no video stream"}
    w0, h0 = probe["width"], probe["height"]
    if w0 <= 0 or h0 <= 0:
        return None, {"note": "unknown frame size"}

    duration = probe["duration"]
    n = max(1, config.AR_SAMPLES)
    if duration > 0:
        lo = duration * config.AR_SKIP_START
        hi = duration * (1.0 - config.AR_SKIP_END)
        if hi - lo < 1.0:  # tiny file — just sample the middle
            lo, hi = 0.0, max(duration, 0.5)
        timestamps = [lo + (hi - lo) * (i + 0.5) / n for i in range(n)]
    else:
        # duration unknown — sample fixed offsets
        timestamps = [0.5 + i * 1.0 for i in range(n)]

    crops: list[tuple[int, int]] = []
    for ts in timestamps:
        c = _sample_crop(path, ts)
        if c is None:
            continue
        cw, ch = c
        if cw >= w0 * config.AR_MIN_CROP_FRACTION and ch >= h0 * config.AR_MIN_CROP_FRACTION:
            crops.append(c)

    if not crops:
        return None, {
            "note": "no valid crop samples (black or near-black frames)",
            "frame": f"{w0}x{h0}",
            "samples_tried": len(timestamps),
        }

    counts = Counter(crops)
    (cw, ch), count = max(counts.items(), key=lambda kv: (kv[1], kv[0][0] * kv[0][1]))
    content_ar = round(cw / ch, 4)
    metrics = {
        "content_crop": f"{cw}x{ch}",
        "frame": f"{w0}x{h0}",
        "content_ar": content_ar,
        "samples_used": len(crops),
        "samples_tried": len(timestamps),
        "mode_count": count,
        "detections": sorted(
            ({"crop": f"{a}x{b}", "count": cnt} for (a, b), cnt in counts.items()),
            key=lambda d: -d["count"],
        )[:5],
    }
    return content_ar, metrics


def _mode_for(job: dict) -> str:
    """An explicit expected ratio always forces strict comparison."""
    if job.get("expected_aspect_ratio"):
        return "strict"
    if job.get("media_type") == "movie":
        return config.AR_MODE_MOVIE
    return config.AR_MODE_TV


def _default_expected(job: dict) -> float:
    """Per-type default expected ratio for strict mode (no explicit value)."""
    if job.get("media_type") == "movie":
        return config.AR_MOVIE_FALLBACK
    return config.AR_TV_DEFAULT


def _near_standard(content_ar: float) -> Optional[float]:
    """Closest standard ratio within tolerance, or None."""
    best = None
    best_diff = float("inf")
    for r in STANDARD_RATIOS:
        diff = abs(content_ar - r) / r
        if diff <= config.AR_TOLERANCE and diff < best_diff:
            best, best_diff = r, diff
    return best


@register("aspect_ratio")
def check(path: str, job: dict) -> FilterResult:
    content_ar, metrics = detect_content_aspect(path)
    metrics = dict(metrics)
    metrics["tolerance"] = config.AR_TOLERANCE

    if content_ar is None:
        # Inconclusive — pass through rather than reject on a blind guess.
        return FilterResult(
            filter_id="aspect_ratio",
            passed=True,
            detail=f"inconclusive ({metrics.get('note', 'unknown')})",
            metrics=metrics,
        )

    metrics["content_ar"] = content_ar
    mode = _mode_for(job)
    metrics["mode"] = mode

    if mode == "lenient":
        std = _near_standard(content_ar)
        if std is not None:
            return FilterResult(
                filter_id="aspect_ratio",
                passed=True,
                detail=(
                    f"content AR {content_ar:.3f} matches standard {std:.3f} "
                    f"(crop {metrics.get('content_crop')})"
                ),
                metrics={**metrics, "standard_matched": std},
            )
        return FilterResult(
            filter_id="aspect_ratio",
            passed=False,
            detail=(
                f"content AR {content_ar:.3f} (crop {metrics.get('content_crop')}) "
                f"is not within {config.AR_TOLERANCE * 100:.0f}% of any standard ratio"
            ),
            metrics=metrics,
            action=config.AR_ACTION,
        )

    # strict mode
    if job.get("expected_aspect_ratio"):
        expected = float(job["expected_aspect_ratio"])
        src = "explicit"
    else:
        expected = _default_expected(job)
        src = "default"
    metrics["expected_ar"] = round(expected, 4)
    metrics["expected_source"] = src

    diff = abs(content_ar - expected) / expected
    metrics["diff"] = round(diff, 4)
    passed = diff <= config.AR_TOLERANCE
    detail = (
        f"expected {expected:.3f} ({src}) got {content_ar:.3f} "
        f"(crop {metrics.get('content_crop')}, {diff * 100:.1f}% off, tol {config.AR_TOLERANCE * 100:.0f}%)"
    )
    return FilterResult(filter_id="aspect_ratio", passed=passed, detail=detail, metrics=metrics, action=config.AR_ACTION)
