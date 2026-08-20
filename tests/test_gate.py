"""The payment gate: challenge, local checks, verify, settle, journal."""

from __future__ import annotations

import pytest

from x402api.payments.facilitator import (
    FacilitatorError,
    MockFacilitator,
    SettleResult,
    VerifyResult,
)
from x402api.payments.gate import Authorized, Challenge, PaymentGate, Settled
from x402api.payments.types import b64_decode_json, b64_encode_json

from conftest import FAKE_PAY_TO, payment_headers, signed_payload


def reqs_for(gate: PaymentGate, route="snapshot", count=1):
    return gate.requirements_for(
        route=route, market_count=count, resource_url="https://api.example.test/v1/x"
    )


# --- quoting -----------------------------------------------------------------


def test_requirements_carry_the_configured_paytoand_asset(gate, settings):
    r = reqs_for(gate)
    assert r.pay_to == settings.pay_to
    assert r.asset == settings.network.usdc_address
    assert r.network_v1 == "base-sepolia"
    assert r.network_v2 == "eip155:84532"


def test_requirements_carry_the_token_eip712_domain(gate):
    # Wrong domain name == every signature fails verification. It differs
    # between mainnet ("USD Coin") and Sepolia ("USDC").
    assert reqs_for(gate).extra == {"name": "USDC", "version": "2"}


def test_mainnet_requirements_use_the_mainnet_domain_name(
    mainnet_settings, facilitator, tax_log, prices
):
    g = PaymentGate(
        settings=mainnet_settings, facilitator=facilitator, tax_log=tax_log, prices=prices
    )
    r = reqs_for(g)
    assert r.extra["name"] == "USD Coin"
    assert r.asset == "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def test_quote_scales_with_market_count(gate):
    assert reqs_for(gate, "compare", 1).amount_atomic == 10_000
    assert reqs_for(gate, "compare", 3).amount_atomic == 26_000


def test_unpriced_route_raises(gate):
    with pytest.raises(KeyError, match="no price"):
        reqs_for(gate, "nonexistent")


# --- challenge ---------------------------------------------------------------


def test_unpaid_request_gets_a_challenge_readable_by_both_versions(gate):
    r = reqs_for(gate)
    result = gate.authorize({}, r)
    assert isinstance(result, Challenge)
    assert result.status_code == 402
    # v1 clients read the body...
    assert result.body["x402Version"] == 1
    assert result.body["accepts"][0]["maxAmountRequired"] == "10000"
    # ...v2 clients read the header.
    v2 = b64_decode_json(result.headers["PAYMENT-REQUIRED"])
    assert v2["x402Version"] == 2
    assert v2["accepts"][0]["amount"] == "10000"


def test_challenge_exposes_the_settlement_headers_to_browsers(gate):
    headers = gate.authorize({}, reqs_for(gate)).headers
    assert "PAYMENT-RESPONSE" in headers["Access-Control-Expose-Headers"]
    assert "X-PAYMENT-RESPONSE" in headers["Access-Control-Expose-Headers"]


def test_challenge_message_names_both_header_options(gate):
    body = gate.authorize({}, reqs_for(gate)).body
    assert "PAYMENT-SIGNATURE" in body["error"] and "X-PAYMENT" in body["error"]


def test_malformed_header_is_a_challenge_not_a_crash(gate):
    result = gate.authorize({"X-PAYMENT": "@@@not-base64@@@"}, reqs_for(gate))
    assert isinstance(result, Challenge)
    assert "base64" in result.reason or "JSON" in result.reason


# --- local checks ------------------------------------------------------------


def test_underpayment_is_rejected_without_calling_the_facilitator(gate, facilitator):
    r = reqs_for(gate)
    result = gate.authorize(payment_headers(version=1, amount=9_999), r)
    assert isinstance(result, Challenge)
    assert "9999" in result.reason and "10000" in result.reason
    assert facilitator.call_names == []  # no round trip wasted


def test_overpayment_is_accepted(gate):
    result = gate.authorize(payment_headers(version=1, amount=50_000), reqs_for(gate))
    assert isinstance(result, Authorized)


def test_wrong_network_is_rejected_locally(gate, facilitator):
    result = gate.authorize(
        payment_headers(version=1, amount=10_000, network="base"), reqs_for(gate)
    )
    assert isinstance(result, Challenge)
    assert "base-sepolia" in result.reason
    assert facilitator.call_names == []


def test_wrong_scheme_is_rejected_locally(gate, facilitator):
    result = gate.authorize(
        payment_headers(version=1, amount=10_000, scheme="upto"), reqs_for(gate)
    )
    assert isinstance(result, Challenge)
    assert "upto" in result.reason
    assert facilitator.call_names == []


def test_payment_to_a_different_recipient_is_rejected(gate, facilitator):
    result = gate.authorize(
        payment_headers(version=1, amount=10_000, pay_to="0x" + "99" * 20),
        reqs_for(gate),
    )
    assert isinstance(result, Challenge)
    assert "different recipient" in result.reason
    assert facilitator.call_names == []


def test_recipient_check_is_case_insensitive(gate):
    # EVM addresses differ only by checksum casing; rejecting on case would
    # break every correctly-signed payment from a client that lowercases.
    result = gate.authorize(
        payment_headers(version=1, amount=10_000, pay_to=FAKE_PAY_TO.upper()),
        reqs_for(gate),
    )
    assert isinstance(result, Authorized)


def test_missing_value_is_rejected(gate):
    payload = signed_payload(version=1, amount=10_000)
    del payload["payload"]["authorization"]["value"]
    result = gate.authorize({"X-PAYMENT": b64_encode_json(payload)}, reqs_for(gate))
    assert isinstance(result, Challenge)
    assert "numeric 'value'" in result.reason


def test_v2_payload_is_checked_against_the_caip2_network(gate):
    assert isinstance(
        gate.authorize(payment_headers(version=2, amount=10_000), reqs_for(gate)),
        Authorized,
    )


# --- verification ------------------------------------------------------------


def test_valid_payment_is_authorized(gate, facilitator):
    result = gate.authorize(payment_headers(version=1, amount=10_000), reqs_for(gate))
    assert isinstance(result, Authorized)
    assert facilitator.call_names == ["verify"]  # verify only -- no settle yet


def test_facilitator_rejection_becomes_a_challenge(settings, tax_log, prices):
    mock = MockFacilitator(
        verify_result=VerifyResult(is_valid=False, invalid_reason="insufficient_funds")
    )
    g = PaymentGate(settings=settings, facilitator=mock, tax_log=tax_log, prices=prices)
    result = g.authorize(payment_headers(version=1, amount=10_000), reqs_for(g))
    assert isinstance(result, Challenge)
    assert "insufficient_funds" in result.reason


def test_facilitator_outage_withholds_the_data(settings, tax_log, prices):
    mock = MockFacilitator(verify_error=FacilitatorError("connection refused"))
    g = PaymentGate(settings=settings, facilitator=mock, tax_log=tax_log, prices=prices)
    result = g.authorize(payment_headers(version=1, amount=10_000), reqs_for(g))
    assert isinstance(result, Challenge)
    assert "verification unavailable" in result.reason


# --- settlement --------------------------------------------------------------


def test_settlement_returns_the_response_header_for_the_right_version(gate):
    for version, header in ((1, "X-PAYMENT-RESPONSE"), (2, "PAYMENT-RESPONSE")):
        auth = gate.authorize(payment_headers(version=version, amount=10_000), reqs_for(gate))
        settled = gate.settle(auth, route="snapshot")
        assert isinstance(settled, Settled)
        assert header in settled.headers
        decoded = b64_decode_json(settled.headers[header])
        assert decoded["success"] is True
        assert decoded["transaction"].startswith("0x")


def test_settlement_writes_exactly_one_journal_line(gate, tax_log):
    auth = gate.authorize(payment_headers(version=1, amount=10_000), reqs_for(gate))
    gate.settle(auth, route="snapshot")
    rows = tax_log.read_all()
    assert len(rows) == 1
    assert rows[0]["route"] == "snapshot"
    assert rows[0]["amountAtomic"] == "10000"
    assert rows[0]["transactionHash"].startswith("0x")
    assert rows[0]["x402Version"] == 1


def test_journal_records_the_quoted_price_not_the_authorized_amount(gate, tax_log):
    # A client may sign for more than the quote. The books must show what was
    # actually charged, or the EUR totals will not reconcile.
    auth = gate.authorize(payment_headers(version=1, amount=99_000), reqs_for(gate))
    gate.settle(auth, route="snapshot")
    assert tax_log.read_all()[0]["amountAtomic"] == "10000"


def test_failed_settlement_withholds_data_and_writes_nothing(
    settings, tax_log, prices
):
    mock = MockFacilitator(
        settle_result=SettleResult(
            success=False, error_reason="insufficient_funds", transaction="", network="base-sepolia"
        )
    )
    g = PaymentGate(settings=settings, facilitator=mock, tax_log=tax_log, prices=prices)
    auth = g.authorize(payment_headers(version=1, amount=10_000), reqs_for(g))
    result = g.settle(auth, route="snapshot")
    assert isinstance(result, Challenge)
    assert "insufficient_funds" in result.reason
    assert tax_log.read_all() == []


def test_settlement_outage_is_a_challenge_not_a_free_lunch(settings, tax_log, prices):
    mock = MockFacilitator(settle_error=FacilitatorError("timeout"))
    g = PaymentGate(settings=settings, facilitator=mock, tax_log=tax_log, prices=prices)
    auth = g.authorize(payment_headers(version=1, amount=10_000), reqs_for(g))
    result = g.settle(auth, route="snapshot")
    assert isinstance(result, Challenge)
    assert tax_log.read_all() == []


def test_verify_then_settle_in_that_order(gate, facilitator):
    auth = gate.authorize(payment_headers(version=1, amount=10_000), reqs_for(gate))
    gate.settle(auth, route="snapshot")
    assert facilitator.call_names == ["verify", "settle"]


def test_settle_is_never_reached_when_verify_fails(settings, tax_log, prices):
    mock = MockFacilitator(verify_result=VerifyResult(is_valid=False, invalid_reason="x"))
    g = PaymentGate(settings=settings, facilitator=mock, tax_log=tax_log, prices=prices)
    g.authorize(payment_headers(version=1, amount=10_000), reqs_for(g))
    assert "settle" not in mock.call_names
