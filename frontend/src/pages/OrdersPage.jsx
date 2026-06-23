import { useState } from 'react'
import { useLang } from '../i18n'
import { Button, PageHeader, Alert } from '../components/ui'

function getApiKey() { return localStorage.getItem('trading_api_key') || '' }

function PlaceOrderForm({ symbols, t }) {
  const [form, setForm] = useState({
    exchange: 'binance', symbol: symbols[0] || 'BTC-USDT',
    side: 'buy', type: 'limit', price: '', quantity: '',
  })
  const [status, setStatus] = useState(null)

  const set = (k, v) => setForm(f => ({ ...f, [k]: v }))

  const handleSubmit = async e => {
    e.preventDefault()
    setStatus('sending')
    try {
      const key = getApiKey()
      const res = await fetch('/api/orders', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...(key ? { 'X-API-Key': key } : {}) },
        body: JSON.stringify({
          exchange: form.exchange,
          symbol: form.symbol,
          side: form.side,
          order_type: form.type,
          price: form.type === 'market' ? undefined : parseFloat(form.price),
          quantity: parseFloat(form.quantity),
        }),
      })
      if (res.ok) { setStatus('ok'); setTimeout(() => setStatus(null), 3000) }
      else { const d = await res.json(); setStatus('error:' + (d.detail || 'failed')) }
    } catch { setStatus('error:Network error') }
  }

  const errMsg = status?.startsWith('error:') ? status.slice(6) : null

  return (
    <form onSubmit={handleSubmit}>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 12 }}>
        <div>
          <label className="label">{t('th_exchange')}</label>
          <select value={form.exchange} onChange={e => set('exchange', e.target.value)}
            style={{ width: '100%', marginTop: 6 }}>
            <option value="binance">{t('bn_futures_label')}</option>
            <option value="binance_spot">{t('bn_spot_label')}</option>
            <option value="okx">{t('okx_futures_label')}</option>
            <option value="okx_spot">{t('okx_spot_label')}</option>
          </select>
        </div>
        <div>
          <label className="label">{t('th_symbol')}</label>
          <select value={form.symbol} onChange={e => set('symbol', e.target.value)}
            style={{ width: '100%', marginTop: 6 }}>
            {symbols.length ? symbols.map(s => <option key={s}>{s}</option>)
              : <option>BTC-USDT</option>}
          </select>
        </div>
        <div>
          <label className="label">{t('th_side')}</label>
          <div style={{ display: 'flex', gap: 8, marginTop: 6 }}>
            {['buy', 'sell'].map(s => (
              <Button key={s} type="button"
                variant={form.side === s ? (s === 'buy' ? 'buy' : 'sell') : 'ghost'}
                style={{ flex: 1 }}
                onClick={() => set('side', s)}>
                {s === 'buy' ? t('side_buy') : t('side_sell')}
              </Button>
            ))}
          </div>
        </div>
        <div>
          <label className="label">{t('th_type')}</label>
          <select value={form.type} onChange={e => set('type', e.target.value)}
            style={{ width: '100%', marginTop: 6 }}>
            <option value="limit">Limit</option>
            <option value="market">Market</option>
          </select>
        </div>
        {form.type === 'limit' && (
          <div>
            <label className="label">{t('price_usdt')}</label>
            <input type="number" placeholder="0.00" value={form.price}
              onChange={e => set('price', e.target.value)}
              step="any" style={{ width: '100%', marginTop: 6 }} required />
          </div>
        )}
        <div>
          <label className="label">{t('quantity')}</label>
          <input type="number" placeholder="0.000" value={form.quantity}
            onChange={e => set('quantity', e.target.value)}
            step="any" style={{ width: '100%', marginTop: 6 }} required />
        </div>
      </div>

      {errMsg && <Alert variant="error" style={{ marginBottom: 12 }}>{errMsg}</Alert>}
      {status === 'ok' && <Alert variant="success" style={{ marginBottom: 12 }}>{t('order_ok')}</Alert>}

      <Button type="submit" variant={form.side === 'buy' ? 'buy' : 'sell'} className="btn-full"
        disabled={status === 'sending'}>
        {status === 'sending' ? t('placing') : t('place_btn', form.side)}
      </Button>
    </form>
  )
}

export default function OrdersPage({ orders, symbols }) {
  const { t } = useLang()
  const fmt = n => n != null ? Number(n).toLocaleString('en', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : '—'

  const cancelOrder = async (exchange, orderId, symbol) => {
    try {
      const key = getApiKey()
      await fetch(`/api/orders/${exchange}/${encodeURIComponent(symbol)}/${orderId}`, { method: 'DELETE', headers: key ? { 'X-API-Key': key } : {} })
    } catch { /* ignore */ }
  }

  const statusColor = s => ({
    open: 'var(--accent)', new: 'var(--accent)',
    partially_filled: 'var(--yellow)', partial: 'var(--yellow)',
    filled: 'var(--green)', canceled: 'var(--t3)', cancelled: 'var(--t3)',
    rejected: 'var(--red)',
  }[s?.toLowerCase()] || 'var(--t2)')

  return (
    <div className="page">
      <PageHeader title={t('orders_title')}>
        <span style={{ fontSize: 12, color: 'var(--t2)' }}>{t('order_count', orders.length)}</span>
        <Button variant="red" size="sm"
          onClick={async () => {
            if (!confirm('Cancel all open orders?')) return
            const key = localStorage.getItem('trading_api_key') || ''
            await fetch('/api/orders/all', { method: 'DELETE', headers: key ? { 'X-API-Key': key } : {} })
          }}>
          ✕ Cancel All
        </Button>
      </PageHeader>

      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) 320px', gap: 20, alignItems: 'start' }}
        className="orders-grid">
        <div className="card" style={{ overflow: 'hidden' }}>
          <div className="card-header">
            <span className="section-title">{t('open_orders_title')}</span>
            <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <span className="dot dot-green blink" />
              <span style={{ fontSize: 11, color: 'var(--t2)' }}>{t('realtime')}</span>
            </span>
          </div>

          {orders.length === 0 ? (
            <div className="empty-state">
              <div className="empty-icon">📋</div>
              <div className="empty-title">{t('no_orders')}</div>
            </div>
          ) : (
            <div className="table-wrap">
              <table style={{ width: '100%' }}>
                <thead><tr>
                  <th>{t('th_exchange')}</th>
                  <th>{t('th_symbol')}</th>
                  <th>{t('th_strategy')}</th>
                  <th>{t('th_side')}</th>
                  <th>{t('th_type')}</th>
                  <th style={{ textAlign: 'right' }}>{t('th_price')}</th>
                  <th style={{ textAlign: 'right' }}>{t('th_qty')}</th>
                  <th style={{ textAlign: 'right' }}>{t('th_filled')}</th>
                  <th>{t('th_status')}</th>
                  <th>{t('th_time')}</th>
                  <th></th>
                </tr></thead>
                <tbody>
                  {orders.map((o, i) => (
                    <tr key={i}
                      onMouseEnter={e => e.currentTarget.style.background = 'rgba(59,123,255,0.03)'}
                      onMouseLeave={e => e.currentTarget.style.background = ''}
                      style={{ transition: 'background 0.1s' }}
                    >
                      <td>
                        <span style={{
                          padding: '2px 6px', borderRadius: 4, fontSize: 10, fontWeight: 700,
                          background: o.exchange === 'binance' ? 'rgba(240,185,11,0.12)' : 'rgba(0,100,220,0.12)',
                          color: o.exchange === 'binance' ? '#f0b90b' : '#0064dc',
                        }}>
                          {o.exchange === 'binance' ? 'BN' : 'OKX'}
                        </span>
                      </td>
                      <td style={{ fontWeight: 600 }}>{o.symbol}</td>
                      <td>
                        {o.strategy_id ? (
                          <span style={{
                            fontSize: 10, fontWeight: 700, padding: '2px 6px', borderRadius: 4,
                            background: 'rgba(59,123,255,0.1)', color: 'var(--accent)',
                          }}>{o.strategy_id}</span>
                        ) : <span style={{ color: 'var(--t4)', fontSize: 11 }}>{t('manual_label')}</span>}
                      </td>
                      <td><span className={`badge badge-${o.side}`}>{o.side?.toUpperCase()}</span></td>
                      <td style={{ color: 'var(--t2)', fontSize: 11, textTransform: 'uppercase' }}>{o.order_type}</td>
                      <td className="num" style={{ textAlign: 'right' }}>{fmt(o.price)}</td>
                      <td className="num" style={{ textAlign: 'right' }}>{fmt(o.quantity)}</td>
                      <td className="num" style={{ textAlign: 'right', color: 'var(--t2)' }}>{fmt(o.filled_qty)}</td>
                      <td>
                        <span style={{ fontSize: 10, fontWeight: 700, color: statusColor(o.status) }}>
                          {o.status?.replace('_', ' ').toUpperCase()}
                        </span>
                      </td>
                      <td style={{ fontSize: 10, color: 'var(--t3)' }}>
                        {o.created_at ? new Date(o.created_at * 1000).toLocaleTimeString() : '—'}
                      </td>
                      <td>
                        {['open', 'new', 'partially_filled', 'partial'].includes(o.status?.toLowerCase()) && (
                          <Button size="xs" variant="red"
                            onClick={() => cancelOrder(o.exchange, o.order_id, o.symbol)}>
                            {t('cancel_order')}
                          </Button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        <div className="card" style={{ padding: 20 }}>
          <div style={{ marginBottom: 16 }}>
            <div className="section-title">{t('place_order')}</div>
            <div style={{ fontSize: 11, color: 'var(--t3)', marginTop: 4 }}>{t('manual_order')}</div>
          </div>
          <PlaceOrderForm symbols={symbols} t={t} />
        </div>
      </div>
    </div>
  )
}
