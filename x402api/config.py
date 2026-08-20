"""Runtime settings. No address, key or token is ever hardcoded here.

Two hard rules from CLAUDE.md shape this module:

  * "Keine Secrets in Docs/Code" -- the receiving wallet lives ONLY in
    config/payout.json at the repo root, read through common/payout.py. This
    module imports that resolver; it never carries a fallback address.
  * "Evidence only, never guess" -- a missing CDP credential raises a loud,
    actionable error. There is no dummy key and no silent degradation to an
    unauthenticated facilitator, because that would look like it works while
    settling nothing.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

# Repo root of the PRIVATE monorepo: income/x402-gold-api/x402api/config.py
# -> up 3 -> jarvis-rhod/. Only exists when this runs inside that monorepo
# (Jarvis's own machine). This repo also ships standalone as its own public
# GitHub repo (x402-gold-api, no jarvis-rhod/common/ around it at all) --
# there parents[3] doesn't exist, so this must not crash at import time.
# resolve_pay_to() below already requires X402_PAY_TO as an env var first and
# only reaches for common.payout as a fallback; REPO_ROOT being None there
# just means that fallback path raises its own clear error instead of this
# module failing to import in the first place.
try:
    REPO_ROOT = Path(__file__).resolve().parents[3]
except IndexError:
    REPO_ROOT = None
else:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

PACKAGE_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    """Raised when the service is not configured well enough to take money."""


# --- Networks ----------------------------------------------------------------
#
# USDC token contracts are PUBLIC, well-known addresses (they are not secrets --
# every client needs them to sign a transfer). Values read from the x402
# reference asset table on 2026-08-19.
#
# The EIP-712 domain `name` differs between mainnet and testnet ("USD Coin" vs
# "USDC"). It goes into PaymentRequirements.extra and must match the token's
# actual domain separator, or every signature fails verification. Copying the
# testnet block to mainnet is the classic way to break this.
@dataclass(frozen=True)
class Network:
    key: str  # x402 v1 network identifier
    caip2: str  # x402 v2 network identifier
    chain_id: int
    usdc_address: str
    eip712_name: str
    eip712_version: str
    is_testnet: bool


NETWORKS: dict[str, Network] = {
    "base": Network(
        key="base",
        caip2="eip155:8453",
        chain_id=8453,
        usdc_address="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        eip712_name="USD Coin",
        eip712_version="2",
        is_testnet=False,
    ),
    "base-sepolia": Network(
        key="base-sepolia",
        caip2="eip155:84532",
        chain_id=84532,
        usdc_address="0x036CbD53842c5426634e7929541eC2318f3dCF7e",
        eip712_name="USDC",
        eip712_version="2",
        is_testnet=True,
    ),
}

# The public testnet facilitator needs no credentials at all, which is why the
# whole service is end-to-end testable before Kevin has a CDP account.
# (docs.x402.org quickstart-for-sellers, confirmed 2026-08-19.)
TESTNET_FACILITATOR_URL = "https://x402.org/facilitator"

# Mainnet goes through CDP and DOES need a key.
# (github.com/coinbase/x402 go/legacy/pkg/coinbasefacilitator, confirmed 2026-08-19.)
CDP_FACILITATOR_URL = "https://api.cdp.coinbase.com/platform/v2/x402"

# Env var names only -- never the values.
ENV_CDP_KEY_ID = "CDP_API_KEY_ID"
ENV_CDP_KEY_SECRET = "CDP_API_KEY_SECRET"


@dataclass(frozen=True)
class Settings:
    network: Network
    facilitator_url: str
    pay_to: str
    base_url: str
    # How long a client has to sign and return the payment before the quote is
    # stale. The x402 examples use 60s; kept as the default because a shorter
    # window breaks agents on slow chains and a longer one widens the replay
    # surface. Overridable via X402_PAYMENT_TIMEOUT_SECONDS.
    payment_timeout_seconds: int = 60
    # CFTC publishes COT weekly (Friday 15:30 ET). Anything shorter than a week
    # cannot go stale in a way that matters, so this TTL is purely about not
    # hammering a government API: 3600s = at most 24 upstream calls per market
    # per day regardless of how many paid calls arrive.
    upstream_cache_ttl_seconds: int = 3600

    @property
    def requires_cdp_credentials(self) -> bool:
        return not self.network.is_testnet


def resolve_pay_to(explicit: str | None = None) -> str:
    """Where settled USDC lands. Explicit value wins (tests), else payout.json.

    Deliberately raises rather than returning a placeholder: an API that answers
    402 with an unusable payTo would collect signatures that can never settle.
    """
    if explicit:
        return explicit
    env = os.environ.get("X402_PAY_TO")
    if env:
        return env
    try:
        from common.payout import PayoutNotConfigured, get_destination
    except ImportError as exc:  # pragma: no cover - repo layout is fixed
        raise ConfigError(
            f"Cannot import common.payout from {REPO_ROOT}. This service must run "
            "inside the jarvis-rhod repo so it can read the single source of truth "
            "for payout destinations."
        ) from exc
    try:
        dest = get_destination("x402")
    except PayoutNotConfigured as exc:
        raise ConfigError(
            f"No x402 payout destination configured. {exc}\n"
            "Fix: copy config/payout.example.json to config/payout.json and set "
            "platforms.x402.address to the receiving EVM address. The file is "
            "gitignored on purpose -- the address never belongs in code or notes."
        ) from exc
    if dest.method != "crypto":
        raise ConfigError(
            f"platforms.x402.method is {dest.method!r}; x402 settles on-chain and "
            "requires method='crypto'."
        )
    return dest.address


def load_settings(
    *,
    network: str | None = None,
    pay_to: str | None = None,
    facilitator_url: str | None = None,
    base_url: str | None = None,
) -> Settings:
    """Build Settings from explicit args, then env, then documented defaults."""
    net_key = (network or os.environ.get("X402_NETWORK") or "base-sepolia").strip()
    net = NETWORKS.get(net_key)
    if net is None:
        raise ConfigError(
            f"Unknown network {net_key!r}. Known: {', '.join(sorted(NETWORKS))}."
        )

    url = facilitator_url or os.environ.get("X402_FACILITATOR_URL")
    if not url:
        url = TESTNET_FACILITATOR_URL if net.is_testnet else CDP_FACILITATOR_URL

    return Settings(
        network=net,
        facilitator_url=url.rstrip("/"),
        pay_to=resolve_pay_to(pay_to),
        base_url=(
            base_url or os.environ.get("X402_BASE_URL") or "http://localhost:8402"
        ).rstrip("/"),
        payment_timeout_seconds=_int_env("X402_PAYMENT_TIMEOUT_SECONDS", 60),
        upstream_cache_ttl_seconds=_int_env("X402_UPSTREAM_CACHE_TTL_SECONDS", 3600),
    )


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}.") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be positive, got {value}.")
    return value
