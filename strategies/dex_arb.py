"""DEX-CEX arbitrage strategy.

When the Uniswap V3 price deviates from the CEX mid price by more than
min_profit_bps (net of DEX fee + estimated gas cost), execute both legs
simultaneously:
  - Buy the cheap leg, sell the expensive leg
  - DEX leg: execute directly via UniswapV3Connector
  - CEX leg: emit a Signal through the normal engine flow (risk-checked)

The strategy is intentionally conservative:
  - Only one in-flight arb per symbol at a time
  - Gas cost is included in profit calculation
  - Cooldown prevents rapid re-entry
  - Checks are async (non-blocking on the event loop)
"""
from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from core.types import (
    Exchange, OrderSide, OrderType,
    Signal, TickerEvent, Ticker,
)
from strategies.base import BaseStrategy

if TYPE_CHECKING:
    from connectors.dex.uniswap_v3 import UniswapV3Connector
    from connectors.onchain.wallet import EVMWallet
    from connectors.onchain.gas import GasManager


class DexCexArbStrategy(BaseStrategy):
    """
    Params:
      cex_exchange    str      CEX to trade against, "binance" or "okx" (default "binance")
      symbol          str      normalized symbol, e.g. "ETH-USDT" (default "ETH-USDT")
      token_in        str      ERC20 address of the input token (e.g. USDC)
      token_out       str      ERC20 address of the output token (e.g. WETH)
      chain           str      chain name matching config.yaml, e.g. "arbitrum"
      amount_usdt     float    trade size in USDT notional (default 300)
      min_profit_bps  float    min net profit after fees to trigger (default 15)
      cooldown_s      float    seconds between arb checks for this symbol (default 60)
      fee_tier        int      Uniswap pool fee tier in hundredths of bps (default 500 = 0.05%)
      gas_usdt_budget float    max gas cost in USDT to consider profitable (default 5)
    """

    def __init__(self, strategy_id: str, params: dict):
        defaults = {
            "cex_exchange": "binance",
            "symbol": "ETH-USDT",
            "token_in": "",
            "token_out": "",
            "chain": "arbitrum",
            "amount_usdt": 300.0,
            "min_profit_bps": 15.0,
            "cooldown_s": 60.0,
            "fee_tier": 500,
            "gas_usdt_budget": 5.0,
        }
        defaults.update(params)
        super().__init__(strategy_id, defaults)

        self._dex_connector: Optional[UniswapV3Connector] = None
        self._wallet: Optional[EVMWallet] = None
        # Fix 9: live gas price source; fallback to conservative default if not set
        self._gas_manager: Optional[GasManager] = None

        self._last_check: float = 0.0
        self._check_in_flight: bool = False

        self._arb_count = 0
        self._total_profit_usdt = 0.0
        self._last_cex_price: Optional[float] = None
        self._last_dex_price: Optional[float] = None
        self._last_diff_bps: Optional[float] = None

    def set_gas_manager(self, gas_manager: "GasManager") -> None:
        self._gas_manager = gas_manager

    def set_dex(self, connector: "UniswapV3Connector", wallet: "EVMWallet") -> None:
        self._dex_connector = connector
        self._wallet = wallet
        self.logger.info(
            f"{self.strategy_id}: DEX connector set "
            f"(chain={self.params['chain']}, wallet={wallet.address})"
        )

    # ── Event handlers ────────────────────────────────────────────────────────

    async def on_ticker(self, event: TickerEvent) -> list[Signal]:
        t = event.ticker
        symbol: str = self.params["symbol"]
        cex_ex = Exchange(self.params["cex_exchange"])

        if t.symbol != symbol or t.exchange != cex_ex:
            return []
        if not self._dex_connector or not self._wallet:
            return []
        if self.params["token_in"] == "" or self.params["token_out"] == "":
            return []

        now = time.time()
        if now - self._last_check < self.params["cooldown_s"]:
            return []
        if self._check_in_flight:
            return []

        self._last_check = now
        self._check_in_flight = True
        asyncio.create_task(self._evaluate_arb(t))
        return []

    # ── Arb evaluation (runs in background task) ──────────────────────────────

    async def _evaluate_arb(self, cex_ticker: Ticker) -> None:
        try:
            await self._do_evaluate(cex_ticker)
        except Exception as e:
            self.logger.warning(f"Arb evaluation error: {e}")
        finally:
            self._check_in_flight = False

    async def _do_evaluate(self, cex_ticker: Ticker) -> None:
        symbol: str = self.params["symbol"]
        amount_usdt = Decimal(str(self.params["amount_usdt"]))
        min_profit_bps = Decimal(str(self.params["min_profit_bps"]))
        gas_budget_usdt = Decimal(str(self.params["gas_usdt_budget"]))
        fee_tier: int = self.params["fee_tier"]
        token_in: str = self.params["token_in"]
        token_out: str = self.params["token_out"]
        cex_ex = Exchange(self.params["cex_exchange"])

        cex_mid = cex_ticker.mid
        self._last_cex_price = float(cex_mid)

        # ── Get DEX quote (token_in → token_out) ─────────────────────────────
        # Assume token_in is a stablecoin (USDC/USDT) → token_out is the asset
        # amount_in is in token_in units ≈ amount_usdt
        try:
            quote = await self._dex_connector.get_quote(
                token_in, token_out, amount_usdt, fee_tier
            )
        except Exception as e:
            self.logger.debug(f"DEX quote failed: {e}")
            return

        if quote.amount_in == 0:
            return

        # DEX effective price: token_out per token_in (e.g. ETH per USDT)
        dex_price_token_out_per_usdt = quote.effective_price
        if dex_price_token_out_per_usdt == 0:
            return

        # Convert to USDT per token_out to compare with CEX
        dex_price_usdt = Decimal("1") / dex_price_token_out_per_usdt
        self._last_dex_price = float(dex_price_usdt)

        diff_bps = (cex_mid - dex_price_usdt) / dex_price_usdt * 10000
        self._last_diff_bps = float(diff_bps)

        # Estimate gas cost in USDT
        # gas_estimate is in gas units; assume ETH price from CEX as proxy
        eth_price_usdt = cex_mid if "ETH" in symbol else Decimal("3000")
        # Fix 9: use live gas price from GasManager; fall back to conservative default
        if self._gas_manager is not None:
            fees = await self._gas_manager.get_fees()
            gas_price_gwei = Decimal(str(fees.estimated_gwei))
        else:
            gas_price_gwei = Decimal("0.1")
        gas_cost_eth = Decimal(str(quote.gas_estimate)) * gas_price_gwei / Decimal("1e9")
        gas_cost_usdt = gas_cost_eth * eth_price_usdt

        if gas_cost_usdt > gas_budget_usdt:
            self.logger.debug(
                f"Gas too expensive: {gas_cost_usdt:.2f} USDT > budget {gas_budget_usdt}"
            )
            return

        # Net profit after Uniswap fee (already reflected in quote) and gas
        net_profit_bps = diff_bps - gas_cost_usdt / amount_usdt * 10000

        self.logger.debug(
            f"{symbol} cex={float(cex_mid):.2f} dex={float(dex_price_usdt):.2f} "
            f"diff={float(diff_bps):.1f}bps gas={float(gas_cost_usdt):.3f}USDT "
            f"net={float(net_profit_bps):.1f}bps"
        )

        if net_profit_bps >= min_profit_bps:
            # DEX cheaper than CEX → buy on DEX, sell on CEX
            await self._execute_arb(
                symbol=symbol, cex_ex=cex_ex, quote=quote,
                dex_side="buy", cex_side=OrderSide.SELL,
                amount_usdt=amount_usdt, profit_bps=net_profit_bps,
            )
        elif net_profit_bps <= -min_profit_bps:
            # CEX cheaper than DEX → buy on CEX, sell on DEX (reverse swap)
            rev_quote = await self._dex_connector.get_quote(
                token_out, token_in, quote.amount_out, fee_tier
            )
            await self._execute_arb(
                symbol=symbol, cex_ex=cex_ex, quote=rev_quote,
                dex_side="sell", cex_side=OrderSide.BUY,
                amount_usdt=amount_usdt, profit_bps=-net_profit_bps,
            )

    async def _execute_arb(
        self,
        symbol: str,
        cex_ex: Exchange,
        quote,
        dex_side: str,
        cex_side: OrderSide,
        amount_usdt: Decimal,
        profit_bps: Decimal,
    ) -> None:
        reason = f"dex_cex_arb={float(profit_bps):.1f}bps"
        self.logger.info(
            f"Executing arb {symbol}: DEX {dex_side}, CEX {cex_side.value} "
            f"profit≈{float(profit_bps):.1f}bps"
        )

        # Run CEX and DEX legs concurrently
        cex_qty = (amount_usdt / Decimal(str(self._last_cex_price or 1))).quantize(Decimal("0.001"))

        cex_signal = Signal(
            exchange=cex_ex,
            symbol=symbol,
            side=cex_side,
            order_type=OrderType.MARKET,
            quantity=cex_qty,
            strategy_id=self.strategy_id,
            reason=reason,
        )

        dex_task = asyncio.create_task(
            self._dex_connector.swap(
                quote, self._wallet,
                slippage_bps=int(self.params.get("slippage_bps", 50)),
                auto_approve=True,
            )
        )

        # CEX leg via engine — capture the order so we can reverse it if DEX fails
        cex_order = None
        if self.engine:
            cex_order = await self.engine.place_order(
                exchange=cex_signal.exchange,
                symbol=cex_signal.symbol,
                side=cex_signal.side,
                order_type=cex_signal.order_type,
                quantity=cex_signal.quantity,
                strategy_id=self.strategy_id,
            )

        # Fix 2: reverse the CEX leg whenever the DEX swap fails to avoid a naked
        # one-sided position. Use reduce_only so risk manager allows it even under limits.
        reversal_side = OrderSide.BUY if cex_side == OrderSide.SELL else OrderSide.SELL

        try:
            dex_result = await dex_task
            if dex_result.success:
                self._arb_count += 1
                profit_usdt = float(profit_bps) / 10000 * float(amount_usdt)
                self._total_profit_usdt += profit_usdt
                self.logger.info(
                    f"Arb complete: tx={dex_result.tx_hash[:10]}… "
                    f"gas={dex_result.gas_used} profit≈{profit_usdt:.2f}USDT"
                )
            else:
                self.logger.warning(f"DEX swap failed: {dex_result.tx_hash} — reversing CEX leg")
                if cex_order and self.engine:
                    await self.engine.place_order(
                        exchange=cex_ex,
                        symbol=symbol,
                        side=reversal_side,
                        order_type=OrderType.MARKET,
                        quantity=cex_qty,
                        reduce_only=True,
                        strategy_id=self.strategy_id,
                    )
        except Exception as e:
            self.logger.error(f"DEX swap error: {e} — reversing CEX leg")
            if cex_order and self.engine:
                await self.engine.place_order(
                    exchange=cex_ex,
                    symbol=symbol,
                    side=reversal_side,
                    order_type=OrderType.MARKET,
                    quantity=cex_qty,
                    reduce_only=True,
                    strategy_id=self.strategy_id,
                )

    # ── Status ────────────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        return {
            "strategy_id": self.strategy_id,
            "enabled": self._enabled,
            "params": self.params,
            "arb_count": self._arb_count,
            "total_profit_usdt": round(self._total_profit_usdt, 4),
            "last_cex_price": self._last_cex_price,
            "last_dex_price": self._last_dex_price,
            "last_diff_bps": self._last_diff_bps,
            "dex_ready": self._dex_connector is not None and self._wallet is not None,
        }
