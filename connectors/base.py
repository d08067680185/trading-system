from __future__ import annotations
import asyncio
import logging
import ssl
import time
import uuid
import certifi
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Optional
from core.types import (
    Exchange, MarketType, OrderSide, OrderType,
    Order, Position, Balance, TickerEvent,
)


def make_ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=certifi.where())


# ── Exchange trading rules (precision / lot size) ────────────────────────────

@dataclass
class SymbolRule:
    """Per-symbol trading constraints fetched from the exchange at connect time."""
    tick_size: Decimal = Decimal("0")     # min price increment
    step_size: Decimal = Decimal("0")     # min quantity increment (lot size)
    min_qty: Decimal = Decimal("0")       # smallest allowed order quantity
    min_notional: Decimal = Decimal("0")  # smallest allowed order value (price*qty)
    contract_val: Decimal = Decimal("0")  # coin per contract (OKX swap); 0 = qty is the coin amount


def snap_to_step(value: Decimal, step: Decimal, rounding=ROUND_DOWN) -> Decimal:
    """Snap a value down (or per `rounding`) to the nearest multiple of `step`."""
    if step is None or step <= 0:
        return value
    return (value / step).to_integral_value(rounding=rounding) * step


def fmt_decimal(d: Decimal) -> str:
    """Plain-string Decimal (no scientific notation) for exchange request params."""
    return format(d.normalize(), "f")


def gen_client_order_id(prefix: str = "ts") -> str:
    """Generate a client order id valid for BOTH Binance (<=36, [.A-Za-z0-9_:/-])
    and OKX (<=32, alphanumeric). Used for idempotent order placement: the same
    id is reused across every retry of one signal so the exchange dedups."""
    return f"{prefix}{uuid.uuid4().hex}"[:32]


class AsyncRateLimiter:
    """Async token-bucket limiter — caps request rate to avoid 429/418 bans."""

    def __init__(self, rate_per_sec: float, capacity: Optional[float] = None):
        self._rate = float(rate_per_sec)
        self._capacity = float(capacity if capacity is not None else rate_per_sec)
        self._tokens = self._capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self._capacity, self._tokens + (now - self._updated) * self._rate)
                self._updated = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                await asyncio.sleep((tokens - self._tokens) / self._rate)


def symbol_to_exchange(symbol: str, exchange: Exchange, market_type: MarketType) -> str:
    """BTC-USDT  →  exchange native format"""
    parts = symbol.split("-")
    base, quote = parts[0], parts[1]
    if exchange in (Exchange.BINANCE, Exchange.BINANCE_SPOT):
        return f"{base}{quote}"                    # BTCUSDT
    if exchange in (Exchange.OKX, Exchange.OKX_SPOT):
        if market_type == MarketType.SWAP:
            return f"{base}-{quote}-SWAP"          # BTC-USDT-SWAP
        return f"{base}-{quote}"                   # BTC-USDT


def symbol_from_exchange(raw: str, exchange: Exchange) -> str:
    """Exchange native  →  BTC-USDT"""
    if exchange in (Exchange.BINANCE, Exchange.BINANCE_SPOT):
        for quote in ("USDT", "BUSD", "BTC", "ETH", "BNB"):
            if raw.endswith(quote):
                return f"{raw[:-len(quote)]}-{quote}"
        return raw
    if exchange in (Exchange.OKX, Exchange.OKX_SPOT):
        parts = raw.split("-")
        return f"{parts[0]}-{parts[1]}"
    return raw


class BaseConnector(ABC):
    def __init__(
        self,
        api_key: str,
        secret: str,
        market_type: MarketType,
        testnet: bool = False,
    ):
        self.api_key = api_key
        self.secret = secret
        self.market_type = market_type
        self.testnet = testnet
        self.logger = logging.getLogger(self.__class__.__name__)
        self._event_queue: Optional[asyncio.Queue] = None
        self._running = False
        # symbol → trading rules (precision/lot size); loaded at connect
        self._rules: dict[str, SymbolRule] = {}
        # token-bucket request limiter (set by subclass)
        self._limiter: Optional[AsyncRateLimiter] = None
        # server-clock offset in ms (server_time − local_time), kept in sync
        self._time_offset_ms: int = 0

    def set_event_queue(self, queue: asyncio.Queue) -> None:
        self._event_queue = queue

    async def _emit(self, event) -> None:
        if self._event_queue is not None:
            await self._event_queue.put(event)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    # ── Subscriptions ────────────────────────────────────────────────────────

    @abstractmethod
    async def subscribe_ticker(self, symbol: str) -> None: ...

    @abstractmethod
    async def subscribe_orderbook(self, symbol: str, depth: int = 20) -> None: ...

    async def unsubscribe_ticker(self, symbol: str) -> None:
        """Optional: unsubscribe from ticker stream for a symbol. Override in subclass."""

    async def unsubscribe_orderbook(self, symbol: str) -> None:
        """Optional: unsubscribe from orderbook stream for a symbol. Override in subclass."""

    # ── Trading ──────────────────────────────────────────────────────────────

    @abstractmethod
    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        quantity: Decimal,
        price: Optional[Decimal] = None,
        reduce_only: bool = False,
        post_only: bool = False,   # Post-Only / maker-only (GTX on Binance, post_only on OKX)
        client_order_id: Optional[str] = None,  # idempotency key; auto-generated if None
    ) -> Order: ...

    @abstractmethod
    async def cancel_order(self, symbol: str, order_id: str) -> bool: ...

    @abstractmethod
    async def get_order(self, symbol: str, order_id: str) -> Order: ...

    @abstractmethod
    async def get_open_orders(self, symbol: Optional[str] = None) -> list[Order]: ...

    @abstractmethod
    async def cancel_all_orders(self, symbol: Optional[str] = None) -> int: ...

    # ── Account ──────────────────────────────────────────────────────────────

    @abstractmethod
    async def get_positions(self) -> list[Position]: ...

    @abstractmethod
    async def get_balances(self) -> list[Balance]: ...

    @abstractmethod
    async def set_leverage(self, symbol: str, leverage: int) -> None: ...

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _quantize_order(
        self, symbol: str, side: OrderSide,
        quantity: Decimal, price: Optional[Decimal],
    ) -> tuple[Decimal, Optional[Decimal]]:
        """Snap qty/price to the exchange's lot/tick size and validate minimums.

        Returns the adjusted (quantity, price). Raises ValueError if the order
        falls below min qty / min notional — better to reject locally than to
        fire a request the exchange will bounce. If no rule was loaded for the
        symbol, passes values through unchanged so trading is never blocked."""
        rule = self._rules.get(symbol)
        if rule is None:
            return quantity, price
        qty = snap_to_step(quantity, rule.step_size, ROUND_DOWN)
        px = price
        if price is not None and rule.tick_size > 0:
            # round to keep a maker limit on its own side of the book
            px = snap_to_step(price, rule.tick_size, ROUND_DOWN if side == OrderSide.BUY else ROUND_UP)
        if rule.min_qty > 0 and qty < rule.min_qty:
            raise ValueError(f"{symbol}: qty {qty} below min_qty {rule.min_qty}")
        ref = px if px is not None else price
        if rule.min_notional > 0 and ref and qty * ref < rule.min_notional:
            raise ValueError(f"{symbol}: notional {qty * ref} below min_notional {rule.min_notional}")
        return qty, px

    def min_order_usdt(self, symbol: str, ref_price: Decimal) -> Decimal:
        """Smallest order notional (USDT) that will clear this exchange's
        min_qty / min_notional for `symbol` at `ref_price`. Returns 0 if no
        rule was loaded (unconstrained) — callers should treat that as
        "unknown" rather than "free to trade any size"."""
        rule = self._rules.get(symbol)
        if rule is None or ref_price <= 0:
            return Decimal("0")
        from_min_qty = rule.min_qty * ref_price
        return max(rule.min_notional, from_min_qty)

    async def _load_symbol_rules(self) -> None:
        """Optional: fetch per-symbol trading rules. Override in subclass."""

    async def _sync_time(self) -> None:
        """Optional: sync local clock to the exchange server. Override in subclass."""

    def to_exchange_symbol(self, symbol: str) -> str:
        return symbol_to_exchange(symbol, self.exchange, self.market_type)

    def from_exchange_symbol(self, raw: str) -> str:
        return symbol_from_exchange(raw, self.exchange)

    @property
    @abstractmethod
    def exchange(self) -> Exchange: ...
