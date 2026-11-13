"""ArbitrageX — Prediction Market Arbitrage FastAPI & WebSocket Server.

Serves the web terminal frontend, computes exact cross-venue YES/NO sizing
with Kalshi cent-rounded quadratic fees and L2 slippage, provides the
deterministic $1,000 Simulated Paper Account Replay ledger, and streams
simulated order-book ticks over WebSocket when no live API keys are set.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import random
import sys
import urllib.request
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
import uvicorn

from backtest.paper_replay import DEFAULT_UNIVERSE, run_paper_replay
from src.arbitrage import per_contract_fee
from src.matcher import verify_rule_compatibility
from src.orderbook import (
    L2Book,
    default_kalshi_fee,
    default_polymarket_fee,
    simulate_cross_venue_pair,
)
from trading.paper_account import PaperAccount

PORT = int(os.environ.get("PORT", 8000))
ROOT_DIR = Path(__file__).resolve().parent


class CalculateRequest(BaseModel):
    capital: float = Field(default=1000.0, gt=0)
    leg1_price: float = Field(default=0.46, gt=0.0, lt=1.0)
    leg2_price: float = Field(default=0.48, gt=0.0, lt=1.0)
    leg1_venue: str = "kalshi"
    leg2_venue: str = "polymarket"
    leg1_fee_rate: float | None = None
    leg2_fee_rate: float | None = None
    days_to_resolution: int = Field(default=30, ge=1, le=1095)
    market_id: str | None = None


class BotConfigRequest(BaseModel):
    min_entry_edge_pct: float = Field(default=2.0, ge=0.1, le=20.0)
    max_position_usd: float = Field(default=450.0, ge=25.0, le=50000.0)
    take_profit_pct: float = Field(default=2.0, ge=0.1, le=20.0)


class ExecuteTradeRequest(BaseModel):
    market_id: str
    capital: float | None = Field(default=None, gt=0)


class ClosePositionRequest(BaseModel):
    market_id: str
    mode: str = "early_convergence"


def _effective_fee_rate(venue: str, price: float, contracts: int = 500) -> float:
    if venue == "kalshi":
        fee = per_contract_fee(default_kalshi_fee(), price, contracts)
        return round(fee / price, 6) if price > 0 else 0.0
    if venue == "polymarket":
        return 0.0
    if venue == "opinion":
        return 0.005
    if venue == "predictit":
        return 0.015
    return 0.0


def _build_initial_markets() -> list[dict[str, Any]]:
    """Build deterministic live-scanner markets derived from DEFAULT_UNIVERSE."""
    seed_quotes = {
        "kalshi:FED-DEC26-CUT25": (0.45, 0.49, 420, 650, "2026-12-16"),
        "kalshi:CPI-Q426-U25": (0.39, 0.56, 380, 520, "2026-12-12"),
        "kalshi:BTC-120K-EOY26": (0.50, 0.44, 520, 780, "2026-12-31"),
        "kalshi:SPACEX-CATCH6": (0.59, 0.35, 340, 490, "2026-10-28"),
        "kalshi:SENATE-CTRL26": (0.53, 0.42, 460, 610, "2026-11-18"),
        "kalshi:TIKTOK-BAN26": (0.36, 0.58, 300, 400, "2026-12-14"),
    }
    markets: list[dict[str, Any]] = []
    for spec in DEFAULT_UNIVERSE:
        audit = verify_rule_compatibility(spec.pair, require_same=True)
        p_yes, p_no, k_sz, p_sz, res_date = seed_quotes[spec.pair.kalshi_id]
        k_fee_rate = _effective_fee_rate("kalshi", p_yes, 500)
        markets.append(
            {
                "id": spec.pair.kalshi_id,
                "title": spec.pair.name,
                "category": spec.category,
                "daysToResolution": spec.days_to_resolution,
                "resolutionDate": res_date,
                "rules": audit.verdict,
                "rulesAccepted": audit.accepted,
                "rulesReason": audit.reason,
                "flipped": spec.pair.flipped,
                "leg1": {
                    "venue": "kalshi",
                    "side": "yes",
                    "price": p_yes,
                    "feeRate": k_fee_rate,
                    "depth": k_sz,
                    "link": "https://kalshi.com",
                },
                "leg2": {
                    "venue": "polymarket",
                    "side": "no",
                    "price": p_no,
                    "feeRate": 0.0,
                    "depth": p_sz,
                    "link": "https://polymarket.com",
                },
            }
        )
    return markets


class EngineState:
    """In-memory server state wrapping the replay results and interactive paper sandbox."""

    def __init__(self) -> None:
        self.replay: dict[str, Any] = run_paper_replay()
        self.markets: list[dict[str, Any]] = _build_initial_markets()
        self.bot_running: bool = True
        self.min_entry_edge_pct: float = 2.0
        self.max_position_usd: float = 450.0
        self.take_profit_pct: float = 2.0
        self.rng = random.Random(101)
        self.account = PaperAccount(
            initial_capital=1000.0,
            max_position_pct=0.45,
            max_open_positions=4,
            min_entry_edge=0.02,
            take_profit=0.02,
        )
        # Seed interactive sandbox with 1 active simulated position & historical replay ledger
        self.open_sandbox_from_replay()

    def open_sandbox_from_replay(self) -> None:
        # Mirror replay summary into interactive bot view while keeping interactive actions safe
        self.replay_summary = self.replay["summary"]
        self.replay_trades = self.replay["trades"]
        self.interactive_positions: list[dict[str, Any]] = []
        self.interactive_trades: list[dict[str, Any]] = list(reversed(self.replay_trades))

    def step_simulation_tick(self) -> dict[str, Any]:
        """Perturb one tradeable market's simulated L2 book and recompute metrics."""
        tradeable_indices = [i for i, m in enumerate(self.markets) if m["rulesAccepted"]]
        idx = self.rng.choice(tradeable_indices)
        m = self.markets[idx]

        d1 = self.rng.choice((-0.01, 0.0, 0.01))
        d2 = self.rng.choice((-0.01, 0.0, 0.01))
        p1 = round(min(0.88, max(0.12, m["leg1"]["price"] + d1)), 2)
        p2 = round(min(0.88, max(0.12, m["leg2"]["price"] + d2)), 2)
        # Keep simulated spread realistic (88c - 101c)
        if p1 + p2 < 0.90:
            p2 = round(0.90 - p1, 2)
        if p1 + p2 > 0.99:
            p2 = round(0.98 - p1, 2)

        m["leg1"]["price"] = p1
        m["leg2"]["price"] = p2
        m["leg1"]["feeRate"] = _effective_fee_rate("kalshi", p1, 500)
        m["leg1"]["depth"] = max(150, int(m["leg1"]["depth"] + self.rng.randint(-40, 40)))
        m["leg2"]["depth"] = max(200, int(m["leg2"]["depth"] + self.rng.randint(-50, 50)))

        return {
            "type": "book_tick",
            "timestamp": datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
            "market": m,
        }

    def l2_books_for_market(self, market: dict[str, Any]) -> tuple[L2Book, L2Book]:
        p1 = market["leg1"]["price"]
        p2 = market["leg2"]["price"]
        poly_yes_mid = round(1.0 - p2, 2)
        k_book = L2Book.from_top(
            max(0.01, round(p1 - 0.01, 2)),
            p1,
            top_size=float(market["leg1"]["depth"]),
            levels=5,
            tick=0.01,
            depth_multiplier=1.35,
        )
        p_book = L2Book.from_top(
            round(1.0 - p2, 2),
            min(0.99, round(poly_yes_mid + 0.01, 2)),
            top_size=float(market["leg2"]["depth"]),
            levels=5,
            tick=0.01,
            depth_multiplier=1.45,
        )
        return k_book, p_book


ENGINE = EngineState()

app = FastAPI(
    title="ArbitrageX Quant Prediction-Market Terminal",
    version="2.0.0",
    description="Cross-venue prediction market arbitrage pricing, L2 slippage engine, and $1,000 simulated paper account replay.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def api_health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "ArbitrageX Quant Engine",
        "version": "2.0.0",
        "mode": "paper_simulation",
        "live_orders_enabled": False,
        "synpath_key_configured": bool(os.environ.get("SYNPATH_API_KEY")),
    }


@app.get("/api/markets")
def api_markets(include_rejected: bool = False) -> dict[str, Any]:
    markets = ENGINE.markets if include_rejected else [m for m in ENGINE.markets if m["rulesAccepted"]]
    return {
        "success": True,
        "source": "simulated_l2_engine",
        "markets": markets,
        "rejected_count": sum(1 for m in ENGINE.markets if not m["rulesAccepted"]),
    }


@app.get("/api/orderbook/{market_id}")
def api_orderbook(market_id: str) -> dict[str, Any]:
    market = next((m for m in ENGINE.markets if m["id"] == market_id), None)
    if market is None:
        raise HTTPException(status_code=404, detail=f"Unknown market_id: {market_id}")
    k_book, p_book = ENGINE.l2_books_for_market(market)
    k_sched = default_kalshi_fee()
    p_sched = default_polymarket_fee(0.0)

    curve = []
    for qty in (50, 100, 250, 500, 750, 1000, 1500):
        quotes = simulate_cross_venue_pair(k_book, p_book, qty, k_sched, p_sched)
        if quotes:
            q = quotes[0]
            curve.append(
                {
                    "requested_contracts": qty,
                    "filled_contracts": q.contracts,
                    "slippage_cents": round(q.slippage_per_pair * 100.0, 3),
                    "slippage_bps": round((q.slippage_per_pair / q.gross_cost_per_pair) * 10_000.0, 2),
                    "fees_cents": round(q.fees_per_pair * 100.0, 3),
                    "net_edge_cents": round(q.net_edge_per_pair * 100.0, 3),
                }
            )

    return {
        "market_id": market_id,
        "title": market["title"],
        "kalshi_yes_asks": [asdict(l) for l in k_book.asks],
        "kalshi_yes_bids": [asdict(l) for l in k_book.bids],
        "polymarket_no_asks": [asdict(l) for l in p_book.no_asks],
        "polymarket_no_bids": [asdict(l) for l in p_book.no_bids],
        "slippage_curve": curve,
    }


@app.get("/api/live-polymarket")
def api_live_polymarket() -> dict[str, Any]:
    try:
        req = urllib.request.Request(
            "https://gamma-api.polymarket.com/events?limit=8&active=true&closed=false",
            headers={"User-Agent": "ArbitrageX-QuantTerminal/2.0"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        live_markets: list[dict[str, Any]] = []
        for idx, ev in enumerate(raw):
            markets = ev.get("markets", [])
            if not markets:
                continue
            target_m = None
            p_yes, p_no = 0.0, 0.0
            for m in markets:
                prices = m.get("outcomePrices")
                if isinstance(prices, str):
                    try:
                        prices = json.loads(prices)
                    except Exception:
                        continue
                if prices and len(prices) >= 2:
                    try:
                        py = float(prices[0])
                        pn = float(prices[1])
                        if 0.05 < py < 0.95:
                            target_m = m
                            p_yes, p_no = py, pn
                            break
                    except (ValueError, TypeError):
                        continue
            if not target_m:
                continue

            suffix = f" ({target_m.get('groupItemTitle', '')})" if target_m.get("groupItemTitle") else ""
            full_title = f"{ev.get('title', 'Market')}{suffix}"
            # Reference synthetic Kalshi quote delta only for UI comparison when Kalshi key is absent
            spread_delta = round((idx % 3 + 1) * 0.02, 2)
            k_yes = max(0.05, round(p_yes - spread_delta, 2))
            vol = float(target_m.get("volume", 5000) or 5000)
            depth_cap = max(150, min(50000, int(vol * 0.02)))
            live_markets.append(
                {
                    "id": f"poly-{ev.get('id', idx)}",
                    "title": full_title,
                    "category": (ev.get("category") or "politics").lower(),
                    "daysToResolution": 30 + (idx * 5),
                    "resolutionDate": (ev.get("endDate") or "2026-12-31")[:10],
                    "rules": "same",
                    "rulesAccepted": True,
                    "rulesReason": "Live Polymarket Gamma price paired with simulated Kalshi reference leg.",
                    "leg1": {
                        "venue": "kalshi",
                        "side": "yes",
                        "price": k_yes,
                        "feeRate": _effective_fee_rate("kalshi", k_yes, 500),
                        "depth": depth_cap,
                        "link": "https://kalshi.com",
                    },
                    "leg2": {
                        "venue": "polymarket",
                        "side": "no",
                        "price": round(p_no, 2),
                        "feeRate": 0.0,
                        "depth": max(300, int(depth_cap * 1.4)),
                        "link": f"https://polymarket.com/event/{ev.get('slug', '')}",
                    },
                }
            )
        if live_markets:
            return {"success": True, "source": "polymarket_gamma_live", "markets": live_markets}
    except Exception as exc:
        return {
            "success": False,
            "source": "simulation_fallback",
            "error": str(exc),
            "markets": [m for m in ENGINE.markets if m["rulesAccepted"]],
        }
    return {
        "success": False,
        "source": "simulation_fallback",
        "markets": [m for m in ENGINE.markets if m["rulesAccepted"]],
    }


@app.get("/api/paper-account")
def api_paper_account() -> dict[str, Any]:
    """Return full deterministic $1,000 Simulated Paper Account Replay & interactive state."""
    eq = ENGINE.replay["equity_curve"]
    stride = max(1, len(eq) // 180)
    sampled_eq = eq[::stride]
    if eq and sampled_eq[-1] != eq[-1]:
        sampled_eq.append(eq[-1])
    return {
        "simulation_only": True,
        "meta": ENGINE.replay["meta"],
        "summary": ENGINE.replay_summary,
        "trades": ENGINE.interactive_trades,
        "open_positions": ENGINE.interactive_positions,
        "equity_curve": sampled_eq,
        "slippage_curve": ENGINE.replay["slippage_curve"],
    }


@app.get("/api/bot/status")
def api_bot_status() -> dict[str, Any]:
    s = ENGINE.replay_summary
    return {
        "simulation_only": True,
        "bot_running": ENGINE.bot_running,
        "execution_mode": "paper_simulation",
        "initial_capital": s["initial_capital"],
        "bankroll": s["final_equity"],
        "realized_pnl": s["realized_pnl"],
        "roi_pct": s["roi_pct"],
        "win_rate": f"{s['win_rate_pct']:.1f}%",
        "win_rate_pct": s["win_rate_pct"],
        "total_trades": len(ENGINE.interactive_trades),
        "open_positions": len(ENGINE.interactive_positions),
        "sharpe_ratio_annualized": s["sharpe_ratio_annualized"],
        "max_drawdown_pct": s["max_drawdown_pct"],
        "max_drawdown_usd": s["max_drawdown_usd"],
        "total_fees_paid": s["total_fees_paid"],
        "total_slippage_cost": s["total_slippage_cost"],
        "config": {
            "min_entry_edge_pct": ENGINE.min_entry_edge_pct,
            "max_position_usd": ENGINE.max_position_usd,
            "take_profit_pct": ENGINE.take_profit_pct,
        },
    }


@app.post("/api/bot/toggle")
def api_bot_toggle() -> dict[str, Any]:
    ENGINE.bot_running = not ENGINE.bot_running
    return {"bot_running": ENGINE.bot_running}


@app.post("/api/bot/config")
def api_bot_config(cfg: BotConfigRequest) -> dict[str, Any]:
    ENGINE.min_entry_edge_pct = cfg.min_entry_edge_pct
    ENGINE.max_position_usd = cfg.max_position_usd
    ENGINE.take_profit_pct = cfg.take_profit_pct
    return {
        "success": True,
        "config": {
            "min_entry_edge_pct": ENGINE.min_entry_edge_pct,
            "max_position_usd": ENGINE.max_position_usd,
            "take_profit_pct": ENGINE.take_profit_pct,
        },
    }


@app.post("/api/bot/execute")
def api_bot_execute(req: ExecuteTradeRequest) -> dict[str, Any]:
    market = next((m for m in ENGINE.markets if m["id"] == req.market_id), None)
    if market is None:
        raise HTTPException(status_code=404, detail=f"Market not found: {req.market_id}")
    if not market["rulesAccepted"]:
        raise HTTPException(status_code=400, detail=f"Rule verification failed: {market['rulesReason']}")

    cap = min(req.capital or ENGINE.max_position_usd, ENGINE.max_position_usd)
    p1 = market["leg1"]["price"]
    p2 = market["leg2"]["price"]
    k_sched = default_kalshi_fee()
    # Estimate contract count iteratively using exact Kalshi cent-rounded quadratic fee
    est_unit = p1 + p2 + per_contract_fee(k_sched, p1, 300)
    contracts = min(int(cap // est_unit), int(market["leg1"]["depth"]), int(market["leg2"]["depth"]))
    if contracts < 1:
        raise HTTPException(status_code=400, detail="Insufficient capital or book depth for 1 contract")

    k_book, p_book = ENGINE.l2_books_for_market(market)
    quotes = simulate_cross_venue_pair(k_book, p_book, contracts, k_sched, default_polymarket_fee(0.0))
    if not quotes:
        raise HTTPException(status_code=400, detail="No valid cross-venue quote available")
    q = quotes[0]
    deployed = round(q.contracts * q.gross_cost_per_pair + q.yes_fill.total_fee + q.no_fill.total_fee, 2)
    locked_profit = round(q.contracts * 1.0 - deployed, 2)
    roi_pct = round((locked_profit / deployed) * 100.0, 2) if deployed > 0 else 0.0

    pos = {
        "id": f"POS-{len(ENGINE.interactive_positions) + 201}",
        "market_id": market["id"],
        "title": market["title"],
        "direction": f"{q.yes_venue.upper()} YES + {q.no_venue.upper()} NO",
        "entryTime": datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
        "capital": deployed,
        "contracts": int(q.contracts),
        "fees": round(q.yes_fill.total_fee + q.no_fill.total_fee, 2),
        "slippage": round(q.slippage_per_pair * q.contracts, 2),
        "lockedProfit": locked_profit,
        "roi": roi_pct,
        "status": "SIMULATED PAPER POSITION",
    }
    ENGINE.interactive_positions.insert(0, pos)
    return {"success": True, "position": pos}


@app.post("/api/bot/close")
def api_bot_close(req: ClosePositionRequest) -> dict[str, Any]:
    idx = next(
        (i for i, p in enumerate(ENGINE.interactive_positions) if p["id"] == req.market_id or p["market_id"] == req.market_id),
        None,
    )
    if idx is None:
        raise HTTPException(status_code=404, detail=f"Open position not found: {req.market_id}")
    pos = ENGINE.interactive_positions.pop(idx)
    trade = {
        "trade_id": f"SIM-{len(ENGINE.interactive_trades) + 1:04d}",
        "kalshi_id": pos["market_id"],
        "market_name": pos["title"],
        "direction": pos["direction"],
        "contracts": pos["contracts"],
        "deployed_capital": pos["capital"],
        "gross_payout": float(pos["contracts"]),
        "total_fees_usd": pos["fees"],
        "slippage_usd": pos["slippage"],
        "pnl_usd": pos["lockedProfit"],
        "roi_pct": pos["roi"],
        "exit_reason": req.mode,
    }
    ENGINE.interactive_trades.insert(0, trade)
    return {"success": True, "trade": trade}


@app.post("/api/calculate")
def api_calculate(req: CalculateRequest) -> dict[str, Any]:
    """Compute exact cross-venue sizing with Kalshi quadratic cent-rounding & optional L2 slippage."""
    p1, p2 = req.leg1_price, req.leg2_price
    k_sched = default_kalshi_fee()
    p_sched = default_polymarket_fee(0.0)

    def _leg_fee(venue: str, price: float, qty: int, override_rate: float | None) -> float:
        if override_rate is not None:
            return round(price * override_rate * qty, 4)
        if venue == "kalshi":
            return round(k_sched.estimate(price, qty) or 0.0, 2)
        if venue == "polymarket":
            return round(p_sched.estimate(price, qty) or 0.0, 4)
        return round(price * _effective_fee_rate(venue, price, qty) * qty, 4)

    # First pass contract estimate
    approx_f1 = _leg_fee(req.leg1_venue, p1, 100, req.leg1_fee_rate) / 100.0
    approx_f2 = _leg_fee(req.leg2_venue, p2, 100, req.leg2_fee_rate) / 100.0
    unit_est = p1 + p2 + approx_f1 + approx_f2
    contracts = int(req.capital // unit_est) if unit_est > 0 else 0

    slippage_total = 0.0
    if req.market_id:
        market = next((m for m in ENGINE.markets if m["id"] == req.market_id), None)
        if market and contracts >= 1:
            k_book, p_book = ENGINE.l2_books_for_market(market)
            quotes = simulate_cross_venue_pair(k_book, p_book, contracts, k_sched, p_sched)
            if quotes:
                q = quotes[0]
                contracts = int(q.contracts)
                slippage_total = round(q.slippage_per_pair * contracts, 2)

    fee1 = _leg_fee(req.leg1_venue, p1, max(1, contracts), req.leg1_fee_rate) if contracts > 0 else 0.0
    fee2 = _leg_fee(req.leg2_venue, p2, max(1, contracts), req.leg2_fee_rate) if contracts > 0 else 0.0
    leg1_notional = round(contracts * p1, 2)
    leg2_notional = round(contracts * p2, 2)
    total_deployed = round(leg1_notional + leg2_notional + fee1 + fee2 + slippage_total, 2)
    gross_payout = round(contracts * 1.0, 2)
    net_profit = round(gross_payout - total_deployed, 2)
    roi_pct = round((net_profit / total_deployed) * 100.0, 2) if total_deployed > 0 else 0.0
    apy_pct = (
        round((math.pow(1.0 + roi_pct / 100.0, 365.0 / req.days_to_resolution) - 1.0) * 100.0, 1)
        if roi_pct > 0
        else 0.0
    )

    return {
        "contracts": contracts,
        "leg1_notional": leg1_notional,
        "leg2_notional": leg2_notional,
        "fee1_usd": round(fee1, 2),
        "fee2_usd": round(fee2, 2),
        "fees_total": round(fee1 + fee2, 2),
        "slippage_total": slippage_total,
        "total_deployed": total_deployed,
        "gross_payout": gross_payout,
        "net_profit": net_profit,
        "roi_percent": roi_pct,
        "apy_percent": apy_pct,
    }


@app.websocket("/ws/stream")
async def websocket_stream(ws: WebSocket) -> None:
    """Stream simulated cross-venue order-book ticks when no live keys are configured."""
    await ws.accept()
    try:
        await ws.send_json(
            {
                "type": "snapshot",
                "mode": "simulated_l2_replay",
                "markets": [m for m in ENGINE.markets if m["rulesAccepted"]],
                "summary": ENGINE.replay_summary,
            }
        )
        while True:
            await asyncio.sleep(2.5)
            tick = ENGINE.step_simulation_tick()
            await ws.send_json(tick)
    except WebSocketDisconnect:
        return


@app.get("/favicon.ico", include_in_schema=False)
def serve_favicon() -> Response:
    return Response(status_code=204)


@app.get("/")
def serve_index() -> FileResponse:
    return FileResponse(ROOT_DIR / "index.html")


@app.get("/{file_path:path}")
def serve_static(file_path: str) -> FileResponse:
    safe_target = (ROOT_DIR / file_path).resolve()
    if not str(safe_target).startswith(str(ROOT_DIR)) or not safe_target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    # Never serve hidden files like .env or .git
    if any(part.startswith(".") for part in safe_target.relative_to(ROOT_DIR).parts):
        raise HTTPException(status_code=403, detail="Forbidden")
    return FileResponse(safe_target)


def run_server(port: int = PORT) -> None:
    print("\n=======================================================")
    print("  ArbitrageX Prediction Market Quant Terminal Online")
    print(f"  URL: http://localhost:{port}")
    print("  Mode: Simulated L2 Order-Book & $1,000 Paper Replay")
    print("=======================================================\n")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    p = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    run_server(p)
