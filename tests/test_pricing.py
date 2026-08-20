"""The price table and the fee arithmetic that justifies it."""

from __future__ import annotations

import json

import pytest

from x402api.pricing import (
    DEFAULT_PRICES,
    FEE_PER_SETTLEMENT_MICRO_USD,
    MICRO_PER_USD,
    MIN_PRICE_MICRO_USD,
    PricingError,
    RoutePrice,
    fee_share_pct,
    format_usd,
    load_prices,
)


def test_floor_is_the_one_derived_in_the_docstring():
    # $0.01 with a flat $0.001 fee == exactly 10% of the ticket. That equality
    # is the whole reason the floor sits where it does.
    assert MIN_PRICE_MICRO_USD == 10_000
    assert fee_share_pct(MIN_PRICE_MICRO_USD) == 10.0


def test_every_default_price_clears_the_floor(prices):
    for name, price in prices.items():
        assert price.base_micro_usd >= MIN_PRICE_MICRO_USD, name


def test_defaults_beat_the_apify_alternative(prices):
    # The vault note's whole argument for building this instead of leaning on
    # Apify is margin: Apify takes a flat 20%. Every route here must take less.
    for name, price in prices.items():
        assert fee_share_pct(price.base_micro_usd) < 20.0, name


def test_the_price_that_would_have_been_wrong():
    # $0.004 is what the Apify actor charges per result. At that price the flat
    # facilitator fee is 25% -- worse than Apify. This is the trap the floor exists
    # to catch, so assert the arithmetic explicitly.
    assert fee_share_pct(4_000) == 25.0


def test_quote_scales_with_market_count(prices):
    compare = prices["compare"]
    assert compare.quote(1) == compare.base_micro_usd
    assert compare.quote(3) == compare.base_micro_usd + 2 * compare.per_extra_market_micro_usd
    # 3 markets on the documented defaults = $0.026
    assert compare.quote(3) == 26_000


def test_quote_never_goes_below_base_for_odd_counts(prices):
    for name, price in prices.items():
        assert price.quote(0) == price.base_micro_usd, name
        assert price.quote(-5) == price.base_micro_usd, name


def test_full_catalog_sweep_is_cheaper_than_buying_snapshots(prices):
    # The extremes route's volume discount has to actually be a discount, or
    # nobody would call it instead of looping snapshot.
    catalog_size = 34
    sweep = prices["extremes"].quote(catalog_size)
    piecemeal = prices["snapshot"].base_micro_usd * catalog_size
    assert sweep < piecemeal
    assert sweep == 20_000 + 33 * 4_000  # $0.152


def test_compare_marginal_market_undercuts_a_separate_snapshot(prices):
    assert prices["compare"].per_extra_market_micro_usd < prices["snapshot"].base_micro_usd


def test_override_below_the_floor_is_rejected(tmp_path):
    path = tmp_path / "pricing.json"
    path.write_text(json.dumps({"snapshot": {"base_micro_usd": 1_000}}), encoding="utf-8")
    with pytest.raises(PricingError) as exc:
        load_prices(path)
    # The message must carry the arithmetic, not just say "no".
    assert "100 % of revenue" in str(exc.value) or "100 %" in str(exc.value)


def test_override_above_the_floor_is_applied(tmp_path):
    path = tmp_path / "pricing.json"
    path.write_text(json.dumps({"snapshot": {"base_micro_usd": 25_000}}), encoding="utf-8")
    table = load_prices(path)
    assert table["snapshot"].base_micro_usd == 25_000
    # Untouched routes keep their defaults.
    assert table["history"].base_micro_usd == DEFAULT_PRICES["history"].base_micro_usd


def test_override_of_unknown_route_is_rejected(tmp_path):
    path = tmp_path / "pricing.json"
    path.write_text(json.dumps({"nonsense": {"base_micro_usd": 50_000}}), encoding="utf-8")
    with pytest.raises(PricingError, match="unknown route"):
        load_prices(path)


def test_malformed_override_is_rejected_not_ignored(tmp_path):
    path = tmp_path / "pricing.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(PricingError, match="not valid JSON"):
        load_prices(path)


def test_missing_override_file_falls_back_to_defaults(tmp_path):
    table = load_prices(tmp_path / "does-not-exist.json")
    assert table == DEFAULT_PRICES


def test_negative_marginal_price_is_rejected():
    from x402api.pricing import _validate

    with pytest.raises(PricingError, match="negative"):
        _validate(
            {
                "x": RoutePrice(
                    route="x",
                    base_micro_usd=10_000,
                    per_extra_market_micro_usd=-1,
                    description="",
                )
            }
        )


def test_format_usd():
    assert format_usd(10_000) == "$0.010"
    assert format_usd(MICRO_PER_USD) == "$1.000"
    assert format_usd(0) == "$0.000"


def test_fee_share_rejects_nonpositive_price():
    with pytest.raises(PricingError):
        fee_share_pct(0)


def test_fee_constant_matches_the_verified_figure():
    # $0.001 per settlement after the first 1000/month (CDP, verified 2026-08-17).
    assert FEE_PER_SETTLEMENT_MICRO_USD == 1_000
