import { useState, useEffect } from 'react'
import Sparkline from '../components/Sparkline'
import EquityChart from '../components/EquityChart'
import { useLang } from '../i18n'

/* ── Metric card ──────────────────────────────────────────────────────────── */
function MetricCard({ label, value, sub, accentClass, icon, pnl }) {
  const isPos = pnl == null ? null : pnl >= 0
  return (
    <div className={`card card-glow fade-in ${accentClass || ''}`}
      style={{ padding: '20px 22px', position: 'relative', overflow: 'hidden' }}>
      {/* Subtle background glow */}
      <div style={{
        position: 'absolute', top: -30, right: -30, width: 100, height: 100,
        borderRadius: '50%', opacity: 0.04,
        background: accentClass?.includes('green') ? 'var(--green)'
          : accentClass?.includes('red') ? 'var(--red)'
          : accentClass?.includes('yellow') ? 'var(--yellow)'
          : 'var(--accent)',
        filter: 'blur(30px)',
        pointerEvents: 'none',
      }} />

      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 14 }}>
        <span className="label">{label}</span>
        <span style={{ fontSize: 20, opacity: 0.35, lineHeight: 1 }}>{icon}</span>
      </div>

      <div className="metric-lg" style={{
        marginBottom: 8,
        color: isPos === null ? 'var(--t1)' : isPos ? 'var(--green)' : 'var(--red)',
      }}>{value}</div>

      <div style={{ fontSize: 11, color: 'var(--t3)' }}>{sub}</div>
    </div>
  )
}

/* ── Price row ────────────────────────────────────────────────────────────── */
function PriceRow({ symbol, bnTicker, okxTicker, history, t }) {
  const last  = bnTicker?.last || okxTicker?.last || 0
  const bnBid = bnTicker?.bid, okxAsk = okxTicker?.ask
  const arb   = bnBid && okxAsk ? ((bnBid - okxAsk) / okxAsk * 10000) : null
  const fmt   = n => n ? Number(n).toLocaleString('en', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : '—'

  return (
    <div style={{
      display: 'flex', alignItems: 'center', padding: '13px 20px',
      borderBottom: '1px solid rgba(23,34,54,0.7)',
      transition: 'background 0.12s', gap: 16,
      cursor: 'default',
    }}
      onMouseEnter={e => e.currentTarget.style.background = 'rgba(59,123,255,0.03)'}
      onMouseLeave={e => e.currentTarget.style.background = 'none'}
    >
      {/* Symbol */}
      <div style={{ width: 100, flexShrink: 0 }}>
        <div style={{ fontWeight: 700, fontSize: 13, color: 'var(--t1)' }}>{symbol}</div>
        <div style={{ fontSize: 10, color: 'var(--t3)', marginTop: 2 }}>PERP · USDT</div>
      </div>

      {/* Last price */}
      <div style={{ flex: 1, minWidth: 0 }}>
        <span className="num" style={{ fontSize: 18, fontWeight: 700, color: 'var(--t1)' }}>
          {fmt(last)}
        </span>
      </div>

      {/* Sparkline */}
      <Sparkline data={history} width={84} height={28} />

      {/* Bid/Ask */}
      <div style={{ display: 'flex', gap: 20 }} className="hide-mobile">
        {[['BN', bnTicker], ['OKX', okxTicker]].map(([name, ticker]) => (
          <div key={name}>
            <div style={{ fontSize: 9, color: 'var(--t3)', marginBottom: 4 }}>{name}</div>
            <div className="num" style={{ fontSize: 11 }}>
              <span style={{ color: 'var(--green)' }}>{fmt(ticker?.bid)}</span>
              <span style={{ color: 'var(--t4)', margin: '0 4px' }}>/</span>
              <span style={{ color: 'var(--red)' }}>{fmt(ticker?.ask)}</span>
            </div>
          </div>
        ))}
      </div>

      {/* Arb badge */}
      {arb !== null && (
        <div style={{
          padding: '4px 10px', borderRadius: 6, fontSize: 11, fontWeight: 700,
          background: Math.abs(arb) > 3 ? 'var(--green-dim)' : 'rgba(36,53,80,0.4)',
          color: Math.abs(arb) > 3 ? 'var(--green)' : 'var(--t2)',
          border: `1px solid ${Math.abs(arb) > 3 ? 'rgba(0,217,163,0.2)' : 'var(--border)'}`,
          minWidth: 78, textAlign: 'center', flexShrink: 0,
        }}>
          {arb > 0 ? '▲' : '▼'} {Math.abs(arb).toFixed(1)} bps
        </div>
      )}
    </div>
  )
}

/* ── Regime summary strip ─────────────────────────────────────────────────── */
function RegimeSummary({ symbols, liveRegimes }) {
  const [regime, setRegime] = useState({})
  useEffect(() => {
    // Initial load from API; updates come via WS regime_update events
    const load = () => fetch('/api/regime').then(r => r.ok ? r.json() : {}).then(setRegime).catch(() => {})
    load()
  }, [])

  // Merge WS live updates on top of initial API snapshot
  const merged = { ...regime }
  if (liveRegimes) {
    for (const [sym, snap] of Object.entries(liveRegimes)) {
      merged[sym] = snap
    }
  }

  const items = Object.entries(merged).filter(([sym]) => symbols.includes(sym))
  if (!items.length) return null

  const cfg = { low: ['↓ LOW', 'var(--green)'], normal: ['→ NORMAL', 'var(--accent)'], high: ['↑ HIGH', 'var(--yellow)'], extreme: ['⚠ EXTREME', 'var(--red)'] }
  return (
    <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
      {items.map(([sym, snap]) => {
        const [label, color] = cfg[snap.regime] || ['?', 'var(--t3)']
        return (
          <div key={sym} style={{
            display: 'flex', alignItems: 'center', gap: 8, padding: '7px 12px',
            borderRadius: 8, background: 'var(--surface)', border: `1px solid ${color}28`,
          }}>
            <span style={{ fontSize: 11, fontWeight: 700, color: 'var(--t1)' }}>{sym}</span>
            <span style={{ fontSize: 10, fontWeight: 700, color, padding: '1px 6px', borderRadius: 5, background: `${color}15` }}>{label}</span>
            <span style={{ fontSize: 10, color: 'var(--t3)' }}>
              vol <span className="num" style={{ color: 'var(--t2)' }}>{(snap.realized_vol_ann * 100).toFixed(1)}%</span>
            </span>
            <span style={{ fontSize: 10, color: 'var(--t3)' }}>
              size ×<span className="num" style={{ color: snap.pos_size_mult > 1 ? 'var(--green)' : snap.pos_size_mult < 1 ? 'var(--red)' : 'var(--t2)' }}>{snap.pos_size_mult}</span>
            </span>
          </div>
        )
      })}
    </div>
  )
}

/* ── Dashboard ────────────────────────────────────────────────────────────── */
export default function DashboardPage({ tickers, positions, orders, balances, risk, priceHistory, symbols, equityRefresh, regimes }) {
  const { t } = useLang()

  // Per-exchange USDT equity (futures + spot + flexible savings).
  // LDUSDT = Binance flexible savings, redeemable 1:1 to USDT — counted as equity.
  const exUsdt = (prefix) =>
    balances
      .filter(b => (b.asset === 'USDT' || b.asset === 'LDUSDT') && b.exchange?.startsWith(prefix))
      .reduce((s, b) => s + (b.free + b.locked), 0)

  const bnUsdt    = exUsdt('binance')
  const okxUsdt   = exUsdt('okx')
  const totalUsdt = bnUsdt + okxUsdt

  const totalPnl   = positions.reduce((s, p) => s + (p.unrealized_pnl || 0), 0)
  const openCount  = positions.length
  const orderCount = orders.length
  const pnl        = risk?.daily_pnl_usdt || 0
  const lossLimit  = risk?.limits?.max_daily_loss_usdt || 0
  const lossUsedPct = lossLimit > 0 ? Math.min(100, Math.abs(Math.min(0, pnl)) / lossLimit * 100) : 0
  const fmt = n => Number(n).toLocaleString('en', { minimumFractionDigits: 2, maximumFractionDigits: 2 })

  return (
    <div className="page">

      {/* Top row: total + per-exchange */}
      <div className="grid-3" style={{ marginBottom: 12 }}>
        <MetricCard
          label={t('total_equity_label')} icon="◈"
          value={`${fmt(totalUsdt)} USDT`}
          sub={t('equity_pos_sub', openCount)}
          accentClass="accent-blue"
        />
        <MetricCard
          label={t('bn_equity')} icon="🟡"
          value={`${fmt(bnUsdt)} USDT`}
          sub={t('bn_equity_sub', totalUsdt > 0 ? ((bnUsdt / totalUsdt) * 100).toFixed(0) : 0)}
          accentClass="accent-yellow"
        />
        <MetricCard
          label={t('okx_equity')} icon="🔵"
          value={`${fmt(okxUsdt)} USDT`}
          sub={t('okx_equity_sub', totalUsdt > 0 ? ((okxUsdt / totalUsdt) * 100).toFixed(0) : 0)}
          accentClass="accent-blue"
        />
      </div>

      {/* Second row: PnL + orders + risk gauge */}
      <div className="grid-3" style={{ marginBottom: 12 }}>
        <MetricCard
          label={t('daily_pnl_label')} icon="◎"
          value={`${pnl >= 0 ? '+' : ''}${fmt(pnl)} USDT`}
          sub={t('pnl_limit', fmt(lossLimit))}
          accentClass={pnl >= 0 ? 'accent-green' : 'accent-red'}
          pnl={pnl}
        />
        <MetricCard
          label={t('unrealized_pnl')} icon="⊞"
          value={`${totalPnl >= 0 ? '+' : ''}${fmt(totalPnl)} USDT`}
          sub={t('pos_sub', openCount)}
          accentClass={totalPnl >= 0 ? 'accent-green' : 'accent-red'}
          pnl={totalPnl}
        />
        <MetricCard
          label={t('open_orders_card')} icon="≡"
          value={orderCount}
          sub={t('orders_sub', risk?.limits?.max_open_orders || 10)}
          accentClass="accent-yellow"
        />
      </div>

      {/* Risk daily loss progress */}
      {lossLimit > 0 && (
        <div className="card" style={{ padding: '14px 20px', marginBottom: 12 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
            <span style={{ fontSize: 11, fontWeight: 700, color: 'var(--t2)', letterSpacing: '0.06em', textTransform: 'uppercase' }}>
              {t('daily_loss_risk')}
            </span>
            <span style={{ fontSize: 11, color: lossUsedPct > 80 ? 'var(--red)' : 'var(--t3)' }}>
              {pnl < 0 ? `-${fmt(Math.abs(pnl))}` : '0.00'} / {fmt(lossLimit)} USDT · {lossUsedPct.toFixed(0)}%
            </span>
          </div>
          <div style={{ height: 6, borderRadius: 3, background: 'var(--bg2)', overflow: 'hidden' }}>
            <div style={{
              height: '100%', borderRadius: 3,
              width: `${lossUsedPct}%`,
              background: lossUsedPct > 80 ? 'var(--red)' : lossUsedPct > 50 ? 'var(--yellow)' : 'var(--green)',
              transition: 'width 0.5s ease, background 0.3s',
            }} />
          </div>
          {risk?.halted && (
            <div style={{ marginTop: 8, fontSize: 11, color: 'var(--red)', fontWeight: 700 }}>
              {t('risk_halted_msg')}
            </div>
          )}
        </div>
      )}

      {/* Regime strip */}
      <RegimeSummary symbols={symbols} liveRegimes={regimes} />

      {/* Market prices */}
      <div className="card">
        <div className="card-header">
          <span className="section-title">{t('market_prices')}</span>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <span className="dot dot-green blink" />
            <span style={{ fontSize: 11, color: 'var(--t2)' }}>{t('realtime')}</span>
          </div>
        </div>
        {symbols.length === 0 ? (
          <div className="empty-state">
            <div className="empty-icon">📈</div>
            <div className="empty-title">{t('no_symbols_hint')}</div>
          </div>
        ) : symbols.map(s => (
          <PriceRow key={s} symbol={s}
            bnTicker={tickers[`binance:${s}`]}
            okxTicker={tickers[`okx:${s}`]}
            history={priceHistory[`binance:${s}`] || priceHistory[`okx:${s}`] || []}
            t={t}
          />
        ))}
      </div>

      {/* Equity curve */}
      <div className="card">
        <div className="card-header">
          <span className="section-title">{t('equity_curve')}</span>
          <span style={{ fontSize: 10, color: 'var(--t3)' }}>
            {t('equity_curve_sub')} · <span style={{ fontStyle: 'italic' }}>sampled every 60s from exchange REST</span>
          </span>
        </div>
        <div style={{ padding: '14px 20px 16px' }}>
          <EquityChart refreshSignal={equityRefresh} />
        </div>
      </div>

      {/* Positions preview */}
      {positions.length > 0 && (
        <div className="card">
          <div className="card-header">
            <span className="section-title">{t('open_positions')}</span>
            <span style={{ fontSize: 11, color: 'var(--t2)' }}>{positions.length} {t('active')}</span>
          </div>
          <div className="table-wrap">
            <table>
              <thead><tr>
                <th>{t('th_exchange')}</th><th>{t('th_symbol')}</th><th>{t('th_side')}</th>
                <th style={{ textAlign: 'right' }}>{t('th_size')}</th>
                <th style={{ textAlign: 'right' }}>{t('th_entry')}</th>
                <th style={{ textAlign: 'right' }}>{t('th_mark')}</th>
                <th style={{ textAlign: 'right' }}>{t('th_upnl')}</th>
                <th style={{ textAlign: 'right' }}>{t('th_leverage')}</th>
              </tr></thead>
              <tbody>
                {positions.map((p, i) => {
                  const pnlColor = p.unrealized_pnl >= 0 ? 'var(--green)' : 'var(--red)'
                  return (
                    <tr key={i}>
                      <td>
                        <span style={{
                          padding: '2px 7px', borderRadius: 4, fontSize: 10, fontWeight: 700,
                          background: p.exchange === 'binance' ? 'rgba(240,185,11,0.1)' : 'rgba(0,100,220,0.1)',
                          color: p.exchange === 'binance' ? '#f0b90b' : '#4488ee',
                        }}>{p.exchange === 'binance' ? 'BN' : 'OKX'}</span>
                      </td>
                      <td style={{ fontWeight: 600 }}>{p.symbol}</td>
                      <td><span className={`badge badge-${p.side}`}>{p.side?.toUpperCase()}</span></td>
                      <td className="num" style={{ textAlign: 'right' }}>{fmt(p.size)}</td>
                      <td className="num" style={{ textAlign: 'right', color: 'var(--t2)' }}>{fmt(p.entry_price)}</td>
                      <td className="num" style={{ textAlign: 'right' }}>{fmt(p.mark_price)}</td>
                      <td className="num" style={{ textAlign: 'right', color: pnlColor, fontWeight: 700 }}>
                        {p.unrealized_pnl >= 0 ? '+' : ''}{fmt(p.unrealized_pnl)}
                      </td>
                      <td style={{ textAlign: 'right', color: 'var(--t2)' }}>{p.leverage}x</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}
