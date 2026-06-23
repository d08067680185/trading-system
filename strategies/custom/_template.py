"""
自定义策略模板 — 复制此文件并重命名（例如 my_strategy.py），然后修改类名和逻辑。

上传规则：
  - 文件名不能以 _ 开头（以 _ 开头的文件会被忽略）
  - 文件内必须包含继承 BaseStrategy 的类
  - 类名会作为 strategy_id 显示在界面中（例如 MyStrategy → my_strategy）
"""
from __future__ import annotations
from decimal import Decimal

from strategies.base import BaseStrategy
from core.types import (
    Exchange, OrderSide, OrderType,
    Signal, TickerEvent, OrderBookEvent,
    OrderUpdateEvent, PositionUpdateEvent,
)


class MyCustomStrategy(BaseStrategy):
    """将 MyCustomStrategy 替换为你的策略名称。"""

    def __init__(self, strategy_id: str, params: dict):
        # 在这里定义默认参数，这些参数会显示在 UI 的参数编辑器中
        defaults = {
            "threshold_bps": 10.0,   # 触发阈值
            "size_usdt":     100.0,  # 每次下单金额
            "cooldown_s":    60.0,   # 两次开仓最小间隔（秒）
        }
        defaults.update(params)
        super().__init__(strategy_id, defaults)

        # 在这里初始化策略内部状态
        self._trade_count = 0

    # ── 事件回调 ──────────────────────────────────────────────────────────────

    async def on_ticker(self, event: TickerEvent) -> list[Signal]:
        """每次收到新的 bid/ask 报价时触发。返回 Signal 列表即可下单。"""
        ticker = event.ticker
        # ticker.exchange  → Exchange.BINANCE / Exchange.OKX
        # ticker.symbol    → "BTC-USDT"
        # ticker.bid       → Decimal
        # ticker.ask       → Decimal
        # ticker.last      → Decimal

        # 示例：发出买入信号（取消注释后生效）
        # self._trade_count += 1
        # return [Signal(
        #     exchange=ticker.exchange,
        #     symbol=ticker.symbol,
        #     side=OrderSide.BUY,
        #     order_type=OrderType.MARKET,
        #     quantity=Decimal("0.001"),
        #     strategy_id=self.strategy_id,
        #     reason="custom_signal",
        # )]
        return []

    async def on_orderbook(self, event: OrderBookEvent) -> list[Signal]:
        """收到新的深度行情时触发。"""
        # event.orderbook.bids → [(price, qty), ...]
        # event.orderbook.asks → [(price, qty), ...]
        return []

    async def on_order_update(self, event: OrderUpdateEvent) -> None:
        """订单状态变化时触发（成交、撤单等）。"""
        pass

    async def on_position_update(self, event: PositionUpdateEvent) -> None:
        """持仓变化时触发。"""
        pass

    def on_params_updated(self, changed: dict) -> None:
        """参数热更新后触发，可以在这里重置内部状态。"""
        pass

    # ── 状态上报（显示在 UI 统计栏）────────────────────────────────────────────

    def get_status(self) -> dict:
        """返回的所有字段都会动态显示在策略卡片的统计栏中。"""
        return {
            "strategy_id": self.strategy_id,
            "enabled":     self._enabled,
            "params":      self.params,
            "trade_count": self._trade_count,
        }
