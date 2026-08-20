"""Derived COT analytics -- the part nobody else on the Apify Store ships.

Every existing COT Actor dumps the CFTC's raw columns. Raw columns are not
what a trader acts on. What they act on is *positioning relative to its own
history*: net exposure, how it changed week over week, and where that net sits
inside its multi-year range (the Williams COT Index). This module computes that.

Pure functions, no network and no Apify dependency, so it is unit-testable and
reusable outside the Actor.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

# Column layout per CFTC report family.
#
# NOTE: the CFTC's own Socrata column names contain real typos that we must
# mirror exactly -- "noncomm_postions_spread_all" (missing 'i') in the legacy
# report and "swap__positions_short_all" (double underscore) in the
# disaggregated report. Verified against the live API on 2026-08-17. Each group
# lists candidate column names; the first one present in the row wins.
GROUPS: dict[str, dict[str, dict[str, list[str]]]] = {
    "legacy": {
        "nonCommercial": {
            "long": ["noncomm_positions_long_all"],
            "short": ["noncomm_positions_short_all"],
            "spread": ["noncomm_postions_spread_all", "noncomm_positions_spread_all"],
            "tradersLong": ["traders_noncomm_long_all"],
            "tradersShort": ["traders_noncomm_short_all"],
        },
        "commercial": {
            "long": ["comm_positions_long_all"],
            "short": ["comm_positions_short_all"],
            "tradersLong": ["traders_comm_long_all"],
            "tradersShort": ["traders_comm_short_all"],
        },
        "nonReportable": {
            "long": ["nonrept_positions_long_all"],
            "short": ["nonrept_positions_short_all"],
        },
    },
    "disaggregated": {
        "producerMerchant": {
            "long": ["prod_merc_positions_long"],
            "short": ["prod_merc_positions_short"],
        },
        "swapDealer": {
            "long": ["swap_positions_long_all"],
            "short": ["swap__positions_short_all", "swap_positions_short_all"],
            "spread": ["swap__positions_spread_all", "swap_positions_spread_all"],
        },
        "managedMoney": {
            "long": ["m_money_positions_long_all"],
            "short": ["m_money_positions_short_all"],
            "spread": ["m_money_positions_spread"],
            "tradersLong": ["traders_m_money_long_all"],
            "tradersShort": ["traders_m_money_short_all"],
        },
        "otherReportable": {
            "long": ["other_rept_positions_long"],
            "short": ["other_rept_positions_short"],
            "spread": ["other_rept_positions_spread"],
        },
        "nonReportable": {
            "long": ["nonrept_positions_long_all"],
            "short": ["nonrept_positions_short_all"],
        },
    },
    "tff": {
        "dealer": {
            "long": ["dealer_positions_long_all"],
            "short": ["dealer_positions_short_all"],
            "spread": ["dealer_positions_spread_all"],
        },
        "assetManager": {
            "long": ["asset_mgr_positions_long"],
            "short": ["asset_mgr_positions_short"],
            "spread": ["asset_mgr_positions_spread"],
        },
        "leveragedMoney": {
            "long": ["lev_money_positions_long"],
            "short": ["lev_money_positions_short"],
            "spread": ["lev_money_positions_spread"],
            "tradersLong": ["traders_lev_money_long_all"],
            "tradersShort": ["traders_lev_money_short_all"],
        },
        "otherReportable": {
            "long": ["other_rept_positions_long"],
            "short": ["other_rept_positions_short"],
            "spread": ["other_rept_positions_spread"],
        },
        "nonReportable": {
            "long": ["nonrept_positions_long_all"],
            "short": ["nonrept_positions_short_all"],
        },
    },
}

# The group whose net position is the headline "speculative" signal.
PRIMARY_GROUP: dict[str, str] = {
    "legacy": "nonCommercial",
    "disaggregated": "managedMoney",
    "tff": "leveragedMoney",
}


def _num(row: dict, candidates: list[str]) -> int | None:
    """First present, parseable value among candidate column names."""
    for name in candidates:
        raw = row.get(name)
        if raw in (None, "", "."):
            continue
        try:
            return int(float(str(raw).replace(",", "")))
        except (TypeError, ValueError):
            continue
    return None


def _report_date(row: dict) -> str:
    return str(row.get("report_date_as_yyyy_mm_dd", ""))[:10]


@dataclass(frozen=True)
class Thresholds:
    """Configurable, never hardcoded at the call site (adaptive-by-config rule)."""

    extreme_long: float = 90.0
    extreme_short: float = 10.0
    stretched_long: float = 75.0
    stretched_short: float = 25.0


def _classify(index: float | None, t: Thresholds) -> str:
    if index is None:
        return "insufficient_history"
    if index >= t.extreme_long:
        return "extreme_long"
    if index >= t.stretched_long:
        return "stretched_long"
    if index <= t.extreme_short:
        return "extreme_short"
    if index <= t.stretched_short:
        return "stretched_short"
    return "neutral"


def _cot_index(current: int, window: list[int]) -> float | None:
    """Williams COT Index: where `current` sits in the min-max range of `window`.

    100 = most long the group has ever been in the lookback, 0 = most short.
    Returns None when the window is too small or completely flat to be meaningful.
    """
    if len(window) < 2:
        return None
    lo, hi = min(window), max(window)
    if hi == lo:
        return None
    return round((current - lo) / (hi - lo) * 100.0, 2)


def _percentile_rank(current: int, window: list[int]) -> float | None:
    """Share of lookback weeks whose net was <= current (rank, not min-max)."""
    if len(window) < 2:
        return None
    below = sum(1 for v in window if v <= current)
    return round(below / len(window) * 100.0, 2)


def _zscore(current: int, window: list[int]) -> float | None:
    if len(window) < 3:
        return None
    try:
        sigma = statistics.pstdev(window)
    except statistics.StatisticsError:
        return None
    if sigma == 0:
        return None
    return round((current - statistics.fmean(window)) / sigma, 3)


def _group_block(row: dict, spec: dict[str, list[str]]) -> dict | None:
    long_ = _num(row, spec["long"])
    short_ = _num(row, spec["short"])
    if long_ is None or short_ is None:
        return None
    block: dict = {
        "long": long_,
        "short": short_,
        "net": long_ - short_,
        "gross": long_ + short_,
    }
    if "spread" in spec:
        block["spreading"] = _num(row, spec["spread"])
    block["longShortRatio"] = round(long_ / short_, 4) if short_ else None
    block["pctLong"] = (
        round(long_ / (long_ + short_) * 100.0, 2) if (long_ + short_) else None
    )
    traders_long = _num(row, spec.get("tradersLong", []))
    traders_short = _num(row, spec.get("tradersShort", []))
    if traders_long is not None or traders_short is not None:
        block["tradersLong"] = traders_long
        block["tradersShort"] = traders_short
    return block


def analyze_market(
    *,
    code: str,
    label: str,
    group: str,
    report: str,
    rows: list[dict],
    weeks: int,
    lookback_weeks: int,
    thresholds: Thresholds | None = None,
) -> list[dict]:
    """Turn newest-first raw CFTC rows into enriched weekly result records.

    `rows` must be sorted newest first (that is what CftcClient returns).
    Returns at most `weeks` records, newest first.
    """
    thresholds = thresholds or Thresholds()
    specs = GROUPS.get(report)
    if specs is None:
        raise ValueError(f"Unsupported report family {report!r}.")
    primary_name = PRIMARY_GROUP[report]
    primary_spec = specs[primary_name]

    # Net series over the whole fetched history, newest first.
    net_series: list[int | None] = []
    for row in rows:
        long_ = _num(row, primary_spec["long"])
        short_ = _num(row, primary_spec["short"])
        net_series.append(None if long_ is None or short_ is None else long_ - short_)

    results: list[dict] = []
    for i, row in enumerate(rows[: max(1, weeks)]):
        current_net = net_series[i]
        if current_net is None:
            continue

        # Lookback window = this week plus the `lookback_weeks - 1` weeks before it.
        window = [v for v in net_series[i : i + lookback_weeks] if v is not None]
        prev_net = net_series[i + 1] if i + 1 < len(net_series) else None

        oi = _num(row, ["open_interest_all"])
        index = _cot_index(current_net, window)

        record: dict = {
            "market": label,
            "marketGroup": group,
            "cftcContractCode": code,
            "cftcMarketName": row.get("market_and_exchange_names"),
            "reportType": report,
            "reportDate": _report_date(row),
            "reportWeek": row.get("yyyy_report_week_ww"),
            "openInterest": oi,
            "openInterestChange": _num(row, ["change_in_open_interest_all"]),
            "primaryGroup": primary_name,
            "net": current_net,
            "netPrevious": prev_net,
            "netChange": None if prev_net is None else current_net - prev_net,
            # Divide by abs(prev_net), not prev_net: nets are routinely
            # negative, and dividing by a negative base would flip the sign so
            # that a growing short read as a positive percentage. With abs()
            # the sign of netChangePct always matches the sign of netChange.
            "netChangePct": (
                round((current_net - prev_net) / abs(prev_net) * 100.0, 2)
                if prev_net not in (None, 0)
                else None
            ),
            "netPctOfOpenInterest": (
                round(current_net / oi * 100.0, 2) if oi else None
            ),
            "cotIndex": index,
            "cotIndexLookbackWeeks": len(window),
            "netPercentile": _percentile_rank(current_net, window),
            "netZScore": _zscore(current_net, window),
            "netMinInLookback": min(window) if window else None,
            "netMaxInLookback": max(window) if window else None,
            "positioning": _classify(index, thresholds),
            "positioningBias": (
                "long" if current_net > 0 else "short" if current_net < 0 else "flat"
            ),
            "flowDirection": (
                None
                if prev_net is None
                else "accumulating"
                if current_net > prev_net
                else "distributing"
                if current_net < prev_net
                else "unchanged"
            ),
            "source": "CFTC Commitments of Traders (publicreporting.cftc.gov)",
        }

        groups_out: dict[str, dict] = {}
        for name, spec in specs.items():
            block = _group_block(row, spec)
            if block is not None:
                groups_out[name] = block
        record["groups"] = groups_out

        # Commercial/producer hedgers are the structural counterparty; surface
        # their net separately because divergence vs. specs is the classic setup.
        hedger_key = next(
            (k for k in ("commercial", "producerMerchant", "dealer") if k in groups_out),
            None,
        )
        if hedger_key:
            record["hedgerGroup"] = hedger_key
            record["hedgerNet"] = groups_out[hedger_key]["net"]

        results.append(record)

    return results


def summarize(records: list[dict]) -> dict:
    """Run-level summary: what a caller wants to see without paging the dataset."""
    latest = {}
    for r in records:
        key = r["cftcContractCode"]
        if key not in latest or r["reportDate"] > latest[key]["reportDate"]:
            latest[key] = r
    rows = list(latest.values())
    return {
        "marketsAnalyzed": len(rows),
        "recordsProduced": len(records),
        "latestReportDate": max((r["reportDate"] for r in rows), default=None),
        "extremes": [
            {
                "market": r["market"],
                "positioning": r["positioning"],
                "cotIndex": r["cotIndex"],
                "net": r["net"],
            }
            for r in rows
            if r["positioning"] in ("extreme_long", "extreme_short")
        ],
        "biggestNetChange": (
            max(
                (r for r in rows if r.get("netChange") is not None),
                key=lambda r: abs(r["netChange"]),
                default=None,
            )
            or {}
        ).get("market"),
    }
