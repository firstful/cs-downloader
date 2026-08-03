"""Videasy provider — reverse-engineered from StreamPlay.cs3 v658 (decompiled 2026-08-02).

Flow (NOT website scraping — the working chain from the extension itself):
  1. GET {api}/seed?mediaId={tmdbId}            -> {"seed": "..."}
  2. GET {api}/{server}/sources-with-title?title={double-URL-encoded-title}
       &mediaType={movie|tv}&year={year}&tmdbId={tmdbId}&imdbId={imdbId}
       &episodeId={ep}&seasonId={season}&enc=2&seed={seed}   -> ENCRYPTED payload
  3. POST https://enc-dec.app/api/dec-videasy  body {"text": <encrypted>, "id": tmdbId, "seed": seed}
       -> {"result": {"sources": [{"quality", "url"}], "subtitles": [{"lang","url"}], "playlist"}}

The returned sources are full m3u8 URLs on moon.ironwallnet.net / lookcrew11.site etc.
Segments are MPEG-TS disguised as .jpg. All requests need the videasy player
Origin/Referer headers (else the CDN 403s).

Verified live 2026-08-02: Peppa Pig S1E1 (tv) + Interstellar (movie) + subs.
"""
from __future__ import annotations

import json
import urllib.parse

import httpx

from ..models import LoadResponse, SearchResult, StreamLink
from . import _http
from .base import Provider

API_BASES = [
    "https://api.speedracelight.com",
    "https://api.wingsdatabase.com",
]
SERVERS = [
    "cdn", "neon2", "ym", "jett", "m4uhd", "hdmovie",
    "meine", "lamovie", "superflix", "downloader2", "vsrc",
]
DEC_API = "https://enc-dec.app/api/dec-videasy"
PLAYER_REF = "https://player.videasy.to/"
CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")
HEADERS = {
    "Accept": "*/*",
    "User-Agent": CHROME_UA,
    "Origin": "https://player.videasy.to",
    "Referer": PLAYER_REF,
}


def _enc_title(title: str) -> str:
    """The extension double-URL-encodes the title (quote(quote(title)))."""
    return urllib.parse.quote(urllib.parse.quote(title, safe=""), safe="")


class VideasyProvider(Provider):
    id = "videasy"
    name = "Videasy"
    kind = "source"
    status = "ok"
    note = ("streaming-embed API. seed handshake -> sources-with-title (enc=2) -> "
            "enc-dec.app decrypt -> moon.ironwallnet.net m3u8 (referer-bound). "
            "Reverse-engineered from StreamPlay.cs3 v658, verified 2026-08-02 "
            "(movie + tv + subs).")

    def search(self, query: str, page: int = 1) -> list[SearchResult]:
        # Videasy has no own search; metadata comes from TMDB provider.
        return []

    def load(self, url: str) -> LoadResponse | None:
        return None

    def _fetch_seed(self, api: str, tmdb_id: int) -> str | None:
        try:
            r = _http.get(f"{api}/seed", params={"mediaId": tmdb_id}, headers=HEADERS)
            r.raise_for_status()
            return (r.json() or {}).get("seed")
        except Exception:
            return None

    def _sources_url(self, api: str, server: str, req, seed: str) -> str:
        # Verified live: only tmdbId (+ seasonId/episodeId for tv) + enc + seed are
        # required; title/year/imdb can be empty and the backend still resolves.
        q = {
            "title": "",
            "mediaType": "tv" if req.media_type == "tv" else "movie",
            "year": "",
            "tmdbId": req.tmdb_id or "",
            "imdbId": req.imdb_id or "",
            "enc": 2,
            "seed": seed,
        }
        if req.media_type == "tv":
            q["episodeId"] = req.episode or ""
            q["seasonId"] = req.season or ""
        return f"{api}/{server}/sources-with-title?" + urllib.parse.urlencode(q)

    def _decrypt(self, enc_text: str, tmdb_id: int, seed: str) -> dict | None:
        try:
            r = _http.post(
                DEC_API,
                data=json.dumps({"text": enc_text, "id": tmdb_id, "seed": seed}),
                headers={"Content-Type": "application/json", "User-Agent": CHROME_UA},
            )
            r.raise_for_status()
            d = r.json()
            return d.get("result") or {}
        except Exception:
            return None

    def resolve(self, req) -> list[StreamLink]:
        tmdb_id = req.tmdb_id
        if not tmdb_id:
            raise RuntimeError("videasy resolve needs tmdb_id (metadata from /api/load)")

        # try API bases in order until a seed works
        seed = None
        api = None
        for base in API_BASES:
            s = self._fetch_seed(base, tmdb_id)
            if s:
                seed, api = s, base
                break
        if not seed or not api:
            raise RuntimeError("videasy: no seed from any API base")

        last_err = None
        for server in SERVERS:
            try:
                url = self._sources_url(api, server, req, seed)
                r = _http.get(url, headers=HEADERS)
                if r.status_code != 200:
                    last_err = f"{server}: HTTP {r.status_code}"
                    continue
                text = r.text
                if "STREAMCRYPTO_SEED_INVALID" in text:
                    last_err = f"{server}: seed invalid"
                    continue
                res = self._decrypt(text, tmdb_id, seed)
                if not res or not res.get("sources"):
                    last_err = f"{server}: no sources in decrypted payload"
                    continue

                links: list[StreamLink] = []
                for src in res["sources"]:
                    u = (src or {}).get("url")
                    if not u:
                        continue
                    links.append(StreamLink(
                        provider=self.id,
                        url=u,
                        quality=src.get("quality"),
                        referer=PLAYER_REF,
                        headers={
                            "User-Agent": CHROME_UA,
                            "Origin": "https://player.videasy.to",
                            "Referer": PLAYER_REF,
                        },
                        note=f"videasy {server}",
                    ))
                if links:
                    return links
                last_err = f"{server}: empty source list"
            except httpx.HTTPError as e:
                last_err = f"{server}: {type(e).__name__}: {e}"
                continue
        raise RuntimeError(f"videasy: all servers failed ({last_err or 'unknown'})")
