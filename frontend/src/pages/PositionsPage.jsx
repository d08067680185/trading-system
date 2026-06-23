import { useLang } from '../i18n'
import { Button, PageHeader } from '../components/ui'

function getApiKey() { return localStorage.getItem('trading_api_key') || '' }

async function closePosition(exchange, symbol) {
  const key = getApiKey()
  const res = await fetch(`/api/positions/close?exchange=${exchange}&symbol=${symbol}`, {
    method: 'POST',
    headers: key ? { 'X-API-Key': key } : {},
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export default function PositionsPage({ positions, tickers }) {
  const { t } = useLang()
  const fmt = n => n != null ? Number(n).toLocaleString('en', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : '—'

  const enriched = positions.map(p => {
    const key = `${p.exchange}:${p.symbol}`
    const ticker = tickers[key]
    const mark = ticker?.last || p.mark_price
    const roi = p.entry_price ? ((mark - p.entry_price) / p.entry_price * 100 * (p.side === 'short' ? -1 : 1)) : null
    return { ...p, mark_price: mark, roi }
  })

  const totalPnl = enriched.reduce((s, p) => s + (p.unrealized_pnl || 0), 0)

  return (
    <div className="page">

      <PageHeader title={t('positions_title')}>
        {positions.length > 0 && (
          <div style={{ textAlign: 'right' }}>
            <div style={{ fontSize: 10, color: 'var(--t3)', marginBottom: 2 }}>{t('total_upnl')}</div>
            <div className="num" style={{ fontSize: 20, fontWeight: 700, color: totalPnl >= 0 ? 'var(--green)' : 'var(--red)' }}>
              {totalPnl >= 0 ? '+' : ''}{fmt(totalPnl)} USDT
            </div>
          </div>
        )}
        <span style={{ fontSize: 12, color: 'var(--t2)' }}>{t('pos_count', positions.length)}</span>
        <Button variant="red" size="sm"
          onClick={async () => {
            if (!confirm('Close ALL open positions with market orders?')) return
            const key = localStorage.getItem('trading_api_key') || ''
            await fetch('/api/positions/close-all', { method: 'POST', headers: key ? { 'X-API-Key': key } : {} })
          }}>
          ✕ Close All
        </Button>
      </PageHeader>

      {positions.length === 0 ? (
        <div className="card">
          <div className="empty-state">
            <div className="empty-icon">📭</div>
            <div className="empty-title">{t('no_positions')}</div>
            <div className="empty-sub">{t('no_positions_sub')}</div>
          </div>
        </div>
      ) : (
        <div className="card" style={{ overflow: 'hidden' }}>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>{t('th_exchange')}</th>
                  <th>{t('th_symbol')}</th>
                  <th>{t('th_side')}</th>
                  <th style={{ textAlign: 'right' }}>{t('th_size')}</th>
                  <th style={{ textAlign: 'right' }} className="hide-mobile">{t('th_entry')}</th>
                  <th style={{ textAlign: 'right' }}>{t('th_mark')}</th>
                  <th style={{ textAlign: 'right' }} className="hide-mobile">{t('th_liq')}</th>
                  <th style={{ textAlign: 'right' }} className="hide-mobile">{t('th_leverage')}</th>
                  <th style={{ textAlign: 'right' }}>{t('th_roi')}</th>
                  <th style={{ textAlign: 'right' }}>{t('th_upnl')}</th>
                  <th style={{ textAlign: 'right' }}>Action</th>
                </tr>
              </thead>
              <tbody>
                {enriched.map((p, i) => {
                  const pnlColor = p.unrealized_pnl >= 0 ? 'var(--green)' : 'var(--red)'
                  return (
                    <tr key={i}
                      onMouseEnter={e => e.currentTarget.style.background = 'rgba(59,123,255,0.03)'}
                      onMouseLeave={e => e.currentTarget.style.background = ''}
                    >
                      <td>
                        <span style={{
                          display: 'inline-block', padding: '2px 7px', borderRadius: 4,
                          fontSize: 10, fontWeight: 700, letterSpacing: '0.05em',
                          background: p.exchange === 'binance' ? 'rgba(240,185,11,0.12)' : 'rgba(0,100,220,0.12)',
                          color: p.exchange === 'binance' ? '#f0b90b' : '#4488ee',
                        }}>
                          {p.exchange === 'binance' ? 'BN' : 'OKX'}
                        </span>
                      </td>
                      <td>
                        <div style={{ fontWeight: 700, fontSize: 13, color: 'var(--t1)' }}>{p.symbol}</div>
                        <div style={{ fontSize: 9, color: 'var(--t3)', marginTop: 1 }}>
                          {p.exchange?.includes('spot') ? 'SPOT' : 'PERP'}
                        </div>
                      </td>
                      <td><span className={`badge badge-${p.side}`}>{p.side?.toUpperCase()}</span></td>
                      <td className="num" style={{ textAlign: 'right' }}>{fmt(p.size)}</td>
                      <td className="num hide-mobile" style={{ textAlign: 'right', color: 'var(--t2)' }}>{fmt(p.entry_price)}</td>
                      <td className="num" style={{ textAlign: 'right' }}>{fmt(p.mark_price)}</td>
                      <td className="num hide-mobile" style={{ textAlign: 'right', color: 'var(--yellow)' }}>
                        {p.liquidation_price ? fmt(p.liquidation_price) : '—'}
                      </td>
                      <td className="hide-mobile" style={{ textAlign: 'right', color: 'var(--t2)' }}>{p.leverage || '—'}x</td>
                      <td className="num" style={{ textAlign: 'right', color: pnlColor, fontWeight: 700 }}>
                        {p.roi != null ? `${p.roi >= 0 ? '+' : ''}${p.roi.toFixed(2)}%` : '—'}
                      </td>
                      <td className="num" style={{ textAlign: 'right', color: pnlColor, fontWeight: 700 }}>
                        {p.unrealized_pnl >= 0 ? '+' : ''}{fmt(p.unrealized_pnl)}
                      </td>
                      <td style={{ textAlign: 'right', padding: '8px 12px' }}>
                        <Button size="xs" variant="red"
                          onClick={async () => {
                            if (!confirm(`Close ${p.symbol} on ${p.exchange}?`)) return
                            try { await closePosition(p.exchange, p.symbol) }
                            catch (e) { alert('Close failed: ' + e.message) }
                          }}>
                          Close
                        </Button>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {positions.length > 0 && (
        <div className="grid-3">
          {[
            [t('long_positions'), enriched.filter(p => p.side === 'long').length, 'var(--green)', 'accent-green'],
            [t('short_positions'), enriched.filter(p => p.side === 'short').length, 'var(--red)', 'accent-red'],
            [t('exchanges_active'), [...new Set(enriched.map(p => p.exchange))].length, 'var(--accent)', 'accent-blue'],
          ].map(([label, val, color, accent]) => (
            <div key={label} className={`card card-glow ${accent}`} style={{ padding: '20px 22px', textAlign: 'center' }}>
              <div className="label" style={{ marginBottom: 10 }}>{label}</div>
              <div style={{ fontSize: 36, fontWeight: 700, color }}>{val}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
