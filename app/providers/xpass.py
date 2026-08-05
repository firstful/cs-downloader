"""Xpass provider — cracked 2026-08-03 (was "video-source API not cracked").

The "uncrackable" xpass video API turned out to be plain page + JSON:
  1. GET https://play.xpass.top/e/{movie|tv}/{tmdbId}[/{s}/{e}]
     -> HTML with inline  var backups=[{"id","name","url": "/vip/<token>/1/playlist.json",...},...]
        and          var suburl="https://sub.1x2.space/api/{movie|tv}/{tmdb}[/{s}/{e}]";
     (cleaner equivalent: GET https://play.xpass.top/data/{movie|tv}/{tmdb}[/{s}/{e}]?autostart=false
      returns the same backups array as JSON)
  2. GET https://play.xpass.top{backup.url}            -> {"playlist":[{"sources":[
        {"file":"https://vip.1x2.space/playlist/<token>/master.m3u8","type":"hls","label":...}]}]}
  3. Sources are plain HLS/MP4; the player cycles backups until one plays, so the
     provider returns EVERY playable source across ALL backups (worker tries each).
     Dead backends return {"file":"/video/error"} — skip those.
Subtitles (optional): GET suburl -> [{"language","url":"/subtitle/.../English.vtt"}] under sub.1x2.space.

Backup URL families (RE 2026-08-05, Bad Monkey S1E3/E4):
  /vip/<tok>/1/playlist.json  -> vip.1x2.space/playlist/<tok>/master.m3u8 (HLS, works; variants
                                 are the /playlist/<tok>/<b64>/index.m3u8 paths Reed pasted)
  /mvid/<tok>/<n>/playlist.json -> VID — cf-master.<ts>.txt?t=...&e=<expired> CDN family (often stale)
  /meg/tv/.../1/playlist.json -> ps1.1x2.space/enproxy/<tok>/master.m3u8
  /mdata/<id>/<n>/playlist.json -> NEW (FIL/WIS/LUL/ARA/BIG/MIX...): tik.1x2.space,
                                 suprox.xpass.top/proxy2/<tok>/2/master.m3u8, dara.1x2.space,
                                 worker-hosted streams (cfbw.p2l.workers.dev/stream/<tok> = BIG,
                                 cflul.bw3.workers.dev/vid = LUL, cfmix.bx8.workers.dev/proxy = MIX).
                                 Worker hosts are gated (404/429 from this host 2026-08-05 —
                                 probably session/geo/token-registry checks); the HLS ones work.
  /vxr/, /vrk/ -> {"file":"/video/error"} (dead)

Verified live 2026-08-03: Peppa Pig (2004) S1E2 + S2E1 — VIP/VID/MEG backends return
working master.m3u8; segments download as MPEG-TS through streamvaultsrc.click proxy.
2026-08-05: Bad Monkey S1E3/E4 VIP masters download (1920x960 HLS, 30s probe OK).
The vip.1x2.space playlist URL Reed provided is exactly what this provider returns.
"""
from __future__ import annotations

import json
import re

import httpx

from ..models import StreamLink
from . import _http
from .base import Provider

PLAYER = "https://play.xpass.top"
CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
HEADERS = {
    "Accept": "*/*",
    "User-Agent": CHROME_UA,
    "Origin": PLAYER,
    "Referer": PLAYER + "/",
}


def _extract_json_var(html: str, name: str):
    """Pull `var <name> = <json>;` from an inline script (balanced-bracket safe)."""
    m = re.search(r"var\s+" + re.escape(name) + r"\s*=\s*", html)
    if not m:
        return None
    start = m.end()
    if start >= len(html) or html[start] not in "[{\"":
        return None
    # walk to the matching close bracket/brace/quote
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(html)):
        ch = html[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == "\"":
                in_str = False
            continue
        if ch == "\"":
            in_str = True
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start:i + 1])
                except Exception:
                    return None
    return None


class XpassProvider(Provider):
    id = "xpass"
    name = "Xpass"
    kind = "source"
    status = "ok"
    note = ("play.xpass.top -> inline backups[] -> playlist.json -> direct HLS "
            "(vip.1x2.space / ps1.1x2.space / svz.greenhavenstore.store). "
            "Video API cracked 2026-08-03 — no obfuscated JS needed, the page "
            "hands over source URLs. Verified Peppa Pig S1E2/S2E1 (VIP+VID+MEG).")

    def search(self, query: str, page: int = 1) -> list:
        return []

    def load(self, url: str):
        return None

    def _page_url(self, req) -> str:
        if req.media_type == "tv":
            if not req.season or not req.episode:
                raise RuntimeError("xpass tv resolve needs season + episode")
            return f"{PLAYER}/e/tv/{req.tmdb_id}/{req.season}/{req.episode}"
        return f"{PLAYER}/e/movie/{req.tmdb_id}"

    def resolve(self, req) -> list[StreamLink]:
        tmdb_id = req.tmdb_id
        if not tmdb_id:
            raise RuntimeError("xpass resolve needs tmdb_id (metadata from /api/load)")

        page_url = self._page_url(req)
        try:
            r = _http.get(page_url, referer=PLAYER + "/", headers=HEADERS)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise RuntimeError(f"xpass page fetch failed: {e}") from e

        backups = _extract_json_var(r.text, "backups")
        if not backups:
            raise RuntimeError("xpass: no backups[] in player page (title may be missing)")

        last_err = None
        links: list[StreamLink] = []
        for b in backups:
            u = (b or {}).get("url")
            if not u:
                continue
            # absolute playlist.json URL (server hands relative paths)
            pu = u if u.startswith("http") else PLAYER + u
            try:
                pr = _http.get(pu, referer=page_url, headers=HEADERS)
                if pr.status_code != 200:
                    last_err = f"{b.get('name')}: HTTP {pr.status_code}"
                    continue
                data = pr.json()
                sources = (data.get("playlist") or [{}])[0].get("sources") or []
                for s in sources:
                    f = (s or {}).get("file") or ""
                    if not f or f == "/video/error":
                        continue
                    if not f.startswith("http"):
                        f = _http.base_url(pu) + f
                    links.append(StreamLink(
                        provider=self.id,
                        url=f,
                        quality=s.get("label") or "HLS",
                        referer=PLAYER + "/",
                        headers={
                            "User-Agent": CHROME_UA,
                            "Origin": PLAYER,
                            "Referer": PLAYER + "/",
                        },
                        note=f"xpass {b.get('name')}",
                    ))
                if not sources:
                    last_err = f"{b.get('name')}: no playable source"
            except (httpx.HTTPError, ValueError) as e:
                last_err = f"{b.get('name')}: {type(e).__name__}: {e}"
                continue
        if not links:
            raise RuntimeError(f"xpass: all backends failed ({last_err or 'unknown'})")
        return links
