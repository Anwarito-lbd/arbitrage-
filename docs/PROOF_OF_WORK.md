# Proof of Work & Verification Ledger

All commands, test counts, browser assertions, visual assets, and `$1,000.00` simulated paper replay metrics documented below were executed and directly observed in this repository session (`c:\Users\issao\Downloads\portfolio\agy-work\arbitrage`, branch `feat/quant-arb-platform`).

> **Simulation Disclaimer:** All execution performance numbers belong exclusively to a **deterministic simulated paper-account replay** (`backtest/paper_replay.py`, `seed=42`, synthetic L2 books). No live trades were placed and no real capital was risked.

---

## 1. Automated Test Suite Verification (`pytest`)

Command executed after final code/UI/server edits:

```text
python -m pytest -v
```

Observed result: **`28 passed in 4.49s`** (0 failed, 0 skipped):

| Test Module | Tests | Scope Verified |
| :--- | :---: | :--- |
| `tests/test_arbitrage.py` | 6 | Upstream Synpath YES/NO book inversion, fee schedules, pair combination ranking, disagreement guard, round-trip unwind, strategy entry/exit |
| `tests/test_backtest.py` | 2 | Upstream backtest entry/exit and loss-prevention invariants |
| `tests/test_bot.py` | 6 | Paper position persistence across restarts, rate-limited status logs, stream/REST feed handling |
| `tests/test_orderbook_and_paper.py` | 7 | `verify_rule_compatibility` audit (`same` vs `not_same`), `L2Book` VWAP slippage & partial fill, edge erosion across L2 tiers, `flipped()` symmetry, `$1,000` `PaperAccount` risk caps (45% allocation / 4 open max), MTM equity & Sharpe math, deterministic replay reproducibility |
| `tests/test_server.py` | 7 | `/api/health`, static HTML index, `/api/markets` rule filtering, `/api/orderbook/{market_id}` L2 depth & slippage curve, `/api/paper-account`, `/api/calculate` Kalshi quadratic cent-rounding, `/api/bot/execute` & `/api/bot/close`, WebSocket `/ws/stream` snapshot |

---

## 2. Deterministic `$1,000.00` Simulated Paper Account Replay

Command executed after final edits:

```text
python -m backtest.paper_replay
```

Exact observed stdout:

```text
=== SIMULATED $1,000 PAPER ACCOUNT REPLAY (NO LIVE ORDERS) ===
Initial Capital : $1,000.00
Final Equity    : $1,748.05
Realized P&L    : +$748.05 (+74.81%)
Trades          : 29 (29 won, 0 lost, win rate 100.0%)
Exit Breakdown  : 29 early convergence, 0 settled at resolution
Sharpe (Daily)  : 8.590
Max MTM DD      : -5.64% (-$82.26)
Fees / Slippage : $499.21 fees / $3.02 slippage
Rejected Rules  : 1 pair(s) skipped
JSON Ledger     : out\paper_replay_1000.json
Chart           : docs\proof\equity_1000_paper.png
Chart           : docs\proof\spread_vs_fees.png
Chart           : docs\proof\orderbook_depth_slippage.png
```

---

## 3. Headless Browser Verification & Walkthrough Capture (Playwright + FFmpeg)

Executed headless Chromium verification against live `uvicorn server:app --host 127.0.0.1 --port 8765`:

- **Desktop Viewport (`1440×900`, 2× DPI):** `Horizontal Overflow = False`, `Console Errors = []`, `Page Errors = []`
- **Mobile Viewport (`390×844`, 2× DPI):** `Horizontal Overflow = False`, `Console Errors = []`, `Page Errors = []`
- **Walkthrough Flow Recorded (`1280×720`, `17.96s`):** Landing Hero → Math Proof scroll → Launch Terminal → Live Scanner L2 stream → Market Detail L2 & Rule Audit Modal → Sizing & Fees Calculator (`$450` recalc) → Paper Bot Simulation Entry → 29-Trade Replay Ledger.

---

## 4. Visual Proof Inventory (`docs/proof/`)

| Artifact | Resolution / Format | Observed Size | Purpose |
| :--- | :--- | :---: | :--- |
| [`docs/proof/equity_1000_paper.png`](proof/equity_1000_paper.png) | Matplotlib PNG (`300 DPI`) | `286 KB` (`292,896 B`) | `$1,000.00` paper account equity & MTM drawdown curve |
| [`docs/proof/spread_vs_fees.png`](proof/spread_vs_fees.png) | Matplotlib PNG (`300 DPI`) | `214 KB` (`218,702 B`) | Gross vs. net arbitrage edge & Kalshi quadratic fee curve |
| [`docs/proof/orderbook_depth_slippage.png`](proof/orderbook_depth_slippage.png) | Matplotlib PNG (`300 DPI`) | `175 KB` (`178,849 B`) | L2 VWAP slippage vs. contract size across 5 depth tiers |
| [`docs/proof/dashboard-desktop.png`](proof/dashboard-desktop.png) | Chromium PNG (`1440×900` @2×) | `477 KB` (`488,187 B`) | Desktop Live Scanner terminal screenshot |
| [`docs/proof/dashboard-mobile.png`](proof/dashboard-mobile.png) | Chromium PNG (`390×844` @2×) | `163 KB` (`167,144 B`) | Mobile responsive terminal screenshot (zero overflow) |
| [`docs/proof/walkthrough.mp4`](proof/walkthrough.mp4) | H.264 MP4 (`1280×720`, 25 fps) | `1.94 MB` (`2,033,338 B`) | Compressed screen-recorded terminal walkthrough |
| [`docs/proof/walkthrough.webm`](proof/walkthrough.webm) | VP9 WebM (`1280×720`, 25 fps) | `5.98 MB` (`6,268,141 B`) | Native Playwright screen recording |
| [`docs/proof/walkthrough.gif`](proof/walkthrough.gif) | Palette GIF (`800×450`, 6 fps) | `6.65 MB` (`6,969,901 B`) | Lightweight README walkthrough preview |

---

## 5. Secrets & Hygiene Audit

- `.env`, `.venv/`, `out/`, `state/`, `__pycache__/`, and `markets.json` remain excluded via `.gitignore`.
- No API keys, private tokens, or credentials exist in tracked git commits.
- Upstream MIT License (`LICENSE`) and attribution (`NOTICE.md`, `README.md`) preserved intact.
