"""The facilitator client: /verify, /settle, /supported.

The facilitator is the third party that checks a signature and pushes the
transfer on-chain. It is NOT a custodian -- funds move straight from payer to
`payTo`, which is why this rail needs no payout threshold and no withdrawal
step (verified 2026-08-17, vault note wave 6).

Two deployments, and the difference matters for what can be tested today:

  * https://x402.org/facilitator  -- Base Sepolia, NO CREDENTIALS AT ALL.
    The entire service can be exercised end to end against this today, before
    any CDP account exists.
  * https://api.cdp.coinbase.com/platform/v2/x402 -- mainnet, needs a CDP API
    key. That key does not exist yet. Per CLAUDE.md ("Evidence only, never
    guess") this module therefore refuses to start on mainnet rather than
    inventing a credential: no dummy key, no unauthenticated fallback, no
    "assume it works". The error names the exact env vars to set.

Wire shapes verified against github.com/coinbase/x402 on 2026-08-19:
    POST {base}/verify  {"x402Version":N,"paymentPayload":{...},"paymentRequirements":{...}}
      -> {"isValid":true,"payer":"0x..."} | {"isValid":false,"invalidReason":"...","payer":"0x..."}
    POST {base}/settle  (same body)
      -> {"success":true,"payer":"0x...","transaction":"0x...","network":"base-sepolia"}
         {"success":false,"errorReason":"...","transaction":"","network":"..."}
    GET  {base}/supported -> {"kinds":[{"x402Version":1,"scheme":"exact","network":"base"}]}
Note `transaction` is an empty STRING on failure, not null.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Protocol
from urllib.parse import urlparse

import httpx

from ..config import ENV_CDP_KEY_ID, ENV_CDP_KEY_SECRET
from .types import PaymentPayload, PaymentRequirements


class FacilitatorError(RuntimeError):
    """The facilitator could not be reached or answered unusably."""


class FacilitatorNotConfigured(RuntimeError):
    """A credential the facilitator requires is missing. Never guessed around."""


@dataclass(frozen=True)
class VerifyResult:
    is_valid: bool
    invalid_reason: str | None = None
    payer: str | None = None

    @classmethod
    def from_wire(cls, body: dict) -> "VerifyResult":
        return cls(
            is_valid=bool(body.get("isValid")),
            invalid_reason=body.get("invalidReason"),
            payer=body.get("payer"),
        )


@dataclass(frozen=True)
class SettleResult:
    success: bool
    transaction: str = ""
    network: str = ""
    payer: str | None = None
    error_reason: str | None = None

    @classmethod
    def from_wire(cls, body: dict) -> "SettleResult":
        return cls(
            success=bool(body.get("success")),
            transaction=str(body.get("transaction") or ""),
            network=str(body.get("network") or ""),
            payer=body.get("payer"),
            error_reason=body.get("errorReason"),
        )

    def to_wire(self) -> dict:
        out: dict[str, Any] = {
            "success": self.success,
            "transaction": self.transaction,
            "network": self.network,
        }
        if self.payer:
            out["payer"] = self.payer
        if self.error_reason:
            out["errorReason"] = self.error_reason
        return out


class Facilitator(Protocol):
    """Everything the payment gate needs. Implemented for real and for tests."""

    def verify(
        self, payload: PaymentPayload, requirements: PaymentRequirements
    ) -> VerifyResult: ...

    def settle(
        self, payload: PaymentPayload, requirements: PaymentRequirements
    ) -> SettleResult: ...


def _request_body(
    payload: PaymentPayload, requirements: PaymentRequirements
) -> dict:
    """The x402Version appears twice: top level AND inside paymentPayload.

    That is redundant but it is what the reference client sends and what
    facilitators validate against, so it is reproduced rather than tidied up.
    """
    return {
        "x402Version": payload.version,
        "paymentPayload": payload.raw,
        "paymentRequirements": requirements.to_wire(payload.version),
    }


# --- CDP authentication ------------------------------------------------------
#
# The CDP facilitator wants `Authorization: Bearer <JWT>` where the JWT is
# minted per request and bound to method + host + path (so /verify and /settle
# need different tokens). The signing algorithm and claim set are NOT documented
# in the public x402 repo -- the reference implementations delegate to Coinbase's
# own `cdp-sdk` package. Hand-rolling it from a guess would be exactly the kind
# of invented detail CLAUDE.md forbids, so this delegates too, and fails with an
# actionable message if the SDK is absent.


def _default_cdp_jwt(key_id: str, key_secret: str, method: str, url: str) -> str:
    parsed = urlparse(url)
    try:
        from cdp.auth.utils.jwt import JwtOptions, generate_jwt  # type: ignore
    except ImportError as exc:
        raise FacilitatorNotConfigured(
            "Mainnet settlement needs a CDP-signed JWT, which Coinbase's own SDK "
            "mints. Install it with `pip install cdp-sdk`.\n"
            "The JWT algorithm and claim set are not publicly specified, so this "
            "service delegates rather than reimplementing them from a guess. If "
            "the SDK's entry point has moved, pass your own callable as "
            "CdpAuth(jwt_minter=...) instead of patching this function."
        ) from exc
    return generate_jwt(
        JwtOptions(
            api_key_id=key_id,
            api_key_secret=key_secret,
            request_method=method.upper(),
            request_host=parsed.netloc,
            request_path=parsed.path,
        )
    )


@dataclass
class CdpAuth:
    """Bearer-JWT auth for the hosted CDP facilitator.

    Credentials come from the environment only. They are never written to a
    file, a log line or a note -- CLAUDE.md's secrets rule -- and this object
    never exposes them in its repr beyond a redacted key id.
    """

    key_id: str
    key_secret: str
    jwt_minter: Callable[[str, str, str, str], str] = _default_cdp_jwt

    @classmethod
    def from_env(cls) -> "CdpAuth":
        key_id = os.environ.get(ENV_CDP_KEY_ID, "").strip()
        key_secret = os.environ.get(ENV_CDP_KEY_SECRET, "").strip()
        missing = [
            name
            for name, value in ((ENV_CDP_KEY_ID, key_id), (ENV_CDP_KEY_SECRET, key_secret))
            if not value
        ]
        if missing:
            raise FacilitatorNotConfigured(
                f"Missing {' and '.join(missing)} in the environment. The hosted "
                "CDP facilitator will not verify or settle a mainnet payment "
                "without them, and this service will not pretend otherwise.\n"
                "Fix: create an API key at portal.cdp.coinbase.com, export both "
                "values (never commit them, never paste them into a note), and "
                "restart.\n"
                "To develop without a CDP account, run against Base Sepolia "
                "instead: X402_NETWORK=base-sepolia uses https://x402.org/facilitator, "
                "which needs no credentials."
            )
        return cls(key_id=key_id, key_secret=key_secret)

    def headers(self, method: str, url: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.jwt_minter(self.key_id, self.key_secret, method, url)}"}

    def __repr__(self) -> str:  # keeps secrets out of tracebacks
        tail = self.key_id[-4:] if len(self.key_id) > 8 else "????"
        return f"CdpAuth(key_id=...{tail}, key_secret=<redacted>)"


@dataclass
class HttpFacilitator:
    """Real facilitator over HTTP."""

    base_url: str
    auth: CdpAuth | None = None
    timeout_seconds: float = 30.0
    _client: httpx.Client | None = None

    def _post(self, path: str, body: dict) -> dict:
        url = f"{self.base_url.rstrip('/')}{path}"
        headers = {"Content-Type": "application/json"}
        if self.auth is not None:
            headers.update(self.auth.headers("POST", url))
        client = self._client or httpx.Client(timeout=self.timeout_seconds)
        try:
            response = client.post(url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise FacilitatorError(f"Facilitator {url} unreachable: {exc}") from exc
        finally:
            if self._client is None:
                client.close()
        if response.status_code >= 400:
            raise FacilitatorError(
                f"Facilitator {url} returned HTTP {response.status_code}: "
                f"{response.text[:300]}"
            )
        try:
            decoded = response.json()
        except ValueError as exc:
            raise FacilitatorError(
                f"Facilitator {url} did not return JSON: {response.text[:300]}"
            ) from exc
        if not isinstance(decoded, dict):
            raise FacilitatorError(f"Facilitator {url} returned {type(decoded).__name__}, expected an object.")
        return decoded

    def verify(
        self, payload: PaymentPayload, requirements: PaymentRequirements
    ) -> VerifyResult:
        return VerifyResult.from_wire(
            self._post("/verify", _request_body(payload, requirements))
        )

    def settle(
        self, payload: PaymentPayload, requirements: PaymentRequirements
    ) -> SettleResult:
        return SettleResult.from_wire(
            self._post("/settle", _request_body(payload, requirements))
        )

    def supported(self) -> dict:
        url = f"{self.base_url.rstrip('/')}/supported"
        headers = {}
        if self.auth is not None:
            headers.update(self.auth.headers("GET", url))
        client = self._client or httpx.Client(timeout=self.timeout_seconds)
        try:
            response = client.get(url, headers=headers)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise FacilitatorError(f"Facilitator {url} /supported failed: {exc}") from exc
        finally:
            if self._client is None:
                client.close()


def build_facilitator(settings, auth: CdpAuth | None = None) -> HttpFacilitator:
    """Wire a facilitator for these settings, refusing an unauthenticated mainnet.

    The refusal is the point: an unauthenticated mainnet client would answer 402
    happily and then fail every settlement, so the failure belongs at startup
    where an operator sees it.
    """
    if settings.requires_cdp_credentials and auth is None:
        auth = CdpAuth.from_env()
    return HttpFacilitator(base_url=settings.facilitator_url, auth=auth)


# --- test double -------------------------------------------------------------


@dataclass
class MockFacilitator:
    """Scriptable stand-in so the full payment path is testable without a key.

    Records every call so tests can assert not just the outcome but the ORDER --
    specifically that settle() is never reached when verify() fails, and never
    reached when the handler itself failed.
    """

    verify_result: VerifyResult = VerifyResult(is_valid=True, payer="0x" + "11" * 20)
    settle_result: SettleResult | None = None
    verify_error: Exception | None = None
    settle_error: Exception | None = None
    calls: list[tuple[str, dict]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.calls is None:
            self.calls = []
        if self.settle_result is None:
            self.settle_result = SettleResult(
                success=True,
                transaction="0x" + "ab" * 32,
                network="base-sepolia",
                payer=self.verify_result.payer,
            )

    def verify(
        self, payload: PaymentPayload, requirements: PaymentRequirements
    ) -> VerifyResult:
        self.calls.append(("verify", _request_body(payload, requirements)))
        if self.verify_error:
            raise self.verify_error
        return self.verify_result

    def settle(
        self, payload: PaymentPayload, requirements: PaymentRequirements
    ) -> SettleResult:
        self.calls.append(("settle", _request_body(payload, requirements)))
        if self.settle_error:
            raise self.settle_error
        return self.settle_result

    @property
    def call_names(self) -> list[str]:
        return [name for name, _ in self.calls]
