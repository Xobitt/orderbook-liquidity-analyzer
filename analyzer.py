#!/usr/bin/env python3
"""Orderbook liquidity analyzer for public Binance L2 depth snapshots.

Pulls a public (unauthenticated) L2 snapshot from Binance, then reports:

* best bid / best ask and the mid price
* spread in basis points
* top-N volume imbalance between bids and asks
* VWAP slippage for a simulated market order of a given notional

No API key is required: only the public ``/api/v3/depth`` endpoint is used.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from dataclasses import dataclass, field
from typing import Iterable, Sequence

import requests

BINANCE_DEPTH_URL = "https://api.binance.com/api/v3/depth"
DEFAULT_SYMBOL = "BTCUSDT"
DEFAULT_DEPTH_LIMIT = 100
DEFAULT_LEVELS = 20
DEFAULT_NOTIONAL = 100_000.0
MOCK_MIDS = {
    "BTCUSDT": 60_000.0,
    "ETHUSDT": 3_000.0,
    "SOLUSDT": 150.0,
    "BNBUSDT": 600.0,
}
DEFAULT_MOCK_MID = 100.0


class AnalyzerError(Exception):
    """Base class for analyzer failures (bad input, network, bad payload)."""


class InsufficientLiquidityError(AnalyzerError):
    """The book cannot absorb the requested notional."""


@dataclass(frozen=True)
class BestQuote:
    bid_price: float
    bid_qty: float
    ask_price: float
    ask_qty: float

    @property
    def mid_price(self) -> float:
        return (self.bid_price + self.ask_price) / 2.0


@dataclass(frozen=True)
class Imbalance:
    levels: int
    bid_volume: float
    ask_volume: float

    @property
    def ratio(self) -> float:
        """(bid - ask) / (bid + ask): +1 all bids, -1 all asks."""
        total = self.bid_volume + self.ask_volume
        if total <= 0:
            return 0.0
        return (self.bid_volume - self.ask_volume) / total

    @property
    def bid_share(self) -> float:
        total = self.bid_volume + self.ask_volume
        return self.bid_volume / total if total > 0 else 0.0

    @property
    def ask_share(self) -> float:
        return 1.0 - self.bid_share if (self.bid_volume + self.ask_volume) > 0 else 0.0


@dataclass
class Fill:
    side: str
    notional: float
    filled_qty: float
    filled_notional: float
    vwap: float
    reference_price: float
    slippage_bps: float
    levels_consumed: int
    levels: list[dict[str, float]] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "side": self.side,
            "notional": round(self.notional, 8),
            "filled_qty": round(self.filled_qty, 8),
            "filled_notional": round(self.filled_notional, 8),
            "vwap": round(self.vwap, 8),
            "reference_price": round(self.reference_price, 8),
            "slippage_bps": round(self.slippage_bps, 4),
            "levels_consumed": self.levels_consumed,
            "levels": [
                {
                    "price": round(lvl["price"], 8),
                    "qty": round(lvl["qty"], 8),
                    "notional": round(lvl["notional"], 8),
                }
                for lvl in self.levels
            ],
        }


def parse_levels(raw: Iterable[Sequence[object]]) -> list[tuple[float, float]]:
    """Convert Binance ``["price", "qty", ...]`` rows into float tuples."""
    levels: list[tuple[float, float]] = []
    for row in raw:
        try:
            price = float(row[0])
            qty = float(row[1])
        except (TypeError, ValueError, IndexError) as exc:
            raise AnalyzerError(f"malformed orderbook level: {row!r}") from exc
        if price <= 0 or qty <= 0:
            continue
        levels.append((price, qty))
    return levels


def sort_book(bids: list[tuple[float, float]], asks: list[tuple[float, float]]):
    """Return bids descending and asks ascending by price."""
    return (
        sorted(bids, key=lambda lvl: lvl[0], reverse=True),
        sorted(asks, key=lambda lvl: lvl[0]),
    )


def best_quote(bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> BestQuote:
    bids, asks = sort_book(bids, asks)
    if not bids or not asks:
        raise AnalyzerError("orderbook needs at least one bid and one ask")
    return BestQuote(
        bid_price=bids[0][0],
        bid_qty=bids[0][1],
        ask_price=asks[0][0],
        ask_qty=asks[0][1],
    )


def spread_bps(bid_price: float, ask_price: float) -> float:
    """Relative spread in basis points, measured against the mid price."""
    if bid_price <= 0 or ask_price <= 0:
        raise AnalyzerError("bid and ask prices must be positive")
    mid = (bid_price + ask_price) / 2.0
    return (ask_price - bid_price) / mid * 10_000.0


def volume_imbalance(
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
    levels: int = DEFAULT_LEVELS,
) -> Imbalance:
    """Volume imbalance over the top ``levels`` price levels on each side."""
    if levels <= 0:
        raise AnalyzerError("levels must be a positive integer")
    bids, asks = sort_book(bids, asks)
    bid_volume = sum(qty for _, qty in bids[:levels])
    ask_volume = sum(qty for _, qty in asks[:levels])
    return Imbalance(levels=levels, bid_volume=bid_volume, ask_volume=ask_volume)


def simulate_market_order(
    levels: list[tuple[float, float]],
    notional: float,
    side: str = "buy",
) -> Fill:
    """Walk the book and fill ``notional`` (quote units) as a market order.

    ``levels`` must be the side you trade against: asks for a buy, bids for a
    sell. Raises :class:`InsufficientLiquidityError` when the book is too thin,
    and :class:`ValueError` for non-positive notional.
    """
    if notional <= 0:
        raise ValueError("notional must be positive")
    if side not in ("buy", "sell"):
        raise ValueError("side must be 'buy' or 'sell'")
    if not levels:
        raise InsufficientLiquidityError("no liquidity available on this side")

    remaining = notional
    filled_qty = 0.0
    consumed: list[dict[str, float]] = []

    for price, qty in levels:
        if remaining <= 0:
            break
        level_notional = price * qty
        if level_notional <= remaining:
            take_qty = qty
            take_notional = level_notional
        else:
            take_qty = remaining / price
            take_notional = remaining
        filled_qty += take_qty
        remaining -= take_notional
        consumed.append({"price": price, "qty": take_qty, "notional": take_notional})

    tolerance = max(notional * 1e-9, 1e-9)
    if filled_qty <= 0 or remaining > tolerance:
        raise InsufficientLiquidityError(
            f"book depth cannot absorb {notional:,.2f} quote units "
            f"(filled {notional - remaining:,.2f}, short {remaining:,.2f})"
        )

    reference_price = levels[0][0]
    vwap = (notional - remaining) / filled_qty
    slippage = abs(vwap - reference_price) / reference_price * 10_000.0
    return Fill(
        side=side,
        notional=notional,
        filled_qty=filled_qty,
        filled_notional=notional - remaining,
        vwap=vwap,
        reference_price=reference_price,
        slippage_bps=slippage,
        levels_consumed=len(consumed),
        levels=consumed,
    )


def simulate_market_buy(asks: list[tuple[float, float]], notional: float) -> Fill:
    return simulate_market_order(asks, notional, side="buy")


def simulate_market_sell(bids: list[tuple[float, float]], notional: float) -> Fill:
    return simulate_market_order(bids, notional, side="sell")


def fetch_orderbook(symbol: str, limit: int = DEFAULT_DEPTH_LIMIT, timeout: float = 10.0):
    """Fetch a public L2 snapshot from Binance."""
    params = {"symbol": symbol.upper(), "limit": int(limit)}
    try:
        response = requests.get(BINANCE_DEPTH_URL, params=params, timeout=timeout)
    except requests.RequestException as exc:
        raise AnalyzerError(f"network error contacting Binance: {exc}") from exc
    if response.status_code != 200:
        raise AnalyzerError(
            f"Binance returned HTTP {response.status_code}: {response.text[:200]}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise AnalyzerError("Binance returned a non-JSON payload") from exc
    if "bids" not in payload or "asks" not in payload:
        raise AnalyzerError("Binance payload is missing bids/asks")
    return parse_levels(payload["bids"]), parse_levels(payload["asks"])


def mock_orderbook(
    symbol: str = DEFAULT_SYMBOL,
    levels: int = 50,
    seed: int = 7,
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Deterministic synthetic book, useful for demos, CI and offline runs.

    Quote notional per level is drawn from a realistic-looking range so the
    book is deep but not infinitely so: a six-figure market order still walks
    several levels and shows measurable slippage.
    """
    rng = random.Random(seed)
    mid = MOCK_MIDS.get(symbol.upper(), DEFAULT_MOCK_MID)
    tick = mid * 1e-4
    bids: list[tuple[float, float]] = []
    asks: list[tuple[float, float]] = []
    for i in range(1, levels + 1):
        bid_price = round(mid - i * tick, 8)
        ask_price = round(mid + i * tick, 8)
        # Deeper levels carry a bit more size, as on a real book.
        growth = 1.0 + (i / levels) * 0.6
        bid_qty = rng.uniform(30_000.0, 180_000.0) * growth / bid_price
        ask_qty = rng.uniform(30_000.0, 180_000.0) * growth / ask_price
        bids.append((bid_price, round(bid_qty, 8)))
        asks.append((ask_price, round(ask_qty, 8)))
    return sort_book(bids, asks)


def build_report(
    symbol: str,
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
    levels: int,
    notional: float,
    side: str,
    source: str,
) -> dict:
    quote = best_quote(bids, asks)
    imbalance = volume_imbalance(bids, asks, levels)
    book_side = asks if side == "buy" else bids
    try:
        fill = simulate_market_order(book_side, notional, side=side).as_dict()
    except InsufficientLiquidityError as exc:
        fill = {"error": str(exc)}
    return {
        "symbol": symbol.upper(),
        "source": source,
        "levels_requested": levels,
        "depth_levels": {"bids": len(bids), "asks": len(asks)},
        "best_quote": {
            "bid_price": round(quote.bid_price, 8),
            "bid_qty": round(quote.bid_qty, 8),
            "ask_price": round(quote.ask_price, 8),
            "ask_qty": round(quote.ask_qty, 8),
            "mid_price": round(quote.mid_price, 8),
        },
        "spread_bps": round(spread_bps(quote.bid_price, quote.ask_price), 4),
        "imbalance": {
            "levels": imbalance.levels,
            "bid_volume": round(imbalance.bid_volume, 8),
            "ask_volume": round(imbalance.ask_volume, 8),
            "ratio": round(imbalance.ratio, 6),
            "bid_share": round(imbalance.bid_share, 6),
            "ask_share": round(imbalance.ask_share, 6),
        },
        "simulated_fill": fill,
    }


def render_text(report: dict) -> str:
    quote = report["best_quote"]
    imb = report["imbalance"]
    lines = [
        f"Symbol:      {report['symbol']}",
        f"Source:      {report['source']}",
        f"Depth:       {report['depth_levels']['bids']} bids / "
        f"{report['depth_levels']['asks']} asks",
        f"Best bid:    {quote['bid_price']:,.8f} x {quote['bid_qty']:,.6f}",
        f"Best ask:    {quote['ask_price']:,.8f} x {quote['ask_qty']:,.6f}",
        f"Mid price:   {quote['mid_price']:,.8f}",
        f"Spread:      {report['spread_bps']:.4f} bps",
        f"Imbalance:   {imb['ratio']:+.4f} over top {imb['levels']} levels "
        f"(bids {imb['bid_share']:.2%} / asks {imb['ask_share']:.2%})",
    ]
    fill = report["simulated_fill"]
    if "error" in fill:
        lines.append(f"Simulation:  failed - {fill['error']}")
    else:
        lines.append(
            f"Simulation:  market {fill['side'].upper()} of {fill['notional']:,.2f} "
            f"-> VWAP {fill['vwap']:,.8f}, slippage {fill['slippage_bps']:.4f} bps, "
            f"filled {fill['filled_qty']:,.6f} across {fill['levels_consumed']} level(s)"
        )
    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="analyzer.py",
        description="Analyze a public Binance L2 orderbook snapshot: spread, "
        "top-N imbalance and VWAP slippage for a simulated market order.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL, help="Binance spot symbol")
    parser.add_argument(
        "--limit", type=int, default=DEFAULT_DEPTH_LIMIT, help="depth levels to fetch"
    )
    parser.add_argument(
        "--levels", type=int, default=DEFAULT_LEVELS, help="top-N levels for imbalance"
    )
    parser.add_argument(
        "--notional",
        type=float,
        default=DEFAULT_NOTIONAL,
        help="quote notional (e.g. USDT) for the market order simulation",
    )
    parser.add_argument(
        "--side", choices=("buy", "sell"), default="buy", help="simulated market side"
    )
    parser.add_argument(
        "--mock", action="store_true", help="use a deterministic synthetic book"
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    parser.add_argument(
        "--timeout", type=float, default=10.0, help="HTTP timeout in seconds"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.mock:
            bids, asks = mock_orderbook(args.symbol, levels=max(args.limit, args.levels, 10))
            source = "mock"
        else:
            bids, asks = fetch_orderbook(args.symbol, limit=args.limit, timeout=args.timeout)
            source = "binance:/api/v3/depth"
        report = build_report(
            symbol=args.symbol,
            bids=bids,
            asks=asks,
            levels=args.levels,
            notional=args.notional,
            side=args.side,
            source=source,
        )
    except AnalyzerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
