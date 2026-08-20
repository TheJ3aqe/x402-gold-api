"""x402 wire format: the 402 shapes, the codec, and payload parsing for v1 + v2."""

from __future__ import annotations

import base64
import json

import pytest

from x402api.payments.types import (
    HEADER_PAYMENT_REQUIRED_V2,
    PaymentProtocolError,
    PaymentRequirements,
    b64_decode_json,
    b64_encode_json,
    build_402_body,
    build_402_header,
    detect_version,
    parse_payment_header,
    response_header_name,
)

from conftest import FAKE_PAY_TO, payment_headers, signed_payload


def reqs(amount: int = 10_000) -> PaymentRequirements:
    return PaymentRequirements(
        amount_atomic=amount,
        asset="0x036CbD53842c5426634e7929541eC2318f3dCF7e",
        pay_to=FAKE_PAY_TO,
        resource="https://api.example.test/v1/cot/snapshot",
        description="Positioning snapshot",
        network_v1="base-sepolia",
        network_v2="eip155:84532",
        max_timeout_seconds=60,
        extra={"name": "USDC", "version": "2"},
    )


# --- codec -------------------------------------------------------------------


def test_codec_roundtrip():
    payload = {"a": 1, "b": ["x", "y"], "c": {"d": True}}
    assert b64_decode_json(b64_encode_json(payload)) == payload


def test_decoder_tolerates_stripped_padding():
    encoded = b64_encode_json({"hello": "world"}).rstrip("=")
    assert b64_decode_json(encoded) == {"hello": "world"}


def test_decoder_rejects_non_base64():
    with pytest.raises(PaymentProtocolError, match="base64|JSON"):
        b64_decode_json("!!!! not base64 !!!!")


def test_decoder_rejects_non_json_payload():
    with pytest.raises(PaymentProtocolError, match="JSON"):
        b64_decode_json(base64.b64encode(b"plain text").decode())


def test_decoder_rejects_json_that_is_not_an_object():
    with pytest.raises(PaymentProtocolError, match="object"):
        b64_decode_json(base64.b64encode(b"[1,2,3]").decode())


def test_decoder_rejects_empty():
    with pytest.raises(PaymentProtocolError, match="empty"):
        b64_decode_json("   ")


# --- 402 shapes --------------------------------------------------------------


def test_v1_402_body_matches_the_spec_shape():
    body = build_402_body(reqs(), "Payment required")
    assert body["x402Version"] == 1
    assert body["error"] == "Payment required"
    assert isinstance(body["accepts"], list) and len(body["accepts"]) == 1
    accepted = body["accepts"][0]
    assert accepted["scheme"] == "exact"
    assert accepted["network"] == "base-sepolia"
    # v1 names the amount maxAmountRequired and sends it as a STRING of atomic units.
    assert accepted["maxAmountRequired"] == "10000"
    assert isinstance(accepted["maxAmountRequired"], str)
    assert accepted["payTo"] == FAKE_PAY_TO
    assert accepted["resource"].endswith("/v1/cot/snapshot")
    assert accepted["maxTimeoutSeconds"] == 60
    assert accepted["extra"] == {"name": "USDC", "version": "2"}
    # v1-only keys that must be present even when empty
    assert "mimeType" in accepted and "outputSchema" in accepted


def test_v2_402_header_matches_the_spec_shape():
    header = build_402_header(reqs(), "Payment required")
    decoded = b64_decode_json(header)
    assert decoded["x402Version"] == 2
    # v2 hoists resource metadata out of accepts[]
    assert decoded["resource"]["url"].endswith("/v1/cot/snapshot")
    assert decoded["resource"]["mimeType"] == "application/json"
    accepted = decoded["accepts"][0]
    # v2 renames the amount and uses CAIP-2 networks
    assert accepted["amount"] == "10000"
    assert "maxAmountRequired" not in accepted
    assert accepted["network"] == "eip155:84532"
    assert "resource" not in accepted


def test_v2_header_carries_the_bazaar_extension_when_present():
    r = reqs()
    r = PaymentRequirements(**{**r.__dict__, "bazaar": {"discoverable": True}})
    decoded = b64_decode_json(build_402_header(r, "x"))
    assert decoded["extensions"]["bazaar"]["discoverable"] is True


def test_v2_header_omits_extensions_when_there_is_nothing_to_say():
    assert b64_decode_json(build_402_header(reqs(), "x"))["extensions"] == {}


def test_amount_is_never_serialised_as_a_float():
    # Floats are how you get an off-by-one-wei mismatch and a failed signature.
    body = build_402_body(reqs(1), "x")
    raw = json.dumps(body)
    assert '"maxAmountRequired":"1"' in raw.replace(" ", "")


def test_unsupported_version_rejected():
    with pytest.raises(PaymentProtocolError, match="Unsupported x402 version"):
        reqs().to_wire(3)


# --- version detection -------------------------------------------------------


def test_detect_version_none_when_unpaid():
    assert detect_version({"accept": "application/json"}) is None


def test_detect_version_v1_and_v2():
    assert detect_version({"X-PAYMENT": "abc"}) == 1
    assert detect_version({"PAYMENT-SIGNATURE": "abc"}) == 2


def test_v2_wins_when_a_client_sends_both():
    assert detect_version({"X-PAYMENT": "a", "PAYMENT-SIGNATURE": "b"}) == 2


def test_detection_is_case_insensitive():
    # Real HTTP stacks lowercase header names.
    assert detect_version({"payment-signature": "abc"}) == 2
    assert detect_version({"x-payment": "abc"}) == 1


def test_response_header_name_per_version():
    assert response_header_name(1) == "X-PAYMENT-RESPONSE"
    assert response_header_name(2) == "PAYMENT-RESPONSE"


# --- payload parsing ---------------------------------------------------------


def test_parse_returns_none_for_an_unpaid_request():
    assert parse_payment_header({"accept": "*/*"}) is None


@pytest.mark.parametrize("version", [1, 2])
def test_parse_roundtrip(version):
    headers = payment_headers(version=version, amount=10_000)
    parsed = parse_payment_header(headers)
    assert parsed is not None
    assert parsed.version == version
    assert parsed.scheme == "exact"
    assert parsed.authorized_amount == 10_000
    assert parsed.payer.startswith("0x")


def test_v1_payload_reads_scheme_and_network_from_the_top_level():
    parsed = parse_payment_header(payment_headers(version=1, amount=10_000))
    assert parsed.network == "base-sepolia"


def test_v2_payload_reads_scheme_and_network_from_accepted():
    parsed = parse_payment_header(payment_headers(version=2, amount=10_000))
    assert parsed.network == "eip155:84532"


def test_body_version_overrides_the_header_it_arrived_in():
    # A client sending a v1 body under the v2 header name is telling us what it
    # actually signed; trusting the header would send the wrong shape to the
    # facilitator and fail an otherwise-good payment.
    payload = signed_payload(version=1, amount=10_000)
    parsed = parse_payment_header({"PAYMENT-SIGNATURE": b64_encode_json(payload)})
    assert parsed.version == 1
    assert parsed.network == "base-sepolia"


def test_v2_payload_without_accepted_is_rejected():
    payload = signed_payload(version=2, amount=10_000)
    del payload["accepted"]
    with pytest.raises(PaymentProtocolError, match="accepted"):
        parse_payment_header({"PAYMENT-SIGNATURE": b64_encode_json(payload)})


def test_payload_without_inner_payload_object_is_rejected():
    payload = signed_payload(version=1, amount=10_000)
    del payload["payload"]
    with pytest.raises(PaymentProtocolError, match="payload"):
        parse_payment_header({"X-PAYMENT": b64_encode_json(payload)})


def test_unknown_protocol_version_is_rejected():
    payload = signed_payload(version=1, amount=10_000)
    payload["x402Version"] = 99
    with pytest.raises(PaymentProtocolError, match="Unsupported x402Version"):
        parse_payment_header({"X-PAYMENT": b64_encode_json(payload)})


def test_non_integer_version_is_rejected():
    payload = signed_payload(version=1, amount=10_000)
    payload["x402Version"] = "one"
    with pytest.raises(PaymentProtocolError, match="integer"):
        parse_payment_header({"X-PAYMENT": b64_encode_json(payload)})


def test_missing_network_is_rejected():
    payload = signed_payload(version=1, amount=10_000)
    payload["network"] = ""
    with pytest.raises(PaymentProtocolError, match="network"):
        parse_payment_header({"X-PAYMENT": b64_encode_json(payload)})


def test_unparseable_amount_reads_as_none_rather_than_crashing():
    payload = signed_payload(version=1, amount=10_000)
    payload["payload"]["authorization"]["value"] = "not-a-number"
    parsed = parse_payment_header({"X-PAYMENT": b64_encode_json(payload)})
    assert parsed.authorized_amount is None


def test_402_header_constant_name():
    assert HEADER_PAYMENT_REQUIRED_V2 == "PAYMENT-REQUIRED"
