"""Post-download validation filters.

Each filter validates a finished file against the job's metadata and returns a
FilterResult. Filters run in the worker after download+remux and before the
file is moved into the flat downloads dir. A failing filter rejects the file:
the worker discards it and tries the next link; if every link fails the job
goes to error with the filter's message.

New filters: write a module under app/filters/, define a function
    fn(path: str, job: dict) -> FilterResult
and decorate it with @register("<id>"). Add the id to config.FILTERS
(default: "aspect_ratio") to enable it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from .. import config


@dataclass
class FilterResult:
    filter_id: str
    passed: bool
    detail: str = ""
    metrics: dict = field(default_factory=dict)
    # enforcement when passed=False: "soft" keeps the file with a warning,
    # "hard" discards it and the worker tries the next link. The filter sets
    # this from its own config (aspect_ratio → AR_ACTION, duration → DUR_ACTION)
    # so each gate chooses how strict it must be.
    action: str = "soft"

    def to_error(self) -> str:
        return f"{self.filter_id} filter: {self.detail}"


FilterFn = Callable[[str, dict], FilterResult]

_REGISTRY: dict[str, FilterFn] = {}


def register(filter_id: str) -> Callable[[FilterFn], FilterFn]:
    def deco(fn: FilterFn) -> FilterFn:
        _REGISTRY[filter_id] = fn
        return fn

    return deco


def available() -> list[str]:
    return sorted(_REGISTRY)


def run(path: str, job: dict, enabled: Optional[list[str]] = None) -> list[FilterResult]:
    """Run enabled filters against a finished file. Inconclusive results
    (detector could not measure anything) pass through with a note — never
    reject a file we could not actually evaluate."""
    ids = enabled if enabled is not None else sorted(config.FILTERS)
    out: list[FilterResult] = []
    for fid in ids:
        fn = _REGISTRY.get(fid)
        if fn is None:
            continue
        try:
            out.append(fn(path, job))
        except Exception as e:  # noqa: BLE001 — a filter bug must not sink the job
            out.append(
                FilterResult(
                    filter_id=fid,
                    passed=True,
                    detail=f"filter error (treated as pass): {type(e).__name__}: {e}",
                )
            )
    return out


# Import filter modules so their @register decorators run.
from . import aspect_ratio  # noqa: E402,F401
from . import duration  # noqa: E402,F401
from . import continuity  # noqa: E402,F401
