"""One-off analysis: cross-exchange spread distribution from recorded ticks.

Replicates SpreadArbStrategy._evaluate_spread() math over historical ticks:
  spread_bn_over_okx = (bn.bid - okx.ask) / okx.ask * 1e4
  spread_okx_over_bn = (okx.bid - bn.ask) / bn.ask * 1e4
Aligns each OKX tick with the latest Binance tick within MAX_AGE_S.
"""
import sqlite3
import sys

DB = "data/trading_data.db"
MAX_AGE_S = 2.0
THRESHOLDS = [3, 5, 8, 10, 13, 15, 20]

def main():
    con = sqlite3.connect(DB)
    cur = con.cursor()
    for symbol in ("BTC-USDT", "ETH-USDT"):
        bn = cur.execute(
            "SELECT ts, bid, ask FROM ticks WHERE exchange='binance' AND symbol=? "
            "AND bid IS NOT NULL AND ask IS NOT NULL ORDER BY ts", (symbol,)
        ).fetchall()
        okx = cur.execute(
            "SELECT ts, bid, ask FROM ticks WHERE exchange='okx' AND symbol=? "
            "AND bid IS NOT NULL AND ask IS NOT NULL ORDER BY ts", (symbol,)
        ).fetchall()
        if not bn or not okx:
            print(f"{symbol}: no data")
            continue

        spreads = []
        i = 0
        for ts, obid, oask in okx:
            while i < len(bn) - 1 and bn[i + 1][0] <= ts:
                i += 1
            bts, bbid, bask = bn[i]
            if ts - bts > MAX_AGE_S or bts > ts:
                continue
            if not (obid and oask and bbid and bask):
                continue
            s1 = (bbid - oask) / oask * 10000
            s2 = (obid - bask) / bask * 10000
            spreads.append(max(s1, s2))

        if not spreads:
            print(f"{symbol}: no aligned pairs")
            continue
        spreads.sort()
        n = len(spreads)
        days = (okx[-1][0] - okx[0][0]) / 86400
        print(f"\n=== {symbol} ===  aligned pairs: {n}  span: {days:.1f}d")
        for p in (50, 90, 99, 99.9):
            print(f"  p{p}: {spreads[int(n * p / 100) - 1]:+.2f} bps")
        print(f"  max: {spreads[-1]:+.2f} bps")
        for th in THRESHOLDS:
            cnt = sum(1 for s in spreads if s >= th)
            print(f"  >= {th:>2} bps: {cnt:>6}  ({cnt / n * 100:.3f}%  ~{cnt / days:.1f}/day)")
    con.close()

if __name__ == "__main__":
    sys.exit(main())
