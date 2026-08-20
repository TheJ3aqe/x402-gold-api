"""Cross-market analytics: ranking, consensus regimes, the dollar score, divergences."""

from __future__ import annotations

from x402api.crossmarket import (
    COT_INDEX_MIDPOINT,
    UNIFORM_DISPERSION,
    CrossMarketThresholds,
    compare,
    consensus,
    divergences,
    dollar_positioning,
    rank_by_stretch,
    screen_extremes,
    stretch,
)


def rec(
    code: str,
    *,
    label: str = "X",
    index: float | None = 50.0,
    positioning: str = "neutral",
    bias: str = "long",
    flow: str | None = None,
    net: int = 1000,
    net_change: int | None = 0,
    date: str = "2026-08-11",
    group: str = "fx",
) -> dict:
    return {
        "cftcContractCode": code,
        "market": label,
        "marketGroup": group,
        "reportDate": date,
        "cotIndex": index,
        "positioning": positioning,
        "positioningBias": bias,
        "flowDirection": flow,
        "net": net,
        "netChange": net_change,
        "netZScore": 0.0,
    }


# --- benchmarks --------------------------------------------------------------


def test_uniform_dispersion_benchmark_is_the_stated_derivation():
    # 100/sqrt(12) is the population stdev of a uniform 0-100 spread. It is the
    # honest "no signal" reference point, not a tuned constant.
    assert UNIFORM_DISPERSION == 28.87
    assert COT_INDEX_MIDPOINT == 50.0


def test_default_thresholds_bracket_the_benchmark():
    t = CrossMarketThresholds()
    assert t.aligned_dispersion_max < t.divided_dispersion_min == UNIFORM_DISPERSION


# --- stretch and ranking -----------------------------------------------------


def test_stretch_is_distance_from_the_midpoint():
    assert stretch(rec("a", index=90)) == 40.0
    assert stretch(rec("a", index=10)) == 40.0
    assert stretch(rec("a", index=50)) == 0.0
    assert stretch(rec("a", index=None)) is None


def test_ranking_orders_most_stretched_first():
    rows = rank_by_stretch(
        [rec("a", label="A", index=55), rec("b", label="B", index=95), rec("c", label="C", index=20)]
    )
    assert [r["market"] for r in rows] == ["B", "C", "A"]
    assert [r["rank"] for r in rows] == [1, 2, 3]


def test_unscoreable_markets_are_listed_last_with_a_reason_not_dropped():
    rows = rank_by_stretch([rec("a", label="A", index=90), rec("b", label="B", index=None)])
    assert len(rows) == 2
    assert rows[-1]["market"] == "B"
    assert rows[-1]["rank"] is None
    assert "COT Index unavailable" in rows[-1]["excludedReason"]


def test_history_records_are_deduplicated_to_the_newest_week():
    # A comparison built from a multi-week pull must not count one market twice.
    rows = rank_by_stretch(
        [
            rec("a", label="A", index=90, date="2026-08-11"),
            rec("a", label="A", index=10, date="2026-08-04"),
        ]
    )
    assert len(rows) == 1
    assert rows[0]["cotIndex"] == 90


# --- consensus ---------------------------------------------------------------


def test_clustered_positioning_reads_as_aligned():
    result = consensus([rec("a", index=88), rec("b", index=90), rec("c", index=92)])
    assert result["regime"] == "aligned"
    assert result["meanCotIndex"] == 90.0
    assert result["dispersion"] < CrossMarketThresholds().aligned_dispersion_max
    assert "leaning long" in result["interpretation"]


def test_aligned_short_side_is_described_as_short():
    assert "leaning short" in consensus(
        [rec("a", index=8), rec("b", index=10), rec("c", index=12)]
    )["interpretation"]


def test_scattered_positioning_reads_as_divided():
    result = consensus([rec("a", index=0), rec("b", index=50), rec("c", index=100)])
    assert result["regime"] == "divided"
    assert result["dispersion"] >= UNIFORM_DISPERSION
    assert "no cross-market read" in result["interpretation"]


def test_intermediate_dispersion_reads_as_mixed():
    assert consensus([rec("a", index=30), rec("b", index=50), rec("c", index=75)])["regime"] == "mixed"


def test_single_market_cannot_form_a_consensus():
    assert consensus([rec("a", index=90)])["regime"] == "insufficient_data"


def test_no_scoreable_market_is_reported_not_crashed():
    result = consensus([rec("a", index=None)])
    assert result["regime"] == "insufficient_data"
    assert result["marketsScored"] == 0
    assert result["meanCotIndex"] is None


def test_consensus_counts_long_and_short_markets():
    result = consensus(
        [rec("a", index=90, bias="long"), rec("b", index=20, bias="short"), rec("c", index=40, bias="short")]
    )
    assert result["netLongMarkets"] == 1 and result["netShortMarkets"] == 2


def test_thresholds_are_overridable():
    rows = [rec("a", index=40), rec("b", index=60)]
    assert consensus(rows)["regime"] == "aligned"
    strict = CrossMarketThresholds(aligned_dispersion_max=1.0, divided_dispersion_min=2.0)
    assert consensus(rows, strict)["regime"] == "divided"


# --- dollar positioning ------------------------------------------------------


def test_long_euro_futures_read_as_short_dollar():
    # CME currency futures are the FOREIGN currency vs USD, so a COT Index of 80
    # on Euro FX is a 20 on the dollar's side of the trade.
    result = dollar_positioning([rec("099741", label="Euro FX", index=80)])
    assert result["dollarCotIndex"] == 20.0
    assert result["bias"] == "short_usd"
    assert result["contributions"][0]["usdSign"] == -1


def test_dollar_index_enters_directly():
    result = dollar_positioning([rec("098662", label="DXY", index=80)])
    assert result["dollarCotIndex"] == 80.0
    assert result["bias"] == "long_usd"
    assert result["contributions"][0]["usdSign"] == 1


def test_consistent_dollar_story_averages_coherently():
    # Short every foreign currency (index 10) AND long the dollar index (90)
    # is one story, and the score must reflect that rather than cancelling out.
    result = dollar_positioning(
        [
            rec("099741", label="EUR", index=10),
            rec("096742", label="GBP", index=10),
            rec("098662", label="DXY", index=90),
        ]
    )
    assert result["dollarCotIndex"] == 90.0
    assert result["bias"] == "long_usd"


def test_gold_is_excluded_from_the_dollar_score():
    # Deliberate: gold's dollar correlation is real but unstable, and folding it
    # in would overstate the signal.
    result = dollar_positioning([rec("088691", label="Gold", index=95, group="metals")])
    assert result["contributingMarkets"] == 0
    assert result["dollarCotIndex"] is None


def test_no_fx_markets_gives_an_actionable_note():
    result = dollar_positioning([rec("088691", label="Gold", index=95)])
    assert "EURUSD" in result["note"]


def test_neutral_dollar():
    assert dollar_positioning([rec("098662", index=50)])["bias"] == "neutral"


# --- divergences -------------------------------------------------------------


def test_crowded_long_that_is_unwinding_is_flagged():
    hits = divergences(
        [rec("a", label="A", index=95, positioning="extreme_long", flow="distributing", net_change=-5000)]
    )
    assert len(hits) == 1
    assert hits[0]["signal"] == "crowded_long_unwinding"


def test_crowded_short_being_covered_is_flagged():
    hits = divergences(
        [rec("a", index=5, positioning="extreme_short", flow="accumulating", net_change=3000)]
    )
    assert hits[0]["signal"] == "crowded_short_covering"


def test_an_extreme_still_building_is_not_a_divergence():
    # That is a trend, not a turn. Flagging it would make the signal noise.
    assert divergences(
        [rec("a", index=95, positioning="extreme_long", flow="accumulating")]
    ) == []


def test_neutral_positioning_is_never_a_divergence():
    assert divergences([rec("a", index=50, positioning="neutral", flow="distributing")]) == []


def test_unchanged_flow_is_not_a_divergence():
    assert divergences(
        [rec("a", index=95, positioning="extreme_long", flow="unchanged")]
    ) == []


def test_divergences_sorted_by_size_of_the_move():
    hits = divergences(
        [
            rec("a", label="Small", index=95, positioning="extreme_long", flow="distributing", net_change=-100),
            rec("b", label="Big", index=95, positioning="extreme_long", flow="distributing", net_change=-9000),
        ]
    )
    assert [h["market"] for h in hits] == ["Big", "Small"]


def test_stretched_but_not_extreme_still_counts():
    assert len(divergences([rec("a", index=80, positioning="stretched_long", flow="distributing")])) == 1


# --- composites --------------------------------------------------------------


def test_compare_returns_all_four_blocks():
    result = compare([rec("099741", index=80), rec("098662", index=20)])
    assert set(result) == {"ranking", "consensus", "dollar", "divergences"}


def test_screen_extremes_separates_extremes_from_stretched():
    result = screen_extremes(
        [
            rec("a", label="Extreme", index=95, positioning="extreme_long"),
            rec("b", label="Stretched", index=80, positioning="stretched_long"),
            rec("c", label="Quiet", index=50, positioning="neutral"),
        ]
    )
    assert [r["market"] for r in result["extremes"]] == ["Extreme"]
    assert [r["market"] for r in result["stretched"]] == ["Stretched"]
    assert result["marketsScanned"] == 3
    assert result["latestReportDate"] == "2026-08-11"


def test_screen_extremes_on_a_quiet_market_set():
    result = screen_extremes([rec("a", index=50, positioning="neutral")])
    assert result["extremes"] == [] and result["stretched"] == []
