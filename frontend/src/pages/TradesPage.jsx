import { useState, useEffect, useCallback } from 'react'
import { useLang } from '../i18n'
import { Button, PageHeader } from '../components/ui'

function getApiKey() { return localStorage.getItem('trading_api_key') || '' }

function fmtTime(ts) {
  if (!ts) return '—'
  return new Date(ts * 1000).toLocaleString([], {
    month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
}

function fmtNum(n, dp = 2) {
  if (n == null) return '—'
  return Number(n).toLocaleString('en', { minimumFractionDigits: dp, maximumFractionDigits: dp })
}

export default function TradesPage() {
  const { t } = useLang()
  const [trades, setTrades]       = useState([])
  const [loading, setLoading]     = useState(false)
  const [filterStrat, setFilterStrat] = useState('')
  const [filterEx, setFilterEx]   = useState('')
  const [filterSym, setFilterSym] = useState('')
  const [limit, setLimit]         = useState(200)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const key = getApiKey()
      const headers = key ? { 'X-API-Key': key } : {}
      const params = new URLSearchParams({ limit })
      if (filterStrat) params.set('strategy_id', filterStrat)
      if (filterEx)    params.set('exchange', filterEx)
      if (filterSym)   params.set('symbol', filterSym)
      const data = await fetch(`/api/data/trades?${params}`, { headers }).then(r => r.json())
      setTrades(Array.isArray(data) ? data : [])
    } catch { setTrades([]) }
    finally { setLoading(false) }
  }, [filterStrat, filterEx, filterSym, limit])

  useEffect(() => { load() }, [load])

  const totalFee = trades.reduce((s, tr) => s + (tr.fee || 0), 0)
  const totalVol = trades.reduce((s, tr) => s + (tr.quantity || 0) * (tr.price || 0), 0)

  return (
    <div className="page">
      <PageHeader title={t('nav_trades')}>
        <Button variant="ghost" size="sm" onClick={load} disabled={loading}>
          {loading ? t('loading') : t('refresh')}
        </Button>
      </PageHeader>

      {/* Filters */}
      <div className="card" style={{ padding: '14px 18px' }}>
        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center' }}>
          <input placeholder={t('filter_strategy_id')} value={filterStrat}
            onChange={e => setFilterStrat(e.target.value)} />
          <select value={filterEx} onChange={e => setFilterEx(e.target.value)}>
            <option value="">{t('all_exchanges')}</option>
            <option value="binance">{t('bn_futures_label')}</option>
            <option value="binance_spot">{t('bn_spot_label')}</option>
            <option value="okx">{t('okx_futures_label')}</option>
            <option value="okx_spot">{t('okx_spot_label')}</option>
          </select>
          <input placeholder={t('filter_symbol_ph')} value={filterSym}
            onChange={e => setFilterSym(e.target.value)} />
          <select value={limit} onChange={e => setLimit(Number(e.target.value))}>
            {[50, 200, 500, 1000].map(n => (
              <option key={n} value={n}>{t('recent_n', n)}</option>
            ))}
          </select>
        </div>
      </div>

      {/* Summary row */}
      {trades.length > 0 && (
        <div style={{ display: 'flex', gap: 20, flexWrap: 'wrap', fontSize: 12, color: 'var(--t2)', padding: '0 2px' }}>
          <span>{t('trades_count', trades.length)}</span>
          <span>{t('total_volume_label')} <b className="num" style={{ color: 'var(--t1)' }}>{fmtNum(totalVol)} USDT</b></span>
          <span>{t('total_fees_label')} <b className="num" style={{ color: 'var(--red)' }}>-{fmtNum(totalFee, 4)} USDT</b></span>
        </div>
      )}

      {/* Table */}
      <div className="card">
        {trades.length === 0 ? (
          <div className="empty-state">
            <div className="empty-icon">📋</div>
            <div className="empty-title">{t('no_trades')}</div>
            <div className="empty-sub">{t('no_trades_sub')}</div>
          </div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>{t('th_time')}</th>
                  <th>{t('th_strategy')}</th>
                  <th>{t('th_exchange')}</th>
                  <th>{t('th_symbol')}</th>
                  <th>{t('th_side')}</th>
                  <th style={{ textAlign: 'right' }}>{t('th_qty')}</th>
                  <th style={{ textAlign: 'right' }}>{t('th_price')}</th>
                  <th style={{ textAlign: 'right' }}>{t('th_amount')}</th>
                  <th style={{ textAlign: 'right' }}>{t('th_fees_usdt')}</th>
                  <th>{t('th_status')}</th>
                </tr>
              </thead>
              <tbody>
                {trades.map((tr, i) => {
                  const notional = (tr.quantity || 0) * (tr.price || 0)
                  const isBuy = tr.side?.toLowerCase() === 'buy'
                  return (
                    <tr key={i}>
                      <td style={{ fontSize: 11, color: 'var(--t3)', whiteSpace: 'nowrap' }}>{fmtTime(tr.ts)}</td>
                      <td>
                        {tr.strategy_id ? (
                          <span style={{
                            fontSize: 10, fontWeight: 700, padding: '2px 6px', borderRadius: 4,
                            background: 'rgba(59,123,255,0.1)', color: 'var(--accent)',
                          }}>{tr.strategy_id}</span>
                        ) : <span style={{ color: 'var(--t4)' }}>—</span>}
                      </td>
                      <td style={{ fontSize: 11, color: 'var(--t2)' }}>{tr.exchange}</td>
                      <td style={{ fontWeight: 600 }}>{tr.symbol}</td>
                      <td>
                        <span className={`badge badge-${isBuy ? 'long' : 'short'}`}>
                          {isBuy ? t('side_buy_badge') : t('side_sell_badge')}
                        </span>
                      </td>
                      <td className="num" style={{ textAlign: 'right' }}>{fmtNum(tr.quantity, 6)}</td>
                      <td className="num" style={{ textAlign: 'right', color: 'var(--t2)' }}>{fmtNum(tr.price)}</td>
                      <td className="num" style={{ textAlign: 'right' }}>{fmtNum(notional)}</td>
                      <td className="num" style={{ textAlign: 'right', color: 'var(--red)', fontSize: 11 }}>
                        {tr.fee ? `-${fmtNum(tr.fee, 4)}` : '—'}
                      </td>
                      <td>
                        <span style={{
                          fontSize: 10, fontWeight: 600,
                          color: tr.status === 'filled' ? 'var(--green)' : 'var(--t3)',
                        }}>{tr.status}</span>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
