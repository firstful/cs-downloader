"""TMDB metadata provider — mirrors the StreamPlay extension's TMDB usage."""
from __future__ import annotations

from typing import Optional

import httpx

from .. import config
from ..models import Episode, LoadResponse, SearchResult, StreamLink
from .base import Provider
from . import _http


class TMDBProvider(Provider):
    id = "tmdb"
    name = "TMDB (metadata)"
    kind = "metadata"
    status = "ok"
    note = "search/load/episodes; same api_key the StreamPlay extension embeds"

    def _get(self, path: str, **params) -> Optional[dict]:
        params = dict(params)
        params.setdefault("api_key", config.TMDB_API_KEY)
        try:
            r = _http.get(f"{config.TMDB_BASE}{path}", params=params)
        except httpx.HTTPError:
            return None
        if r.status_code != 200:
            return None
        try:
            return r.json()
        except Exception:
            return None

    def search(self, query: str, page: int = 1) -> list[SearchResult]:
        data = self._get("/search/multi", query=query, page=page, include_adult="true")
        out: list[SearchResult] = []
        if not data:
            return out
        for res in data.get("results", [])[:30]:
            mtype = res.get("media_type")
            if mtype not in ("movie", "tv"):
                continue
            title = res.get("title") or res.get("name") or ""
            out.append(
                SearchResult(
                    id=res.get("id", 0),
                    title=title,
                    year=_year(res.get("release_date") or res.get("first_air_date")),
                    media_type=mtype,
                    poster=_img(res.get("poster_path")),
                    backdrop=_img(res.get("backdrop_path"), orig=True),
                    overview=res.get("overview"),
                    url=f"tmdb://{res.get('id', 0)}",
                )
            )
        return out

    def load(self, url: str) -> Optional[LoadResponse]:
        # url formats: "tmdb://155" or "tmdb://155/movie" or bare id
        m = url.replace("tmdb://", "")
        parts = m.split("/")
        try:
            tid = int(parts[0])
        except ValueError:
            return None
        mtype = parts[1] if len(parts) > 1 else ""

        # try movie first
        if not mtype or mtype == "movie":
            d = self._get(f"/movie/{tid}", append_to_response="external_ids")
            if d:
                return self._movie_load(d)
        if not mtype or mtype == "tv":
            d = self._get(f"/tv/{tid}", append_to_response="external_ids")
            if d:
                return self._tv_load(d)
        return None

    def _movie_load(self, d: dict) -> LoadResponse:
        return LoadResponse(
            id=d.get("id", 0),
            media_type="movie",
            title=d.get("title") or d.get("name") or "",
            year=_year(d.get("release_date")),
            poster=_img(d.get("poster_path")),
            backdrop=_img(d.get("backdrop_path"), orig=True),
            overview=d.get("overview"),
            imdb_id=(d.get("external_ids") or {}).get("imdb_id"),
            url=f"tmdb://{d.get('id', 0)}/movie",
        )

    def _tv_load(self, d: dict) -> LoadResponse:
        seasons = [s.get("season_number") for s in d.get("seasons", []) if s.get("season_number", 0) > 0]
        eps: list[Episode] = []
        for sn in seasons[:8]:  # cap season fetch to keep load() fast
            sdata = self._get(f"/tv/{d.get('id')}/season/{sn}")
            if not sdata:
                continue
            for e in sdata.get("episodes", []):
                eps.append(
                    Episode(
                        season=sn,
                        episode=e.get("episode_number", 0),
                        title=e.get("name"),
                        air_date=e.get("air_date"),
                        overview=e.get("overview"),
                    )
                )
        return LoadResponse(
            id=d.get("id", 0),
            media_type="tv",
            title=d.get("name") or "",
            year=_year(d.get("first_air_date")),
            poster=_img(d.get("poster_path")),
            backdrop=_img(d.get("backdrop_path"), orig=True),
            overview=d.get("overview"),
            imdb_id=(d.get("external_ids") or {}).get("imdb_id"),
            seasons=seasons,
            episodes=eps,
            url=f"tmdb://{d.get('id', 0)}/tv",
        )

    def resolve(self, req) -> list[StreamLink]:
        raise NotImplementedError("tmdb is metadata-only")


def _img(path, orig=False) -> Optional[str]:
    if not path:
        return None
    base = config.TMDB_IMG_ORIG if orig else config.TMDB_IMG
    return f"{base}{path}"


def _year(date: Optional[str]) -> Optional[int]:
    if not date or len(date) < 4:
        return None
    try:
        return int(date[:4])
    except ValueError:
        return None
