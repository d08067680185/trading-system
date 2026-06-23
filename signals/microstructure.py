"""
Order book microstructure signals.

Computed on each OrderBookEvent:
  - OBI  (Order Book Imbalance): (bid_depth - ask_depth) / total_depth
          > 0 → more buying pressure → price likely to go up
          < 0 → more selling pressure → price likely to go down
  - WOBI (Weighted OBI): depth weighted by inverse distance from mid-price
  - Spread BPS: current best bid-ask spread in basis points
  - Depth ratio: how bid depth compares to ask depth at multiple levels
  - Mid skew: mid-price deviation from VWAP (indicates short-term price pressure)

Strategies query current snapshot for signal filtering/enhancement.
"""
from __future__ import annotations
import math
import logging
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

logger = logging.getLogger("Microstructure")


@dataclass
class MicroSnapshot:
    symbol: str
    exchange: str
    bid: float
    ask: float
    mid: float
    spread_bps: float
    obi: float           # -1 to +1; positive = buy pressure
    wobi: float          # weighted OBI (closer levels count more)
    bid_depth_usdt: float
    ask_depth_usdt: float
    depth_ratio: float   # bid_depth / ask_depth
    vwap_mid: float      # VWAP mid from recent ticks
    mid_vs_vwap_bps: float  # how far mid is from VWAP
    signal_strength: float  # composite 0–1 score (0=neutral, 1=strong directional)
    direction: str       # "buy", "sell", "neutral"


class MicrostructureSignals:
    def __init__(
        self,
        levels: int = 5,            # how many order book levels to use
        vwap_window: int = 50,      # number of mid-price ticks for VWAP
        obi_threshold: float = 0.3, # |OBI| threshold to consider "directional"
    ):
        self._levels = levels
        self._obi_threshold = obi_threshold
        self._snapshots: dict[str, MicroSnapshot] = {}  # "exchange:symbol" → snapshot
        # Mid-price history for VWAP: key → deque of (mid_price, approx_volume)
        self._mid_history: dict[str, deque] = {}
        self._vwap_window = vwap_window

    def update(self, exchange: str, symbol: str,
               bids: list[tuple[Decimal, Decimal]],
               asks: list[tuple[Decimal, Decimal]]) -> Optional[MicroSnapshot]:
        """Update with new order book data. bids/asks are [(price, qty), ...] sorted."""
        if not bids or not asks:
            return None

        key = f"{exchange}:{symbol}"
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        mid = (best_bid + best_ask) / 2.0
        spread_bps = (best_ask - best_bid) / mid * 10000 if mid > 0 else 0.0

        # Compute depth (USDT value) over top N levels
        bid_levels = bids[: self._levels]
        ask_levels = asks[: self._levels]
        bid_depth = sum(float(p) * float(q) for p, q in bid_levels)
        ask_depth = sum(float(p) * float(q) for p, q in ask_levels)
        total_depth = bid_depth + ask_depth

        obi = (bid_depth - ask_depth) / total_depth if total_depth > 0 else 0.0

        # Weighted OBI: levels closer to mid get higher weight
        wobi = self._weighted_obi(best_bid, best_ask, bid_levels, ask_levels)

        depth_ratio = bid_depth / ask_depth if ask_depth > 0 else 1.0

        # VWAP mid
        if key not in self._mid_history:
            self._mid_history[key] = deque(maxlen=self._vwap_window)
        self._mid_history[key].append(mid)
        vwap_mid = sum(self._mid_history[key]) / len(self._mid_history[key])
        mid_vs_vwap_bps = (mid - vwap_mid) / vwap_mid * 10000 if vwap_mid > 0 else 0.0

        # Composite signal
        obi_signal = abs(obi)
        direction = "neutral"
        if obi >= self._obi_threshold:
            direction = "buy"
        elif obi <= -self._obi_threshold:
            direction = "sell"

        signal_strength = min(1.0, obi_signal * 2.0)  # scale 0.3 OBI → 0.6 strength

        snap = MicroSnapshot(
            symbol=symbol,
            exchange=exchange,
            bid=best_bid,
            ask=best_ask,
            mid=mid,
            spread_bps=round(spread_bps, 2),
            obi=round(obi, 4),
            wobi=round(wobi, 4),
            bid_depth_usdt=round(bid_depth, 2),
            ask_depth_usdt=round(ask_depth, 2),
            depth_ratio=round(depth_ratio, 3),
            vwap_mid=round(vwap_mid, 4),
            mid_vs_vwap_bps=round(mid_vs_vwap_bps, 2),
            signal_strength=round(signal_strength, 3),
            direction=direction,
        )
        self._snapshots[key] = snap
        return snap

    def get(self, exchange: str, symbol: str) -> Optional[MicroSnapshot]:
        return self._snapshots.get(f"{exchange}:{symbol}")

    def obi(self, exchange: str, symbol: str) -> float:
        snap = self.get(exchange, symbol)
        return snap.obi if snap else 0.0

    def bid_depth_usdt(self, exchange: str, symbol: str) -> float:
        snap = self.get(exchange, symbol)
        return snap.bid_depth_usdt if snap else 0.0

    def ask_depth_usdt(self, exchange: str, symbol: str) -> float:
        snap = self.get(exchange, symbol)
        return snap.ask_depth_usdt if snap else 0.0

    def is_buy_pressure(self, exchange: str, symbol: str) -> bool:
        return self.obi(exchange, symbol) >= self._obi_threshold

    def is_sell_pressure(self, exchange: str, symbol: str) -> bool:
        return self.obi(exchange, symbol) <= -self._obi_threshold

    def spread_bps(self, exchange: str, symbol: str) -> float:
        snap = self.get(exchange, symbol)
        return snap.spread_bps if snap else 0.0

    def all_snapshots(self) -> dict[str, dict]:
        result = {}
        for key, snap in self._snapshots.items():
            result[key] = {
                "bid": snap.bid,
                "ask": snap.ask,
                "spread_bps": snap.spread_bps,
                "obi": snap.obi,
                "wobi": snap.wobi,
                "bid_depth_usdt": snap.bid_depth_usdt,
                "ask_depth_usdt": snap.ask_depth_usdt,
                "depth_ratio": snap.depth_ratio,
                "mid_vs_vwap_bps": snap.mid_vs_vwap_bps,
                "signal_strength": snap.signal_strength,
                "direction": snap.direction,
            }
        return result

    # ── Internal ─────────────────────────────────────────────────────────────

    def _weighted_obi(
        self,
        best_bid: float,
        best_ask: float,
        bid_levels: list,
        ask_levels: list,
    ) -> float:
        """OBI weighted by inverse distance from mid (closer = more weight)."""
        mid = (best_bid + best_ask) / 2.0
        if mid <= 0:
            return 0.0

        def _weighted_depth(levels, is_bid: bool) -> float:
            total = 0.0
            for price, qty in levels:
                p = float(price)
                dist = abs(p - mid) / mid
                weight = 1.0 / (1.0 + dist * 100)  # decay with distance
                total += float(qty) * p * weight
            return total

        w_bid = _weighted_depth(bid_levels, True)
        w_ask = _weighted_depth(ask_levels, False)
        total = w_bid + w_ask
        return (w_bid - w_ask) / total if total > 0 else 0.0
