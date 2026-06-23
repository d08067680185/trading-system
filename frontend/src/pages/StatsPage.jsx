import { useState, useEffect, useRef } from 'react'
import { useLang } from '../i18n'
import { Button, PageHeader, StatTile } from '../components/ui'

function getApiKey() { return localStorage.getItem('trading_api_key') || '' }
async function apiFetch(path) {
  const key = getApiKey()
  const headers = { 'Content-Type': 'application/json', ...(key ? { 'X-API-Key': key } : {}) }
  const res = await fetch(path, { headers })
  if (!res.ok) {
    const body = await res.text()
    throw new Error(`${res.status} ${body}`)
  }
  return res.json()
}

/* ── Equity curve ─────────────────────────────────────────────────────────── */
function EquityCurve({ data }) {
  const ref = useRef(null)
  const [w, setW] = useState(480)
  const h = 120

  useEffect(() => {
    if (!ref.current) return
    const ro = new ResizeObserver(e => setW(e[0].contentRect.width))
    ro.observe(ref.current)
    return () => ro.disconnect()
  }, [])

  if (!data || data.length < 2) return (
    <div ref={ref} style={{ height: h, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--t3)', fontSize: 12 }}>
      No equity data yet
    </div>
  )

  const values = data.map(d => d.total_usdt)
  const min = Math.min(...values), max = Math.max(...values)
  const range = max - min || 1
  const pts = values.map((v, i) => [
    (i / (values.length - 1)) * w,
    h - ((v - min) / range) * (h - 6) - 3,
  ])
  const line = pts.map((p, i) => `${i === 0 ? 'M' : 'L'}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ')
  const area = `${line} L${w},${h} L0,${h} Z`
  const isUp = values[values.length - 1] >= values[0]
  const c = isUp ? '#00d9a3' : '#ff3c5c'

  return (
    <div ref={ref} style={{ width: '100%' }}>
      <svg width="100%" height={h} viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none">
        <defs>
          <linearGradient id="eq-grad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={c} stopOpacity="0.2" />
            <stop offset="100%" stopColor={c} stopOpacity="0" />
          </linearGradient>
        </defs>
        <path d={area} fill="url(#eq-grad)" />
        <path d={line} fill="none" stroke={c} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    </div>
  )
}


/* ── Strategy breakdown ───────────────────────────────────────────────────── */
function StratBreakdown({ rows, t }) {
  if (!rows || rows.length === 0) return null
  const totalVol = rows.reduce((s, r) => s + (r.volume || 0), 0)
  const maxTrades = Math.max(...rows.map(r => r.trades || 0), 1)
  const COLORS = ['var(--accent)', 'var(--green)', 'var(--yellow)', 'var(--red)', '#a78bfa', '#34d399']
  return (
    <div className="card">
      <div className="card-header">
        <span className="section-title">{t('stats_by_strategy')}</span>
        <span style={{ fontSize: 11, color: 'var(--t3)' }}>{t('stats_by_strat_vol', fmt(totalVol))}</span>
      </div>
      {/* Visual attribution cards */}
      <div style={{ padding: '12px 20px 4px', display: 'flex', flexWrap: 'wrap', gap: 10 }}>
        {rows.map((r, i) => {
          const sharePct = totalVol > 0 ? r.volume / totalVol * 100 : 0
          const color = COLORS[i % COLORS.length]
          return (
            <div key={i} style={{
              flex: '1 1 140px', background: 'var(--surface)', borderRadius: 10,
              border: '1px solid var(--border)', padding: '12px 14px',
            }}>
              <div style={{ fontSize: 11, fontWeight: 700, color, marginBottom: 8, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {r.strategy_id || t('manual_label')}
              </div>
              <div style={{ height: 3, borderRadius: 2, background: 'var(--bg2)', marginBottom: 10, overflow: 'hidden' }}>
                <div style={{ height: '100%', borderRadius: 2, background: color, width: `${Math.min(100, r.trades / maxTrades * 100)}%` }} />
              </div>
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 10, color: 'var(--t3)' }}>
                <span>{r.trades} {t('opt_runs')}</span>
                <span>{sharePct.toFixed(0)}% vol</span>
              </div>
              <div style={{ fontSize: 11, color: 'var(--red)', marginTop: 4 }}>
                -{fmtFee(r.fees)} {t('th_fees_usdt')}
              </div>
            </div>
          )
        })}
      </div>
      <div className="table-wrap">
        <table>
          <thead><tr>
            <th>{t('th_strategy')}</th>
            <th style={{ textAlign: 'right' }}>{t('th_trade_count')}</th>
            <th style={{ textAlign: 'right' }}>{t('th_volume_usdt')}</th>
            <th style={{ textAlign: 'right' }}>{t('th_fees_usdt')}</th>
            <th style={{ textAlign: 'right' }}>{t('th_share')}</th>
          </tr></thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i}>
                <td>
                  <span style={{
                    fontSize: 11, fontWeight: 700, padding: '2px 7px', borderRadius: 4,
                    background: 'rgba(59,123,255,0.1)', color: 'var(--accent)',
                  }}>{r.strategy_id || t('manual_label')}</span>
                </td>
                <td className="num" style={{ textAlign: 'right' }}>{r.trades}</td>
                <td className="num" style={{ textAlign: 'right' }}>{fmt(r.volume)}</td>
                <td className="num" style={{ textAlign: 'right', color: 'var(--red)' }}>-{fmtFee(r.fees)}</td>
                <td style={{ textAlign: 'right' }}>
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'flex-end', gap: 6 }}>
                    <div style={{ width: 50, height: 4, borderRadius: 2, background: 'var(--bg2)', overflow: 'hidden' }}>
                      <div style={{
                        height: '100%', borderRadius: 2, background: 'var(--accent)',
                        width: `${totalVol > 0 ? Math.min(100, r.volume / totalVol * 100) : 0}%`,
                      }} />
                    </div>
                    <span style={{ fontSize: 10, color: 'var(--t3)', minWidth: 32 }}>
                      {totalVol > 0 ? (r.volume / totalVol * 100).toFixed(0) : 0}%
                    </span>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

const fmtPct = n => n != null ? `${n >= 0 ? '+' : ''}${Number(n).toFixed(2)}%` : '—'
const fmtBps = n => n != null ? `${Number(n).toFixed(1)} bps` : '—'

/* ── TCA (execution quality) section ─────────────────────────────────────── */
function TCASection({ tcaData }) {
  const entries = Object.entries(tcaData || {})
  if (entries.length === 0) return (
    <div style={{ padding: '12px 20px', fontSize: 12, color: 'var(--t3)' }}>
      No TCA data yet — data appears after fills are recorded.
    </div>
  )
  const scoreColor = s => s >= 80 ? 'var(--green)' : s >= 50 ? 'var(--yellow)' : 'var(--red)'
  return (
    <div style={{ padding: '0 20px 20px' }}>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10 }}>
        {entries.map(([sid, d]) => (
          <div key={sid} style={{
            flex: '1 1 220px', background: 'var(--bg2)', borderRadius: 10,
            border: '1px solid var(--border)', padding: '14px 16px',
          }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
              <span style={{ fontSize: 11, fontWeight: 700, color: 'var(--accent)' }}>{sid}</span>
              <span style={{
                fontSize: 13, fontWeight: 800, color: scoreColor(d.execution_score ?? 0),
              }}>{(d.execution_score ?? 0).toFixed(0)}/100</span>
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6 }}>
              {[
                ['Fills', d.n_fills ?? 0],
                ['Avg Slip', fmtBps(d.mean_slippage_bps)],
                ['p99 Slip', fmtBps(d.p99_slippage_bps)],
                ['Maker %', `${(d.maker_rate_pct ?? 0).toFixed(0)}%`],
                ['Over Budget', `${(d.over_budget_rate_pct ?? 0).toFixed(0)}%`],
                ['Total Cost', `${(d.total_cost_usdt ?? 0).toFixed(3)} U`],
              ].map(([label, val]) => (
                <div key={label}>
                  <div style={{ fontSize: 9, color: 'var(--t3)', letterSpacing: '0.06em', marginBottom: 2 }}>{label.toUpperCase()}</div>
                  <div className="num" style={{ fontSize: 12, fontWeight: 600, color: 'var(--t1)' }}>{val}</div>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

export default function StatsPage() {
  const { t, lang } = useLang()
  const fmt = n => n != null ? Number(n).toLocaleString(lang, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : '—'
  const fmtFee = n => n != null ? Number(n).toLocaleString(lang, { minimumFractionDigits: 4, maximumFractionDigits: 4 }) : '—'
  const fmtTs = ts => ts ? new Date(ts * 1000).toLocaleDateString(lang, { month: 'short', day: 'numeric', year: 'numeric' }) : '—'
  const [stats, setStats] = useState(null)
  const [equity, setEquity] = useState([])
  const [tcaData, setTcaData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const load = async () => {
    setLoading(true)
    setError(null)
    try {
      const [statsRes, equityRes, tcaRes] = await Promise.all([
        apiFetch('/api/stats'),
        apiFetch('/api/data/equity?limit=720').catch(() => []),
        apiFetch('/api/tca/stats').catch(() => null),
      ])
      setStats(statsRes)
      setEquity(Array.isArray(equityRes) ? equityRes : [])
      setTcaData(tcaRes)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  if (loading) return <div className="page"><div style={{ color: 'var(--t3)', padding: 40, textAlign: 'center' }}>{t('loading')}</div></div>
  if (error) return <div className="page"><div style={{ color: 'var(--red)', padding: 40, textAlign: 'center' }}>{error}</div></div>
  if (!stats) return null

  const returnColor = stats.total_return_pct == null ? 'var(--t1)' : stats.total_return_pct >= 0 ? 'var(--green)' : 'var(--red)'
  const ddColor = stats.max_drawdown_pct == null ? 'var(--t1)' : stats.max_drawdown_pct < -10 ? 'var(--red)' : 'var(--yellow)'

  return (
    <div className="page">
      <PageHeader title={t('nav_stats')}>
        <a href="/api/data/trades/export" download="trades.csv" style={{ textDecoration: 'none' }}>
          <Button variant="ghost" size="sm">{t('stats_export_trades')}</Button>
        </a>
        <a href="/api/data/equity/export" download="equity.csv" style={{ textDecoration: 'none' }}>
          <Button variant="ghost" size="sm">{t('stats_export_equity')}</Button>
        </a>
        <Button variant="ghost" size="sm" onClick={load}>{t('bt_refresh')}</Button>
      </PageHeader>

      {/* Equity curve */}
      <div className="card">
        <div className="card-header">
          <span className="section-title">{t('stats_equity_curve')}</span>
          {equity.length > 0 && (
            <span style={{ fontSize: 11, color: 'var(--t3)' }}>
              {fmtTs(equity[0]?.ts)} — {fmtTs(equity[equity.length - 1]?.ts)} · {t('stat_data_pts', equity.length)}
            </span>
          )}
        </div>
        <div style={{ padding: '12px 20px 16px' }}>
          <EquityCurve data={equity} />
        </div>
      </div>

      {/* KPI tiles */}
      <div className="grid-4">
        <StatTile label={t('stats_total_return')} value={fmtPct(stats.total_return_pct)} color={returnColor}
          sub={`${fmt(stats.initial_equity_usdt)} → ${fmt(stats.current_equity_usdt)} USDT`} />
        <StatTile label={t('stats_max_drawdown')} value={fmtPct(stats.max_drawdown_pct)} color={ddColor}
          sub={t('stat_peak', fmt(stats.peak_equity_usdt))} />
        <StatTile label={t('stats_total_trades')} value={stats.total_trades ?? 0}
          sub={`${fmtTs(stats.first_trade_ts)} — ${fmtTs(stats.last_trade_ts)}`} />
        <StatTile label={t('stats_total_volume')} value={`${fmt(stats.total_volume_usdt)} USDT`}
          sub={t('stat_fees_label', fmtFee(stats.total_fees_usdt))} />
      </div>

      {/* By strategy */}
      <StratBreakdown rows={stats.by_strategy} t={t} />

      {/* TCA — execution quality */}
      {tcaData && (
        <div className="card">
          <div className="card-header">
            <span className="section-title">Execution Quality (TCA)</span>
            <span style={{ fontSize: 11, color: 'var(--t3)' }}>slippage · maker rate · score per strategy</span>
          </div>
          <TCASection tcaData={tcaData} />
        </div>
      )}

      {stats.total_trades === 0 && (
        <div className="card">
          <div className="empty-state">
            <div className="empty-icon">📊</div>
            <div className="empty-title">{t('no_trade_data')}</div>
            <div className="empty-sub">{t('no_trade_data_sub')}</div>
          </div>
        </div>
      )}
    </div>
  )
}
