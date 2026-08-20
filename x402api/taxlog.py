"""Append-only journal of every settled payment.

Required by the vault note (Jarvis-Einkommensquellen, "Krypto-Zufluss DE",
point 3): per-call settlement means potentially thousands of taxable inflows a
month, each needing a timestamp and a EUR valuation at the moment of inflow.
Retrofitting that after money has moved is painful, so it ships with v1.

Design constraints, all of them deliberate:

  * APPEND-ONLY. One JSON object per line, never rewritten, never re-ordered.
    A bookkeeping journal that can be edited in place is not a journal.
  * NO WALLET ADDRESS IN FULL. Kevin's receiving address is redacted to its
    last 4 characters (same convention as common/payout.py Destination.redacted)
    so a leaked log file cannot leak the destination. The PAYER address is kept
    in full -- it is the counterparty on a public chain, it is what makes the
    entry auditable, and it is not Kevin's secret.
  * WRITE FAILURES ARE LOUD. If the journal cannot be written the request is
    failed rather than silently served. Serving unlogged paid calls would
    produce exactly the untraceable inflows this file exists to prevent.
  * The transaction hash is recorded so any line can be checked against the
    chain independently of this service.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .fx import RateProvider, default_provider
from .pricing import MICRO_PER_USD

DEFAULT_JOURNAL_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "settlements.jsonl"
)

_WRITE_LOCK = threading.Lock()


class TaxLogError(RuntimeError):
    """Raised when a settlement could not be journalled."""


@dataclass(frozen=True)
class SettlementRecord:
    timestamp: str  # ISO-8601 UTC, the moment of inflow
    route: str
    resource: str
    network: str
    asset: str
    amountAtomic: str  # string: atomic units can exceed JS-safe integers
    amountUsd: float
    amountEur: float
    fxRateUsdEur: float
    fxRateSource: str
    fxRateIsPlaceholder: bool
    transactionHash: str
    payer: str
    payToRedacted: str
    facilitator: str
    x402Version: int


def _redact(address: str) -> str:
    """Last 4 characters only -- matches common/payout.py's convention."""
    if not address:
        return "????"
    return f"...{address[-4:]}" if len(address) > 8 else "????"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class TaxLog:
    path: Path = DEFAULT_JOURNAL_PATH
    rates: RateProvider | None = None

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.rates = self.rates or default_provider()

    def record(
        self,
        *,
        route: str,
        resource: str,
        amount_atomic: int,
        network: str,
        asset: str,
        transaction_hash: str,
        payer: str,
        pay_to: str,
        facilitator: str,
        x402_version: int,
        timestamp: str | None = None,
    ) -> SettlementRecord:
        """Value the inflow in EUR and append one immutable line. Raises on failure."""
        rate = self.rates.usd_to_eur()
        amount_usd = round(int(amount_atomic) / MICRO_PER_USD, 6)
        entry = SettlementRecord(
            timestamp=timestamp or _utc_now_iso(),
            route=route,
            resource=resource,
            network=network,
            asset=asset,
            amountAtomic=str(int(amount_atomic)),
            amountUsd=amount_usd,
            amountEur=rate.convert(amount_usd),
            fxRateUsdEur=rate.value,
            fxRateSource=rate.source,
            fxRateIsPlaceholder=rate.is_placeholder,
            transactionHash=transaction_hash,
            payer=payer,
            payToRedacted=_redact(pay_to),
            facilitator=facilitator,
            x402Version=int(x402_version),
        )
        line = json.dumps(asdict(entry), ensure_ascii=False, sort_keys=True)
        try:
            with _WRITE_LOCK:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                # Append + flush + fsync: a settlement that reached the chain but
                # not the disk is the one case that must not survive a crash.
                with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
                    fh.write(line + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
        except OSError as exc:
            raise TaxLogError(
                f"Could not append to the settlement journal at {self.path}: {exc}. "
                "Refusing to serve a paid call that cannot be journalled."
            ) from exc
        return entry

    def read_all(self) -> list[dict]:
        """Every journalled settlement, oldest first. Empty list if never written."""
        if not self.path.exists():
            return []
        out: list[dict] = []
        for lineno, raw in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not raw.strip():
                continue
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError as exc:
                raise TaxLogError(
                    f"{self.path} line {lineno} is not valid JSON: {exc}. "
                    "The journal is append-only -- do not edit it by hand."
                ) from exc
        return out

    def summary(self) -> dict:
        """Totals for a bookkeeping check. Never returns an address."""
        rows = self.read_all()
        placeholder = sum(1 for r in rows if r.get("fxRateIsPlaceholder"))
        return {
            "settlements": len(rows),
            "totalUsd": round(sum(float(r.get("amountUsd", 0)) for r in rows), 6),
            "totalEur": round(sum(float(r.get("amountEur", 0)) for r in rows), 2),
            "firstTimestamp": rows[0]["timestamp"] if rows else None,
            "lastTimestamp": rows[-1]["timestamp"] if rows else None,
            # Loud on purpose: these lines must be restated once a real FX feed
            # exists, and the operator should see the count without grepping.
            "recordsWithPlaceholderFxRate": placeholder,
            "journalPath": str(self.path),
        }
