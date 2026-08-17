"""Camrip / telecine validation filter.

Rejects downloads that are theater camcorder recordings ("camrips") instead of
legit digital rips. Calibrated 2026-08-17 on a confirmed positive (PAW Patrol:
The Dino Movie 2026 — 30.000 fps, 1696x720, 97 kbps audio, TMDB premiere
2026-07-23 with NO digital release date, whisper-garbled subs) and known-good
negatives (PAW Patrol: The Movie 2021 + Mighty Movie 2023 — both 23.976 fps,
128 kbps, released years ago). Full rationale + what was calibrated OUT
(cropdetect jitter, spaced-luma flicker, astats noise floor, vidstabdetect,
Laplacian-variance blur, color cast): scripts/camrip_detect.py (same weights).

Signals (additive score, CAM >= 0.60, LIKELY_CAM >= 0.35, SUSPICIOUS >= 0.15):
  T1 filename tags     CAM/CAMRIP/HDTS/HDCAM/TELESYNC/...            +0.45
  T2 release window    no digital release date yet on TMDB, or movie  +0.40
                       younger than SP_CAM_MIN_DAYS (45) since premiere
  T3 film-cadence miss 29.97/30.000 fps on a movie-type title         +0.35
  T4 low audio bitrate AAC stereo < 110 kbps                          +0.10
  T7 visual softness   sampled 6-frame mean edge energy < threshold   +0.15
                       (only when Pillow is installed; auto-skipped otherwise)

Enforcement (SP_CAM_ACTION, default hard — Reed: cams must not land):
  CAM / LIKELY_CAM -> passed=False, action=hard  (discard, try next link)
  SUSPICIOUS       -> passed=False, action=soft  (keep file + filter_warning)
  CLEAN / inconclusive -> passed=True
Flip SP_CAM_ACTION=soft to downgrade everything to warn-only.
"""
from __future__ import annotations

import io
import os
import re
import subprocess
from datetime import datetime, timezone
from typing import Optional

from .. import config
from ..providers.tmdb import TMDBProvider
from . import FilterResult, register
from .duration import _tmdb_id

CAM_TAG_RE = re.compile(
    r"(?i)(?<![a-z0-9])(cam|camrip|hdcam|hdts|telesync|telecine|dvdscr|bdscr|"
    r"screener|workprint|r5rip|r5)(?![a-z0-9])"
)
CAM_FPS_RANGE = (29.9, 30.1)
EDGE_SOFT_THRESHOLD = 6.0  # 6 mid-window 640x360 frames: cam ~4, good ~7-11

# Weights mirror scripts/camrip_detect.py — keep them in sync.
W = {
    "T1": 0.45,
    "T2": 0.40,
    "T3": 0.35,
    "T4": 0.10,
    "T7": 0.15,
}

_BMP_PROBE = ["ffmpeg", "-v", "error", "-ss", None, "-i", None,
              "-frames:v", "1", "-f", "image2pipe", "-vcodec", "bmp", "-"]


def _probe_full(path: str) -> dict:
    """ffprobe: duration, size, fps, audio bitrate for the scoring path."""
    cmd = [config.AR_FFPROBE_BIN, "-v", "error",
           "-show_entries", "format=duration,size",
           "-show_entries", "stream=codec_type,width,height,avg_frame_rate,"
                            "r_frame_rate,bit_rate,channels",
           "-of", "json", path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        data = __import__("json").loads(r.stdout or "{}")
    except Exception:  # noqa: BLE001
        return {}
    fmt = data.get("format") or {}
    v = a = None
    for s in data.get("streams") or []:
        if s.get("codec_type") == "video" and v is None:
            v = s
        elif s.get("codec_type") == "audio" and a is None:
            a = s
    if not v:
        return {}
    def _fps(s):
        try:
            num, den = (s.get("avg_frame_rate") or s.get("r_frame_rate") or "0/0").split("/")
            return float(num) / float(den) if float(den) else 0.0
        except (ValueError, ZeroDivisionError):
            return 0.0
    return {
        "duration_s": float(fmt.get("duration", 0) or 0),
        "width": int(v.get("width", 0) or 0),
        "height": int(v.get("height", 0) or 0),
        "fps": _fps(v),
        "abps": int((a or {}).get("bit_rate", 0) or 0),
        "channels": int((a or {}).get("channels", 0) or 0),
    }


def _tmdb_context(tid: int) -> Optional[dict]:
    """Release-window + runtime facts for T2/T5, via the app's TMDBProvider."""
    tmdb = TMDBProvider()
    d = tmdb._get(f"/movie/{tid}")
    if not d:
        return None
    theatrical = d.get("release_date")
    runtime_min = d.get("runtime")
    digital = None
    rd = tmdb._get(f"/movie/{tid}/release_dates")
    for rc in (rd or {}).get("results", []):
        for item in rc.get("release_dates", []):
            if item.get("type") in (4, 5, 6):
                ts = item.get("release_date")
                if ts and (digital is None or ts < digital):
                    digital = ts
    return {"runtime_min": runtime_min, "theatrical": theatrical, "digital": digital}


def _edge_softness(path: str, duration_s: float, n: int = 6) -> Optional[float]:
    """Mean Sobel-edge energy over n frames in the middle 25%-75% of the file.
    None if Pillow missing (T7 auto-skips) or sampling fails."""
    try:
        from PIL import Image, ImageFilter, ImageStat
    except ImportError:
        return None
    if not duration_s or duration_s < 60:
        return None
    vals = []
    for i in range(n):
        t = 0.25 * duration_s + i * (0.5 * duration_s) / (n - 1)
        cmd = list(_BMP_PROBE)
        cmd[3] = str(t)
        cmd[5] = path
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=120)
        except Exception:  # noqa: BLE001
            continue
        if r.returncode != 0 or not r.stdout:
            continue
        try:
            g = Image.open(io.BytesIO(r.stdout)).convert("L").resize((640, 360))
            vals.append(ImageStat.Stat(g.filter(ImageFilter.FIND_EDGES)).mean[0])
        except Exception:  # noqa: BLE001
            continue
    return (sum(vals) / len(vals)) if vals else None


@register("camrip")
def check(path: str, job: dict) -> FilterResult:
    p = _probe_full(path)
    if not p or not p.get("duration_s"):
        return FilterResult(
            filter_id="camrip", passed=True,
            detail="inconclusive (no readable video stream)",
            metrics={"note": "probe failed"}, action=config.CAM_ACTION)

    is_movie = job.get("media_type") == "movie"
    base = os.path.basename(path)
    score = 0.0
    fired: list[str] = []

    m = CAM_TAG_RE.search(base)
    if m:
        score += W["T1"]
        fired.append(f"filename_tag:{m.group(1).upper()}")

    if is_movie and p["duration_s"] >= config.CAM_MIN_DURATION_S:
        if CAM_FPS_RANGE[0] <= p["fps"] <= CAM_FPS_RANGE[1]:
            score += W["T3"]
            fired.append(f"fps30:{p['fps']:.3f}fps")

        if p["abps"] and p["channels"] <= 2 and p["abps"] < 110_000:
            score += W["T4"]
            fired.append(f"audio:{p['abps']/1000:.0f}kbps")

        # T7 visual softness (auto-skips without Pillow)
        if os.environ.get("SP_CAM_VISUAL", "1") not in ("0", "false", "False"):
            ee = _edge_softness(path, p["duration_s"])
            if ee is not None and ee < EDGE_SOFT_THRESHOLD:
                score += W["T7"]
                fired.append(f"visual_softness:{ee:.2f}")

        tid = _tmdb_id(job)
        if tid:
            ctx = _tmdb_context(tid)
            if ctx:
                today = datetime.now(timezone.utc).date().isoformat()
                digital, theatrical = ctx.get("digital"), ctx.get("theatrical")
                reason = ""
                if not theatrical and not digital:
                    reason = "no release date on TMDB (unreleased/announced title)"
                elif theatrical and today < theatrical:
                    reason = f"premiere {theatrical} is in the future"
                elif theatrical and (not digital or digital > today):
                    days = (datetime.fromisoformat(today)
                            - datetime.fromisoformat(theatrical)).days
                    if days < config.CAM_MIN_DAYS:
                        reason = f"{days}d since theatrical {theatrical}, no digital release"
                if reason:
                    score += W["T2"]
                    fired.append(f"release_window:{reason}")

    if score >= 0.60:
        verdict, passed, action = "CAM", False, config.CAM_ACTION
    elif score >= 0.35:
        # LIKELY_CAM rejects hard only when a STRONG signal convicted it (T1 tag
        # or T2 release window); bare fps30+low-audio on an old title is a
        # soft warn (could be a legit broadcast capture of a released film).
        strong = any(s.startswith(("filename_tag", "release_window")) for s in fired)
        verdict, passed, action = "LIKELY_CAM", False, config.CAM_ACTION if strong else "soft"
    elif score >= 0.15:
        verdict, passed, action = "SUSPICIOUS", False, "soft"
    else:
        verdict, passed, action = "CLEAN", True, config.CAM_ACTION

    detail = f"{verdict} (score {score:.2f})"
    if fired:
        detail += " — " + "; ".join(fired)
    metrics = {
        "cam_score": round(score, 2),
        "cam_verdict": verdict,
        "signals": fired,
        "fps": round(p.get("fps", 0), 3),
        "audio_bitrate": p.get("abps"),
    }
    return FilterResult(
        filter_id="camrip", passed=passed, detail=detail,
        metrics=metrics, action=action)