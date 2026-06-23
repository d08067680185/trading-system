"""Lightweight Telegram alerter — async, reuses existing aiohttp dep."""
from __future__ import annotations
import asyncio
import logging
import os
import time
from typing import Optional
import aiohttp

logger = logging.getLogger("TelegramAlerter")
_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
_THROTTLE_S = 60.0      # min seconds between same-tag messages
_THROTTLE_MAX = 200     # max entries in the throttle dict before pruning


class TelegramAlerter:
    def __init__(self, token: str = "", chat_id: str = ""):
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
        self._last: dict[str, float] = {}
        self._enabled = bool(self.token and self.chat_id)
        if self._enabled:
            logger.info(f"Telegram alerts active (chat={self.chat_id})")

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _prune_throttle(self) -> None:
        """Remove stale entries to prevent unbounded growth."""
        if len(self._last) > _THROTTLE_MAX:
            cutoff = time.time() - _THROTTLE_S * 2
            self._last = {k: v for k, v in self._last.items() if v > cutoff}

    async def send(self, text: str, tag: str = "", force: bool = False) -> bool:
        if not self._enabled:
            return False
        now = time.time()
        if tag and not force and (now - self._last.get(tag, 0) < _THROTTLE_S):
            return False
        if tag:
            self._last[tag] = now
            self._prune_throttle()

        url = _API_URL.format(token=self.token)
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"}
        for attempt in range(3):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        url, json=payload,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as r:
                        if r.status == 200:
                            return True
                        if r.status == 429:
                            retry_after = int(r.headers.get("Retry-After", 5))
                            logger.warning(f"Telegram rate-limited, retrying in {retry_after}s")
                            await asyncio.sleep(retry_after)
                            continue
                        if r.status >= 500:
                            wait = 2 ** attempt
                            logger.warning(f"Telegram server error {r.status}, retrying in {wait}s")
                            await asyncio.sleep(wait)
                            continue
                        logger.warning(f"Telegram HTTP {r.status}: {await r.text()}")
                        return False
            except Exception as e:
                wait = 2 ** attempt
                logger.warning(f"Telegram send error (attempt {attempt+1}): {e}, retrying in {wait}s")
                await asyncio.sleep(wait)
        return False

    async def alert_halt(self, reason: str = "") -> None:
        msg = "🚨 <b>TRADING HALTED</b>"
        if reason:
            msg += f"\nReason: {reason}"
        await self.send(msg, tag="halt", force=True)

    async def alert_resume(self) -> None:
        await self.send("▶️ <b>Trading RESUMED</b>", tag="resume", force=True)

    async def alert_loss_warning(self, daily_pnl: float, limit: float) -> None:
        pct = abs(daily_pnl) / limit * 100 if limit else 0
        await self.send(
            f"⚠️ <b>Loss Warning</b>\n"
            f"Daily P&amp;L: <b>{daily_pnl:.2f} USDT</b> "
            f"({pct:.0f}% of {limit:.0f} limit)",
            tag="loss_warn",
        )

    async def alert_arb(self, symbol: str, bps: float, strategy: str) -> None:
        await self.send(
            f"💰 <b>Arb Signal</b> · {symbol}\nNet spread: {bps:.1f} bps · {strategy}",
            tag=f"arb_{symbol}",
        )

    async def alert_fill(self, strategy_id: str, symbol: str, side: str,
                          qty: float, price: float) -> None:
        emoji = "🟢" if side.lower() in ("buy", "long") else "🔴"
        await self.send(
            f"{emoji} <b>Order Filled</b>\n"
            f"Strategy: <code>{strategy_id}</code>\n"
            f"{side.upper()} {qty:.6g} {symbol} @ {price:,.4f}",
            tag=f"fill_{strategy_id}",
        )

    async def alert_strategy_error(self, strategy_id: str, error: str) -> None:
        await self.send(
            f"❌ <b>Strategy Error</b>\n"
            f"<code>{strategy_id}</code>\n{error[:300]}",
            tag=f"strat_err_{strategy_id}",
            force=True,
        )

    async def alert_drawdown(self, drawdown_pct: float, peak: float, current: float) -> None:
        await self.send(
            f"📉 <b>Drawdown Alert</b>\n"
            f"Current drawdown: <b>{drawdown_pct:.1f}%</b>\n"
            f"Peak: {peak:,.2f} USDT → Current: {current:,.2f} USDT",
            tag="drawdown",
            force=True,
        )

    async def alert_price(self, symbol: str, exchange: str, price: float, rule_desc: str) -> None:
        await self.send(
            f"🔔 <b>Price Alert</b> · {symbol}\n"
            f"Exchange: {exchange}\nPrice: <b>{price:,.4f}</b>\n{rule_desc}",
            tag=f"price_alert_{symbol}",
        )

    async def test(self) -> bool:
        return await self.send(
            "✅ <b>Trading System</b>\nTelegram alerts configured correctly.",
            tag="test",
            force=True,
        )

    async def daily_report(
        self,
        date_str: str,
        daily_pnl: float,
        total_equity: float,
        strategy_stats: list[dict],
        total_fees: float,
        open_positions: int,
        next_funding_ts: Optional[float] = None,
    ) -> bool:
        """Send a structured daily P&L summary."""
        pnl_emoji = "🟢" if daily_pnl >= 0 else "🔴"
        lines = [
            f"📊 <b>Daily Report — {date_str}</b>",
            "",
            f"{pnl_emoji} Net P&L: <b>{daily_pnl:+.4f} USDT</b>",
            f"💼 Total Equity: <b>{total_equity:.2f} USDT</b>",
            f"💸 Fees Paid: <b>{total_fees:.4f} USDT</b>",
            f"📋 Open Positions: <b>{open_positions}</b>",
        ]
        if strategy_stats:
            lines.append("")
            lines.append("📈 <b>Strategy Breakdown:</b>")
            for s in strategy_stats:
                sid = s.get("strategy_id", "?")
                pnl = s.get("daily_pnl", 0.0)
                trades = s.get("trade_count", 0)
                emoji = "▲" if pnl >= 0 else "▼"
                lines.append(f"  {emoji} {sid}: <b>{pnl:+.4f} USDT</b> ({trades} trades)")
        if next_funding_ts:
            mins = int((next_funding_ts - __import__('time').time()) / 60)
            lines.append(f"\n⏰ Next funding in: <b>{mins} min</b>")
        return await self.send("\n".join(lines), tag="daily_report", force=True)
