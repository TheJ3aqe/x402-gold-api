"""Input validation + fetch + analyze, with zero Apify coupling.

The Actor entrypoint (`src/main.py`) is a thin shell around this, so the whole
product can be exercised locally without an Apify account or token.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from .analytics import Thresholds, analyze_market
from .cftc import CftcClient, CftcError
from .markets import Market, expand

log = logging.getLogger(__name__)

MAX_WEEKS = 520
MAX_LOOKBACK = 1040
VALID_REPORTS = ("auto", "legacy", "disaggregated", "tff")


@dataclass
class RunConfig:
    markets: list[Market]
    report: str = "auto"
    combined: bool = False
    weeks: int = 1
    lookback_weeks: int = 156
    thresholds: Thresholds = field(default_factory=Thresholds)


def _clamp(value, lo: int, hi: int, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _threshold(raw: dict, key: str, default: float) -> float:
    """Read a numeric threshold, tolerating an explicit JSON null.

    `raw.get(key, default)` is NOT enough: when a caller sends {"key": null}
    the key exists, so the default is skipped and float(None) raises TypeError
    -- which the Actor's input-error handler does not catch. Anything present
    but non-numeric is a real input mistake and gets a readable ValueError.
    """
    value = raw.get(key)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"{key} must be a number between 0 and 100, got {value!r}."
        ) from None


def build_config(raw: dict | None) -> RunConfig:
    """Validate and normalize Actor input into a RunConfig.

    Raises ValueError with an actionable message for anything unusable -- the
    Actor must fail loudly on bad input, never silently return an empty dataset.
    """
    raw = raw or {}

    report = str(raw.get("reportType") or "auto").strip().lower()
    if report not in VALID_REPORTS:
        raise ValueError(
            f"reportType must be one of {', '.join(VALID_REPORTS)}, got {report!r}."
        )

    markets_in = raw.get("markets")
    if isinstance(markets_in, str):
        markets_in = [p for p in markets_in.replace(";", ",").split(",") if p.strip()]
    if markets_in is not None and not isinstance(markets_in, list):
        raise ValueError("markets must be an array of symbols or a comma-separated string.")
    markets = expand(markets_in)

    weeks = _clamp(raw.get("weeks", 1), 1, MAX_WEEKS, 1)
    lookback = _clamp(raw.get("cotIndexLookbackWeeks", 156), 8, MAX_LOOKBACK, 156)

    t = Thresholds(
        extreme_long=_threshold(raw, "extremeLongThreshold", 90.0),
        extreme_short=_threshold(raw, "extremeShortThreshold", 10.0),
        stretched_long=_threshold(raw, "stretchedLongThreshold", 75.0),
        stretched_short=_threshold(raw, "stretchedShortThreshold", 25.0),
    )
    if not (0 <= t.extreme_short < t.stretched_short < t.stretched_long < t.extreme_long <= 100):
        raise ValueError(
            "Thresholds must satisfy 0 <= extremeShort < stretchedShort < "
            f"stretchedLong < extremeLong <= 100 (got {t})."
        )

    return RunConfig(
        markets=markets,
        report=report,
        combined=bool(raw.get("combinedFuturesAndOptions", False)),
        weeks=weeks,
        lookback_weeks=lookback,
        thresholds=t,
    )


def run(
    cfg: RunConfig,
    client: CftcClient | None = None,
    on_error: Callable[[Market, Exception], None] | None = None,
) -> Iterator[list[dict]]:
    """Yield one list of enriched records per requested market.

    Yielding per market (instead of returning everything at once) lets the Actor
    push results and charge for them incrementally, so a failure on market 7
    does not throw away markets 1-6.
    """
    client = client or CftcClient()
    # We need `weeks` rows of output plus `lookback_weeks - 1` extra rows of
    # history behind the oldest one, or the COT Index of older weeks is wrong.
    fetch_limit = min(cfg.weeks + cfg.lookback_weeks, MAX_WEEKS + MAX_LOOKBACK)

    for market in cfg.markets:
        report = market.preferred_report if cfg.report == "auto" else cfg.report
        try:
            rows = client.fetch_history(
                market.code, report=report, combined=cfg.combined, limit=fetch_limit
            )
        except CftcError as exc:
            # 'auto' picked a report family that does not carry this contract:
            # fall back to legacy, which covers essentially every contract.
            if cfg.report == "auto" and report != "legacy":
                log.warning("%s not in %s report, falling back to legacy: %s",
                            market.label, report, exc)
                try:
                    rows, report = (
                        client.fetch_history(
                            market.code, report="legacy",
                            combined=cfg.combined, limit=fetch_limit,
                        ),
                        "legacy",
                    )
                except CftcError as exc2:
                    log.error("Skipping %s: %s", market.label, exc2)
                    if on_error:
                        on_error(market, exc2)
                    continue
            else:
                log.error("Skipping %s: %s", market.label, exc)
                if on_error:
                    on_error(market, exc)
                continue

        records = analyze_market(
            code=market.code,
            label=market.label,
            group=market.group,
            report=report,
            rows=rows,
            weeks=cfg.weeks,
            lookback_weeks=cfg.lookback_weeks,
            thresholds=cfg.thresholds,
        )
        if not records:
            log.error("Skipping %s: CFTC rows carried no usable position columns.",
                      market.label)
            if on_error:
                on_error(market, CftcError("No usable position columns in CFTC rows."))
            continue
        yield records
