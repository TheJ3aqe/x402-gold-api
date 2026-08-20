"""Curated map: trader-friendly symbol -> CFTC contract market code.

The CFTC identifies every contract by a 6-char `cftc_contract_market_code`
(e.g. GOLD on COMEX = "088691"). Nobody outside the CFTC thinks in those
codes, so this module lets callers say `GOLD`, `XAUUSD` or `EURUSD` instead.

Codes were read off the live CFTC dataset (report week 2026-08-11) and are
stable identifiers -- they do not change when a contract rolls.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Market:
    code: str
    label: str
    group: str
    # Which report family carries the most meaningful breakdown for this market.
    preferred_report: str
    aliases: tuple[str, ...] = ()


# fmt: off
_MARKETS: tuple[Market, ...] = (
    # --- Precious & base metals (disaggregated = Managed Money is the signal) ---
    Market("088691", "Gold (COMEX)",            "metals", "disaggregated", ("GOLD", "XAUUSD", "XAU", "GC")),
    Market("084691", "Silver (COMEX)",          "metals", "disaggregated", ("SILVER", "XAGUSD", "XAG", "SI")),
    Market("076651", "Platinum (NYMEX)",        "metals", "disaggregated", ("PLATINUM", "XPTUSD", "PL")),
    Market("075651", "Palladium (NYMEX)",       "metals", "disaggregated", ("PALLADIUM", "XPDUSD", "PA")),
    Market("085692", "Copper #1 (COMEX)",       "metals", "disaggregated", ("COPPER", "HG")),
    Market("088695", "Micro Gold (COMEX)",      "metals", "disaggregated", ("MICROGOLD", "MGC")),

    # --- FX majors (TFF = Leveraged Money is the speculative signal) ---
    Market("099741", "Euro FX (CME)",           "fx", "tff", ("EURUSD", "EUR", "EURO", "6E")),
    Market("096742", "British Pound (CME)",     "fx", "tff", ("GBPUSD", "GBP", "POUND", "6B")),
    Market("097741", "Japanese Yen (CME)",      "fx", "tff", ("JPY", "USDJPY", "YEN", "6J")),
    Market("092741", "Swiss Franc (CME)",       "fx", "tff", ("CHF", "USDCHF", "FRANC", "6S")),
    Market("090741", "Canadian Dollar (CME)",   "fx", "tff", ("CAD", "USDCAD", "6C")),
    Market("232741", "Australian Dollar (CME)", "fx", "tff", ("AUD", "AUDUSD", "6A")),
    Market("112741", "New Zealand Dollar (CME)", "fx", "tff", ("NZD", "NZDUSD", "6N")),
    Market("095741", "Mexican Peso (CME)",      "fx", "tff", ("MXN", "USDMXN", "PESO", "6M")),
    Market("102741", "Brazilian Real (CME)",    "fx", "tff", ("BRL", "USDBRL", "REAL")),
    Market("098662", "US Dollar Index (ICE)",   "fx", "tff", ("DXY", "USDINDEX", "USDX", "DOLLARINDEX")),

    # --- Energy ---
    Market("067651", "WTI Crude Oil (NYMEX)",   "energy", "disaggregated", ("WTI", "CRUDE", "CL", "USOIL")),
    Market("023651", "Natural Gas (NYMEX)",     "energy", "disaggregated", ("NATGAS", "NG", "NATURALGAS")),

    # --- Equity indices (TFF) ---
    Market("13874A", "E-mini S&P 500 (CME)",    "indices", "tff", ("SPX", "SP500", "ES", "SPY")),
    Market("209742", "E-mini Nasdaq-100 (CME)", "indices", "tff", ("NDX", "NASDAQ", "NQ")),
    Market("239742", "E-mini Russell 2000 (CME)", "indices", "tff", ("RUSSELL", "RTY", "RUT")),
    Market("124603", "DJIA x $5 (CBOT)",        "indices", "tff", ("DJIA", "DOW", "YM")),
    Market("1170E1", "VIX Futures (CFE)",       "indices", "tff", ("VIX",)),

    # --- Crypto ---
    Market("133741", "Bitcoin (CME)",           "crypto", "tff", ("BTC", "BITCOIN", "BTCUSD")),
    Market("146021", "Ether Cash Settled (CME)", "crypto", "tff", ("ETH", "ETHER", "ETHEREUM", "ETHUSD")),

    # --- Rates ---
    Market("043602", "UST 10Y Note (CBOT)",     "rates", "tff", ("UST10Y", "ZN", "TNOTE")),
    Market("020601", "UST Bond (CBOT)",         "rates", "tff", ("USTBOND", "ZB")),
    Market("042601", "UST 2Y Note (CBOT)",      "rates", "tff", ("UST2Y", "ZT")),

    # --- Ags & softs ---
    Market("002602", "Corn (CBOT)",             "ags", "disaggregated", ("CORN", "ZC")),
    Market("005602", "Soybeans (CBOT)",         "ags", "disaggregated", ("SOYBEANS", "SOY", "ZS")),
    Market("001602", "Wheat SRW (CBOT)",        "ags", "disaggregated", ("WHEAT", "ZW")),
    Market("083731", "Coffee C (ICE)",          "ags", "disaggregated", ("COFFEE", "KC")),
    Market("073732", "Cocoa (ICE)",             "ags", "disaggregated", ("COCOA", "CC")),
    Market("080732", "Sugar No. 11 (ICE)",      "ags", "disaggregated", ("SUGAR", "SB")),
)
# fmt: on

BY_CODE: dict[str, Market] = {m.code: m for m in _MARKETS}

_ALIAS_INDEX: dict[str, Market] = {}
for _m in _MARKETS:
    _ALIAS_INDEX[_m.code.upper()] = _m
    for _a in _m.aliases:
        _ALIAS_INDEX[_a.upper()] = _m

# Sensible default when the caller gives us nothing: the markets a gold/FX
# desk actually looks at on a Monday morning.
DEFAULT_MARKETS: tuple[str, ...] = ("GOLD", "SILVER", "EURUSD", "GBPUSD", "JPY", "DXY")

PRESETS: dict[str, tuple[str, ...]] = {
    "metals": ("GOLD", "SILVER", "PLATINUM", "PALLADIUM", "COPPER"),
    "fx": ("EURUSD", "GBPUSD", "JPY", "CHF", "CAD", "AUD", "NZD", "DXY"),
    "energy": ("WTI", "NATGAS"),
    "indices": ("SPX", "NDX", "RUSSELL", "DJIA", "VIX"),
    "crypto": ("BTC", "ETH"),
    "rates": ("UST10Y", "USTBOND", "UST2Y"),
    "ags": ("CORN", "SOYBEANS", "WHEAT", "COFFEE", "COCOA", "SUGAR"),
}


class UnknownMarketError(ValueError):
    """Raised when a caller asks for a symbol we cannot map to a CFTC code."""


def resolve(symbol: str) -> Market:
    """Map a user-supplied symbol/alias/raw CFTC code to a Market.

    Raw 6-char CFTC codes that are not in the curated catalog are accepted and
    returned as a synthetic Market, so power users are never boxed in by our
    list. Anything else raises UnknownMarketError with close suggestions.
    """
    key = (symbol or "").strip().upper().replace("/", "").replace("-", "").replace("_", "")
    if not key:
        raise UnknownMarketError("Empty market symbol.")
    hit = _ALIAS_INDEX.get(key)
    if hit is not None:
        return hit
    # Unlisted but syntactically valid raw CFTC code -> pass through.
    if len(key) == 6 and key.isalnum():
        return Market(key, f"CFTC contract {key}", "custom", "legacy")
    suggestions = sorted(a for a in _ALIAS_INDEX if key[:3] in a)[:6]
    raise UnknownMarketError(
        f"Unknown market {symbol!r}. Use a symbol like GOLD / EURUSD / BTC, a preset "
        f"({', '.join(PRESETS)}), or a raw 6-character CFTC contract code."
        + (f" Did you mean: {', '.join(suggestions)}?" if suggestions else "")
    )


def expand(symbols: list[str] | None) -> list[Market]:
    """Expand a mixed list of symbols/presets into a deduplicated Market list."""
    raw = symbols if symbols else list(DEFAULT_MARKETS)
    out: list[Market] = []
    seen: set[str] = set()
    for item in raw:
        token = (item or "").strip()
        if not token:
            continue
        preset = PRESETS.get(token.lower())
        members = preset if preset else (token,)
        for member in members:
            market = resolve(member)
            if market.code not in seen:
                seen.add(market.code)
                out.append(market)
    if not out:
        raise UnknownMarketError("No valid markets resolved from input.")
    return out


def catalog() -> list[dict]:
    """Machine-readable dump of the catalog (used by the README + tests)."""
    return [
        {
            "code": m.code,
            "label": m.label,
            "group": m.group,
            "preferredReport": m.preferred_report,
            "aliases": list(m.aliases),
        }
        for m in _MARKETS
    ]
