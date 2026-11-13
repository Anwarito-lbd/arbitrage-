"""Multi-level L2 order book modeling, VWAP fill simulation, and slippage math.

Extends top-of-book pricing (`src/arbitrage.py`) to full depth ladders so the
paper account, calculator, and backtest replay account for partial fills,
market impact, and exact venue taker fees across order sizes.
"""
from __future__ import annotations

from dataclasses import dataclass

from synpath import FeeSchedule

from .arbitrage import Book, MAX_DISAGREEMENT, per_contract_fee


@dataclass(frozen=True)
class DepthLevel:
    """Single price level on a binary prediction market book (YES price in [0.01, 0.99])."""
    price: float
    size: float


@dataclass(frozen=True)
class FillSimulation:
    """Result of walking an L2 book to fill `requested_contracts`."""
    requested_contracts: float
    filled_contracts: float
    top_price: float
    vwap_price: float
    slippage_per_contract: float
    slippage_bps: float
    total_notional: float
    total_fee: float
    total_cost: float
    levels_consumed: int
    partial_fill: bool


@dataclass(frozen=True)
class PairExecutionQuote:
    """Cross-venue execution quote accounting for L2 depth, slippage, and fees."""
    direction: str
    contracts: float
    yes_venue: str
    no_venue: str
    yes_fill: FillSimulation
    no_fill: FillSimulation
    gross_cost_per_pair: float
    fees_per_pair: float
    slippage_per_pair: float
    net_cost_per_pair: float
    net_edge_per_pair: float
    net_edge_pct: float
    max_fillable_contracts: float


def default_kalshi_fee() -> FeeSchedule:
    """Standard Kalshi quadratic taker schedule (0.07 * C * P * (1-P), rounded up to cent)."""
    return FeeSchedule(
        venue="kalshi",
        scope="series",
        scope_id="DEFAULT",
        fee_type="quadratic",
        multiplier=1.0,
        rounding="up_to_cent",
    )


def default_polymarket_fee(taker_rate: float = 0.0) -> FeeSchedule:
    """Polymarket fee schedule (0% on standard CTF markets, configurable quadratic_theta)."""
    return FeeSchedule(
        venue="polymarket",
        scope="market",
        scope_id="DEFAULT",
        fee_type="quadratic_theta",
        taker_rate=taker_rate,
        maker_rate=0.0,
        exponent=1.0,
    )


class L2Book:
    """Two-sided L2 order book quoted in YES prices."""

    def __init__(self, bids: list[DepthLevel], asks: list[DepthLevel]):
        self.bids: list[DepthLevel] = sorted(
            [lvl for lvl in bids if lvl.size > 0 and 0.0 < lvl.price < 1.0],
            key=lambda x: -x.price,
        )
        self.asks: list[DepthLevel] = sorted(
            [lvl for lvl in asks if lvl.size > 0 and 0.0 < lvl.price < 1.0],
            key=lambda x: x.price,
        )

    @property
    def best_bid(self) -> DepthLevel | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> DepthLevel | None:
        return self.asks[0] if self.asks else None

    @property
    def total_bid_size(self) -> float:
        return sum(lvl.size for lvl in self.bids)

    @property
    def total_ask_size(self) -> float:
        return sum(lvl.size for lvl in self.asks)

    @property
    def no_asks(self) -> list[DepthLevel]:
        """Buying NO hits YES bids: NO ask = 1 - YES bid, sorted cheapest NO price first."""
        return sorted(
            [DepthLevel(round(1.0 - b.price, 4), b.size) for b in self.bids],
            key=lambda x: x.price,
        )

    @property
    def no_bids(self) -> list[DepthLevel]:
        """Selling NO hits YES asks: NO bid = 1 - YES ask, sorted highest NO price first."""
        return sorted(
            [DepthLevel(round(1.0 - a.price, 4), a.size) for a in self.asks],
            key=lambda x: -x.price,
        )

    def top_book(self) -> Book:
        bb = self.best_bid
        ba = self.best_ask
        return Book(
            bid=bb.price if bb else None,
            ask=ba.price if ba else None,
            bid_size=bb.size if bb else None,
            ask_size=ba.size if ba else None,
        )

    def flipped(self) -> "L2Book":
        """Flip YES <-> NO for venues framing the proposition inversely."""
        new_bids = [DepthLevel(round(1.0 - a.price, 4), a.size) for a in self.asks]
        new_asks = [DepthLevel(round(1.0 - b.price, 4), b.size) for b in self.bids]
        return L2Book(new_bids, new_asks)

    @classmethod
    def from_top(
        cls,
        bid: float,
        ask: float,
        *,
        top_size: float = 500.0,
        levels: int = 5,
        tick: float = 0.01,
        depth_multiplier: float = 1.4,
    ) -> "L2Book":
        """Construct a deterministic synthetic L2 ladder around top-of-book prices."""
        bids: list[DepthLevel] = []
        asks: list[DepthLevel] = []
        for i in range(levels):
            sz = round(top_size * (depth_multiplier ** i), 2)
            b_px = round(bid - i * tick, 4)
            a_px = round(ask + i * tick, 4)
            if 0.01 <= b_px <= 0.99:
                bids.append(DepthLevel(b_px, sz))
            if 0.01 <= a_px <= 0.99:
                asks.append(DepthLevel(a_px, sz))
        return cls(bids, asks)

    def simulate_buy(
        self,
        contracts: float,
        *,
        side: str = "yes",
        fee_schedule: FeeSchedule | None = None,
    ) -> FillSimulation | None:
        """Walk the ask ladder for `side` ('yes' or 'no') to simulate buying `contracts`."""
        levels = self.asks if side == "yes" else self.no_asks
        if not levels or contracts <= 0:
            return None

        remaining = float(contracts)
        filled = 0.0
        notional = 0.0
        consumed = 0
        top_price = levels[0].price

        for lvl in levels:
            if remaining <= 0:
                break
            take = min(remaining, lvl.size)
            notional += take * lvl.price
            filled += take
            remaining -= take
            consumed += 1

        if filled <= 0:
            return None

        vwap = notional / filled
        slippage = max(0.0, vwap - top_price)
        slippage_bps = (slippage / top_price * 10_000.0) if top_price > 0 else 0.0
        if fee_schedule is not None:
            per_fee = per_contract_fee(fee_schedule, vwap, filled)
            total_fee = round(per_fee * filled, 6)
        else:
            total_fee = 0.0

        return FillSimulation(
            requested_contracts=contracts,
            filled_contracts=round(filled, 4),
            top_price=round(top_price, 4),
            vwap_price=round(vwap, 6),
            slippage_per_contract=round(slippage, 6),
            slippage_bps=round(slippage_bps, 2),
            total_notional=round(notional, 6),
            total_fee=round(total_fee, 6),
            total_cost=round(notional + total_fee, 6),
            levels_consumed=consumed,
            partial_fill=filled < contracts,
        )


def simulate_cross_venue_pair(
    kalshi: L2Book,
    poly: L2Book,
    contracts: float,
    kalshi_fee: FeeSchedule | None = None,
    poly_fee: FeeSchedule | None = None,
) -> list[PairExecutionQuote]:
    """Evaluate both cross-venue YES/NO combinations with L2 VWAP slippage and venue fees."""
    k_top = kalshi.top_book()
    p_top = poly.top_book()
    if k_top.mid is not None and p_top.mid is not None:
        if abs(k_top.mid - p_top.mid) > MAX_DISAGREEMENT:
            return []

    k_sched = kalshi_fee or default_kalshi_fee()
    p_sched = poly_fee or default_polymarket_fee()

    candidates = [
        ("kalshi_yes", "kalshi", kalshi, k_sched, "polymarket", poly, p_sched),
        ("poly_yes", "polymarket", poly, p_sched, "kalshi", kalshi, k_sched),
    ]

    quotes: list[PairExecutionQuote] = []
    for direction, y_venue, y_book, y_sched, n_venue, n_book, n_sched in candidates:
        max_fillable = min(y_book.total_ask_size, n_book.total_bid_size)
        if max_fillable <= 0:
            continue
        exec_qty = min(contracts, max_fillable)
        y_fill = y_book.simulate_buy(exec_qty, side="yes", fee_schedule=y_sched)
        n_fill = n_book.simulate_buy(exec_qty, side="no", fee_schedule=n_sched)
        if y_fill is None or n_fill is None:
            continue

        gross_cost_pair = y_fill.vwap_price + n_fill.vwap_price
        fees_pair = (y_fill.total_fee + n_fill.total_fee) / exec_qty
        slippage_pair = y_fill.slippage_per_contract + n_fill.slippage_per_contract
        net_cost_pair = gross_cost_pair + fees_pair
        net_edge_pair = 1.0 - net_cost_pair
        net_edge_pct = (net_edge_pair / net_cost_pair * 100.0) if net_cost_pair > 0 else 0.0

        quotes.append(
            PairExecutionQuote(
                direction=direction,
                contracts=exec_qty,
                yes_venue=y_venue,
                no_venue=n_venue,
                yes_fill=y_fill,
                no_fill=n_fill,
                gross_cost_per_pair=round(gross_cost_pair, 6),
                fees_per_pair=round(fees_pair, 6),
                slippage_per_pair=round(slippage_pair, 6),
                net_cost_per_pair=round(net_cost_pair, 6),
                net_edge_per_pair=round(net_edge_pair, 6),
                net_edge_pct=round(net_edge_pct, 4),
                max_fillable_contracts=round(max_fillable, 2),
            )
        )

    return sorted(quotes, key=lambda q: -q.net_edge_per_pair)
