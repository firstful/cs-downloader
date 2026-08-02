"""2Embed provider — transcribed from the StreamPlay extension's invoke2embed.

Verified end-to-end 2026-08-02 (The Shawshank Redemption, tt0111161):
  POST https://www.2embed.cc/embed/{imdb} (pls=pls)  -> iframe#iframesrc data-src
  id = data-src query param, ref = data-src origin
  https://uqloads.xyz/e/{id}  (referer: ref/)       -> Dean-Edwards packed JWPlayer JS
  links.hls3 (master.txt) — hls2 returns 403 on the CDN, player falls back to hls3
"""
from __future__ import annotations

import re

import httpx

from ..models import StreamLink
from .base import Provider
from . import _http


class TwoEmbedProvider(Provider):
    id = "2embed"
    name = "2Embed"
    kind = "source"
    status = "ok"
    note = "movies+TV via imdbId -> uqloads embed -> HLS (verified)"

    EMBED = "https://www.2embed.cc"
    UQLOAD = "https://uqloads.xyz"

    def resolve(self, req) -> list[StreamLink]:
        if req.media_type == "tv":
            if not req.season or not req.episode:
                raise ValueError("tv links need season + episode")
            url = f"{self.EMBED}/embedtv/{req.imdb_id}&s={req.season}&e={req.episode}"
        else:
            url = f"{self.EMBED}/embed/{req.imdb_id}"

        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        try:
            r = _http.post(url, data={"pls": "pls"}, referer=url, headers=headers)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise RuntimeError(f"2embed embed fetch failed: {e}") from e

        m = re.search(r'<iframe[^>]*id="iframesrc"[^>]*data-src="([^"]+)"', r.text)
        if not m:
            raise RuntimeError("2embed: no iframesrc found (title may be missing on source)")
        framesrc = m.group(1)

        idm = re.search(r"id=([^&]+)", framesrc)
        if not idm:
            raise RuntimeError("2embed: no id in iframesrc")
        fid = idm.group(1)
        ref = _http.base_url(framesrc)

        uq_url = f"{self.UQLOAD}/e/{fid}"
        try:
            r2 = _http.get(uq_url, referer=f"{ref}/")
            r2.raise_for_status()
        except httpx.HTTPError as e:
            raise RuntimeError(f"uqloads fetch failed: {e}") from e

        decoded = _http.unpack_packed_js(r2.text)
        hls = _http.extract_hls_from_packed(decoded)
        if not hls:
            raise RuntimeError("2embed: no HLS source in uqloads player")

        links = []
        for u in hls:
            links.append(
                StreamLink(
                    provider=self.id,
                    url=u,
                    quality="auto",
                    headers={"Referer": f"{self.UQLOAD}/"},
                    referer=f"{self.UQLOAD}/",
                    note="hls3 preferred; hls2 often 403",
                )
            )
        return links
