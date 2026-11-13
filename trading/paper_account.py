"""Simulated paper account ledger, position sizing, and risk accounting.

Tracks cash, capital locked in hedged YES+NO positions, realized P&L,
mark-to-market equity curve, drawdown, and daily annualized Sharpe ratio.
All balances and returns represent simulated paper trading only.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from src.arbitrage import Book, Pricer


@dataclass
class AccountPosition:
    """An open hedged YES+NO paper position."""
    kalshi_id: str
    poly_id: str
    market_name: str
    direction: str
    yes_venue: str
    yes_price: float
    no_venue: str
    no_price: float
    contracts: int
    entry_fees_usd: float
    slippage_usd: float
    deployed_capital: float
    locked_edge_per_pair: float
    locked_profit_usd: float
    opened_ms: int

    @property
    def unit_cost(self) -> float:
        return self.yes_price + self.no_price


@dataclass
class ClosedTrade:
    """A closed or settled hedged arbitrage trade."""
    trade_id: str
    kalshi_id: str
    market_name: str
    direction: str
    contracts: int
    yes_venue: str
    yes_entry: float
    no_venue: str
    no_entry: float
    yes_exit: float
    no_exit: float
    deployed_capital: float
    gross_payout: float
    entry_fees_usd: float
    exit_fees_usd: float
    total_fees_usd: float
    slippage_usd: float
    pnl_usd: float
    roi_pct: float
    locked_edge_per_pair: float
    opened_ms: int
    closed_ms: int
    exit_reason: str


@dataclass
class EquityPoint:
    """Single point on the simulated paper account equity curve."""
    timestamp_ms: int
    cash: float
    deployed_capital: float
    mark_equity: float
    hedged_equity: float
    drawdown_pct: float


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


class PaperAccount:
    """Deterministic $1,000 (configurable) simulated paper trading account."""

    def __init__(
        self,
        initial_capital: float = 1000.0,
        *,
        max_position_pct: float = 0.45,
        max_open_positions: int = 4,
        min_entry_edge: float = 0.02,
        take_profit: float = 0.02,
    ):
        if initial_capital <= 0:
            raise ValueError("initial_capital must be positive")
        self.initial_capital = float(initial_capital)
        self.cash = float(initial_capital)
        self.max_position_pct = float(max_position_pct)
        self.max_open_positions = int(max_open_positions)
        self.min_entry_edge = float(min_entry_edge)
        self.take_profit = float(take_profit)

        self.positions: dict[str, AccountPosition] = {}
        self.closed_trades: list[ClosedTrade] = []
        self.equity_curve: list[EquityPoint] = []
        self._peak_mark_equity: float = self.initial_capital
        self._trade_seq: int = 0

    def deployed_capital(self) -> float:
        return round(sum(p.deployed_capital for p in self.positions.values()), 6)

    def hedged_equity(self) -> float:
        """Cash plus cost basis of open hedged pairs (each pair settles to $1.00)."""
        return round(self.cash + self.deployed_capital(), 6)

    def locked_resolution_equity(self) -> float:
        """Cash plus guaranteed $1.00/contract payout at settlement."""
        return round(self.cash + sum(p.contracts * 1.0 for p in self.positions.values()), 6)

    def max_allocation_usd(self) -> float:
        """Maximum USD allowed for a single new market position."""
        cap = self.hedged_equity() * self.max_position_pct
        return max(0.0, min(self.cash, cap))

    def size_contracts(
        self,
        unit_cost_with_fee: float,
        book_available_contracts: float,
    ) -> int:
        """Compute integer contracts bounded by cash, risk limit, and L2 depth."""
        if unit_cost_with_fee <= 0 or book_available_contracts < 1:
            return 0
        budget = self.max_allocation_usd()
        affordable = int(budget // unit_cost_with_fee)
        return max(0, min(affordable, int(book_available_contracts)))

    def can_open(self, kalshi_id: str, required_capital: float) -> tuple[bool, str]:
        if kalshi_id in self.positions:
            return False, "Position already open for market"
        if len(self.positions) >= self.max_open_positions:
            return False, f"Max open positions ({self.max_open_positions}) reached"
        if required_capital <= 0:
            return False, "Required capital must be positive"
        if required_capital > self.cash + 1e-6:
            return False, "Insufficient available cash"
        if required_capital > self.hedged_equity() * self.max_position_pct + 1e-2:
            return False, "Exceeds single-market risk allocation cap"
        return True, "ok"

    def open_position(
        self,
        *,
        kalshi_id: str,
        poly_id: str,
        market_name: str,
        direction: str,
        yes_venue: str,
        yes_price: float,
        no_venue: str,
        no_price: float,
        contracts: int,
        entry_fees_usd: float,
        slippage_usd: float,
        opened_ms: int,
    ) -> AccountPosition:
        deployed = round(contracts * (yes_price + no_price) + entry_fees_usd, 6)
        allowed, reason = self.can_open(kalshi_id, deployed)
        if not allowed:
            raise ValueError(f"Cannot open {kalshi_id}: {reason}")
        if contracts < 1:
            raise ValueError("contracts must be >= 1")

        locked_profit = round(contracts * 1.0 - deployed, 6)
        locked_edge = round(locked_profit / contracts, 6)
        if locked_edge + 1e-9 < self.min_entry_edge:
            raise ValueError(
                f"Net edge {locked_edge:.4f} below min_entry_edge {self.min_entry_edge:.4f}"
            )

        self.cash = round(self.cash - deployed, 6)
        pos = AccountPosition(
            kalshi_id=kalshi_id,
            poly_id=poly_id,
            market_name=market_name,
            direction=direction,
            yes_venue=yes_venue,
            yes_price=round(yes_price, 6),
            no_venue=no_venue,
            no_price=round(no_price, 6),
            contracts=int(contracts),
            entry_fees_usd=round(entry_fees_usd, 6),
            slippage_usd=round(slippage_usd, 6),
            deployed_capital=deployed,
            locked_edge_per_pair=locked_edge,
            locked_profit_usd=locked_profit,
            opened_ms=opened_ms,
        )
        self.positions[kalshi_id] = pos
        return pos

    def close_position(
        self,
        kalshi_id: str,
        *,
        yes_exit_price: float,
        no_exit_price: float,
        exit_fees_usd: float,
        closed_ms: int,
        exit_reason: str = "early_convergence",
    ) -> ClosedTrade:
        pos = self.positions.pop(kalshi_id)
        gross_payout = round(pos.contracts * (yes_exit_price + no_exit_price), 6)
        net_proceeds = round(gross_payout - exit_fees_usd, 6)
        pnl_usd = round(net_proceeds - pos.deployed_capital, 6)
        roi_pct = round((pnl_usd / pos.deployed_capital) * 100.0, 4) if pos.deployed_capital > 0 else 0.0

        self.cash = round(self.cash + net_proceeds, 6)
        self._trade_seq += 1
        trade = ClosedTrade(
            trade_id=f"SIM-{self._trade_seq:04d}",
            kalshi_id=pos.kalshi_id,
            market_name=pos.market_name,
            direction=pos.direction,
            contracts=pos.contracts,
            yes_venue=pos.yes_venue,
            yes_entry=pos.yes_price,
            no_venue=pos.no_venue,
            no_entry=pos.no_price,
            yes_exit=round(yes_exit_price, 6),
            no_exit=round(no_exit_price, 6),
            deployed_capital=pos.deployed_capital,
            gross_payout=gross_payout,
            entry_fees_usd=pos.entry_fees_usd,
            exit_fees_usd=round(exit_fees_usd, 6),
            total_fees_usd=round(pos.entry_fees_usd + exit_fees_usd, 6),
            slippage_usd=pos.slippage_usd,
            pnl_usd=pnl_usd,
            roi_pct=roi_pct,
            locked_edge_per_pair=pos.locked_edge_per_pair,
            opened_ms=pos.opened_ms,
            closed_ms=closed_ms,
            exit_reason=exit_reason,
        )
        self.closed_trades.append(trade)
        return trade

    def settle_position(
        self,
        kalshi_id: str,
        *,
        winning_side: str = "yes",
        closed_ms: int,
    ) -> ClosedTrade:
        """Settle a hedged pair at $1.00 per contract on resolution."""
        yes_out = 1.0 if winning_side == "yes" else 0.0
        no_out = 1.0 if winning_side == "no" else 0.0
        return self.close_position(
            kalshi_id,
            yes_exit_price=yes_out,
            no_exit_price=no_out,
            exit_fees_usd=0.0,
            closed_ms=closed_ms,
            exit_reason=f"settled_{winning_side}",
        )

    def mark_equity(
        self,
        timestamp_ms: int,
        mark_books: dict[str, tuple[Book, Book, Pricer]] | None = None,
    ) -> EquityPoint:
        """Snapshot account equity at `timestamp_ms`.

        `mark_equity` uses immediate exit-bid liquidation value when books are supplied
        (reflecting bid-ask spread mark-to-market fluctuations), while `hedged_equity`
        tracks cash + deployed cost basis.
        """
        deployed = self.deployed_capital()
        mark_positions_val = 0.0
        for kid, pos in self.positions.items():
            if mark_books and kid in mark_books:
                k_book, p_book, pricer = mark_books[kid]
                books = {"kalshi": k_book, "polymarket": p_book}
                y_bid = books[pos.yes_venue].bid
                n_bid = books[pos.no_venue].no_bid
                if y_bid is not None and n_bid is not None:
                    exit_fee = (
                        pricer.leg(pos.yes_venue, "yes", y_bid).fee
                        + pricer.leg(pos.no_venue, "no", n_bid).fee
                    ) * pos.contracts
                    mark_positions_val += pos.contracts * (y_bid + n_bid) - exit_fee
                    continue
            mark_positions_val += pos.deployed_capital

        mark_eq = round(self.cash + mark_positions_val, 4)
        hedged_eq = round(self.cash + deployed, 4)
        self._peak_mark_equity = max(self._peak_mark_equity, mark_eq)
        dd_pct = (
            ((self._peak_mark_equity - mark_eq) / self._peak_mark_equity) * 100.0
            if self._peak_mark_equity > 0
            else 0.0
        )
        pt = EquityPoint(
            timestamp_ms=timestamp_ms,
            cash=round(self.cash, 4),
            deployed_capital=round(deployed, 4),
            mark_equity=mark_eq,
            hedged_equity=hedged_eq,
            drawdown_pct=round(max(0.0, dd_pct), 4),
        )
        self.equity_curve.append(pt)
        return pt

    def daily_sharpe(self) -> float:
        """Compute annualized Sharpe ratio from daily UTC mark-equity returns."""
        if len(self.equity_curve) < 2:
            return 0.0
        by_day: dict[str, float] = {}
        for pt in self.equity_curve:
            day = datetime.fromtimestamp(pt.timestamp_ms / 1000, timezone.utc).strftime("%Y-%m-%d")
            by_day[day] = pt.mark_equity
        vals = list(by_day.values())
        if len(vals) < 2:
            return 0.0
        rets = [(vals[i] / vals[i - 1]) - 1.0 for i in range(1, len(vals)) if vals[i - 1] > 0]
        if len(rets) < 2:
            return 0.0
        mean_r = sum(rets) / len(rets)
        var_r = sum((r - mean_r) ** 2 for r in rets) / (len(rets) - 1)
        std_r = math.sqrt(var_r)
        if std_r <= 1e-12:
            return 0.0
        return round((mean_r / std_r) * math.sqrt(365.0), 3)

    def summary(self) -> dict[str, Any]:
        """Return exact observed paper account summary metrics."""
        final_eq = self.equity_curve[-1].hedged_equity if self.equity_curve else self.hedged_equity()
        final_mark_eq = self.equity_curve[-1].mark_equity if self.equity_curve else final_eq
        realized_pnl = round(sum(t.pnl_usd for t in self.closed_trades), 2)
        total_fees = round(sum(t.total_fees_usd for t in self.closed_trades), 2)
        total_slippage = round(sum(t.slippage_usd for t in self.closed_trades), 2)
        wins = sum(1 for t in self.closed_trades if t.pnl_usd > 0)
        losses = sum(1 for t in self.closed_trades if t.pnl_usd <= 0)
        n_trades = len(self.closed_trades)
        win_rate = round((wins / n_trades) * 100.0, 2) if n_trades else 0.0
        roi_pct = round(((final_eq - self.initial_capital) / self.initial_capital) * 100.0, 2)
        max_dd_pct = round(max((pt.drawdown_pct for pt in self.equity_curve), default=0.0), 2)
        max_dd_usd = round(
            max(
                (
                    max(0.0, max(p.mark_equity for p in self.equity_curve[: i + 1]) - pt.mark_equity)
                    for i, pt in enumerate(self.equity_curve)
                ),
                default=0.0,
            ),
            2,
        )
        avg_edge = (
            round(sum(t.locked_edge_per_pair for t in self.closed_trades) / n_trades, 4)
            if n_trades
            else 0.0
        )
        early_exits = sum(1 for t in self.closed_trades if t.exit_reason == "early_convergence")
        settled = sum(1 for t in self.closed_trades if t.exit_reason.startswith("settled_"))

        return {
            "simulation_only": True,
            "label": "Simulated $1,000 Paper Account Replay",
            "initial_capital": round(self.initial_capital, 2),
            "cash": round(self.cash, 2),
            "deployed_capital": round(self.deployed_capital(), 2),
            "final_equity": round(final_eq, 2),
            "final_mark_equity": round(final_mark_eq, 2),
            "realized_pnl": realized_pnl,
            "roi_pct": roi_pct,
            "total_trades": n_trades,
            "winning_trades": wins,
            "losing_trades": losses,
            "win_rate_pct": win_rate,
            "early_exits": early_exits,
            "settled_at_resolution": settled,
            "open_positions_count": len(self.positions),
            "total_fees_paid": total_fees,
            "total_slippage_cost": total_slippage,
            "avg_locked_edge_per_pair": avg_edge,
            "max_drawdown_pct": max_dd_pct,
            "max_drawdown_usd": max_dd_usd,
            "sharpe_ratio_annualized": self.daily_sharpe(),
        }

    def serialize_trades(self) -> list[dict[str, Any]]:
        out = []
        for t in self.closed_trades:
            row = asdict(t)
            row["opened_at"] = _iso(t.opened_ms)
            row["closed_at"] = _iso(t.closed_ms)
            out.append(row)
        return out
