"""Gold & FX positioning data, sold per call over the x402 payment protocol.

Layout:
  config.py      runtime settings (env + common/payout.py, no secrets in code)
  pricing.py     the price table and the fee arithmetic that justifies it
  fx.py          USD -> EUR conversion for the tax log (placeholder, swappable)
  taxlog.py      append-only settlement journal (German bookkeeping requirement)
  cot_source.py  adapter onto the existing CFTC layer in income/apify-cot-analytics
  crossmarket.py cross-market derived analytics (the part raw data cannot give)
  payments/      the x402 protocol implementation (wire types, facilitator, gate)
  app.py         the FastAPI application
"""

__version__ = "1.0.0"
