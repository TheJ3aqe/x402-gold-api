"""The Bazaar listing manifest, and that it cannot drift from the price table."""

from __future__ import annotations

import json

import pytest

from x402api.config import NETWORKS
from x402api.manifest import (
    ManifestError,
    bazaar_extension,
    document,
    load_manifest,
    output_schema,
    route_entry,
    validate,
)
from x402api.pricing import RoutePrice, load_prices


@pytest.fixture
def manifest():
    return load_manifest()


def test_shipped_manifest_loads(manifest):
    assert manifest["service"]["name"]
    assert manifest["service"]["category"] == "market-data"
    assert len(manifest["routes"]) == 4


def test_shipped_manifest_matches_the_shipped_prices(manifest, prices):
    validate(manifest, prices)  # must not raise


def test_every_route_has_the_fields_a_listing_needs(manifest):
    for route in manifest["routes"]:
        assert route["id"] and route["method"] and route["path"]
        assert route["title"] and route["description"]
        assert route["discoverable"] is True


def test_manifest_declares_the_free_routes(manifest):
    paths = {r["path"] for r in manifest["freeRoutes"]}
    assert {"/v1/markets", "/health", "/.well-known/x402"} <= paths


def test_manifest_names_its_upstream_source(manifest):
    upstream = manifest["service"]["upstreamSource"]
    assert "cftc.gov" in upstream["url"]
    assert upstream["updateCadence"]


def test_prices_are_not_duplicated_into_the_manifest_file(manifest):
    # Single source of truth: a price written here too would be free to drift
    # from the one that actually charges.
    raw = json.dumps(manifest["routes"])
    assert '"price"' not in raw


# --- drift detection ---------------------------------------------------------


def test_a_priced_route_missing_from_the_manifest_is_caught(manifest, prices):
    extra = dict(prices)
    extra["ghost"] = RoutePrice("ghost", 10_000, 0, "not listed anywhere")
    with pytest.raises(ManifestError, match="priced but not listed"):
        validate(manifest, extra)


def test_a_listed_route_with_no_price_is_caught(manifest, prices):
    broken = json.loads(json.dumps(manifest))
    broken["routes"].append(
        {"id": "phantom", "method": "GET", "path": "/x", "title": "t", "description": "d"}
    )
    with pytest.raises(ManifestError, match="listed but not priced"):
        validate(broken, prices)


def test_a_route_without_an_id_is_caught(manifest, prices):
    broken = json.loads(json.dumps(manifest))
    del broken["routes"][0]["id"]
    with pytest.raises(ManifestError, match="needs an 'id'"):
        validate(broken, prices)


def test_a_route_missing_a_description_is_caught(manifest, prices):
    broken = json.loads(json.dumps(manifest))
    broken["routes"][0]["description"] = ""
    with pytest.raises(ManifestError, match="missing 'description'"):
        validate(broken, prices)


def test_missing_manifest_file_is_an_actionable_error(tmp_path):
    with pytest.raises(ManifestError, match="No listing manifest"):
        load_manifest(tmp_path / "gone.json")


def test_malformed_manifest_file_is_rejected(tmp_path):
    path = tmp_path / "m.json"
    path.write_text("{nope", encoding="utf-8")
    with pytest.raises(ManifestError, match="not valid JSON"):
        load_manifest(path)


def test_manifest_without_routes_is_rejected(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(json.dumps({"service": {}}), encoding="utf-8")
    with pytest.raises(ManifestError, match="'routes'"):
        load_manifest(path)


# --- discovery payloads ------------------------------------------------------


def test_bazaar_extension_shape(manifest):
    ext = bazaar_extension(manifest, "snapshot")
    assert ext["discoverable"] is True
    assert "market" in ext["inputSchema"]["queryParams"]


def test_output_schema_carries_the_v1_discovery_convention(manifest):
    schema = output_schema(manifest, "compare")
    assert schema["input"]["type"] == "http"
    assert schema["input"]["method"] == "GET"
    assert schema["input"]["discoverable"] is True
    assert "markets" in schema["input"]["queryParams"]


def test_unknown_route_lookup_raises(manifest):
    with pytest.raises(ManifestError, match="No manifest entry"):
        route_entry(manifest, "nope")


# --- served document ---------------------------------------------------------


def test_document_injects_live_prices(manifest, prices):
    doc = document(manifest, prices, base_url="https://api.test", network=NETWORKS["base"])
    snapshot = next(r for r in doc["routes"] if r["id"] == "snapshot")
    assert snapshot["price"]["baseUsd"] == "$0.010"
    assert snapshot["price"]["baseAtomic"] == "10000"
    assert snapshot["price"]["facilitatorFeeSharePct"] == 10.0
    assert snapshot["url"] == "https://api.test/v1/cot/snapshot"


def test_document_reflects_a_price_override(manifest):
    prices = load_prices()
    prices["snapshot"] = RoutePrice("snapshot", 50_000, 0, "pricier")
    doc = document(manifest, prices, base_url="https://api.test", network=NETWORKS["base"])
    snapshot = next(r for r in doc["routes"] if r["id"] == "snapshot")
    assert snapshot["price"]["baseUsd"] == "$0.050"
    assert snapshot["price"]["facilitatorFeeSharePct"] == 2.0


def test_document_states_the_network_and_asset(manifest, prices):
    doc = document(manifest, prices, base_url="https://api.test", network=NETWORKS["base"])
    assert doc["x402"]["network"] == "base"
    assert doc["x402"]["networkCaip2"] == "eip155:8453"
    assert doc["x402"]["asset"]["decimals"] == 6
    assert doc["x402"]["isTestnet"] is False


def test_document_marks_testnet(manifest, prices):
    doc = document(manifest, prices, base_url="https://x", network=NETWORKS["base-sepolia"])
    assert doc["x402"]["isTestnet"] is True
