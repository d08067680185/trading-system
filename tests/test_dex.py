"""Tests for the DEX / on-chain module:
  - GasManager EIP-1559 EMA math + estimate_gas fallback
  - EVMWallet sign_and_send (nonce handling, web3 v7 raw_transaction, gas-fee
    injection, 0x-prefixed hash, nonce resync on send failure)
  - wait_for_receipt TransactionNotFound polling
  - SwapQuote price/expiry helpers
  - UniswapV3Connector.get_quote price-impact math (mocked quoter)
  - DexCexArbStrategy gas-cost / net-profit decision logic

No network, no real chain — everything is stubbed.
"""
import asyncio
import time
from decimal import Decimal
from types import SimpleNamespace

import pytest
from hexbytes import HexBytes

from connectors.dex.base import SwapQuote
from connectors.onchain.gas import GasManager, GasFees
from connectors.onchain.wallet import EVMWallet


# ── Fake async web3 plumbing ─────────────────────────────────────────────────

class _FakeEth:
    def __init__(self, base_fee_wei=int(20e9), priority_wei=int(1.5e9),
                 chain_nonce=5, fail_send=False):
        self._base_fee = base_fee_wei
        self._priority = priority_wei
        self.chain_nonce = chain_nonce
        self.fail_send = fail_send
        self.sent = []
        self.estimate_calls = 0
        self._receipts = {}          # tx_hash hex -> receipt (or None until "mined")
        self.account = _FakeAccount()

    async def get_block(self, _):
        return {"baseFeePerGas": self._base_fee}

    @property
    def max_priority_fee(self):
        async def _f():
            return self._priority
        return _f()

    async def estimate_gas(self, tx):
        self.estimate_calls += 1
        return 21000

    async def get_transaction_count(self, _addr):
        return self.chain_nonce

    async def send_raw_transaction(self, raw):
        if self.fail_send:
            raise ValueError("nonce too low")
        self.sent.append(raw)
        return HexBytes(b"\xab" * 32)

    async def get_transaction_receipt(self, tx_hash):
        from web3.exceptions import TransactionNotFound
        key = str(tx_hash)
        if key not in self._receipts or self._receipts[key] is None:
            raise TransactionNotFound(f"{tx_hash} not found")
        return self._receipts[key]


class _FakeSigned:
    """Mimics web3 v7 SignedTransaction (has raw_transaction, not rawTransaction)."""
    def __init__(self):
        self.raw_transaction = HexBytes(b"\x01\x02\x03")


class _FakeAccount:
    def __init__(self):
        self.last_tx = None

    def from_key(self, _k):
        return SimpleNamespace(address="0x000000000000000000000000000000000000dEaD")

    def sign_transaction(self, tx, _key):
        self.last_tx = dict(tx)
        return _FakeSigned()


class _FakeW3:
    def __init__(self, eth):
        self.eth = eth

    @staticmethod
    def to_checksum_address(a):
        return a


def _wallet(eth):
    w = EVMWallet(private_key="0x" + "1" * 64, rpc_url="http://x", chain_id=42161)
    w._w3 = _FakeW3(eth)
    w.address = "0x000000000000000000000000000000000000dEaD"
    w._nonce = 5
    return w


# ── GasManager ───────────────────────────────────────────────────────────────

def test_gasfees_to_wei():
    f = GasFees(base_fee_gwei=20, max_priority_fee_gwei=2, max_fee_gwei=24, estimated_gwei=24)
    assert f.to_wei() == {"maxFeePerGas": int(24e9), "maxPriorityFeePerGas": int(2e9)}


def test_gas_manager_refresh_computes_eip1559():
    eth = _FakeEth(base_fee_wei=int(20e9), priority_wei=int(1.5e9))
    gm = GasManager(_FakeW3(eth), priority_multiplier=2.0, base_multiplier=1.1)
    fees = asyncio.run(_run_refresh(gm))
    # first refresh seeds EMA = raw base = 20 gwei
    assert fees.base_fee_gwei == pytest.approx(20.0)
    # priority 1.5 * 2.0 = 3.0
    assert fees.max_priority_fee_gwei == pytest.approx(3.0)
    # max = 20*1.1 + 3.0 = 25.0
    assert fees.max_fee_gwei == pytest.approx(25.0)


async def _run_refresh(gm):
    await gm._refresh()
    return await gm.get_fees()


def test_gas_manager_ema_smooths_spike():
    eth = _FakeEth(base_fee_wei=int(20e9))
    gm = GasManager(_FakeW3(eth), priority_multiplier=1.0, base_multiplier=1.0)

    async def run():
        await gm._refresh()              # seed EMA at 20
        eth._base_fee = int(120e9)       # spike to 120
        await gm._refresh()              # EMA = 0.3*120 + 0.7*20 = 50
        return (await gm.get_fees()).base_fee_gwei

    assert asyncio.run(run()) == pytest.approx(50.0)


def test_gas_manager_estimate_gas_fallback():
    class _BoomEth:
        async def estimate_gas(self, tx):
            raise RuntimeError("revert")
    gm = GasManager(SimpleNamespace(eth=_BoomEth()))
    assert asyncio.run(gm.estimate_gas({})) == 200_000


# ── EVMWallet.sign_and_send ──────────────────────────────────────────────────

def test_sign_and_send_increments_nonce_and_returns_0x_hash():
    eth = _FakeEth()
    w = _wallet(eth)
    tx_hash = asyncio.run(w.sign_and_send({"to": "0xabc", "value": 0,
                                           "maxFeePerGas": 1, "maxPriorityFeePerGas": 1}))
    assert tx_hash.startswith("0x")
    assert len(tx_hash) == 66                      # 0x + 64 hex chars
    assert w._nonce == 6                           # incremented after success
    assert eth.account.last_tx["nonce"] == 5       # signed with the pre-increment nonce


def test_sign_and_send_uses_v7_raw_transaction_attr():
    # _FakeSigned only exposes raw_transaction (v7); send must not crash.
    eth = _FakeEth()
    w = _wallet(eth)
    asyncio.run(w.sign_and_send({"to": "0xabc", "maxFeePerGas": 1, "maxPriorityFeePerGas": 1}))
    assert len(eth.sent) == 1
    assert eth.sent[0] == HexBytes(b"\x01\x02\x03")


def test_sign_and_send_injects_gas_manager_fees():
    eth = _FakeEth(base_fee_wei=int(30e9), priority_wei=int(2e9))
    w = _wallet(eth)
    gm = GasManager(_FakeW3(eth), priority_multiplier=1.0, base_multiplier=1.0)
    asyncio.run(gm._refresh())
    w.set_gas_manager(gm)
    asyncio.run(w.sign_and_send({"to": "0xabc"}))  # no fee fields → manager fills them
    signed_tx = eth.account.last_tx
    assert signed_tx["maxFeePerGas"] == int(32e9)        # 30*1.0 + 2
    assert signed_tx["maxPriorityFeePerGas"] == int(2e9)


def test_sign_and_send_does_not_override_explicit_fees():
    eth = _FakeEth()
    w = _wallet(eth)
    gm = GasManager(_FakeW3(eth))
    asyncio.run(gm._refresh())
    w.set_gas_manager(gm)
    asyncio.run(w.sign_and_send({"to": "0xabc", "maxFeePerGas": 999, "maxPriorityFeePerGas": 7}))
    assert eth.account.last_tx["maxFeePerGas"] == 999     # caller value preserved


def test_sign_and_send_estimates_gas_when_absent():
    eth = _FakeEth()
    w = _wallet(eth)
    asyncio.run(w.sign_and_send({"to": "0xabc", "maxFeePerGas": 1, "maxPriorityFeePerGas": 1}))
    assert eth.estimate_calls == 1
    assert eth.account.last_tx["gas"] == 21000


def test_sign_and_send_resyncs_nonce_on_failure():
    eth = _FakeEth(chain_nonce=42, fail_send=True)
    w = _wallet(eth)              # local nonce starts at 5
    with pytest.raises(ValueError):
        asyncio.run(w.sign_and_send({"to": "0xabc", "maxFeePerGas": 1, "maxPriorityFeePerGas": 1}))
    assert w._nonce == 42        # resynced from chain so the wallet isn't wedged


# ── wait_for_receipt ─────────────────────────────────────────────────────────

def test_wait_for_receipt_polls_until_mined():
    eth = _FakeEth()
    w = _wallet(eth)
    h = "0x" + "ab" * 32

    async def run():
        # becomes available on the 2nd poll
        async def arm():
            await asyncio.sleep(0.01)
            eth._receipts[h] = {"status": 1, "gasUsed": 21000}
        asyncio.create_task(arm())
        return await w.wait_for_receipt(h, timeout=5)

    receipt = asyncio.run(run())
    assert receipt["status"] == 1


def test_wait_for_receipt_times_out():
    eth = _FakeEth()
    w = _wallet(eth)
    with pytest.raises(TimeoutError):
        asyncio.run(w.wait_for_receipt("0xdead", timeout=0))


# ── SwapQuote helpers ────────────────────────────────────────────────────────

def _quote(**kw):
    base = dict(chain="arbitrum", dex="uniswap_v3", token_in="0xin", token_out="0xout",
                amount_in=Decimal("300"), amount_out=Decimal("0.1"),
                price_impact_pct=Decimal("0.05"), gas_estimate=120000, route=["0xin", "0xout"])
    base.update(kw)
    return SwapQuote(**base)


def test_swapquote_effective_price():
    q = _quote(amount_in=Decimal("300"), amount_out=Decimal("0.1"))
    assert q.effective_price == Decimal("0.1") / Decimal("300")


def test_swapquote_zero_amount_in_safe():
    q = _quote(amount_in=Decimal("0"))
    assert q.effective_price == Decimal("0")


def test_swapquote_expiry():
    q = _quote(valid_until=time.time() - 1)
    assert q.is_expired is True
    q2 = _quote(valid_until=time.time() + 60)
    assert q2.is_expired is False


# ── UniswapV3Connector.get_quote price-impact math ───────────────────────────

class _FakeCall:
    def __init__(self, result):
        self._result = result

    async def call(self):
        return self._result


class _FakeQuoter:
    """Returns a smaller per-unit rate for the big trade than for 1 unit,
    so price impact comes out positive."""
    def __init__(self):
        self.functions = self

    def quoteExactInputSingle(self, params):
        amount_in = params["amountIn"]
        # 1 unit (1e6 raw for 6-dec stable) → spot ~ 0.0005 out/unit
        # big trade (300e6) → slightly worse rate
        if amount_in <= 10 ** 6:
            return _FakeCall([int(0.0005 * 1e18), 0, 0, 50000])     # amountOut raw (18 dec)
        return _FakeCall([int(0.1485 * 1e18), 0, 0, 120000])        # 300 in → 0.1485 out


def test_uniswap_get_quote_price_impact():
    from connectors.dex.uniswap_v3 import UniswapV3Connector
    conn = UniswapV3Connector(chain="arbitrum", rpc_url="http://x")
    conn._w3 = _FakeW3(_FakeEth())
    conn._quoter = _FakeQuoter()

    async def fake_dec(token):
        return 6 if "in" in token else 18
    conn._get_decimals = fake_dec

    q = asyncio.run(conn.get_quote("0xtokenin", "0xtokenout", Decimal("300"), 500))
    # big trade: 0.1485 out for 300 in → 0.000495 per unit
    assert q.amount_out == Decimal("0.1485")
    assert q.gas_estimate == 120000
    # spot 0.0005 vs exec 0.000495 → ~1% impact
    assert q.price_impact_pct > 0
    assert q.price_impact_pct == pytest.approx(Decimal("1.0"), abs=Decimal("0.2"))


# ── DexCexArbStrategy decision logic ─────────────────────────────────────────

def _arb_strategy():
    from strategies.dex_arb import DexCexArbStrategy
    return DexCexArbStrategy("dex_test", {
        "symbol": "ETH-USDT", "token_in": "0xin", "token_out": "0xout",
        "chain": "arbitrum", "amount_usdt": 300.0, "min_profit_bps": 15.0,
        "fee_tier": 500, "gas_usdt_budget": 5.0,
    })


def test_arb_skips_when_gas_exceeds_budget():
    from core.types import Exchange, Ticker
    strat = _arb_strategy()

    # DEX quote with huge gas estimate → gas cost blows the budget → no execute.
    async def fake_quote(ti, to, amt, fee):
        return _quote(amount_in=Decimal("300"), amount_out=Decimal("0.1"),
                      gas_estimate=50_000_000)
    strat._dex_connector = SimpleNamespace(get_quote=fake_quote)
    strat._wallet = SimpleNamespace(address="0x")

    executed = []
    async def fake_exec(**kw):
        executed.append(kw)
    strat._execute_arb = fake_exec

    # gas manager → 100 gwei, eth ~ 3000 → 50M gas * 100gwei = 5 ETH = huge
    strat._gas_manager = SimpleNamespace(
        get_fees=lambda: _coro(GasFees(100, 5, 105, 105)))

    ticker = Ticker(exchange=Exchange.BINANCE, symbol="ETH-USDT",
                    bid=Decimal("3000"), ask=Decimal("3002"),
                    last=Decimal("3001"), volume_24h=Decimal("1000"), timestamp=time.time())
    asyncio.run(strat._do_evaluate(ticker))
    assert executed == []          # gas gate blocked it


def test_arb_executes_when_profitable():
    from core.types import Exchange, Ticker
    strat = _arb_strategy()

    # DEX price much cheaper than CEX → profitable buy-on-dex.
    # amount_out 0.105 ETH for 300 USDT → dex price ≈ 2857 USDT/ETH vs CEX 3001.
    async def fake_quote(ti, to, amt, fee):
        return _quote(amount_in=Decimal("300"), amount_out=Decimal("0.105"),
                      gas_estimate=120000)
    strat._dex_connector = SimpleNamespace(get_quote=fake_quote)
    strat._wallet = SimpleNamespace(address="0x")
    strat._gas_manager = SimpleNamespace(
        get_fees=lambda: _coro(GasFees(0.1, 0.01, 0.11, 0.11)))

    executed = []
    async def fake_exec(**kw):
        executed.append(kw)
    strat._execute_arb = fake_exec

    ticker = Ticker(exchange=Exchange.BINANCE, symbol="ETH-USDT",
                    bid=Decimal("3000"), ask=Decimal("3002"),
                    last=Decimal("3001"), volume_24h=Decimal("1000"), timestamp=time.time())
    asyncio.run(strat._do_evaluate(ticker))
    assert len(executed) == 1
    assert executed[0]["dex_side"] == "buy"


async def _coro(v):
    return v
