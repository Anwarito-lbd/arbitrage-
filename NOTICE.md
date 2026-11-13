# Attribution & Notice

This repository builds upon and extends the open-source **Prediction Market Arbitrage Trading Bot** by **Synpath**:

- **Upstream Repository**: [Synpath-ai/prediction-market-arbitrage-trading-bot](https://github.com/Synpath-ai/prediction-market-arbitrage-trading-bot)
- **License**: MIT License (`Copyright (c) 2026 Synpath`) — see [LICENSE](LICENSE)

## Upstream Components Retained & Built Upon
- `src/arbitrage.py` — Cross-venue YES/NO pricing, `Book`, `Leg`, `Opportunity`, `Pricer`, and `MAX_DISAGREEMENT` guard
- `src/matcher.py` — Synpath catalog market matching and settlement-rule comparison (`same` / `insufficient` / `not_same`)
- `src/strategy.py` — Entry edge & early-convergence take-profit strategy rules
- `src/discover.py` — Market discovery CLI across Kalshi & Polymarket
- `trading/bot.py`, `trading/feed.py`, `trading/__main__.py` — Live WebSocket/REST feed and paper trading runner
- `backtest/backtest.py`, `backtest/data.py`, `backtest/charts.py` — Historical bar backtester and matplotlib charts
- `tests/conftest.py`, `tests/test_arbitrage.py`, `tests/test_backtest.py`, `tests/test_bot.py` — Core offline unit tests

## Enhancements & Platform Extensions (MIT)
- `src/orderbook.py` — Multi-level L2 order book modeling, VWAP fill simulation, execution slippage (bps & cents), and max-fillable liquidity bounds
- `trading/paper_account.py` — Stateful `$1,000.00` paper account ledger, risk allocation caps, mark-to-market equity & drawdown tracking, and daily annualized Sharpe computation
- `backtest/paper_replay.py` — Deterministic 60-day walk-forward L2 order-book simulation & proof-chart generator (`docs/proof/*.png`)
- `server.py` — FastAPI REST & WebSocket (`/ws/stream`) backend powered by the pricing, L2 slippage, and paper-account engines
- `index.html`, `app.js`, `index.css` — Interactive ArbitrageX web terminal with live Polymarket API fallback, L2 simulation mode, sizing calculator, and paper ledger audit trail
- `tests/test_orderbook_and_paper.py`, `tests/test_server.py` — Extended unit and API/WebSocket integration test suite
