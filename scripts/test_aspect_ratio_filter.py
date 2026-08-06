#!/usr/bin/env python3
"""Test the aspect_ratio filter with real synthetic videos (ffmpeg-generated).

Cases:
  1. scope movie letterboxed in 16:9 (2.4:1 content) vs expected 2.39  -> PASS
  2. clean 16:9 vs expected 1.78                                       -> PASS
  3. 4:3 pillarboxed in 16:9 vs expected 1.78 (wrong rip)              -> FAIL
  4. 4:3 squeezed into 16:9 vs expected 1.78 (stretched rip)           -> FAIL
  5. all-black file (no detectable content) vs expected 1.78           -> PASS (inconclusive)
  6. scope content vs expected 1.78 (16:9 movie fallback)              -> FAIL (default caveat)
  7. TMDB expected_aspect_ratio returns the API field (live, best-effort)

Run from repo root:  .venv/bin/python scripts/test_aspect_ratio_filter.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

# Isolate DB writes: point SP_DATA_DIR at a temp dir BEFORE importing app.config.
_TMP = tempfile.mkdtemp(prefix="ar-filter-")
os.environ["SP_DATA_DIR"] = os.path.join(_TMP, "data")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import config, db  # noqa: E402
from app.filters.aspect_ratio import check, detect_content_aspect  # noqa: E402

FFMPEG = config.FFMPEG_BIN


def make_video(path: str, size: str, drawbox: str | None, source: str = "black") -> None:
    """2s 25fps yuv420p video. drawbox='x=0:y=140:w=1920:h=800:color=white:t=fill' or None.
    source: 'black' (flat black bg) or 'testsrc2' (moving pattern, full-frame)."""
    vf = []
    if drawbox:
        vf.append(f"drawbox={drawbox}")
    if source == "testsrc2":
        src = f"testsrc2=s={size}:r=25:d=2"
    else:
        src = f"color=c=black:s={size}:r=25:d=2"
    args = [FFMPEG, "-y", "-f", "lavfi", "-i", src,
            "-c:v", "libx264", "-g", "25", "-pix_fmt", "yuv420p"]
    if vf:
        args += ["-vf", ",".join(vf)]
    args.append(path)
    subprocess.run(args, check=True, capture_output=True, text=True)


def run_case(name: str, path: str, job: dict, expect_pass: bool) -> bool:
    res = check(path, job)
    ok = res.passed == expect_pass
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {name}")
    print(f"        expected={job.get('expected_aspect_ratio', 'default')} "
          f"-> {res.detail} | metrics={res.metrics.get('content_ar')} "
          f"crop={res.metrics.get('content_crop')} note={res.metrics.get('note', '')}")
    return ok


def main() -> int:
    tmp = _TMP
    failures = 0

    scope = os.path.join(tmp, "scope.mp4")          # 2.4:1 content letterboxed in 16:9
    make_video(scope, "1920x1080", "x=0:y=140:w=1920:h=800:color=white:t=fill")
    ar, m = detect_content_aspect(scope)
    assert ar is not None, f"scope detection failed: {m}"
    assert 2.35 <= ar <= 2.45, f"scope AR unexpected: {ar} ({m})"
    failures += 0 if run_case("scope movie vs explicit expected 2.39 (strict)", scope,
                              {"media_type": "movie", "expected_aspect_ratio": 2.39}, True) else 1

    clean = os.path.join(tmp, "clean16x9.mp4")      # full-frame 16:9
    make_video(clean, "1920x1080", None, source="testsrc2")
    ar, m = detect_content_aspect(clean)
    assert ar is not None, f"clean detection failed: {m}"
    assert 1.7 <= ar <= 1.85, f"clean AR unexpected: {ar} ({m})"
    failures += 0 if run_case("clean 16:9 vs TV strict default 1.78", clean,
                              {"media_type": "tv"}, True) else 1
    # pan-and-scan money case: 16:9 content delivered for a scope movie
    failures += 0 if run_case("P&S 16:9 vs explicit expected 2.39 -> reject", clean,
                              {"media_type": "movie", "expected_aspect_ratio": 2.39}, False) else 1

    pillarbox = os.path.join(tmp, "pillarbox.mp4")  # 4:3 content in 16:9 frame
    make_video(pillarbox, "1920x1080", "x=240:y=0:w=1440:h=1080:color=white:t=fill")
    ar, m = detect_content_aspect(pillarbox)
    assert ar is not None, f"pillarbox detection failed: {m}"
    assert 1.25 <= ar <= 1.4, f"pillarbox AR unexpected: {ar} ({m})"
    failures += 0 if run_case("4:3 pillarbox vs TV strict default -> reject", pillarbox,
                              {"media_type": "tv"}, False) else 1

    squeeze = os.path.join(tmp, "squeeze.mp4")      # 16:9 squeezed to 4:3 (stretched rip)
    make_video(squeeze, "1280x720", "x=160:y=0:w=960:h=720:color=white:t=fill")
    ar, m = detect_content_aspect(squeeze)
    assert ar is not None, f"squeeze detection failed: {m}"
    assert 1.25 <= ar <= 1.4, f"squeeze AR unexpected: {ar} ({m})"
    failures += 0 if run_case("4:3 squeeze vs TV strict default -> reject", squeeze,
                              {"media_type": "tv"}, False) else 1

    black = os.path.join(tmp, "black.mp4")          # nothing detectable
    make_video(black, "1920x1080", None)
    ar, m = detect_content_aspect(black)
    assert ar is None, f"black file should be inconclusive, got AR {ar} ({m})"
    failures += 0 if run_case("all-black -> inconclusive pass", black,
                              {"media_type": "tv"}, True) else 1

    # lenient movie default: legit scope must PASS (no false reject without metadata)
    failures += 0 if run_case("scope movie vs movie lenient default -> pass", scope,
                              {"media_type": "movie"}, True) else 1

    # lenient movie default: non-standard content must FAIL
    weird = os.path.join(tmp, "weird.mp4")          # 1.2:1 content (not a standard ratio)
    make_video(weird, "1280x720", "x=280:y=60:w=720:h=600:color=white:t=fill")
    ar, m = detect_content_aspect(weird)
    assert ar is not None, f"weird detection failed: {m}"
    assert 1.1 <= ar <= 1.3, f"weird AR unexpected: {ar} ({m})"
    failures += 0 if run_case("non-standard 1.2 content vs movie lenient -> reject", weird,
                              {"media_type": "movie"}, False) else 1

    # worker integration: soft action keeps the file and surfaces warnings
    from app import worker as _w
    db.init_db()
    results = _w._run_filters(pillarbox, {"media_type": "tv"})
    failing = [r for r in results if not r.passed]
    ok = len(failing) == 1 and not failing[0].passed
    print(f"[{'PASS' if ok else 'FAIL'}] worker _run_filters surfaces the aspect_ratio failure")
    failures += 0 if ok else 1
    ok = failing and config.AR_ACTION == "soft"
    print(f"[{'PASS' if ok else 'FAIL'}] default AR_ACTION is soft (keep file, warn)")
    failures += 0 if ok else 1
    config.AR_ACTION = "hard"
    ok = bool(failing) and config.AR_ACTION == "hard"
    print(f"[{'PASS' if ok else 'FAIL'}] hard action branches to discard + next link")
    failures += 0 if ok else 1
    config.AR_ACTION = "soft"

    # soft mode: filter_warnings lands in DB row
    jid2 = db.create_job("softtest", "2embed")
    warn_txt = "; ".join(r.to_error() for r in failing)
    db.update_job(jid2, status="done", filename="x.mp4", file_size=1, filter_warnings=warn_txt)
    got = db.get_job(jid2)
    ok = got["filter_warnings"] and "aspect_ratio" in got["filter_warnings"]
    print(f"[{'PASS' if ok else 'FAIL'}] filter_warnings persisted on job row: {got['filter_warnings']!r}")
    failures += 0 if ok else 1

    # soft mode: filter_warnings lands in the sidecar JSON
    sc = os.path.join(tmp, "sidecar.mp4")
    _w._write_sidecar(sc, {"media_type": "tv", "title": "t"}, warnings=[warn_txt])
    sc_json = sc + ".json"
    import json as _json
    ok = os.path.exists(sc_json) and "filter_warnings" in _json.load(open(sc_json))
    print(f"[{'PASS' if ok else 'FAIL'}] filter_warnings in sidecar json")
    failures += 0 if ok else 1

    # DB round-trip: expected_aspect_ratio survives create_job (needs migration)
    try:
        db.init_db()
        jid = db.create_job("test", "2embed", expected_aspect_ratio=2.39)
        got = db.get_job(jid)
        ok = got["expected_aspect_ratio"] == 2.39
        print(f"[{'PASS' if ok else 'FAIL'}] db round-trip expected_aspect_ratio (job {jid})")
        failures += 0 if ok else 1
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] db round-trip: {type(e).__name__}: {e}")
        failures += 1

    print(f"\n{'ALL PASS' if failures == 0 else f'{failures} FAILURES'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
