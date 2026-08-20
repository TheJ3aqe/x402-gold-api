"""Shared fixtures. Nothing here touches the network or a real facilitator."""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from x402api.config import NETWORKS, Settings  # noqa: E402
from x402api.cot_source import CotSource  # noqa: E402
from x402api.payments.facilitator import MockFacilitator  # noqa: E402
from x402api.payments.gate import PaymentGate  # noqa: E402
from x402api.payments.types import b64_encode_json  # noqa: E402
from x402api.pricing import load_prices  # noqa: E402
from x402api.taxlog import TaxLog  # noqa: E402

# Obvious throwaway addresses. Never a real wallet -- CLAUDE.md forbids one in
# the tree, and a checksummed-looking fake would be worse because someone might
# believe it.
FAKE_PAY_TO = "0x" + "de" * 20
FAKE_PAYER = "0x" + "11" * 20
FAKE_TX = "0x" + "ab" * 32


# --- synthetic CFTC rows -----------------------------------------------------


def make_row(
    report_date: str,
    *,
    nc_long: int,
    nc_short: int,
    open_interest: int = 400_000,
) -> dict:
    """One weekly CFTC row carrying all three report families' columns.

    Mirrors the CFTC's real column-name typos ("noncomm_postions_spread_all",
    "swap__positions_short_all") because the shared analytics layer matches on
    them verbatim -- a test using tidied-up names would pass while production
    silently read None.
    """
    return {
        "report_date_as_yyyy_mm_dd": f"{report_date}T00:00:00.000",
        "yyyy_report_week_ww": "2026 Report Week 1",
        "market_and_exchange_names": "TEST MARKET - TEST EXCHANGE",
        "cftc_contract_market_code": "088691",
        "open_interest_all": str(open_interest),
        "change_in_open_interest_all": "100",
        # legacy
        "noncomm_positions_long_all": str(nc_long),
        "noncomm_positions_short_all": str(nc_short),
        "noncomm_postions_spread_all": "500",
        "comm_positions_long_all": "40000",
        "comm_positions_short_all": "55000",
        "nonrept_positions_long_all": "1000",
        "nonrept_positions_short_all": "900",
        "traders_noncomm_long_all": "120",
        "traders_noncomm_short_all": "80",
        "traders_comm_long_all": "50",
        "traders_comm_short_all": "60",
        # disaggregated
        "prod_merc_positions_long": "30000",
        "prod_merc_positions_short": "45000",
        "swap_positions_long_all": "12000",
        "swap__positions_short_all": "9000",
        "m_money_positions_long_all": str(nc_long),
        "m_money_positions_short_all": str(nc_short),
        "m_money_positions_spread": "700",
        "traders_m_money_long_all": "90",
        "traders_m_money_short_all": "40",
        "other_rept_positions_long": "5000",
        "other_rept_positions_short": "4000",
        "other_rept_positions_spread": "300",
        # tff
        "dealer_positions_long_all": "20000",
        "dealer_positions_short_all": "35000",
        "dealer_positions_spread_all": "1000",
        "asset_mgr_positions_long": "50000",
        "asset_mgr_positions_short": "12000",
        "asset_mgr_positions_spread": "800",
        "lev_money_positions_long": str(nc_long),
        "lev_money_positions_short": str(nc_short),
        "lev_money_positions_spread": "600",
        "traders_lev_money_long_all": "70",
        "traders_lev_money_short_all": "45",
    }


def make_history(weeks: int = 200, *, base_long: int = 140_000, trend: int = 250) -> list[dict]:
    """`weeks` rows, newest first, with a steadily rising net so the COT Index
    of the newest week is at the top of its range (a deterministic extreme)."""
    start = date(2026, 8, 11)
    rows = []
    for i in range(weeks):
        rows.append(
            make_row(
                (start - timedelta(weeks=i)).isoformat(),
                nc_long=base_long - i * trend,
                nc_short=10_000,
            )
        )
    return rows


class FakeCftcClient:
    """Stands in for CftcClient. Records calls, can be told to fail."""

    def __init__(self, rows: list[dict] | None = None, error: Exception | None = None):
        self.rows = rows if rows is not None else make_history()
        self.error = error
        self.calls: list[tuple] = []

    def fetch_history(self, contract_code, report="legacy", combined=False, limit=200):
        self.calls.append((contract_code, report, combined, limit))
        if self.error:
            raise self.error
        return self.rows[:limit]

    def latest_report_date(self, report="legacy"):
        return "2026-08-11"


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def prices():
    return load_prices()


@pytest.fixture
def settings():
    return Settings(
        network=NETWORKS["base-sepolia"],
        facilitator_url="https://x402.org/facilitator",
        pay_to=FAKE_PAY_TO,
        base_url="https://api.example.test",
        payment_timeout_seconds=60,
        upstream_cache_ttl_seconds=3600,
    )


@pytest.fixture
def mainnet_settings():
    return Settings(
        network=NETWORKS["base"],
        facilitator_url="https://api.cdp.coinbase.com/platform/v2/x402",
        pay_to=FAKE_PAY_TO,
        base_url="https://api.example.test",
    )


@pytest.fixture
def tax_log(tmp_path):
    return TaxLog(path=tmp_path / "settlements.jsonl")


@pytest.fixture
def facilitator():
    return MockFacilitator()


@pytest.fixture
def gate(settings, facilitator, tax_log, prices):
    return PaymentGate(
        settings=settings, facilitator=facilitator, tax_log=tax_log, prices=prices
    )


@pytest.fixture
def fake_source():
    return CotSource(client=FakeCftcClient())


def signed_payload(
    *,
    version: int,
    amount: int,
    pay_to: str = FAKE_PAY_TO,
    network: str | None = None,
    scheme: str = "exact",
) -> dict:
    """A structurally valid client payload. The signature is a placeholder --
    only the facilitator can judge it, and in tests that is the mock."""
    net = network or ("eip155:84532" if version == 2 else "base-sepolia")
    authorization = {
        "from": FAKE_PAYER,
        "to": pay_to,
        "value": str(amount),
        "validAfter": "1740672089",
        "validBefore": "1740672154",
        "nonce": "0x" + "f3" * 32,
    }
    inner = {"signature": "0x" + "cd" * 65, "authorization": authorization}
    if version == 1:
        return {
            "x402Version": 1,
            "scheme": scheme,
            "network": net,
            "payload": inner,
        }
    return {
        "x402Version": 2,
        "resource": {"url": "https://api.example.test/v1/cot/snapshot"},
        "accepted": {"scheme": scheme, "network": net},
        "payload": inner,
    }


def payment_headers(*, version: int, amount: int, **kwargs) -> dict[str, str]:
    payload = signed_payload(version=version, amount=amount, **kwargs)
    name = "PAYMENT-SIGNATURE" if version == 2 else "X-PAYMENT"
    return {name: b64_encode_json(payload)}
