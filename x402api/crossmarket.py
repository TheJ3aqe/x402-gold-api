"""Cross-market derived analytics -- what a single-market feed cannot tell you.

The Apify actor already turns raw CFTC columns into per-market positioning
(net, week-over-week change, Williams COT Index, z-score). This module answers
the questions that only exist ACROSS markets, and they are the reason an agent
would pay for one call here instead of scraping the CFTC itself:

  * How stretched is this market relative to the others right now?
  * Is speculative positioning aligned across the complex, or scattered?
  * Are the FX contracts telling a single coherent story about the dollar?
  * Where is positioning extreme but the weekly flow already turning against it?

Pure functions over the enriched records the shared pipeline produces. No
network, no framework, no state -- every branch is unit-testable offline.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

# The COT Index is a 0-100 min-max rank by construction, so 50 is its exact
# midpoint. "Stretch" is distance from that midpoint -- not a tuned constant.
COT_INDEX_MIDPOINT = 50.0

# Dispersion benchmark. A COT Index spread uniformly at random over 0-100 has a
# population standard deviation of 100/sqrt(12) = 28.87. That gives two honest
# reference points rather than invented ones:
#   * at or above 28.87 the markets are no more clustered than random -> no
#     cross-market signal exists, report it as "divided"
#   * below half of it (14.4) positioning is genuinely clustered -> "aligned"
# Both are overridable per call; they are defaults, not hardcoded thresholds.
UNIFORM_DISPERSION = round(100.0 / (12.0**0.5), 2)  # 28.87


@dataclass(frozen=True)
class CrossMarketThresholds:
    aligned_dispersion_max: float = UNIFORM_DISPERSION / 2.0
    divided_dispersion_min: float = UNIFORM_DISPERSION


# Which side of the US dollar a net-long position in each contract sits on.
#
# CME currency futures are quoted as the FOREIGN currency against USD, so being
# net long Euro FX futures is a short-dollar position -> sign -1. The ICE Dollar
# Index is the dollar itself -> sign +1. This mapping is the piece that turns
# six unrelated FX contracts into one dollar reading, and it is exactly the sort
# of domain detail a generic data feed gets wrong.
#
# Gold is deliberately EXCLUDED. It is USD-denominated and often trades inversely
# to the dollar, but the relationship is unstable (it breaks down in risk-off
# episodes when both rally), so folding it in would import a claim the data does
# not support. It is reported alongside, never inside, the dollar score.
USD_SIGN_BY_CODE: dict[str, int] = {
    "099741": -1,  # Euro FX
    "096742": -1,  # British Pound
    "097741": -1,  # Japanese Yen
    "092741": -1,  # Swiss Franc
    "090741": -1,  # Canadian Dollar
    "232741": -1,  # Australian Dollar
    "112741": -1,  # New Zealand Dollar
    "095741": -1,  # Mexican Peso
    "102741": -1,  # Brazilian Real
    "098662": +1,  # US Dollar Index
}


def _latest_per_market(records: list[dict]) -> list[dict]:
    """Newest record per contract, so a history query cannot double-count."""
    latest: dict[str, dict] = {}
    for record in records:
        code = record.get("cftcContractCode")
        if code is None:
            continue
        current = latest.get(code)
        if current is None or record.get("reportDate", "") > current.get("reportDate", ""):
            latest[code] = record
    return list(latest.values())


def stretch(record: dict) -> float | None:
    """Distance of the COT Index from its midpoint. Higher = more one-sided."""
    index = record.get("cotIndex")
    if index is None:
        return None
    return round(abs(float(index) - COT_INDEX_MIDPOINT), 2)


def rank_by_stretch(records: list[dict]) -> list[dict]:
    """Markets ordered most- to least-stretched, with their rank attached.

    Markets whose COT Index could not be computed (too little history, or a
    completely flat net) are returned last and flagged, never silently dropped --
    a caller paying for a comparison must see which markets did not participate.
    """
    rows = _latest_per_market(records)
    scored = [(stretch(r), r) for r in rows]
    ranked = sorted(
        (row for row in scored if row[0] is not None),
        key=lambda row: row[0],
        reverse=True,
    )
    out: list[dict] = []
    for position, (value, record) in enumerate(ranked, start=1):
        out.append(
            {
                "rank": position,
                "market": record.get("market"),
                "cftcContractCode": record.get("cftcContractCode"),
                "marketGroup": record.get("marketGroup"),
                "reportDate": record.get("reportDate"),
                "cotIndex": record.get("cotIndex"),
                "stretch": value,
                "positioning": record.get("positioning"),
                "positioningBias": record.get("positioningBias"),
                "flowDirection": record.get("flowDirection"),
                "net": record.get("net"),
                "netChange": record.get("netChange"),
                "netZScore": record.get("netZScore"),
            }
        )
    for value, record in scored:
        if value is None:
            out.append(
                {
                    "rank": None,
                    "market": record.get("market"),
                    "cftcContractCode": record.get("cftcContractCode"),
                    "marketGroup": record.get("marketGroup"),
                    "reportDate": record.get("reportDate"),
                    "cotIndex": None,
                    "stretch": None,
                    "positioning": record.get("positioning"),
                    "excludedReason": "COT Index unavailable (insufficient or flat history)",
                }
            )
    return out


def consensus(
    records: list[dict], thresholds: CrossMarketThresholds | None = None
) -> dict:
    """Is speculative positioning clustered across these markets, or scattered?"""
    t = thresholds or CrossMarketThresholds()
    rows = _latest_per_market(records)
    indices = [float(r["cotIndex"]) for r in rows if r.get("cotIndex") is not None]

    if not indices:
        return {
            "marketsScored": 0,
            "meanCotIndex": None,
            "dispersion": None,
            "regime": "insufficient_data",
            "netLongMarkets": 0,
            "netShortMarkets": 0,
            "interpretation": (
                "No market in this set had enough history for a COT Index."
            ),
        }

    mean_index = round(statistics.fmean(indices), 2)
    dispersion = round(statistics.pstdev(indices), 2) if len(indices) > 1 else 0.0

    if len(indices) < 2:
        regime = "insufficient_data"
    elif dispersion <= t.aligned_dispersion_max:
        regime = "aligned"
    elif dispersion >= t.divided_dispersion_min:
        regime = "divided"
    else:
        regime = "mixed"

    longs = sum(1 for r in rows if r.get("positioningBias") == "long")
    shorts = sum(1 for r in rows if r.get("positioningBias") == "short")

    if regime == "aligned":
        side = "long" if mean_index > COT_INDEX_MIDPOINT else "short"
        interpretation = (
            f"Positioning is clustered (dispersion {dispersion} vs "
            f"{t.aligned_dispersion_max} threshold): the complex is leaning {side} "
            "together, so a single catalyst can unwind several markets at once."
        )
    elif regime == "divided":
        interpretation = (
            f"Positioning is no more clustered than random (dispersion {dispersion} "
            f">= {t.divided_dispersion_min}): treat each market on its own, there is "
            "no cross-market read here."
        )
    elif regime == "mixed":
        interpretation = (
            f"Partial clustering (dispersion {dispersion}). Some markets share a "
            "lean, others do not -- see the stretch ranking."
        )
    else:
        interpretation = "Only one market scored; a consensus needs at least two."

    return {
        "marketsScored": len(indices),
        "meanCotIndex": mean_index,
        "dispersion": dispersion,
        "dispersionBenchmarkUniform": UNIFORM_DISPERSION,
        "regime": regime,
        "netLongMarkets": longs,
        "netShortMarkets": shorts,
        "interpretation": interpretation,
    }


def dollar_positioning(records: list[dict]) -> dict:
    """Turn the FX contracts into one speculative dollar reading.

    Each currency future is signed so that +1 means the position is LONG the
    dollar, then the COT Indices are averaged on that dollar-relative basis. A
    long Euro future (COT Index 80) is a short-dollar position and enters as 20.
    """
    rows = [
        r
        for r in _latest_per_market(records)
        if r.get("cftcContractCode") in USD_SIGN_BY_CODE
        and r.get("cotIndex") is not None
    ]
    if not rows:
        return {
            "contributingMarkets": 0,
            "dollarCotIndex": None,
            "bias": "unknown",
            "note": (
                "No dollar-relevant FX contract in this set. Request FX symbols "
                "(EURUSD, GBPUSD, JPY, DXY, ...) or the 'fx' preset."
            ),
        }

    contributions = []
    for record in rows:
        sign = USD_SIGN_BY_CODE[record["cftcContractCode"]]
        index = float(record["cotIndex"])
        # Flip a foreign-currency index onto the dollar's side of the trade.
        dollar_index = index if sign > 0 else round(100.0 - index, 2)
        contributions.append(
            {
                "market": record.get("market"),
                "cotIndex": index,
                "usdSign": sign,
                "dollarRelativeIndex": dollar_index,
            }
        )

    score = round(statistics.fmean(c["dollarRelativeIndex"] for c in contributions), 2)
    if score > COT_INDEX_MIDPOINT:
        bias = "long_usd"
    elif score < COT_INDEX_MIDPOINT:
        bias = "short_usd"
    else:
        bias = "neutral"

    return {
        "contributingMarkets": len(contributions),
        "dollarCotIndex": score,
        "bias": bias,
        "contributions": contributions,
        "note": (
            "Each CME currency future is quoted as the foreign currency vs USD, so "
            "its COT Index is inverted before averaging; the ICE Dollar Index enters "
            "directly. Gold is excluded on purpose -- its dollar correlation is real "
            "but unstable, and folding it in would overstate the signal."
        ),
    }


def divergences(records: list[dict]) -> list[dict]:
    """Markets where positioning is one-sided but the weekly flow has turned.

    An extreme that is still building is a trend. An extreme that has started
    unwinding is the interesting one, and it is only visible because the shared
    analytics layer carries both `positioning` and `flowDirection`.
    """
    out: list[dict] = []
    for record in _latest_per_market(records):
        positioning = record.get("positioning")
        flow = record.get("flowDirection")
        if positioning not in ("extreme_long", "stretched_long", "extreme_short", "stretched_short"):
            continue
        if flow not in ("accumulating", "distributing"):
            continue
        leaning_long = positioning.endswith("_long")
        turning_against = (leaning_long and flow == "distributing") or (
            not leaning_long and flow == "accumulating"
        )
        if not turning_against:
            continue
        out.append(
            {
                "market": record.get("market"),
                "cftcContractCode": record.get("cftcContractCode"),
                "reportDate": record.get("reportDate"),
                "positioning": positioning,
                "flowDirection": flow,
                "cotIndex": record.get("cotIndex"),
                "netChange": record.get("netChange"),
                "signal": (
                    "crowded_long_unwinding" if leaning_long else "crowded_short_covering"
                ),
            }
        )
    return sorted(out, key=lambda r: abs(r.get("netChange") or 0), reverse=True)


def compare(
    records: list[dict], thresholds: CrossMarketThresholds | None = None
) -> dict:
    """The full cross-market block returned by the /compare route."""
    return {
        "ranking": rank_by_stretch(records),
        "consensus": consensus(records, thresholds),
        "dollar": dollar_positioning(records),
        "divergences": divergences(records),
    }


def screen_extremes(records: list[dict]) -> dict:
    """Only the markets at a positioning extreme, plus how crowded the set is."""
    rows = _latest_per_market(records)
    hits = [
        row
        for row in rank_by_stretch(records)
        if row.get("positioning") in ("extreme_long", "extreme_short")
    ]
    stretched = [
        row
        for row in rank_by_stretch(records)
        if row.get("positioning") in ("stretched_long", "stretched_short")
    ]
    return {
        "marketsScanned": len(rows),
        "extremes": hits,
        "stretched": stretched,
        "divergences": divergences(records),
        "consensus": consensus(records),
        "latestReportDate": max(
            (r.get("reportDate") for r in rows if r.get("reportDate")), default=None
        ),
    }
