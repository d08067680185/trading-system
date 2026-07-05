"""SpreadScanner: ingest/report logic on captured-payload stubs (no network)."""
import time

from signals.spread_scanner import SpreadScanner


def _bn(symbol, bid, ask):
    return {"symbol": symbol, "bidPrice": str(bid), "askPrice": str(ask)}


def _okx(inst, bid, ask, vol=1_000_000):
    return {"instId": inst, "bidPx": str(bid), "askPx": str(ask), "volCcy24h": str(vol)}


def test_spread_computed_for_intersection():
    sc = SpreadScanner(target_bps=8.0, min_vol24h_usdt=100_000)
    # XYZ: binance bid 100.2 vs okx ask 100.0 → ~20 bps
    sc.ingest(
        [_bn("XYZUSDT", 100.2, 100.3), _bn("ONLYBNUSDT", 1, 1.01)],
        {"data": [_okx("XYZ-USDT", 99.9, 100.0), _okx("ONLYOKX-USDT", 1, 1.01)]},
    )
    rep = sc.report()
    syms = {r["symbol"] for r in rep["symbols"]}
    assert syms == {"XYZ-USDT"}          # intersection only
    row = rep["symbols"][0]
    assert 19 < row["last_bps"] < 21
    assert row["hits"] == 1              # 20 bps >= 8 bps target


def test_low_volume_and_stables_excluded():
    sc = SpreadScanner(min_vol24h_usdt=500_000)
    sc.ingest(
        [_bn("THINUSDT", 10.1, 10.2), _bn("USDCUSDT", 1.001, 1.002)],
        {"data": [_okx("THIN-USDT", 10.0, 10.05, vol=1_000),      # below floor
                  _okx("USDC-USDT", 0.999, 1.0)]},                # stable pair
    )
    assert sc.report()["symbols"] == []


def test_hits_accumulate_and_ranking():
    sc = SpreadScanner(target_bps=8.0, min_vol24h_usdt=0)
    for _ in range(3):
        sc.ingest([_bn("AAAUSDT", 100.2, 100.3), _bn("BBBUSDT", 100.01, 100.02)],
                  {"data": [_okx("AAA-USDT", 99.9, 100.0), _okx("BBB-USDT", 100.0, 100.01)]})
    rep = sc.report()
    assert rep["symbols"][0]["symbol"] == "AAA-USDT"   # 3 hits ranks first
    assert rep["symbols"][0]["hits"] == 3
    assert rep["symbols"][0]["samples"] == 3
    bbb = next(r for r in rep["symbols"] if r["symbol"] == "BBB-USDT")
    assert bbb["hits"] == 0              # ~0 bps spread never hits


def test_window_trims_old_samples():
    sc = SpreadScanner(target_bps=8.0, window_s=1.0, min_vol24h_usdt=0)
    sc.ingest([_bn("AAAUSDT", 100.2, 100.3)], {"data": [_okx("AAA-USDT", 99.9, 100.0)]})
    time.sleep(1.1)
    sc.ingest([_bn("AAAUSDT", 100.2, 100.3)], {"data": [_okx("AAA-USDT", 99.9, 100.0)]})
    rep = sc.report()
    assert rep["symbols"][0]["samples"] == 1   # old sample trimmed


def test_ticker_collision_blacklisted():
    """Same ticker, different asset (e.g. AI-USDT ~4000bps apart) must never
    surface as an arb candidate — permanently blacklisted on first sight."""
    sc = SpreadScanner(min_vol24h_usdt=0, max_sane_bps=300)
    sc.ingest([_bn("AIUSDT", 0.50, 0.51)],
              {"data": [_okx("AI-USDT", 0.10, 0.11)]})   # ~35000 bps apart
    rep = sc.report()
    assert rep["symbols"] == []
    assert "AI-USDT" in rep["blacklisted"]
    # stays excluded even if a later sample looks sane
    sc.ingest([_bn("AIUSDT", 0.100, 0.101)],
              {"data": [_okx("AI-USDT", 0.100, 0.101)]})
    assert sc.report()["symbols"] == []
