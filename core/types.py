from __future__ import annotations
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Optional
import time
import uuid


class Exchange(str, Enum):
    BINANCE = "binance"           # Binance USDT-M futures
    BINANCE_SPOT = "binance_spot" # Binance spot
    OKX = "okx"                   # OKX USDT perpetual swap
    OKX_SPOT = "okx_spot"         # OKX spot


class MarketType(str, Enum):
    SPOT = "spot"
    FUTURES = "futures"   # Binance USDT-M perpetual
    SWAP = "swap"         # OKX USDT perpetual


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP_MARKET = "stop_market"


class OrderStatus(str, Enum):
    PENDING = "pending"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class PositionSide(str, Enum):
    LONG = "long"
    SHORT = "short"
    BOTH = "both"  # hedge mode off


# ── Market Data ──────────────────────────────────────────────────────────────

@dataclass
class Ticker:
    exchange: Exchange
    symbol: str          # normalized: "BTC-USDT"
    bid: Decimal
    ask: Decimal
    last: Decimal
    volume_24h: Decimal
    timestamp: float = field(default_factory=time.time)

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    @property
    def spread_bps(self) -> Decimal:
        return self.spread / self.mid * 10000


@dataclass
class OrderBook:
    exchange: Exchange
    symbol: str
    bids: list[tuple[Decimal, Decimal]]  # [(price, qty), ...] descending
    asks: list[tuple[Decimal, Decimal]]  # [(price, qty), ...] ascending
    timestamp: float = field(default_factory=time.time)

    @property
    def best_bid(self) -> Optional[Decimal]:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Optional[Decimal]:
        return self.asks[0][0] if self.asks else None

    def bid_depth(self, levels: int = 5) -> Decimal:
        return sum(p * q for p, q in self.bids[:levels])

    def ask_depth(self, levels: int = 5) -> Decimal:
        return sum(p * q for p, q in self.asks[:levels])


# ── Orders ────────────────────────────────────────────────────────────────────

@dataclass
class Order:
    exchange: Exchange
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: Decimal
    price: Optional[Decimal] = None
    order_id: Optional[str] = None
    client_order_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    status: OrderStatus = OrderStatus.PENDING
    strategy_id: str = ""
    filled_qty: Decimal = Decimal("0")
    avg_price: Decimal = Decimal("0")
    fee: Decimal = Decimal("0")
    fee_ccy: str = ""          # currency the fee is charged in (for USDT normalization)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def is_done(self) -> bool:
        return self.status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED)

    @property
    def notional(self) -> Decimal:
        price = self.avg_price if self.avg_price else (self.price or Decimal("0"))
        return self.filled_qty * price


# ── Positions & Balance ───────────────────────────────────────────────────────

@dataclass
class Position:
    exchange: Exchange
    symbol: str
    side: PositionSide
    size: Decimal          # absolute contracts/coins
    entry_price: Decimal
    mark_price: Decimal
    leverage: int
    unrealized_pnl: Decimal
    margin: Decimal
    liquidation_price: Decimal = Decimal("0")
    updated_at: float = field(default_factory=time.time)

    @property
    def notional(self) -> Decimal:
        return self.size * self.mark_price


@dataclass
class Balance:
    exchange: Exchange
    asset: str
    free: Decimal
    locked: Decimal
    updated_at: float = field(default_factory=time.time)

    @property
    def total(self) -> Decimal:
        return self.free + self.locked


# ── Events (put on asyncio.Queue) ─────────────────────────────────────────────

@dataclass
class TickerEvent:
    ticker: Ticker


@dataclass
class OrderBookEvent:
    orderbook: OrderBook


@dataclass
class OrderUpdateEvent:
    order: Order


@dataclass
class PositionUpdateEvent:
    position: Position


@dataclass
class BalanceUpdateEvent:
    balance: Balance


@dataclass
class ConnectorReadyEvent:
    exchange: Exchange


@dataclass
class ConnectorErrorEvent:
    exchange: Exchange
    error: str


# ── Strategy Signals ──────────────────────────────────────────────────────────

@dataclass
class Signal:
    exchange: Exchange
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: Decimal
    price: Optional[Decimal] = None
    reduce_only: bool = False
    strategy_id: str = ""
    reason: str = ""
    post_only: bool = False   # use maker/post-only order (GTX on Binance, post_only on OKX)
