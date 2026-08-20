"""The settlement journal: append-only, EUR-valued, redacted, and loud on failure."""

from __future__ import annotations

import json

import pytest

from x402api.fx import (
    ECB_SOURCE_PREFIX,
    PLACEHOLDER_SOURCE,
    PLACEHOLDER_USD_EUR,
    EcbRateProvider,
    FixedRateProvider,
    Rate,
    default_provider,
)
from x402api.taxlog import TaxLog, TaxLogError, _redact

from conftest import FAKE_PAY_TO, FAKE_PAYER, FAKE_TX


def record(log: TaxLog, **overrides):
    kwargs = dict(
        route="snapshot",
        resource="https://api.example.test/v1/cot/snapshot",
        amount_atomic=10_000,
        network="base-sepolia",
        asset="0x036CbD53842c5426634e7929541eC2318f3dCF7e",
        transaction_hash=FAKE_TX,
        payer=FAKE_PAYER,
        pay_to=FAKE_PAY_TO,
        facilitator="https://x402.org/facilitator",
        x402_version=1,
    )
    kwargs.update(overrides)
    return log.record(**kwargs)


# --- FX ----------------------------------------------------------------------


def test_placeholder_rate_is_flagged_as_such():
    rate = FixedRateProvider().usd_to_eur()
    assert rate.value == PLACEHOLDER_USD_EUR
    assert rate.is_placeholder
    # The marker must literally say it is not a market rate, so a grep finds
    # every line that will need restating once a real feed exists.
    assert "not-a-market-rate" in rate.source


def test_env_rate_overrides_the_placeholder_and_is_not_flagged(monkeypatch):
    monkeypatch.setenv("X402_USD_EUR_RATE", "0.9123")
    rate = FixedRateProvider().usd_to_eur()
    assert rate.value == 0.9123
    assert not rate.is_placeholder
    assert rate.source == "env:X402_USD_EUR_RATE"


def test_explicit_rate_beats_the_environment(monkeypatch):
    monkeypatch.setenv("X402_USD_EUR_RATE", "0.5")
    assert FixedRateProvider(rate=0.8).usd_to_eur().value == 0.8


def test_nonsense_env_rate_raises_rather_than_silently_reverting(monkeypatch):
    monkeypatch.setenv("X402_USD_EUR_RATE", "not-a-number")
    with pytest.raises(ValueError, match="EUR per 1 USD"):
        FixedRateProvider().usd_to_eur()


def test_negative_env_rate_rejected(monkeypatch):
    monkeypatch.setenv("X402_USD_EUR_RATE", "-1")
    with pytest.raises(ValueError, match="positive"):
        FixedRateProvider().usd_to_eur()


def test_rate_converts_to_cents():
    assert Rate(value=0.9, source="x").convert(0.01) == 0.01
    assert Rate(value=0.5, source="x").convert(3.0) == 1.5


def test_default_provider_stays_fixed_unless_opted_in(monkeypatch):
    monkeypatch.delenv("X402_FX_PROVIDER", raising=False)
    assert isinstance(default_provider(), FixedRateProvider)


def test_default_provider_switches_to_ecb_when_opted_in(monkeypatch):
    monkeypatch.setenv("X402_FX_PROVIDER", "ecb")
    assert isinstance(default_provider(), EcbRateProvider)


class _FakeEcbResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_ecb_payload(usd_per_eur: float, obs_date: str = "2026-08-18") -> bytes:
    return json.dumps(
        {
            "dataSets": [{"series": {"0:0:0:0:0": {"observations": {"0": [usd_per_eur, 0, 0, None, None]}}}}],
            "structure": {"dimensions": {"observation": [{"values": [{"id": obs_date}]}]}},
        }
    ).encode()


def test_ecb_provider_fetches_and_inverts_the_rate(monkeypatch):
    monkeypatch.setattr(
        "x402api.fx.urllib.request.urlopen",
        lambda req, timeout=None: _FakeEcbResponse(_fake_ecb_payload(1.16)),
    )
    provider = EcbRateProvider()
    rate = provider.usd_to_eur()
    assert not rate.is_placeholder
    assert rate.source == f"{ECB_SOURCE_PREFIX}2026-08-18"
    assert abs(rate.value - (1 / 1.16)) < 1e-6


def test_ecb_provider_caches_within_ttl(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(1)
        return _FakeEcbResponse(_fake_ecb_payload(1.16))

    monkeypatch.setattr("x402api.fx.urllib.request.urlopen", fake_urlopen)
    provider = EcbRateProvider(cache_seconds=3600)
    provider.usd_to_eur()
    provider.usd_to_eur()
    assert len(calls) == 1


def test_ecb_provider_falls_back_loudly_on_failure(monkeypatch, caplog):
    def fake_urlopen(req, timeout=None):
        raise OSError("network is down")

    monkeypatch.setattr("x402api.fx.urllib.request.urlopen", fake_urlopen)
    provider = EcbRateProvider()
    with caplog.at_level("WARNING"):
        rate = provider.usd_to_eur()
    assert rate.value == PLACEHOLDER_USD_EUR
    # Must still count as a placeholder for the journal flag (startswith, see
    # Rate.is_placeholder) even though the source string carries extra detail
    # -- an auditor filtering on the flag must not silently miss this row.
    assert rate.is_placeholder is True
    assert "ecb-fetch-failed" in rate.source
    assert rate.source.startswith(PLACEHOLDER_SOURCE)
    assert any("ECB FX fetch failed" in r.message for r in caplog.records)


# --- journal -----------------------------------------------------------------


def test_records_are_appended_one_json_object_per_line(tax_log):
    record(tax_log, route="snapshot")
    record(tax_log, route="compare", amount_atomic=26_000)
    raw = tax_log.path.read_text(encoding="utf-8").splitlines()
    assert len(raw) == 2
    assert [json.loads(line)["route"] for line in raw] == ["snapshot", "compare"]


def test_appending_never_rewrites_earlier_lines(tax_log):
    record(tax_log, route="first")
    first_line = tax_log.path.read_text(encoding="utf-8").splitlines()[0]
    record(tax_log, route="second")
    assert tax_log.path.read_text(encoding="utf-8").splitlines()[0] == first_line


def test_amount_is_valued_in_usd_and_eur(tmp_path):
    log = TaxLog(path=tmp_path / "j.jsonl", rates=FixedRateProvider(rate=0.9, source="test"))
    entry = record(log, amount_atomic=10_000)
    assert entry.amountUsd == 0.01
    assert entry.amountEur == 0.01  # 0.01 * 0.9 = 0.009 -> rounded to cents
    assert entry.fxRateUsdEur == 0.9
    assert entry.fxRateSource == "test"
    assert entry.fxRateIsPlaceholder is False


def test_larger_amount_converts_correctly(tmp_path):
    log = TaxLog(path=tmp_path / "j.jsonl", rates=FixedRateProvider(rate=0.9, source="test"))
    entry = record(log, amount_atomic=5_000_000)  # $5.00
    assert entry.amountUsd == 5.0
    assert entry.amountEur == 4.5


def test_placeholder_rate_is_recorded_as_placeholder_in_the_journal(tax_log):
    entry = record(tax_log)
    assert entry.fxRateIsPlaceholder is True
    assert entry.fxRateSource == PLACEHOLDER_SOURCE


def test_timestamp_is_utc_iso(tax_log):
    entry = record(tax_log)
    assert entry.timestamp.endswith("+00:00")
    assert "T" in entry.timestamp


def test_payout_address_is_redacted_but_payer_is_kept(tax_log):
    entry = record(tax_log)
    # Kevin's receiving address must never appear in full anywhere.
    assert FAKE_PAY_TO not in tax_log.path.read_text(encoding="utf-8")
    assert entry.payToRedacted == f"...{FAKE_PAY_TO[-4:]}"
    # The counterparty is public chain data and is what makes the entry auditable.
    assert entry.payer == FAKE_PAYER


def test_redact_handles_short_input():
    assert _redact("") == "????"
    assert _redact("0x12") == "????"


def test_transaction_hash_is_recorded_for_independent_verification(tax_log):
    assert record(tax_log).transactionHash == FAKE_TX


def test_atomic_amount_is_stored_as_a_string(tax_log):
    # Atomic units can exceed the JS-safe integer range; a JSON number would be
    # silently mangled by any JavaScript consumer of this journal.
    entry = record(tax_log, amount_atomic=9_007_199_254_740_993)
    assert entry.amountAtomic == "9007199254740993"


def test_read_all_returns_empty_before_anything_is_written(tmp_path):
    assert TaxLog(path=tmp_path / "nothing.jsonl").read_all() == []


def test_read_all_round_trips(tax_log):
    record(tax_log, route="a")
    record(tax_log, route="b")
    assert [r["route"] for r in tax_log.read_all()] == ["a", "b"]


def test_corrupt_line_is_reported_with_its_number(tax_log):
    record(tax_log)
    with open(tax_log.path, "a", encoding="utf-8") as fh:
        fh.write("{ not json\n")
    with pytest.raises(TaxLogError, match="line 2"):
        tax_log.read_all()


def test_summary_totals(tmp_path):
    log = TaxLog(path=tmp_path / "j.jsonl", rates=FixedRateProvider(rate=1.0, source="t"))
    record(log, amount_atomic=10_000)
    record(log, amount_atomic=30_000)
    summary = log.summary()
    assert summary["settlements"] == 2
    assert summary["totalUsd"] == 0.04
    assert summary["totalEur"] == 0.04
    assert summary["recordsWithPlaceholderFxRate"] == 0


def test_summary_counts_placeholder_records(tax_log):
    record(tax_log)
    record(tax_log)
    assert tax_log.summary()["recordsWithPlaceholderFxRate"] == 2


def test_summary_on_an_empty_journal(tmp_path):
    summary = TaxLog(path=tmp_path / "j.jsonl").summary()
    assert summary["settlements"] == 0
    assert summary["firstTimestamp"] is None


def test_summary_never_contains_an_address(tax_log):
    record(tax_log)
    assert FAKE_PAY_TO not in json.dumps(tax_log.summary())


def test_unwritable_journal_raises_instead_of_silently_dropping(tmp_path):
    # A settlement that reached the chain but not the books is exactly what this
    # file exists to prevent, so the failure must be loud.
    blocker = tmp_path / "blocked"
    blocker.write_text("i am a file, not a directory", encoding="utf-8")
    log = TaxLog(path=blocker / "sub" / "j.jsonl")
    with pytest.raises(TaxLogError, match="Refusing to serve"):
        record(log)


def test_journal_creates_its_parent_directory(tmp_path):
    log = TaxLog(path=tmp_path / "deep" / "nested" / "j.jsonl")
    record(log)
    assert log.path.exists()
