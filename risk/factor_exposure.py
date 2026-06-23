"""
Factor Exposure Monitor.

Computes the portfolio's net directional exposure to each reference asset
(BTC, ETH) by aggregating open position deltas across all strategies.

Delta = signed notional value:
  Long  BTC-USDT position → +delta to BTC
  Short BTC-USDT position → -delta to BTC
  Funding arb (long spot + short perp) → ~0 net delta (correctly captured)

Alerts when net beta to BTC or ETH exceeds configured threshold.
"""
from __future__ import annotations
import asyncio
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from core.engine import TradingEngine

logger = logging.getLogger("FactorExposure")


@dataclass
class ExposureSnapshot:
    ts: float
    net_btc_usdt: float     # net USDT exposure to BTC
    net_eth_usdt: float     # net USDT exposure to ETH
    total_notional: float   # total open notional across all positions
    btc_weight: float       # net_btc / total_notional
    eth_weight: float       # net_eth / total_notional
    positions: list[dict]   # per-position breakdown


class FactorExposureMonitor:
    def __init__(
        self,
        engine: "TradingEngine",
        max_net_exposure_pct: float = 50.0,   # warn if |net_exposure| > 50% of total notional
        interval_s: int = 60,
    ):
        self._engine = engine
        self._max_exposure_pct = max_net_exposure_pct
        self._interval = interval_s
        self._task: Optional[asyncio.Task] = None
        self._snapshot: Optional[ExposureSnapshot] = None
        self._last_alert: dict[str, float] = {}
        self._alert_cooldown = 600

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())
        logger.info("FactorExposure monitor started")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def get_snapshot(self) -> Optional[dict]:
        if not self._snapshot:
            return None
        s = self._snapshot
        return {
            "ts": s.ts,
            "net_btc_usdt": round(s.net_btc_usdt, 2),
            "net_eth_usdt": round(s.net_eth_usdt, 2),
            "total_notional": round(s.total_notional, 2),
            "btc_weight_pct": round(s.btc_weight * 100, 1),
            "eth_weight_pct": round(s.eth_weight * 100, 1),
            "positions": s.positions,
            "alert": self._exposure_alert_level(s),
        }

    async def compute_now(self) -> ExposureSnapshot:
        return await self._compute()

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        await asyncio.sleep(15)
        while True:
            try:
                snap = await self._compute()
                self._snapshot = snap
                await self._check_alerts(snap)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"FactorExposure error: {e}")
            await asyncio.sleep(self._interval)

    async def _compute(self) -> ExposureSnapshot:
        positions = await self._engine.get_positions()
        net_btc = 0.0
        net_eth = 0.0
        total_notional = 0.0
        pos_list = []

        for pos in positions:
            notional = float(pos.notional) if pos.notional else 0.0
            if notional <= 0:
                continue
            signed = notional if pos.side.value == "long" else -notional
            sym_upper = pos.symbol.upper()

            pos_list.append({
                "exchange": pos.exchange.value,
                "symbol": pos.symbol,
                "side": pos.side.value,
                "notional": round(notional, 2),
                "signed_notional": round(signed, 2),
            })
            total_notional += notional

            if "BTC" in sym_upper:
                net_btc += signed
            elif "ETH" in sym_upper:
                net_eth += signed

        btc_w = net_btc / total_notional if total_notional > 0 else 0.0
        eth_w = net_eth / total_notional if total_notional > 0 else 0.0

        return ExposureSnapshot(
            ts=time.time(),
            net_btc_usdt=net_btc,
            net_eth_usdt=net_eth,
            total_notional=total_notional,
            btc_weight=btc_w,
            eth_weight=eth_w,
            positions=pos_list,
        )

    async def _check_alerts(self, snap: ExposureSnapshot) -> None:
        notifier = getattr(self._engine, "_notifier", None)
        now = time.time()
        for asset, weight in [("BTC", snap.btc_weight), ("ETH", snap.eth_weight)]:
            if abs(weight * 100) > self._max_exposure_pct:
                key = f"exposure:{asset}"
                if now - self._last_alert.get(key, 0) >= self._alert_cooldown:
                    self._last_alert[key] = now
                    direction = "LONG" if weight > 0 else "SHORT"
                    msg = (
                        f"⚠️ HIGH {asset} EXPOSURE\n"
                        f"Net {direction} {abs(weight * 100):.0f}% of total notional\n"
                        f"Net {asset}: ${abs(snap.net_btc_usdt if asset == 'BTC' else snap.net_eth_usdt):.0f} USDT"
                    )
                    logger.warning(f"Factor exposure alert: {msg.replace(chr(10), ' ')}")
                    if notifier:
                        asyncio.create_task(notifier.send(msg))

    def _exposure_alert_level(self, s: ExposureSnapshot) -> str:
        max_w = max(abs(s.btc_weight), abs(s.eth_weight)) * 100
        if max_w > self._max_exposure_pct:
            return "warning"
        if max_w > self._max_exposure_pct * 0.7:
            return "caution"
        return "ok"
