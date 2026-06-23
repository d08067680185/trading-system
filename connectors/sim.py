"""
SimulatedConnector — a BaseConnector that fills orders against replayed OHLCV
bars instead of a live exchange.

The whole point of this module is *backtest/live parity*: the backtest runner
wires this connector into a real `TradingEngine` with the real strategy and the
real `RiskManager`, so a strategy runs the identical
`_process_event → on_ticker → emit_signal → _execute_signal → place_order` code
path in simulation that it runs in production. The legacy `backtest/engine.py`
re-implemented order handling with a stubbed risk manager and a single net
position per exchange — grid layered inventory could not be modelled and risk
gates never ran. This connector models a net position with average entry (the
way Binance USDT-M / OKX one-way mode actually net), so many resting grid orders
accumulate and realize PnL correctly.

Fill semantics are ported verbatim from the legacy engine (and keep its fix
history): a limit order marketable at the bar open fills at open as a taker;
one that rests and is reached intra-bar fills at its limit as a maker; slippage
applies only to taker fills; the fee is charged once; a `reduce_only` order only
closes and never flips into a fresh opposite position.

No-look-ahead is enforced by the runner: an order placed while reacting to bar i
is promoted to "resting" only after bar i is fully processed, so it can fill no
earlier than bar i+1's open.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from core.types import (
    Exchange, MarketType, OrderSide, OrderType, OrderStatus, PositionSide,
    Order, Position, Balance, Ticker, TickerEvent, OrderUpdateEvent,
)
from connectors.base import BaseConnector, SymbolRule

# Defaults mirror backtest/engine.py (Binance USDT-M futures)
DEFAULT_TAKER_FEE = Decimal("0.0004")  # 0.04%
DEFAULT_MAKER_FEE = Decimal("0.0002")  # 0.02%
FUNDING_INTERVAL_S = 8 * 3600


@dataclass
class _NetPosition:
    """Net position per symbol: signed size, average entry, opening time, and the
    accumulated entry fees still attributable to the open quantity (so a close can
    report a per-trade PnL net of both entry and exit fees, like the legacy engine)."""
    size: Decimal = Decimal("0")        # >0 long, <0 short
    avg_entry: Decimal = Decimal("0")
    entry_time: float = 0.0
    entry_fee_acc: Decimal = Decimal("0")


@dataclass
class SimTrade:
    """A realized (closed) round-trip leg, shaped for backtest.metrics."""
    symbol: str
    side: str          # "long" | "short" — direction of the closed position
    entry_time: float
    exit_time: float
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float         # net of entry+exit fees attributed to the closed qty
    pnl_pct: float
    fee: float


@dataclass
class _RestingOrder:
    order: Order
    reduce_only: bool


class SimulatedConnector(BaseConnector):
    def __init__(
        self,
        exchange: Exchange = Exchange.BINANCE,
        market_type: MarketType = MarketType.FUTURES,
        initial_capital: Decimal = Decimal("10000"),
        taker_fee: Decimal = DEFAULT_TAKER_FEE,
        maker_fee: Decimal = DEFAULT_MAKER_FEE,
        slippage_bps: Decimal = Decimal("0"),
        funding_rate: Decimal = Decimal("0"),   # per 8h period, fraction (0.0001 = 0.01%)
        half_spread_bps: Decimal = Decimal("1"),  # synthesized bid/ask half-spread around bar open
        rules: Optional[dict[str, SymbolRule]] = None,
    ):
        super().__init__(api_key="", secret="", market_type=market_type, testnet=True)
        self._exchange = exchange
        self._balance = Decimal(initial_capital)   # cash; includes realized PnL & fees
        self._taker_fee = taker_fee
        self._maker_fee = maker_fee
        self._slip = slippage_bps / Decimal("10000")
        self._funding_rate = funding_rate
        self._half_spread = half_spread_bps / Decimal("10000")
        self._rules = dict(rules or {})

        self._positions: dict[str, _NetPosition] = {}
        self._resting: list[_RestingOrder] = []        # eligible to fill this/next bar
        self._pending_new: list[_RestingOrder] = []     # placed this bar — promoted after the bar
        self._orders_by_id: dict[str, Order] = {}
        self._last_mark: dict[str, Decimal] = {}        # symbol → last bar open (mark)
        self.trades: list[SimTrade] = []
        self._last_funding_ts: Optional[int] = None

    # ── BaseConnector plumbing ────────────────────────────────────────────────

    @property
    def exchange(self) -> Exchange:
        return self._exchange

    async def connect(self) -> None:
        self._running = True

    async def disconnect(self) -> None:
        self._running = False

    async def subscribe_ticker(self, symbol: str) -> None:
        return None

    async def subscribe_orderbook(self, symbol: str, depth: int = 20) -> None:
        return None

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        return None

    # ── Trading ───────────────────────────────────────────────────────────────

    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        quantity: Decimal,
        price: Optional[Decimal] = None,
        reduce_only: bool = False,
        post_only: bool = False,
        client_order_id: Optional[str] = None,
    ) -> Order:
        # Precision parity: snap to lot/tick and reject sub-minimum, exactly like
        # the live connectors. With no rule loaded this is a passthrough.
        qty, px = self._quantize_order(symbol, side, quantity, price)
        order = Order(
            exchange=self._exchange, symbol=symbol, side=side,
            order_type=order_type, quantity=qty, price=px,
            order_id=uuid.uuid4().hex[:12],
            client_order_id=client_order_id or uuid.uuid4().hex[:16],
            status=OrderStatus.OPEN,
        )
        self._orders_by_id[order.order_id] = order
        # Queued, not yet eligible — the runner promotes after the current bar so
        # the order cannot fill on the same bar that triggered it (no look-ahead).
        self._pending_new.append(_RestingOrder(order, reduce_only))
        return order

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        for bucket in (self._resting, self._pending_new):
            for i, ro in enumerate(bucket):
                if ro.order.order_id == order_id:
                    ro.order.status = OrderStatus.CANCELLED
                    bucket.pop(i)
                    return True
        return False

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> int:
        n = 0
        for bucket in (self._resting, self._pending_new):
            keep: list[_RestingOrder] = []
            for ro in bucket:
                if symbol is None or ro.order.symbol == symbol:
                    ro.order.status = OrderStatus.CANCELLED
                    n += 1
                else:
                    keep.append(ro)
            bucket[:] = keep
        return n

    async def get_order(self, symbol: str, order_id: str) -> Order:
        return self._orders_by_id.get(order_id)

    async def get_open_orders(self, symbol: Optional[str] = None) -> list[Order]:
        out = [ro.order for ro in (self._resting + self._pending_new)
               if symbol is None or ro.order.symbol == symbol]
        return out

    async def get_positions(self) -> list[Position]:
        out: list[Position] = []
        for sym, pos in self._positions.items():
            if pos.size == 0:
                continue
            mark = self._last_mark.get(sym, pos.avg_entry)
            side = PositionSide.LONG if pos.size > 0 else PositionSide.SHORT
            size = abs(pos.size)
            upnl = (mark - pos.avg_entry) * size if pos.size > 0 else (pos.avg_entry - mark) * size
            out.append(Position(
                exchange=self._exchange, symbol=sym, side=side,
                size=size, entry_price=pos.avg_entry, mark_price=mark,
                leverage=1, unrealized_pnl=upnl, margin=Decimal("0"),
            ))
        return out

    async def get_balances(self) -> list[Balance]:
        return [Balance(self._exchange, "USDT", self._balance, Decimal("0"))]

    # ── Replay driver hooks (called by BacktestRunner) ────────────────────────

    async def settle_bar(self, bar) -> None:
        """Settle resting orders against `bar` and emit fills + the bar's ticker.

        Order matters: fills are emitted before the ticker, so the engine runs
        `record_fill` before `on_ticker` for the same bar (matching the legacy
        engine). Uncrossed limit orders stay resting for a later bar."""
        self._settle_funding(bar)
        self._last_mark[bar_symbol(bar)] = Decimal(str(bar.open))

        still: list[_RestingOrder] = []
        for ro in self._resting:
            fill = self._try_fill(ro.order, bar)
            if fill is None:
                still.append(ro)
                continue
            fill_price, fee, is_maker = fill
            self._apply_fill(ro.order, ro.reduce_only, fill_price, fee, float(bar.ts))
            filled = Order(
                exchange=self._exchange, symbol=ro.order.symbol, side=ro.order.side,
                order_type=ro.order.order_type, quantity=ro.order.quantity,
                price=ro.order.price, order_id=ro.order.order_id,
                client_order_id=ro.order.client_order_id,
                status=OrderStatus.FILLED, filled_qty=ro.order.quantity,
                avg_price=fill_price, fee=fee, fee_ccy="USDT",
                strategy_id=ro.order.strategy_id,
            )
            self._orders_by_id[ro.order.order_id] = filled
            await self._emit(OrderUpdateEvent(filled))
        self._resting = still

    async def emit_ticker(self, bar) -> None:
        """Emit a TickerEvent for `bar` using bar-open prices (no close look-ahead)."""
        sym = bar_symbol(bar)
        open_p = Decimal(str(bar.open))
        ticker = Ticker(
            exchange=self._exchange, symbol=sym,
            bid=open_p * (1 - self._half_spread), ask=open_p * (1 + self._half_spread),
            last=open_p, volume_24h=Decimal(str(getattr(bar, "volume", 0) or 0)),
            timestamp=float(bar.ts),
        )
        await self._emit(TickerEvent(ticker))

    def promote_pending(self) -> None:
        """Move orders placed during this bar into the resting book so they become
        eligible to fill no earlier than the next bar."""
        if self._pending_new:
            self._resting.extend(self._pending_new)
            self._pending_new = []

    def equity(self, mark_prices: Optional[dict[str, Decimal]] = None) -> Decimal:
        """Cash balance plus mark-to-market unrealized PnL across open positions."""
        marks = mark_prices or self._last_mark
        total = self._balance
        for sym, pos in self._positions.items():
            if pos.size == 0:
                continue
            mark = marks.get(sym, self._last_mark.get(sym, pos.avg_entry))
            if pos.size > 0:
                total += (mark - pos.avg_entry) * pos.size
            else:
                total += (pos.avg_entry - mark) * abs(pos.size)
        return total

    def liquidate_all(self, mark_prices: dict[str, Decimal], ts: float) -> None:
        """Force-close every open position at the given marks (end-of-test) as a
        taker market close. Mirrors the legacy engine's final liquidation."""
        for sym, pos in list(self._positions.items()):
            if pos.size == 0:
                continue
            mark = mark_prices.get(sym, self._last_mark.get(sym, pos.avg_entry))
            qty = abs(pos.size)
            fee = qty * mark * self._taker_fee
            side = OrderSide.SELL if pos.size > 0 else OrderSide.BUY
            close = Order(
                exchange=self._exchange, symbol=sym, side=side,
                order_type=OrderType.MARKET, quantity=qty,
                order_id=uuid.uuid4().hex[:12], status=OrderStatus.FILLED,
            )
            self._apply_fill(close, reduce_only=True, fill_price=mark, fee=fee, ts=ts)

    # ── Fill mechanics (ported from backtest/engine.py) ───────────────────────

    def _try_fill(self, order: Order, bar) -> Optional[tuple[Decimal, Decimal, bool]]:
        """Return (fill_price, fee, is_maker) if `bar` fills `order`, else None."""
        open_p = Decimal(str(bar.open))
        is_maker = False
        if order.order_type == OrderType.LIMIT and order.price is not None:
            limit = order.price
            if order.side == OrderSide.BUY:
                if open_p <= limit:
                    raw = open_p                       # marketable at open → taker
                elif Decimal(str(bar.low)) <= limit:
                    raw, is_maker = limit, True         # rested, reached intra-bar → maker
                else:
                    return None
            else:
                if open_p >= limit:
                    raw = open_p
                elif Decimal(str(bar.high)) >= limit:
                    raw, is_maker = limit, True
                else:
                    return None
        else:
            raw = open_p                                # market order
        # Slippage applies only to taker fills (a resting maker executes at its price)
        if is_maker:
            fill_price = raw
        elif order.side == OrderSide.BUY:
            fill_price = raw * (1 + self._slip)
        else:
            fill_price = raw * (1 - self._slip)
        fee = order.quantity * fill_price * (self._maker_fee if is_maker else self._taker_fee)
        return fill_price, fee, is_maker

    def _apply_fill(
        self, order: Order, reduce_only: bool,
        fill_price: Decimal, fee: Decimal, ts: float,
    ) -> None:
        """Apply a fill to the net position; realize PnL on the closed portion.

        Charges the fee once (against cash). A reduce_only order only closes and
        never flips. A non-reduce order that exceeds the opposing position flips
        and re-bases the average entry at the fill price."""
        self._balance -= fee
        sym = order.symbol
        qty = order.quantity
        signed = qty if order.side == OrderSide.BUY else -qty
        pos = self._positions.get(sym)

        if pos is None or pos.size == 0:
            if reduce_only:
                return  # nothing to reduce
            self._positions[sym] = _NetPosition(
                size=signed, avg_entry=fill_price, entry_time=ts, entry_fee_acc=fee)
            return

        same_dir = (pos.size > 0) == (signed > 0)
        if same_dir:
            if reduce_only:
                return  # reduce_only can never increase exposure
            total = abs(pos.size) + qty
            pos.avg_entry = (pos.avg_entry * abs(pos.size) + fill_price * qty) / total
            pos.size += signed
            pos.entry_fee_acc += fee
            return

        # Opposite direction → close (and possibly flip)
        size_before = abs(pos.size)
        close_qty = min(qty, size_before)
        if pos.size > 0:   # long, being sold
            realized = (fill_price - pos.avg_entry) * close_qty
            closed_side = "long"
        else:              # short, being bought
            realized = (pos.avg_entry - fill_price) * close_qty
            closed_side = "short"
        self._balance += realized
        # Attribute entry+exit fee shares to the closed quantity so the trade's
        # reported PnL is net of both, matching the legacy engine.
        entry_fee_share = pos.entry_fee_acc * (close_qty / size_before) if size_before > 0 else pos.entry_fee_acc
        pos.entry_fee_acc -= entry_fee_share
        exit_fee_share = fee * (close_qty / qty) if qty > 0 else fee
        self._record_trade(sym, closed_side, pos.avg_entry, fill_price, close_qty,
                           entry_fee_share + exit_fee_share, pos.entry_time, ts, realized)

        if reduce_only:
            # Move toward zero only, never flip
            delta = close_qty if pos.size < 0 else -close_qty
            pos.size += delta
            if pos.size == 0:
                del self._positions[sym]
            return

        pos.size += signed
        if pos.size == 0:
            del self._positions[sym]
        else:
            # flipped to the opposite side: re-base on the fill, carrying the
            # remaining exit fee as the new position's entry fee.
            pos.avg_entry = fill_price
            pos.entry_time = ts
            remaining = qty - close_qty
            pos.entry_fee_acc = fee * (remaining / qty) if qty > 0 else Decimal("0")

    def _record_trade(
        self, symbol: str, side: str, entry_price: Decimal, exit_price: Decimal,
        qty: Decimal, fee: Decimal, entry_time: float, exit_time: float,
        gross_pnl: Decimal,
    ) -> None:
        if entry_price > 0:
            pnl_pct = float((exit_price - entry_price) / entry_price * 100)
            if side == "short":
                pnl_pct = -pnl_pct
        else:
            pnl_pct = 0.0
        self.trades.append(SimTrade(
            symbol=symbol, side=side,
            entry_time=entry_time, exit_time=exit_time,
            entry_price=float(entry_price), exit_price=float(exit_price),
            quantity=float(qty), pnl=float(gross_pnl - fee), pnl_pct=pnl_pct,
            fee=float(fee),
        ))

    def _settle_funding(self, bar) -> None:
        if self._funding_rate == 0:
            return
        if self._last_funding_ts is None:
            self._last_funding_ts = bar.ts
            return
        if bar.ts - self._last_funding_ts < FUNDING_INTERVAL_S:
            return
        mark = Decimal(str(bar.open))
        for pos in self._positions.values():
            if pos.size == 0:
                continue
            payment = abs(pos.size) * mark * self._funding_rate
            # Longs pay funding on a positive rate, shorts receive
            self._balance += -payment if pos.size > 0 else payment
        self._last_funding_ts = bar.ts


def bar_symbol(bar) -> str:
    """OHLCVRow rows carry their own symbol; fall back to a generic attr name."""
    return getattr(bar, "symbol", None) or getattr(bar, "sym", "")
