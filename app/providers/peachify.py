"""Peachify provider — chain reverse-engineered from StreamPlay.cs3 v658 + smy778/EncDecEndpoints.

Status: EXPERIMENTAL (2026-08-05). The chain is fully verified against the live
backends (encrypted payload -> enc-dec.app dec-peachify -> schema-correct result),
but every server currently returns EMPTY sources for all 18 titles probed (incl.
exact sample params from EncDecEndpoints/samples/peachify.py). Backend appears
hollow/rotated. Flip status to "ok" ONLY after a real E2E download succeeds.

Flow (NOT website scraping — the working chain from the extension itself):
  1. GET {api}/{server}/{movie|tv}/{tmdbId}[/{season}/{episode}]   (headers: peachify Origin/Referer)
       -> {"isEncrypted": true, "data": "<encrypted>"}
  2. POST https://enc-dec.app/api/dec-peachify  body {"text": <encrypted>}
       -> {"status": 200, "result": {"type", "tmdbId", "providerName", "sources": [...], "subtitles": [...]}}

Verified live 2026-08-05:
  - GoT tmdb 1399 s1e1 + Interstellar + Dune + 13 more titles x {holly,multi,air}:
    encrypted payload fetched, dec-peachify returns 200, result echoes tmdbId/season/episode,
    but sources=[] always.
  - moviebox (uwu.eat-peach.sbs) = 522 dead; uwu.peachify.top/moviebox = HTML (wrong host);
    ice/net = "Provider not Found".
  - The extension's server list: usa.eat-peach.sbs/{holly,multi,ice,air,net} + uwu.peachify.top/moviebox.
"""
from __future__ import annotations

import json

import httpx

from ..models import LoadResponse, SearchResult, StreamLink
from . import _http
from .base import Provider

SERVERS = [
    ("holly", "https://usa.eat-peach.sbs"),
    ("multi", "https://usa.eat-peach.sbs"),
    ("ice", "https://usa.eat-peach.sbs"),
    ("air", "https://usa.eat-peach.sbs"),
    ("net", "https://usa.eat-peach.sbs"),
    ("moviebox", "https://uwu.peachify.top"),
]
DEC_API = "https://enc-dec.app/api/dec-peachify"
PEACHIFY_REF = "https://peachify.top/"
CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36")
HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.5",
    "Origin": "https://peachify.top",
    "Referer": PEACHIFY_REF,
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "cross-site",
}


class PeachifyProvider(Provider):
    id = "peachify"
    name = "Peachify"
    kind = "source"
    status = "experimental"   # backend returns empty sources as of 2026-08-05
    note = ("eat-peach.sbs servers -> encrypted data -> enc-dec.app dec-peachify. "
            "Chain verified live but all servers return empty sources (backend "
            "hollow/rotated); moviebox dead (522). Re-verify before enabling.")

    def search(self, query: str, page: int = 1) -> list[SearchResult]:
        # Peachify has no own search; metadata comes from TMDB provider.
        return []

    def load(self, url: str) -> LoadResponse | None:
        return None

    def _fetch_encrypted(self, api: str, server: str, req) -> str | None:
        kind = "tv" if req.media_type == "tv" else "movie"
        url = f"{api}/{server}/{kind}/{req.tmdb_id}"
        if req.media_type == "tv":
            url += f"/{req.season}/{req.episode}"
        try:
            _http.acquire_ext_api()  # shared third-party API cap
            r = _http.get(url, headers=HEADERS)
            if r.status_code != 200:
                return None
            d = r.json()
            return d.get("data") if isinstance(d, dict) else None
        except Exception:
            return None

    def _decrypt(self, enc_text: str) -> dict | None:
        try:
            _http.acquire_ext_api()
            r = _http.post(
                DEC_API,
                data=json.dumps({"text": enc_text}),
                headers={"Content-Type": "application/json", "User-Agent": CHROME_UA},
            )
            r.raise_for_status()
            d = r.json()
            return d.get("result") or {}
        except Exception:
            return None

    def resolve(self, req) -> list[StreamLink]:
        if not req.tmdb_id:
            raise RuntimeError("peachify resolve needs tmdb_id (metadata from /api/load)")

        last_err = None
        for server, api in SERVERS:
            try:
                enc = self._fetch_encrypted(api, server, req)
                if not enc:
                    last_err = f"{server}: no encrypted data"
                    continue
                res = self._decrypt(enc)
                if not res:
                    last_err = f"{server}: decrypt failed"
                    continue
                sources = res.get("sources") or []
                if not sources:
                    last_err = f"{server}: empty sources (backend hollow?)"
                    continue

                links: list[StreamLink] = []
                for src in sources:
                    u = (src or {}).get("url") or (src or {}).get("src")
                    if not u:
                        continue
                    links.append(StreamLink(
                        provider=self.id,
                        url=u,
                        quality=src.get("quality"),
                        referer=PEACHIFY_REF,
                        headers={
                            "User-Agent": CHROME_UA,
                            "Origin": "https://peachify.top",
                            "Referer": PEACHIFY_REF,
                        },
                        note=f"peachify {server}",
                    ))
                if links:
                    return links
                last_err = f"{server}: no playable urls in {len(sources)} sources"
            except httpx.HTTPError as e:
                last_err = f"{server}: {type(e).__name__}: {e}"
                continue
        raise RuntimeError(f"peachify: all servers failed ({last_err or 'unknown'})")
