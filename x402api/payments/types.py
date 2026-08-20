"""x402 wire types for BOTH protocol versions.

Two formats are live at once and the official SDK server middleware accepts
either, so this one does too (verified against github.com/coinbase/x402 on
2026-08-19; the Python SDK's own FastAPI middleware reads `payment-signature`
OR `x-payment`). Rejecting v1 clients would cut off most existing integrations;
rejecting v2 would mean shipping something already labelled legacy.

What actually differs -- everything else is shared:

                      v1 (legacy)              v2 (current)
  402 payload in      response BODY (JSON)     PAYMENT-REQUIRED header (b64)
  client sends        X-PAYMENT                PAYMENT-SIGNATURE
  server replies      X-PAYMENT-RESPONSE       PAYMENT-RESPONSE
  amount field        maxAmountRequired        amount
  network id          "base"                   "eip155:8453"  (CAIP-2)
  resource metadata   flat on each accepts[]   hoisted to a top-level object
  chosen requirement  implicit                 explicit `accepted` object
  discovery           outputSchema convention  extensions.bazaar

Amounts are STRINGS of atomic token units on the wire in both versions (USDC
has 6 decimals, so "10000" is $0.01). They are parsed to int at the boundary
and never held as float.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass, field
from typing import Any

# Header names, verbatim from the reference implementation's constants module.
HEADER_PAYMENT_REQUIRED_V2 = "PAYMENT-REQUIRED"
HEADER_PAYMENT_SIGNATURE_V2 = "PAYMENT-SIGNATURE"
HEADER_PAYMENT_RESPONSE_V2 = "PAYMENT-RESPONSE"
HEADER_PAYMENT_V1 = "X-PAYMENT"
HEADER_PAYMENT_RESPONSE_V1 = "X-PAYMENT-RESPONSE"

# Browser-based callers cannot read the settlement header without this.
HEADER_EXPOSE = "Access-Control-Expose-Headers"
EXPOSED_HEADERS = f"{HEADER_PAYMENT_RESPONSE_V2},{HEADER_PAYMENT_RESPONSE_V1}"

SCHEME_EXACT = "exact"
SUPPORTED_VERSIONS = (1, 2)


class PaymentProtocolError(ValueError):
    """A client's payment header is malformed. Always answered with a 402."""


# --- codec -------------------------------------------------------------------


def b64_encode_json(obj: Any) -> str:
    """Compact JSON -> standard base64 with padding (what the spec examples show)."""
    raw = json.dumps(obj, separators=(",", ":"), sort_keys=False).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def b64_decode_json(value: str) -> dict:
    """Inverse of b64_encode_json, with errors a caller can act on.

    Tolerates missing padding: some clients strip '='. Rejecting a payment for
    that would be pedantry, and the signature is what actually authorises it.
    """
    if not value or not value.strip():
        raise PaymentProtocolError("Payment header is empty.")
    text = value.strip()
    padding = (-len(text)) % 4
    try:
        raw = base64.b64decode(text + "=" * padding, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise PaymentProtocolError(f"Payment header is not valid base64: {exc}") from exc
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PaymentProtocolError(
            f"Payment header does not decode to JSON: {exc}"
        ) from exc
    if not isinstance(decoded, dict):
        raise PaymentProtocolError("Payment header must decode to a JSON object.")
    return decoded


# --- requirements ------------------------------------------------------------


@dataclass(frozen=True)
class PaymentRequirements:
    """What the server demands for one resource, version-agnostic in memory."""

    amount_atomic: int
    asset: str
    pay_to: str
    resource: str
    description: str
    network_v1: str  # "base"
    network_v2: str  # "eip155:8453"
    max_timeout_seconds: int
    mime_type: str = "application/json"
    scheme: str = SCHEME_EXACT
    extra: dict[str, Any] = field(default_factory=dict)
    # v1 carries discovery metadata inside outputSchema; v2 uses extensions.bazaar.
    output_schema: dict[str, Any] | None = None
    bazaar: dict[str, Any] | None = None

    def to_wire(self, version: int) -> dict:
        if version == 1:
            return {
                "scheme": self.scheme,
                "network": self.network_v1,
                "maxAmountRequired": str(self.amount_atomic),
                "asset": self.asset,
                "payTo": self.pay_to,
                "resource": self.resource,
                "description": self.description,
                "mimeType": self.mime_type,
                "outputSchema": self.output_schema,
                "maxTimeoutSeconds": self.max_timeout_seconds,
                "extra": self.extra or None,
            }
        if version == 2:
            return {
                "scheme": self.scheme,
                "network": self.network_v2,
                "amount": str(self.amount_atomic),
                "asset": self.asset,
                "payTo": self.pay_to,
                "maxTimeoutSeconds": self.max_timeout_seconds,
                "extra": self.extra or None,
            }
        raise PaymentProtocolError(f"Unsupported x402 version {version!r}.")


def build_402_body(requirements: PaymentRequirements, error: str) -> dict:
    """v1 shape -- goes in the 402 response BODY."""
    return {
        "x402Version": 1,
        "error": error,
        "accepts": [requirements.to_wire(1)],
    }


def build_402_header(requirements: PaymentRequirements, error: str) -> str:
    """v2 shape -- goes base64-encoded in the PAYMENT-REQUIRED response HEADER."""
    payload: dict[str, Any] = {
        "x402Version": 2,
        "error": error,
        "resource": {
            "url": requirements.resource,
            "description": requirements.description,
            "mimeType": requirements.mime_type,
        },
        "accepts": [requirements.to_wire(2)],
        "extensions": {"bazaar": requirements.bazaar} if requirements.bazaar else {},
    }
    return b64_encode_json(payload)


# --- payloads ----------------------------------------------------------------


@dataclass(frozen=True)
class PaymentPayload:
    """A client's signed authorisation, normalised across versions."""

    version: int
    scheme: str
    network: str
    raw: dict  # exactly what the client sent, forwarded to the facilitator as-is

    @property
    def authorization(self) -> dict:
        inner = self.raw.get("payload")
        return inner.get("authorization", {}) if isinstance(inner, dict) else {}

    @property
    def authorized_amount(self) -> int | None:
        """Atomic units the client signed for, or None if absent/unparseable.

        Used only as a fast local pre-check. The facilitator's /verify is the
        authority -- it checks the signature, the balance and the nonce, none of
        which can be established here.
        """
        value = self.authorization.get("value")
        if value is None:
            return None
        try:
            return int(str(value))
        except (TypeError, ValueError):
            return None

    @property
    def payer(self) -> str:
        return str(self.authorization.get("from") or "")


def detect_version(headers: dict[str, str]) -> int | None:
    """Which protocol version the client is speaking, from its headers alone.

    v2 wins when both are present: a client sending PAYMENT-SIGNATURE is on the
    current spec, and X-PAYMENT alongside it is a compatibility shim.
    Returns None when the request carries no payment at all.
    """
    lower = {k.lower(): v for k, v in headers.items()}
    if lower.get(HEADER_PAYMENT_SIGNATURE_V2.lower()):
        return 2
    if lower.get(HEADER_PAYMENT_V1.lower()):
        return 1
    return None


def parse_payment_header(headers: dict[str, str]) -> PaymentPayload | None:
    """Decode whichever payment header is present. None means unpaid."""
    version = detect_version(headers)
    if version is None:
        return None
    lower = {k.lower(): v for k, v in headers.items()}
    header_name = (
        HEADER_PAYMENT_SIGNATURE_V2 if version == 2 else HEADER_PAYMENT_V1
    )
    decoded = b64_decode_json(lower[header_name.lower()])

    declared = decoded.get("x402Version")
    if declared is not None:
        try:
            declared = int(declared)
        except (TypeError, ValueError):
            raise PaymentProtocolError(
                f"x402Version must be an integer, got {decoded.get('x402Version')!r}."
            ) from None
        if declared not in SUPPORTED_VERSIONS:
            raise PaymentProtocolError(
                f"Unsupported x402Version {declared}; this server speaks "
                f"{' and '.join(str(v) for v in SUPPORTED_VERSIONS)}."
            )
        # Trust the body over the header name: a client that sent
        # PAYMENT-SIGNATURE with a v1 body is telling us what it actually signed.
        version = declared

    if not isinstance(decoded.get("payload"), dict):
        raise PaymentProtocolError(
            "Payment payload is missing its 'payload' object "
            "(expected payload.signature and payload.authorization)."
        )

    if version == 1:
        scheme = str(decoded.get("scheme") or "")
        network = str(decoded.get("network") or "")
    else:
        accepted = decoded.get("accepted")
        if not isinstance(accepted, dict):
            raise PaymentProtocolError(
                "x402 v2 payload is missing its 'accepted' object naming the "
                "payment requirement the client chose."
            )
        scheme = str(accepted.get("scheme") or "")
        network = str(accepted.get("network") or "")

    if not scheme:
        raise PaymentProtocolError("Payment payload does not name a scheme.")
    if not network:
        raise PaymentProtocolError("Payment payload does not name a network.")

    return PaymentPayload(version=version, scheme=scheme, network=network, raw=decoded)


def response_header_name(version: int) -> str:
    return HEADER_PAYMENT_RESPONSE_V2 if version == 2 else HEADER_PAYMENT_RESPONSE_V1
