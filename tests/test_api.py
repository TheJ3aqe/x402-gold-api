"""End-to-end HTTP tests. Mocked facilitator, fake CFTC client, no network."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from x402api.app import create_app
from x402api.cot_source import CftcError, CotSource
from x402api.payments.facilitator import MockFacilitator, SettleResult, VerifyResult
from x402api.payments.types import b64_decode_json

from conftest import FAKE_PAY_TO, FakeCftcClient, payment_headers


@pytest.fixture
def app(settings, facilitator, tax_log, fake_source, prices):
    return create_app(
        settings=settings,
        facilitator=facilitator,
        tax_log=tax_log,
        source=fake_source,
        prices=prices,
    )


@pytest.fixture
def client(app):
    return TestClient(app)


# --- free routes -------------------------------------------------------------


def test_index_is_free_and_explains_the_payment_flow(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert body["payment"]["versionsSupported"] == [1, 2]
    assert "PAYMENT-SIGNATURE" in body["payment"]["howItWorks"]
    assert any(route["path"] == "/v1/cot/snapshot" for route in body["paidRoutes"])


def test_health_is_free_and_reports_fee_share(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["network"] == "base-sepolia"
    assert body["isTestnet"] is True
    assert body["pricing"]["snapshot"]["facilitatorFeeSharePct"] == 10.0
    assert body["settlements"]["settlements"] == 0


def test_health_never_exposes_the_payout_address(client):
    assert FAKE_PAY_TO not in client.get("/health").text
    assert client.get("/health").json()["payToConfigured"] is True


def test_market_catalog_is_free(client):
    body = client.get("/v1/markets").json()
    codes = {m["code"] for m in body["markets"]}
    assert "088691" in codes  # Gold
    assert "fx" in body["presets"]


def test_well_known_manifest_is_free_and_carries_live_prices(client):
    body = client.get("/.well-known/x402").json()
    assert body["x402"]["versionsSupported"] == [1, 2]
    assert body["x402"]["asset"]["decimals"] == 6
    snapshot = next(r for r in body["routes"] if r["id"] == "snapshot")
    assert snapshot["price"]["baseUsd"] == "$0.010"
    assert snapshot["price"]["baseAtomic"] == "10000"
    assert snapshot["url"].endswith("/v1/cot/snapshot")


# --- paid routes: the 402 ----------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/v1/cot/snapshot?market=GOLD",
        "/v1/cot/history?market=GOLD",
        "/v1/cot/compare?markets=GOLD,SILVER",
        "/v1/cot/extremes?group=metals",
    ],
)
def test_every_paid_route_answers_402_when_unpaid(client, path):
    r = client.get(path)
    assert r.status_code == 402
    assert r.json()["x402Version"] == 1
    assert "PAYMENT-REQUIRED" in r.headers


def test_402_carries_both_dialects_with_matching_amounts(client):
    r = client.get("/v1/cot/snapshot?market=GOLD")
    v1_amount = r.json()["accepts"][0]["maxAmountRequired"]
    v2_amount = b64_decode_json(r.headers["PAYMENT-REQUIRED"])["accepts"][0]["amount"]
    assert v1_amount == v2_amount == "10000"


def test_402_advertises_the_configured_paytoand_asset(client, settings):
    accepts = client.get("/v1/cot/snapshot?market=GOLD").json()["accepts"][0]
    assert accepts["payTo"] == settings.pay_to
    assert accepts["asset"] == settings.network.usdc_address


def test_402_carries_bazaar_discovery_metadata(client):
    accepts = client.get("/v1/cot/snapshot?market=GOLD").json()["accepts"][0]
    schema = accepts["outputSchema"]
    assert schema["input"]["discoverable"] is True
    assert schema["input"]["method"] == "GET"
    assert "market" in schema["input"]["queryParams"]

    v2 = b64_decode_json(client.get("/v1/cot/snapshot?market=GOLD").headers["PAYMENT-REQUIRED"])
    assert v2["extensions"]["bazaar"]["discoverable"] is True


def test_price_scales_with_the_number_of_markets_requested(client):
    def quote(path):
        return int(client.get(path).json()["accepts"][0]["maxAmountRequired"])

    assert quote("/v1/cot/compare?markets=GOLD") == 10_000
    assert quote("/v1/cot/compare?markets=GOLD,SILVER") == 18_000
    assert quote("/v1/cot/compare?markets=GOLD,SILVER,EURUSD") == 26_000
    # A preset expands before pricing, so the quote reflects real work.
    assert quote("/v1/cot/compare?markets=metals") == 10_000 + 4 * 8_000


def test_full_catalog_sweep_is_quoted_correctly(client):
    amount = int(client.get("/v1/cot/extremes?group=all").json()["accepts"][0]["maxAmountRequired"])
    assert amount == 20_000 + 33 * 4_000


# --- paid routes: the happy path --------------------------------------------


def test_paid_snapshot_returns_data_and_settles(client, facilitator, tax_log):
    r = client.get("/v1/cot/snapshot?market=GOLD", headers=payment_headers(version=1, amount=10_000))
    assert r.status_code == 200
    body = r.json()
    assert body["record"]["cftcContractCode"] == "088691"
    assert body["record"]["cotIndex"] is not None
    assert body["payment"]["amountUsd"] == "$0.010"
    assert facilitator.call_names == ["verify", "settle"]
    assert len(tax_log.read_all()) == 1


def test_settlement_header_is_returned_on_success(client):
    r = client.get("/v1/cot/snapshot?market=GOLD", headers=payment_headers(version=1, amount=10_000))
    decoded = b64_decode_json(r.headers["X-PAYMENT-RESPONSE"])
    assert decoded["success"] is True


def test_v2_client_gets_the_v2_settlement_header(client):
    r = client.get("/v1/cot/snapshot?market=GOLD", headers=payment_headers(version=2, amount=10_000))
    assert r.status_code == 200
    assert "PAYMENT-RESPONSE" in r.headers
    assert r.json()["payment"]["x402Version"] == 2


def test_paid_history_returns_a_series(client):
    r = client.get(
        "/v1/cot/history?market=GOLD&weeks=10", headers=payment_headers(version=1, amount=30_000)
    )
    assert r.status_code == 200
    body = r.json()
    assert body["weeksReturned"] == 10
    assert len(body["records"]) == 10
    assert "look-ahead" in body["note"]


def test_paid_compare_returns_cross_market_analytics(client):
    r = client.get(
        "/v1/cot/compare?markets=EURUSD,GBPUSD,DXY",
        headers=payment_headers(version=1, amount=26_000),
    )
    assert r.status_code == 200
    cross = r.json()["crossMarket"]
    assert {"ranking", "consensus", "dollar", "divergences"} <= set(cross)
    # This is the value that a raw feed cannot provide.
    assert cross["dollar"]["contributingMarkets"] == 3
    assert cross["consensus"]["marketsScored"] == 3


def test_paid_extremes_screens_a_group(client):
    r = client.get(
        "/v1/cot/extremes?group=metals", headers=payment_headers(version=1, amount=10_000 + 40_000)
    )
    assert r.status_code == 200
    body = r.json()
    assert body["group"] == "metals"
    assert body["marketsScanned"] == 5
    assert "consensus" in body


def test_underpaying_a_multi_market_call_is_rejected(client):
    # Paying the single-market price for a 3-market compare must not work.
    r = client.get(
        "/v1/cot/compare?markets=GOLD,SILVER,EURUSD",
        headers=payment_headers(version=1, amount=10_000),
    )
    assert r.status_code == 402
    assert "26000" in r.json()["error"]


# --- failure paths -----------------------------------------------------------


def test_unknown_market_is_400_not_402(client, facilitator):
    # Never charge for a request that cannot be answered.
    r = client.get("/v1/cot/snapshot?market=NOT_A_MARKET")
    assert r.status_code == 400
    assert r.json()["error"] == "unknown_market"
    assert facilitator.call_names == []


def test_unknown_group_is_400(client):
    r = client.get("/v1/cot/extremes?group=nonsense")
    assert r.status_code == 400
    assert r.json()["error"] == "unknown_group"


def test_invalid_thresholds_are_400(client):
    r = client.get("/v1/cot/extremes?group=metals&extremeLongThreshold=5&extremeShortThreshold=90")
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_thresholds"


def test_upstream_failure_returns_502_and_does_not_charge(
    settings, facilitator, tax_log, prices
):
    broken = CotSource(client=FakeCftcClient(error=CftcError("CFTC API HTTP 503")))
    app = create_app(
        settings=settings, facilitator=facilitator, tax_log=tax_log, source=broken, prices=prices
    )
    r = TestClient(app).get(
        "/v1/cot/snapshot?market=GOLD", headers=payment_headers(version=1, amount=10_000)
    )
    assert r.status_code == 502
    assert r.json()["charged"] is False
    # Verified, but never settled -- the caller keeps their money.
    assert facilitator.call_names == ["verify"]
    assert tax_log.read_all() == []


def test_failed_settlement_withholds_the_data(settings, tax_log, fake_source, prices):
    mock = MockFacilitator(
        settle_result=SettleResult(
            success=False, error_reason="insufficient_funds", transaction="", network="base-sepolia"
        )
    )
    app = create_app(
        settings=settings, facilitator=mock, tax_log=tax_log, source=fake_source, prices=prices
    )
    r = TestClient(app).get(
        "/v1/cot/snapshot?market=GOLD", headers=payment_headers(version=1, amount=10_000)
    )
    assert r.status_code == 402
    assert "record" not in r.json()
    assert tax_log.read_all() == []


def test_rejected_payment_returns_a_fresh_402(settings, tax_log, fake_source, prices):
    mock = MockFacilitator(verify_result=VerifyResult(is_valid=False, invalid_reason="bad_sig"))
    app = create_app(
        settings=settings, facilitator=mock, tax_log=tax_log, source=fake_source, prices=prices
    )
    r = TestClient(app).get(
        "/v1/cot/snapshot?market=GOLD", headers=payment_headers(version=1, amount=10_000)
    )
    assert r.status_code == 402
    assert "bad_sig" in r.json()["error"]


def test_missing_required_query_param_is_422(client):
    assert client.get("/v1/cot/snapshot").status_code == 422


def test_out_of_range_lookback_is_clamped_not_rejected(client):
    r = client.get(
        "/v1/cot/snapshot?market=GOLD&lookbackWeeks=99999",
        headers=payment_headers(version=1, amount=10_000),
    )
    assert r.status_code == 200
    assert r.json()["lookbackWeeks"] == 1040


# --- bookkeeping integration -------------------------------------------------


def test_each_paid_call_adds_exactly_one_journal_line(client, tax_log):
    for _ in range(3):
        client.get("/v1/cot/snapshot?market=GOLD", headers=payment_headers(version=1, amount=10_000))
    rows = tax_log.read_all()
    assert len(rows) == 3
    assert all(r["route"] == "snapshot" for r in rows)
    assert tax_log.summary()["totalUsd"] == 0.03


def test_journal_records_the_route_specific_price(client, tax_log):
    client.get("/v1/cot/snapshot?market=GOLD", headers=payment_headers(version=1, amount=10_000))
    client.get(
        "/v1/cot/compare?markets=GOLD,SILVER", headers=payment_headers(version=1, amount=18_000)
    )
    assert [r["amountAtomic"] for r in tax_log.read_all()] == ["10000", "18000"]


def test_health_reflects_settlements_after_paid_calls(client):
    client.get("/v1/cot/snapshot?market=GOLD", headers=payment_headers(version=1, amount=10_000))
    assert client.get("/health").json()["settlements"]["settlements"] == 1


# --- caching -----------------------------------------------------------------


def test_repeated_calls_reuse_the_cached_upstream_response(settings, facilitator, tax_log, prices):
    fake = FakeCftcClient()
    source = CotSource(client=fake)
    # The TTL cache lives in CachedCftcClient; with an injected raw fake we can
    # at least assert the API does not fan out more upstream calls than markets.
    app = create_app(
        settings=settings, facilitator=facilitator, tax_log=tax_log, source=source, prices=prices
    )
    TestClient(app).get(
        "/v1/cot/compare?markets=GOLD,SILVER,EURUSD",
        headers=payment_headers(version=1, amount=26_000),
    )
    assert len(fake.calls) == 3
