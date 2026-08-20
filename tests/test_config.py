"""Settings resolution, and that the payout address can only come from one place."""

from __future__ import annotations

import pytest

from x402api.config import (
    CDP_FACILITATOR_URL,
    NETWORKS,
    TESTNET_FACILITATOR_URL,
    ConfigError,
    load_settings,
    resolve_pay_to,
)

from conftest import FAKE_PAY_TO


# --- networks ----------------------------------------------------------------


def test_base_mainnet_constants():
    net = NETWORKS["base"]
    assert net.chain_id == 8453
    assert net.caip2 == "eip155:8453"
    assert net.is_testnet is False
    # The EIP-712 domain name on mainnet is "USD Coin", NOT "USDC". Copying the
    # testnet block here is the classic way to make every signature fail.
    assert net.eip712_name == "USD Coin"


def test_base_sepolia_constants():
    net = NETWORKS["base-sepolia"]
    assert net.chain_id == 84532
    assert net.caip2 == "eip155:84532"
    assert net.is_testnet is True
    assert net.eip712_name == "USDC"


def test_the_two_networks_use_different_usdc_contracts():
    assert NETWORKS["base"].usdc_address != NETWORKS["base-sepolia"].usdc_address


def test_unknown_network_is_rejected():
    with pytest.raises(ConfigError, match="Unknown network"):
        load_settings(network="ethereum-mainnet", pay_to=FAKE_PAY_TO)


# --- facilitator selection ---------------------------------------------------


def test_testnet_defaults_to_the_unauthenticated_facilitator():
    s = load_settings(network="base-sepolia", pay_to=FAKE_PAY_TO)
    assert s.facilitator_url == TESTNET_FACILITATOR_URL
    assert s.requires_cdp_credentials is False


def test_mainnet_defaults_to_cdp_and_demands_credentials():
    s = load_settings(network="base", pay_to=FAKE_PAY_TO)
    assert s.facilitator_url == CDP_FACILITATOR_URL
    assert s.requires_cdp_credentials is True


def test_facilitator_url_is_overridable(monkeypatch):
    monkeypatch.setenv("X402_FACILITATOR_URL", "https://my-facilitator.test/")
    s = load_settings(network="base-sepolia", pay_to=FAKE_PAY_TO)
    assert s.facilitator_url == "https://my-facilitator.test"  # trailing slash trimmed


def test_network_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("X402_NETWORK", "base")
    assert load_settings(pay_to=FAKE_PAY_TO).network.key == "base"


# --- payout resolution -------------------------------------------------------


def test_explicit_address_wins(monkeypatch):
    monkeypatch.setenv("X402_PAY_TO", "0xenv")
    assert resolve_pay_to("0xexplicit") == "0xexplicit"


def test_environment_is_the_second_choice(monkeypatch):
    monkeypatch.setenv("X402_PAY_TO", "0xfromenv")
    assert resolve_pay_to() == "0xfromenv"


def test_without_config_it_refuses_and_says_exactly_what_to_do(monkeypatch):
    # The repo ships payout.example.json and NO payout.json, so this is the
    # real, current state: the service cannot take money yet, and says so.
    monkeypatch.delenv("X402_PAY_TO", raising=False)
    with pytest.raises(ConfigError) as exc:
        resolve_pay_to()
    message = str(exc.value)
    assert "payout.example.json" in message
    assert "payout.json" in message
    assert "gitignored" in message


def test_no_silent_fallback_address_exists(monkeypatch):
    # A default address would mean settling someone else's money. There must be
    # no code path that produces one.
    monkeypatch.delenv("X402_PAY_TO", raising=False)
    with pytest.raises(ConfigError):
        load_settings(network="base-sepolia")


# --- tunables ----------------------------------------------------------------


def test_timeout_and_ttl_defaults():
    s = load_settings(network="base-sepolia", pay_to=FAKE_PAY_TO)
    assert s.payment_timeout_seconds == 60
    assert s.upstream_cache_ttl_seconds == 3600


def test_tunables_are_overridable(monkeypatch):
    monkeypatch.setenv("X402_PAYMENT_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("X402_UPSTREAM_CACHE_TTL_SECONDS", "60")
    s = load_settings(network="base-sepolia", pay_to=FAKE_PAY_TO)
    assert s.payment_timeout_seconds == 120
    assert s.upstream_cache_ttl_seconds == 60


def test_nonsense_tunable_is_rejected_not_ignored(monkeypatch):
    monkeypatch.setenv("X402_PAYMENT_TIMEOUT_SECONDS", "soon")
    with pytest.raises(ConfigError, match="must be an integer"):
        load_settings(network="base-sepolia", pay_to=FAKE_PAY_TO)


def test_nonpositive_tunable_is_rejected(monkeypatch):
    monkeypatch.setenv("X402_UPSTREAM_CACHE_TTL_SECONDS", "0")
    with pytest.raises(ConfigError, match="must be positive"):
        load_settings(network="base-sepolia", pay_to=FAKE_PAY_TO)


def test_base_url_trailing_slash_is_trimmed():
    s = load_settings(network="base-sepolia", pay_to=FAKE_PAY_TO, base_url="https://x.test/")
    assert s.base_url == "https://x.test"
