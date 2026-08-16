"""Continuity validation filter — rejects downloads with missing segments.

Motivating case (2026-08-16): the 2026-08-04 xpass batch downloaded Peppa
S03-S06 through a CDN that intermittently failed specific HLS segments.
yt-dlp's default skip_unavailable_fragments=True silently DROPPED them after
fragment_retries were exhausted, producing files with exact 5-10s video+audio
holes (74 eps verified; pts gaps of exactly one or two 5s segments). No
existing filter caught this: duration and aspect ratio were unaffected.

The worker now sets skip_unavailable_fragments=False (fragment failures fail
the job → next link / URL auto-block), which covers fragments that FAIL. This
filter is the belt-and-suspenders guarantee: it catches gaps regardless of
cause — including manifest-side discontinuities that never even get attempted
(a segment missing from the m3u8 itself, which yt-dlp cannot fail on).

Detection: ffprobe video packet PTS scan; any inter-packet jump > threshold
(SP_CONT_GAP_THRESHOLD, default 0.5s — a 25/30fps frame delta is ~0.04s) is a
missing segment. Detector-inconclusive (probe error / no packets) PASSES with
a note — never reject a file we could not actually evaluate.
"""
from __future__ import annotations

import subprocess

from .. import config
from . import FilterResult, register


@register("continuity")
def continuity(path: str, job: dict) -> FilterResult:
    cmd = [
        config.CONT_FFPROBE_BIN, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "packet=pts_time",
        "-of", "csv=p=0",
        path,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except Exception as e:  # noqa: BLE001 — probe failure must not sink the job
        return FilterResult(
            "continuity", True,
            detail=f"probe error (treated as pass): {type(e).__name__}: {e}",
        )
    if r.returncode != 0:
        return FilterResult(
            "continuity", True,
            detail=f"ffprobe rc={r.returncode} (treated as pass): {r.stderr.strip()[:120]}",
        )

    gaps: list[tuple[float, float, float]] = []
    prev = None
    n_packets = 0
    for line in r.stdout.splitlines():
        try:
            t = float(line.strip())
        except ValueError:
            continue
        n_packets += 1
        if prev is not None and t - prev > config.CONT_GAP_THRESHOLD:
            gaps.append((round(prev, 2), round(t, 2), round(t - prev, 2)))
        prev = t

    if not gaps:
        return FilterResult(
            "continuity", True,
            detail=f"{n_packets} video packets, no gaps > {config.CONT_GAP_THRESHOLD}s",
            metrics={"packets": n_packets, "gaps": 0},
        )

    detail = "; ".join(f"{a}->{b} (+{d}s)" for a, b, d in gaps[:6])
    if len(gaps) > 6:
        detail += f" ... ({len(gaps)} gaps total)"
    return FilterResult(
        "continuity", False,
        detail=f"{len(gaps)} missing-segment gap(s): {detail}",
        metrics={
            "packets": n_packets,
            "gaps": len(gaps),
            "gap_seconds": round(sum(d for _, _, d in gaps), 2),
        },
        action=config.CONT_ACTION,
    )
