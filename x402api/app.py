"""The HTTP surface.

Paid routes follow one shape, implemented once in `_serve_paid`:

    resolve markets -> quote -> authorize -> DO THE WORK -> settle -> respond

Path operations are declared `def`, not `async def`, on purpose: the CFTC client
underneath is synchronous, and Starlette runs sync handlers in a worker thread.
Declaring them async would block the event loop on every upstream call and make
one slow request stall every other payment in flight.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from . import __version__, manifest as manifest_mod
from .config import Settings, load_settings
from .cot_source import CftcError, CotSource, Thresholds, UnknownMarketError
from .crossmarket import compare as cross_compare
from .crossmarket import screen_extremes
from .payments.facilitator import Facilitator, build_facilitator
from .payments.gate import Challenge, PaymentGate
from .pricing import (
    MICRO_PER_USD,
    RoutePrice,
    fee_share_pct,
    format_usd,
    load_prices,
)
from .taxlog import TaxLog

log = logging.getLogger(__name__)

# Input bounds, mirrored from the shared pipeline so the API cannot ask the
# analytics layer for something it will reject.
MIN_LOOKBACK_WEEKS = 8
MAX_LOOKBACK_WEEKS = 1040
DEFAULT_LOOKBACK_WEEKS = 156  # 3 years of weekly reports, the COT Index convention
MAX_HISTORY_WEEKS = 520
DEFAULT_HISTORY_WEEKS = 52


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(value)))


def _split_symbols(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    parts = [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
    return parts or None


def create_app(
    *,
    settings: Settings | None = None,
    facilitator: Facilitator | None = None,
    tax_log: TaxLog | None = None,
    source: CotSource | None = None,
    prices: dict[str, RoutePrice] | None = None,
) -> FastAPI:
    """Build the app. Every dependency is injectable so tests need no network."""
    settings = settings or load_settings()
    prices = prices or load_prices()
    manifest = manifest_mod.load_manifest()
    manifest_mod.validate(manifest, prices)

    facilitator = facilitator or build_facilitator(settings)
    tax_log = tax_log or TaxLog()
    source = source or CotSource(ttl_seconds=settings.upstream_cache_ttl_seconds)
    gate = PaymentGate(
        settings=settings, facilitator=facilitator, tax_log=tax_log, prices=prices
    )

    app = FastAPI(
        title=manifest["service"]["name"],
        version=__version__,
        description=manifest["service"]["summary"],
        docs_url="/docs",
    )
    app.state.settings = settings
    app.state.gate = gate
    app.state.source = source
    app.state.tax_log = tax_log
    app.state.prices = prices
    app.state.manifest = manifest

    # --- shared paid-route machinery ---

    def _requirements(request: Request, route: str, market_count: int):
        return gate.requirements_for(
            route=route,
            market_count=market_count,
            resource_url=f"{settings.base_url}{request.url.path}",
            bazaar=manifest_mod.bazaar_extension(manifest, route),
            output_schema=manifest_mod.output_schema(manifest, route),
        )

    def _challenge_response(challenge: Challenge) -> JSONResponse:
        return JSONResponse(
            status_code=challenge.status_code,
            content=challenge.body,
            headers=challenge.headers,
        )

    def _serve_paid(
        request: Request,
        *,
        route: str,
        symbols: list[str] | None,
        produce: Callable[[list], dict],
    ) -> JSONResponse:
        # 1. Resolve first: the price depends on how many markets were asked for,
        #    and a typo should be a 400 rather than a 402 for an impossible call.
        try:
            resolved = source.resolve(symbols)
        except UnknownMarketError as exc:
            return JSONResponse(
                status_code=400,
                content={"error": "unknown_market", "detail": str(exc)},
            )

        requirements = _requirements(request, route, len(resolved))

        # 2. Challenge or verify.
        outcome = gate.authorize(dict(request.headers), requirements)
        if isinstance(outcome, Challenge):
            return _challenge_response(outcome)

        # 3. The expensive part, BEFORE any money moves. If the CFTC is down the
        #    caller is not charged -- they get a 502 and no settlement happens.
        try:
            payload = produce(resolved)
        except CftcError as exc:
            log.error("Upstream CFTC failure on %s: %s", route, exc)
            return JSONResponse(
                status_code=502,
                content={
                    "error": "upstream_unavailable",
                    "detail": str(exc),
                    "charged": False,
                },
            )
        except UnknownMarketError as exc:  # pragma: no cover - resolve() ran already
            return JSONResponse(
                status_code=400,
                content={"error": "unknown_market", "detail": str(exc)},
            )

        # 4. Settle, then journal. A failure here withholds the data rather than
        #    serving it unpaid.
        settled = gate.settle(outcome, route=route)
        if isinstance(settled, Challenge):
            return _challenge_response(settled)

        payload["payment"] = {
            "amountAtomic": str(requirements.amount_atomic),
            "amountUsd": format_usd(requirements.amount_atomic),
            "asset": "USDC",
            "network": settled.result.network or settings.network.key,
            "transaction": settled.result.transaction,
            "x402Version": outcome.payload.version,
        }
        return JSONResponse(status_code=200, content=payload, headers=settled.headers)

    # --- free routes ---

    @app.get("/")
    def index() -> dict:
        return {
            "service": manifest["service"]["name"],
            "version": __version__,
            "summary": manifest["service"]["summary"],
            "payment": {
                "protocol": "x402",
                "versionsSupported": [1, 2],
                "asset": "USDC",
                "network": settings.network.key,
                "howItWorks": (
                    "Call a paid route without payment to receive an HTTP 402 "
                    "carrying the exact price and payment requirements, sign it, "
                    "and repeat the call with the signature in the "
                    "PAYMENT-SIGNATURE header (x402 v2) or X-PAYMENT (v1)."
                ),
            },
            "manifest": "/.well-known/x402",
            "freeRoutes": [r["path"] for r in manifest.get("freeRoutes", [])],
            "paidRoutes": [
                {
                    "path": r["path"],
                    "title": r["title"],
                    "priceUsd": format_usd(prices[r["id"]].base_micro_usd),
                }
                for r in manifest["routes"]
            ],
        }

    @app.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "version": __version__,
            "network": settings.network.key,
            "networkCaip2": settings.network.caip2,
            "isTestnet": settings.network.is_testnet,
            "facilitator": settings.facilitator_url,
            "payToConfigured": bool(settings.pay_to),
            # Surfaces margin erosion immediately if prices are ever overridden
            # downward, instead of on a statement three weeks later.
            "pricing": {
                name: {
                    "baseUsd": format_usd(price.base_micro_usd),
                    "perExtraMarketUsd": format_usd(price.per_extra_market_micro_usd),
                    "facilitatorFeeSharePct": fee_share_pct(price.base_micro_usd),
                }
                for name, price in sorted(prices.items())
            },
            "settlements": tax_log.summary(),
        }

    @app.get("/v1/markets")
    def markets_catalog() -> dict:
        return {
            "markets": source.catalog(),
            "presets": source.presets(),
            "note": (
                "Any raw 6-character CFTC contract code is also accepted, so the "
                "catalog is a convenience, not a limit."
            ),
        }

    @app.get("/.well-known/x402")
    def well_known() -> dict:
        return manifest_mod.document(
            manifest, prices, base_url=settings.base_url, network=settings.network
        )

    # --- paid routes ---

    @app.get("/v1/cot/snapshot")
    def snapshot(
        request: Request,
        market: str = Query(..., description="Symbol, alias or CFTC contract code"),
        report: str = Query("auto"),
        lookbackWeeks: int = Query(DEFAULT_LOOKBACK_WEEKS),
    ) -> JSONResponse:
        lookback = _clamp(lookbackWeeks, MIN_LOOKBACK_WEEKS, MAX_LOOKBACK_WEEKS)

        def produce(resolved: list) -> dict:
            batches, errors = source.analyze(
                [market], weeks=1, lookback_weeks=lookback, report=report
            )
            records = [r for batch in batches for r in batch]
            return {
                "market": market.upper(),
                "lookbackWeeks": lookback,
                "record": records[0] if records else None,
                "errors": errors,
            }

        return _serve_paid(request, route="snapshot", symbols=[market], produce=produce)

    @app.get("/v1/cot/history")
    def history(
        request: Request,
        market: str = Query(...),
        weeks: int = Query(DEFAULT_HISTORY_WEEKS),
        report: str = Query("auto"),
        lookbackWeeks: int = Query(DEFAULT_LOOKBACK_WEEKS),
    ) -> JSONResponse:
        span = _clamp(weeks, 1, MAX_HISTORY_WEEKS)
        lookback = _clamp(lookbackWeeks, MIN_LOOKBACK_WEEKS, MAX_LOOKBACK_WEEKS)

        def produce(resolved: list) -> dict:
            batches, errors = source.analyze(
                [market], weeks=span, lookback_weeks=lookback, report=report
            )
            records = [r for batch in batches for r in batch]
            return {
                "market": market.upper(),
                "weeksRequested": span,
                "weeksReturned": len(records),
                "lookbackWeeks": lookback,
                "records": records,
                "errors": errors,
                "note": (
                    "Each week is scored using only data available up to that week, "
                    "so the series carries no look-ahead bias."
                ),
            }

        return _serve_paid(request, route="history", symbols=[market], produce=produce)

    @app.get("/v1/cot/compare")
    def compare(
        request: Request,
        markets: str | None = Query(None),
        report: str = Query("auto"),
        lookbackWeeks: int = Query(DEFAULT_LOOKBACK_WEEKS),
    ) -> JSONResponse:
        symbols = _split_symbols(markets)
        lookback = _clamp(lookbackWeeks, MIN_LOOKBACK_WEEKS, MAX_LOOKBACK_WEEKS)

        def produce(resolved: list) -> dict:
            batches, errors = source.analyze(
                symbols, weeks=1, lookback_weeks=lookback, report=report
            )
            records = [r for batch in batches for r in batch]
            return {
                "marketsRequested": [m.label for m in resolved],
                "lookbackWeeks": lookback,
                "summary": source.summarize(records),
                "crossMarket": cross_compare(records),
                "records": records,
                "errors": errors,
            }

        return _serve_paid(request, route="compare", symbols=symbols, produce=produce)

    @app.get("/v1/cot/extremes")
    def extremes(
        request: Request,
        group: str = Query("all"),
        report: str = Query("auto"),
        lookbackWeeks: int = Query(DEFAULT_LOOKBACK_WEEKS),
        extremeLongThreshold: float = Query(90.0),
        extremeShortThreshold: float = Query(10.0),
        stretchedLongThreshold: float = Query(75.0),
        stretchedShortThreshold: float = Query(25.0),
    ) -> JSONResponse:
        key = (group or "all").strip().lower()
        presets = source.presets()
        if key == "all":
            symbols = [m["code"] for m in source.catalog()]
        elif key in presets:
            symbols = list(presets[key])
        else:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "unknown_group",
                    "detail": (
                        f"Unknown group {group!r}. Use 'all' or one of: "
                        f"{', '.join(sorted(presets))}."
                    ),
                },
            )

        lookback = _clamp(lookbackWeeks, MIN_LOOKBACK_WEEKS, MAX_LOOKBACK_WEEKS)
        ordered = (
            extremeShortThreshold
            < stretchedShortThreshold
            < stretchedLongThreshold
            < extremeLongThreshold
        )
        if not (0 <= extremeShortThreshold and extremeLongThreshold <= 100 and ordered):
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_thresholds",
                    "detail": (
                        "Thresholds must satisfy 0 <= extremeShort < stretchedShort "
                        "< stretchedLong < extremeLong <= 100."
                    ),
                },
            )
        thresholds = Thresholds(
            extreme_long=extremeLongThreshold,
            extreme_short=extremeShortThreshold,
            stretched_long=stretchedLongThreshold,
            stretched_short=stretchedShortThreshold,
        )

        def produce(resolved: list) -> dict:
            batches, errors = source.analyze(
                symbols,
                weeks=1,
                lookback_weeks=lookback,
                report=report,
                thresholds=thresholds,
            )
            records = [r for batch in batches for r in batch]
            result = screen_extremes(records)
            result["group"] = key
            result["lookbackWeeks"] = lookback
            result["errors"] = errors
            return result

        return _serve_paid(request, route="extremes", symbols=symbols, produce=produce)

    return app


def build_default_app() -> FastAPI:
    """Entry point for `uvicorn x402api.app:build_default_app --factory`.

    Not a module-level `app = create_app()`: that would run at import time and
    make an unconfigured payout destination look like an import error.
    """
    return create_app()


__all__ = ["create_app", "build_default_app", "MICRO_PER_USD"]
