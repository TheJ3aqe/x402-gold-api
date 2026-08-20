# Deployment — prepared, not executed

Research + config done 19.08.2026 (autonomous, no account needed for this part).
Actually deploying needs Kevin: every option below requires a new external
account, which is NOVEL per the autonomy rules, not something to sign up for
unattended.

## Comparison (web research 19.08.2026, sources in Daily Note)

| | Free tier | Always-on cost | Cold starts | Verdict |
|---|---|---|---|---|
| **Render** | Yes, real free tier | $7/mo (Starter) | Free tier sleeps after 15 min idle, ~30-50s wake | **Testnet / pre-launch**: zero cost while nobody's paying yet |
| **Fly.io** | 2h trial only | ~$2/mo (shared-cpu-1x, 256MB, 24/7) | None if `min_machines_running=1` | **Mainnet / real traffic**: cheapest true always-on |
| **Railway** | None anymore | $5/mo (Hobby) | None | Good DX, no cost edge over Fly here |

Why cold starts actually matter here and aren't just a UX nitpick: an x402
client signs a payment authorization with a validity window
(`X402_PAYMENT_TIMEOUT_SECONDS`, default 60s). A sleeping instance's wake-up
time eats directly into that window — worst case the signed payment expires
before the server is even up to verify it. Fine to accept while dry-running
on testnet; not fine once a real agent is paying real USDC.

## Recommendation

1. **Now (testnet, `X402_NETWORK=base-sepolia`):** Render free tier. Zero
   cost, zero commitment, proves the whole flow publicly reachable.
2. **At go-live (mainnet, real USDC):** move to Fly.io (`fly.toml` in this
   repo, ~$2/month). Bazaar indexing needs "public HTTPS + one successful
   paid mainnet call" (README) — a sleeping Render instance risks that first
   call timing out during discovery.

## What's prepared

- `Dockerfile` — builds from `income/` (not from `x402-gold-api/` alone),
  because `x402api/cot_source.py` reuses `income/apify-cot-analytics/src/`
  as a sibling package at a fixed relative path. Verified 19.08. by
  replicating the exact container directory layout in a temp folder and
  running `python -m x402api --check` from it — resolved the shared CFTC
  layer correctly and only failed on the (expected, correct) missing payout
  address. Docker Desktop wasn't running to do a real `docker build` tonight
  (deliberately not started — Kevin killed GPU-heavy processes earlier
  tonight to free resources for gaming; starting a new heavy background app
  unattended crosses into "should ask" territory). **Real `docker build`
  still needs doing before the first actual deploy** — this is prep, not
  a substitute for that.
- `fly.toml` — mainnet config, `min_machines_running=1` so it never sleeps.

## What Kevin needs to do (none of this is autonomous)

1. Pick Render or Fly (recommendation: both, in the order above).
2. Create the account (email/GitHub login — a few minutes).
3. `X402_PAY_TO` as a platform secret/env var — **not** a file in the repo;
   `resolve_pay_to()` in `x402api/config.py` checks the env var before it
   ever touches `common/payout.py`, so the cloud deploy doesn't need the
   parent jarvis-rhod repo at all, just this one env var.
4. For mainnet: `CDP_API_KEY_ID` / `CDP_API_KEY_SECRET` as secrets too
   (README "Going live", step 2).
5. `docker build -f x402-gold-api/Dockerfile -t x402-gold-api income/` once
   for real, from `C:\Users\kevin\jarvis-rhod\income\` — then push/deploy
   per the chosen platform's docs.
