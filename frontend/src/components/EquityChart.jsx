import { useEffect, useState, useRef } from 'react'
import { useLang } from '../i18n'

function getApiKey() { return localStorage.getItem('trading_api_key') || '' }

const RANGES = [
  { label: '1H',  limit: 60    },
  { label: '6H',  limit: 360   },
  { label: '24H', limit: 1440  },
  { label: '7D',  limit: 10080 },
  { label: '30D', limit: 43200 },
  { label: 'All', limit: 999999 },
]

export default function EquityChart({ refreshSignal }) {
  const { t } = useLang()
  const [data, setData]   = useState([])
  const [range, setRange] = useState(RANGES[1])
  const stateRef = useRef({ limit: RANGES[1].limit, lastFetch: 0 })

  const doLoad = () => {
    const key = getApiKey()
    stateRef.current.lastFetch = Date.now()
    fetch(`/api/data/equity?limit=${stateRef.current.limit}`, { headers: key ? { 'X-API-Key': key } : {} })
      .then(r => r.json())
      .then(d => setData(Array.isArray(d) ? d : []))
      .catch(() => {})
  }

  useEffect(() => {
    stateRef.current.limit = range.limit
    doLoad()
    const iv = setInterval(doLoad, 60000)
    return () => clearInterval(iv)
  }, [range]) // eslint-disable-line react-hooks/exhaustive-deps

  // Real-time refresh on balance_update, at most every 30s
  useEffect(() => {
    if (!refreshSignal) return
    if (Date.now() - stateRef.current.lastFetch < 30000) return
    doLoad()
  }, [refreshSignal]) // eslint-disable-line react-hooks/exhaustive-deps

  const changeRange = (r) => {
    stateRef.current.limit = r.limit
    setRange(r)
  }

  const RangeBtn = ({ r }) => (
    <button onClick={() => changeRange(r)} style={{
      padding: '3px 9px', borderRadius: 5, fontSize: 10, fontWeight: 700,
      cursor: 'pointer', border: 'none',
      background: range.label === r.label ? 'rgba(59,123,255,0.18)' : 'transparent',
      color: range.label === r.label ? 'var(--accent)' : 'var(--t3)',
      transition: 'all 0.12s',
    }}>{r.label}</button>
  )

  if (data.length < 2) {
    return (
      <div>
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 2, marginBottom: 8 }}>
          {RANGES.map(r => <RangeBtn key={r.label} r={r} />)}
        </div>
        <div style={{ padding: '20px 0', textAlign: 'center' }}>
          <div style={{ fontSize: 28, marginBottom: 8, opacity: 0.3 }}>📈</div>
          <div style={{ fontSize: 12, color: 'var(--t3)' }}>{t('equity_no_data')}</div>
        </div>
      </div>
    )
  }

  const W = 600, H = 110
  const PAD = { l: 4, r: 4, t: 8, b: 20 }
  const iW = W - PAD.l - PAD.r
  const iH = H - PAD.t - PAD.b

  const values = data.map(d => d.total_usdt)
  const min = Math.min(...values)
  const max = Math.max(...values)
  const range_ = max - min || 1

  const xS = i => PAD.l + (i / (data.length - 1)) * iW
  const yS = v => PAD.t + iH - ((v - min) / range_) * iH

  const pts = data.map((d, i) => `${xS(i)},${yS(d.total_usdt)}`).join(' ')
  const latest = values[values.length - 1]
  const initial = values[0]
  const change = latest - initial
  const changePct = initial > 0 ? (change / initial * 100) : 0
  const isPos = change >= 0
  const color = isPos ? 'var(--green)' : 'var(--red)'

  const fmtDate = ts => new Date(ts * 1000).toLocaleString([], {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  })

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10 }}>
        <span className="num" style={{ fontSize: 22, fontWeight: 700, color: 'var(--t1)' }}>
          {latest.toLocaleString('en', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
          <span style={{ fontSize: 12, color: 'var(--t3)', marginLeft: 4 }}>USDT</span>
        </span>
        <span style={{ fontSize: 12, fontWeight: 600, color }}>
          {isPos ? '+' : ''}{change.toFixed(2)} ({isPos ? '+' : ''}{changePct.toFixed(2)}%)
        </span>
        <div style={{ flex: 1 }} />
        <div style={{ display: 'flex', gap: 2 }}>
          {RANGES.map(r => <RangeBtn key={r.label} r={r} />)}
        </div>
      </div>

      <svg width="100%" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none"
        style={{ display: 'block', overflow: 'visible' }}>
        <defs>
          <linearGradient id="eq-fill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={color} stopOpacity="0.25" />
            <stop offset="100%" stopColor={color} stopOpacity="0.01" />
          </linearGradient>
        </defs>
        <polygon
          points={`${PAD.l},${PAD.t + iH} ${pts} ${PAD.l + iW},${PAD.t + iH}`}
          fill="url(#eq-fill)"
        />
        <polyline points={pts} fill="none" stroke={color} strokeWidth="1.8"
          strokeLinejoin="round" strokeLinecap="round" />
      </svg>

      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 10, color: 'var(--t3)', marginTop: 4 }}>
        <span>{fmtDate(data[0].ts)}</span>
        <span style={{ color: 'var(--t4)' }}>{t('stat_data_pts', data.length)}</span>
        <span>{fmtDate(data[data.length - 1].ts)}</span>
      </div>
    </div>
  )
}
