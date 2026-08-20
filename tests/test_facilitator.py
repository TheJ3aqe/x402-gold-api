"""Facilitator client: request shape, response parsing, and the CDP key gate."""

from __future__ import annotations

import pytest

from x402api.config import ENV_CDP_KEY_ID, ENV_CDP_KEY_SECRET
from x402api.payments.facilitator import (
    CdpAuth,
    FacilitatorError,
    FacilitatorNotConfigured,
    HttpFacilitator,
    MockFacilitator,
    SettleResult,
    VerifyResult,
    _request_body,
    build_facilitator,
)
from x402api.payments.types import parse_payment_header

from conftest import FAKE_PAY_TO, payment_headers
from test_wire import reqs


def payload(version: int = 1, amount: int = 10_000):
    return parse_payment_header(payment_headers(version=version, amount=amount))


# --- request shape -----------------------------------------------------------


def test_request_body_shape_matches_the_reference_client():
    body = _request_body(payload(1), reqs())
    assert set(body) == {"x402Version", "paymentPayload", "paymentRequirements"}
    assert body["x402Version"] == 1
    # The version is deliberately duplicated: top level AND inside the payload.
    assert body["paymentPayload"]["x402Version"] == 1
    assert body["paymentRequirements"]["maxAmountRequired"] == "10000"


def test_request_body_uses_the_payload_version_for_the_requirements_shape():
    body = _request_body(payload(2), reqs())
    assert body["x402Version"] == 2
    # v2 requirements use `amount`, not `maxAmountRequired`
    assert body["paymentRequirements"]["amount"] == "10000"
    assert body["paymentRequirements"]["network"] == "eip155:84532"


def test_requirements_sent_are_a_single_object_not_the_accepts_array():
    assert isinstance(_request_body(payload(), reqs())["paymentRequirements"], dict)


# --- response parsing --------------------------------------------------------


def test_verify_result_parsing():
    ok = VerifyResult.from_wire({"isValid": True, "payer": "0xabc"})
    assert ok.is_valid and ok.payer == "0xabc" and ok.invalid_reason is None

    bad = VerifyResult.from_wire({"isValid": False, "invalidReason": "insufficient_funds"})
    assert not bad.is_valid and bad.invalid_reason == "insufficient_funds"


def test_verify_result_treats_a_missing_flag_as_invalid():
    # Fail closed: an unparseable answer must never read as "paid".
    assert VerifyResult.from_wire({}).is_valid is False


def test_settle_result_parsing_success():
    r = SettleResult.from_wire(
        {"success": True, "transaction": "0x123", "network": "base-sepolia", "payer": "0xa"}
    )
    assert r.success and r.transaction == "0x123" and r.network == "base-sepolia"


def test_settle_result_handles_empty_string_transaction_on_failure():
    # The spec uses "" on failure, not null. Reading it as null would crash the
    # journal writer, which is the one place that must not crash.
    r = SettleResult.from_wire(
        {"success": False, "errorReason": "insufficient_funds", "transaction": "", "network": "base"}
    )
    assert r.success is False and r.transaction == "" and r.error_reason == "insufficient_funds"


def test_settle_result_to_wire_omits_absent_optionals():
    wire = SettleResult(success=True, transaction="0x1", network="base").to_wire()
    assert "errorReason" not in wire and "payer" not in wire


# --- CDP credential gate -----------------------------------------------------


def test_cdp_auth_without_env_raises_with_both_var_names(monkeypatch):
    monkeypatch.delenv(ENV_CDP_KEY_ID, raising=False)
    monkeypatch.delenv(ENV_CDP_KEY_SECRET, raising=False)
    with pytest.raises(FacilitatorNotConfigured) as exc:
        CdpAuth.from_env()
    message = str(exc.value)
    assert ENV_CDP_KEY_ID in message and ENV_CDP_KEY_SECRET in message
    # The error must point at the escape hatch, not just complain.
    assert "base-sepolia" in message


def test_cdp_auth_names_only_the_missing_variable(monkeypatch):
    monkeypatch.setenv(ENV_CDP_KEY_ID, "some-id")
    monkeypatch.delenv(ENV_CDP_KEY_SECRET, raising=False)
    with pytest.raises(FacilitatorNotConfigured) as exc:
        CdpAuth.from_env()
    assert ENV_CDP_KEY_SECRET in str(exc.value)


def test_cdp_auth_blank_env_counts_as_missing(monkeypatch):
    monkeypatch.setenv(ENV_CDP_KEY_ID, "   ")
    monkeypatch.setenv(ENV_CDP_KEY_SECRET, "   ")
    with pytest.raises(FacilitatorNotConfigured):
        CdpAuth.from_env()


def test_cdp_auth_mints_a_bearer_token_per_method_and_url(monkeypatch):
    seen = []

    def minter(key_id, key_secret, method, url):
        seen.append((method, url))
        return "TOKEN"

    auth = CdpAuth(key_id="id", key_secret="secret", jwt_minter=minter)
    assert auth.headers("POST", "https://x/verify") == {"Authorization": "Bearer TOKEN"}
    auth.headers("POST", "https://x/settle")
    # A separate token per endpoint -- the JWT is bound to method + host + path.
    assert seen == [("POST", "https://x/verify"), ("POST", "https://x/settle")]


def test_cdp_auth_repr_never_leaks_the_secret():
    text = repr(CdpAuth(key_id="abcdefghijkl", key_secret="TOP-SECRET-VALUE"))
    assert "TOP-SECRET-VALUE" not in text
    assert "abcdefghijkl" not in text
    assert "redacted" in text


def test_default_jwt_minter_fails_loudly_without_the_sdk():
    # The JWT algorithm is not publicly specified, so the code delegates to
    # Coinbase's SDK rather than guessing. Absent SDK must be an actionable error.
    from x402api.payments.facilitator import _default_cdp_jwt

    with pytest.raises(FacilitatorNotConfigured) as exc:
        _default_cdp_jwt("id", "secret", "POST", "https://api.cdp.coinbase.com/x/verify")
    assert "cdp-sdk" in str(exc.value)


def test_build_facilitator_refuses_mainnet_without_credentials(
    mainnet_settings, monkeypatch
):
    monkeypatch.delenv(ENV_CDP_KEY_ID, raising=False)
    monkeypatch.delenv(ENV_CDP_KEY_SECRET, raising=False)
    with pytest.raises(FacilitatorNotConfigured):
        build_facilitator(mainnet_settings)


def test_build_facilitator_allows_testnet_without_credentials(settings, monkeypatch):
    monkeypatch.delenv(ENV_CDP_KEY_ID, raising=False)
    monkeypatch.delenv(ENV_CDP_KEY_SECRET, raising=False)
    f = build_facilitator(settings)
    assert isinstance(f, HttpFacilitator)
    assert f.auth is None
    assert f.base_url == "https://x402.org/facilitator"


def test_build_facilitator_accepts_injected_auth_on_mainnet(mainnet_settings):
    auth = CdpAuth(key_id="id", key_secret="s", jwt_minter=lambda *a: "T")
    assert build_facilitator(mainnet_settings, auth=auth).auth is auth


# --- HTTP layer --------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeClient:
    def __init__(self, response):
        self.response = response
        self.posted = []

    def post(self, url, json=None, headers=None):
        self.posted.append((url, json, headers))
        return self.response

    def close(self):
        pass


def test_http_facilitator_posts_to_the_right_paths():
    client = _FakeClient(_FakeResponse(200, {"isValid": True, "payer": "0xa"}))
    f = HttpFacilitator(base_url="https://fac.test", _client=client)
    assert f.verify(payload(), reqs()).is_valid
    assert client.posted[0][0] == "https://fac.test/verify"

    client.response = _FakeResponse(
        200, {"success": True, "transaction": "0x9", "network": "base-sepolia"}
    )
    assert f.settle(payload(), reqs()).transaction == "0x9"
    assert client.posted[1][0] == "https://fac.test/settle"


def test_http_facilitator_adds_auth_header_when_configured():
    client = _FakeClient(_FakeResponse(200, {"isValid": True}))
    auth = CdpAuth(key_id="id", key_secret="s", jwt_minter=lambda *a: "TOKEN")
    HttpFacilitator(base_url="https://fac.test", auth=auth, _client=client).verify(
        payload(), reqs()
    )
    assert client.posted[0][2]["Authorization"] == "Bearer TOKEN"


def test_http_facilitator_surfaces_an_error_status():
    client = _FakeClient(_FakeResponse(500, None, "upstream exploded"))
    f = HttpFacilitator(base_url="https://fac.test", _client=client)
    with pytest.raises(FacilitatorError, match="HTTP 500"):
        f.verify(payload(), reqs())


def test_http_facilitator_surfaces_non_json():
    client = _FakeClient(_FakeResponse(200, None, "<html>"))
    f = HttpFacilitator(base_url="https://fac.test", _client=client)
    with pytest.raises(FacilitatorError, match="did not return JSON"):
        f.verify(payload(), reqs())


# --- test double ------------------------------------------------------------


def test_mock_facilitator_records_call_order():
    mock = MockFacilitator()
    mock.verify(payload(), reqs())
    mock.settle(payload(), reqs())
    assert mock.call_names == ["verify", "settle"]


def test_mock_facilitator_can_be_scripted_to_fail():
    mock = MockFacilitator(verify_result=VerifyResult(is_valid=False, invalid_reason="nope"))
    assert mock.verify(payload(), reqs()).invalid_reason == "nope"
