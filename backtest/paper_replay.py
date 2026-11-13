"""Deterministic $1,000.00 Simulated Paper Account Replay & Proof Chart Generator.

Runs a 60-day walk-forward L2 order book simulation across 6 cross-venue
prediction market pairs (5 verified `same`-rule pairs + 1 `not_same` pair
rejected by the settlement rule verifier) with zero live orders or API keys.

Usage:
    python -m backtest.paper_replay
"""
from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np

from src.arbitrage import Book, Leg, Opportunity, Pricer
from src.matcher import Pair, verify_rule_compatibility
from src.orderbook import (
    L2Book,
    default_kalshi_fee,
    default_polymarket_fee,
    simulate_cross_venue_pair,
)
from src.strategy import Strategy
from trading.paper_account import PaperAccount

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
START_MS = int(datetime(2026, 7, 15, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)

BG = "#0b0f17"
PANEL = "#111827"
TEXT = "#f1f5f9"
MUTED = "#94a3b8"
GRID = "#1e293b"
EDGE = "#334155"
GREEN = "#00f090"
CYAN = "#38bdf8"
VIOLET = "#a78bfa"
AMBER = "#f59e0b"
ROSE = "#fb7185"
MONO = "DejaVu Sans Mono"


@dataclass(frozen=True)
class ReplayMarketSpec:
    pair: Pair
    category: str
    init_prob: float
    vol: float
    shock_prob: float
    base_top_size: float
    days_to_resolution: int
    settle_outcome: str


DEFAULT_UNIVERSE: tuple[ReplayMarketSpec, ...] = (
    ReplayMarketSpec(
        pair=Pair(
            event_id="evt-fed-dec26",
            event="US Federal Reserve Rate Cut Dec 2026",
            outcome="Cut >= 25bps",
            kalshi_id="kalshi:FED-DEC26-CUT25",
            poly_id="polymarket:fed-dec26-cut25",
            flipped=False,
            rules="same",
            rules_reason="Both resolve on official FOMC target rate announcement.",
        ),
        category="economics",
        init_prob=0.47,
        vol=0.011,
        shock_prob=0.045,
        base_top_size=420.0,
        days_to_resolution=45,
        settle_outcome="yes",
    ),
    ReplayMarketSpec(
        pair=Pair(
            event_id="evt-cpi-q426",
            event="US Headline CPI YoY < 2.5% in Q4 2026",
            outcome="Yes",
            kalshi_id="kalshi:CPI-Q426-U25",
            poly_id="polymarket:cpi-q426-u25",
            flipped=False,
            rules="same",
            rules_reason="Both resolve on BLS CPI-U NSA release.",
        ),
        category="economics",
        init_prob=0.41,
        vol=0.010,
        shock_prob=0.040,
        base_top_size=380.0,
        days_to_resolution=60,
        settle_outcome="no",
    ),
    ReplayMarketSpec(
        pair=Pair(
            event_id="evt-btc-120k",
            event="Bitcoin >= $120,000 Before 2027",
            outcome="Yes",
            kalshi_id="kalshi:BTC-120K-EOY26",
            poly_id="polymarket:btc-120k-eoy26",
            flipped=True,
            rules="same",
            rules_reason="Both resolve on CF Benchmarks BRTI / spot index; Poly framed inversely.",
        ),
        category="crypto",
        init_prob=0.52,
        vol=0.014,
        shock_prob=0.050,
        base_top_size=520.0,
        days_to_resolution=60,
        settle_outcome="yes",
    ),
    ReplayMarketSpec(
        pair=Pair(
            event_id="evt-starship-ft6",
            event="SpaceX Starship Booster Catch Flight 6",
            outcome="Catch Successful",
            kalshi_id="kalshi:SPACEX-CATCH6",
            poly_id="polymarket:spacex-catch6",
            flipped=False,
            rules="same",
            rules_reason="Both resolve on SpaceX official broadcast tower catch confirmation.",
        ),
        category="science",
        init_prob=0.60,
        vol=0.012,
        shock_prob=0.042,
        base_top_size=340.0,
        days_to_resolution=32,
        settle_outcome="yes",
    ),
    ReplayMarketSpec(
        pair=Pair(
            event_id="evt-senate-26",
            event="US Senate Majority Control Confirmed",
            outcome="Majority >= 51 Seats",
            kalshi_id="kalshi:SENATE-CTRL26",
            poly_id="polymarket:senate-ctrl26",
            flipped=False,
            rules="same",
            rules_reason="Both resolve on AP call + official Senate certification.",
        ),
        category="politics",
        init_prob=0.55,
        vol=0.009,
        shock_prob=0.038,
        base_top_size=460.0,
        days_to_resolution=58,
        settle_outcome="no",
    ),
    ReplayMarketSpec(
        pair=Pair(
            event_id="evt-tiktok-rule-mismatch",
            event="TikTok US Ban Enforcement Ruling",
            outcome="Enforced",
            kalshi_id="kalshi:TIKTOK-BAN26",
            poly_id="polymarket:tiktok-ban26",
            flipped=False,
            rules="not_same",
            rules_reason="Kalshi settles on DOJ enforcement action; Polymarket settles on Supreme Court stay expiration.",
        ),
        category="politics",
        init_prob=0.38,
        vol=0.013,
        shock_prob=0.060,
        base_top_size=300.0,
        days_to_resolution=50,
        settle_outcome="no",
    ),
)


def _clamp_px(x: float) -> float:
    return round(min(0.92, max(0.08, x)), 2)


def generate_synthetic_l2_series(
    spec: ReplayMarketSpec,
    *,
    bars: int = 1440,
    start_ms: int = START_MS,
    seed: int = 42,
) -> list[tuple[int, L2Book, L2Book]]:
    """Generate deterministic hourly L2 books for Kalshi and Polymarket.

    Returns normalized books (Polymarket already normalized via `.flipped()` when needed,
    matching `LiveFeed.books()` semantics).
    """
    rng = random.Random(seed)
    p_fair = spec.init_prob
    k_disloc = 0.0
    p_disloc = 0.0
    series: list[tuple[int, L2Book, L2Book]] = []

    for i in range(bars):
        t_ms = start_ms + i * HOUR_MS
        # Mean-reverting latent fair probability
        p_fair += 0.03 * (spec.init_prob - p_fair) + rng.gauss(0.0, spec.vol)
        p_fair = min(0.84, max(0.16, p_fair))

        # Venue-specific flow dislocations that mean-revert over ~8-20 hours
        k_disloc = 0.84 * k_disloc + rng.gauss(0.0, 0.0045)
        p_disloc = 0.84 * p_disloc + rng.gauss(0.0, 0.0040)

        # Occasional liquidity shock creating genuine cross-venue arbitrage windows
        if rng.random() < spec.shock_prob:
            mag = rng.uniform(0.035, 0.068)
            if rng.random() < 0.5:
                k_disloc -= mag
                p_disloc += mag * 0.35
            else:
                p_disloc -= mag
                k_disloc += mag * 0.35

        k_mid = p_fair + k_disloc
        p_mid = p_fair + p_disloc

        k_half = 0.005 if rng.random() < 0.7 else 0.01
        p_half = 0.005

        k_bid = _clamp_px(k_mid - k_half)
        k_ask = max(round(k_bid + 0.01, 2), _clamp_px(k_mid + k_half))
        p_bid = _clamp_px(p_mid - p_half)
        p_ask = max(round(p_bid + 0.01, 2), _clamp_px(p_mid + p_half))

        k_sz = max(120.0, round(spec.base_top_size * rng.uniform(0.65, 1.55), 0))
        p_sz = max(160.0, round(spec.base_top_size * 1.25 * rng.uniform(0.65, 1.65), 0))

        k_l2 = L2Book.from_top(k_bid, k_ask, top_size=k_sz, levels=5, tick=0.01, depth_multiplier=1.35)
        p_raw = L2Book.from_top(p_bid, p_ask, top_size=p_sz, levels=5, tick=0.01, depth_multiplier=1.45)
        # Verify flipped invariant by constructing raw venue book if flipped, then flipping back
        if spec.pair.flipped:
            p_raw_venue = p_raw.flipped()
            p_l2 = p_raw_venue.flipped()
        else:
            p_l2 = p_raw

        series.append((t_ms, k_l2, p_l2))

    return series


def run_paper_replay(
    *,
    initial_capital: float = 1000.0,
    bars: int = 1440,
    seed: int = 42,
    entry_edge: float = 0.02,
    take_profit: float = 0.02,
    poly_taker_rate: float = 0.0,
    universe: tuple[ReplayMarketSpec, ...] = DEFAULT_UNIVERSE,
) -> dict[str, Any]:
    """Run deterministic walk-forward $1,000 paper account replay."""
    kalshi_fee = default_kalshi_fee()
    poly_fee = default_polymarket_fee(taker_rate=poly_taker_rate)
    strategy = Strategy(entry_edge=entry_edge, take_profit=take_profit)
    account = PaperAccount(
        initial_capital=initial_capital,
        max_position_pct=0.45,
        max_open_positions=4,
        min_entry_edge=entry_edge,
        take_profit=take_profit,
    )

    accepted_specs: list[ReplayMarketSpec] = []
    rejected_markets: list[dict[str, str]] = []
    for spec in universe:
        audit = verify_rule_compatibility(spec.pair, require_same=True)
        if audit.accepted:
            accepted_specs.append(spec)
        else:
            rejected_markets.append(
                {
                    "kalshi_id": spec.pair.kalshi_id,
                    "name": spec.pair.name,
                    "rules": audit.verdict,
                    "reason": audit.reason,
                }
            )

    market_series: dict[str, list[tuple[int, L2Book, L2Book]]] = {
        spec.pair.kalshi_id: generate_synthetic_l2_series(
            spec, bars=bars, start_ms=START_MS, seed=seed + idx * 101
        )
        for idx, spec in enumerate(accepted_specs)
    }

    pricers: dict[str, Pricer] = {
        spec.pair.kalshi_id: Pricer(kalshi_fee, poly_fee, contracts=250)
        for spec in accepted_specs
    }
    spec_by_id = {spec.pair.kalshi_id: spec for spec in accepted_specs}

    # Pre-sample spread vs fee observations for analytics chart
    observed_spread_samples: list[dict[str, float]] = []

    for bar_idx in range(bars):
        t_ms = START_MS + bar_idx * HOUR_MS
        mark_books: dict[str, tuple[Book, Book, Pricer]] = {}

        for spec in accepted_specs:
            kid = spec.pair.kalshi_id
            _, k_l2, p_l2 = market_series[kid][bar_idx]
            k_top, p_top = k_l2.top_book(), p_l2.top_book()
            pricer = Pricer(kalshi_fee, poly_fee, contracts=max(1, account.positions[kid].contracts if kid in account.positions else 250))
            mark_books[kid] = (k_top, p_top, pricer)

            # 1) Check open position for early convergence unwind
            if kid in account.positions:
                pos = account.positions[kid]
                opp = Opportunity(
                    direction=pos.direction,
                    yes=Leg(pos.yes_venue, "yes", pos.yes_price, pos.entry_fees_usd / (2 * pos.contracts)),
                    no=Leg(pos.no_venue, "no", pos.no_price, pos.entry_fees_usd / (2 * pos.contracts)),
                )
                round_trip = pricer.unwind(opp, k_top, p_top)
                exit_prices = pricer.exit_prices(opp, k_top, p_top)
                if bar_idx > (pos.opened_ms - START_MS) // HOUR_MS and strategy.should_exit(round_trip) and exit_prices is not None:
                    y_bid, n_bid = exit_prices
                    exit_fee = (
                        pricer.leg(pos.yes_venue, "yes", y_bid).fee
                        + pricer.leg(pos.no_venue, "no", n_bid).fee
                    ) * pos.contracts
                    account.close_position(
                        kid,
                        yes_exit_price=y_bid,
                        no_exit_price=n_bid,
                        exit_fees_usd=exit_fee,
                        closed_ms=t_ms,
                        exit_reason="early_convergence",
                    )
                continue

            # 2) Scan for new entry using L2 order books + capital risk limits
            probe_quotes = simulate_cross_venue_pair(k_l2, p_l2, 200, kalshi_fee, poly_fee)
            if not probe_quotes:
                continue
            best_probe = probe_quotes[0]
            if best_probe.gross_cost_per_pair < 1.0:
                observed_spread_samples.append(
                    {
                        "gross_spread_cents": round((1.0 - best_probe.gross_cost_per_pair) * 100.0, 3),
                        "fees_plus_slip_cents": round((best_probe.fees_per_pair + best_probe.slippage_per_pair) * 100.0, 3),
                        "net_edge_cents": round(best_probe.net_edge_per_pair * 100.0, 3),
                        "yes_price": round(best_probe.yes_fill.vwap_price, 4),
                    }
                )

            if best_probe.net_edge_per_pair < strategy.entry_edge:
                continue

            qty = account.size_contracts(best_probe.net_cost_per_pair, best_probe.max_fillable_contracts)
            if qty < 25:
                continue

            # Re-simulate at exact sized contract quantity `qty`
            sized_quotes = simulate_cross_venue_pair(k_l2, p_l2, qty, kalshi_fee, poly_fee)
            if not sized_quotes:
                continue
            q = next((sq for sq in sized_quotes if sq.direction == best_probe.direction), sized_quotes[0])
            if q.net_edge_per_pair < strategy.entry_edge:
                continue

            req_cap = round(q.contracts * q.gross_cost_per_pair + q.yes_fill.total_fee + q.no_fill.total_fee, 6)
            allowed, _ = account.can_open(kid, req_cap)
            if not allowed:
                continue

            account.open_position(
                kalshi_id=kid,
                poly_id=spec.pair.poly_id,
                market_name=spec.pair.name,
                direction=q.direction,
                yes_venue=q.yes_venue,
                yes_price=q.yes_fill.vwap_price,
                no_venue=q.no_venue,
                no_price=q.no_fill.vwap_price,
                contracts=int(q.contracts),
                entry_fees_usd=q.yes_fill.total_fee + q.no_fill.total_fee,
                slippage_usd=round(q.slippage_per_pair * q.contracts, 6),
                opened_ms=t_ms,
            )

        account.mark_equity(t_ms, mark_books)

    # 3) Settle any open positions at resolution ($1.00 deterministic pair payout)
    end_ms = START_MS + bars * HOUR_MS
    for kid in list(account.positions.keys()):
        spec = spec_by_id[kid]
        account.settle_position(kid, winning_side=spec.settle_outcome, closed_ms=end_ms)
    account.mark_equity(end_ms, {})

    # Sample slippage curve on the first market's midpoint book for diagnostics
    sample_k = L2Book.from_top(0.44, 0.45, top_size=350, levels=5, tick=0.01, depth_multiplier=1.35)
    sample_p = L2Book.from_top(0.49, 0.50, top_size=480, levels=5, tick=0.01, depth_multiplier=1.45)
    slippage_curve = []
    for sz in (50, 100, 200, 350, 500, 750, 1000, 1250, 1500, 2000, 2500):
        sq = simulate_cross_venue_pair(sample_k, sample_p, sz, kalshi_fee, poly_fee)
        if sq:
            best = sq[0]
            slippage_curve.append(
                {
                    "requested_contracts": sz,
                    "filled_contracts": best.contracts,
                    "slippage_cents_per_pair": round(best.slippage_per_pair * 100.0, 3),
                    "slippage_bps": round((best.slippage_per_pair / best.gross_cost_per_pair) * 10_000.0, 2),
                    "fees_cents_per_pair": round(best.fees_per_pair * 100.0, 3),
                    "net_edge_cents_per_pair": round(best.net_edge_per_pair * 100.0, 3),
                }
            )

    summary = account.summary()
    return {
        "meta": {
            "simulation_only": True,
            "seed": seed,
            "bars": bars,
            "timeframe": "1h",
            "start_utc": datetime.fromtimestamp(START_MS / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "end_utc": datetime.fromtimestamp(end_ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "strategy": strategy.describe(),
            "max_position_pct": account.max_position_pct,
            "max_open_positions": account.max_open_positions,
            "poly_taker_rate": poly_taker_rate,
            "accepted_markets_count": len(accepted_specs),
            "rejected_markets": rejected_markets,
        },
        "summary": summary,
        "trades": account.serialize_trades(),
        "equity_curve": [
            {
                "timestamp_ms": pt.timestamp_ms,
                "cash": pt.cash,
                "deployed_capital": pt.deployed_capital,
                "mark_equity": pt.mark_equity,
                "hedged_equity": pt.hedged_equity,
                "drawdown_pct": pt.drawdown_pct,
            }
            for pt in account.equity_curve
        ],
        "spread_samples": observed_spread_samples[:500],
        "slippage_curve": slippage_curve,
        "sample_books": {
            "kalshi_asks": [{"price": l.price, "size": l.size} for l in sample_k.asks],
            "poly_no_asks": [{"price": l.price, "size": l.size} for l in sample_p.no_asks],
        },
    }


def _style_ax(ax: plt.Axes) -> None:
    ax.set_facecolor(PANEL)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    for sp in ("left", "bottom"):
        ax.spines[sp].set_color(EDGE)
    ax.grid(color=GRID, lw=0.8, alpha=0.85)
    ax.tick_params(colors=MUTED, labelsize=9)


def generate_proof_charts(replay: dict[str, Any], out_dir: Path = Path("docs/proof")) -> list[Path]:
    """Generate the 3 high-resolution proof charts in `docs/proof/`."""
    out_dir.mkdir(parents=True, exist_ok=True)
    generated: list[Path] = []
    s = replay["summary"]
    eq = replay["equity_curve"]
    trades = replay["trades"]

    # 1. equity_1000_paper.png
    fig = plt.figure(figsize=(14, 9.2), dpi=180)
    fig.patch.set_facecolor(BG)
    gs = fig.add_gridspec(3, 1, height_ratios=[2.1, 1.0, 1.2], hspace=0.38, left=0.07, right=0.97, top=0.79, bottom=0.08)
    ax_eq = fig.add_subplot(gs[0])
    ax_dd = fig.add_subplot(gs[1], sharex=ax_eq)
    ax_tr = fig.add_subplot(gs[2])

    fig.text(0.03, 0.96, "Simulated $1,000 Paper Account Replay — 60-Day Cross-Venue Arbitrage",
             fontsize=16, fontweight="bold", color=TEXT)
    fig.text(0.03, 0.925, f"Kalshi x Polymarket L2 Order-Book Replay  |  {replay['meta']['start_utc']} to {replay['meta']['end_utc']}  |  SIMULATION ONLY (NO LIVE ORDERS)",
             fontsize=9.8, color=MUTED, fontfamily=MONO)

    kpis = [
        ("INITIAL CAPITAL", f"${s['initial_capital']:,.2f}", TEXT),
        ("FINAL EQUITY", f"${s['final_equity']:,.2f}", GREEN),
        ("NET PROFIT (ROI)", f"+${s['realized_pnl']:,.2f} (+{s['roi_pct']:.2f}%)", GREEN),
        ("SHARPE (DAILY ANN.)", f"{s['sharpe_ratio_annualized']:.2f}", CYAN),
        ("MAX MTM DRAWDOWN", f"-{s['max_drawdown_pct']:.2f}% (-${s['max_drawdown_usd']:.2f})", AMBER),
        ("TRADES / WIN RATE", f"{s['total_trades']} ({s['win_rate_pct']:.1f}%)", VIOLET),
    ]
    for i, (lbl, val, col) in enumerate(kpis):
        x = 0.03 + i * 0.162
        fig.text(x, 0.875, lbl, fontsize=8, color=MUTED, fontfamily=MONO)
        fig.text(x, 0.845, val, fontsize=12.5, fontweight="bold", color=col, fontfamily=MONO)

    ts = [datetime.fromtimestamp(p["timestamp_ms"] / 1000, timezone.utc) for p in eq]
    hedged = [p["hedged_equity"] for p in eq]
    mark = [p["mark_equity"] for p in eq]
    dd = [-p["drawdown_pct"] for p in eq]

    _style_ax(ax_eq)
    ax_eq.axhline(s["initial_capital"], color=EDGE, ls="--", lw=1.1, label="Initial $1,000.00")
    ax_eq.plot(ts, mark, color=CYAN, lw=1.2, alpha=0.8, label="Mark-to-Market Liquidation Equity")
    ax_eq.plot(ts, hedged, color=GREEN, lw=2.3, label="Hedged Cost-Basis Equity (Realized)")
    ax_eq.fill_between(ts, s["initial_capital"], hedged, color=GREEN, alpha=0.10)
    ax_eq.set_ylabel("Equity (USD)", color=MUTED, fontsize=9.5)
    ax_eq.yaxis.set_major_formatter(lambda v, _: f"${v:,.0f}")
    ax_eq.set_title("EQUITY CURVE ($1,000.00 STARTING CAPITAL)", loc="left", fontsize=9, color=MUTED, fontfamily=MONO)
    leg = ax_eq.legend(loc="upper left", frameon=False, fontsize=8.8, ncol=3)
    for t in leg.get_texts():
        t.set_color(TEXT)

    _style_ax(ax_dd)
    ax_dd.fill_between(ts, dd, 0, color=AMBER, alpha=0.25)
    ax_dd.plot(ts, dd, color=AMBER, lw=1.3)
    ax_dd.set_ylabel("Drawdown %", color=MUTED, fontsize=9.5)
    ax_dd.yaxis.set_major_formatter(lambda v, _: f"{v:.1f}%")
    ax_dd.set_title("MARK-TO-MARKET UNDERWATER DRAWDOWN", loc="left", fontsize=9, color=MUTED, fontfamily=MONO)
    locator = mdates.AutoDateLocator(minticks=5, maxticks=9)
    ax_dd.xaxis.set_major_locator(locator)
    ax_dd.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))

    _style_ax(ax_tr)
    pnls = [t["pnl_usd"] for t in trades]
    idx = np.arange(len(pnls))
    cols = [GREEN if t["exit_reason"] == "early_convergence" else VIOLET for t in trades]
    ax_tr.bar(idx, pnls, color=cols, width=0.62)
    ax_tr.axhline(0, color=EDGE, lw=1)
    ax_tr.set_ylabel("Trade P&L ($)", color=MUTED, fontsize=9.5)
    ax_tr.yaxis.set_major_formatter(lambda v, _: f"${v:,.0f}")
    ax_tr.set_xticks(idx[::max(1, len(idx) // 12)])
    ax_tr.set_xticklabels([trades[i]["trade_id"] for i in range(0, len(trades), max(1, len(trades) // 12))], fontfamily=MONO, fontsize=8)
    ax_tr.set_title("PER-TRADE REALIZED P&L (GREEN = EARLY CONVERGENCE, VIOLET = SETTLED AT RESOLUTION)", loc="left", fontsize=9, color=MUTED, fontfamily=MONO)

    fig.text(0.97, 0.015, f"Deterministic seed={replay['meta']['seed']} | Total Fees Paid: USD {s['total_fees_paid']:.2f} | L2 Slippage Cost: USD {s['total_slippage_cost']:.2f} | Simulated Paper Replay Only",
             ha="right", fontsize=8, color=MUTED, fontfamily=MONO)
    p1 = out_dir / "equity_1000_paper.png"
    fig.savefig(p1, dpi=180, facecolor=BG)
    plt.close(fig)
    generated.append(p1)

    # 2. spread_vs_fees.png
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6.0), dpi=180)
    fig.patch.set_facecolor(BG)
    fig.suptitle("Venue Taker Fee Schedules vs. Cross-Venue Arbitrage Spread", fontsize=14.5, fontweight="bold", color=TEXT, y=0.96)

    prices = np.linspace(0.03, 0.97, 95)
    k_sched = default_kalshi_fee()
    p_zero = default_polymarket_fee(0.0)
    p_four = default_polymarket_fee(0.04)

    k_1000 = [ (k_sched.estimate(float(p), 1000) or 0.0) / 1000 * 100 for p in prices ]
    k_25 = [ (k_sched.estimate(float(p), 25) or 0.0) / 25 * 100 for p in prices ]
    poly_4 = [ (p_four.estimate(float(p), 1000) or 0.0) / 1000 * 100 for p in prices ]
    poly_0 = [ (p_zero.estimate(float(p), 1000) or 0.0) / 1000 * 100 for p in prices ]

    _style_ax(ax1)
    ax1.plot(prices * 100, k_1000, color=CYAN, lw=2.2, label="Kalshi Quadratic Fee (1,000 contracts)")
    ax1.plot(prices * 100, k_25, color=AMBER, lw=1.6, ls="--", label="Kalshi Fee w/ Cent Rounding (25 contracts)")
    ax1.plot(prices * 100, poly_4, color=VIOLET, lw=1.8, label="Polymarket 4% Quadratic Theta")
    ax1.plot(prices * 100, poly_0, color=GREEN, lw=1.8, ls=":", label="Polymarket Standard CTF (0.0% Fee)")
    ax1.axhline(2.0, color=ROSE, lw=1.1, ls="-.", label="2.0c Min Entry Edge Hurdle")
    ax1.set_ylim(-0.08, 2.75)
    ax1.set_title("Per-Contract Taker Fee vs. Contract Price", color=TEXT, fontsize=11, pad=10)
    ax1.set_xlabel("Contract Price (c)", color=MUTED)
    ax1.set_ylabel("Fee per Contract (c)", color=MUTED)
    leg1 = ax1.legend(loc="upper right", frameon=True, facecolor=PANEL, edgecolor=EDGE, fontsize=8.1)
    for t in leg1.get_texts():
        t.set_color(TEXT)

    _style_ax(ax2)
    samples = replay["spread_samples"]
    gross = [x["gross_spread_cents"] for x in samples]
    costs = [x["fees_plus_slip_cents"] for x in samples]
    net = [x["net_edge_cents"] for x in samples]
    cols_sc = [GREEN if n >= 2.0 else ROSE for n in net]
    ax2.scatter(gross, net, c=cols_sc, alpha=0.65, s=26, edgecolors="none", label="Observed Opportunity Bars")
    g_line = np.linspace(0, max(gross + [8.0]), 50)
    avg_cost = float(np.mean(costs)) if costs else 1.5
    ax2.plot(g_line, g_line - avg_cost, color=CYAN, lw=1.6, ls="--", label=f"Net = Gross - Mean Cost ({avg_cost:.2f}c)")
    ax2.axhline(2.0, color=AMBER, lw=1.3, ls="-.", label="Entry Threshold (+2.0c Net)")
    ax2.axhline(0.0, color=EDGE, lw=1.0)
    ax2.set_title("Gross Cross-Venue Spread vs. Net Edge After Fees & Slippage", color=TEXT, fontsize=11, pad=10)
    ax2.set_xlabel("Gross Spread: 100c - (YES + NO) [c]", color=MUTED)
    ax2.set_ylabel("Net Edge After Fees & L2 Slippage [c]", color=MUTED)
    leg2 = ax2.legend(loc="upper left", frameon=False, fontsize=8.3)
    for t in leg2.get_texts():
        t.set_color(TEXT)

    fig.tight_layout(rect=(0, 0.02, 1, 0.92))
    p2 = out_dir / "spread_vs_fees.png"
    fig.savefig(p2, dpi=180, facecolor=BG)
    plt.close(fig)
    generated.append(p2)

    # 3. orderbook_depth_slippage.png
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6.0), dpi=180)
    fig.patch.set_facecolor(BG)
    fig.suptitle("L2 Order-Book Depth & Execution Slippage vs. Contract Size", fontsize=14.5, fontweight="bold", color=TEXT, y=0.96)

    k_asks = replay["sample_books"]["kalshi_asks"]
    p_no_asks = replay["sample_books"]["poly_no_asks"]

    _style_ax(ax1)
    k_px = [l["price"] * 100 for l in k_asks]
    k_cum = np.cumsum([l["size"] for l in k_asks])
    p_px = [l["price"] * 100 for l in p_no_asks]
    p_cum = np.cumsum([l["size"] for l in p_no_asks])
    ax1.step(k_px, k_cum, where="post", color=CYAN, lw=2.3, label="Kalshi YES Ask Cumulative Depth")
    ax1.step(p_px, p_cum, where="post", color=VIOLET, lw=2.3, label="Polymarket NO Ask Cumulative Depth")
    ax1.set_title("5-Level L2 Ask Depth Ladder (Sample Pair)", color=TEXT, fontsize=11, pad=10)
    ax1.set_xlabel("Execution Price Level (c)", color=MUTED)
    ax1.set_ylabel("Cumulative Fillable Contracts", color=MUTED)
    leg3 = ax1.legend(loc="upper left", frameon=False, fontsize=8.8)
    for t in leg3.get_texts():
        t.set_color(TEXT)

    _style_ax(ax2)
    sc = replay["slippage_curve"]
    req = [x["requested_contracts"] for x in sc]
    slip_bps = [x["slippage_bps"] for x in sc]
    net_c = [x["net_edge_cents_per_pair"] for x in sc]

    ax2.plot(req, slip_bps, color=AMBER, marker="o", ms=5, lw=2.2, label="VWAP Execution Slippage (bps)")
    ax2.set_xlabel("Order Size (Contracts per Leg)", color=MUTED)
    ax2.set_ylabel("Execution Slippage (bps)", color=AMBER)
    ax2.tick_params(axis="y", colors=AMBER)

    ax2_r = ax2.twinx()
    ax2_r.spines["top"].set_visible(False)
    ax2_r.spines["left"].set_visible(False)
    ax2_r.spines["bottom"].set_visible(False)
    ax2_r.spines["right"].set_color(EDGE)
    ax2_r.plot(req, net_c, color=GREEN, marker="s", ms=5, lw=2.2, ls="--", label="Net Arbitrage Edge (c/pair)")
    ax2_r.axhline(2.0, color=ROSE, lw=1.0, ls=":", label="2.0c Min Entry Edge")
    ax2_r.set_ylabel("Net Arbitrage Edge (c/pair)", color=GREEN)
    ax2_r.tick_params(axis="y", colors=GREEN)
    ax2.set_title("Slippage & Edge Erosion Across Order Sizes", color=TEXT, fontsize=11, pad=10)

    lines1, labels1 = ax2.get_legend_handles_labels()
    lines2, labels2 = ax2_r.get_legend_handles_labels()
    leg4 = ax2.legend(lines1 + lines2, labels1 + labels2, loc="center right", frameon=False, fontsize=8.3)
    for t in leg4.get_texts():
        t.set_color(TEXT)

    fig.tight_layout(rect=(0, 0.02, 1, 0.92))
    p3 = out_dir / "orderbook_depth_slippage.png"
    fig.savefig(p3, dpi=180, facecolor=BG)
    plt.close(fig)
    generated.append(p3)

    return generated


def main() -> None:
    replay = run_paper_replay()
    out_json = Path("out") / "paper_replay_1000.json"
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(replay, indent=2), encoding="utf-8")
    charts = generate_proof_charts(replay, Path("docs/proof"))

    s = replay["summary"]
    print("=== SIMULATED $1,000 PAPER ACCOUNT REPLAY (NO LIVE ORDERS) ===")
    print(f"Initial Capital : ${s['initial_capital']:,.2f}")
    print(f"Final Equity    : ${s['final_equity']:,.2f}")
    print(f"Realized P&L    : +${s['realized_pnl']:,.2f} ({s['roi_pct']:+.2f}%)")
    print(f"Trades          : {s['total_trades']} ({s['winning_trades']} won, {s['losing_trades']} lost, win rate {s['win_rate_pct']:.1f}%)")
    print(f"Exit Breakdown  : {s['early_exits']} early convergence, {s['settled_at_resolution']} settled at resolution")
    print(f"Sharpe (Daily)  : {s['sharpe_ratio_annualized']:.3f}")
    print(f"Max MTM DD      : -{s['max_drawdown_pct']:.2f}% (-${s['max_drawdown_usd']:.2f})")
    print(f"Fees / Slippage : ${s['total_fees_paid']:.2f} fees / ${s['total_slippage_cost']:.2f} slippage")
    print(f"Rejected Rules  : {len(replay['meta']['rejected_markets'])} pair(s) skipped")
    print(f"JSON Ledger     : {out_json}")
    for c in charts:
        print(f"Chart           : {c}")


if __name__ == "__main__":
    main()
