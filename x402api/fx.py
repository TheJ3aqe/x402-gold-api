"""USD -> EUR conversion for the settlement journal.

WHY THIS EXISTS AT ALL: German bookkeeping values every inflow at its EUR
equivalent AT THE MOMENT OF INFLOW (vault note, "Krypto-Zufluss DE", point 2).
With per-call settlement that is potentially thousands of taxable events per
month, so the rate has to be captured per settlement, live, not reconstructed
from memory a year later.

STATUS: PLACEHOLDER. There is no live FX feed wired up yet, and inventing one
would be worse than admitting it -- so every record this module produces is
stamped with its source, and the placeholder source string says so in plain
text. Nothing downstream can mistake a placeholder for a market rate.

TODO(go-live): replace FixedRateProvider with a real feed before the first
mainnet settlement. The ECB publishes a free daily reference rate
(https://data.ecb.europa.eu, EXR/D.USD.EUR.SP00.A) which is the rate German tax
practice normally accepts; wire that in behind the same RateProvider protocol
and nothing else in this package changes. Ask the tax advisor first whether a
daily reference rate is acceptable for thousands of micro-inflows or whether a
monthly average is (vault note, same section, open question).

STATUS UPDATE (19.08.2026): EcbRateProvider below implements that feed
(ECB Statistical Data Warehouse REST API, free, no key). It is NOT wired in as
`default_provider()` yet -- the tax-advisor question above is still genuinely
open and not something to decide unilaterally. `default_provider()` keeps
returning FixedRateProvider (placeholder-flagged) until Kevin confirms daily-
vs-monthly with the advisor; then swap one line here. Use
`X402_FX_PROVIDER=ecb` to opt in and exercise it before that (e.g. for manual
comparison against a bank statement), or instantiate EcbRateProvider() directly.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol

log = logging.getLogger(__name__)

# Marker written into every journal line whose rate did not come from a feed.
# Grep for it to find every record that still needs restating.
PLACEHOLDER_SOURCE = "PLACEHOLDER-not-a-market-rate"
ENV_SOURCE = "env:X402_USD_EUR_RATE"

# Order-of-magnitude anchor only, so the journal is not full of nulls and the
# arithmetic path is exercised. USD/EUR sat near 0.86 EUR per USD in mid-2026;
# this is NOT a quoted rate for any particular day and must never be used to
# file anything. Override with X402_USD_EUR_RATE, replace with a feed at go-live.
PLACEHOLDER_USD_EUR = 0.86


class RateProvider(Protocol):
    """Swap-in point for a real feed. One method, no state assumptions."""

    def usd_to_eur(self) -> "Rate": ...


@dataclass(frozen=True)
class Rate:
    value: float  # EUR per 1 USD
    source: str  # provenance, written verbatim into the journal

    @property
    def is_placeholder(self) -> bool:
        # startswith, not == : the ECB-fallback source ("PLACEHOLDER-...:ecb-
        # fetch-failed") also used the placeholder VALUE and must be flagged
        # for restating too -- an auditor filtering the journal on this flag
        # would otherwise silently miss every row where the feed happened to
        # be down. The suffix stays distinguishable in the source string
        # itself for anyone reading it, just not in this boolean.
        return self.source.startswith(PLACEHOLDER_SOURCE)

    def convert(self, usd: float) -> float:
        """EUR equivalent, rounded to cents (the unit the books are kept in)."""
        return round(usd * self.value, 2)


@dataclass(frozen=True)
class FixedRateProvider:
    """A constant rate: from X402_USD_EUR_RATE if set, else the placeholder.

    Kept explicit rather than defaulting silently, because the difference
    between "operator pinned a rate" and "nobody has wired a feed yet" is
    exactly what the auditor will ask about.
    """

    rate: float | None = None
    source: str | None = None

    def usd_to_eur(self) -> Rate:
        if self.rate is not None:
            return Rate(value=float(self.rate), source=self.source or "fixed:explicit")
        raw = os.environ.get("X402_USD_EUR_RATE")
        if raw is not None and str(raw).strip():
            try:
                value = float(raw)
            except ValueError as exc:
                raise ValueError(
                    f"X402_USD_EUR_RATE must be a number (EUR per 1 USD), got {raw!r}."
                ) from exc
            if value <= 0:
                raise ValueError(
                    f"X402_USD_EUR_RATE must be positive, got {value}."
                )
            return Rate(value=value, source=ENV_SOURCE)
        return Rate(value=PLACEHOLDER_USD_EUR, source=PLACEHOLDER_SOURCE)


# ECB Statistical Data Warehouse, free REST API, no key. D.USD.EUR.SP00.A =
# daily spot, USD per 1 EUR (ECB quotes EUR as the base currency) -- inverted
# below because this module's unit is EUR per 1 USD throughout.
ECB_URL = "https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A"
ECB_SOURCE_PREFIX = "ecb:D.USD.EUR.SP00.A:"


@dataclass
class EcbRateProvider:
    """Live ECB daily reference rate. NOT the default yet -- see the module
    docstring's "STATUS UPDATE" for why (open tax-advisor question on
    daily-vs-monthly, not something to decide unilaterally).

    Caches successful reads for `cache_seconds` -- the ECB publishes once per
    business day around 16:00 CET, so re-fetching per settlement would be
    pure waste on a per-call-priced API. On any network/parse failure this
    does NOT raise: a paid settlement must still be journalled even if the
    FX feed is down. It logs loudly and falls back to the placeholder rate,
    tagged so the fallback is distinguishable both from a real feed read and
    from the ordinary "nobody wired a feed" placeholder.
    """

    url: str = ECB_URL
    timeout: float = 5.0
    cache_seconds: float = 3600.0
    _cached: Rate | None = field(default=None, init=False, repr=False, compare=False)
    _cached_at: float = field(default=0.0, init=False, repr=False, compare=False)

    def usd_to_eur(self) -> Rate:
        now = time.monotonic()
        if self._cached is not None and (now - self._cached_at) < self.cache_seconds:
            return self._cached
        try:
            rate = self._fetch()
        except Exception as exc:  # noqa: BLE001 -- a settlement must still get journalled
            log.warning("ECB FX fetch failed (%s) -- falling back to placeholder rate.", exc)
            return Rate(
                value=PLACEHOLDER_USD_EUR,
                source=f"{PLACEHOLDER_SOURCE}:ecb-fetch-failed",
            )
        self._cached = rate
        self._cached_at = now
        return rate

    def _fetch(self) -> Rate:
        req = urllib.request.Request(
            f"{self.url}?lastNObservations=1&format=jsondata",
            headers={"Accept": "application/json", "User-Agent": "jarvis-x402-gold-api/1.0"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 -- fixed ECB host
            data = json.load(resp)

        series = data["dataSets"][0]["series"]
        series_key = next(iter(series))
        observations = series[series_key]["observations"]
        obs_index_str, obs_value = next(iter(observations.items()))
        usd_per_eur = float(obs_value[0])
        if usd_per_eur <= 0:
            raise ValueError(f"ECB returned a non-positive rate: {usd_per_eur!r}")

        time_values = data["structure"]["dimensions"]["observation"][0]["values"]
        obs_date = time_values[int(obs_index_str)]["id"] if time_values else "unknown-date"

        eur_per_usd = round(1.0 / usd_per_eur, 6)
        return Rate(value=eur_per_usd, source=f"{ECB_SOURCE_PREFIX}{obs_date}")


def default_provider() -> RateProvider:
    """FixedRateProvider unless X402_FX_PROVIDER=ecb is set explicitly.

    Not flipped to EcbRateProvider by default even though it works (see
    EcbRateProvider docstring) -- the daily-vs-monthly tax question is
    Kevin's / the tax advisor's call, not an autonomous one.
    """
    if os.environ.get("X402_FX_PROVIDER", "").strip().lower() == "ecb":
        return EcbRateProvider()
    return FixedRateProvider()
