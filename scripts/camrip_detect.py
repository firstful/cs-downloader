#!/usr/bin/env python3
"""camrip_detect.py — camrip / telecine detection heuristic for the media library.

Layered signal design (calibrated 2026-08-17 against PAW Patrol: The Dino Movie
(2026) as the confirmed positive and PAW Patrol: The Movie (2021) + PAW Patrol:
The Mighty Movie (2023) as known-good negatives):

  T1 filename tags      CAM/CAMRIP/HDTS/HDCAM/TELESYNC/TELECINE/SCREENER/DVDSCR/R5/WORKPRINT  +0.45
  T2 release window     no digital (type 4/5/6) release date yet, or movie younger than
                        SP_CAM_MIN_DAYS (default 45) since theatrical premiere               +0.40
  T3 film-cadence miss  29.97/30.000 fps on a movie-type title (consumer camcorder cadence)  +0.35
  T4 low audio bitrate  AAC stereo < 110 kbps (legit web rips sit at ~128)                   +0.10
  T5 runtime mismatch   report-only (weight 0): fires ~100% on known-good files here too
                        (2021 file 72min vs TMDB 86, 2023 file 78 vs 88) — no discrimination  0.00
  T6 odd frame size     width % 8 != 0 or height/width outside standard sets (movie-type)    +0.10
  T7 visual softness    sampled-frame edge energy below threshold (--visual / SP_CAM_VISUAL) +0.15
                        (weak — calibrated margin is thin on this library's soft web rips)

Verdict thresholds: >=0.60 CAM (hard reject) · 0.35-0.60 LIKELY_CAM (hard if a
strong file-level signal T1/T3 fired, else soft) · 0.15-0.35 SUSPICIOUS (soft)
· <0.15 CLEAN.

CALIBRATED OUT (don't re-add without fresh evidence; numbers from 2026-08-17,
mean over sampled frames): Laplacian-variance blur — cam 149 avg vs good 164/140
(overlapping, these low-bitrate web rips are all soft); color cast — cam 14.8 vs
good 10.0/5.4 (tracks scene brightness, not cam-ness); per-channel edge fringe —
cam 0.37 vs good 1.10/0.41 (no stable separation). cropdetect jitter: good files
jitter MORE than the cam (scene content). vidstabdetect: writes empty TRF rows
on ffmpeg 4.2.7. Spaced-luma flicker + astats noise floor: scene-content noise.

Runtime: ffprobe metadata only; TMDB lookup only for T2/T5 (--with-tmdb);
T7 visual softness extracts 4 frames (SP_CAM_VISUAL=1 / --visual). stdlib only
(no numpy — it's broken on the CP host: illegal instruction).

Usage:
  camrip_detect.py <file-or-dir>...            # verdict per file
  camrip_detect.py --scan <media-root>         # recursive sweep, all video files
  camrip_detect.py ... --json                  # machine-readable output
  camrip_detect.py ... --visual                # enable T7 frame sampling
Env: TMDB_API_KEY (for T2/T5), SP_CAM_MIN_DAYS (default 45), SP_CAM_VISUAL.
"""
import argparse
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".m4v", ".mov", ".webm", ".wmv", ".ts"}
CAM_TAG_RE = re.compile(
    r"(?i)(?<![a-z0-9])(cam|camrip|hdcam|hdts|telesync|telecine|dvdscr|bdscr|"
    r"screener|workprint|r5rip|r5)(?![a-z0-9])"
)
# Standard-ish widths/heights that appear in legit encodes (incl. 2.35-2.40 crops)
STD_WIDTHS = {640, 704, 720, 854, 960, 1024, 1280, 1366, 1440, 1600, 1696, 1920, 2560, 3840}
STD_HEIGHTS = {360, 480, 540, 576, 720, 800, 816, 1080, 1440, 2160}
MOVIE_LIKE_MIN_SECONDS = 25 * 60  # only movie-length files get the film-cadence/context rules
CAM_FPS_RANGE = (29.9, 30.1)
FILM_FPS_RANGE = (23.5, 25.5)  # 23.976 / 24 / 25 PAL
EDGE_SOFT_THRESHOLD = 6.0  # mean Sobel-edge energy over 6 mid-window 640x360 frames:
# cam ~4.0 (Dino 3.97), good ~7.1/10.8 (Mighty 2023 / Movie 2021) — requires n>=6 + mid-window mean


def run(cmd, timeout=60, binary=False):
    return subprocess.run(cmd, capture_output=True, text=not binary, timeout=timeout)


def probe(path):
    """ffprobe -> {duration_s, size, width, height, fps, vcodec, acodec, abps, arate, sample_rate, channels}"""
    r = run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", path], timeout=120)
    if r.returncode != 0:
        return None
    try:
        d = json.loads(r.stdout)
    except ValueError:
        return None
    fmt = d.get("format", {})
    v = a = None
    for s in d.get("streams", []):
        if s.get("codec_type") == "video" and v is None:
            v = s
        elif s.get("codec_type") == "audio" and a is None:
            a = s
    if not v:
        return None
    def fr(st):
        ar = st.get("avg_frame_rate") or st.get("r_frame_rate") or "0/0"
        try:
            num, den = ar.split("/")
            num, den = float(num), float(den)
            return num / den if den else 0.0
        except (ValueError, ZeroDivisionError):
            return 0.0
    return {
        "duration_s": float(fmt.get("duration", 0) or 0),
        "size": int(fmt.get("size", 0) or 0),
        "width": int(v.get("width", 0) or 0),
        "height": int(v.get("height", 0) or 0),
        "fps": fr(v),
        "vcodec": v.get("codec_name"),
        "acodec": a.get("codec_name") if a else None,
        "abps": int(a.get("bit_rate", 0) or 0) if a else 0,
        "arate": int(a.get("sample_rate", 0) or 0) if a else 0,
        "channels": int(a.get("channels", 0) or 0) if a else 0,
    }


def tmdb_lookup(title, year):
    """TMDB v3: find best movie match, return runtime + release-window facts.
    Returns None on miss/error. Uses the standard TMDB search + release_dates."""
    key = os.environ.get("TMDB_API_KEY")
    if not key:
        return None
    hdr = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) camrip-detect/1.0"}
    try:
        q = urllib.parse.quote(title)
        hits = None
        for url in (f"https://api.themoviedb.org/3/search/movie?api_key={key}"
                    f"&query={q}&year={year}&language=en",
                    f"https://api.themoviedb.org/3/search/movie?api_key={key}"
                    f"&query={q}&language=en"):
            req = urllib.request.Request(url, headers=hdr)
            with urllib.request.urlopen(req, timeout=15) as r:
                cand = json.load(r).get("results", [])
            if cand:
                hits = cand
                break
        if not hits:
            return None
        mid = hits[0]["id"]
        req = urllib.request.Request(
            f"https://api.themoviedb.org/3/movie/{mid}?api_key={key}"
            "&append_to_response=release_dates&language=en", headers=hdr)
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.load(r)
        digital = None
        theatrical = d.get("release_date")
        for rc in d.get("release_dates", {}).get("results", []):
            for rd in rc.get("release_dates", []):
                if rd.get("type") in (4, 5, 6):  # 4=digital, 5=physical, 6=TV
                    ts = rd.get("release_date")
                    if ts and (digital is None or ts < digital):
                        digital = ts
        return {
            "tmdb_id": mid,
            "title": d.get("title"),
            "runtime_min": d.get("runtime"),
            "theatrical": theatrical,
            "digital": digital,
            "status": d.get("status"),
        }
    except Exception:
        return None


def edge_softness(path, duration_s, n=6):
    """Sample n frames in the middle 25%-75% of the file, return mean Sobel-edge
    energy on 640x360 grayscale. Lower = softer. Mid-window skips dark credits/
    intro frames (they depress edge energy and false-flag legit files); the mean
    of >=6 frames is what separates (cam ~4 vs good ~7-11 on 2026-08-17 cal).
    Returns None if sampling fails (then T7 doesn't fire)."""
    try:
        from PIL import Image, ImageFilter, ImageStat
    except ImportError:
        return None
    if not duration_s or duration_s < 60:
        return None
    vals = []
    ts = [0.25 * duration_s + i * (0.5 * duration_s) / (n - 1) for i in range(n)]
    for t in ts:
        r = run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", path,
                 "-frames:v", "1", "-f", "image2pipe", "-vcodec", "bmp", "-"],
                timeout=90, binary=True)
        if r.returncode != 0 or not r.stdout:
            continue
        try:
            g = Image.open(io.BytesIO(r.stdout)).convert("L").resize((640, 360))
            edge = ImageStat.Stat(g.filter(ImageFilter.FIND_EDGES)).mean[0]
            vals.append(edge)
        except Exception:
            continue
    return sum(vals) / len(vals) if vals else None


def _fps_str(fps):
    if not fps:
        return "?"
    return f"{fps:.3f}"


def detect(path, tmdb=None, movie_type=True, min_days=None, visual=None):
    """Run the full heuristic. tmdb: dict from tmdb_lookup() (optional).
    visual: None=env SP_CAM_VISUAL, True/False to force. Returns
    {verdict, score, signals, summary, meta}."""
    if min_days is None:
        min_days = float(os.environ.get("SP_CAM_MIN_DAYS", "45"))
    if visual is None:
        visual = os.environ.get("SP_CAM_VISUAL", "").strip() not in ("", "0", "false", "False")
    base = os.path.basename(path)
    sigs = []

    def sig(name, weight, fired, detail=""):
        sigs.append({"signal": name, "weight": weight, "fired": bool(fired), "detail": detail})
        return bool(fired)

    # T1 filename tags
    tag = CAM_TAG_RE.search(base)
    t1 = sig("filename_tag", 0.45, bool(tag), f"tag '{tag.group(1).upper()}' in name" if tag else "")

    p = probe(path)
    if not p:
        return {"verdict": "UNKNOWN", "score": 0.0, "signals": sigs,
                "summary": "ffprobe failed", "meta": {"path": path}}
    meta = {"path": path, "duration_s": p["duration_s"], "size": p["size"],
            "width": p["width"], "height": p["height"], "fps": p["fps"],
            "vcodec": p["vcodec"], "acodec": p["acodec"], "abps": p["abps"]}

    movie_like = p["duration_s"] >= MOVIE_LIKE_MIN_SECONDS and movie_type

    # T3 film-cadence miss (movie-type only)
    t3 = False
    if movie_like and p["fps"]:
        t3 = sig("fps30_camcorder_cadence", 0.35,
                 CAM_FPS_RANGE[0] <= p["fps"] <= CAM_FPS_RANGE[1],
                 f"{_fps_str(p['fps'])} fps (expect ~23.976/24/25 for film)")
        if not (CAM_FPS_RANGE[0] <= p["fps"] <= CAM_FPS_RANGE[1]
                or FILM_FPS_RANGE[0] <= p["fps"] <= FILM_FPS_RANGE[1]):
            sig("unusual_fps", 0.10, True, f"{_fps_str(p['fps'])} fps")

    # T4 low audio bitrate
    if p["acodec"] and p["abps"]:
        t4 = sig("low_audio_bitrate", 0.10, p["abps"] < 110_000 and p["channels"] <= 2,
                 f"{p['abps']/1000:.0f} kbps {p['acodec']} {p['arate']}Hz {p['channels']}ch")

    # T6 odd frame size
    odd = (p["width"] and p["width"] % 8 != 0) or (
        p["width"] and p["height"]
        and p["width"] not in STD_WIDTHS and p["height"] not in STD_HEIGHTS)
    t6 = sig("odd_frame_size", 0.10, movie_like and odd,
             f"{p['width']}x{p['height']} (non-standard)" if odd else f"{p['width']}x{p['height']}")

    # T5 runtime mismatch (report-only: 0 weight — legit files trip it too, see docstring)
    t5 = False
    if tmdb and tmdb.get("runtime_min") and p["duration_s"]:
        diff = abs(p["duration_s"] / 60.0 - tmdb["runtime_min"]) / tmdb["runtime_min"]
        t5 = sig("runtime_mismatch", 0.0, diff > 0.05,
                 f"file {p['duration_s']/60:.1f}min vs TMDB {tmdb['runtime_min']}min ({diff*100:.1f}%)")

    # T2 release window (needs TMDB)
    t2 = False
    if tmdb:
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).date().isoformat()
        digital = tmdb.get("digital")
        theatrical = tmdb.get("theatrical")
        if digital and digital <= today:
            reason = ""  # a legit rip can exist — no context flag
        elif not theatrical and not digital:
            reason = "no release date on TMDB (unreleased/announced title)"
        elif theatrical and (today < theatrical):
            reason = f"premiere {theatrical} is in the future"
        elif theatrical and (not digital or digital > today):
            days = (datetime.fromisoformat(today) - datetime.fromisoformat(theatrical)).days
            if days < min_days:
                reason = (f"only {days}d since theatrical {theatrical}, "
                          f"no digital release yet")
            else:
                reason = ""
        else:
            reason = ""
        t2 = sig("pre_digital_release_window", 0.40, bool(reason), reason or "")

    # T7 visual softness (sampled-frame edge energy; weak corroborating signal)
    t7 = False
    if visual and movie_like:
        ee = edge_softness(path, p["duration_s"])
        if ee is not None:
            t7 = sig("visual_softness", 0.15, ee < EDGE_SOFT_THRESHOLD,
                     f"mean edge energy {ee:.2f} (threshold {EDGE_SOFT_THRESHOLD})")

    score = sum(s["weight"] for s in sigs if s["fired"])
    if score >= 0.60:
        verdict = "CAM"
    elif score >= 0.35:
        verdict = "LIKELY_CAM"
    elif score >= 0.15:
        verdict = "SUSPICIOUS"
    else:
        verdict = "CLEAN"

    fired = [s for s in sigs if s["fired"]]
    if not fired:
        summary = "no cam signals"
    else:
        summary = "; ".join(f"{s['signal']} ({s['detail']})" for s in fired)
    return {"verdict": verdict, "score": round(score, 2), "signals": sigs,
            "summary": summary, "meta": meta}


def extract_title_year(path):
    """'The Matrix (1999)/The Matrix (1999).mkv' -> ('The Matrix', '1999') or (None, None)."""
    base = os.path.basename(path)
    base = re.sub(r"\.(mp4|mkv|avi|m4v|mov|webm|wmv|ts)$", "", base, flags=re.I)
    m = re.search(r"(.*?)\s*\((\d{4})\)", base)
    if m:
        return m.group(1).strip(), m.group(2)
    return None, None


def iter_videos(roots):
    seen = set()
    for root in roots:
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                if os.path.splitext(fn)[1].lower() in VIDEO_EXTS:
                    p = os.path.join(dirpath, fn)
                    if p not in seen:
                        seen.add(p)
                        yield p


def main():
    ap = argparse.ArgumentParser(description="Camrip detection heuristic")
    ap.add_argument("paths", nargs="*", help="files or directories")
    ap.add_argument("--scan", action="store_true", help="recursive sweep of given roots; "
                    "per-title TMDB release-window context is applied")
    ap.add_argument("--with-tmdb", action="store_true",
                    help="enable TMDB title lookups (T2 release window + T5 runtime)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--visual", action="store_true",
                    help="enable T7 sampled-frame visual softness (default: env SP_CAM_VISUAL)")
    ap.add_argument("--min-score", type=float, default=0.15,
                    help="only report files with score >= this (sweep mode)")
    args = ap.parse_args()

    if args.scan:
        files = list(iter_videos([p for p in args.paths if os.path.isdir(p)]))
    else:
        files = [p for p in args.paths if os.path.isfile(p)]
    if not files:
        sys.stderr.write("no video files found\n")
        return 1

    out = []
    for i, f in enumerate(files):
        title, year = extract_title_year(f)
        tmdb = None
        if args.with_tmdb and title and year:
            tmdb = tmdb_lookup(title, year)
            time.sleep(0.15)  # be polite to TMDB
        is_movie_dir = "/Movies" in f or "Kids_Movies" in f or "Adult_Movies" in f
        res = detect(f, tmdb=tmdb, movie_type=is_movie_dir, visual=args.visual)
        res["path"] = f
        if res["score"] >= args.min_score or not args.scan:
            out.append(res)
        if args.scan and (i + 1) % 25 == 0:
            sys.stderr.write(f"  scanned {i+1}/{len(files)}...\n")

    if args.json:
        print(json.dumps(out, indent=2))
        return 0
    print(f"{'VERDICT':<12}{'SCORE':>6}  FILE  --  signals")
    for r in sorted(out, key=lambda x: -x["score"]):
        print(f"{r['verdict']:<12}{r['score']:>6.2f}  {r['path']}  --  {r['summary']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())