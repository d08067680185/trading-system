import { useState, useEffect, useCallback } from 'react'
import { useLang } from '../i18n'
import { Button, PageHeader, StatTile, Card, Alert } from '../components/ui'

function getApiKey() { return localStorage.getItem('trading_api_key') || '' }
function authHeaders() { const k = getApiKey(); return k ? { 'X-API-Key': k } : {} }

async function apiFetch(path) {
  const res = await fetch(`/api${path}`, { headers: authHeaders() })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

/* ── Alert badge ─────────────────────────────────────────────────────────── */
function AlertBadge({ level }) {
  const cfg = {
    ok:       { bg: 'rgba(0,217,163,0.1)',  color: 'var(--green)', label: 'OK' },
    caution:  { bg: 'rgba(255,193,7,0.1)',  color: 'var(--yellow)', label: 'CAUTION' },
    warning:  { bg: 'rgba(255,60,92,0.1)',  color: 'var(--red)',  label: 'WARN' },
    critical: { bg: 'rgba(255,60,92,0.15)', color: 'var(--red)',  label: 'CRIT' },
  }[level] || { bg: 'var(--surface)', color: 'var(--t3)', label: level }
  return (
    <span style={{ fontSize: 9, fontWeight: 700, padding: '2px 7px', borderRadius: 8, background: cfg.bg, color: cfg.color }}>
      {cfg.label}
    </span>
  )
}

/* ── Correlation heatmap ─────────────────────────────────────────────────── */
function CorrelationHeatmap({ matrix }) {
  const strategies = Object.keys(matrix)
  if (strategies.length < 2) return (
    <div style={{ fontSize: 12, color: 'var(--t3)', padding: '12px 0' }}>
      Need ≥ 2 strategies with trade history to compute correlation.
    </div>
  )
  const cellColor = corr => {
    const abs = Math.abs(corr)
    if (corr === 1.0) return 'rgba(59,123,255,0.15)'
    if (corr > 0.7)  return `rgba(255,60,92,${0.1 + abs * 0.3})`
    if (corr < -0.7) return `rgba(0,217,163,${0.1 + abs * 0.3})`
    return `rgba(100,140,180,${abs * 0.08})`
  }
  const textColor = corr => {
    if (corr === 1.0) return 'var(--accent)'
    if (Math.abs(corr) > 0.6) return 'var(--t1)'
    return 'var(--t2)'
  }
  return (
    <div style={{ overflowX: 'auto', overflowY: 'hidden' }}>
      <table style={{ borderCollapse: 'separate', borderSpacing: 3, fontSize: 11 }}>
        <thead>
          <tr>
            <th style={{ padding: '4px 8px', color: 'var(--t3)', fontWeight: 400, fontSize: 10 }}></th>
            {strategies.map(s => (
              <th key={s} style={{ padding: '4px 8px', color: 'var(--t3)', fontWeight: 400, fontSize: 10, textAlign: 'center', maxWidth: 90 }}>
                {s.replace('_', '​_')}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {strategies.map(s1 => (
            <tr key={s1}>
              <td style={{ padding: '4px 10px 4px 0', color: 'var(--t3)', fontWeight: 400, fontSize: 10, whiteSpace: 'nowrap' }}>
                {s1}
              </td>
              {strategies.map(s2 => {
                const v = matrix[s1]?.[s2] ?? 0
                return (
                  <td key={s2} title={`${s1} vs ${s2}: ${v.toFixed(3)}`}
                    style={{
                      padding: '6px 10px', textAlign: 'center', borderRadius: 6,
                      background: cellColor(v), color: textColor(v),
                      fontFamily: 'monospace', fontWeight: 600,
                      border: '1px solid var(--bg)',
                    }}>
                    {v.toFixed(2)}
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/* ── Regime badge ────────────────────────────────────────────────────────── */
function RegimeBadge({ regime }) {
  const cfg = {
    low:     { bg: 'rgba(0,217,163,0.12)',  color: 'var(--green)',  icon: '↓' },
    normal:  { bg: 'rgba(59,123,255,0.12)', color: 'var(--accent)', icon: '→' },
    high:    { bg: 'rgba(255,193,7,0.12)',  color: 'var(--yellow)', icon: '↑' },
    extreme: { bg: 'rgba(255,60,92,0.12)',  color: 'var(--red)',    icon: '⚠' },
  }[regime] || { bg: 'var(--surface)', color: 'var(--t3)', icon: '?' }
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 4,
      padding: '3px 9px', borderRadius: 8, fontSize: 11, fontWeight: 700,
      background: cfg.bg, color: cfg.color,
    }}>
      {cfg.icon} {regime?.toUpperCase()}
    </span>
  )
}

/* ── VaR bar ─────────────────────────────────────────────────────────────── */
function VarBar({ label, value, limit, color }) {
  const pct = limit > 0 ? Math.min(100, Math.abs(value) / limit * 100) : 0
  const dangerColor = pct > 80 ? 'var(--red)' : pct > 50 ? 'var(--yellow)' : color || 'var(--accent)'
  return (
    <div style={{ marginBottom: 12 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 5 }}>
        <span style={{ fontSize: 11, color: 'var(--t2)' }}>{label}</span>
        <span className="num" style={{ fontSize: 12, fontWeight: 700, color: dangerColor }}>
          {value >= 0 ? '+' : ''}{value.toFixed(2)} USDT
        </span>
      </div>
      {limit > 0 && (
        <div style={{ height: 4, borderRadius: 2, background: 'var(--border)', overflow: 'hidden' }}>
          <div style={{ height: '100%', width: `${pct}%`, background: dangerColor, borderRadius: 2, transition: 'width 0.4s' }} />
        </div>
      )}
    </div>
  )
}

/* ── Attribution summary ─────────────────────────────────────────────────── */
function AttributionSummary({ data }) {
  if (!data?.by_strategy || Object.keys(data.by_strategy).length === 0)
    return <div style={{ fontSize: 12, color: 'var(--t3)', padding: '8px 0' }}>No attribution data yet.</div>

  const SOURCE_COLORS = {
    spread: 'var(--accent)',
    funding: 'var(--green)',
    execution: 'var(--yellow)',
    fee: 'var(--red)',
  }
  const strategies = Object.entries(data.by_strategy)
  return (
    <div>
      {strategies.map(([sid, sources]) => (
        <div key={sid} style={{ marginBottom: 16 }}>
          <div style={{ fontSize: 11, fontWeight: 700, color: 'var(--t1)', marginBottom: 8 }}>{sid}</div>
          {Object.entries(sources).map(([src, info]) => (
            <div key={src} style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 5 }}>
              <span style={{ width: 8, height: 8, borderRadius: 2, flexShrink: 0, background: SOURCE_COLORS[src] || 'var(--t3)' }} />
              <span style={{ fontSize: 11, color: 'var(--t2)', flex: 1, textTransform: 'capitalize' }}>{src}</span>
              <span className="num" style={{ fontSize: 12, fontWeight: 700, color: info.total_pnl >= 0 ? 'var(--green)' : 'var(--red)' }}>
                {info.total_pnl >= 0 ? '+' : ''}{info.total_pnl.toFixed(4)}
              </span>
              <span style={{ fontSize: 10, color: 'var(--t3)' }}>({info.count})</span>
            </div>
          ))}
        </div>
      ))}
    </div>
  )
}

/* ── Main page ────────────────────────────────────────────────────────────── */
export default function RiskPage({ risk }) {
  const { t } = useLang()
  const [portfolioRisk, setPortfolioRisk] = useState(null)
  const [regime, setRegime]               = useState(null)
  const [margin, setMargin]               = useState(null)
  const [exposure, setExposure]           = useState(null)
  const [attribution, setAttribution]     = useState(null)
  const [reconciler, setReconciler]       = useState(null)
  const [positionSizer, setPositionSizer] = useState(null)
  const [reconRunning, setReconRunning]   = useState(false)
  const [stressData, setStressData]       = useState(null)
  const [tcaStats, setTcaStats]           = useState(null)
  const [latency, setLatency]             = useState(null)
  const [stressLoading, setStressLoading] = useState(false)

  const load = useCallback(async () => {
    const results = await Promise.allSettled([
      apiFetch('/portfolio/risk'),
      apiFetch('/regime'),
      apiFetch('/margin'),
      apiFetch('/factor-exposure'),
      apiFetch('/attribution/summary?days=30'),
      apiFetch('/reconciler/status'),
      apiFetch('/position-sizer/status'),
      apiFetch('/tca/stats'),
      apiFetch('/latency'),
    ])
    if (results[0].status === 'fulfilled') setPortfolioRisk(results[0].value)
    if (results[1].status === 'fulfilled') setRegime(results[1].value)
    if (results[2].status === 'fulfilled') setMargin(results[2].value)
    if (results[3].status === 'fulfilled') setExposure(results[3].value)
    if (results[4].status === 'fulfilled') setAttribution(results[4].value)
    if (results[5].status === 'fulfilled') setReconciler(results[5].value)
    if (results[6].status === 'fulfilled') setPositionSizer(results[6].value)
    if (results[7].status === 'fulfilled') setTcaStats(results[7].value)
    if (results[8].status === 'fulfilled') setLatency(results[8].value)
  }, [])

  const runStress = async () => {
    setStressLoading(true)
    try { const d = await apiFetch('/stress/report'); setStressData(d) }
    catch {} finally { setStressLoading(false) }
  }

  useEffect(() => { load(); const id = setInterval(load, 15000); return () => clearInterval(id) }, [load])

  const runReconcile = async () => {
    setReconRunning(true)
    try {
      const res = await fetch('/api/reconciler/run', { method: 'POST', headers: authHeaders() })
      const d = await res.json()
      setReconciler(prev => ({ ...prev, last_report: d }))
    } catch {} finally { setReconRunning(false); load() }
  }

  const pnl = v => `${v >= 0 ? '+' : ''}${(v || 0).toFixed(2)}`
  const pct = v => `${(v || 0) >= 0 ? '+' : ''}${(v || 0).toFixed(2)}%`

  return (
    <div className="page">
      <PageHeader title={t('risk_page_title')}>
        <Button variant="ghost" size="sm" onClick={load}>↻ {t('bt_refresh')}</Button>
      </PageHeader>

      {/* ── P&L Overview ─────────────────────────────────────────────────── */}
      {risk && (
        <Card style={{ padding: 20 }}>
          <div className="section-title" style={{ marginBottom: 12 }}>{t('risk_pnl_overview')}</div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(140px, 1fr))', gap: 10 }}>
            <StatTile label={t('daily_pnl_label')} value={`${pnl(risk.daily_pnl_usdt)} USDT`}
              color={(risk.daily_pnl_usdt || 0) >= 0 ? 'var(--green)' : 'var(--red)'} />
            <StatTile label="7-Day Rolling" value={`${pnl(risk.rolling_7d_pnl_usdt)} USDT`}
              color={(risk.rolling_7d_pnl_usdt || 0) >= 0 ? 'var(--green)' : 'var(--red)'} />
            <StatTile label="30-Day Rolling" value={`${pnl(risk.rolling_30d_pnl_usdt)} USDT`}
              color={(risk.rolling_30d_pnl_usdt || 0) >= 0 ? 'var(--green)' : 'var(--red)'} />
            <StatTile label="Peak Equity" value={`${(risk.peak_equity_usdt || 0).toFixed(2)} USDT`} />
            <StatTile label="Open Orders" value={risk.open_orders ?? 0}
              sub={`Max: ${risk.limits?.max_open_orders ?? '—'}`} />
            <StatTile label="Status" value={risk.halted ? '⏹ HALTED' : '▶ ACTIVE'}
              color={risk.halted ? 'var(--red)' : 'var(--green)'} />
          </div>
          {risk.daily_pnl_usdt != null && risk.limits?.max_daily_loss_usdt > 0 && (
            <div style={{ marginTop: 16 }}>
              <VarBar
                label={`Daily Loss Usage (limit: ${risk.limits.max_daily_loss_usdt} USDT)`}
                value={risk.daily_pnl_usdt}
                limit={risk.limits.max_daily_loss_usdt}
              />
            </div>
          )}
        </Card>
      )}

      {/* ── Portfolio VaR ────────────────────────────────────────────────── */}
      {portfolioRisk && (
        <Card style={{ padding: 20 }}>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, marginBottom: 12 }}>
            <div className="section-title">{t('risk_var_title')}</div>
            <div style={{ fontSize: 11, color: 'var(--t3)' }}>{portfolioRisk.lookback_days}d history</div>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(140px, 1fr))', gap: 10, marginBottom: 16 }}>
            <StatTile label="VaR (95%)" value={`${(portfolioRisk.portfolio_var_95 || 0).toFixed(2)} USDT`}
              color={(portfolioRisk.portfolio_var_95 || 0) < 0 ? 'var(--red)' : 'var(--t1)'}
              sub="max daily loss at 95% conf" />
            <StatTile label="CVaR (95%)" value={`${(portfolioRisk.portfolio_cvar_95 || 0).toFixed(2)} USDT`}
              color={(portfolioRisk.portfolio_cvar_95 || 0) < 0 ? 'var(--red)' : 'var(--t1)'}
              sub="expected loss beyond VaR" />
            <StatTile label="Portfolio Sharpe" value={(portfolioRisk.portfolio_sharpe || 0).toFixed(3)}
              color={(portfolioRisk.portfolio_sharpe || 0) >= 1 ? 'var(--green)' : (portfolioRisk.portfolio_sharpe || 0) >= 0 ? 'var(--yellow)' : 'var(--red)'} />
            <StatTile label="Beta BTC" value={(portfolioRisk.beta_btc || 0).toFixed(3)} sub="net BTC exposure" />
            <StatTile label="Beta ETH" value={(portfolioRisk.beta_eth || 0).toFixed(3)} sub="net ETH exposure" />
          </div>

          {portfolioRisk.high_correlation_pairs?.length > 0 && (
            <Alert variant="error" style={{ marginBottom: 14 }}>
              <div style={{ fontWeight: 700, marginBottom: 6 }}>⚠ HIGH CORRELATION DETECTED</div>
              {portfolioRisk.high_correlation_pairs.map((p, i) => (
                <div key={i} style={{ fontSize: 11, marginBottom: 2 }}>
                  {p.s1} ↔ {p.s2}: <span className="num" style={{ fontWeight: 700 }}>{p.corr != null ? p.corr.toFixed(3) : '—'}</span>
                  <span style={{ color: 'var(--t3)', marginLeft: 6 }}>(consider reducing one)</span>
                </div>
              ))}
            </Alert>
          )}

          {portfolioRisk.correlation_matrix && Object.keys(portfolioRisk.correlation_matrix).length >= 2 && (
            <div>
              <div className="label" style={{ marginBottom: 8 }}>Strategy Correlation Matrix</div>
              <CorrelationHeatmap matrix={portfolioRisk.correlation_matrix} />
            </div>
          )}
          {(!portfolioRisk.correlation_matrix || Object.keys(portfolioRisk.correlation_matrix).length < 2) && (
            <div style={{ fontSize: 12, color: 'var(--t3)' }}>Correlation data accumulates after strategies complete trades.</div>
          )}
        </Card>
      )}

      {/* ── Market Regime ────────────────────────────────────────────────── */}
      {regime && Object.keys(regime).length > 0 && (
        <Card style={{ padding: 20 }}>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, marginBottom: 12 }}>
            <div className="section-title">{t('risk_regime_title')}</div>
            <div style={{ fontSize: 11, color: 'var(--t3)' }}>Realized volatility classification</div>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))', gap: 12 }}>
            {Object.entries(regime).map(([symbol, snap]) => (
              <div key={symbol} style={{ padding: '14px 16px', borderRadius: 10, border: '1px solid var(--border)', background: 'var(--surface)' }}>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 10 }}>
                  <span style={{ fontWeight: 700, color: 'var(--t1)' }}>{symbol}</span>
                  <RegimeBadge regime={snap.regime} />
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8, fontSize: 11 }}>
                  <div>
                    <div style={{ color: 'var(--t3)', fontSize: 9, textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 2 }}>Ann. Vol</div>
                    <span className="num" style={{ color: 'var(--t1)', fontWeight: 600 }}>{(snap.realized_vol_ann * 100).toFixed(1)}%</span>
                  </div>
                  <div>
                    <div style={{ color: 'var(--t3)', fontSize: 9, textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 2 }}>Percentile</div>
                    <span className="num" style={{ color: 'var(--t2)', fontWeight: 600 }}>{snap.vol_percentile?.toFixed(0)}th</span>
                  </div>
                  <div>
                    <div style={{ color: 'var(--t3)', fontSize: 9, textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 2 }}>Size Mult</div>
                    <span className="num" style={{ color: snap.pos_size_mult > 1 ? 'var(--green)' : snap.pos_size_mult < 1 ? 'var(--red)' : 'var(--t1)', fontWeight: 600 }}>×{snap.pos_size_mult}</span>
                  </div>
                  <div>
                    <div style={{ color: 'var(--t3)', fontSize: 9, textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 2 }}>Threshold Mult</div>
                    <span className="num" style={{ color: snap.threshold_mult > 1 ? 'var(--yellow)' : 'var(--green)', fontWeight: 600 }}>×{snap.threshold_mult}</span>
                  </div>
                </div>
              </div>
            ))}
          </div>
          {positionSizer && (
            <div style={{ marginTop: 14 }}>
              <div className="label" style={{ marginBottom: 8 }}>Vol-Adjusted Position Sizes (at 10k USDT capital)</div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
                {Object.entries(positionSizer.vols || {}).map(([sym, vol]) => (
                  <div key={sym} style={{ padding: '6px 12px', borderRadius: 8, background: 'var(--bg2)', border: '1px solid var(--border)' }}>
                    <span style={{ fontSize: 11, color: 'var(--t2)', marginRight: 8 }}>{sym}</span>
                    <span className="num" style={{ fontWeight: 700, color: 'var(--t1)' }}>
                      {vol > 0 ? `~${(positionSizer.target_vol_pct / (vol / Math.sqrt(252)) * 10000).toFixed(0)} USDT` : '—'}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </Card>
      )}

      {/* ── Factor Exposure ──────────────────────────────────────────────── */}
      {exposure && !exposure.status && (
        <Card style={{ padding: 20 }}>
          <div className="section-title" style={{ marginBottom: 12 }}>{t('risk_exposure_title')}</div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(160px, 1fr))', gap: 10, marginBottom: 14 }}>
            <StatTile label="Net BTC Exposure"
              value={`${pnl(exposure.net_btc_usdt)} USDT`}
              sub={`${(exposure.btc_weight_pct || 0).toFixed(1)}% of notional`}
              color={Math.abs(exposure.btc_weight_pct || 0) > 40 ? 'var(--red)' : 'var(--t1)'} />
            <StatTile label="Net ETH Exposure"
              value={`${pnl(exposure.net_eth_usdt)} USDT`}
              sub={`${(exposure.eth_weight_pct || 0).toFixed(1)}% of notional`}
              color={Math.abs(exposure.eth_weight_pct || 0) > 40 ? 'var(--red)' : 'var(--t1)'} />
            <StatTile label="Total Notional" value={`${(exposure.total_notional || 0).toFixed(2)} USDT`} />
            <div style={{ padding: '18px 20px', borderRadius: 12, background: 'var(--surface)', border: '1px solid var(--border)' }}>
              <div style={{ fontSize: 10, color: 'var(--t3)', letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 10 }}>Alert</div>
              <AlertBadge level={exposure.alert} />
            </div>
          </div>
          {exposure.positions?.length > 0 && (
            <div className="table-wrap">
              <table>
                <thead><tr>
                  <th>Exchange</th><th>Symbol</th><th>Side</th>
                  <th style={{ textAlign: 'right' }}>Notional</th>
                  <th style={{ textAlign: 'right' }}>Signed</th>
                </tr></thead>
                <tbody>
                  {exposure.positions.map((p, i) => (
                    <tr key={i}>
                      <td style={{ textTransform: 'capitalize' }}>{p.exchange}</td>
                      <td style={{ fontWeight: 600 }}>{p.symbol}</td>
                      <td><span className={`badge badge-${p.side}`}>{p.side?.toUpperCase()}</span></td>
                      <td className="num" style={{ textAlign: 'right' }}>{p.notional?.toFixed(2)}</td>
                      <td className="num" style={{ textAlign: 'right', fontWeight: 700, color: (p.signed_notional || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>
                        {(p.signed_notional || 0) >= 0 ? '+' : ''}{p.signed_notional?.toFixed(2)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}

      {/* ── Margin Monitor ───────────────────────────────────────────────── */}
      <Card style={{ padding: 20 }}>
        <div className="section-title" style={{ marginBottom: 12 }}>{t('risk_margin_title')}</div>
        {margin?.positions?.length > 0 ? (
          <div className="table-wrap">
            <table>
              <thead><tr>
                <th>Exchange</th><th>Symbol</th><th>Side</th>
                <th style={{ textAlign: 'right' }}>Mark</th>
                <th style={{ textAlign: 'right' }}>Liq.</th>
                <th style={{ textAlign: 'right' }}>Safety %</th>
                <th style={{ textAlign: 'right' }}>Leverage</th>
                <th>Status</th>
              </tr></thead>
              <tbody>
                {margin.positions.map((p, i) => (
                  <tr key={i}>
                    <td style={{ textTransform: 'capitalize' }}>{p.exchange}</td>
                    <td style={{ fontWeight: 600 }}>{p.symbol}</td>
                    <td><span className={`badge badge-${p.side}`}>{p.side?.toUpperCase()}</span></td>
                    <td className="num" style={{ textAlign: 'right' }}>{p.mark_price?.toLocaleString()}</td>
                    <td className="num" style={{ textAlign: 'right', color: 'var(--red)' }}>{p.liq_price?.toLocaleString()}</td>
                    <td className="num" style={{ textAlign: 'right', fontWeight: 700, color: p.safety_pct < 8 ? 'var(--red)' : p.safety_pct < 15 ? 'var(--yellow)' : 'var(--green)' }}>
                      {(p.safety_pct || 0).toFixed(1)}%
                    </td>
                    <td className="num" style={{ textAlign: 'right' }}>{p.leverage}×</td>
                    <td><AlertBadge level={p.alert_level} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div style={{ fontSize: 12, color: 'var(--t3)' }}>No open leveraged positions.</div>
        )}
      </Card>

      {/* ── PnL Attribution ─────────────────────────────────────────────── */}
      <Card style={{ padding: 20 }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, marginBottom: 12 }}>
          <div className="section-title">{t('risk_attribution_title')}</div>
          <div style={{ fontSize: 11, color: 'var(--t3)' }}>Last 30 days</div>
        </div>
        <AttributionSummary data={attribution} />
      </Card>

      {/* ── Reconciler ──────────────────────────────────────────────────── */}
      {reconciler && (
        <Card style={{ padding: 20 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 14, flexWrap: 'wrap', gap: 8 }}>
            <div className="section-title">{t('risk_reconciler_title')}</div>
            <Button variant="ghost" size="sm" onClick={runReconcile} disabled={reconRunning}>
              {reconRunning ? 'Running…' : '↺ Run Now'}
            </Button>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(160px, 1fr))', gap: 10, marginBottom: 14 }}>
            <StatTile label="Last Run"
              value={reconciler.last_run_ts > 0 ? new Date(reconciler.last_run_ts * 1000).toLocaleTimeString() : 'Never'} />
            <StatTile label="Since Last Run"
              value={reconciler.seconds_since_run != null ? `${Math.round(reconciler.seconds_since_run / 60)}m ago` : '—'} />
            <StatTile label="Total Corrections" value={reconciler.discrepancy_count ?? 0}
              color={reconciler.discrepancy_count > 0 ? 'var(--yellow)' : 'var(--green)'} />
          </div>
          {reconciler.last_report?.discrepancies?.length > 0 && (
            <Alert variant="warning">
              <div style={{ fontWeight: 700, marginBottom: 6 }}>DISCREPANCIES CORRECTED</div>
              {reconciler.last_report.discrepancies.map((d, i) => (
                <div key={i} style={{ fontSize: 11, marginBottom: 2 }}>
                  {d.exchange}:{d.symbol} — actual={d.actual_notional} local={d.local_notional} (Δ{d.diff})
                </div>
              ))}
            </Alert>
          )}
          {reconciler.last_report?.discrepancies?.length === 0 && reconciler.last_run_ts > 0 && (
            <div style={{ fontSize: 12, color: 'var(--green)' }}>✓ All positions reconciled — no discrepancies.</div>
          )}
        </Card>
      )}

      {/* ── Stress Test ─────────────────────────────────────────────────── */}
      <Card style={{ padding: 20 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 14, flexWrap: 'wrap', gap: 8 }}>
          <div className="section-title">{t('risk_stress_title')}</div>
          <Button variant="ghost" size="sm" onClick={runStress} disabled={stressLoading}>
            {stressLoading ? 'Running…' : '▶ Run Scenarios'}
          </Button>
        </div>
        {stressData ? (
          <div>
            {stressData.monte_carlo && stressData.monte_carlo.n_simulations > 0 && (
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(140px, 1fr))', gap: 10, marginBottom: 16 }}>
                <StatTile label={`MC VaR (${(stressData.monte_carlo.confidence_level * 100).toFixed(0)}%)`}
                  value={`${stressData.monte_carlo.var_usdt.toFixed(2)} USDT`}
                  color={stressData.monte_carlo.var_usdt > 0 ? 'var(--red)' : 'var(--green)'}
                  sub={`${stressData.monte_carlo.n_simulations.toLocaleString()} simulations`} />
                <StatTile label="MC CVaR" value={`${stressData.monte_carlo.cvar_usdt.toFixed(2)} USDT`} color="var(--red)" />
                <StatTile label="Worst Scenario" value={`${(stressData.worst_scenario_pnl || 0).toFixed(2)} USDT`} color="var(--red)" />
              </div>
            )}
            {stressData.scenarios?.length > 0 && (
              <div className="table-wrap">
                <table>
                  <thead><tr>
                    <th>Scenario</th><th>Shocks</th>
                    <th style={{ textAlign: 'right' }}>Portfolio P&L</th>
                    <th style={{ textAlign: 'right' }}>% of Equity</th>
                  </tr></thead>
                  <tbody>
                    {stressData.scenarios.map((s, i) => (
                      <tr key={i}>
                        <td style={{ fontWeight: 600, fontSize: 11 }}>{s.scenario.replace(/_/g, ' ')}</td>
                        <td style={{ fontSize: 10, color: 'var(--t3)' }}>
                          {Object.entries(s.shocks || {}).map(([k, v]) => `${k}: ${v}`).join(', ')}
                        </td>
                        <td className="num" style={{ textAlign: 'right', fontWeight: 700, color: s.total_pnl_usdt >= 0 ? 'var(--green)' : 'var(--red)' }}>
                          {(s.total_pnl_usdt || 0) >= 0 ? '+' : ''}{(s.total_pnl_usdt || 0).toFixed(2)}
                        </td>
                        <td className="num" style={{ textAlign: 'right', color: (s.pct_of_equity || 0) >= 0 ? 'var(--green)' : 'var(--red)', fontSize: 11 }}>
                          {s.pct_of_equity != null ? `${s.pct_of_equity >= 0 ? '+' : ''}${s.pct_of_equity.toFixed(1)}%` : '—'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {!stressData.scenarios?.length && (
              <div style={{ fontSize: 12, color: 'var(--t3)' }}>No open positions — scenarios show 0 impact.</div>
            )}
          </div>
        ) : (
          <div style={{ fontSize: 12, color: 'var(--t3)' }}>Click "Run Scenarios" to compute stress test with current positions.</div>
        )}
      </Card>

      {/* ── TCA ─────────────────────────────────────────────────────────── */}
      {tcaStats && tcaStats.length > 0 && (
        <Card style={{ padding: 20 }}>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, marginBottom: 12 }}>
            <div className="section-title">{t('risk_tca_title')}</div>
            <div style={{ fontSize: 11, color: 'var(--t3)' }}>Transaction Cost Analysis</div>
          </div>
          {tcaStats.map((s, i) => (
            <div key={i} style={{ marginBottom: i < tcaStats.length - 1 ? 20 : 0 }}>
              <div style={{ fontSize: 12, fontWeight: 700, color: 'var(--t1)', marginBottom: 10 }}>{s.strategy_id}</div>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(130px, 1fr))', gap: 8, marginBottom: 8 }}>
                <StatTile label="Exec Score" value={`${(s.execution_score || 0).toFixed(0)}/100`}
                  color={s.execution_score >= 80 ? 'var(--green)' : s.execution_score >= 60 ? 'var(--yellow)' : 'var(--red)'} />
                <StatTile label="Mean Slippage" value={`${(s.mean_slippage_bps || 0).toFixed(2)} bps`}
                  color={(s.mean_slippage_bps || 0) >= 0 ? 'var(--green)' : 'var(--red)'} />
                <StatTile label="Maker Rate" value={`${(s.maker_rate_pct || 0).toFixed(1)}%`}
                  color={s.maker_rate_pct >= 20 ? 'var(--green)' : 'var(--t1)'} />
                <StatTile label="Over Budget" value={`${(s.over_budget_rate_pct || 0).toFixed(1)}%`}
                  color={s.over_budget_rate_pct > 20 ? 'var(--red)' : 'var(--t1)'} />
                <StatTile label="Total Cost" value={`${(s.total_cost_usdt || 0).toFixed(4)} USDT`} color="var(--red)" />
                <StatTile label="Fills" value={s.n_fills || 0} />
              </div>
            </div>
          ))}
        </Card>
      )}

      {/* ── Latency Monitor ─────────────────────────────────────────────── */}
      {latency && latency.stats?.length > 0 && (
        <Card style={{ padding: 20 }}>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, marginBottom: 12 }}>
            <div className="section-title">{t('risk_latency_title')}</div>
            <div style={{ fontSize: 11, color: 'var(--t3)' }}>5-minute rolling window</div>
          </div>
          {latency.has_alerts && (
            <Alert variant="error" style={{ marginBottom: 12 }}>⚠ Latency alerts detected</Alert>
          )}
          <div className="table-wrap">
            <table>
              <thead><tr>
                <th>Exchange</th><th>Type</th>
                <th style={{ textAlign: 'right' }}>p50 ms</th>
                <th style={{ textAlign: 'right' }}>p95 ms</th>
                <th style={{ textAlign: 'right' }}>p99 ms</th>
                <th style={{ textAlign: 'right' }}>Max ms</th>
                <th>Status</th>
              </tr></thead>
              <tbody>
                {latency.stats.map((s, i) => (
                  <tr key={i}>
                    <td style={{ textTransform: 'capitalize' }}>{s.exchange}</td>
                    <td style={{ color: 'var(--t3)', fontSize: 11 }}>{s.category?.toUpperCase()}</td>
                    <td className="num" style={{ textAlign: 'right' }}>{s.p50_ms}</td>
                    <td className="num" style={{ textAlign: 'right' }}>{s.p95_ms}</td>
                    <td className="num" style={{ textAlign: 'right', fontWeight: 700, color: s.alert ? 'var(--red)' : s.p99_ms > s.alert_threshold_ms * 0.7 ? 'var(--yellow)' : 'var(--green)' }}>
                      {s.p99_ms}
                    </td>
                    <td className="num" style={{ textAlign: 'right', color: 'var(--t3)' }}>{s.max_ms}</td>
                    <td><AlertBadge level={s.alert ? 'warning' : 'ok'} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </div>
  )
}
