"""Pydantic models — CloudStream-style response shapes."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


# ---- CloudStream-style search / load shapes -------------------------------

class SearchResult(BaseModel):
    """One search hit (CloudStream SearchResponse analog)."""
    id: int
    title: str
    year: Optional[int] = None
    media_type: str = "movie"          # movie | tv
    poster: Optional[str] = None
    backdrop: Optional[str] = None
    overview: Optional[str] = None
    url: str = ""                      # tmdb://<id>

class Episode(BaseModel):
    season: int
    episode: int
    title: Optional[str] = None
    air_date: Optional[str] = None
    overview: Optional[str] = None

class LoadResponse(BaseModel):
    """CloudStream LoadResponse analog."""
    id: int
    media_type: str                    # movie | tv
    title: str
    year: Optional[int] = None
    poster: Optional[str] = None
    backdrop: Optional[str] = None
    overview: Optional[str] = None
    imdb_id: Optional[str] = None
    tvdb_id: Optional[int] = None
    seasons: list[int] = Field(default_factory=list)
    episodes: list[Episode] = Field(default_factory=list)   # tv only
    url: str = ""

# ---- Stream links ----------------------------------------------------------

class StreamLink(BaseModel):
    """CloudStream ExtractorLink analog."""
    provider: str
    url: str
    quality: Optional[str] = None
    headers: dict[str, str] = Field(default_factory=dict)
    referer: Optional[str] = None
    note: Optional[str] = None

class LinksRequest(BaseModel):
    tmdb_id: Optional[int] = None
    imdb_id: Optional[str] = None
    media_type: str = "movie"          # movie | tv
    season: Optional[int] = None
    episode: Optional[int] = None
    providers: Optional[list[str]] = None   # default: all enabled

class LinksResponse(BaseModel):
    links: list[StreamLink] = Field(default_factory=list)
    provider_results: dict[str, Any] = Field(default_factory=dict)

# ---- Downloads / jobs ------------------------------------------------------

class DownloadRequest(BaseModel):
    tmdb_id: Optional[int] = None
    imdb_id: Optional[str] = None
    media_type: str = "movie"
    season: Optional[int] = None
    episode: Optional[int] = None
    title: Optional[str] = None
    provider: str = "2embed"
    quality: Optional[str] = None
    expected_aspect_ratio: Optional[float] = None   # e.g. 2.39 — pins the AR filter (strict)


class WantedRequest(BaseModel):
    title: str = ""
    provider: str = ""               # "" = auto (server picks best across registry)
    source_url: str = ""             # optional — else resolved from tmdb_id
    tmdb_id: Optional[int] = None
    media_type: str = "movie"
    quality: Optional[str] = None
    season: Optional[int] = None     # legacy single-season add
    episode: Optional[int] = None    # legacy single-episode add
    seasons: list[int] = Field(default_factory=list)      # whole seasons → enumerate eps
    episodes: list[dict] = Field(default_factory=list)    # [{"season":1,"episode":3}, ...] partial picks
    kid_friendly: bool = False                            # route to the Kids library
    priority: int = 0                 # higher = earlier in the queue (declarative ordering)
    expected_aspect_ratio: Optional[float] = None   # pins the AR filter (strict)

class Job(BaseModel):
    id: int
    status: str = "queued"             # queued|resolving|downloading|done|error|deleted
    title: str = ""
    provider: str = ""
    progress: float = 0.0              # 0..100
    bytes_downloaded: int = 0
    total_bytes: Optional[int] = None
    filename: Optional[str] = None
    file_size: Optional[int] = None
    error: Optional[str] = None
    filter_warnings: Optional[str] = None   # e.g. "aspect_ratio filter: expected 1.778 got 0.466"
    created_at: str = ""
    updated_at: str = ""
