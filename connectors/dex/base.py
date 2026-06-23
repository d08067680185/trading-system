"""Abstract DEX connector and shared data types."""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional
import time


# ── Chain registry ────────────────────────────────────────────────────────────

CHAIN_IDS = {
    "ethereum": 1,
    "arbitrum": 42161,
    "optimism": 10,
    "base": 8453,
    "bsc": 56,
    "polygon": 137,
}

# Well-known stablecoins / native tokens per chain
WRAPPED_NATIVE = {
    "ethereum": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",   # WETH
    "arbitrum":  "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",   # WETH on Arbitrum
    "optimism":  "0x4200000000000000000000000000000000000006",   # WETH on Optimism
    "base":      "0x4200000000000000000000000000000000000006",   # WETH on Base
}

USDC = {
    "ethereum": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    "arbitrum":  "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
    "optimism":  "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",
    "base":      "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
}


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class SwapQuote:
    chain: str
    dex: str
    token_in: str       # address
    token_out: str      # address
    amount_in: Decimal
    amount_out: Decimal
    price_impact_pct: Decimal
    gas_estimate: int
    route: list[str]    # token addresses in the path
    fee_tier: int = 3000  # Uniswap fee tier (bps * 100)
    valid_until: float = field(default_factory=lambda: time.time() + 30)

    @property
    def effective_price(self) -> Decimal:
        if not self.amount_in:
            return Decimal("0")
        return self.amount_out / self.amount_in

    @property
    def is_expired(self) -> bool:
        return time.time() > self.valid_until


@dataclass
class SwapResult:
    tx_hash: str
    chain: str
    dex: str
    token_in: str
    token_out: str
    amount_in: Decimal
    amount_out: Decimal
    gas_used: int
    gas_price_gwei: float
    timestamp: float = field(default_factory=time.time)
    success: bool = True


@dataclass
class PoolInfo:
    chain: str
    dex: str
    address: str
    token0: str
    token1: str
    fee_tier: int
    liquidity: Decimal
    price: Decimal       # token1 per token0
    tick: int


# ── Abstract connector ────────────────────────────────────────────────────────

class BaseDexConnector(ABC):
    def __init__(self, chain: str, rpc_url: str):
        self.chain = chain
        self.rpc_url = rpc_url
        self._w3 = None

    async def connect(self) -> None:
        try:
            from web3 import AsyncWeb3
            self._w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(self.rpc_url))
        except ImportError:
            raise RuntimeError("web3 not installed. Run: pip install web3>=6.0")

    @abstractmethod
    async def get_quote(
        self,
        token_in: str,
        token_out: str,
        amount_in: Decimal,
        fee_tier: int = 3000,
    ) -> SwapQuote: ...

    @abstractmethod
    async def get_pool_price(
        self,
        token0: str,
        token1: str,
        fee_tier: int = 3000,
    ) -> Decimal: ...

    @abstractmethod
    async def swap(
        self,
        quote: SwapQuote,
        wallet,
        slippage_bps: int = 50,
    ) -> SwapResult: ...

    async def get_price_vs_cex(
        self,
        dex_token_in: str,
        dex_token_out: str,
        cex_price: Decimal,
        amount: Decimal = Decimal("1"),
        fee_tier: int = 3000,
    ) -> dict:
        """Compare DEX price to a CEX price, return arb opportunity info."""
        quote = await self.get_quote(dex_token_in, dex_token_out, amount, fee_tier)
        dex_price = quote.effective_price
        diff_bps = (dex_price - cex_price) / cex_price * 10000
        return {
            "dex_price": float(dex_price),
            "cex_price": float(cex_price),
            "diff_bps": float(diff_bps),
            "price_impact_pct": float(quote.price_impact_pct),
            "profitable": abs(float(diff_bps)) > float(quote.price_impact_pct) * 100 + 10,
        }
