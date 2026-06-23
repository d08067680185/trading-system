import { useState, useEffect, useRef } from 'react'
import { useLang } from '../i18n'
import { Button, Card, PageHeader, StatTile, Alert } from '../components/ui'

function getApiKey() { return localStorage.getItem('trading_api_key') || '' }
function authHeaders() { const k = getApiKey(); return k ? { 'X-API-Key': k } : {} }

/* ── Responsive equity curve ─────────────────────────────────────────────── */
function EquityCurve({ data }) {
  const ref = useRef(null)
  const [width, setWidth] = useState(480)
  const height = 100

  useEffect(() => {
    if (!ref.current) return
    const ro = new ResizeObserver(entries => setWidth(entries[0].contentRect.width))
    ro.observe(ref.current)
    return () => ro.disconnect()
  }, [])

  if (!data || data.length < 2) return (
    <div ref={ref} style={{ height, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--t3)', fontSize: 12 }}>
      No data
    </div>
  )

  const values = data.map(d => d[1])
  const min = Math.min(...values), max = Math.max(...values)
  const range = max - min || 1
  const pts = data.map((d, i) => [
    (i / (data.length - 1)) * width,
    height - ((d[1] - min) / range) * (height - 4) - 2,
  ])
  const line = pts.map((p, i) => `${i === 0 ? 'M' : 'L'}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ')
  const area = `${line} L${width},${height} L0,${height} Z`
  const c = values[values.length - 1] >= values[0] ? '#00d9a3' : '#ff3c5c'

  return (
    <div ref={ref} style={{ width: '100%' }}>
      <svg width="100%" height={height} viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none">
        <defs>
          <linearGradient id="ec-grad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={c} stopOpacity="0.25" />
            <stop offset="100%" stopColor={c} stopOpacity="0" />
          </linearGradient>
        </defs>
        <path d={area} fill="url(#ec-grad)" />
        <path d={line} fill="none" stroke={c} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    </div>
  )
}

/* ── Apply best params to live strategy ─────────────────────────────────── */
function ApplyParamsButton({ strategyId, params, t }) {
  const [status, setStatus] = useState(null)
  const handleApply = async () => {
    setStatus('applying')
    try {
      const res = await fetch(`/api/strategies/${encodeURIComponent(strategyId)}/params`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify(params),
      })
      setStatus(res.ok ? 'ok' : 'error')
      setTimeout(() => setStatus(null), 3000)
    } catch { setStatus('error'); setTimeout(() => setStatus(null), 3000) }
  }
  return (
    <Button variant={status === 'ok' ? 'green' : 'ghost'} size="xs"
      onClick={handleApply} disabled={status === 'applying'}>
      {status === 'ok' ? '✓ Applied' : status === 'error' ? '✕ Failed' : status === 'applying' ? '…' : 'Apply'}
    </Button>
  )
}

/* ── Fetch data panel ─────────────────────────────────────────────────────── */
function FetchDataPanel({ t }) {
  const [form, setForm] = useState({ exchange: 'binance', symbol: 'BTC-USDT', interval: '1h', days: 365 })
  const [status, setStatus] = useState(null)
  const set = (k, v) => setForm(f => ({ ...f, [k]: v }))

  const handleFetch = async () => {
    setStatus('fetching')
    try {
      const res = await fetch('/api/data/fetch', {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify(form),
      })
      const d = await res.json()
      setStatus(res.ok ? `ok:${d.stored}` : 'error:' + (d.detail || 'failed'))
    } catch { setStatus('error:Network error') }
  }

  return (
    <Card style={{ padding: 20 }}>
      <div className="section-title" style={{ marginBottom: 14 }}>{t('fetch_data')}</div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))', gap: 10, marginBottom: 12 }}>
        <div>
          <label className="label">{t('th_exchange')}</label>
          <select value={form.exchange} onChange={e => set('exchange', e.target.value)} style={{ width: '100%', marginTop: 6 }}>
            <option value="binance">Binance</option>
            <option value="okx">OKX</option>
          </select>
        </div>
        <div>
          <label className="label">{t('th_symbol')}</label>
          <input value={form.symbol} onChange={e => set('symbol', e.target.value.toUpperCase())}
            style={{ width: '100%', marginTop: 6 }} placeholder="BTC-USDT" />
        </div>
        <div>
          <label className="label">{t('bt_interval')}</label>
          <select value={form.interval} onChange={e => set('interval', e.target.value)} style={{ width: '100%', marginTop: 6 }}>
            {['1m','5m','15m','30m','1h','4h','1d'].map(v => <option key={v}>{v}</option>)}
          </select>
        </div>
        <div>
          <label className="label">{t('bt_days')}</label>
          <input type="number" value={form.days} min={1} max={1000}
            onChange={e => set('days', parseInt(e.target.value))}
            style={{ width: '100%', marginTop: 6 }} />
        </div>
      </div>
      {status?.startsWith('ok:') && (
        <Alert variant="success" style={{ marginBottom: 10 }}>
          {t('bt_fetched', status.split(':')[1])}
        </Alert>
      )}
      {status?.startsWith('error:') && (
        <Alert variant="error" style={{ marginBottom: 10 }}>{status.slice(6)}</Alert>
      )}
      <Button variant="primary" className="btn-full" onClick={handleFetch} disabled={status === 'fetching'}>
        {status === 'fetching' ? t('bt_downloading') : t('bt_download')}
      </Button>
    </Card>
  )
}

/* ── Run backtest panel ───────────────────────────────────────────────────── */
function RunBacktestPanel({ onJobCreated, t }) {
  const now = Math.floor(Date.now() / 1000)
  const [form, setForm] = useState({
    strategy_id: 'arb_spread', exchange: 'binance', symbol: 'BTC-USDT', interval: '1h',
    start_ts: now - 365 * 86400, end_ts: now, initial_capital: 10000,
    params: '{"min_spread_bps": 5, "cooldown_s": 30}',
    slippage_bps: 0, funding_rate_pct: 0,
  })
  const [status, setStatus] = useState(null)
  const set = (k, v) => setForm(f => ({ ...f, [k]: v }))

  const handleRun = async () => {
    setStatus('running')
    try {
      let params = {}
      try { params = JSON.parse(form.params) } catch { setStatus('error:Invalid JSON params'); return }
      const res = await fetch('/api/backtest/run', {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ ...form, params, slippage_bps: form.slippage_bps, funding_rate_pct: form.funding_rate_pct }),
      })
      const d = await res.json()
      if (res.ok) { setStatus(null); onJobCreated(d.job_id) }
      else setStatus('error:' + (d.detail || 'failed'))
    } catch { setStatus('error:Network error') }
  }

  return (
    <Card style={{ padding: 20 }}>
      <div className="section-title" style={{ marginBottom: 14 }}>{t('bt_run')}</div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))', gap: 10, marginBottom: 12 }}>
        <div>
          <label className="label">{t('bt_strategy')}</label>
          <input value={form.strategy_id} onChange={e => set('strategy_id', e.target.value)}
            style={{ width: '100%', marginTop: 6 }} placeholder="arb_spread" />
        </div>
        <div>
          <label className="label">{t('th_exchange')}</label>
          <select value={form.exchange} onChange={e => set('exchange', e.target.value)} style={{ width: '100%', marginTop: 6 }}>
            <option value="binance">Binance</option>
            <option value="okx">OKX</option>
          </select>
        </div>
        <div>
          <label className="label">{t('th_symbol')}</label>
          <input value={form.symbol} onChange={e => set('symbol', e.target.value.toUpperCase())}
            style={{ width: '100%', marginTop: 6 }} />
        </div>
        <div>
          <label className="label">{t('bt_interval')}</label>
          <select value={form.interval} onChange={e => set('interval', e.target.value)} style={{ width: '100%', marginTop: 6 }}>
            {['1m','5m','15m','30m','1h','4h','1d'].map(v => <option key={v}>{v}</option>)}
          </select>
        </div>
        <div>
          <label className="label">{t('bt_start')}</label>
          <input type="date" style={{ width: '100%', marginTop: 6 }}
            defaultValue={new Date((now - 365 * 86400) * 1000).toISOString().split('T')[0]}
            onChange={e => set('start_ts', Math.floor(new Date(e.target.value).getTime() / 1000))} />
        </div>
        <div>
          <label className="label">{t('bt_end')}</label>
          <input type="date" style={{ width: '100%', marginTop: 6 }}
            defaultValue={new Date(now * 1000).toISOString().split('T')[0]}
            onChange={e => set('end_ts', Math.floor(new Date(e.target.value).getTime() / 1000))} />
        </div>
        <div>
          <label className="label">{t('bt_capital')}</label>
          <input type="number" value={form.initial_capital}
            onChange={e => set('initial_capital', parseFloat(e.target.value))}
            style={{ width: '100%', marginTop: 6 }} />
          <div style={{ fontSize: 10, color: 'var(--t3)', marginTop: 3 }}>
            Minimum viable: SpreadArb 500 · FundingArb 2000 · Grid 300 USDT
          </div>
        </div>
        <div>
          <label className="label">{t('bt_params_json')}</label>
          <input value={form.params} onChange={e => set('params', e.target.value)}
            style={{ width: '100%', marginTop: 6, fontFamily: 'monospace', fontSize: 11 }}
            placeholder='{"key": value}' />
        </div>
        <div>
          <label className="label">Slippage (bps)</label>
          <input type="number" value={form.slippage_bps ?? 0} min={0} max={100}
            onChange={e => set('slippage_bps', parseInt(e.target.value) || 0)}
            style={{ width: '100%', marginTop: 6 }} />
          <div style={{ fontSize: 10, color: 'var(--t3)', marginTop: 3 }}>
            Binance taker: 4 bps · OKX taker: 5 bps · typical slippage: 1–3 bps
          </div>
        </div>
        <div>
          <label className="label">Funding Rate %/8h</label>
          <input type="number" value={form.funding_rate_pct ?? 0} min={0} max={1} step={0.001}
            onChange={e => set('funding_rate_pct', parseFloat(e.target.value) || 0)}
            style={{ width: '100%', marginTop: 6 }} />
        </div>
      </div>
      {status?.startsWith('error:') && (
        <Alert variant="error" style={{ marginBottom: 10 }}>{status.slice(6)}</Alert>
      )}
      <Button variant="primary" className="btn-full" onClick={handleRun} disabled={status === 'running'}>
        {status === 'running' ? t('bt_submitting') : t('bt_run_btn')}
      </Button>
    </Card>
  )
}

/* ── Job result card ──────────────────────────────────────────────────────── */
function JobResultCard({ job, t }) {
  const r = job.result
  if (!r) return null
  const pct = v => `${v >= 0 ? '+' : ''}${(v || 0).toFixed(2)}%`
  const n = v => (v || 0).toFixed(3)

  return (
    <Card style={{ padding: 0, overflow: 'hidden' }}>
      <div style={{
        padding: '14px 20px',
        background: 'linear-gradient(90deg, rgba(59,123,255,0.06) 0%, transparent 70%)',
        borderBottom: '1px solid var(--border)',
        display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 8,
      }}>
        <div>
          <span style={{ fontWeight: 700, color: 'var(--t1)' }}>{job.strategy_id}</span>
          <span style={{ color: 'var(--t3)', fontSize: 11, marginLeft: 10 }}>{job.symbol} · {job.interval}</span>
        </div>
        <span className="badge badge-filled">DONE</span>
      </div>
      <div style={{ padding: 16 }}>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(110px, 1fr))', gap: 8, marginBottom: 8 }}>
          <StatTile label={t('bt_return')} value={pct(r.total_return_pct)}
            color={r.total_return_pct >= 0 ? 'var(--green)' : 'var(--red)'} />
          <StatTile label="Sharpe" value={n(r.sharpe_ratio)}
            color={r.sharpe_ratio >= 1 ? 'var(--green)' : r.sharpe_ratio >= 0 ? 'var(--yellow)' : 'var(--red)'} />
          <StatTile label={t('bt_drawdown')} value={pct(-r.max_drawdown_pct)} color="var(--red)" />
          <StatTile label={t('win_rate')} value={pct(r.win_rate * 100)} />
          <StatTile label="Sortino" value={n(r.sortino_ratio)} />
          <StatTile label="Calmar"  value={n(r.calmar_ratio)} />
          <StatTile label={t('bt_trades')} value={r.total_trades} />
          <StatTile label={t('bt_ann_return')} value={pct(r.annualized_return_pct)}
            color={r.annualized_return_pct >= 0 ? 'var(--green)' : 'var(--red)'} />
        </div>
        {r.equity_curve?.length > 1 && (
          <div style={{ borderRadius: 8, overflow: 'hidden', background: 'var(--surface)', padding: 12 }}>
            <div className="label" style={{ marginBottom: 8 }}>{t('bt_equity_curve')}</div>
            <EquityCurve data={r.equity_curve} />
          </div>
        )}
      </div>
    </Card>
  )
}

/* ── Optimizer panel ──────────────────────────────────────────────────────── */
function OptimizerPanel({ onJobCreated, t }) {
  const now = Math.floor(Date.now() / 1000)
  const [method, setMethod] = useState('grid')
  const [form, setForm] = useState({
    strategy_id: 'arb_spread', exchange: 'binance', symbol: 'BTC-USDT', interval: '1h',
    start_ts: now - 365 * 86400, end_ts: now, initial_capital: 10000,
    n_calls: 30, metric: 'sharpe_ratio',
    param_grid: '{"min_spread_bps": [3, 5, 8, 12], "cooldown_s": [20, 30, 60]}',
    param_bounds: '{"min_spread_bps": [2, 15], "cooldown_s": [10, 120]}',
  })
  const [status, setStatus] = useState(null)
  const set = (k, v) => setForm(f => ({ ...f, [k]: v }))

  const handleRun = async () => {
    setStatus('running')
    const isGrid = method === 'grid'
    const endpoint = isGrid ? '/api/optimizer/grid' : '/api/optimizer/bayesian'
    try {
      let parsed
      try { parsed = JSON.parse(isGrid ? form.param_grid : form.param_bounds) }
      catch { setStatus('error:Invalid JSON'); return }

      const body = {
        strategy_id: form.strategy_id, exchange: form.exchange,
        symbol: form.symbol, interval: form.interval,
        start_ts: form.start_ts, end_ts: form.end_ts,
        initial_capital: form.initial_capital, metric: form.metric,
        ...(isGrid ? { param_grid: parsed } : { param_bounds: parsed, n_calls: form.n_calls }),
      }
      const res = await fetch(endpoint, {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify(body),
      })
      const d = await res.json()
      if (res.ok) { setStatus(null); onJobCreated(d.job_id, method) }
      else setStatus('error:' + (d.detail || 'failed'))
    } catch { setStatus('error:Network error') }
  }

  return (
    <Card style={{ padding: 20 }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 14, flexWrap: 'wrap', gap: 8 }}>
        <div className="section-title">{t('opt_title')}</div>
        <div style={{ display: 'flex', gap: 6 }}>
          {['grid', 'bayesian'].map(m => (
            <Button key={m} size="xs" variant={method === m ? 'primary' : 'ghost'} onClick={() => setMethod(m)}>
              {m === 'grid' ? t('opt_grid') : t('opt_bayesian')}
            </Button>
          ))}
        </div>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(160px, 1fr))', gap: 10, marginBottom: 12 }}>
        <div>
          <label className="label">{t('bt_strategy')}</label>
          <input value={form.strategy_id} onChange={e => set('strategy_id', e.target.value)}
            style={{ width: '100%', marginTop: 6 }} placeholder="arb_spread" />
        </div>
        <div>
          <label className="label">{t('th_exchange')}</label>
          <select value={form.exchange} onChange={e => set('exchange', e.target.value)} style={{ width: '100%', marginTop: 6 }}>
            <option value="binance">Binance</option>
            <option value="okx">OKX</option>
          </select>
        </div>
        <div>
          <label className="label">{t('th_symbol')}</label>
          <input value={form.symbol} onChange={e => set('symbol', e.target.value.toUpperCase())}
            style={{ width: '100%', marginTop: 6 }} />
        </div>
        <div>
          <label className="label">{t('bt_interval')}</label>
          <select value={form.interval} onChange={e => set('interval', e.target.value)} style={{ width: '100%', marginTop: 6 }}>
            {['1m','5m','15m','30m','1h','4h','1d'].map(v => <option key={v}>{v}</option>)}
          </select>
        </div>
        <div>
          <label className="label">{t('bt_start')}</label>
          <input type="date" style={{ width: '100%', marginTop: 6 }}
            defaultValue={new Date((now - 365 * 86400) * 1000).toISOString().split('T')[0]}
            onChange={e => set('start_ts', Math.floor(new Date(e.target.value).getTime() / 1000))} />
        </div>
        <div>
          <label className="label">{t('bt_end')}</label>
          <input type="date" style={{ width: '100%', marginTop: 6 }}
            defaultValue={new Date(now * 1000).toISOString().split('T')[0]}
            onChange={e => set('end_ts', Math.floor(new Date(e.target.value).getTime() / 1000))} />
        </div>
        <div>
          <label className="label">{t('bt_capital')}</label>
          <input type="number" value={form.initial_capital}
            onChange={e => set('initial_capital', parseFloat(e.target.value))}
            style={{ width: '100%', marginTop: 6 }} />
        </div>
        <div>
          <label className="label">{t('opt_metric')}</label>
          <select value={form.metric} onChange={e => set('metric', e.target.value)} style={{ width: '100%', marginTop: 6 }}>
            <option value="sharpe_ratio">Sharpe Ratio</option>
            <option value="total_return_pct">Total Return</option>
            <option value="sortino_ratio">Sortino</option>
            <option value="calmar_ratio">Calmar</option>
          </select>
        </div>
        {method === 'bayesian' && (
          <div>
            <label className="label">{t('opt_n_calls')}</label>
            <input type="number" value={form.n_calls} min={5} max={200}
              onChange={e => set('n_calls', parseInt(e.target.value))}
              style={{ width: '100%', marginTop: 6 }} />
          </div>
        )}
      </div>

      <div style={{ marginBottom: 12 }}>
        <label className="label">{method === 'grid' ? t('opt_param_grid') : t('opt_param_bounds')}</label>
        <textarea value={method === 'grid' ? form.param_grid : form.param_bounds}
          onChange={e => set(method === 'grid' ? 'param_grid' : 'param_bounds', e.target.value)}
          rows={3} style={{ width: '100%', marginTop: 6, fontFamily: 'monospace', fontSize: 11, resize: 'vertical' }}
          placeholder={method === 'grid' ? t('opt_grid_hint') : t('opt_bounds_hint')} />
      </div>

      {status?.startsWith('error:') && (
        <Alert variant="error" style={{ marginBottom: 10 }}>{status.slice(6)}</Alert>
      )}
      <Button variant="primary" className="btn-full" onClick={handleRun} disabled={status === 'running'}>
        {status === 'running' ? t('opt_running') : t('opt_run_btn')}
      </Button>
    </Card>
  )
}

/* ── Optimizer result card ────────────────────────────────────────────────── */
function OptJobCard({ job, t }) {
  const pct = v => `${(v >= 0 ? '+' : '')}${(v || 0).toFixed(2)}%`
  const n3  = v => (v || 0).toFixed(3)

  return (
    <Card style={{ padding: 0, overflow: 'hidden' }}>
      <div style={{
        padding: '12px 20px', borderBottom: '1px solid var(--border)',
        display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 8,
        background: 'linear-gradient(90deg, rgba(255,200,0,0.05) 0%, transparent 70%)',
      }}>
        <div>
          <span style={{ fontWeight: 700, color: 'var(--t1)' }}>{job.strategy_id}</span>
          <span style={{ fontSize: 10, color: 'var(--t3)', marginLeft: 8 }}>
            {job.method === 'grid' ? t('opt_grid') : t('opt_bayesian')} · {job.runs || 0} {t('opt_runs')}
          </span>
        </div>
        <span className={`badge ${job.status === 'done' ? 'badge-filled' : 'badge-open'}`}>
          {job.status?.toUpperCase()}
        </span>
      </div>

      {job.status === 'running' && (
        <div style={{ padding: '10px 20px' }}>
          <div style={{ height: 3, borderRadius: 2, background: 'var(--border)', overflow: 'hidden' }}>
            <div style={{ height: '100%', width: `${(job.progress || 0) * 100}%`, background: 'var(--accent)', transition: 'width 0.5s' }} />
          </div>
          <div style={{ fontSize: 11, color: 'var(--t3)', marginTop: 5 }}>
            {((job.progress || 0) * 100).toFixed(0)}% · {job.runs || 0} {t('opt_runs')}
          </div>
        </div>
      )}

      {job.status === 'done' && (
        <div style={{ padding: 16 }}>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(110px, 1fr))', gap: 8, marginBottom: 12 }}>
            <StatTile label={t('opt_best_sharpe')} value={n3(job.best_sharpe)}
              color={job.best_sharpe >= 1 ? 'var(--green)' : job.best_sharpe >= 0 ? 'var(--yellow)' : 'var(--red)'} />
            <StatTile label={t('opt_best_return')} value={pct(job.best_return)}
              color={job.best_return >= 0 ? 'var(--green)' : 'var(--red)'} />
            <StatTile label={t('opt_runs')} value={job.runs || 0} />
          </div>
          {job.best_params && Object.keys(job.best_params).length > 0 && (
            <div style={{ background: 'var(--surface)', borderRadius: 8, padding: '10px 14px', border: '1px solid var(--border)' }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
                <div className="label">{t('opt_best_params')}</div>
                <ApplyParamsButton strategyId={job.strategy_id} params={job.best_params} t={t} />
              </div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                {Object.entries(job.best_params).map(([k, v]) => (
                  <span key={k} style={{
                    padding: '3px 8px', borderRadius: 6, fontSize: 11, fontFamily: 'monospace',
                    background: 'rgba(59,123,255,0.08)', border: '1px solid rgba(59,123,255,0.15)',
                    color: 'var(--accent)',
                  }}>
                    {k}: {typeof v === 'number' ? v.toFixed(2) : v}
                  </span>
                ))}
              </div>
            </div>
          )}
          {job.top_results?.length > 0 && (
            <div style={{ marginTop: 12 }}>
              <div className="label" style={{ marginBottom: 8 }}>Top Results</div>
              {/* 2-param heatmap */}
              {(() => {
                const paramKeys = Object.keys(job.top_results[0]?.params || {})
                if (paramKeys.length !== 2 || job.top_results.length < 4) return null
                const [xKey, yKey] = paramKeys
                const xVals = [...new Set(job.top_results.map(r => r.params[xKey]))].sort((a,b)=>a-b)
                const yVals = [...new Set(job.top_results.map(r => r.params[yKey]))].sort((a,b)=>a-b)
                const lookup = {}
                job.top_results.forEach(r => { lookup[`${r.params[xKey]}_${r.params[yKey]}`] = r.sharpe || 0 })
                const allS = job.top_results.map(r => r.sharpe || 0)
                const minS = Math.min(...allS), maxS = Math.max(...allS)
                const range = maxS - minS || 1
                const cellColor = s => {
                  const t2 = (s - minS) / range
                  const r = Math.round(255 * (1 - t2)), g = Math.round(180 * t2), b = Math.round(100 * (1 - Math.abs(t2 - 0.5) * 2))
                  return `rgba(${r},${g},${b},0.7)`
                }
                return (
                  <div style={{ marginBottom: 12 }}>
                    <div className="label" style={{ marginBottom: 6 }}>Parameter Heatmap (Sharpe)</div>
                    <div style={{ overflowX: 'auto', overflowY: 'hidden' }}>
                      <table style={{ borderCollapse: 'collapse', fontSize: 10 }}>
                        <thead>
                          <tr>
                            <th style={{ padding: '3px 8px', color: 'var(--t3)', fontWeight: 400, textAlign: 'left' }}>{yKey} \ {xKey}</th>
                            {xVals.map(x => <th key={x} style={{ padding: '3px 6px', color: 'var(--t3)', fontWeight: 400 }}>{typeof x === 'number' ? x.toFixed(1) : x}</th>)}
                          </tr>
                        </thead>
                        <tbody>
                          {yVals.map(y => (
                            <tr key={y}>
                              <td style={{ padding: '3px 8px', color: 'var(--t3)', fontFamily: 'monospace' }}>{typeof y === 'number' ? y.toFixed(1) : y}</td>
                              {xVals.map(x => {
                                const s = lookup[`${x}_${y}`]
                                return (
                                  <td key={x} title={`${xKey}=${x}, ${yKey}=${y}: Sharpe=${s?.toFixed(3)}`}
                                    style={{
                                      padding: '4px 6px', textAlign: 'center', fontFamily: 'monospace',
                                      background: s != null ? cellColor(s) : 'var(--bg2)',
                                      borderRadius: 4, border: '1px solid var(--bg)',
                                    }}>
                                    {s != null ? s.toFixed(2) : '—'}
                                  </td>
                                )
                              })}
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                )
              })()}
              <div className="table-wrap">
                <table>
                  <thead><tr>
                    {Object.keys(job.top_results[0].params || {}).map(k => <th key={k}>{k}</th>)}
                    <th style={{ textAlign: 'right' }}>Sharpe</th>
                    <th style={{ textAlign: 'right' }}>Return</th>
                  </tr></thead>
                  <tbody>
                    {job.top_results.slice(0, 5).map((r, i) => (
                      <tr key={i}
                        onMouseEnter={e => e.currentTarget.style.background = 'rgba(59,123,255,0.03)'}
                        onMouseLeave={e => e.currentTarget.style.background = ''}>
                        {Object.values(r.params || {}).map((v, j) => (
                          <td key={j} className="num" style={{ fontFamily: 'monospace', fontSize: 11 }}>
                            {typeof v === 'number' ? v.toFixed(2) : v}
                          </td>
                        ))}
                        <td className="num" style={{ textAlign: 'right', color: r.sharpe >= 1 ? 'var(--green)' : r.sharpe >= 0 ? 'var(--yellow)' : 'var(--red)' }}>
                          {(r.sharpe || 0).toFixed(3)}
                        </td>
                        <td className="num" style={{ textAlign: 'right', color: (r.total_return_pct || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>
                          {(r.total_return_pct || 0) >= 0 ? '+' : ''}{(r.total_return_pct || 0).toFixed(2)}%
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}
    </Card>
  )
}

/* ── Gap-fill button ─────────────────────────────────────────────────────── */
function GapFillButton({ exchange, symbol, interval, t }) {
  const [status, setStatus] = useState(null)

  const handleFill = async () => {
    setStatus('filling')
    try {
      const res = await fetch(`/api/data/fill-gaps/${exchange}/${encodeURIComponent(symbol)}?interval=${interval}`, {
        method: 'POST', headers: authHeaders(),
      })
      const d = await res.json()
      setStatus(d)
      setTimeout(() => setStatus(null), 4000)
    } catch { setStatus(null) }
  }

  if (status === 'filling') return <span style={{ fontSize: 10, color: 'var(--t3)' }}>Filling…</span>
  if (status && typeof status === 'object') {
    return (
      <span style={{ fontSize: 10, color: status.candles_added > 0 ? 'var(--green)' : 'var(--t3)' }}>
        +{status.candles_added} filled
      </span>
    )
  }
  return <Button size="xs" variant="ghost" onClick={handleFill} title="Auto-fill data gaps">Fill Gaps</Button>
}

/* ── Walk-forward panel ───────────────────────────────────────────────────── */
function WalkForwardPanel({ onJobCreated, t }) {
  const now = Math.floor(Date.now() / 1000)
  const [method, setMethod] = useState('grid')
  const [form, setForm] = useState({
    strategy_id: 'arb_spread', exchange: 'binance', symbol: 'BTC-USDT', interval: '1h',
    start_ts: now - 365 * 86400, end_ts: now, initial_capital: 10000,
    n_folds: 5, train_frac: 0.7,
    param_grid: '{"min_spread_bps": [3, 5, 8, 12], "cooldown_s": [20, 30, 60]}',
    param_bounds: '{"min_spread_bps": [2, 15], "cooldown_s": [10, 120]}',
  })
  const [status, setStatus] = useState(null)
  const set = (k, v) => setForm(f => ({ ...f, [k]: v }))

  const handleRun = async () => {
    setStatus('running')
    const isGrid = method === 'grid'
    try {
      let parsed
      try { parsed = JSON.parse(isGrid ? form.param_grid : form.param_bounds) }
      catch { setStatus('error:Invalid JSON'); return }
      const body = {
        strategy_id: form.strategy_id, exchange: form.exchange,
        symbol: form.symbol, interval: form.interval,
        start_ts: form.start_ts, end_ts: form.end_ts,
        initial_capital: form.initial_capital,
        n_folds: form.n_folds, train_frac: form.train_frac,
        method,
        ...(isGrid ? { param_grid: parsed } : { param_bounds: parsed }),
      }
      const res = await fetch('/api/optimizer/walk-forward', {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify(body),
      })
      const d = await res.json()
      if (res.ok) { setStatus(null); onJobCreated(d.job_id) }
      else setStatus('error:' + (d.detail || 'failed'))
    } catch { setStatus('error:Network error') }
  }

  return (
    <Card style={{ padding: 20 }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 14, flexWrap: 'wrap', gap: 8 }}>
        <div className="section-title">{t('wf_title')}</div>
        <div style={{ display: 'flex', gap: 6 }}>
          {['grid', 'bayesian'].map(m => (
            <Button key={m} size="xs" variant={method === m ? 'primary' : 'ghost'} onClick={() => setMethod(m)}>
              {m === 'grid' ? t('opt_grid') : t('opt_bayesian')}
            </Button>
          ))}
        </div>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(160px, 1fr))', gap: 10, marginBottom: 12 }}>
        <div>
          <label className="label">{t('bt_strategy')}</label>
          <input value={form.strategy_id} onChange={e => set('strategy_id', e.target.value)}
            style={{ width: '100%', marginTop: 6 }} placeholder="arb_spread" />
        </div>
        <div>
          <label className="label">{t('th_exchange')}</label>
          <select value={form.exchange} onChange={e => set('exchange', e.target.value)} style={{ width: '100%', marginTop: 6 }}>
            <option value="binance">Binance</option>
            <option value="okx">OKX</option>
          </select>
        </div>
        <div>
          <label className="label">{t('th_symbol')}</label>
          <input value={form.symbol} onChange={e => set('symbol', e.target.value.toUpperCase())}
            style={{ width: '100%', marginTop: 6 }} />
        </div>
        <div>
          <label className="label">{t('bt_interval')}</label>
          <select value={form.interval} onChange={e => set('interval', e.target.value)} style={{ width: '100%', marginTop: 6 }}>
            {['1m','5m','15m','30m','1h','4h','1d'].map(v => <option key={v}>{v}</option>)}
          </select>
        </div>
        <div>
          <label className="label">{t('bt_start')}</label>
          <input type="date" style={{ width: '100%', marginTop: 6 }}
            defaultValue={new Date((now - 365 * 86400) * 1000).toISOString().split('T')[0]}
            onChange={e => set('start_ts', Math.floor(new Date(e.target.value).getTime() / 1000))} />
        </div>
        <div>
          <label className="label">{t('bt_end')}</label>
          <input type="date" style={{ width: '100%', marginTop: 6 }}
            defaultValue={new Date(now * 1000).toISOString().split('T')[0]}
            onChange={e => set('end_ts', Math.floor(new Date(e.target.value).getTime() / 1000))} />
        </div>
        <div>
          <label className="label">{t('bt_capital')}</label>
          <input type="number" value={form.initial_capital}
            onChange={e => set('initial_capital', parseFloat(e.target.value))}
            style={{ width: '100%', marginTop: 6 }} />
        </div>
        <div>
          <label className="label">{t('wf_folds')}</label>
          <input type="number" value={form.n_folds} min={2} max={20}
            onChange={e => set('n_folds', parseInt(e.target.value))}
            style={{ width: '100%', marginTop: 6 }} />
        </div>
        <div>
          <label className="label">{t('wf_train_frac')}</label>
          <input type="number" value={form.train_frac} min={0.3} max={0.9} step={0.05}
            onChange={e => set('train_frac', parseFloat(e.target.value))}
            style={{ width: '100%', marginTop: 6 }} />
        </div>
      </div>

      <div style={{ marginBottom: 12 }}>
        <label className="label">{method === 'grid' ? t('opt_param_grid') : t('opt_param_bounds')}</label>
        <textarea value={method === 'grid' ? form.param_grid : form.param_bounds}
          onChange={e => set(method === 'grid' ? 'param_grid' : 'param_bounds', e.target.value)}
          rows={2} style={{ width: '100%', marginTop: 6, fontFamily: 'monospace', fontSize: 11, resize: 'vertical' }}
          placeholder={method === 'grid' ? t('opt_grid_hint') : t('opt_bounds_hint')} />
      </div>

      {status?.startsWith('error:') && (
        <Alert variant="error" style={{ marginBottom: 10 }}>{status.slice(6)}</Alert>
      )}
      <Button variant="primary" className="btn-full" onClick={handleRun} disabled={status === 'running'}>
        {status === 'running' ? t('wf_running') : t('wf_run_btn')}
      </Button>
    </Card>
  )
}

/* ── Walk-forward job card ────────────────────────────────────────────────── */
function WFJobCard({ job, t }) {
  const fmtDate = ts => ts ? new Date(ts * 1000).toLocaleDateString() : '—'
  const pct = v => `${(v >= 0 ? '+' : '')}${(v || 0).toFixed(2)}%`
  const n3 = v => (v || 0).toFixed(3)

  return (
    <Card style={{ padding: 0, overflow: 'hidden' }}>
      <div style={{
        padding: '12px 20px', borderBottom: '1px solid var(--border)',
        display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 8,
        background: 'linear-gradient(90deg, rgba(124,58,237,0.07) 0%, transparent 70%)',
      }}>
        <div>
          <span style={{ fontWeight: 700, color: 'var(--t1)' }}>{job.strategy_id}</span>
          <span style={{ fontSize: 10, color: 'var(--t3)', marginLeft: 8 }}>
            Walk-Forward · {job.n_folds || '?'} folds
          </span>
        </div>
        <span className={`badge ${job.status === 'done' ? 'badge-filled' : 'badge-open'}`}
          style={job.status !== 'done' ? { background: 'rgba(124,58,237,0.12)', color: '#a78bfa', borderColor: 'rgba(124,58,237,0.2)' } : undefined}>
          {job.status?.toUpperCase()}
        </span>
      </div>

      {job.status === 'running' && (
        <div style={{ padding: '10px 20px' }}>
          <div style={{ height: 3, borderRadius: 2, background: 'var(--border)', overflow: 'hidden' }}>
            <div style={{ height: '100%', width: `${(job.progress || 0) * 100}%`, background: '#7c3aed', transition: 'width 0.5s' }} />
          </div>
          <div style={{ fontSize: 11, color: 'var(--t3)', marginTop: 5 }}>
            {t('wf_running')} {((job.progress || 0) * 100).toFixed(0)}%
          </div>
        </div>
      )}

      {job.status === 'done' && job.aggregate && (
        <div style={{ padding: 16 }}>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(110px, 1fr))', gap: 8, marginBottom: 14 }}>
            <StatTile label={t('wf_oos_sharpe')} value={n3(job.aggregate.mean_oos_sharpe)}
              color={job.aggregate.mean_oos_sharpe >= 1 ? 'var(--green)' : job.aggregate.mean_oos_sharpe >= 0 ? 'var(--yellow)' : 'var(--red)'} />
            <StatTile label={t('wf_oos_return')} value={pct(job.aggregate.mean_oos_return)}
              color={job.aggregate.mean_oos_return >= 0 ? 'var(--green)' : 'var(--red)'} />
            <StatTile label={t('wf_folds')} value={job.aggregate.n_folds || 0} />
          </div>

          {job.folds?.length > 0 && (
            <div className="table-wrap">
              <table>
                <thead><tr>
                  <th>{t('wf_fold_label', '')}</th>
                  <th>{t('wf_train_period')}</th>
                  <th>{t('wf_test_period')}</th>
                  <th style={{ textAlign: 'right' }}>OOS Sharpe</th>
                  <th style={{ textAlign: 'right' }}>OOS Return</th>
                  <th>{t('opt_best_params')}</th>
                </tr></thead>
                <tbody>
                  {job.folds.map((fold, i) => (
                    <tr key={i}
                      onMouseEnter={e => e.currentTarget.style.background = 'rgba(124,58,237,0.03)'}
                      onMouseLeave={e => e.currentTarget.style.background = ''}>
                      <td style={{ fontWeight: 600, color: '#a78bfa' }}>#{fold.fold}</td>
                      <td style={{ fontSize: 11, color: 'var(--t3)' }}>
                        {fmtDate(fold.train_start)} – {fmtDate(fold.train_end)}
                      </td>
                      <td style={{ fontSize: 11, color: 'var(--t3)' }}>
                        {fmtDate(fold.test_start)} – {fmtDate(fold.test_end)}
                      </td>
                      <td className="num" style={{ textAlign: 'right', color: (fold.oos_metrics?.sharpe_ratio || 0) >= 1 ? 'var(--green)' : (fold.oos_metrics?.sharpe_ratio || 0) >= 0 ? 'var(--yellow)' : 'var(--red)' }}>
                        {n3(fold.oos_metrics?.sharpe_ratio)}
                      </td>
                      <td className="num" style={{ textAlign: 'right', color: (fold.oos_metrics?.total_return_pct || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>
                        {pct(fold.oos_metrics?.total_return_pct)}
                      </td>
                      <td style={{ fontSize: 10, fontFamily: 'monospace', color: 'var(--accent)' }}>
                        {fold.best_params ? Object.entries(fold.best_params).map(([k,v]) => `${k}:${typeof v === 'number' ? v.toFixed(1) : v}`).join(' ') : '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {job.status === 'error' && (
        <div style={{ padding: '10px 20px', fontSize: 12, color: 'var(--red)' }}>✕ {job.error}</div>
      )}
    </Card>
  )
}

/* ── Main page ────────────────────────────────────────────────────────────── */
export default function BacktestPage() {
  const { t } = useLang()
  const [jobs, setJobs]           = useState([])
  const [optJobs, setOptJobs]     = useState([])
  const [wfJobs, setWfJobs]       = useState([])
  const [availData, setAvailData] = useState([])
  const [activeJobId, setActiveJobId]     = useState(null)
  const [activeOptId, setActiveOptId]     = useState(null)
  const [activeWfId, setActiveWfId]       = useState(null)

  const loadJobs = async () => {
    try {
      const h = authHeaders()
      const [bt, opt, wf] = await Promise.all([
        fetch('/api/backtest/jobs', { headers: h }).then(r => r.json()),
        fetch('/api/optimizer/jobs', { headers: h }).then(r => r.json()),
        fetch('/api/optimizer/walk-forward/jobs', { headers: h }).then(r => r.json()).catch(() => []),
      ])
      setJobs(Array.isArray(bt) ? bt : [])
      setOptJobs(Array.isArray(opt) ? opt : [])
      setWfJobs(Array.isArray(wf) ? wf : [])
    } catch { /* ignore */ }
  }

  const loadAvailData = async () => {
    try {
      const d = await fetch('/api/data/symbols', { headers: authHeaders() }).then(r => r.json())
      setAvailData(Array.isArray(d) ? d : [])
    } catch { /* ignore */ }
  }

  useEffect(() => { loadJobs(); loadAvailData() }, [])

  useEffect(() => {
    if (!activeJobId) return
    let delay = 1000
    let tid
    const poll = async () => {
      try {
        const d = await fetch(`/api/backtest/${activeJobId}`, { headers: authHeaders() }).then(r => r.json())
        setJobs(prev => prev.map(j => j.job_id === activeJobId ? { ...j, ...d } : j))
        if (d.status === 'done' || d.status === 'error') { setActiveJobId(null); return }
      } catch { /* ignore */ }
      delay = Math.min(delay * 1.5, 8000)
      tid = setTimeout(poll, delay)
    }
    tid = setTimeout(poll, delay)
    return () => clearTimeout(tid)
  }, [activeJobId])

  useEffect(() => {
    if (!activeOptId) return
    let delay = 1000
    let tid
    const poll = async () => {
      try {
        const d = await fetch(`/api/optimizer/${activeOptId}`, { headers: authHeaders() }).then(r => r.json())
        setOptJobs(prev => prev.map(j => j.job_id === activeOptId ? { ...j, ...d } : j))
        if (d.status === 'done' || d.status === 'error') { setActiveOptId(null); return }
      } catch { /* ignore */ }
      delay = Math.min(delay * 1.5, 10000)
      tid = setTimeout(poll, delay)
    }
    tid = setTimeout(poll, delay)
    return () => clearTimeout(tid)
  }, [activeOptId])

  useEffect(() => {
    if (!activeWfId) return
    let delay = 2000
    let tid
    const poll = async () => {
      try {
        const d = await fetch(`/api/optimizer/walk-forward/${activeWfId}`, { headers: authHeaders() }).then(r => r.json())
        setWfJobs(prev => prev.map(j => j.job_id === activeWfId ? { ...j, ...d } : j))
        if (d.status === 'done' || d.status === 'error') { setActiveWfId(null); return }
      } catch { /* ignore */ }
      delay = Math.min(delay * 1.5, 12000)
      tid = setTimeout(poll, delay)
    }
    tid = setTimeout(poll, delay)
    return () => clearTimeout(tid)
  }, [activeWfId])

  const handleJobCreated = job_id => {
    setActiveJobId(job_id)
    setJobs(prev => [{ job_id, status: 'running', progress: 0, strategy_id: '…', symbol: '…', interval: '…' }, ...prev])
  }

  const handleOptJobCreated = (job_id, method) => {
    setActiveOptId(job_id)
    setOptJobs(prev => [{ job_id, status: 'running', progress: 0, runs: 0, strategy_id: '…', method }, ...prev])
  }

  const handleWfJobCreated = job_id => {
    setActiveWfId(job_id)
    setWfJobs(prev => [{ job_id, status: 'running', progress: 0, strategy_id: '…' }, ...prev])
  }

  return (
    <div className="page">
      <PageHeader title={t('bt_title')}>
        {availData.length > 0 && (
          <span style={{ fontSize: 12, color: 'var(--t2)' }}>{t('bt_datasets', availData.length)}</span>
        )}
        <Button variant="ghost" size="sm" onClick={() => { loadJobs(); loadAvailData() }}>
          ↻ {t('bt_refresh')}
        </Button>
      </PageHeader>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(min(100%, 380px), 1fr))', gap: 20, alignItems: 'start' }}>
        <FetchDataPanel t={t} />
        <RunBacktestPanel onJobCreated={handleJobCreated} t={t} />
      </div>

      <OptimizerPanel onJobCreated={handleOptJobCreated} t={t} />
      <WalkForwardPanel onJobCreated={handleWfJobCreated} t={t} />

      {/* Available datasets */}
      {availData.length > 0 && (
        <Card style={{ overflow: 'hidden' }}>
          <div className="card-header">
            <span className="section-title">{t('bt_available_data')}</span>
          </div>
          <div className="table-wrap">
            <table>
              <thead><tr>
                <th>{t('th_exchange')}</th>
                <th>{t('th_symbol')}</th>
                <th>Interval</th>
                <th style={{ textAlign: 'right' }}>Bars</th>
                <th>From</th>
                <th>To</th>
                <th></th>
              </tr></thead>
              <tbody>
                {availData.map((d, i) => (
                  <tr key={i}
                    onMouseEnter={e => e.currentTarget.style.background = 'rgba(59,123,255,0.03)'}
                    onMouseLeave={e => e.currentTarget.style.background = ''}>
                    <td style={{ textTransform: 'capitalize' }}>{d.exchange}</td>
                    <td style={{ fontWeight: 600 }}>{d.symbol}</td>
                    <td style={{ color: 'var(--t2)' }}>{d.interval}</td>
                    <td className="num" style={{ textAlign: 'right' }}>{d.bars?.toLocaleString()}</td>
                    <td style={{ color: 'var(--t3)', fontSize: 11 }}>
                      {d.start_ts ? new Date(d.start_ts * 1000).toLocaleDateString() : '—'}
                    </td>
                    <td style={{ color: 'var(--t3)', fontSize: 11 }}>
                      {d.end_ts ? new Date(d.end_ts * 1000).toLocaleDateString() : '—'}
                    </td>
                    <td style={{ textAlign: 'right' }}>
                      <GapFillButton exchange={d.exchange} symbol={d.symbol} interval={d.interval} t={t} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {wfJobs.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <div className="section-title">{t('wf_jobs')}</div>
          {wfJobs.map(job => <WFJobCard key={job.job_id} job={job} t={t} />)}
        </div>
      )}

      {optJobs.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <div className="section-title">{t('opt_jobs')}</div>
          {optJobs.map(job => <OptJobCard key={job.job_id} job={job} t={t} />)}
        </div>
      )}

      {jobs.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <div className="section-title">{t('bt_jobs')}</div>
          {jobs.map(job => (
            <div key={job.job_id}>
              {job.status === 'running' && (
                <Card style={{ padding: 16 }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 10, flexWrap: 'wrap', gap: 6 }}>
                    <span style={{ fontWeight: 600, color: 'var(--t1)' }}>{job.strategy_id} · {job.symbol}</span>
                    <span style={{ fontSize: 11, color: 'var(--accent)' }}>{t('bt_running')}</span>
                  </div>
                  <div style={{ height: 4, borderRadius: 2, background: 'var(--border)', overflow: 'hidden' }}>
                    <div style={{
                      height: '100%', width: `${(job.progress || 0) * 100}%`,
                      background: 'var(--accent)', transition: 'width 0.3s', borderRadius: 2,
                    }} />
                  </div>
                  <div style={{ fontSize: 11, color: 'var(--t3)', marginTop: 6 }}>
                    {((job.progress || 0) * 100).toFixed(0)}%
                  </div>
                </Card>
              )}
              {job.status === 'error' && (
                <Card style={{ padding: 16, border: '1px solid rgba(255,60,92,0.2)' }}>
                  <span style={{ color: 'var(--red)', fontSize: 12 }}>✕ {job.strategy_id} — {job.error}</span>
                </Card>
              )}
              {job.status === 'done' && job.result && <JobResultCard job={job} t={t} />}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
