"""
Uniswap V3 connector.
Uses QuoterV2 for off-chain price quotes and SwapRouter02 for execution.
"""
from __future__ import annotations
import logging
from decimal import Decimal
from typing import Optional

from connectors.dex.base import BaseDexConnector, SwapQuote, SwapResult

logger = logging.getLogger("UniswapV3")

# Contract addresses (same on Ethereum, Arbitrum, Optimism, Base)
QUOTER_V2   = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"
SWAP_ROUTER = "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45"   # SwapRouter02

QUOTER_ABI = [
    {
        "inputs": [{"components": [
            {"name": "tokenIn",  "type": "address"},
            {"name": "tokenOut", "type": "address"},
            {"name": "amountIn", "type": "uint256"},
            {"name": "fee",      "type": "uint24"},
            {"name": "sqrtPriceLimitX96", "type": "uint160"},
        ], "name": "params", "type": "tuple"}],
        "name": "quoteExactInputSingle",
        "outputs": [
            {"name": "amountOut",            "type": "uint256"},
            {"name": "sqrtPriceX96After",    "type": "uint160"},
            {"name": "initializedTicksCrossed", "type": "uint32"},
            {"name": "gasEstimate",          "type": "uint256"},
        ],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

ROUTER_ABI = [
    {
        "inputs": [{"components": [
            {"name": "tokenIn",           "type": "address"},
            {"name": "tokenOut",          "type": "address"},
            {"name": "fee",               "type": "uint24"},
            {"name": "recipient",         "type": "address"},
            {"name": "amountIn",          "type": "uint256"},
            {"name": "amountOutMinimum",  "type": "uint256"},
            {"name": "sqrtPriceLimitX96", "type": "uint160"},
        ], "name": "params", "type": "tuple"}],
        "name": "exactInputSingle",
        "outputs": [{"name": "amountOut", "type": "uint256"}],
        "stateMutability": "payable",
        "type": "function",
    }
]

ERC20_DECIMALS_ABI = [{"inputs":[],"name":"decimals","outputs":[{"type":"uint8"}],"stateMutability":"view","type":"function"}]

ERC20_ALLOWANCE_ABI = [
    {"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],
     "name":"allowance","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
]

_MAX_UINT256 = 2**256 - 1


class UniswapV3Connector(BaseDexConnector):
    def __init__(self, chain: str, rpc_url: str):
        super().__init__(chain, rpc_url)
        self._quoter = None
        self._router = None
        self._decimals_cache: dict[str, int] = {}

    async def connect(self) -> None:
        await super().connect()
        self._quoter = self._w3.eth.contract(
            address=self._w3.to_checksum_address(QUOTER_V2),
            abi=QUOTER_ABI,
        )
        self._router = self._w3.eth.contract(
            address=self._w3.to_checksum_address(SWAP_ROUTER),
            abi=ROUTER_ABI,
        )
        logger.info(f"UniswapV3 connected on {self.chain}")

    async def _get_decimals(self, token: str) -> int:
        addr = self._w3.to_checksum_address(token)
        if addr not in self._decimals_cache:
            c = self._w3.eth.contract(address=addr, abi=ERC20_DECIMALS_ABI)
            self._decimals_cache[addr] = await c.functions.decimals().call()
        return self._decimals_cache[addr]

    async def get_quote(
        self,
        token_in: str,
        token_out: str,
        amount_in: Decimal,
        fee_tier: int = 3000,
    ) -> SwapQuote:
        if not self._quoter:
            await self.connect()

        dec_in  = await self._get_decimals(token_in)
        dec_out = await self._get_decimals(token_out)
        raw_in  = int(amount_in * Decimal(10 ** dec_in))

        try:
            result = await self._quoter.functions.quoteExactInputSingle({
                "tokenIn":           self._w3.to_checksum_address(token_in),
                "tokenOut":          self._w3.to_checksum_address(token_out),
                "amountIn":          raw_in,
                "fee":               fee_tier,
                "sqrtPriceLimitX96": 0,
            }).call()
            raw_out     = result[0]
            gas_est     = result[3]
            amount_out  = Decimal(raw_out) / Decimal(10 ** dec_out)

            # Spot price with no size (for price impact calc)
            spot_result = await self._quoter.functions.quoteExactInputSingle({
                "tokenIn":           self._w3.to_checksum_address(token_in),
                "tokenOut":          self._w3.to_checksum_address(token_out),
                "amountIn":          int(Decimal(10 ** dec_in)),   # 1 unit
                "fee":               fee_tier,
                "sqrtPriceLimitX96": 0,
            }).call()
            spot_price = Decimal(spot_result[0]) / Decimal(10 ** dec_out)
            exec_price = amount_out / amount_in if amount_in else Decimal(0)
            impact = abs(exec_price - spot_price) / spot_price * 100 if spot_price else Decimal(0)

            return SwapQuote(
                chain=self.chain,
                dex="uniswap_v3",
                token_in=token_in.lower(),
                token_out=token_out.lower(),
                amount_in=amount_in,
                amount_out=amount_out,
                price_impact_pct=impact,
                gas_estimate=int(gas_est),
                route=[token_in.lower(), token_out.lower()],
                fee_tier=fee_tier,
            )
        except Exception as e:
            logger.error(f"Quote failed ({token_in[:8]}→{token_out[:8]}): {e}")
            raise

    async def get_pool_price(
        self,
        token0: str,
        token1: str,
        fee_tier: int = 3000,
    ) -> Decimal:
        """Returns token1 per token0 (spot, tiny amount)."""
        dec0 = await self._get_decimals(token0)
        result = await self._quoter.functions.quoteExactInputSingle({
            "tokenIn":           self._w3.to_checksum_address(token0),
            "tokenOut":          self._w3.to_checksum_address(token1),
            "amountIn":          int(Decimal(10 ** dec0)),
            "fee":               fee_tier,
            "sqrtPriceLimitX96": 0,
        }).call()
        dec1 = await self._get_decimals(token1)
        return Decimal(result[0]) / Decimal(10 ** dec1)

    async def ensure_allowance(self, wallet, token: str, amount_raw: int) -> Optional[str]:
        """Check ERC20 allowance for SwapRouter02; approve max if insufficient. Returns tx_hash or None."""
        addr = self._w3.to_checksum_address(token)
        contract = self._w3.eth.contract(address=addr, abi=ERC20_ALLOWANCE_ABI)
        current = await contract.functions.allowance(wallet.address, SWAP_ROUTER).call()
        if current >= amount_raw:
            return None
        logger.info(f"Approving {token[:8]}… for SwapRouter02")
        tx_hash = await wallet.approve_token(token, SWAP_ROUTER, _MAX_UINT256)
        await wallet.wait_for_receipt(tx_hash)
        logger.info(f"Approval confirmed: {tx_hash}")
        return tx_hash

    async def swap(
        self,
        quote: SwapQuote,
        wallet,
        slippage_bps: int = 50,
        auto_approve: bool = True,
    ) -> SwapResult:
        if quote.is_expired:
            raise ValueError("Quote expired")

        dec_in = await self._get_decimals(quote.token_in)
        raw_in = int(quote.amount_in * Decimal(10 ** dec_in))

        if auto_approve:
            await self.ensure_allowance(wallet, quote.token_in, raw_in)

        dec_out = await self._get_decimals(quote.token_out)
        min_out = int(
            quote.amount_out * Decimal(10 ** dec_out) * (10000 - slippage_bps) / 10000
        )

        tx = await self._router.functions.exactInputSingle({
            "tokenIn":           self._w3.to_checksum_address(quote.token_in),
            "tokenOut":          self._w3.to_checksum_address(quote.token_out),
            "fee":               quote.fee_tier,
            "recipient":         wallet.address,
            "amountIn":          raw_in,
            "amountOutMinimum":  min_out,
            "sqrtPriceLimitX96": 0,
        }).build_transaction({"chainId": wallet.chain_id, "from": wallet.address})

        tx_hash = await wallet.sign_and_send(tx)
        receipt = await wallet.wait_for_receipt(tx_hash)

        gas_used = receipt.get("gasUsed", 0)
        gas_price_gwei = receipt.get("effectiveGasPrice", 0) / 1e9

        return SwapResult(
            tx_hash=tx_hash,
            chain=self.chain,
            dex="uniswap_v3",
            token_in=quote.token_in,
            token_out=quote.token_out,
            amount_in=quote.amount_in,
            amount_out=quote.amount_out,
            gas_used=gas_used,
            gas_price_gwei=gas_price_gwei,
            success=receipt.get("status", 0) == 1,
        )
