"""Adapter onto the CFTC data layer that already exists in this repo.

`apify-cot-analytics/src/` holds a tested, dependency-free CFTC client,
market catalog and analytics engine (59 tests green, verified against live CFTC
data 2026-08-17). CLAUDE.md says consolidate rather than accrete, so this API
IMPORTS that layer instead of shipping a second copy that would drift.

VENDORED, NOT SYMLINKED (20.08.2026). This package ships as its own public
GitHub repo (x402-gold-api), separate from the private jarvis-rhod monorepo
where the Apify actor also lives (vault-privacy: only this folder is public).
That means the old `../../apify-cot-analytics/src` sibling-of-the-parent path
does not exist once this repo is cloned standalone on Render/Fly -- there is
no parent income/ directory out there. So a copy of apify-cot-analytics/src
is vendored into THIS repo at ./apify-cot-analytics/src (one level shallower
than before) and the path below matches that. If the upstream actor's src/
changes, re-copy it here -- see README for the sync note.

Loading it needs one trick: the actor's package is literally named `src`, which
would collide with anything else called `src` on the path, and its modules use
relative imports so they must be loaded as a real package. So it is registered
under the unambiguous alias `cot_core` via importlib, and `cot_core.pipeline`
et al. resolve normally from there.

Added on top of the actor's layer, because an HTTP API has needs a batch job
does not:
  * a TTL cache, so paid calls do not each hit a government API
  * per-market error capture, so one dead contract cannot fail a whole sweep
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ACTOR_SRC = (
    Path(__file__).resolve().parent.parent / "apify-cot-analytics" / "src"
)
ALIAS = "cot_core"


class CotSourceUnavailable(RuntimeError):
    """The shared CFTC layer could not be loaded."""


def _load_cot_core():
    """Import the vendored apify-cot-analytics/src as the package `cot_core`."""
    if ALIAS in sys.modules:
        return sys.modules[ALIAS]
    init = ACTOR_SRC / "__init__.py"
    if not init.exists():
        raise CotSourceUnavailable(
            f"Expected the vendored CFTC layer at {ACTOR_SRC}, but {init} is "
            "missing. This API vendors a copy of the Apify actor's data layer "
            "into ./apify-cot-analytics/src -- re-copy it if it's gone missing."
        )
    spec = importlib.util.spec_from_file_location(
        ALIAS, init, submodule_search_locations=[str(ACTOR_SRC)]
    )
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise CotSourceUnavailable(f"Could not build an import spec for {init}.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[ALIAS] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        del sys.modules[ALIAS]
        raise
    return module


_load_cot_core()

analytics = importlib.import_module(f"{ALIAS}.analytics")
cftc = importlib.import_module(f"{ALIAS}.cftc")
markets = importlib.import_module(f"{ALIAS}.markets")
pipeline = importlib.import_module(f"{ALIAS}.pipeline")

Thresholds = analytics.Thresholds
UnknownMarketError = markets.UnknownMarketError
CftcError = cftc.CftcError
PRESETS = markets.PRESETS


@dataclass
class CachedCftcClient(cftc.CftcClient):
    """CftcClient with a process-local TTL cache.

    The CFTC publishes COT once a week (Friday 15:30 ET), so cached rows cannot
    go stale in any way that affects an answer within the TTL. The cache exists
    to be a good citizen toward a free government API: with a 3600s TTL, one
    market costs at most 24 upstream requests a day no matter how many paid
    calls arrive. It also keeps the paid-call latency low enough that an agent
    does not time out mid-payment.
    """

    ttl_seconds: int = 3600
    _cache: dict[tuple, tuple[float, list[dict]]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def fetch_history(
        self,
        contract_code: str,
        report: str = "legacy",
        combined: bool = False,
        limit: int = 200,
    ) -> list[dict]:
        key = (contract_code, report, bool(combined), int(limit))
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and now - hit[0] < self.ttl_seconds:
                return hit[1]
        rows = super().fetch_history(
            contract_code, report=report, combined=combined, limit=limit
        )
        with self._lock:
            self._cache[key] = (now, rows)
        return rows

    def cache_stats(self) -> dict:
        with self._lock:
            return {"entries": len(self._cache), "ttlSeconds": self.ttl_seconds}


@dataclass
class CotSource:
    """What the HTTP layer talks to. One instance per app."""

    client: Any = None
    ttl_seconds: int = 3600

    def __post_init__(self) -> None:
        self.client = self.client or CachedCftcClient(ttl_seconds=self.ttl_seconds)

    # --- catalog (free routes) ---

    @staticmethod
    def catalog() -> list[dict]:
        return markets.catalog()

    @staticmethod
    def presets() -> dict[str, list[str]]:
        return {name: list(members) for name, members in PRESETS.items()}

    @staticmethod
    def resolve(symbols: list[str] | None):
        """Symbols/presets -> Market list. Raises UnknownMarketError on nonsense.

        Called BEFORE pricing so the quote reflects the true market count, and
        so a typo produces a 400 instead of a 402 for an unanswerable request.
        """
        return markets.expand(symbols)

    # --- paid work ---

    def analyze(
        self,
        symbols: list[str] | None,
        *,
        weeks: int = 1,
        lookback_weeks: int = 156,
        report: str = "auto",
        combined: bool = False,
        thresholds: Any = None,
    ) -> tuple[list[list[dict]], list[dict]]:
        """Run the shared pipeline. Returns (per-market record lists, errors)."""
        cfg = pipeline.RunConfig(
            markets=self.resolve(symbols),
            report=report,
            combined=combined,
            weeks=weeks,
            lookback_weeks=lookback_weeks,
            thresholds=thresholds or Thresholds(),
        )
        errors: list[dict] = []

        def on_error(market, exc: Exception) -> None:
            errors.append({"market": market.label, "code": market.code, "error": str(exc)})

        batches = list(pipeline.run(cfg, client=self.client, on_error=on_error))

        # The shared pipeline is a batch job: it logs a dead market and carries
        # on, which is right for an Actor run but wrong for a paid HTTP call.
        # If EVERY requested market failed there is nothing to sell, so raise
        # and let the caller return 502 without settling. Partial failures still
        # return data plus an explicit `errors` list -- the caller paid for a
        # set and got most of it, and can see exactly what is missing.
        if not batches and errors:
            raise CftcError(
                "No requested market could be retrieved from the CFTC API: "
                + "; ".join(f"{e['market']}: {e['error']}" for e in errors)
            )
        return batches, errors

    def summarize(self, records: list[dict]) -> dict:
        return analytics.summarize(records)
