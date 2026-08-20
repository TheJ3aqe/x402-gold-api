"""Loads config/manifest.json and turns it into what discovery layers expect.

Two consumers, one source:
  * the x402 Bazaar, which indexes whatever a 402 advertises plus a `bazaar`
    extension (v2) or an `outputSchema` block (v1 convention)
  * /.well-known/x402, the human- and agent-readable service document

Prices are injected here from pricing.py rather than stored in the JSON, so a
listed price can never disagree with the price actually charged. validate()
enforces that the two lists of routes match exactly -- a priced route missing
from the manifest would be unsellable-because-undiscoverable, and a manifest
route with no price would advertise something the server refuses to quote.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .pricing import RoutePrice, fee_share_pct, format_usd

MANIFEST_PATH = Path(__file__).resolve().parent.parent / "config" / "manifest.json"


class ManifestError(RuntimeError):
    """The listing manifest is missing, malformed, or out of sync with pricing."""


def load_manifest(path: Path | None = None) -> dict:
    target = MANIFEST_PATH if path is None else Path(path)
    if not target.exists():
        raise ManifestError(
            f"No listing manifest at {target}. The Bazaar entry and "
            "/.well-known/x402 both read it."
        )
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManifestError(f"{target} is not valid JSON: {exc}") from exc
    for key in ("service", "routes"):
        if key not in data:
            raise ManifestError(f"{target} is missing the required '{key}' key.")
    return data


def validate(manifest: dict, prices: dict[str, RoutePrice]) -> None:
    """Fail loudly when the manifest and the price table disagree."""
    listed = {r.get("id") for r in manifest.get("routes", [])}
    if None in listed:
        raise ManifestError("Every entry in manifest 'routes' needs an 'id'.")
    priced = set(prices)
    if listed != priced:
        missing = sorted(priced - listed)
        extra = sorted(listed - priced)
        problems = []
        if missing:
            problems.append(f"priced but not listed: {', '.join(missing)}")
        if extra:
            problems.append(f"listed but not priced: {', '.join(extra)}")
        raise ManifestError(
            "Manifest routes and the price table are out of sync (" +
            "; ".join(problems) + ")."
        )
    for route in manifest["routes"]:
        for key in ("method", "path", "title", "description"):
            if not route.get(key):
                raise ManifestError(
                    f"Route {route.get('id')!r} is missing '{key}' in the manifest."
                )


def route_entry(manifest: dict, route_id: str) -> dict:
    for route in manifest.get("routes", []):
        if route.get("id") == route_id:
            return route
    raise ManifestError(f"No manifest entry for route {route_id!r}.")


def bazaar_extension(manifest: dict, route_id: str) -> dict:
    """The v2 `extensions.bazaar` block for one route."""
    route = route_entry(manifest, route_id)
    return {
        "discoverable": bool(route.get("discoverable", True)),
        "inputSchema": {"queryParams": route.get("queryParams", {})},
        "outputSchema": {"type": "object"},
    }


def output_schema(manifest: dict, route_id: str) -> dict:
    """The v1 `outputSchema` convention carrying the same discovery metadata."""
    route = route_entry(manifest, route_id)
    return {
        "input": {
            "type": "http",
            "method": route.get("method", "GET"),
            "discoverable": bool(route.get("discoverable", True)),
            "queryParams": route.get("queryParams", {}),
        },
        "output": {"type": "object"},
    }


def document(
    manifest: dict, prices: dict[str, RoutePrice], *, base_url: str, network
) -> dict:
    """The full /.well-known/x402 document, with live prices folded in."""
    routes: list[dict[str, Any]] = []
    for route in manifest.get("routes", []):
        price = prices[route["id"]]
        entry = dict(route)
        entry["price"] = {
            "baseAtomic": str(price.base_micro_usd),
            "baseUsd": format_usd(price.base_micro_usd),
            "perExtraMarketAtomic": str(price.per_extra_market_micro_usd),
            "perExtraMarketUsd": format_usd(price.per_extra_market_micro_usd),
            "asset": "USDC",
            "assetAddress": network.usdc_address,
            "network": network.key,
            "networkCaip2": network.caip2,
            "facilitatorFeeSharePct": fee_share_pct(price.base_micro_usd),
        }
        entry["url"] = f"{base_url}{route['path']}"
        routes.append(entry)

    return {
        "x402": {
            "versionsSupported": [1, 2],
            "scheme": "exact",
            "network": network.key,
            "networkCaip2": network.caip2,
            "asset": {"symbol": "USDC", "address": network.usdc_address, "decimals": 6},
            "isTestnet": network.is_testnet,
        },
        "service": manifest["service"],
        "routes": routes,
        "freeRoutes": manifest.get("freeRoutes", []),
    }
