"""Hindmoviez provider — WordPress download-site source (Hindi + English dual-audio).

Sites: hindmovie.icu (dynamic via extension's domains.json), alternates
hindmoviez.xyz/.bond/.com. Content: Hollywood + Indian movies/TV, dual-audio
Hindi-English and "English With Subtitle" posts. Broad English catalog.

Reverse-engineered 2026-08-02 from dedicated Hindmoviez.cs3 v15 (jadx) + live probes:
  search  GET {base}/?s={q}                       -> WordPress archive <h2><a>
  load    GET {base}/{slug}/                      -> post; quality links are mvlink.blog/<id>
  links   mvlink.blog/<id> page contains:
            AJAX_URL = ...wp-admin/admin-ajax.php  (in inline script)
            <a href=".../?id=<filename>">Get Links</a>
          POST AJAX_URL  action=hindshare_sign&d=b64url(filename)
            -> {"success":true,"data":{"url":"https://hshare.ink/r.php?d=..&t=..&s=.."}}
          GET signed url (follow) -> hshare.ink/f.php Download File page
          f.php has 3 option anchors:
            GDirect  https://gdirect.online/redirect.php?id=<obf>   (skip - obfuscated)
            GDShine  https://gdshine.org/s/<id>                      (skip v1 - needs its extractor)
            HPage    https://hcloud.ink/redirect.php?url=<b64>       (VERIFIED)
          HPage: b64(url) -> https://hcloud.ink/direct/index.php?url=<b64 of workers.dev file>
          decode -> direct file URL (Cloudflare Workers, real MKV bytes; HEAD 403 but GET works)
"""
from __future__ import annotations

import base64
import re
import urllib.parse

import httpx

from ..models import LoadResponse, SearchResult, StreamLink
from . import _http
from .base import Provider

YANDEX_API = "https://cloud-api.yandex.net/v1/disk/public/resources"
CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")
MVLINK_RE = re.compile(r"https?://mvlink\.blog/\d+", re.I)


def _b64url(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def _b64dec(s: str) -> str:
    try:
        return base64.b64decode(s + "===").decode(errors="replace")
    except Exception:
        return ""


class HindmoviezProvider(Provider):
    id = "hindmoviez"
    name = "Hindmoviez"
    kind = "source"
    status = "ok"
    note = ("WordPress download site (Hindi+English dual-audio). Real flow reverse-engineered "
            "from Hindmoviez.cs3 v15: mvlink.blog -> hindshare_sign AJAX -> hshare f.php -> "
            "hcloud b64 chain -> direct workers.dev file (verified 2026-08-02)")

    BASE = "https://hindmovie.icu"

    # ---- search / load (CloudStream MainAPI shape) -------------------------

    def search(self, query: str, page: int = 1) -> list[SearchResult]:
        try:
            r = _http.get(f"{self.BASE}/", params={"s": query},
                          headers={"Accept-Language": "en"})
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise RuntimeError(f"hindmoviez search failed: {e}") from e
        out: list[SearchResult] = []
        for m in re.finditer(
            r"<h2[^>]*>.*?<a[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>",
            r.text, re.S,
        ):
            url, title = m.group(1), re.sub(r"<[^>]+>", "", m.group(2)).strip()
            if not url or not title or "hindmovie" not in urllib.parse.urlparse(url).netloc:
                continue
            ym = re.search(r"\b(19\d\d|20\d\d)\b", title)
            out.append(SearchResult(
                id=0, title=title, year=int(ym.group(1)) if ym else None,
                media_type="movie", url=url, overview="", poster=None,
            ))
        return out[:30]

    def load(self, url: str) -> LoadResponse | None:
        try:
            r = _http.get(url, headers={"Accept-Language": "en"})
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise RuntimeError(f"hindmoviez load failed: {e}") from e
        title = _og(r.text, "og:title") or _title(r.text)
        ym = re.search(r"\b(19\d\d|20\d\d)\b", title or "")
        return LoadResponse(
            id=0, media_type="movie", title=title or url,
            year=int(ym.group(1)) if ym else None,
            poster=_og(r.text, "og:image"), overview="", url=url,
        )

    # ---- resolve (the real chain) ------------------------------------------

    def resolve(self, req) -> list[StreamLink]:
        post_url = req.imdb_id if req.imdb_id and req.imdb_id.startswith("http") else None
        if not post_url:
            raise RuntimeError("hindmoviez resolve expects the post URL as imdb_id "
                               "(from /api/search -> result.url)")
        try:
            r = _http.get(post_url, headers={"Accept-Language": "en"})
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise RuntimeError(f"hindmoviez post fetch failed: {e}") from e

        mvlinks = []
        for raw in re.findall(r'href="([^"]+)"', r.text):
            u = raw.strip()
            if MVLINK_RE.search(u) and u not in mvlinks:
                mvlinks.append(u)
        if not mvlinks:
            # some posts carry yandex/direct links directly
            return self._classify_links(r.text, post_url)

        links: list[StreamLink] = []
        for mv in mvlinks[:8]:  # cap per-quality links
            try:
                links.extend(self._resolve_mvlink(mv, post_url))
            except Exception:
                continue
        # prefer links that are actually downloadable (workers.dev direct / real media)
        links.sort(key=lambda l: (
            0 if ("workers.dev" in l.url or re.search(r"\.(mp4|mkv|m3u8|webm)(\?|$)", l.url, re.I))
            else (1 if "disk.yandex" in l.url else 2),
            0 if "workers.dev" in l.url else 1,
        ))
        return links

    def _resolve_mvlink(self, mv: str, post_url: str) -> list[StreamLink]:
        r = _http.get(mv, referer=post_url)
        r.raise_for_status()
        html = r.text

        m = re.search(r"AJAX_URL\s*=\s*['\"]([^'\"]+)['\"]", html, re.I)
        if not m:
            return []
        ajax = m.group(1)
        # quality/name hint from the mvlink page title
        qual = "?"
        tm = re.search(r"<title>(.*?)</title>", html, re.S)
        if tm:
            t = tm.group(1).strip()
            qm = re.search(r"(\d{3,4}p|4K|1080p|720p|480p)", t)
            qual = qm.group(1) if qm else t[:30]

        # the "Get Links" href carries ?id=<filename>
        get_href = None
        bm = re.search(r'<a[^>]+href="([^"]+)"[^>]*>\s*Get Links', html, re.I)
        if bm:
            get_href = bm.group(1)
        else:
            for raw in re.findall(r'href="([^"]*id=[^"]+)"', html):
                get_href = raw
                break
        if not get_href or "id=" not in get_href:
            return []

        raw_id = urllib.parse.unquote(get_href.split("id=")[1].replace("+", "%2B"))
        # sign
        sign = _http.post(
            ajax,
            data={"action": "hindshare_sign", "d": _b64url(raw_id)},
            headers={"user-agent": CHROME_UA, "Content-Type": "application/x-www-form-urlencoded"},
            referer=mv,
        )
        sign.raise_for_status()
        signed = sign.json().get("data", {}).get("url")
        if not signed:
            return []

        # follow the signed URL -> f.php Download File page
        f = _http.get(signed, referer=mv)
        f.raise_for_status()
        return self._classify_links(f.text, signed, qual=qual)

    def _classify_links(self, html: str, referer: str, qual: str = "?") -> list[StreamLink]:
        links: list[StreamLink] = []
        seen = set()
        for raw in re.findall(r'href="(https?://[^"]+)"', html):
            u = raw.strip()
            if u in seen:
                continue
            seen.add(u)
            if "hcloud.ink/redirect.php" in u:
                direct = self._hcloud_direct(u)
                if direct:
                    links.append(StreamLink(
                        provider=self.id, url=direct, quality=qual,
                        referer=referer, headers={"User-Agent": CHROME_UA},
                        note="hcloud b64 chain -> workers.dev direct",
                    ))
            elif "gdshine.org/s/" in u:
                links.append(StreamLink(
                    provider=self.id, url=u, quality=qual, referer=referer,
                    note="gdshine share page (extractor v2)",
                ))
            elif "gdirect.online" in u:
                links.append(StreamLink(
                    provider=self.id, url=u, quality=qual, referer=referer,
                    note="gdirect obfuscated (extractor v2)",
                ))
            elif "disk.yandex.com" in u or "yadi.sk" in u:
                direct = _yandex_resolve(u)
                if direct:
                    links.append(StreamLink(
                        provider=self.id, url=direct, quality=qual,
                        referer="https://disk.yandex.com/",
                        headers={"User-Agent": _http.config.USER_AGENT},
                        note="resolved via Yandex public API",
                    ))
            elif re.search(r"\.(mp4|mkv|m3u8|webm)(\?|$)", u, re.I):
                links.append(StreamLink(provider=self.id, url=u, quality=qual,
                                        referer=referer))
        return links

    def _hcloud_direct(self, redirect_url: str) -> str | None:
        m = re.search(r"url=([A-Za-z0-9+/=]+)", redirect_url)
        if not m:
            return None
        lvl1 = _b64dec(m.group(1))
        m2 = re.search(r"url=([A-Za-z0-9+/=]+)", lvl1)
        if not m2:
            return None
        return _b64dec(m2.group(1))


def _yandex_resolve(public_url: str) -> str | None:
    try:
        r = _http.get(YANDEX_API, params={"public_key": public_url})
        r.raise_for_status()
        d = r.json()
    except Exception:
        return None
    if d.get("file"):
        return d["file"]
    if d.get("type") == "dir":
        items = (d.get("_embedded") or {}).get("items") or []
        files = [i for i in items if i.get("file")]
        if files:
            return files[0]["file"]
    return None


def _og(html: str, prop: str) -> str | None:
    m = re.search(rf'<meta[^>]+property=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']+)', html)
    if m:
        return m.group(1)
    m = re.search(rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']{re.escape(prop)}["\']', html)
    return m.group(1) if m else None


def _title(html: str) -> str | None:
    m = re.search(r"<title>(.*?)</title>", html, re.S)
    return m.group(1).strip() if m else None
