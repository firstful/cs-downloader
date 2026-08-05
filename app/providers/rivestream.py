"""RiveStream provider — reverse-engineered from StreamPlay.cs3 v658 (decompiled 2026-08-05).

Flow (verified live 2026-08-05):
  1. GET https://www.rivestream.app/api/backendfetch?requestID=VideoProviderServices&secretKey=rive
       -> {"data": ["primevids", "flowcast", "asiacloud", "hindicast", "guru", "ophim"]}
  2. GET https://www.rivestream.app/  -> find <script src*="_app"> -> GET the Next.js _app chunk
     -> regex `let c=["...","..."]` (first array with >2 strings) = keyList (rotates)
  3. GET https://rivestream.supe2372.workers.dev/?input={tmdbId}&cList={keyList joined by ,}
       -> per-title secretKey (CF worker derives it from id + key list)  [NO cache across ids]
  4. Per backend (service), GET:
       movie: .../api/backendfetch?requestID=movieVideoProvider&id={tmdb}&service={svc}&secretKey={key}
       tv:    .../api/backendfetch?requestID=tvVideoProvider&id={tmdb}&season={s}&episode={e}&service={svc}&secretKey={key}
     -> {"data": {"sources": [{"quality", "url", "source", "format"}]}}
  5. Per source:
       url contains "proxy?url=" -> URL-decode; stream = decode(after "proxy?url=", before "&headers=");
                                    headers = JSON(decode(after "&headers=")) e.g. {Referer, Origin, User-Agent}
       else -> plain url, no special headers
  Display quality: "RiveStream <source> [<quality>]".

Verified live: Interstellar (movie, primevids plain-url m3u8) + Oppenheimer (movie, flowcast proxy-url).
Coverage caveat (2026-08-05): Bad Monkey (130853) returns data:null on ALL backends for every ep —
Apple TV+ exclusives may be absent from the free-embed pool entirely. Probe before promising.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse

import httpx

from ..models import LoadResponse, SearchResult, StreamLink
from . import _http
from .base import Provider

API = "https://www.rivestream.app"
KEY_WORKER = "https://rivestream.supe2372.workers.dev"
BACKENDS = ["primevids", "flowcast", "asiacloud", "hindicast", "guru", "ophim"]
CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": CHROME_UA}

_CACHE_TTL = 3600.0
_keylist_cache: tuple[float, list[str]] | None = None   # (fetched_at, keys)
_secret_cache: dict[int, tuple[float, str]] = {}        # tmdb_id -> (fetched_at, key)


class RiveStreamProvider(Provider):
    id = "rivestream"
    name = "RiveStream"
    kind = "source"
    status = "experimental"
    note = ("multi-backend aggregator (primevids/flowcast/asiacloud/hindicast/guru/ophim). "
            "Resolves correctly (Next.js key-list -> CF-worker per-title secret -> backendfetch "
            "movie/tvVideoProvider; tries BOTH query-param orders — guru serves some eps only "
            "with id,service,secretKey,season,episode). NOT in auto-resolve pool 2026-08-05: "
            "every source URL 403/404s from this host (1shows.app Cloudflare bot, flowcast "
            "web-cache ACCESS DENIED + proxy unauthorized, guru cf-master tokens expired "
            "Dec-2025 + hls3 paths 404, ophim 404). Resolver-only until a CDN path opens.")

    def search(self, query: str, page: int = 1) -> list[SearchResult]:
        # RiveStream has no own search; metadata comes from TMDB provider.
        return []

    def load(self, url: str) -> LoadResponse | None:
        return None

    # ---- key material --------------------------------------------------------

    def _keylist(self) -> list[str]:
        """Rotating key list from the rivestream.app Next.js _app chunk (cached 1h)."""
        global _keylist_cache
        now = time.time()
        if _keylist_cache and now - _keylist_cache[0] < _CACHE_TTL:
            return _keylist_cache[1]

        page = _http.get(f"{API}/", headers=HEADERS)
        page.raise_for_status()
        m = re.search(r'src="([^"]*_app[^"]*)"', page.text)
        if not m:
            raise RuntimeError("rivestream: no _app script on homepage")
        js = _http.get(API + m.group(1), headers=HEADERS)
        js.raise_for_status()

        keys = None
        for m2 in re.finditer(r"let\s+c\s*=\s*(\[[^]]*\])", js.text):
            cand = re.findall(r'"([^"]+)"', m2.group(1))
            if len(cand) > 2:
                keys = cand
                break
        if not keys:
            raise RuntimeError("rivestream: key list not found in _app chunk")
        _keylist_cache = (now, keys)
        return keys

    def _secret(self, tmdb_id: int) -> str:
        """Per-title secret from the CF worker (cached 1h per tmdb id)."""
        now = time.time()
        ent = _secret_cache.get(tmdb_id)
        if ent and now - ent[0] < _CACHE_TTL:
            return ent[1]
        keys = self._keylist()
        r = _http.get(KEY_WORKER, params={"input": tmdb_id, "cList": ",".join(keys)}, headers=HEADERS)
        r.raise_for_status()
        key = r.text.strip()
        if not key:
            raise RuntimeError("rivestream: empty secret key")
        _secret_cache[tmdb_id] = (now, key)
        return key

    def _backends(self) -> list[str]:
        """Live backend list; fall back to the known set if the fetch fails."""
        try:
            r = _http.get(f"{API}/api/backendfetch",
                          params={"requestID": "VideoProviderServices", "secretKey": "rive"},
                          headers=HEADERS)
            r.raise_for_status()
            data = (r.json() or {}).get("data")
            if isinstance(data, list) and data:
                return [str(x) for x in data]
        except Exception:
            pass
        return list(BACKENDS)

    # ---- resolve -------------------------------------------------------------

    def _fetch_sources(self, tmdb_id: int, media_type: str, season, episode,
                       service: str, secret: str) -> list[dict]:
        """Query one backend. Tries BOTH query-param orders.

        Empirically (2026-08-05, Bad Monkey guru backend) the response differs by
        query-param ORDER, not just values: `id,season,episode,service,secretKey`
        (the extension's order) serves E01, while `id,service,secretKey,season,
        episode` serves E02/E07/E08/E09. Order A is the extension's exact order;
        order B is the variant that cracked guru. Each costs one GET.
        """
        rid = "tvVideoProvider" if media_type == "tv" else "movieVideoProvider"
        variants: list[dict] = []

        # A — extension order: id, [season, episode], service, secretKey
        a: dict = {"requestID": rid, "id": tmdb_id}
        if media_type == "tv":
            if season is not None:
                a["season"] = season
            if episode is not None:
                a["episode"] = episode
        a["service"] = service
        a["secretKey"] = secret
        variants.append(a)

        # B — id, service, secretKey, [season, episode]  (guru needs this)
        b: dict = {"requestID": rid, "id": tmdb_id, "service": service, "secretKey": secret}
        if media_type == "tv":
            if season is not None:
                b["season"] = season
            if episode is not None:
                b["episode"] = episode
        variants.append(b)

        seen: set[str] = set()
        out: list[dict] = []
        for q in variants:
            try:
                r = _http.get(f"{API}/api/backendfetch", params=q, headers=HEADERS)
                if r.status_code != 200:
                    continue
                data = (r.json() or {}).get("data") or {}
                for src in data.get("sources") or []:
                    u = (src or {}).get("url") or ""
                    if u and u not in seen:
                        seen.add(u)
                        out.append(src)
            except Exception:
                continue
        return out

    def resolve(self, req) -> list[StreamLink]:
        tmdb_id = req.tmdb_id
        if not tmdb_id:
            raise RuntimeError("rivestream resolve needs tmdb_id (metadata from /api/load)")

        secret = self._secret(tmdb_id)
        links: list[StreamLink] = []
        for svc in self._backends():
            try:
                for src in self._fetch_sources(tmdb_id, req.media_type, req.season, req.episode, svc, secret):
                    u = (src or {}).get("url")
                    if not u:
                        continue
                    if "proxy?url=" in u:
                        try:
                            decoded = urllib.parse.unquote(u)
                            stream = urllib.parse.unquote(
                                decoded.split("proxy?url=", 1)[1].split("&headers=", 1)[0]
                            )
                            hdr_raw = decoded.split("&headers=", 1)[1] if "&headers=" in decoded else ""
                            headers = json.loads(urllib.parse.unquote(hdr_raw)) if hdr_raw else {}
                            if not isinstance(headers, dict):
                                headers = {}
                        except Exception:
                            continue
                    else:
                        stream = u
                        headers = {}

                    q = src.get("quality")
                    display = f"RiveStream {src.get('source') or svc}"
                    if q:
                        display = f"{display} [{q}]"
                    links.append(StreamLink(
                        provider=self.id,
                        url=stream,
                        quality=str(q) if q is not None else None,
                        referer=headers.get("Referer"),
                        headers={"User-Agent": CHROME_UA, **headers},
                        note=f"rivestream {svc}",
                    ))
            except httpx.HTTPError:
                continue
        if not links:
            raise RuntimeError("rivestream: no sources from any backend")
        return links
