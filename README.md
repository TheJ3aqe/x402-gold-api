# Gold & FX Positioning API

**Weekly CFTC Commitments-of-Traders positioning for gold, FX majors, metals, energy, indices and crypto — scored, ranked and cross-referenced. Paid per call in USDC over [x402](https://x402.org). No account, no API key, no subscription.**

An AI agent that needs to know how crowded gold is right now should be able to ask, pay a cent, and get an answer — in one HTTP round trip, with no signup flow, no billing page and no human in the loop. That is exactly what this API is.

---

## Why not just read the CFTC data yourself?

You can. It is free and public. But the raw report is 200+ columns of position counts, and none of them tell you anything on their own. What matters is **positioning relative to its own history**, and that takes three years of weekly data plus knowing which trader group is the signal in which market.

Every response here already contains:

| Field | What it tells you |
|---|---|
| `net`, `netChange`, `netChangePct` | Net speculative exposure and how it moved this week |
| `cotIndex` | **Williams COT Index** — where the current net sits inside its own multi-year range, 0–100 |
| `netPercentile`, `netZScore` | Two independent reads on how unusual the current net is |
| `netPctOfOpenInterest` | Position size normalised against the whole market |
| `positioning` | `extreme_long` / `stretched_long` / `neutral` / `stretched_short` / `extreme_short` |
| `flowDirection` | `accumulating` / `distributing` — is the position still building, or unwinding? |
| `groups` | Full breakdown per trader class, including the hedger counterposition |

And the report family is chosen per market automatically: **Managed Money** for commodities, **Leveraged Money** for FX and indices — the group that actually carries the speculative signal, not whichever one the raw file lists first.

### The part you cannot get from raw data at all

The cross-market routes compute things that only exist across markets:

- **Stretch ranking** — which market in your set is the most one-sided right now.
- **Consensus regime** — is positioning *clustered* across the complex or *scattered*? Measured as dispersion of the COT Index against the 28.87 benchmark of a uniform random spread, so "no signal" is a number, not an opinion.
- **A single speculative US-dollar score** — CME currency futures are quoted as the *foreign* currency against USD, so a long Euro future is a short-dollar position. Each contract is sign-corrected before averaging, and the ICE Dollar Index enters directly. Six unrelated contracts become one dollar reading. (Gold is deliberately excluded — its dollar correlation is real but unstable.)
- **Divergences** — crowded positions where the weekly flow has *already turned against* the extreme. An extreme still building is a trend; an extreme unwinding is the interesting one.

Data comes from the CFTC's own open-data API (`publicreporting.cftc.gov`). No scraping, no HTML parsing, no layout drift, no terms-of-service grey zone.

---

## Endpoints

### Paid

| Route | Price | What you get |
|---|---|---|
| `GET /v1/cot/snapshot?market=GOLD` | **$0.010** | Latest fully-enriched positioning for one market |
| `GET /v1/cot/history?market=GOLD&weeks=52` | **$0.030** | Up to 520 enriched weekly records, backtest-safe |
| `GET /v1/cot/compare?markets=GOLD,SILVER,EURUSD` | **$0.010** + $0.008/extra market | Ranking, consensus, dollar score, divergences |
| `GET /v1/cot/extremes?group=metals` | **$0.020** + $0.004/extra market | Screener over a group or the full 34-market catalog |

A full 34-market sweep via `/v1/cot/extremes?group=all` costs **$0.152** — versus $0.34 for the same coverage one snapshot at a time.

### Free

| Route | Purpose |
|---|---|
| `GET /` | Service overview and how the payment flow works |
| `GET /v1/markets` | Full symbol catalog, aliases and presets — discover what you can buy before paying |
| `GET /health` | Liveness, network, per-route fee share |
| `GET /.well-known/x402` | Machine-readable manifest with live prices |

**Markets:** 34 curated contracts — Gold, Silver, Platinum, Palladium, Copper, all FX majors plus DXY, WTI, NatGas, S&P/Nasdaq/Russell/DJIA/VIX, BTC/ETH, US Treasuries and the main ags. Addressable by trader-friendly alias (`GOLD`, `XAUUSD`, `EURUSD`, `BTC`) or by preset (`metals`, `fx`, `energy`, `indices`, `crypto`, `rates`, `ags`). **Any** raw 6-character CFTC contract code also works, so roughly 350 contracts are reachable.

---

## How to pay

Standard x402. **Both protocol versions are supported** — v1 (`X-PAYMENT`) and v2 (`PAYMENT-SIGNATURE`) — so whichever client library you use will work.

**1. Call without payment.** You get `402 Payment Required` carrying the exact price:

```bash
curl -i https://your-host/v1/cot/snapshot?market=GOLD
```

```json
{
  "x402Version": 1,
  "error": "Payment required. Send a signed x402 payment in the PAYMENT-SIGNATURE header (v2) or X-PAYMENT header (v1).",
  "accepts": [{
    "scheme": "exact",
    "network": "base",
    "maxAmountRequired": "10000",
    "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "payTo": "0x…",
    "resource": "https://your-host/v1/cot/snapshot",
    "maxTimeoutSeconds": 60,
    "extra": { "name": "USD Coin", "version": "2" }
  }]
}
```

The same requirements are also returned base64-encoded in the `PAYMENT-REQUIRED` header for v2 clients.

**2. Sign it and call again.** Amounts are atomic USDC units — `"10000"` is $0.01 (USDC has 6 decimals).

**3. You get the data**, plus the settlement receipt in `X-PAYMENT-RESPONSE` (v1) or `PAYMENT-RESPONSE` (v2) containing the on-chain transaction hash.

Any x402 client handles all of this for you. With the official SDK it is one wrapper around your HTTP client.

### What you are never charged for

- **Unknown market** → `400`, no payment attempted.
- **CFTC upstream down** → `502` with `"charged": false`. Payment is verified *before* the data is fetched but only **settled after it succeeds**, so a failed request costs you nothing.
- **Free routes** → always free, no 402.

---

## Running it

```bash
pip install -r requirements.txt

# Config check — prints prices and tells you exactly what is still missing
python -m x402api --check

# Serve
python -m x402api --host 0.0.0.0 --port 8402
# or: uvicorn x402api.app:build_default_app --factory --port 8402
```

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `X402_NETWORK` | `base-sepolia` | `base` for mainnet |
| `X402_BASE_URL` | `http://localhost:8402` | Public URL, used in the `resource` field |
| `X402_FACILITATOR_URL` | auto | Override the facilitator |
| `X402_PAYMENT_TIMEOUT_SECONDS` | `60` | How long a quote stays signable |
| `X402_UPSTREAM_CACHE_TTL_SECONDS` | `3600` | CFTC cache TTL |
| `X402_USD_EUR_RATE` | *(placeholder)* | EUR per 1 USD, for the settlement journal |
| `CDP_API_KEY_ID` / `CDP_API_KEY_SECRET` | — | **Mainnet only.** Required, never defaulted |

Prices are overridable via `config/pricing.json` — but the service will refuse to start if an override drops a route below **$0.01/call** (see *Pricing* below).

### Testing

```bash
pytest            # 242 offline tests, no network
pytest -m live    # 6 more against the real CFTC API
```

---

## Pricing, and why these numbers

The binding constraint is the CDP facilitator fee: **the first 1,000 settlements each month are free, then $0.001 per settlement**. That is a *flat* fee, so the share of revenue it eats depends entirely on ticket size:

| Price per call | Fee share |
|---|---|
| $0.001 | 100% |
| $0.004 | 25% |
| **$0.010** | **10%** |
| $0.030 | 3.3% |

$0.01 is therefore the floor, and it is **enforced in code**, not just documented — `pricing.py` raises on any configuration that would breach it. For comparison, a marketplace like Apify takes a flat 20% commission; every route here stays below that.

Multi-market routes price a base plus a marginal amount per additional market, because they do one upstream fetch and one analytics pass per market. The marginal market in `/compare` is 20% cheaper than a separate snapshot, and in `/extremes` 60% cheaper — the screener returns less per market, so it costs less per market.

---

## Going live

The service is complete and tested. Three things are still needed, all of them one-time account setup rather than engineering:

**1. A receiving wallet address (~2 minutes).**
Copy `config/payout.example.json` → `config/payout.json` (repo root) and set `platforms.x402.address` to an EVM address. That file is gitignored and is the **only** place an address ever lives — never in code, never in a note. One `0x…` address works for Base, Ethereum and Polygon, so the same one covers other payout rails too.

**2. A CDP API key, for mainnet only (~5 minutes).**
Create one at [portal.cdp.coinbase.com](https://portal.cdp.coinbase.com), export `CDP_API_KEY_ID` and `CDP_API_KEY_SECRET`, and `pip install cdp-sdk` (it mints the request-bound JWT the CDP facilitator expects). Without these the service **refuses to start on mainnet** rather than pretending to work — there is no dummy key and no unauthenticated fallback.

> **You do not need this to develop or test.** Base Sepolia uses `https://x402.org/facilitator`, which requires no credentials at all. The entire payment path — 402, verify, settle, settlement header, journal — runs end to end without a CDP account.

**3. Public HTTPS hosting (~15 minutes).**
Any host works; there is no state to persist beyond the settlement journal. Set `X402_BASE_URL` to the public URL. **Prepared, not executed:** `Dockerfile` + `fly.toml` in this repo, plus a Render-vs-Fly cost/cold-start comparison and recommendation, in [`DEPLOY.md`](DEPLOY.md).

Then the **x402 Bazaar** listing is automatic: the catalog indexes what the 402 already advertises. `config/manifest.json` carries the name, description, category, tags and per-route input schemas, and the service marks every paid route `discoverable`. Requirements are public HTTPS plus **one successful paid call through the CDP facilitator** (a testnet call does not register it); indexing then takes about 10–15 minutes.

### FX feed for the tax journal

Every settlement is journalled with a EUR valuation, because German bookkeeping values each inflow at its EUR equivalent at the moment it arrives. The default rate is still a **clearly marked placeholder** — every journal line carries `"fxRateSource": "PLACEHOLDER-not-a-market-rate"` and `"fxRateIsPlaceholder": true` unless overridden.

**A live feed exists now** (`EcbRateProvider` in `x402api/fx.py`, free ECB Statistical Data Warehouse API, no key, cached hourly, falls back to the placeholder — loudly logged, still flagged — if the feed is unreachable). Set `X402_FX_PROVIDER=ecb` to use it. **Not the default yet on purpose:** whether a daily reference rate is acceptable for thousands of micro-inflows, or whether German tax practice wants a monthly average instead, is a question for the tax advisor, not something to decide unilaterally. Ask, then either flip the env var default in `fx.py::default_provider()` or wire in a monthly-average provider behind the same one-method `RateProvider` interface — nothing else in the package changes either way.

---

## Design notes

- **The CFTC data layer is reused, not copied.** `income/apify-cot-analytics/src/` already holds a tested, dependency-free CFTC client, market catalog and analytics engine. This API imports it under an alias rather than shipping a second copy that would drift; a test asserts no duplicate exists.
- **Money is never a float.** All amounts are integer atomic units end to end. `0.1 + 0.2 != 0.3` in binary floating point, and an off-by-one-unit mismatch fails signature verification.
- **Verify → work → settle.** Settling before the work would charge for data the upstream might fail to deliver.
- **Nothing is served unpaid.** If settlement fails after the work succeeded, you get a 402 — not the data.
- **The settlement journal is append-only and fsynced**, and a write failure fails the request. An inflow that reached the chain but not the books is the one thing that must never happen quietly.
- **The receiving address is redacted to its last 4 characters** everywhere it could be logged.
- **No look-ahead bias.** Each week in a history response is scored using only data available up to that week.

## Data source & disclaimer

Positioning data: [CFTC Commitments of Traders](https://publicreporting.cftc.gov), US Government public data, published weekly on Friday at 15:30 ET. Provided as-is for informational purposes. **Not investment advice.**
