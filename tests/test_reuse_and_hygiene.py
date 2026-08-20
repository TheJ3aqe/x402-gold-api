"""Two things that are easy to break silently:

  1. the CFTC layer is REUSED from the Apify actor, not copied
  2. no wallet address, key or token has crept into the tree
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from x402api import cot_source
from x402api.cot_source import ACTOR_SRC, CotSource, Thresholds

from conftest import FakeCftcClient, make_history, make_row

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# --- reuse -------------------------------------------------------------------


def test_the_shared_layer_is_loaded_from_the_apify_actor_not_copied_here():
    # If someone "fixes" an import by copying cftc.py in here, this fails.
    assert ACTOR_SRC.name == "src"
    assert ACTOR_SRC.parent.name == "apify-cot-analytics"
    assert (ACTOR_SRC / "analytics.py").exists()
    assert not (PROJECT_ROOT / "x402api" / "analytics.py").exists()
    assert not (PROJECT_ROOT / "x402api" / "cftc.py").exists()
    assert not (PROJECT_ROOT / "x402api" / "markets.py").exists()


def test_shared_modules_resolve_under_the_alias():
    assert cot_source.analytics.__name__ == "cot_core.analytics"
    assert cot_source.markets.__name__ == "cot_core.markets"
    # The alias exists precisely so the actor's package name `src` does not
    # collide with anything else on the path.
    assert cot_source.pipeline.__name__ == "cot_core.pipeline"


def test_the_curated_market_catalog_comes_through():
    catalog = CotSource.catalog()
    by_code = {m["code"]: m for m in catalog}
    assert by_code["088691"]["label"].startswith("Gold")
    assert by_code["088691"]["preferredReport"] == "disaggregated"
    assert by_code["099741"]["preferredReport"] == "tff"
    assert len(catalog) == 34


def test_symbol_aliases_resolve():
    resolved = CotSource.resolve(["XAUUSD"])
    assert resolved[0].code == "088691"


def test_presets_expand():
    assert len(CotSource.resolve(["metals"])) == 5


def test_raw_cftc_codes_pass_through():
    assert CotSource.resolve(["088691"])[0].code == "088691"


def test_unknown_symbol_raises_with_suggestions():
    from x402api.cot_source import UnknownMarketError

    with pytest.raises(UnknownMarketError, match="Unknown market"):
        CotSource.resolve(["ZZZNOTAMARKET"])


# --- the reused analytics still compute what we sell -------------------------


def test_analytics_produce_the_derived_metrics_the_api_charges_for():
    source = CotSource(client=FakeCftcClient())
    batches, errors = source.analyze(["GOLD"], weeks=1, lookback_weeks=156)
    record = batches[0][0]
    assert errors == []
    for key in ("net", "netChange", "cotIndex", "netPercentile", "netZScore", "positioning"):
        assert key in record, key
    assert record["net"] == record["groups"]["managedMoney"]["net"]


def test_rising_net_puts_the_newest_week_at_the_top_of_its_range():
    # make_history trends the net upward toward the newest week, so the COT
    # Index of the newest record must be 100.
    source = CotSource(client=FakeCftcClient(rows=make_history(200)))
    batches, _ = source.analyze(["GOLD"], weeks=1, lookback_weeks=156)
    assert batches[0][0]["cotIndex"] == 100.0
    assert batches[0][0]["positioning"] == "extreme_long"


def test_thresholds_actually_move_the_classification():
    rows = make_history(200)
    source = CotSource(client=FakeCftcClient(rows=rows))
    strict = Thresholds(extreme_long=101.0, stretched_long=99.0, stretched_short=25.0, extreme_short=10.0)
    batches, _ = source.analyze(["GOLD"], weeks=1, lookback_weeks=156, thresholds=strict)
    # Index is 100; with extreme_long raised above 100 it can no longer be extreme.
    assert batches[0][0]["positioning"] == "stretched_long"


def test_total_upstream_failure_raises_so_the_caller_is_not_charged():
    from x402api.cot_source import CftcError

    source = CotSource(client=FakeCftcClient(error=CftcError("HTTP 503")))
    with pytest.raises(CftcError, match="No requested market"):
        source.analyze(["GOLD"], weeks=1)


def test_partial_failure_still_returns_what_worked():
    class Flaky(FakeCftcClient):
        def fetch_history(self, contract_code, report="legacy", combined=False, limit=200):
            from x402api.cot_source import CftcError

            if contract_code == "084691":  # Silver
                raise CftcError("no rows")
            return super().fetch_history(contract_code, report, combined, limit)

    source = CotSource(client=Flaky())
    batches, errors = source.analyze(["GOLD", "SILVER"], weeks=1)
    assert len(batches) == 1
    assert len(errors) == 1
    assert "Silver" in errors[0]["market"]


def test_cached_client_only_hits_upstream_once_per_key():
    from x402api.cot_source import CachedCftcClient

    calls = []

    class Counting(CachedCftcClient):
        def _get(self, url):
            calls.append(url)
            return make_history(20)

    client = Counting(ttl_seconds=3600)
    client.fetch_history("088691", report="legacy", limit=20)
    client.fetch_history("088691", report="legacy", limit=20)
    assert len(calls) == 1
    assert client.cache_stats()["entries"] == 1


def test_cache_distinguishes_different_requests():
    from x402api.cot_source import CachedCftcClient

    calls = []

    class Counting(CachedCftcClient):
        def _get(self, url):
            calls.append(url)
            return make_history(20)

    client = Counting(ttl_seconds=3600)
    client.fetch_history("088691", report="legacy", limit=20)
    client.fetch_history("084691", report="legacy", limit=20)
    client.fetch_history("088691", report="tff", limit=20)
    assert len(calls) == 3


def test_expired_cache_entry_refetches():
    from x402api.cot_source import CachedCftcClient

    calls = []

    class Counting(CachedCftcClient):
        def _get(self, url):
            calls.append(url)
            return make_history(20)

    client = Counting(ttl_seconds=0)
    client.fetch_history("088691", limit=20)
    client.fetch_history("088691", limit=20)
    assert len(calls) == 2


def test_cftc_column_typos_are_still_mirrored():
    # The CFTC really does ship "noncomm_postions_spread_all" (missing an i).
    # If the shared layer ever "fixes" the spelling, this catches it.
    row = make_row("2026-08-11", nc_long=1, nc_short=1)
    assert "noncomm_postions_spread_all" in row
    legacy = cot_source.analytics.GROUPS["legacy"]["nonCommercial"]["spread"]
    assert "noncomm_postions_spread_all" in legacy


# --- hygiene -----------------------------------------------------------------

SOURCE_FILES = [
    p
    for p in PROJECT_ROOT.rglob("*")
    if p.is_file()
    and p.suffix in {".py", ".json", ".md", ".txt", ".ini"}
    and "__pycache__" not in p.parts
    and ".pytest_cache" not in p.parts
    and "data" not in p.parts
]

# A 40-hex-character EVM address. The test-only placeholders are all runs of a
# single repeated byte (0xdedede..., 0x1111...), which no real wallet is.
EVM_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\b")

# Public, well-known token contracts that MUST appear -- clients cannot sign a
# transfer without them. These are not secrets.
ALLOWED_ADDRESSES = {
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC on Base mainnet
    "0x036cbd53842c5426634e7929541ec2318f3dcf7e",  # USDC on Base Sepolia
    # Example addresses copied verbatim from the x402 spec, used in docs only.
    "0x209693bc6afc0c5328ba36faf03c514ef312287c",
    "0x857b06519e91e3a54538791bdbb0e22373e36b66",
}


def _is_obvious_placeholder(address: str) -> bool:
    body = address[2:].lower()
    return len(set(body)) <= 2


def test_no_real_wallet_address_anywhere_in_the_tree():
    offenders = []
    for path in SOURCE_FILES:
        for match in EVM_ADDRESS.findall(path.read_text(encoding="utf-8", errors="replace")):
            if match.lower() in ALLOWED_ADDRESSES or _is_obvious_placeholder(match):
                continue
            offenders.append(f"{path.relative_to(PROJECT_ROOT)}: {match}")
    assert not offenders, "Unexpected address-like strings: " + "; ".join(offenders)


def test_no_private_key_or_seed_material():
    # 64 hex chars = a raw EVM private key. Nonces in test fixtures are runs of
    # a single repeated byte and are excluded by the placeholder check.
    pattern = re.compile(r"\b(?:0x)?[0-9a-fA-F]{64}\b")
    offenders = []
    for path in SOURCE_FILES:
        for match in pattern.findall(path.read_text(encoding="utf-8", errors="replace")):
            body = match[2:] if match.startswith("0x") else match
            if len(set(body.lower())) <= 2:
                continue
            offenders.append(f"{path.relative_to(PROJECT_ROOT)}: {match[:12]}...")
    assert not offenders, "Possible key material: " + "; ".join(offenders)


def test_no_cdp_credential_literals():
    # Only the env var NAMES may appear, never a value assigned to them.
    pattern = re.compile(r"CDP_API_KEY_(?:ID|SECRET)\s*=\s*[\"'][^\"'<]{6,}[\"']")
    offenders = [
        str(p.relative_to(PROJECT_ROOT))
        for p in SOURCE_FILES
        if pattern.search(p.read_text(encoding="utf-8", errors="replace"))
    ]
    assert not offenders, f"Hardcoded CDP credential in: {offenders}"


def test_the_settlement_journal_is_gitignored():
    ignored = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "data/" in ignored
