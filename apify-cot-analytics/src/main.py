"""Apify Actor entrypoint for COT Report Analytics.

Deliberately thin: all real work lives in `pipeline` / `analytics` / `cftc`,
which have no Apify dependency and are unit-tested offline. This file only
handles Actor lifecycle, pay-per-event charging and dataset writes.

Charging model (pay-per-event):
  actor-start        charged once per run
  market-week-result charged once per emitted weekly record
"""

from __future__ import annotations

import logging

from apify import Actor

from .analytics import summarize
from .cftc import CftcClient
from .pipeline import build_config, run

EVENT_START = "actor-start"
EVENT_RESULT = "market-week-result"

log = logging.getLogger(__name__)


async def main() -> None:
    async with Actor:
        raw_input = await Actor.get_input() or {}

        try:
            cfg = build_config(raw_input)
        except (ValueError, TypeError) as exc:
            # TypeError is the belt-and-braces case: a JSON null or a wrongly
            # typed field must still produce a readable failure, never a raw
            # traceback in a paying user's run log.
            # Bad input is the user's problem, but it must be loud and free:
            # fail the run with a readable message rather than emitting nothing.
            await Actor.fail(status_message=f"Invalid input: {exc}")
            return

        await Actor.charge(EVENT_START)

        symbols = ", ".join(m.label for m in cfg.markets)
        Actor.log.info(
            "Analyzing %d market(s) [%s] | report=%s | weeks=%d | lookback=%dw",
            len(cfg.markets), symbols, cfg.report, cfg.weeks, cfg.lookback_weeks,
        )
        await Actor.set_status_message(f"Fetching CFTC data for {len(cfg.markets)} market(s)...")

        client = CftcClient()
        failures: list[str] = []

        def note_failure(market, exc: Exception) -> None:
            failures.append(f"{market.label}: {exc}")

        emitted = 0
        pushed_all: list[dict] = []
        charging = Actor.get_charging_manager()

        for records in run(cfg, client=client, on_error=note_failure):
            # Respect the user's max-cost-per-run setting instead of blowing
            # through it and silently dropping the overflow.
            allowed = charging.calculate_max_event_charge_count_within_limit(EVENT_RESULT)
            if allowed is not None and allowed <= 0:
                Actor.log.warning(
                    "Max cost per run reached after %d records; stopping early.", emitted
                )
                break
            batch = records if allowed is None else records[:allowed]

            await Actor.push_data(batch, charged_event_name=EVENT_RESULT)
            pushed_all.extend(batch)
            emitted += len(batch)
            await Actor.set_status_message(f"{emitted} weekly record(s) written...")

        if not pushed_all:
            await Actor.fail(
                status_message=(
                    "No data produced. "
                    + ("; ".join(failures) if failures else "CFTC returned nothing for this input.")
                )
            )
            return

        summary = summarize(pushed_all)
        summary["failedMarkets"] = failures
        await Actor.set_value("RUN_SUMMARY", summary)

        msg = (
            f"Done: {summary['recordsProduced']} record(s) for "
            f"{summary['marketsAnalyzed']} market(s), latest CFTC report "
            f"{summary['latestReportDate']}."
        )
        if failures:
            msg += f" {len(failures)} market(s) skipped."
        Actor.log.info(msg)
        await Actor.set_status_message(msg, is_terminal=True)
