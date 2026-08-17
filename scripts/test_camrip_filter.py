#!/usr/bin/env python3
"""test_camrip_filter.py — synthetic-video E2E for the camrip filter.

Pattern (mirrors test_aspect_ratio_filter.py): generate short clips with known
characteristics, run the filter, assert verdicts. Synthetic clips keep it fast
and deterministic — no real files, no TMDB calls (tmdb_id omitted → T2 can't
fire; the test covers the file-level signals T3/T4/T7).

Cases (SP_CAM_MIN_DURATION_S=10, so >=10s clips are "movie-like" in the test):
  cam30       — 30.000 fps + quiet compressed 96k audio      → rejected (hard)
  good24000   — 23.976 fps + pink-noise 128k audio           → CLEAN
  cam_tag     — 23.976 fps, normal audio, "CAM" in filename  → rejected (hard)
  good_short  — 5s clip (below the floor)                    → CLEAN

Run: SP_CAM_VISUAL=0 SP_CAM_MIN_DURATION_S=10 python3 scripts/test_camrip_filter.py
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Tests use short clips; they must not be excluded by the movie-length floor.
os.environ.setdefault("SP_CAM_MIN_DURATION_S", "10")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root

from app.filters.camrip import check as cam_check  # noqa: E402


def synth(path: Path, fps: str, seconds: int = 60, audio_kbps: int = 128,
          noise: bool = False):
    """Encode a synthetic clip: testsrc2 video + sine (compressed, cam-like)
    or pink-noise (incompressible, clean-like) audio."""
    src = (f"anoisesrc=d={seconds}:colour=pink" if noise
           else f"sine=frequency=440:duration={seconds}")
    cmd = [
        "ffmpeg", "-v", "error", "-f", "lavfi",
        "-i", f"testsrc2=s=640x360:r={fps}:d={seconds}",
        "-f", "lavfi", "-i", src,
        "-b:a", f"{audio_kbps}k",
        "-c:v", "libx264", "-preset", "ultrafast", "-g", "25",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def main() -> int:
    fails = 0
    total = 4
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)

        cam30 = td / "cam30.mp4"
        synth(cam30, "30", audio_kbps=96)
        good = td / "good24000.mp4"
        synth(good, "24000/1001", audio_kbps=128, noise=True)
        camtag = td / "Paw Patrol CAM.mp4"   # 23.976 fps but tag in filename
        synth(camtag, "24000/1001", audio_kbps=128, noise=True)
        short = td / "short5s.mp4"
        synth(short, "24000/1001", seconds=5, audio_kbps=128, noise=True)

        cases = [
            # label, path, expect_rejected, expect_hard, expect_verdict_hint
            ("cam30", cam30, True, False, "LIKELY_CAM"),   # bare fps30+audio → soft warn
            ("good24000", good, False, False, "CLEAN"),
            ("cam_tag", camtag, True, True, "LIKELY_CAM"),  # tag fired → hard
            ("good_short", short, False, False, "CLEAN"),
        ]
        for label, path, want_rejected, want_hard, want_hint in cases:
            job = {"media_type": "movie", "tmdb_id": None}
            res = cam_check(str(path), job)
            ok = (res.passed != want_rejected
                  and want_hint in res.detail
                  and ((res.action == "hard") == want_hard or not want_rejected))
            print(f"{label:<12} passed={res.passed} action={res.action} | {res.detail}"
                  f"{'  OK' if ok else '  FAIL'}")
            if not ok:
                fails += 1
    print(f"\n{total - fails}/{total} passed" + ("  ALL OK" if fails == 0 else "  FAILURES"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())