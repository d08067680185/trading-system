import { useState, useCallback } from 'react'
import { useLang } from '../i18n'
import { Button, PageHeader, Badge, Card, CardHeader, EmptyState } from '../components/ui'

function getApiKey() { return localStorage.getItem('trading_api_key') || '' }

async function apiFetch(path, opts = {}) {
  const key = getApiKey()
  const headers = { 'Content-Type': 'application/json', ...(key ? { 'X-API-Key': key } : {}) }
  const res = await fetch(`/api${path}`, { headers, ...opts })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

// ── Strategy card sub-components ─────────────────────────────────────────────

function ParamRow({ label, value }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '5px 0', borderBottom: '1px solid var(--border)' }}>
      <span style={{ fontSize: 11, color: 'var(--t3)' }}>{label}</span>
      <span className="num" style={{ fontSize: 12, fontWeight: 600, color: 'var(--t1)' }}>{value}</span>
    </div>
  )
}

function ParamEditor({ stratId, params, meta, onSaved }) {
  const [local, setLocal] = useState(() => ({ ...params }))
  const [saving, setSaving] = useState(false)
  const [flash, setFlash] = useState('')
  const { t } = useLang()

  const save = async () => {
    setSaving(true)
    try {
      await apiFetch(`/strategies/${stratId}/params`, {
        method: 'POST',
        body: JSON.stringify({ params: local }),
      })
      setFlash('✓ Saved')
      setTimeout(() => setFlash(''), 2000)
      onSaved?.()
    } catch (e) {
      setFlash(`Error: ${e.message}`)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div style={{ borderTop: '1px solid var(--border)', padding: '16px 20px', background: 'var(--surface)' }}>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(160px, 1fr))', gap: 10, marginBottom: 12 }}>
        {meta.map(({ key, label, type, options }) => (
          <div key={key}>
            <label className="label" style={{ marginBottom: 4, display: 'block' }}>{label}</label>
            {type === 'select' ? (
              <select value={local[key] ?? ''} onChange={e => setLocal(p => ({ ...p, [key]: e.target.value }))}
                style={{ width: '100%' }}>
                {options.map(o => <option key={o} value={o}>{o}</option>)}
              </select>
            ) : type === 'boolean' ? (
              <select value={local[key] ? 'true' : 'false'} onChange={e => setLocal(p => ({ ...p, [key]: e.target.value === 'true' }))}
                style={{ width: '100%' }}>
                <option value="true">Yes</option>
                <option value="false">No</option>
              </select>
            ) : (
              <input
                type={type === 'integer' ? 'number' : 'number'}
                step={type === 'integer' ? 1 : 'any'}
                value={local[key] ?? ''}
                onChange={e => {
                  const v = type === 'integer' ? parseInt(e.target.value) : parseFloat(e.target.value)
                  setLocal(p => ({ ...p, [key]: isNaN(v) ? e.target.value : v }))
                }}
                style={{ width: '100%' }}
              />
            )}
          </div>
        ))}
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <Button variant="primary" size="sm" onClick={save} disabled={saving}>
          {saving ? t('saving') : t('save')}
        </Button>
        {flash && <span style={{ fontSize: 12, color: flash.startsWith('Error') ? 'var(--red)' : 'var(--green)' }}>{flash}</span>}
      </div>
    </div>
  )
}

function FuturesStratCard({ strat, name, desc, paramMeta, onUpdate, children }) {
  const { t } = useLang()
  const [expanded, setExpanded] = useState(false)
  const [toggling, setToggling] = useState(false)
  const enabled = strat?.enabled ?? false
  const status = strat || {}

  const toggle = async () => {
    if (!strat) return
    setToggling(true)
    try {
      await apiFetch(`/strategies/${strat.id}/` + (enabled ? 'disable' : 'enable'), { method: 'POST' })
      onUpdate?.()
    } catch (e) {
      console.error('Toggle failed:', e)
    } finally {
      setToggling(false)
    }
  }

  return (
    <div className="card" style={{ overflow: 'visible' }}>
      {/* Header */}
      <div style={{ padding: '16px 20px' }}>
        <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 12 }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
              <span style={{
                width: 8, height: 8, borderRadius: '50%', flexShrink: 0,
                background: enabled ? 'var(--green)' : 'var(--border2)',
                boxShadow: enabled ? '0 0 6px var(--green)' : 'none',
              }} />
              <span style={{ fontWeight: 700, fontSize: 14, color: 'var(--t1)' }}>{name}</span>
              <Badge variant={enabled ? 'green' : 'muted'} size="sm">
                {enabled ? t('strat_active') : t('strat_inactive')}
              </Badge>
            </div>
            <div style={{ fontSize: 11, color: 'var(--t3)', lineHeight: 1.5 }}>{desc}</div>
          </div>
          {/* Toggle */}
          <button onClick={toggle} disabled={toggling || !strat}
            className={`toggle ${enabled ? 'toggle-on' : ''}`}
            style={{ flexShrink: 0, marginTop: 2 }}
            title={enabled ? 'Disable' : 'Enable'}
          />
        </div>

        {/* Stats row */}
        {strat && (
          <div style={{
            display: 'flex', flexWrap: 'wrap', gap: 16, marginTop: 14,
            paddingTop: 14, borderTop: '1px solid var(--border)',
          }}>
            {children}
            <div>
              <div className="label">PnL</div>
              <div className="num metric" style={{
                color: (status.realized_pnl_usdt || 0) >= 0 ? 'var(--green)' : 'var(--red)',
              }}>
                {(status.realized_pnl_usdt || 0) >= 0 ? '+' : ''}{(status.realized_pnl_usdt || 0).toFixed(2)}
              </div>
            </div>
            <div>
              <div className="label">Trades</div>
              <div className="num metric">{status.trade_count ?? 0}</div>
            </div>
          </div>
        )}

        {!strat && (
          <div style={{ marginTop: 10, fontSize: 11, color: 'var(--t3)' }}>
            {t('futures_enable_tip')}
          </div>
        )}
      </div>

      {/* Params toggle */}
      {strat && (
        <div style={{ borderTop: '1px solid var(--border)' }}>
          <button onClick={() => setExpanded(e => !e)} style={{
            width: '100%', padding: '8px 20px',
            background: 'none', border: 'none', cursor: 'pointer',
            display: 'flex', alignItems: 'center', gap: 6,
            color: 'var(--t3)', fontSize: 12, textAlign: 'left',
          }}>
            <span style={{ transform: expanded ? 'rotate(90deg)' : 'none', transition: '0.15s', display: 'inline-block' }}>▶</span>
            {t('parameters')}
          </button>
          {expanded && (
            <ParamEditor
              stratId={strat.id}
              params={strat.params || {}}
              meta={paramMeta}
              onSaved={onUpdate}
            />
          )}
        </div>
      )}
    </div>
  )
}

// ── Param meta for each strategy ─────────────────────────────────────────────

const TREND_PARAMS = [
  { key: 'exchange',        label: 'Exchange',          type: 'select',  options: ['binance', 'okx'] },
  { key: 'symbol',          label: 'Symbol',            type: 'text' },
  { key: 'fast_period',     label: 'Fast MA Period',    type: 'integer' },
  { key: 'slow_period',     label: 'Slow MA Period',    type: 'integer' },
  { key: 'position_usdt',   label: 'Position (USDT)',   type: 'number' },
  { key: 'stop_loss_pct',   label: 'Stop Loss %',       type: 'number' },
  { key: 'take_profit_pct', label: 'Take Profit %',     type: 'number' },
  { key: 'direction',       label: 'Direction',         type: 'select',  options: ['both', 'long_only', 'short_only'] },
  { key: 'cooldown_s',      label: 'Cooldown (sec)',    type: 'number' },
]

const GRID_PARAMS = [
  { key: 'exchange',        label: 'Exchange',          type: 'select',  options: ['binance', 'okx'] },
  { key: 'symbol',          label: 'Symbol',            type: 'text' },
  { key: 'grid_low',        label: 'Grid Low',          type: 'number' },
  { key: 'grid_high',       label: 'Grid High',         type: 'number' },
  { key: 'grid_count',      label: 'Grid Levels',       type: 'integer' },
  { key: 'grid_usdt',       label: 'Per-grid (USDT)',   type: 'number' },
  { key: 'mode',            label: 'Mode',              type: 'select',  options: ['neutral', 'long', 'short'] },
]

const SIGNAL_PARAMS = [
  { key: 'exchange',        label: 'Exchange',          type: 'select',  options: ['binance', 'okx'] },
  { key: 'symbol',          label: 'Symbol',            type: 'text' },
  { key: 'position_usdt',   label: 'Position (USDT)',   type: 'number' },
  { key: 'signal_type',     label: 'Signal Type',       type: 'select',  options: ['rsi', 'breakout', 'ma_cross'] },
  { key: 'rsi_period',      label: 'RSI Period',        type: 'integer' },
  { key: 'rsi_oversold',    label: 'RSI Oversold',      type: 'number' },
  { key: 'rsi_overbought',  label: 'RSI Overbought',    type: 'number' },
  { key: 'breakout_period', label: 'Breakout Period',   type: 'integer' },
  { key: 'stop_loss_pct',   label: 'Stop Loss %',       type: 'number' },
  { key: 'take_profit_pct', label: 'Take Profit %',     type: 'number' },
  { key: 'direction',       label: 'Direction',         type: 'select',  options: ['both', 'long_only', 'short_only'] },
  { key: 'cooldown_s',      label: 'Cooldown (sec)',    type: 'number' },
]

// ── Positions table ───────────────────────────────────────────────────────────

function PositionsTable({ positions, tickers, onUpdate }) {
  const { t } = useLang()
  const [closing, setClosing] = useState({})
  const [flash, setFlash] = useState('')

  const futuresPositions = Object.values(positions).filter(p =>
    p.exchange === 'binance' || p.exchange === 'okx'
  )

  const closePos = async (pos) => {
    const k = `${pos.exchange}:${pos.symbol}`
    setClosing(c => ({ ...c, [k]: true }))
    try {
      await apiFetch('/positions/close', {
        method: 'POST',
        body: JSON.stringify({ exchange: pos.exchange, symbol: pos.symbol }),
      })
      setFlash(`${pos.symbol} close order sent`)
      setTimeout(() => setFlash(''), 3000)
      onUpdate?.()
    } catch (e) {
      setFlash(`Error: ${e.message}`)
    } finally {
      setClosing(c => ({ ...c, [k]: false }))
    }
  }

  if (futuresPositions.length === 0) {
    return (
      <EmptyState
        title={t('futures_no_positions')}
        subtitle={t('futures_no_positions_sub')}
      />
    )
  }

  return (
    <div>
      {flash && (
        <div style={{
          marginBottom: 12, padding: '8px 14px', borderRadius: 8, fontSize: 12,
          background: flash.startsWith('Error') ? 'rgba(255,60,92,0.1)' : 'rgba(0,217,163,0.1)',
          color: flash.startsWith('Error') ? 'var(--red)' : 'var(--green)',
          border: `1px solid ${flash.startsWith('Error') ? 'rgba(255,60,92,0.2)' : 'rgba(0,217,163,0.2)'}`,
        }}>{flash}</div>
      )}
      <div style={{ overflowX: 'auto' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
          <thead>
            <tr style={{ borderBottom: '1px solid var(--border)' }}>
              {['Exchange', 'Symbol', 'Side', 'Size', 'Entry', 'Mark', 'Liq.', 'Unr. PnL', 'ROI%', ''].map(h => (
                <th key={h} style={{ padding: '8px 10px', textAlign: 'left', color: 'var(--t3)', fontWeight: 600, fontSize: 10, textTransform: 'uppercase', whiteSpace: 'nowrap' }}>
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {futuresPositions.map(pos => {
              const k = `${pos.exchange}:${pos.symbol}`
              const upnl = Number(pos.unrealized_pnl || 0)
              const entry = Number(pos.entry_price || 0)
              const notional = Number(pos.size || 0) * entry
              const roi = notional > 0 ? (upnl / notional) * 100 * Number(pos.leverage || 1) : 0
              const isLong = pos.side === 'long' || pos.side === 'LONG' || Number(pos.size || 0) > 0
              return (
                <tr key={k} style={{ borderBottom: '1px solid var(--border)' }}>
                  <td style={{ padding: '10px 10px', color: 'var(--t2)' }}>
                    <span style={{ fontSize: 10, background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 4, padding: '2px 6px' }}>
                      {pos.exchange.toUpperCase()}
                    </span>
                  </td>
                  <td style={{ padding: '10px 10px', fontWeight: 600, color: 'var(--t1)' }}>{pos.symbol}</td>
                  <td style={{ padding: '10px 10px' }}>
                    <Badge variant={isLong ? 'green' : 'red'} size="sm">{isLong ? 'LONG' : 'SHORT'}</Badge>
                  </td>
                  <td className="num" style={{ padding: '10px 10px', color: 'var(--t1)' }}>{Number(pos.size || 0).toFixed(4)}</td>
                  <td className="num" style={{ padding: '10px 10px', color: 'var(--t2)' }}>{Number(entry).toLocaleString('en', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</td>
                  <td className="num" style={{ padding: '10px 10px', color: 'var(--t2)' }}>{Number(pos.mark_price || 0).toLocaleString('en', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</td>
                  <td className="num" style={{ padding: '10px 10px', color: 'var(--t3)' }}>{Number(pos.liquidation_price || 0).toLocaleString('en', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</td>
                  <td className="num" style={{ padding: '10px 10px', fontWeight: 700, color: upnl >= 0 ? 'var(--green)' : 'var(--red)' }}>
                    {upnl >= 0 ? '+' : ''}{upnl.toFixed(2)}
                  </td>
                  <td className="num" style={{ padding: '10px 10px', color: roi >= 0 ? 'var(--green)' : 'var(--red)' }}>
                    {roi >= 0 ? '+' : ''}{roi.toFixed(2)}%
                  </td>
                  <td style={{ padding: '10px 10px' }}>
                    <Button variant="red" size="xs" onClick={() => closePos(pos)} disabled={closing[k]}>
                      {closing[k] ? '…' : t('futures_close_btn')}
                    </Button>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ── Main Page ─────────────────────────────────────────────────────────────────

export default function FuturesPage({ strategies = [], positions = {}, tickers = {}, onUpdate }) {
  const { t } = useLang()

  const getStrat = (id) => {
    const s = strategies.find(s => (s.id || s.strategy_id) === id)
    if (!s) return null
    return { ...s, id: s.id || s.strategy_id }
  }

  const trendStrat  = getStrat('futures_trend')
  const gridStrat   = getStrat('futures_grid')
  const signalStrat = getStrat('futures_signal')

  const ts = trendStrat || {}
  const gs = gridStrat  || {}
  const ss = signalStrat || {}

  const posCount = Object.values(positions).filter(p =>
    p.exchange === 'binance' || p.exchange === 'okx'
  ).length

  return (
    <div style={{ padding: '24px', maxWidth: 1100 }}>
      <PageHeader
        title={t('futures_title')}
        subtitle={posCount > 0 ? `${posCount} open position${posCount !== 1 ? 's' : ''}` : t('futures_no_positions_sub')}
        right={
          <Button variant="ghost" size="sm" onClick={onUpdate}>
            {t('refresh')}
          </Button>
        }
      />

      {/* Open positions */}
      <Card style={{ marginBottom: 24 }}>
        <CardHeader title={t('futures_positions_title')} />
        <div style={{ padding: '0 20px 20px' }}>
          <PositionsTable positions={positions} tickers={tickers} onUpdate={onUpdate} />
        </div>
      </Card>

      {/* Three strategy cards */}
      <div style={{ marginBottom: 16 }}>
        <div style={{ fontSize: 11, fontWeight: 700, letterSpacing: '0.1em', textTransform: 'uppercase', color: 'var(--t3)', marginBottom: 14 }}>
          {t('futures_strategies_title')}
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(340px, 1fr))', gap: 16 }}>

          {/* 趋势跟踪 */}
          <FuturesStratCard
            strat={trendStrat}
            name={t('futures_trend_name')}
            desc={t('futures_trend_desc')}
            paramMeta={TREND_PARAMS}
            onUpdate={onUpdate}
          >
            <div>
              <div className="label">Exchange</div>
              <div className="num metric">{(ts.params?.exchange || '—').toUpperCase()}</div>
            </div>
            <div>
              <div className="label">Symbol</div>
              <div className="metric" style={{ fontWeight: 600, color: 'var(--t1)' }}>{ts.params?.symbol || '—'}</div>
            </div>
            <div>
              <div className="label">{t('futures_position_side')}</div>
              <div className="metric" style={{
                fontWeight: 700,
                color: ts.position_side === 'long' ? 'var(--green)' : ts.position_side === 'short' ? 'var(--red)' : 'var(--t3)',
              }}>
                {ts.position_side ? ts.position_side.toUpperCase() : '—'}
              </div>
            </div>
            {ts.fast_ma && ts.slow_ma && (
              <div>
                <div className="label">MA</div>
                <div className="num metric">{ts.fast_ma} / {ts.slow_ma}</div>
              </div>
            )}
          </FuturesStratCard>

          {/* 合约网格 */}
          <FuturesStratCard
            strat={gridStrat}
            name={t('futures_grid_name')}
            desc={t('futures_grid_desc')}
            paramMeta={GRID_PARAMS}
            onUpdate={onUpdate}
          >
            <div>
              <div className="label">Exchange</div>
              <div className="num metric">{(gs.params?.exchange || '—').toUpperCase()}</div>
            </div>
            <div>
              <div className="label">Symbol</div>
              <div className="metric" style={{ fontWeight: 600, color: 'var(--t1)' }}>{gs.params?.symbol || '—'}</div>
            </div>
            <div>
              <div className="label">Mode</div>
              <div className="metric" style={{ fontWeight: 600, color: 'var(--t1)' }}>{gs.params?.mode || 'neutral'}</div>
            </div>
            <div>
              <div className="label">Orders</div>
              <div className="num metric">{gs.open_orders ?? 0}</div>
            </div>
            {(gs.params?.grid_low === 0 || !gs.params?.grid_low) && (
              <div style={{ gridColumn: '1 / -1' }}>
                <Badge variant="yellow" size="sm">{t('futures_grid_inactive')}</Badge>
              </div>
            )}
          </FuturesStratCard>

          {/* 信号跟单 */}
          <FuturesStratCard
            strat={signalStrat}
            name={t('futures_signal_name')}
            desc={t('futures_signal_desc')}
            paramMeta={SIGNAL_PARAMS}
            onUpdate={onUpdate}
          >
            <div>
              <div className="label">Exchange</div>
              <div className="num metric">{(ss.params?.exchange || '—').toUpperCase()}</div>
            </div>
            <div>
              <div className="label">Symbol</div>
              <div className="metric" style={{ fontWeight: 600, color: 'var(--t1)' }}>{ss.params?.symbol || '—'}</div>
            </div>
            <div>
              <div className="label">{t('futures_signal_type')}</div>
              <div className="metric" style={{ fontWeight: 600, color: 'var(--t1)' }}>{ss.params?.signal_type || 'rsi'}</div>
            </div>
            <div>
              <div className="label">{t('futures_position_side')}</div>
              <div className="metric" style={{
                fontWeight: 700,
                color: ss.position_side === 'long' ? 'var(--green)' : ss.position_side === 'short' ? 'var(--red)' : 'var(--t3)',
              }}>
                {ss.position_side ? ss.position_side.toUpperCase() : '—'}
              </div>
            </div>
            {ss.current_rsi != null && (
              <div>
                <div className="label">RSI</div>
                <div className="num metric" style={{
                  color: ss.current_rsi < (ss.params?.rsi_oversold ?? 30) ? 'var(--green)' :
                         ss.current_rsi > (ss.params?.rsi_overbought ?? 70) ? 'var(--red)' : 'var(--t1)',
                }}>
                  {ss.current_rsi.toFixed(1)}
                </div>
              </div>
            )}
          </FuturesStratCard>

        </div>
      </div>

      {/* Info box */}
      <div style={{
        padding: '14px 18px', borderRadius: 10, marginTop: 8,
        background: 'rgba(59,123,255,0.04)', border: '1px solid rgba(59,123,255,0.1)',
        fontSize: 12, color: 'var(--t3)', lineHeight: 1.7,
      }}>
        <strong style={{ color: 'var(--t2)' }}>使用说明：</strong>
        {' '}1. 在参数区配置交易所/币对/金额 → 2. 点击 toggle 启用策略 → 3. 系统自动开平仓。
        {' '}建议先用小仓位（position_usdt = 5~10 USDT）测试，观察 Logs 页确认信号触发。
        {' '}合约网格需要先设置 grid_low / grid_high 价格区间。
      </div>
    </div>
  )
}
