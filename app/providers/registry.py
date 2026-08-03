"""Provider registry — mirrors StreamPlay's ProvidersList (the 57-provider list).

Only providers with `status: ok` are wired and verified. Experimental entries
keep the exact endpoints from the decompiled extension for when backends are
re-verified (they drift — e.g. VidFast's /api/auth/challenge 404s on current builds).
"""
from __future__ import annotations

from .base import Provider
from .embed2 import TwoEmbedProvider
from .hindmoviez import HindmoviezProvider
from .tmdb import TMDBProvider
from .videasy import VideasyProvider

_registry: dict[str, Provider] = {}


class _Experimental(Provider):
    """Placeholder slot for a provider whose backend needs re-verification."""
    kind = "source"
    status = "experimental"

    def __init__(self, pid: str, name: str, note: str):
        self.id = pid
        self.name = name
        self.note = note

    def resolve(self, req) -> list:
        raise RuntimeError(f"{self.id}: backend not verified — see registry note")


def _register(p: Provider) -> None:
    _registry[p.id] = p


def init_registry() -> None:
    _register(TMDBProvider())
    _register(TwoEmbedProvider())
    _register(HindmoviezProvider())
    _register(VideasyProvider())
    # Experimental slots (endpoints from StreamPlay.cs3 v658, decompiled):
    _register(_Experimental("xpass", "Xpass", "https://play.xpass.top/e/{movie,tv}/{tmdbId}; video API behind obfuscated mainmini.js"))
    _register(_Experimental("peachify", "Peachify", "servers https://usa.eat-peach.sbs/{holly,multi,ice,air,net} + uwu.peachify.top/moviebox"))
    _register(_Experimental("vidfast", "VidFast", "https://vidfast.vc/api/auth/challenge|session|providers + /links?api_key=... (routes drifted)"))
    _register(_Experimental("vaplayer", "VaPlayer", "https://streamdata.vaplayer.ru — needs route discovery"))


def all_providers() -> list[Provider]:
    return list(_registry.values())


def source_providers() -> list[Provider]:
    return [p for p in _registry.values() if p.kind == "source"]


def metadata_provider() -> Provider:
    return _registry["tmdb"]


def get(pid: str) -> Provider | None:
    return _registry.get(pid)
