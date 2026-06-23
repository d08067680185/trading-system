import { useState, useEffect, useRef } from 'react'
import { useLang } from '../i18n'
import { Button, PageHeader } from '../components/ui'

function getApiKey() { return localStorage.getItem('trading_api_key') || '' }
async function apiFetch(path) {
  const key = getApiKey()
  const res = await fetch(`/api${path}`, { headers: key ? { 'X-API-Key': key } : {} })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

const LEVEL_COLOR = {
  DEBUG:    'var(--t3)',
  INFO:     'var(--t2)',
  WARNING:  'var(--yellow)',
  ERROR:    'var(--red)',
  CRITICAL: 'var(--red)',
}
const LEVEL_BG = {
  WARNING:  'rgba(255,184,0,0.06)',
  ERROR:    'rgba(255,60,92,0.06)',
  CRITICAL: 'rgba(255,60,92,0.1)',
}

export default function LogsPage() {
  const { t } = useLang()
  const [logs, setLogs]       = useState([])
  const [strategies, setStrategies] = useState([])
  const [filterStrat, setFilterStrat] = useState('')
  const [filterLevel, setFilterLevel] = useState('')
  const [limit, setLimit]     = useState(200)
  const [loading, setLoading] = useState(false)
  const [autoRefresh, setAutoRefresh] = useState(false)
  const intervalRef = useRef(null)

  const load = async () => {
    setLoading(true)
    try {
      const params = new URLSearchParams({ limit })
      if (filterStrat) params.set('strategy_id', filterStrat)
      if (filterLevel) params.set('level', filterLevel)
      const data = await apiFetch(`/logs?${params}`)
      setLogs(Array.isArray(data) ? data : [])
    } catch { setLogs([]) } finally { setLoading(false) }
  }

  useEffect(() => {
    apiFetch('/strategies').then(d => setStrategies(d.map(s => s.strategy_id))).catch(() => {})
  }, [])

  useEffect(() => { load() }, [filterStrat, filterLevel, limit])

  useEffect(() => {
    if (autoRefresh) {
      intervalRef.current = setInterval(load, 3000)
    } else {
      clearInterval(intervalRef.current)
    }
    return () => clearInterval(intervalRef.current)
  }, [autoRefresh, filterStrat, filterLevel, limit])

  const fmtTs = ts => {
    const d = new Date(ts * 1000)
    return `${d.toLocaleDateString()} ${d.toLocaleTimeString()}`
  }

  return (
    <div className="page">
      <PageHeader title="Strategy Logs">
        <select value={filterStrat} onChange={e => setFilterStrat(e.target.value)}
          style={{ width: 160, fontSize: 12 }}>
          <option value="">All Strategies</option>
          {strategies.map(s => <option key={s} value={s}>{s}</option>)}
        </select>
        <select value={filterLevel} onChange={e => setFilterLevel(e.target.value)}
          style={{ width: 120, fontSize: 12 }}>
          <option value="">All Levels</option>
          {['DEBUG','INFO','WARNING','ERROR','CRITICAL'].map(l => <option key={l} value={l}>{l}</option>)}
        </select>
        <select value={limit} onChange={e => setLimit(Number(e.target.value))}
          style={{ width: 90, fontSize: 12 }}>
          {[100, 200, 500, 1000].map(n => <option key={n} value={n}>Last {n}</option>)}
        </select>
        <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: 'var(--t2)', cursor: 'pointer' }}>
          <input type="checkbox" checked={autoRefresh} onChange={e => setAutoRefresh(e.target.checked)} />
          Auto-refresh
        </label>
        <Button variant="ghost" size="sm" onClick={load} disabled={loading}>
          {loading ? '…' : '↺ Refresh'}
        </Button>
      </PageHeader>

      <div className="card" style={{ padding: 0 }}>
        {logs.length === 0 ? (
          <div className="empty-state" style={{ padding: 40 }}>
            <div className="empty-icon">📋</div>
            <div className="empty-title">{loading ? 'Loading…' : 'No logs found'}</div>
            <div className="empty-sub">Logs appear when strategies are active</div>
          </div>
        ) : (
          <div style={{ overflowX: 'auto', overflowY: 'hidden' }}>
            <table style={{ width: '100%', fontSize: 11, borderCollapse: 'collapse' }}>
              <thead>
                <tr style={{ borderBottom: '1px solid var(--border)' }}>
                  <th style={{ padding: '10px 16px', textAlign: 'left', color: 'var(--t3)', fontWeight: 600, whiteSpace: 'nowrap' }}>Time</th>
                  <th style={{ padding: '10px 12px', textAlign: 'left', color: 'var(--t3)', fontWeight: 600 }}>Strategy</th>
                  <th style={{ padding: '10px 12px', textAlign: 'left', color: 'var(--t3)', fontWeight: 600 }}>Level</th>
                  <th style={{ padding: '10px 12px', textAlign: 'left', color: 'var(--t3)', fontWeight: 600 }}>Message</th>
                </tr>
              </thead>
              <tbody>
                {logs.map((log, i) => (
                  <tr key={i} style={{
                    borderBottom: '1px solid var(--border)',
                    background: LEVEL_BG[log.level] || 'transparent',
                    transition: 'background 0.1s',
                  }}
                    onMouseEnter={e => e.currentTarget.style.background = 'rgba(59,123,255,0.04)'}
                    onMouseLeave={e => e.currentTarget.style.background = LEVEL_BG[log.level] || 'transparent'}
                  >
                    <td style={{ padding: '7px 16px', color: 'var(--t3)', whiteSpace: 'nowrap', fontFamily: 'monospace' }}>
                      {fmtTs(log.ts)}
                    </td>
                    <td style={{ padding: '7px 12px', whiteSpace: 'nowrap' }}>
                      <span style={{
                        fontSize: 10, fontWeight: 700, padding: '2px 7px', borderRadius: 5,
                        background: 'rgba(59,123,255,0.1)', color: 'var(--accent)',
                      }}>{log.strategy_id || '—'}</span>
                    </td>
                    <td style={{ padding: '7px 12px', whiteSpace: 'nowrap' }}>
                      <span style={{
                        fontSize: 10, fontWeight: 700, color: LEVEL_COLOR[log.level] || 'var(--t2)',
                      }}>{log.level}</span>
                    </td>
                    <td style={{ padding: '7px 12px', color: 'var(--t1)', wordBreak: 'break-word', maxWidth: 600 }}>
                      {log.message}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
