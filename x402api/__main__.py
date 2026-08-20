"""`python -m x402api` -- start the server, or inspect the config without one.

    python -m x402api                 serve
    python -m x402api --check         print the resolved config and exit

--check is the fast way to find out what is still missing before go-live: it
reports the network, the facilitator, whether a payout address is configured and
whether a CDP credential is present, without needing any of them to be.
"""

from __future__ import annotations

import argparse
import os
import sys

from .config import ENV_CDP_KEY_ID, ENV_CDP_KEY_SECRET, ConfigError, load_settings
from .pricing import fee_share_pct, format_usd, load_prices


def _check() -> int:
    print("x402 Gold & FX API -- configuration check\n")
    prices = load_prices()
    for name, price in sorted(prices.items()):
        print(
            f"  price  {name:<10} {format_usd(price.base_micro_usd)}"
            f" + {format_usd(price.per_extra_market_micro_usd)}/extra market"
            f"   (facilitator fee = {fee_share_pct(price.base_micro_usd)}% of ticket)"
        )
    print()
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"  [BLOCKED] {exc}")
        return 1
    print(f"  network      {settings.network.key} ({settings.network.caip2})")
    print(f"  facilitator  {settings.facilitator_url}")
    print(f"  payTo        configured (address deliberately not printed)")
    if settings.requires_cdp_credentials:
        have = all(os.environ.get(v) for v in (ENV_CDP_KEY_ID, ENV_CDP_KEY_SECRET))
        print(
            f"  CDP key      {'present' if have else 'MISSING -- mainnet will refuse to start'}"
        )
    else:
        print("  CDP key      not needed (testnet facilitator is unauthenticated)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="x402api")
    parser.add_argument("--check", action="store_true", help="print config and exit")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8402)
    args = parser.parse_args(argv)

    if args.check:
        return _check()

    import uvicorn

    from .app import create_app

    try:
        app = create_app()
    except ConfigError as exc:
        print(f"Refusing to start: {exc}", file=sys.stderr)
        return 1
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
