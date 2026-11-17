# Orderbook Liquidity Analyzer

A small, dependency-light Python CLI that inspects a **public L2 orderbook snapshot** from
Binance and answers the questions that matter before sending an order: *how wide is the
book, which side is heavier, and what will my fill actually cost?*

No API key, no account and no private endpoints are involved — the tool only reads
`GET https://api.binance.com/api/v3/depth`.

Repository: <https://github.com/Xobitt/orderbook-liquidity-analyzer.git>

---

## Features

| Metric | What it reports |
| --- | --- |
| **Best bid / ask** | Innermost price and size on each side, plus the mid price |
| **Spread (bps)** | `(ask - bid) / mid * 10_000`, so books of any price scale are comparable |
| **Top-N volume imbalance** | `(bid_vol - ask_vol) / (bid_vol + ask_vol)` over the top N levels, plus each side's share of volume. `+1.0` means only bids, `-1.0` means only asks |
| **VWAP slippage simulation** | Walks the book level by level for a market buy or sell of a given quote notional and returns the resulting VWAP and the slippage in bps versus the best price |
| **`--mock` mode** | Deterministic synthetic book (seeded RNG) so demos, CI and offline work never depend on the network |
| **`--json` mode** | Machine-readable report on stdout for piping into `jq`, notebooks or a dashboard |

Robustness notes:

* Levels with `price <= 0` or `qty <= 0` are skipped, and both sides are re-sorted defensively.
* A book too thin for the requested notional raises `InsufficientLiquidityError`; the CLI reports it
  and exits with status `1` instead of silently returning a partial fill.
* Non-positive notional and unknown sides are rejected with `ValueError`.

---

## Project layout

```
orderbook-liquidity-analyzer/
├── analyzer.py            # CLI + pure metric functions
├── tests/test_metrics.py  # pytest unit tests
├── requirements.txt
├── pytest.ini             # makes the repo root importable for tests
├── .gitignore
└── README.md
```

---

## Installation

Python 3.9+ is required (the code uses `from __future__ import annotations` and type hints).

```bash
git clone https://github.com/Xobitt/orderbook-liquidity-analyzer.git
cd orderbook-liquidity-analyzer

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

---

## CLI usage

Show the built-in help:

```bash
python analyzer.py --help
```

Offline demo against the deterministic mock book (no network calls):

```bash
python analyzer.py --mock
```

```
Symbol:      BTCUSDT
Source:      mock
Depth:       100 bids / 100 asks
Best bid:    59,994.00000000 x 1.317571
Best ask:    60,006.00000000 x 0.882297
Mid price:   60,000.00000000
Spread:      2.0000 bps
Imbalance:   +0.0272 over top 20 levels (bids 51.36% / asks 48.64%)
Simulation:  market BUY of 100,000.00 -> VWAP 60,009.16525557, slippage 0.5275 bps, filled 1.666412 across 3 level(s)
```

Live snapshot for another symbol, narrower depth, JSON output:

```bash
python analyzer.py --symbol ETHUSDT --limit 50 --levels 10 --json
```

Estimate the cost of selling 250,000 USDT worth of BTC against the bids:

```bash
python analyzer.py --symbol BTCUSDT --side sell --notional 250000
```

Pipe the JSON into `jq` to extract a single field:

```bash
python analyzer.py --mock --json | jq '.simulated_fill.slippage_bps'
```

### Options

| Flag | Default | Description |
| --- | --- | --- |
| `--symbol` | `BTCUSDT` | Binance spot symbol |
| `--limit` | `100` | Depth levels requested from the exchange |
| `--levels` | `20` | Top-N levels used for the imbalance calculation |
| `--notional` | `100000` | Quote notional (e.g. USDT) for the market order simulation |
| `--side` | `buy` | `buy` walks the asks, `sell` walks the bids |
| `--mock` | off | Use the deterministic synthetic book instead of the network |
| `--json` | off | Emit the full report as JSON |
| `--timeout` | `10.0` | HTTP timeout in seconds |

### JSON shape

```json
{
  "symbol": "BTCUSDT",
  "source": "mock",
  "levels_requested": 20,
  "depth_levels": {"bids": 100, "asks": 100},
  "best_quote": {"bid_price": 59994.0, "bid_qty": 1.31757116, "ask_price": 60006.0,
                 "ask_qty": 0.88229744, "mid_price": 60000.0},
  "spread_bps": 2.0,
  "imbalance": {"levels": 20, "bid_volume": 32.34840615, "ask_volume": 30.63617337,
                "ratio": 0.027185, "bid_share": 0.513592, "ask_share": 0.486408},
  "simulated_fill": {"side": "buy", "notional": 100000.0, "filled_qty": 1.66641212,
                     "filled_notional": 100000.0, "vwap": 60009.16525557,
                     "reference_price": 60006.0, "slippage_bps": 0.5275,
                     "levels_consumed": 3,
                     "levels": [{"price": 60006.0, "qty": 0.88229744, "notional": 52943.14018464},
                                {"price": 60012.0, "qty": 0.68912598, "notional": 41355.82831176},
                                {"price": 60018.0, "qty": 0.0949887, "notional": 5701.0315036}]}
}
```

If the book cannot absorb the requested notional, `simulated_fill` becomes
`{"error": "book depth cannot absorb ..."}` rather than aborting the report.

---

## Running the tests

```bash
pytest -v
```

The suite covers spread maths, imbalance (balanced book, bid-heavy book, top-N limiting),
single-level and multi-level VWAP slippage on both sides, and the
`InsufficientLiquidityError` / `ValueError` paths. Everything runs offline — no test hits
the network.

---

## Roadmap

Planned work on `github.com/Xobitt/orderbook-liquidity-analyzer`:

- [ ] **WebSocket depth streams** (`@depth@100ms`) for rolling, time-weighted imbalance instead of single snapshots
- [ ] **Multi-exchange adapters** (Kraken, Coinbase, Bybit) behind a common `ExchangeClient` interface, so spreads can be compared across venues
- [ ] **Maker/taker fee awareness** — include fee in bps in the effective-cost calculation
- [ ] **Liquidity heatmap / depth chart** export as CSV and a lightweight HTML figure
- [ ] **Round-trip cost estimator** (buy then sell) to expose the real cost of a hedge
- [ ] **Time series mode** that samples the book on an interval and writes metrics to SQLite for later analysis
- [ ] **CI pipeline** (GitHub Actions matrix on Python 3.9–3.13) running `pytest` and `ruff`
- [ ] **Packaging** as an installable wheel with a `orderbook-liquidity` console entry point

Contributions and issue reports are welcome.

---

## Disclaimer

Educational and analytical tooling only — not investment advice, and not connected to any
trading account. Public market data can be delayed or throttled by Binance; the `--mock`
mode exists precisely so behaviour stays reproducible when it is.
