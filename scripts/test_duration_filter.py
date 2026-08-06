#!/usr/bin/env python3
"""Duration filter tests: synthetic clips (deterministic) + real Furious files.

Run from the repo root:  python3 scripts/test_duration_filter.py
Synthetic cases pin job["expected_runtime_minutes"] so no TMDB is needed.
Real-world cases hit TMDB (live network) — skipped if the media files are gone.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.filters.duration import check  # noqa: E402

FURIOUS_DIR = "/mnt/raid_store/jellyfin/Media/Adult_TV/Furious (2026)/Season 01"
PASS, FAIL = "PASS", "FAIL"
fails = 0
total = 0


def expect(name: str, result, want_passed: bool, action: str = "hard"):
    global fails, total
    total += 1
    ok = result.passed == want_passed and result.action == action
    if not ok:
        fails += 1
    print(f"[{'ok ' if ok else 'BAD'}] {name}: passed={result.passed} action={result.action} :: {result.detail}")


def make_clip(path: str, seconds: int):
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi",
         "-i", f"testsrc2=s=320x180:r=25:d={seconds}",
         "-c:v", "libx264", "-g", "25", "-pix_fmt", "yuv420p", path],
        check=True,
    )


# ---- synthetic: compare logic via pinned expected runtime ----
with tempfile.TemporaryDirectory() as td:
    clip = os.path.join(td, "clip.mp4")
    make_clip(clip, 120)  # 2.0 min

    expect("exact match (2.0 vs 2.0)", check(clip, {"expected_runtime_minutes": 2.0}), True)
    expect("slight over (2.0 vs 1.5)", check(clip, {"expected_runtime_minutes": 1.5}), True)
    expect("movie-for-episode 2x (2.0 vs 1.0)", check(clip, {"expected_runtime_minutes": 1.0}), False)
    expect("under by half (2.0 vs 4.0)", check(clip, {"expected_runtime_minutes": 4.0}), False)
    expect("short cut still in bounds (0.75 ratio)", check(clip, {"expected_runtime_minutes": 2.67}), True)  # 2.0/2.67=0.75

    # inconclusive: no expected anywhere -> PASS with note, never sink
    r = check(clip, {})
    total += 1
    if r.passed and "inconclusive" in r.detail:
        print(f"[ok ] no-metadata inconclusive: {r.detail}")
    else:
        fails += 1
        print(f"[BAD] no-metadata inconclusive: passed={r.passed} :: {r.detail}")

# ---- real-world: Furious (2026) junk vs legit ----
if os.path.isdir(FURIOUS_DIR):
    print("\n-- real-world: Furious (2026) --")
    expect("E02 legit (2embed, 52.9 min)",
           check(os.path.join(FURIOUS_DIR, "Furious S01E02.mp4"),
                 {"media_type": "tv", "season": 1, "episode": 2, "imdb_id": "tmdb://287238"}),
           True)
    expect("E03 junk (hindmoviez movie rip, 113 min)",
           check(os.path.join(FURIOUS_DIR, "Furious S01E03.mkv"),
                 {"media_type": "tv", "season": 1, "episode": 3, "imdb_id": "tmdb://287238"}),
           False)
    expect("E06 junk + un-aired (season-median fallback)",
           check(os.path.join(FURIOUS_DIR, "Furious S01E06.mkv"),
                 {"media_type": "tv", "season": 1, "episode": 6, "imdb_id": "tmdb://287238"}),
           False)
else:
    print("\n-- real-world skipped: Furious dir not found --")

print(f"\n{total - fails}/{total} passed")
sys.exit(1 if fails else 0)
