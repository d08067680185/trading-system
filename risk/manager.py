from __future__ import annotations
import datetime
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional


def _utc_day_start() -> float:
    """Return Unix timestamp of the start of today in UTC."""
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

from core.types import Exchange, Order, OrderSide, OrderStatus, Signal


@dataclass
class RiskConfig:
    max_position_usdt: Decimal = Decimal("1000")
    max_order_usdt: Decimal = Decimal("500")
    max_daily_loss_usdt: Decimal = Decimal("200")
    max_open_orders: int = 10
    enabled: bool = True
    # Drawdown circuit breaker: halt if equity falls this % below its peak (0 = disabled)
    max_drawdown_pct: Decimal = Decimal("0")
    # Concentration limit: one symbol can't exceed this % of total notional (0 = disabled)
    max_symbol_concentration_pct: Decimal = Decimal("0")
    # Rolling window loss limits (0 = disabled)
    max_rolling_7d_loss_usdt: Decimal = Decimal("0")
    max_rolling_30d_loss_usdt: Decimal = Decimal("0")


@dataclass
class RiskState:
    daily_pnl: Decimal = Decimal("0")
    day_start: float = field(default_factory=_utc_day_start)
    # Fix 5: key = (exchange, symbol) to avoid cross-exchange overwrites
    position_notionals: dict[tuple[str, str], Decimal] = field(default_factory=dict)
    # Fix 6: track by order_id to prevent drift from duplicate events
    _open_order_ids: set[str] = field(default_factory=set)
    # Processed fill IDs: prevent double-counting PnL if FILLED event arrives twice
    _processed_fill_ids: set[str] = field(default_factory=set)
    # Per-symbol inventory for realized-PnL tracking.
    # Value: (avg_entry_price, net_qty) — positive = net long, negative = net short
    _inventory: dict[str, tuple[Decimal, Decimal]] = field(default_factory=dict)
    # Drawdown tracking
    peak_equity_usdt: float = 0.0
    # Rolling PnL history: deque of (day_start_ts, pnl_usdt) pairs
    _daily_pnl_history: deque = field(default_factory=lambda: deque(maxlen=31))

    @property
    def open_order_count(self) -> int:
        return len(self._open_order_ids)

    @property
    def position_notional(self) -> dict:
        """Backward-compat alias for position_notionals."""
        return self.position_notionals

    def rolling_pnl(self, days: int) -> Decimal:
        """Sum of daily PnL over the last N days from history."""
        cutoff = _utc_day_start() - (days - 1) * 86400
        return sum(
            Decimal(str(pnl)) for ts, pnl in self._daily_pnl_history
            if ts >= cutoff
        ) + self.daily_pnl  # include current in-progress day

    def reset_if_new_day(self) -> None:
        today_start = _utc_day_start()
        if self.day_start < today_start:
            # Commit completed day to rolling history before resetting
            self._daily_pnl_history.append((self.day_start, float(self.daily_pnl)))
            self.daily_pnl = Decimal("0")
            self.day_start = today_start
            # Fix 4: do NOT clear _inventory — overnight positions need their
            # entry prices to compute PnL correctly when they close next day


class RiskManager:
    def __init__(self, config: RiskConfig):
        self.config = config
        self.state = RiskState()
        self.logger = logging.getLogger("RiskManager")
        self._halted = False
        self._halt_reason: str = ""

    # ── Pre-trade check ───────────────────────────────────────────────────────

    def check_signal(self, signal: Signal) -> bool:
        if not self.config.enabled:
            return True
        if self._halted:
            self.logger.warning("Risk halt active — blocking signal")
            return False

        self.state.reset_if_new_day()

        if self.state.daily_pnl <= -self.config.max_daily_loss_usdt:
            self.logger.error(
                f"Daily loss limit hit ({self.state.daily_pnl} USDT) — halting"
            )
            self.halt(
                f"Daily loss limit hit: {float(self.state.daily_pnl):.2f} USDT "
                f"(limit: {float(self.config.max_daily_loss_usdt):.2f})"
            )
            return False

        # Rolling window checks
        if self.config.max_rolling_7d_loss_usdt > 0:
            rolling7 = self.state.rolling_pnl(7)
            if rolling7 <= -self.config.max_rolling_7d_loss_usdt:
                self.halt(
                    f"7-day rolling loss limit hit: {float(rolling7):.2f} USDT "
                    f"(limit: -{float(self.config.max_rolling_7d_loss_usdt):.2f})"
                )
                return False

        if self.config.max_rolling_30d_loss_usdt > 0:
            rolling30 = self.state.rolling_pnl(30)
            if rolling30 <= -self.config.max_rolling_30d_loss_usdt:
                self.halt(
                    f"30-day rolling loss limit hit: {float(rolling30):.2f} USDT "
                    f"(limit: -{float(self.config.max_rolling_30d_loss_usdt):.2f})"
                )
                return False

        if self.state.open_order_count >= self.config.max_open_orders:
            self.logger.warning(f"Max open orders ({self.config.max_open_orders}) reached")
            return False

        notional = self._estimate_notional(signal)
        if notional > self.config.max_order_usdt:
            self.logger.warning(
                f"Order notional {notional:.2f} > limit {self.config.max_order_usdt}"
            )
            return False

        # Fix 5: look up position by (exchange, symbol) to avoid cross-exchange collision
        current_pos = self.state.position_notionals.get(
            (signal.exchange.value, signal.symbol), Decimal("0")
        )
        if not signal.reduce_only and current_pos + notional > self.config.max_position_usdt:
            self.logger.warning(
                f"Position limit [{signal.exchange.value}:{signal.symbol}]: "
                f"{current_pos:.2f} + {notional:.2f} > {self.config.max_position_usdt}"
            )
            return False

        # Concentration limit: prevent one symbol from dominating total notional
        if self.config.max_symbol_concentration_pct > 0 and not signal.reduce_only and notional > 0:
            total_notional = sum(self.state.position_notionals.values())
            sym_notional = self.state.position_notionals.get(
                (signal.exchange.value, signal.symbol), Decimal("0")
            )
            new_total = total_notional + notional
            new_sym = sym_notional + notional
            conc_pct = new_sym / new_total * 100 if new_total > 0 else Decimal("0")
            if conc_pct > self.config.max_symbol_concentration_pct:
                self.logger.warning(
                    f"Concentration limit: {signal.symbol} would be {float(conc_pct):.0f}% "
                    f"of total notional (max {float(self.config.max_symbol_concentration_pct):.0f}%)"
                )
                return False

        return True

    # ── Post-trade recording ──────────────────────────────────────────────────

    def record_order_placed(self, order: Order) -> None:
        # Fix 6: use order_id set — placing the same order twice is idempotent
        if order.order_id:
            self.state._open_order_ids.add(order.order_id)

    def record_order_update(self, order: Order) -> None:
        # Fix 6: discard by id — duplicate terminal events don't double-decrement
        if order.is_done and order.order_id:
            self.state._open_order_ids.discard(order.order_id)

        if order.status != OrderStatus.FILLED:
            return

        # Deduplicate: same fill event can arrive via both REST reconciliation and WS
        if order.order_id and order.order_id in self.state._processed_fill_ids:
            return
        if order.order_id:
            self.state._processed_fill_ids.add(order.order_id)

        fill_price = order.avg_price if order.avg_price else order.price
        if not fill_price:
            return

        qty = order.filled_qty
        sym = order.symbol
        fee = order.fee
        inv = self.state._inventory
        entry_price, net_qty = inv.get(sym, (Decimal("0"), Decimal("0")))

        if order.side == OrderSide.BUY:
            if net_qty < 0:
                # Closing a short (partially or fully, possibly flipping to long)
                close_qty = min(qty, -net_qty)
                self.state.daily_pnl += (entry_price - fill_price) * close_qty - fee
                new_net = net_qty + qty
                if new_net == 0:
                    inv.pop(sym, None)
                elif new_net > 0:
                    inv[sym] = (fill_price, new_net)    # flipped to long
                else:
                    inv[sym] = (entry_price, new_net)   # still short, reduced
            else:
                # Opening or adding to a long — track VWAP entry
                total = net_qty + qty
                new_avg = (entry_price * net_qty + fill_price * qty) / total
                inv[sym] = (new_avg, total)
                self.state.daily_pnl -= fee

        else:  # SELL
            if net_qty > 0:
                # Closing a long (partially or fully, possibly flipping to short)
                close_qty = min(qty, net_qty)
                self.state.daily_pnl += (fill_price - entry_price) * close_qty - fee
                new_net = net_qty - qty
                if new_net == 0:
                    inv.pop(sym, None)
                elif new_net < 0:
                    inv[sym] = (fill_price, new_net)    # flipped to short
                else:
                    inv[sym] = (entry_price, new_net)   # still long, reduced
            else:
                # Opening or adding to a short — track VWAP entry
                abs_net = abs(net_qty)
                total_abs = abs_net + qty
                new_avg = (entry_price * abs_net + fill_price * qty) / total_abs
                inv[sym] = (new_avg, net_qty - qty)
                self.state.daily_pnl -= fee

        self.logger.debug(f"Daily PnL: {self.state.daily_pnl:.2f} USDT")

    # Fix 5: accept exchange + symbol as separate args
    def record_position_notional(self, exchange: str, symbol: str, notional: Decimal) -> None:
        self.state.position_notionals[(exchange, symbol)] = Decimal(str(notional))

    # ── Manual controls ───────────────────────────────────────────────────────

    def halt(self, reason: str = "") -> None:
        self._halted = True
        self._halt_reason = reason
        self.logger.error(f"Risk HALT: {reason}")

    def update_equity(self, equity_usdt: float) -> None:
        """Called by LiveCollector on each equity snapshot to track peak and check drawdown."""
        if equity_usdt > self.state.peak_equity_usdt:
            self.state.peak_equity_usdt = equity_usdt
        if (self.config.max_drawdown_pct > 0
                and self.state.peak_equity_usdt > 0
                and not self._halted):
            drawdown_pct = (self.state.peak_equity_usdt - equity_usdt) / self.state.peak_equity_usdt * 100
            if drawdown_pct >= float(self.config.max_drawdown_pct):
                self.halt(
                    f"Drawdown limit hit: {drawdown_pct:.1f}% from peak "
                    f"({self.state.peak_equity_usdt:.2f} → {equity_usdt:.2f} USDT)"
                )

    def resume(self) -> None:
        # Fix 3: only clear the halt flag — do NOT reset daily_pnl or inventory.
        # Resetting PnL here would allow bypassing the daily loss limit by
        # triggering a halt and immediately resuming.
        self._halted = False
        self.logger.info("Risk manager resumed")

    @property
    def is_halted(self) -> bool:
        return self._halted

    def status(self) -> dict:
        self.state.reset_if_new_day()
        peak = self.state.peak_equity_usdt
        return {
            "halted": self._halted,
            "halt_reason": self._halt_reason,
            "daily_pnl_usdt": float(self.state.daily_pnl),
            "rolling_7d_pnl_usdt": float(self.state.rolling_pnl(7)),
            "rolling_30d_pnl_usdt": float(self.state.rolling_pnl(30)),
            "open_orders": self.state.open_order_count,
            "peak_equity_usdt": peak,
            "position_notional": {
                f"{ex}:{sym}": float(v)
                for (ex, sym), v in self.state.position_notionals.items()
            },
            "limits": {
                "max_position_usdt": float(self.config.max_position_usdt),
                "max_order_usdt": float(self.config.max_order_usdt),
                "max_daily_loss_usdt": float(self.config.max_daily_loss_usdt),
                "max_open_orders": self.config.max_open_orders,
                "max_drawdown_pct": float(self.config.max_drawdown_pct),
                "max_symbol_concentration_pct": float(self.config.max_symbol_concentration_pct),
                "max_rolling_7d_loss_usdt": float(self.config.max_rolling_7d_loss_usdt),
                "max_rolling_30d_loss_usdt": float(self.config.max_rolling_30d_loss_usdt),
            },
        }

    @staticmethod
    def _estimate_notional(signal: Signal) -> Decimal:
        """For limit orders use the limit price; for market orders quantity alone
        can't give us notional — return 0 so the size check is skipped and the
        strategy's own sizing (already capped via params) is trusted."""
        if signal.price:
            return signal.quantity * signal.price
        return Decimal("0")
