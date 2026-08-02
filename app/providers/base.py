"""Provider base class — CloudStream MainAPI-style contract."""
from __future__ import annotations

from typing import Optional

from .. import config
from ..models import LoadResponse, SearchResult, StreamLink


class ProviderError(Exception):
    pass


class Provider:
    """One StreamPlay-style source.

    The three methods mirror the CloudStream MainAPI contract:
    search() -> list[SearchResult], load() -> LoadResponse, resolve() -> list[StreamLink].
    Metadata (tmdb) is a special provider that only implements search/load.
    """

    id: str = "base"
    name: str = "Base"
    kind: str = "source"          # source | metadata
    status: str = "ok"            # ok | experimental | broken
    note: str = ""

    def search(self, query: str, page: int = 1) -> list[SearchResult]:
        raise NotImplementedError

    def load(self, url: str) -> Optional[LoadResponse]:
        raise NotImplementedError

    def resolve(self, req) -> list[StreamLink]:
        """Resolve stream links for a LinksRequest."""
        raise NotImplementedError

    @property
    def enabled(self) -> bool:
        return self.id not in config.DISABLED_PROVIDERS

    def describe(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "status": self.status,
            "note": self.note,
            "enabled": self.enabled,
        }
