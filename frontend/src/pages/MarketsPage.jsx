import { useState, useEffect, Fragment } from 'react'
import Sparkline from '../components/Sparkline'
import KlineChart from '../components/KlineChart'
import { useLang } from '../i18n'
import { Loading, PageHeader } from '../components/ui'

function authHeaders() { const k = localStorage.getItem('trading_api_key') || ''; return k ? { 'X-API-Key': k } : {} }

function ExchangeBlock({ name, ticker, t }) {
  if (!ticker) return (
    <div style={{ flex: 1, background: 'var(--surface)', borderRadius: 8, padding: 14 }}>
      <div style={{ fontSize: 9, fontWeight: 700, letterSpacing: '0.1em', textTransform: 'uppercase',
                    color: 'var(--t3)', marginBottom: 8 }}>{name}</div>
      <div style={{ color: 'var(--t3)', fontSize: 12 }}>{t('connecting')}</div>
    </div>
  )
  const fmt = n => Number(n).toLocaleString('en', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  return (
    <div style={{ flex: 1, background: 'var(--surface)', borderRadius: 8, padding: 14 }}>
      <div style={{ fontSize: 9, fontWeight: 700, letterSpacing: '0.1em', textTransform: 'uppercase',
                    color: 'var(--accent)', marginBottom: 10 }}>{name}</div>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '10px 0' }}>
        {[
          ['Bid', fmt(ticker.bid), 'var(--green)'],
          ['Ask', fmt(ticker.ask), 'var(--red)'],
          ['Last', fmt(ticker.last), 'var(--t1)'],
          ['Spread', `${Number(ticker.spread_bps || 0).toFixed(2)} bps`, 'var(--t2)'],
        ].map(([l, v, c]) => (
          <div key={l}>
            <div style={{ fontSize: 9, color: 'var(--t3)', marginBottom: 3 }}>{l}</div>
            <div className="num" style={{ fontSize: 12, fontWeight: 600, color: c }}>{v}</div>
          </div>
        ))}
      </div>
    </div>
  )
}

// Candlestick icon button
function KlineBtn({ open, onClick, label }) {
  return (
    <button onClick={onClick} title={label} style={{
      display: 'flex', alignItems: 'center', gap: 5,
      padding: '5px 12px', borderRadius: 7,
      border: `1px solid ${open ? 'rgba(59,123,255,0.4)' : 'var(--border)'}`,
      background: open ? 'var(--accent-dim)' : 'var(--surface)',
      color: open ? 'var(--accent)' : 'var(--t3)',
      fontSize: 11, fontWeight: 600, cursor: 'pointer',
      transition: 'all 0.15s',
    }}>
      <svg viewBox="0 0 16 16" fill="currentColor" width="13" height="13">
        <rect x="2"  y="4"  width="3" height="7" rx="0.5"/>
        <line x1="3.5"  y1="2"  x2="3.5"  y2="4"  stroke="currentColor" strokeWidth="1.2"/>
        <line x1="3.5"  y1="11" x2="3.5"  y2="13" stroke="currentColor" strokeWidth="1.2"/>
        <rect x="6.5" y="6"  width="3" height="5" rx="0.5"/>
        <line x1="8"    y1="4"  x2="8"    y2="6"  stroke="currentColor" strokeWidth="1.2"/>
        <line x1="8"    y1="11" x2="8"    y2="13" stroke="currentColor" strokeWidth="1.2"/>
        <rect x="11"  y="3"  width="3" height="8" rx="0.5"/>
        <line x1="12.5" y1="1"  x2="12.5" y2="3"  stroke="currentColor" strokeWidth="1.2"/>
        <line x1="12.5" y1="11" x2="12.5" y2="14" stroke="currentColor" strokeWidth="1.2"/>
      </svg>
      {label}
    </button>
  )
}

function SymbolCard({ symbol, bnTicker, okxTicker, history, tickers, t }) {
  const [chartOpen, setChartOpen]     = useState(false)
  const [chartExchange, setChartEx]  = useState('binance')

  const bnBid = bnTicker?.bid, okxAsk = okxTicker?.ask
  const okxBid = okxTicker?.bid, bnAsk = bnTicker?.ask
  const arb1 = bnBid && okxAsk ? ((bnBid - okxAsk) / okxAsk * 10000) : null
  const arb2 = okxBid && bnAsk ? ((okxBid - bnAsk) / bnAsk * 10000) : null
  const last = bnTicker?.last || okxTicker?.last
  const fmt = n => n ? Number(n).toLocaleString('en', { minimumFractionDigits: 2 }) : '—'

  const liveTicker = chartExchange === 'binance' ? bnTicker : okxTicker

  return (
    <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
      {/* Header row */}
      <div style={{
        padding: '14px 20px', display: 'flex', alignItems: 'center', gap: 16,
        borderBottom: chartOpen ? 'none' : '1px solid var(--border)',
        background: 'linear-gradient(90deg, rgba(59,123,255,0.06) 0%, transparent 60%)',
      }}>
        <div>
          <div style={{ fontWeight: 700, fontSize: 16, color: 'var(--t1)', marginBottom: 2 }}>{symbol}</div>
          <div style={{ fontSize: 10, color: 'var(--t3)' }}>{t('perpetual')}</div>
        </div>
        <div style={{ flex: 1 }}>
          <span className="num" style={{ fontSize: 24, fontWeight: 700, color: 'var(--t1)' }}>{fmt(last)}</span>
        </div>
        <Sparkline data={history} width={100} height={36} />
        {arb1 !== null && (
          <div style={{ textAlign: 'right' }}>
            {[['BN→OKX', arb1], ['OKX→BN', arb2]].map(([label, val]) => val !== null && (
              <div key={label} style={{
                fontSize: 11, fontWeight: 700, padding: '3px 10px', borderRadius: 6, marginBottom: 4,
                background: Math.abs(val) > 3 ? 'var(--green-dim)' : 'rgba(36,53,80,0.6)',
                color: Math.abs(val) > 3 ? 'var(--green)' : 'var(--t2)',
                border: `1px solid ${Math.abs(val) > 3 ? 'rgba(0,217,163,0.2)' : 'var(--border)'}`,
              }}>
                {label}: {val > 0 ? '+' : ''}{Number(val).toFixed(1)} bps
              </div>
            ))}
          </div>
        )}
        <KlineBtn open={chartOpen} onClick={() => setChartOpen(o => !o)} label={t('kline_btn')} />
      </div>

      {/* K-line chart panel */}
      {chartOpen && (
        <>
          {/* Exchange selector */}
          <div style={{
            display: 'flex', alignItems: 'center', gap: 8,
            padding: '8px 20px', background: 'var(--bg2)', borderBottom: '1px solid var(--border)',
          }}>
            <span style={{ fontSize: 10, color: 'var(--t4)', fontWeight: 700, textTransform: 'uppercase', marginRight: 4 }}>
              {t('th_exchange')}
            </span>
            {[['binance', t('bn_futures')], ['okx', t('okx_swap')]].map(([ex, label]) => (
              <button key={ex} onClick={() => setChartEx(ex)} style={{
                padding: '3px 12px', borderRadius: 5, fontSize: 11, fontWeight: 600,
                cursor: 'pointer', border: 'none',
                background: chartExchange === ex ? 'rgba(59,123,255,0.18)' : 'transparent',
                color: chartExchange === ex ? 'var(--accent)' : 'var(--t3)',
                transition: 'all 0.12s',
              }}>{label}</button>
            ))}
          </div>

          <KlineChart
            key={`${symbol}-${chartExchange}`}
            exchange={chartExchange}
            symbol={symbol}
            ticker={liveTicker}
            height={460}
          />
          <DepthChart exchange={chartExchange} symbol={symbol} />
        </>
      )}

      {/* Ticker detail blocks */}
      <div style={{ display: 'flex', gap: 12, padding: 16 }}>
        <ExchangeBlock name={t('bn_futures')} ticker={bnTicker} t={t} />
        <ExchangeBlock name={t('okx_swap')}   ticker={okxTicker} t={t} />
      </div>
    </div>
  )
}

function FundingRates({ symbols, t }) {
  const [rates, setRates] = useState([])
  useEffect(() => {
    const load = () => fetch('/api/funding-rates').then(r => r.json()).then(d => setRates(Array.isArray(d) ? d : [])).catch(() => {})
    load()
    const iv = setInterval(load, 60000)
    return () => clearInterval(iv)
  }, [])

  const relevant = rates.filter(r => !r.error && symbols.some(s => r.symbol === s))
  if (relevant.length === 0) return null

  return (
    <div className="card">
      <div className="card-header">
        <span className="section-title">{t('funding_rates')}</span>
        <span style={{ fontSize: 11, color: 'var(--t3)' }}>{t('funding_rates_sub')}</span>
      </div>
      <div style={{ padding: '10px 20px 14px' }}>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))', gap: 12 }}>
          {relevant.map((r, i) => {
            const rate = r.funding_rate || 0
            const annPct = r.annualized_pct || rate * 3 * 365 * 100
            const isPos = rate >= 0
            const color = isPos ? 'var(--red)' : 'var(--green)'
            const nextFund = r.next_funding_time ? new Date(r.next_funding_time * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '—'
            return (
              <div key={i} style={{ padding: '10px 14px', borderRadius: 8, background: 'var(--surface)', border: '1px solid var(--border)' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 6 }}>
                  <span style={{ fontWeight: 700, fontSize: 12 }}>{r.symbol}</span>
                  <span style={{ fontSize: 10, color: 'var(--t3)' }}>{r.exchange}</span>
                </div>
                <div className="num" style={{ fontSize: 18, fontWeight: 700, color, marginBottom: 4 }}>
                  {isPos ? '+' : ''}{(rate * 100).toFixed(4)}%
                </div>
                <div style={{ fontSize: 10, color: 'var(--t3)' }}>
                  {t('annualized')} <span style={{ color }}>{annPct >= 0 ? '+' : ''}{annPct.toFixed(1)}%</span>
                  &nbsp;·&nbsp;{t('next_settlement')} {nextFund}
                </div>
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}

function FundingHarvest({ t }) {
  const [rows, setRows] = useState(null)   // null = loading, [] = empty
  const [bt, setBt] = useState({})         // symbol → { loading | result | error }
  const [open, setOpen] = useState(null)   // expanded symbol
  useEffect(() => {
    fetch('/api/funding-harvest?exchange=binance&days=60&min_periods=15&min_span_days=7&top_n=15',
          { headers: authHeaders() })
      .then(r => r.json())
      .then(d => setRows(Array.isArray(d) ? d : []))
      .catch(() => setRows([]))
  }, [])

  const runBacktest = (sym) => {
    if (open === sym) { setOpen(null); return }
    setOpen(sym)
    if (bt[sym] && !bt[sym].error) return   // cached
    setBt(s => ({ ...s, [sym]: { loading: true } }))
    fetch(`/api/funding-harvest/backtest?symbol=${encodeURIComponent(sym)}&days=60&fee_bps_per_leg=2`,
          { headers: authHeaders() })
      .then(async r => {
        const d = await r.json()
        if (!r.ok) throw new Error(d.detail || 'error')
        setBt(s => ({ ...s, [sym]: { result: d } }))
      })
      .catch(e => setBt(s => ({ ...s, [sym]: { error: String(e.message || e) } })))
  }

  if (rows === null) return (
    <div className="card"><div style={{ padding: 16 }}><Loading label={t('fh_loading')} /></div></div>
  )
  if (rows.length === 0) return (
    <div className="card"><div style={{ padding: 16 }}><Loading label={t('fh_empty')} /></div></div>
  )

  const favColor = v => v >= 90 ? 'var(--green)' : v >= 70 ? 'var(--yellow)' : 'var(--red)'
  const cell = { padding: '6px 10px', fontSize: 12 }
  const head = { ...cell, fontSize: 9, fontWeight: 700, letterSpacing: '0.08em',
                 textTransform: 'uppercase', color: 'var(--t3)', textAlign: 'right' }

  return (
    <div className="card">
      <div className="card-header">
        <span className="section-title">{t('funding_harvest')}</span>
        <span style={{ fontSize: 11, color: 'var(--t3)' }}>{t('funding_harvest_sub')}</span>
      </div>
      <div style={{ overflowX: 'auto', padding: '4px 8px 8px' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              <th style={{ ...head, textAlign: 'left' }}>{t('symbol') || 'Symbol'}</th>
              <th style={head}>{t('fh_net_annual')}</th>
              <th style={head}>{t('fh_favorable')}</th>
              <th style={head}>{t('fh_avg_rate')}</th>
              <th style={{ ...head, textAlign: 'center' }}>{t('fh_side')}</th>
              <th style={head}>{t('fh_periods')}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => {
              const b = bt[r.symbol]
              const expanded = open === r.symbol
              return (
              <Fragment key={i}>
              <tr onClick={() => runBacktest(r.symbol)}
                  style={{ borderTop: '1px solid var(--border)', cursor: 'pointer',
                           background: expanded ? 'var(--surface)' : 'transparent' }}>
                <td style={{ ...cell, fontWeight: 700 }}>
                  <span style={{ color: 'var(--accent)', marginRight: 5 }}>{expanded ? '▾' : '▸'}</span>
                  {r.symbol}
                </td>
                <td className="num" style={{ ...cell, textAlign: 'right', fontWeight: 700,
                      color: r.net_annual_pct >= 0 ? 'var(--green)' : 'var(--red)' }}>
                  {r.net_annual_pct >= 0 ? '+' : ''}{r.net_annual_pct.toFixed(0)}%
                </td>
                <td className="num" style={{ ...cell, textAlign: 'right', color: favColor(r.favorable_pct) }}>
                  {r.favorable_pct.toFixed(0)}%
                </td>
                <td className="num" style={{ ...cell, textAlign: 'right', color: 'var(--t2)' }}>
                  {r.mean_abs_rate_bps.toFixed(1)} bps
                  <span style={{ color: 'var(--t3)', fontSize: 10 }}>
                    {' / '}{Math.round(8760 / (r.settlements_per_year || 1095))}h
                  </span>
                </td>
                <td style={{ ...cell, textAlign: 'center', color: 'var(--t2)', fontSize: 11 }}>
                  {r.dominant_sign > 0 ? t('fh_short_perp') : t('fh_long_perp')}
                </td>
                <td className="num" style={{ ...cell, textAlign: 'right', color: 'var(--t3)' }}>
                  {r.n_periods}
                </td>
              </tr>
              {expanded && (
                <tr style={{ background: 'var(--surface)' }}>
                  <td colSpan={6} style={{ padding: '8px 14px 12px' }}>
                    {(!b || b.loading) && <span style={{ fontSize: 11, color: 'var(--t3)' }}>{t('fh_bt_running')}</span>}
                    {b && b.error && <span style={{ fontSize: 11, color: 'var(--yellow)' }}>{b.error}</span>}
                    {b && b.result && (() => {
                      const d = b.result
                      const item = (label, val, color) => (
                        <div style={{ marginRight: 18 }}>
                          <div style={{ fontSize: 9, color: 'var(--t3)', textTransform: 'uppercase' }}>{label}</div>
                          <div className="num" style={{ fontSize: 13, fontWeight: 700, color: color || 'var(--t1)' }}>{val}</div>
                        </div>
                      )
                      const sign = v => (v >= 0 ? '+' : '')
                      return (
                        <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'flex-end' }}>
                          {item(t('fh_net'), `${sign(d.net_pnl_usdt)}$${d.net_pnl_usdt.toFixed(0)}`,
                                d.net_pnl_usdt >= 0 ? 'var(--green)' : 'var(--red)')}
                          {item(t('fh_apr'), `${sign(d.apr_pct)}${d.apr_pct.toFixed(0)}%`,
                                d.apr_pct >= 0 ? 'var(--green)' : 'var(--red)')}
                          {item(t('fh_funding'), `+$${d.funding_collected_usdt.toFixed(0)}`, 'var(--green)')}
                          {item(t('fh_basis'), `${sign(d.basis_pnl_usdt)}$${d.basis_pnl_usdt.toFixed(0)}`,
                                d.basis_pnl_usdt >= 0 ? 'var(--t2)' : 'var(--red)')}
                          {item(t('fh_fees'), `-$${d.fees_usdt.toFixed(0)}`, 'var(--t3)')}
                          {item('Sharpe', d.sharpe_ratio.toFixed(1), 'var(--t2)')}
                          {item(t('fh_side'), d.side === 'short_perp' ? t('fh_short_perp') : t('fh_long_perp'), 'var(--t2)')}
                          <div style={{ fontSize: 9, color: 'var(--t3)', alignSelf: 'center' }}>
                            {d.span_days.toFixed(0)}d · {d.notional_usdt.toFixed(0)} USDT
                          </div>
                        </div>
                      )
                    })()}
                  </td>
                </tr>
              )}
              </Fragment>
            )})}
          </tbody>
        </table>
        <div style={{ padding: '8px 10px 2px', fontSize: 10, color: 'var(--t3)', lineHeight: 1.5 }}>
          {t('fh_bt_hint')} · {t('fh_caveat')}
        </div>
      </div>
    </div>
  )
}

function RegimeStrip() {
  const [regime, setRegime] = useState(null)
  const [micro, setMicro]   = useState(null)

  useEffect(() => {
    const load = async () => {
      try {
        const [r, m] = await Promise.all([
          fetch('/api/regime').then(x => x.json()),
          fetch('/api/microstructure').then(x => x.json()),
        ])
        setRegime(r); setMicro(m)
      } catch { /* ignore */ }
    }
    load()
    const iv = setInterval(load, 5000)
    return () => clearInterval(iv)
  }, [])

  const regimeColor = { low: 'var(--green)', normal: 'var(--accent)', high: 'var(--yellow)', extreme: 'var(--red)' }
  const obiColor = v => v > 0.3 ? 'var(--green)' : v < -0.3 ? 'var(--red)' : 'var(--t2)'
  const items = Object.entries(regime || {})
  if (!items.length) return null

  return (
    <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
      {items.map(([sym, snap]) => {
        const microKey = `binance:${sym}`
        const obi = micro?.[microKey]?.obi
        return (
          <div key={sym} style={{
            display: 'flex', alignItems: 'center', gap: 10,
            padding: '8px 14px', borderRadius: 8,
            background: 'var(--surface)', border: '1px solid var(--border)', flex: '1 1 200px',
          }}>
            <span style={{ fontWeight: 700, fontSize: 12, color: 'var(--t1)' }}>{sym}</span>
            <span style={{
              fontSize: 10, fontWeight: 700, padding: '2px 7px', borderRadius: 6,
              background: `${regimeColor[snap.regime] || 'var(--t3)'}18`,
              color: regimeColor[snap.regime] || 'var(--t3)',
            }}>{snap.regime?.toUpperCase()}</span>
            <span style={{ fontSize: 10, color: 'var(--t3)' }}>
              vol <span className="num" style={{ color: 'var(--t1)' }}>{(snap.realized_vol_ann * 100).toFixed(1)}%</span>
            </span>
            {obi != null && (
              <span style={{ fontSize: 10, color: 'var(--t3)' }}>
                OBI <span className="num" style={{ color: obiColor(obi), fontWeight: 700 }}>
                  {obi >= 0 ? '+' : ''}{obi.toFixed(3)}
                </span>
              </span>
            )}
            <span style={{ fontSize: 10, color: 'var(--t3)' }}>×{snap.pos_size_mult}</span>
          </div>
        )
      })}
    </div>
  )
}

function DepthChart({ exchange, symbol }) {
  const [book, setBook] = useState(null)
  useEffect(() => {
    const load = () =>
      fetch(`/api/microstructure`).then(r => r.ok ? r.json() : null)
        .then(data => {
          const key = `${exchange}:${symbol}`
          if (data && data[key]) setBook(data[key])
        }).catch(() => {})
    load()
    const iv = setInterval(load, 2000)
    return () => clearInterval(iv)
  }, [exchange, symbol])

  if (!book) return null

  const bidD = book.bid_depth_usdt || 0
  const askD = book.ask_depth_usdt || 0
  const total = bidD + askD || 1

  return (
    <div style={{ padding: '8px 20px 12px' }}>
      <div style={{ fontSize: 9, color: 'var(--t3)', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 6 }}>
        Order Book Depth
      </div>
      <div style={{ display: 'flex', height: 20, borderRadius: 4, overflow: 'hidden', gap: 2 }}>
        <div style={{ width: `${bidD / total * 100}%`, background: 'rgba(0,217,163,0.25)', borderRadius: '4px 0 0 4px', transition: 'width 0.5s' }} title={`Bids: $${bidD.toFixed(0)}`} />
        <div style={{ width: `${askD / total * 100}%`, background: 'rgba(255,60,92,0.25)', borderRadius: '0 4px 4px 0', transition: 'width 0.5s' }} title={`Asks: $${askD.toFixed(0)}`} />
      </div>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 4, fontSize: 10 }}>
        <span style={{ color: 'var(--green)' }}>Bids ${(bidD/1000).toFixed(0)}K</span>
        <span style={{ color: 'var(--t3)', fontSize: 9 }}>
          OBI {book.obi != null ? (book.obi > 0.5 ? '↑' : '↓') : '—'} {book.obi != null ? Math.abs(book.obi * 100 - 50).toFixed(0) + '%' : ''}
        </span>
        <span style={{ color: 'var(--red)' }}>Asks ${(askD/1000).toFixed(0)}K</span>
      </div>
    </div>
  )
}

export default function MarketsPage({ tickers, priceHistory, symbols }) {
  const { t } = useLang()
  return (
    <div className="page">
      <PageHeader title={t('markets_title')}>
        <span style={{ fontSize: 12, color: 'var(--t2)' }}>{t('pairs_realtime', symbols.length)}</span>
      </PageHeader>

      <RegimeStrip />
      <FundingRates symbols={symbols} t={t} />
      <FundingHarvest t={t} />

      {symbols.length === 0 && (
        <div className="card">
          <div className="empty-state">
            <div className="empty-icon">📈</div>
            <div className="empty-title">{t('no_symbols_cfg')}</div>
            <div className="empty-sub">{t('add_pairs_hint')}</div>
          </div>
        </div>
      )}

      {symbols.map(s => (
        <SymbolCard key={s} symbol={s}
          bnTicker={tickers[`binance:${s}`]}
          okxTicker={tickers[`okx:${s}`]}
          history={priceHistory[`binance:${s}`] || []}
          tickers={tickers}
          t={t}
        />
      ))}
    </div>
  )
}
