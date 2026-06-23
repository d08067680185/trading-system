"""Quick connectivity test — checks REST auth + WebSocket for both exchanges."""
import asyncio, sys, time
sys.path.insert(0, ".")

from config.manager import load_config
from core.types import Exchange, MarketType
from connectors.binance import BinanceConnector
from connectors.okx import OKXConnector

GREEN = "\033[92m"; RED = "\033[91m"; YELLOW = "\033[93m"; RESET = "\033[0m"
ok  = lambda s: print(f"  {GREEN}✓{RESET} {s}")
err = lambda s: print(f"  {RED}✗{RESET} {s}")
inf = lambda s: print(f"  {YELLOW}·{RESET} {s}")

async def test_binance(cfg):
    print(f"\n{'─'*40}\n Binance Futures\n{'─'*40}")
    c = BinanceConnector(cfg.api_key, cfg.secret, cfg.market_type, cfg.testnet)
    q = asyncio.Queue()
    c.set_event_queue(q)

    # REST connect
    try:
        await c.connect()
        ok("REST connected, listen key obtained")
    except Exception as e:
        err(f"connect() failed: {e}")
        return False

    # Balances
    try:
        bals = await c.get_balances()
        usdt = next((b for b in bals if b.asset == "USDT"), None)
        if usdt:
            ok(f"Balance: {float(usdt.free):.2f} USDT free / {float(usdt.total):.2f} total")
        else:
            inf("No USDT balance found")
    except Exception as e:
        err(f"get_balances() failed: {e}")

    # Positions
    try:
        pos = await c.get_positions()
        open_pos = [p for p in pos if float(p.size) != 0]
        if open_pos:
            inf(f"Open positions: {len(open_pos)}")
            for p in open_pos:
                inf(f"  {p.symbol} {p.side.value} {float(p.size)} @ {float(p.entry_price)}")
        else:
            ok("No open positions")
    except Exception as e:
        err(f"get_positions() failed: {e}")

    # WebSocket ticker (subscribe + wait 3s)
    try:
        await c.subscribe_ticker("BTC-USDT")
        deadline = time.time() + 10
        got_tick = False
        while time.time() < deadline:
            try:
                ev = q.get_nowait()
                if hasattr(ev, "ticker"):
                    ok(f"WebSocket tick: BTC-USDT bid={float(ev.ticker.bid):.2f} ask={float(ev.ticker.ask):.2f}")
                    got_tick = True
                    break
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.2)
        if not got_tick:
            err("WebSocket: no ticker received within 10s")
    except Exception as e:
        err(f"WebSocket subscribe failed: {e}")

    await c.disconnect()
    ok("Disconnected cleanly")
    return True


async def test_okx(cfg):
    print(f"\n{'─'*40}\n OKX Swap\n{'─'*40}")
    c = OKXConnector(cfg.api_key, cfg.secret, cfg.passphrase, cfg.market_type, cfg.testnet)
    q = asyncio.Queue()
    c.set_event_queue(q)

    try:
        await c.connect()
        ok("REST + WebSocket connected")
    except Exception as e:
        err(f"connect() failed: {e}")
        return False

    try:
        bals = await c.get_balances()
        usdt = next((b for b in bals if b.asset == "USDT"), None)
        if usdt:
            ok(f"Balance: {float(usdt.free):.2f} USDT free / {float(usdt.total):.2f} total")
        else:
            inf("No USDT balance found")
    except Exception as e:
        err(f"get_balances() failed: {e}")

    try:
        pos = await c.get_positions()
        open_pos = [p for p in pos if float(p.size) != 0]
        if open_pos:
            inf(f"Open positions: {len(open_pos)}")
            for p in open_pos:
                inf(f"  {p.symbol} {p.side.value} {float(p.size)} @ {float(p.entry_price)}")
        else:
            ok("No open positions")
    except Exception as e:
        err(f"get_positions() failed: {e}")

    try:
        await c.subscribe_ticker("BTC-USDT")
        deadline = time.time() + 5
        got_tick = False
        while time.time() < deadline:
            try:
                ev = q.get_nowait()
                if hasattr(ev, "ticker"):
                    ok(f"WebSocket tick: BTC-USDT bid={float(ev.ticker.bid):.2f} ask={float(ev.ticker.ask):.2f}")
                    got_tick = True
                    break
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.1)
        if not got_tick:
            err("WebSocket: no ticker received within 5s")
    except Exception as e:
        err(f"WebSocket subscribe failed: {e}")

    await c.disconnect()
    ok("Disconnected cleanly")
    return True


async def main():
    print("Loading config…")
    config = load_config("config.yaml")

    results = {}
    if Exchange.BINANCE in config.exchanges:
        results["binance"] = await test_binance(config.exchanges[Exchange.BINANCE])
    if Exchange.OKX in config.exchanges:
        results["okx"] = await test_okx(config.exchanges[Exchange.OKX])

    print(f"\n{'═'*40}")
    print(" Summary")
    print(f"{'═'*40}")
    all_ok = True
    for ex, ok_flag in results.items():
        status = f"{GREEN}PASS{RESET}" if ok_flag else f"{RED}FAIL{RESET}"
        print(f"  {ex:10s}  {status}")
        if not ok_flag:
            all_ok = False
    print()
    sys.exit(0 if all_ok else 1)


asyncio.run(main())
