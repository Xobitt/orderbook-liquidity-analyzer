"""Unit tests for the orderbook metrics in analyzer.py."""

from __future__ import annotations

import math

import pytest

from analyzer import (
    AnalyzerError,
    InsufficientLiquidityError,
    best_quote,
    build_report,
    mock_orderbook,
    simulate_market_buy,
    simulate_market_order,
    simulate_market_sell,
    spread_bps,
    volume_imbalance,
)

BIDS = [(100.0, 1.0), (99.5, 2.0), (99.0, 3.0)]
ASKS = [(100.5, 1.0), (101.0, 2.0), (101.5, 3.0)]


# --------------------------------------------------------------------------- #
# Best quote and spread
# --------------------------------------------------------------------------- #
def test_best_quote_picks_inner_prices_and_mid():
    quote = best_quote(BIDS, ASKS)
    assert quote.bid_price == pytest.approx(100.0)
    assert quote.ask_price == pytest.approx(100.5)
    assert quote.mid_price == pytest.approx(100.25)


def test_best_quote_sorts_unsorted_book():
    quote = best_quote(list(reversed(BIDS)), list(reversed(ASKS)))
    assert quote.bid_price == pytest.approx(100.0)
    assert quote.ask_price == pytest.approx(100.5)


@pytest.mark.parametrize(
    ("bid", "ask", "expected"),
    [
        (99.0, 101.0, 200.0),      # 2 / 100 mid
        (100.0, 100.0, 0.0),       # locked book
        (9_999.0, 10_001.0, 2.0),  # tight spread on a round book
    ],
)
def test_spread_bps_against_mid(bid, ask, expected):
    assert spread_bps(bid, ask) == pytest.approx(expected)


def test_spread_bps_rejects_non_positive_prices():
    with pytest.raises(AnalyzerError):
        spread_bps(0.0, 100.0)


def test_best_quote_requires_both_sides():
    with pytest.raises(AnalyzerError):
        best_quote([], ASKS)


# --------------------------------------------------------------------------- #
# Volume imbalance
# --------------------------------------------------------------------------- #
def test_imbalance_is_zero_for_balanced_book():
    imb = volume_imbalance(BIDS, ASKS, levels=3)
    assert imb.bid_volume == pytest.approx(6.0)
    assert imb.ask_volume == pytest.approx(6.0)
    assert imb.ratio == pytest.approx(0.0)
    assert imb.bid_share == pytest.approx(0.5)


def test_imbalance_ratio_formula_on_bid_heavy_book():
    bids = [(100.0, 10.0), (99.0, 0.0)]
    asks = [(101.0, 2.0)]
    imb = volume_imbalance(bids, asks, levels=1)
    assert imb.ratio == pytest.approx((10.0 - 2.0) / (10.0 + 2.0))
    assert imb.ask_share == pytest.approx(2.0 / 12.0)


def test_imbalance_respects_top_n_limit():
    deep_bids = [(100.0 - i, 1.0) for i in range(5)]
    deep_asks = [(100.0 + i, 1.0) for i in range(5)]
    imb = volume_imbalance(deep_bids, deep_asks, levels=2)
    assert imb.bid_volume == pytest.approx(2.0)
    assert imb.ask_volume == pytest.approx(2.0)
    assert imb.levels == 2


def test_imbalance_rejects_non_positive_levels():
    with pytest.raises(AnalyzerError):
        volume_imbalance(BIDS, ASKS, levels=0)


# --------------------------------------------------------------------------- #
# Slippage / VWAP simulation
# --------------------------------------------------------------------------- #
def test_single_level_buy_has_no_slippage():
    asks = [(100.0, 5.0)]
    fill = simulate_market_buy(asks, notional=250.0)
    assert fill.filled_qty == pytest.approx(2.5)
    assert fill.vwap == pytest.approx(100.0)
    assert fill.slippage_bps == pytest.approx(0.0)
    assert fill.levels_consumed == 1


def test_multi_level_buy_vwap_and_partial_level():
    asks = [(100.0, 1.0), (101.0, 2.0), (102.0, 5.0)]
    fill = simulate_market_buy(asks, notional=150.0)
    # 100 quote at 100.0 fills 1.0 base; remaining 50 quote at 101.0 fills 50/101.
    expected_qty = 1.0 + 50.0 / 101.0
    expected_vwap = 150.0 / expected_qty
    assert fill.levels_consumed == 2
    assert fill.filled_qty == pytest.approx(expected_qty)
    assert fill.filled_notional == pytest.approx(150.0)
    assert fill.vwap == pytest.approx(expected_vwap)
    assert fill.slippage_bps == pytest.approx((expected_vwap - 100.0) / 100.0 * 10_000)
    assert fill.levels[1]["qty"] == pytest.approx(50.0 / 101.0)


def test_multi_level_sell_slips_below_best_bid():
    bids = [(100.0, 1.0), (99.0, 2.0)]
    fill = simulate_market_sell(bids, notional=151.5)
    expected_qty = 1.0 + 51.5 / 99.0
    assert fill.side == "sell"
    assert fill.vwap == pytest.approx(151.5 / expected_qty)
    assert fill.vwap < fill.reference_price
    assert fill.slippage_bps == pytest.approx((100.0 - fill.vwap) / 100.0 * 10_000)


def test_insufficient_liquidity_raises_on_thin_book():
    asks = [(100.0, 1.0), (101.0, 1.0)]
    with pytest.raises(InsufficientLiquidityError):
        simulate_market_buy(asks, notional=10_000.0)


def test_insufficient_liquidity_raises_on_empty_side():
    with pytest.raises(InsufficientLiquidityError):
        simulate_market_buy([], notional=100.0)


@pytest.mark.parametrize("notional", [0.0, -5.0])
def test_non_positive_notional_rejected(notional):
    with pytest.raises(ValueError):
        simulate_market_order(ASKS, notional=notional, side="buy")


def test_unknown_side_rejected():
    with pytest.raises(ValueError):
        simulate_market_order(ASKS, notional=100.0, side="hodl")


# --------------------------------------------------------------------------- #
# End-to-end report (offline, mock book)
# --------------------------------------------------------------------------- #
def test_mock_book_is_consistent_and_report_builds():
    bids, asks = mock_orderbook("BTCUSDT", levels=30, seed=11)
    assert len(bids) == 30 and len(asks) == 30
    assert max(price for price, _ in bids) < min(price for price, _ in asks)

    report = build_report(
        symbol="BTCUSDT",
        bids=bids,
        asks=asks,
        levels=10,
        notional=1_000.0,
        side="buy",
        source="mock",
    )
    assert report["symbol"] == "BTCUSDT"
    assert report["spread_bps"] > 0
    assert -1.0 <= report["imbalance"]["ratio"] <= 1.0
    assert math.isfinite(report["simulated_fill"]["vwap"])


def test_report_records_simulation_error_instead_of_crashing():
    report = build_report(
        symbol="THIN",
        bids=[(100.0, 0.001)],
        asks=[(100.01, 0.001)],
        levels=5,
        notional=1_000_000.0,
        side="buy",
        source="mock",
    )
    assert "error" in report["simulated_fill"]
