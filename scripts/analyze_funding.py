"""One-off edge check for FundingRateArbStrategy.scan_all.

Replicates the strategy's candidate scan + fee gate against LIVE public APIs:
  1. Binance: rank ALL USDT perps by |lastFundingRate|, filter by 24h quote volume.
  2. For the top N, fetch BOTH Binance and OKX funding rates (cross-exchange arb
     needs the symbol to exist on OKX too).
  3. Compute the per-8h funding-rate differential and test it against the round-trip
     fee gate (4 taker legs).

Edge question answered: how many liquid alts have a cross-exchange funding diff large
enough to clear the fee gate — i.e. does this strategy have anything to trade?
"""
import json
import ssl
import time
import urllib.request

# This host sits behind an SSL-intercepting proxy (self-signed chain). These are
# PUBLIC market endpoints, so for a one-off edge check we skip cert verification.
_SSL = ssl._create_unverified_context()

BINANCE_FUNDING = "https://fapi.binance.com/fapi/v1/premiumIndex"
BINANCE_TICKER24 = "https://fapi.binance.com/fapi/v1/ticker/24hr"
OKX_FUNDING = "https://www.okx.com/api/v5/public/funding-rate?instId={}"

# ── Fee gate params (FundingRateArbStrategy defaults) ─────────────────────────
TAKER_BPS = 4.0          # taker fee per leg, bps
LEGS = 4                 # open 2 + close 2
FEE_MULTIPLE = 1.5       # required margin over fees to enter
MIN_VOL_24H = 50e6       # liquidity filter (quote volume USDT)
TOP_N = 40               # scan depth

ROUNDTRIP_BPS = TAKER_BPS * LEGS                     # 16 bps absolute cost
ENTRY_GATE_8H_BPS = ROUNDTRIP_BPS * FEE_MULTIPLE     # 24 bps diff/8h to enter (min_hold=1 period)


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "funding-edge-check"})
    with urllib.request.urlopen(req, timeout=15, context=_SSL) as r:
        return json.loads(r.read().decode())


def main():
    print("Fetching Binance funding + volume ...")
    prem = _get(BINANCE_FUNDING)
    tickers = _get(BINANCE_TICKER24)
    volume = {t["symbol"]: float(t.get("quoteVolume") or 0) for t in tickers}

    ranked = []
    for item in prem:
        bsym = item.get("symbol", "")
        if not bsym.endswith("USDT"):
            continue
        if volume.get(bsym, 0.0) < MIN_VOL_24H:
            continue
        try:
            rate = float(item.get("lastFundingRate") or 0)
        except (TypeError, ValueError):
            continue
        if rate == 0.0:
            continue
        ranked.append((abs(rate), bsym, rate))
    ranked.sort(reverse=True)
    top = ranked[:TOP_N]

    print(f"Binance liquid USDT perps (vol≥${MIN_VOL_24H/1e6:.0f}M): {len(ranked)}; "
          f"checking top {len(top)} on OKX\n")
    print(f"Fee gate: round-trip {ROUNDTRIP_BPS:.0f}bps (4×{TAKER_BPS:.0f}bps); "
          f"entry needs diff ≥ {ENTRY_GATE_8H_BPS:.0f}bps/8h (×{FEE_MULTIPLE} margin)\n")

    hdr = f"{'SYMBOL':12} {'BN/8h':>9} {'OKX/8h':>9} {'DIFF/8h':>9} {'DIFF_ann':>9}  {'BN_vol':>8}  VERDICT"
    print(hdr)
    print("-" * len(hdr))

    n_okx = n_breakeven = n_gate = 0
    rows = []
    for absrate, bsym, bn_rate in top:
        base = bsym[:-4]
        okx_inst = f"{base}-USDT-SWAP"
        okx_rate = None
        try:
            d = _get(OKX_FUNDING.format(okx_inst))
            if d.get("code") == "0" and d.get("data"):
                okx_rate = float(d["data"][0]["fundingRate"])
        except Exception:
            okx_rate = None
        time.sleep(0.06)  # be gentle with OKX

        bn_bps = bn_rate * 10000
        if okx_rate is None:
            print(f"{base+'-USDT':12} {bn_bps:>8.2f}  {'(no OKX)':>9} {'—':>9} {'—':>9}  "
                  f"{volume[bsym]/1e6:>7.0f}M  OKX无合约,无法跨所")
            continue
        n_okx += 1
        okx_bps = okx_rate * 10000
        diff_bps = abs(bn_rate - okx_rate) * 10000          # per-8h
        diff_ann = abs(bn_rate - okx_rate) * 3 * 365 * 10000

        if diff_bps >= ENTRY_GATE_8H_BPS:
            verdict = "✅ 过进场门槛(单周期)"
            n_gate += 1
            n_breakeven += 1
        elif diff_bps >= ROUNDTRIP_BPS:
            need = ROUNDTRIP_BPS / diff_bps
            verdict = f"⚠️ 回本需{need:.1f}周期({need*8:.0f}h)持有"
            n_breakeven += 1
        else:
            need = ROUNDTRIP_BPS / diff_bps if diff_bps > 0 else 999
            verdict = f"❌ 差太小(回本需{need:.0f}周期/{need*8/24:.0f}天)"
        rows.append((diff_bps, base, bn_bps, okx_bps, diff_bps, diff_ann, volume[bsym], verdict))

    rows.sort(reverse=True)
    for _, base, bn_bps, okx_bps, diff_bps, diff_ann, vol, verdict in rows:
        print(f"{base+'-USDT':12} {bn_bps:>8.2f}  {okx_bps:>8.2f}  {diff_bps:>8.2f}  "
              f"{diff_ann:>8.0f}  {vol/1e6:>7.0f}M  {verdict}")

    print("\n" + "=" * 60)
    print(f"候选 {len(top)} 个 | OKX 也有合约 {n_okx} 个 | "
          f"持有可回本(diff≥{ROUNDTRIP_BPS:.0f}bps) {n_breakeven} 个 | "
          f"单周期过门槛(diff≥{ENTRY_GATE_8H_BPS:.0f}bps) {n_gate} 个")
    print("注意: 仅当前时点快照; Binance 部分 alt 为 4h/1h 结算(此处按 8h 近似),")
    print("      OKX 同理; 真实 edge 需多时点持续性,不能凭单点快照下结论。")


if __name__ == "__main__":
    main()
