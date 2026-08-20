"""Live contract tests against the real CFTC API. Run with: pytest -m live

Excluded from the default run (pytest.ini) so the suite stays offline and fast,
but they are the evidence that the product actually works end to end rather
than only against fixtures. They catch the one failure mode fixtures cannot:
the CFTC renaming a column.

The facilitator is still mocked -- no real payment is ever made here.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from x402api.app import create_app
from x402api.cot_source import CotSource

from conftest import payment_headers

pytestmark = pytest.mark.live


@pytest.fixture
def live_client(settings, facilitator, tax_log, prices):
    app = create_app(
        settings=settings,
        facilitator=facilitator,
        tax_log=tax_log,
        source=CotSource(ttl_seconds=3600),
        prices=prices,
    )
    return TestClient(app)


def test_gold_snapshot_against_the_real_cftc_api(live_client):
    r = live_client.get(
        "/v1/cot/snapshot?market=GOLD", headers=payment_headers(version=1, amount=10_000)
    )
    assert r.status_code == 200
    record = r.json()["record"]
    assert record["cftcContractCode"] == "088691"
    assert record["reportType"] == "disaggregated"
    assert record["primaryGroup"] == "managedMoney"
    # The derived metrics are what is being sold; none of them may be null on a
    # market with decades of history.
    assert record["net"] == record["groups"]["managedMoney"]["net"]
    assert record["cotIndex"] is not None
    assert 0 <= record["cotIndex"] <= 100
    assert record["netZScore"] is not None
    assert record["openInterest"] > 0
    assert record["reportDate"].startswith("20")


def test_euro_fx_uses_the_tff_report_and_leveraged_money(live_client):
    r = live_client.get(
        "/v1/cot/history?market=EURUSD&weeks=4", headers=payment_headers(version=1, amount=30_000)
    )
    assert r.status_code == 200
    body = r.json()
    assert body["weeksReturned"] == 4
    first = body["records"][0]
    assert first["reportType"] == "tff"
    assert first["primaryGroup"] == "leveragedMoney"
    # Newest first, strictly descending.
    dates = [rec["reportDate"] for rec in body["records"]]
    assert dates == sorted(dates, reverse=True)


def test_cross_market_dollar_score_computes_on_real_data(live_client):
    r = live_client.get(
        "/v1/cot/compare?markets=EURUSD,GBPUSD,JPY,DXY",
        headers=payment_headers(version=1, amount=10_000 + 3 * 8_000),
    )
    assert r.status_code == 200
    cross = r.json()["crossMarket"]
    assert cross["dollar"]["contributingMarkets"] == 4
    assert cross["dollar"]["bias"] in ("long_usd", "short_usd", "neutral")
    assert 0 <= cross["dollar"]["dollarCotIndex"] <= 100
    assert cross["consensus"]["regime"] in ("aligned", "mixed", "divided", "insufficient_data")
    assert len(cross["ranking"]) == 4


def test_metals_screener_on_real_data(live_client):
    r = live_client.get(
        "/v1/cot/extremes?group=metals", headers=payment_headers(version=1, amount=20_000 + 4 * 4_000)
    )
    assert r.status_code == 200
    body = r.json()
    assert body["marketsScanned"] >= 4
    assert body["errors"] == []
    assert body["latestReportDate"].startswith("20")


def test_full_catalog_sweep_has_no_dead_markets(live_client):
    """Every curated symbol must still resolve against the live CFTC datasets."""
    r = live_client.get(
        "/v1/cot/extremes?group=all", headers=payment_headers(version=1, amount=20_000 + 33 * 4_000)
    )
    assert r.status_code == 200
    body = r.json()
    assert body["marketsScanned"] >= 30, f"errors: {body['errors']}"


def test_settlements_are_journalled_with_eur_values(live_client, tax_log):
    live_client.get(
        "/v1/cot/snapshot?market=GOLD", headers=payment_headers(version=1, amount=10_000)
    )
    rows = tax_log.read_all()
    assert len(rows) == 1
    assert rows[0]["amountUsd"] == 0.01
    assert rows[0]["amountEur"] > 0
    # Until a real FX feed is wired in, every line must admit it is a placeholder.
    assert rows[0]["fxRateIsPlaceholder"] is True
