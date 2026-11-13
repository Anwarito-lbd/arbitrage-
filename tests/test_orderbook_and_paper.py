"""Comprehensive tests for settlement rule verification, L2 order-book slippage,
$1,000 paper-account risk accounting, and deterministic replay.
"""
from __future__ import annotations

import pytest

from backtest.paper_replay import run_paper_replay
from src.arbitrage import Book, Pricer
from src.matcher import Pair, tradeable, verify_rule_compatibility
from src.orderbook import (
    DepthLevel,
    L2Book,
    default_kalshi_fee,
    default_polymarket_fee,
    simulate_cross_venue_pair,
)
from trading.paper_account import PaperAccount


def test_settlement_rule_verification_audit():
    same_pair = Pair("e1", "Fed Cut", "Yes", "kalshi:FED", "polymarket:fed", False, "same")
    insuff_pair = Pair("e2", "CPI", "Yes", "kalshi:CPI", "polymarket:cpi", False, "insufficient", "Missing timezone")
    mismatch_pair = Pair("e3", "TikTok", "Yes", "kalshi:TT", "polymarket:tt", False, "not_same", "Different triggers")
    unknown_pair = Pair("e4", "Unknown", "Yes", "kalshi:U", "polymarket:u", False, "unknown")

    assert tradeable(same_pair, require_same=True)
    assert not tradeable(insuff_pair, require_same=True)
    assert tradeable(insuff_pair, require_same=False)
    assert not tradeable(mismatch_pair, require_same=False)

    a_same = verify_rule_compatibility(same_pair)
    a_mismatch = verify_rule_compatibility(mismatch_pair, require_same=False)
    a_unknown = verify_rule_compatibility(unknown_pair)
    assert a_same.accepted and a_same.verdict == "same"
    assert not a_mismatch.accepted and "triggers" in a_mismatch.reason
    assert not a_unknown.accepted and a_unknown.verdict == "unknown"


def test_l2_order_book_vwap_slippage_and_partial_fill():
    book = L2Book(
        bids=[DepthLevel(0.44, 100), DepthLevel(0.43, 200)],
        asks=[DepthLevel(0.45, 100), DepthLevel(0.46, 200), DepthLevel(0.48, 300)],
    )
    k_fee = default_kalshi_fee()

    # Exact top-of-book fill (zero slippage)
    f1 = book.simulate_buy(100, side="yes", fee_schedule=k_fee)
    assert f1 is not None
    assert f1.vwap_price == pytest.approx(0.45)
    assert f1.slippage_per_contract == pytest.approx(0.0)
    assert f1.slippage_bps == pytest.approx(0.0)
    assert f1.levels_consumed == 1
    assert not f1.partial_fill

    # Multi-level fill: 100 @ 0.45 + 200 @ 0.46 = 137 / 300 = 0.456667
    f2 = book.simulate_buy(300, side="yes", fee_schedule=k_fee)
    assert f2 is not None
    assert f2.vwap_price == pytest.approx((100 * 0.45 + 200 * 0.46) / 300, rel=1e-5)
    assert f2.slippage_per_contract > 0.0
    assert f2.slippage_bps > 100.0
    assert f2.levels_consumed == 2
    assert not f2.partial_fill

    # Requesting beyond total ask depth (600) produces a capped partial fill
    f3 = book.simulate_buy(1000, side="yes", fee_schedule=k_fee)
    assert f3 is not None
    assert f3.filled_contracts == 600.0
    assert f3.partial_fill


def test_l2_cross_venue_pair_slippage_erodes_edge():
    kalshi = L2Book.from_top(0.42, 0.43, top_size=200, levels=4, tick=0.01, depth_multiplier=1.2)
    poly = L2Book.from_top(0.50, 0.51, top_size=250, levels=4, tick=0.01, depth_multiplier=1.2)
    k_fee = default_kalshi_fee()
    p_fee = default_polymarket_fee(0.0)

    q_small = simulate_cross_venue_pair(kalshi, poly, 150, k_fee, p_fee)[0]
    q_large = simulate_cross_venue_pair(kalshi, poly, 700, k_fee, p_fee)[0]
    assert q_small.direction == "kalshi_yes"
    assert q_small.slippage_per_pair == pytest.approx(0.0)
    assert q_large.slippage_per_pair > 0.0
    assert q_large.net_edge_per_pair < q_small.net_edge_per_pair


def test_l2_flipped_book_symmetry():
    raw = L2Book(
        bids=[DepthLevel(0.54, 400), DepthLevel(0.53, 500)],
        asks=[DepthLevel(0.55, 300), DepthLevel(0.56, 600)],
    )
    flipped = raw.flipped()
    assert flipped.best_bid == DepthLevel(0.45, 300)
    assert flipped.best_ask == DepthLevel(0.46, 400)


def test_paper_account_1000_risk_caps_and_settlement():
    acct = PaperAccount(initial_capital=1000.0, max_position_pct=0.45, max_open_positions=2, min_entry_edge=0.02)
    # Budget cap is 45% of $1,000 = $450 -> at $0.95/pair, max affordable = 473 contracts
    qty = acct.size_contracts(0.95, 1000)
    assert qty == 473

    pos = acct.open_position(
        kalshi_id="kalshi:M1",
        poly_id="polymarket:m1",
        market_name="Market 1",
        direction="kalshi_yes",
        yes_venue="kalshi",
        yes_price=0.44,
        no_venue="polymarket",
        no_price=0.49,
        contracts=400,
        entry_fees_usd=6.90,
        slippage_usd=0.0,
        opened_ms=1_000_000,
    )
    assert pos.deployed_capital == pytest.approx(400 * 0.93 + 6.90, abs=1e-4)
    assert acct.cash == pytest.approx(1000.0 - pos.deployed_capital, abs=1e-4)

    # Exceeding single-market cap raises ValueError
    with pytest.raises(ValueError, match="cap|cash"):
        acct.open_position(
            kalshi_id="kalshi:M2",
            poly_id="polymarket:m2",
            market_name="Market 2",
            direction="kalshi_yes",
            yes_venue="kalshi",
            yes_price=0.45,
            no_venue="polymarket",
            no_price=0.48,
            contracts=600,
            entry_fees_usd=10.0,
            slippage_usd=0.0,
            opened_ms=2_000_000,
        )

    # Settle at resolution: pays $1.00/contract regardless of outcome
    trade = acct.settle_position("kalshi:M1", winning_side="no", closed_ms=5_000_000)
    assert trade.gross_payout == pytest.approx(400.0)
    assert trade.pnl_usd == pytest.approx(400.0 - pos.deployed_capital, abs=1e-4)
    assert acct.cash == pytest.approx(1000.0 + trade.pnl_usd, abs=1e-4)


def test_paper_account_mark_equity_and_sharpe():
    acct = PaperAccount(initial_capital=1000.0)
    pricer = Pricer(default_kalshi_fee(), default_polymarket_fee(0.0), contracts=300)
    acct.open_position(
        kalshi_id="kalshi:M1",
        poly_id="polymarket:m1",
        market_name="Market 1",
        direction="kalshi_yes",
        yes_venue="kalshi",
        yes_price=0.44,
        no_venue="polymarket",
        no_price=0.49,
        contracts=300,
        entry_fees_usd=5.18,
        slippage_usd=0.0,
        opened_ms=0,
    )
    # Day 1 wide spread (mark-to-market liquidation dips below $1000)
    p1 = acct.mark_equity(86_400_000, {"kalshi:M1": (Book(0.40, 0.42), Book(0.48, 0.49), pricer)})
    # Day 2 converged spread
    p2 = acct.mark_equity(2 * 86_400_000, {"kalshi:M1": (Book(0.48, 0.49), Book(0.47, 0.48), pricer)})
    assert p1.mark_equity < 1000.0
    assert p1.drawdown_pct > 0.0
    assert p2.mark_equity > p1.mark_equity
    acct.settle_position("kalshi:M1", winning_side="yes", closed_ms=3 * 86_400_000)
    acct.mark_equity(3 * 86_400_000, {})
    summary = acct.summary()
    assert summary["initial_capital"] == 1000.0
    assert summary["final_equity"] > 1000.0
    assert summary["max_drawdown_pct"] > 0.0


def test_deterministic_1000_paper_replay_is_reproducible():
    r1 = run_paper_replay(seed=42)
    r2 = run_paper_replay(seed=42)
    assert r1["summary"] == r2["summary"]
    assert r1["summary"]["simulation_only"] is True
    assert r1["summary"]["initial_capital"] == 1000.0
    assert r1["summary"]["total_trades"] > 10
    assert r1["summary"]["final_equity"] > 1000.0
    assert len(r1["meta"]["rejected_markets"]) == 1
    assert r1["meta"]["rejected_markets"][0]["rules"] == "not_same"
