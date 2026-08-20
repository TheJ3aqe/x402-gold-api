"""Thin client for the CFTC public reporting API (Socrata).

Source: https://publicreporting.cftc.gov -- the CFTC's own open-data portal.
It is free, documented, requires no key and no login, and carries weekly
Commitments of Traders data back to 1986 (legacy) / 2006 (disaggregated, TFF).

Using the official API rather than scraping cot HTML pages is the whole point:
no Cloudflare, no layout drift, no terms-of-service grey zone.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

log = logging.getLogger(__name__)

BASE = "https://publicreporting.cftc.gov/resource"

# Socrata dataset ids, verified live 2026-08-17.
DATASETS: dict[str, dict[str, str]] = {
    "legacy": {
        "futures_only": "6dca-aqww",
        "combined": "jun7-fc8e",
        "title": "Legacy (Non-Commercial / Commercial / Non-Reportable)",
    },
    "disaggregated": {
        "futures_only": "72hh-3qpy",
        # The disaggregated combined view is not exposed as a separate resource;
        # fall back to futures-only so the input never dead-ends.
        "combined": "72hh-3qpy",
        "title": "Disaggregated (Producer/Merchant, Swap, Managed Money, Other)",
    },
    "tff": {
        "futures_only": "gpe5-46if",
        "combined": "gpe5-46if",
        "title": "Traders in Financial Futures (Dealer, Asset Manager, Leveraged Money)",
    },
}

USER_AGENT = "apify-cot-analytics/1.0 (+https://apify.com)"


class CftcError(RuntimeError):
    """Upstream CFTC API failed in a way the caller should see."""


@dataclass
class CftcClient:
    timeout: int = 45
    retries: int = 3
    backoff: float = 1.5
    app_token: str | None = None  # optional Socrata token, raises rate limits

    def _get(self, url: str) -> list[dict]:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if self.app_token:
            headers["X-App-Token"] = self.app_token
        last: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                body = ""
                try:
                    body = exc.read().decode("utf-8", "replace")[:300]
                except Exception:  # noqa: BLE001 - diagnostics only
                    pass
                last = CftcError(f"CFTC API HTTP {exc.code}: {body}")
                # 4xx other than 429 will not fix themselves on retry.
                if exc.code != 429 and 400 <= exc.code < 500:
                    raise last from exc
            except Exception as exc:  # noqa: BLE001 - network layer is broad
                last = CftcError(f"CFTC API request failed: {exc!r}")
            if attempt < self.retries:
                sleep_for = self.backoff**attempt
                log.warning(
                    "CFTC request failed (attempt %s/%s), retrying in %.1fs: %s",
                    attempt,
                    self.retries,
                    sleep_for,
                    last,
                )
                time.sleep(sleep_for)
        raise last or CftcError("CFTC API request failed for an unknown reason.")

    def fetch_history(
        self,
        contract_code: str,
        report: str = "legacy",
        combined: bool = False,
        limit: int = 200,
    ) -> list[dict]:
        """Return up to `limit` weekly rows for one contract, newest first."""
        family = DATASETS.get(report)
        if family is None:
            raise CftcError(
                f"Unknown report type {report!r}; expected one of {sorted(DATASETS)}."
            )
        dataset = family["combined" if combined else "futures_only"]
        qs = urllib.parse.urlencode(
            {
                "$where": f"cftc_contract_market_code='{contract_code}'",
                "$order": "report_date_as_yyyy_mm_dd DESC",
                "$limit": max(1, int(limit)),
            }
        )
        rows = self._get(f"{BASE}/{dataset}.json?{qs}")
        if not rows:
            raise CftcError(
                f"CFTC returned no rows for contract {contract_code!r} in the "
                f"{report!r} report. This contract may not be covered by that "
                f"report family -- try report='legacy'."
            )
        return rows

    def latest_report_date(self, report: str = "legacy") -> str | None:
        """Most recent report date present in a dataset (ISO date, no time)."""
        family = DATASETS.get(report)
        if family is None:
            return None
        qs = urllib.parse.urlencode(
            {
                "$select": "report_date_as_yyyy_mm_dd",
                "$order": "report_date_as_yyyy_mm_dd DESC",
                "$limit": 1,
            }
        )
        rows = self._get(f"{BASE}/{family['futures_only']}.json?{qs}")
        if not rows:
            return None
        return str(rows[0]["report_date_as_yyyy_mm_dd"])[:10]
