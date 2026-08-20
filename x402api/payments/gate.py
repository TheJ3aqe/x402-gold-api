"""The payment decision logic, with no web framework anywhere in it.

Keeping this framework-free is what makes the protocol testable without
spinning up a server: every branch below is reachable from a plain dict of
headers. middleware.py is a thin adapter on top.

The order of operations is deliberate and is the part most worth getting right:

    1. Price the request        (deterministic from the URL + query params)
    2. No payment header        -> 402 challenge, stop
    3. Local sanity checks      -> 402, without troubling the facilitator
    4. facilitator.verify()     -> 402 if the signature/balance is bad
    5. RUN THE HANDLER          <- the expensive upstream work happens here
    6. facilitator.settle()     -> move the money
    7. Journal the settlement, return data + settlement header

Step 5 sits between verify and settle on purpose. Settling first would charge
for data that the upstream CFTC API might then fail to provide; settling only
after the handler succeeds means a failed request costs the caller nothing. The
residual risk is the reverse -- handler succeeded, settle failed -- and that is
handled by returning 402 WITHOUT the data rather than serving it unpaid.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..pricing import RoutePrice
from ..taxlog import TaxLog
from .facilitator import Facilitator, FacilitatorError, SettleResult, VerifyResult
from .types import (
    EXPOSED_HEADERS,
    HEADER_EXPOSE,
    HEADER_PAYMENT_REQUIRED_V2,
    SCHEME_EXACT,
    PaymentPayload,
    PaymentProtocolError,
    PaymentRequirements,
    b64_encode_json,
    build_402_body,
    build_402_header,
    parse_payment_header,
    response_header_name,
)


@dataclass(frozen=True)
class Challenge:
    """A ready-to-send 402: the body, the headers, and why it happened."""

    body: dict
    headers: dict[str, str]
    reason: str
    status_code: int = 402


@dataclass(frozen=True)
class Authorized:
    """Verification passed. The handler may run; settlement is still pending."""

    payload: PaymentPayload
    requirements: PaymentRequirements
    verify: VerifyResult


@dataclass(frozen=True)
class Settled:
    result: SettleResult
    headers: dict[str, str]
    journal_entry: Any = None


def build_challenge(
    requirements: PaymentRequirements, error: str
) -> Challenge:
    """A 402 that BOTH protocol versions can read.

    v1 clients parse the JSON body; v2 clients read the PAYMENT-REQUIRED header.
    Emitting both costs a few hundred bytes and removes the need to guess which
    dialect an unknown agent speaks.
    """
    return Challenge(
        body=build_402_body(requirements, error),
        headers={
            HEADER_PAYMENT_REQUIRED_V2: build_402_header(requirements, error),
            HEADER_EXPOSE: EXPOSED_HEADERS,
            "Content-Type": "application/json",
        },
        reason=error,
    )


@dataclass
class PaymentGate:
    """Prices a request, challenges it, verifies it and settles it."""

    settings: Any  # x402api.config.Settings
    facilitator: Facilitator
    tax_log: TaxLog
    prices: dict[str, RoutePrice] = field(default_factory=dict)

    # --- pricing ---

    def requirements_for(
        self,
        *,
        route: str,
        market_count: int,
        resource_url: str,
        description: str | None = None,
        bazaar: dict | None = None,
        output_schema: dict | None = None,
    ) -> PaymentRequirements:
        price = self.prices.get(route)
        if price is None:
            raise KeyError(
                f"Route {route!r} has no price. Priced routes: "
                f"{', '.join(sorted(self.prices))}."
            )
        net = self.settings.network
        return PaymentRequirements(
            amount_atomic=price.quote(market_count),
            asset=net.usdc_address,
            pay_to=self.settings.pay_to,
            resource=resource_url,
            description=description or price.description,
            network_v1=net.key,
            network_v2=net.caip2,
            max_timeout_seconds=self.settings.payment_timeout_seconds,
            # The EIP-712 domain of the token being transferred. It must match
            # the deployed contract or every signature fails verification, and
            # it differs between mainnet ("USD Coin") and Sepolia ("USDC").
            extra={"name": net.eip712_name, "version": net.eip712_version},
            bazaar=bazaar,
            output_schema=output_schema,
        )

    # --- authorisation ---

    def authorize(
        self, headers: dict[str, str], requirements: PaymentRequirements
    ) -> Authorized | Challenge:
        try:
            payload = parse_payment_header(headers)
        except PaymentProtocolError as exc:
            return build_challenge(requirements, str(exc))

        if payload is None:
            return build_challenge(
                requirements,
                "Payment required. Send a signed x402 payment in the "
                "PAYMENT-SIGNATURE header (v2) or X-PAYMENT header (v1).",
            )

        local = self._local_checks(payload, requirements)
        if local is not None:
            return build_challenge(requirements, local)

        try:
            result = self.facilitator.verify(payload, requirements)
        except FacilitatorError as exc:
            # Upstream trouble, not the client's fault -- but the data still
            # cannot be released, so it is still a 402 and it says why.
            return build_challenge(requirements, f"Payment verification unavailable: {exc}")

        if not result.is_valid:
            return build_challenge(
                requirements,
                f"Payment verification failed: {result.invalid_reason or 'unspecified'}.",
            )
        return Authorized(payload=payload, requirements=requirements, verify=result)

    def _local_checks(
        self, payload: PaymentPayload, requirements: PaymentRequirements
    ) -> str | None:
        """Cheap checks that would otherwise cost a facilitator round trip.

        These are a fast path and a source of readable errors, never the
        security boundary -- the signature, the balance and the nonce can only
        be checked by the facilitator, and verify() is always called too.
        """
        if payload.scheme != SCHEME_EXACT:
            return (
                f"Unsupported payment scheme {payload.scheme!r}; this resource is "
                f"priced with the {SCHEME_EXACT!r} scheme."
            )

        expected_network = (
            requirements.network_v2 if payload.version == 2 else requirements.network_v1
        )
        if payload.network != expected_network:
            return (
                f"Payment is on network {payload.network!r} but this resource "
                f"settles on {expected_network!r}."
            )

        authorized = payload.authorized_amount
        if authorized is None:
            return "Payment authorization is missing a numeric 'value'."
        if authorized < requirements.amount_atomic:
            return (
                f"Payment authorizes {authorized} atomic units but this call costs "
                f"{requirements.amount_atomic}."
            )

        recipient = str(payload.authorization.get("to") or "")
        if recipient and recipient.lower() != requirements.pay_to.lower():
            return "Payment is authorized to a different recipient than this resource requires."
        return None

    # --- settlement ---

    def settle(
        self, authorized: Authorized, *, route: str
    ) -> Settled | Challenge:
        """Move the money, then journal it. Either both happen or the call fails."""
        try:
            result = self.facilitator.settle(authorized.payload, authorized.requirements)
        except FacilitatorError as exc:
            return build_challenge(
                authorized.requirements, f"Payment settlement unavailable: {exc}"
            )

        if not result.success:
            return build_challenge(
                authorized.requirements,
                f"Payment settlement failed: {result.error_reason or 'unspecified'}.",
            )

        # Journal before returning. A TaxLogError propagates: an inflow that
        # reached the chain but not the books is precisely what the journal
        # exists to prevent, so the operator must see it immediately.
        entry = self.tax_log.record(
            route=route,
            resource=authorized.requirements.resource,
            amount_atomic=authorized.requirements.amount_atomic,
            network=result.network or authorized.requirements.network_v1,
            asset=authorized.requirements.asset,
            transaction_hash=result.transaction,
            payer=result.payer or authorized.payload.payer,
            pay_to=authorized.requirements.pay_to,
            facilitator=str(getattr(self.settings, "facilitator_url", "")),
            x402_version=authorized.payload.version,
        )

        header_name = response_header_name(authorized.payload.version)
        return Settled(
            result=result,
            headers={
                header_name: b64_encode_json(result.to_wire()),
                HEADER_EXPOSE: EXPOSED_HEADERS,
            },
            journal_entry=entry,
        )
