"""What each route costs, and the arithmetic that makes the number defensible.

CLAUDE.md forbids magic numbers, so every price below carries its derivation.
The binding constraint is the CDP facilitator fee, verified live 2026-08-17
(see vault note Jarvis-Einkommensquellen, wave 6):

    first 1,000 on-chain settlements per calendar month  -> free
    every settlement after that                          -> $0.001

That fee is a FLAT amount per settlement, so it eats a percentage of revenue
that depends entirely on the ticket size:

    price/call   fee share of revenue   verdict
    $0.001            100 %             the whole call is fee
    $0.004             25 %             worse than Apify's flat 20 % cut
    $0.010             10 %             half of Apify -- acceptable floor
    $0.030              3.3 %
    $0.100              1 %

Hence the rule this module enforces in code rather than in a comment:
NO PRICED ROUTE MAY COST LESS THAN $0.01 PER CALL (MIN_PRICE_MICRO_USD).
The alternative escape hatch named in the vault note is the `batch-settlement`
scheme, which amortises one on-chain transaction over thousands of calls and
drives the fee toward zero -- but per the x402 spec that is a separate payment
scheme requiring a funded payment channel to operate, so it is deliberately NOT
the launch configuration. Pricing at/above the floor gets the same margin
protection with none of the operational surface. See README "Pricing".

Money is handled exclusively as INTEGER atomic units (USDC has 6 decimals, so
1 USD = 1_000_000 units). Floats never touch a payment amount: 0.1 + 0.2 is not
0.3 in binary floating point, and an off-by-one-wei mismatch fails verification.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# USDC on every supported chain uses 6 decimals (verified against the x402
# reference asset table, 2026-08-19). One "micro" here == one atomic USDC unit.
MICRO_PER_USD = 1_000_000

# The floor derived in the module docstring: $0.01 keeps the flat $0.001
# facilitator fee at or below 10 % of revenue.
MIN_PRICE_MICRO_USD = 10_000

# Free monthly settlements before the fee starts (CDP, verified 2026-08-17).
# Used only to report headroom on the /health route -- never to justify pricing
# below the floor, because the allowance resets monthly and volume is unknown.
FREE_SETTLEMENTS_PER_MONTH = 1_000
FEE_PER_SETTLEMENT_MICRO_USD = 1_000  # $0.001


class PricingError(ValueError):
    """Raised when a price table would violate the economics above."""


@dataclass(frozen=True)
class RoutePrice:
    """Base price plus a marginal price per additional market analysed.

    Multi-market routes do N upstream CFTC round trips and N analytics passes,
    so a flat price would either overcharge a 1-market call or subsidise a
    34-market sweep. `base` covers the first market, `per_extra_market` covers
    each one after it.
    """

    route: str
    base_micro_usd: int
    per_extra_market_micro_usd: int
    description: str

    def quote(self, market_count: int) -> int:
        """Exact price in atomic USDC units for a call over `market_count` markets."""
        extra = max(0, int(market_count) - 1)
        return self.base_micro_usd + extra * self.per_extra_market_micro_usd


# --- The table ---------------------------------------------------------------
#
# Per-market marginal costs are anchored on the snapshot price, which is the
# unit of work: exactly one CFTC HTTP request plus one analytics pass over
# ~3 years of weekly history.
#
#   snapshot  $0.010  one market, latest week, fully enriched. The floor price:
#                     it is the cheapest thing worth settling on-chain.
#   history   $0.030  same single upstream request, but returns up to 520
#                     enriched weekly records instead of one. Priced at 3x the
#                     snapshot because the series -- not the last print -- is
#                     what a backtesting agent is actually buying.
#   compare   $0.010 + $0.008/extra market. The marginal market is discounted
#                     20 % vs. buying snapshots separately ($0.008 vs $0.010),
#                     which is the incentive to call this instead of looping
#                     snapshot; the cross-market consensus/dispersion block is
#                     only computable here anyway.
#   extremes  $0.020 + $0.004/extra market. The screener sweeps a whole group
#                     (5-8 markets) or the full 34-market catalog. The marginal
#                     market is 60 % cheaper than in compare because the output
#                     is filtered down to the extremes rather than returned in
#                     full -- the buyer gets less data per market, so pays less.
#                     Full-catalog sweep = $0.020 + 33 x $0.004 = $0.152, i.e.
#                     one call replaces 34 snapshot calls that would cost $0.34.
#
# Every base price is >= MIN_PRICE_MICRO_USD, which _validate() enforces.
DEFAULT_PRICES: dict[str, RoutePrice] = {
    "snapshot": RoutePrice(
        route="snapshot",
        base_micro_usd=10_000,
        per_extra_market_micro_usd=0,
        description="Latest COT positioning snapshot for one market",
    ),
    "history": RoutePrice(
        route="history",
        base_micro_usd=30_000,
        per_extra_market_micro_usd=0,
        description="Enriched weekly COT history for one market",
    ),
    "compare": RoutePrice(
        route="compare",
        base_micro_usd=10_000,
        per_extra_market_micro_usd=8_000,
        description="Cross-market COT comparison with consensus and dispersion",
    ),
    "extremes": RoutePrice(
        route="extremes",
        base_micro_usd=20_000,
        per_extra_market_micro_usd=4_000,
        description="Positioning-extreme screener across a group of markets",
    ),
}

# Optional operator override so prices are tunable without a code change
# (CLAUDE.md: read thresholds from config, keep them learnable over time).
# Shape: {"snapshot": {"base_micro_usd": 12000, "per_extra_market_micro_usd": 0}}
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "pricing.json"


def _validate(table: dict[str, RoutePrice]) -> dict[str, RoutePrice]:
    for name, price in table.items():
        if price.base_micro_usd < MIN_PRICE_MICRO_USD:
            raise PricingError(
                f"Route {name!r} is priced at {price.base_micro_usd} atomic units "
                f"(${price.base_micro_usd / MICRO_PER_USD:.4f}), below the "
                f"${MIN_PRICE_MICRO_USD / MICRO_PER_USD:.2f} floor. At that price the "
                f"flat ${FEE_PER_SETTLEMENT_MICRO_USD / MICRO_PER_USD:.3f} facilitator "
                f"fee would take "
                f"{FEE_PER_SETTLEMENT_MICRO_USD / price.base_micro_usd * 100:.0f} % of "
                "revenue. Raise the price or switch to the batch-settlement scheme."
            )
        if price.per_extra_market_micro_usd < 0:
            raise PricingError(f"Route {name!r} has a negative marginal price.")
    return table


def load_prices(config_path: Path | None = None) -> dict[str, RoutePrice]:
    """Defaults, overlaid with config/pricing.json when present, then validated.

    A malformed or economically invalid override is a hard error: silently
    falling back to defaults would mean the operator thinks they changed a price
    and did not.
    """
    path = CONFIG_PATH if config_path is None else config_path
    table = dict(DEFAULT_PRICES)
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise PricingError(f"{path} is not valid JSON: {exc}") from exc
        for name, override in (raw or {}).items():
            if name not in table:
                raise PricingError(
                    f"{path} prices unknown route {name!r}. "
                    f"Known routes: {', '.join(sorted(table))}."
                )
            current = table[name]
            table[name] = RoutePrice(
                route=name,
                base_micro_usd=int(
                    override.get("base_micro_usd", current.base_micro_usd)
                ),
                per_extra_market_micro_usd=int(
                    override.get(
                        "per_extra_market_micro_usd", current.per_extra_market_micro_usd
                    )
                ),
                description=str(override.get("description", current.description)),
            )
    return _validate(table)


def format_usd(micro_usd: int) -> str:
    """Human/manifest-facing dollar string, e.g. 10000 -> '$0.010'."""
    return f"${micro_usd / MICRO_PER_USD:.3f}"


def fee_share_pct(micro_usd: int) -> float:
    """What share of this ticket the flat facilitator fee would take, in percent.

    Reported on /health so the operator can see the margin degrade if they ever
    override prices downward, instead of discovering it on a statement.
    """
    if micro_usd <= 0:
        raise PricingError("Price must be positive.")
    return round(FEE_PER_SETTLEMENT_MICRO_USD / micro_usd * 100.0, 2)
