import { useState, useEffect, useCallback } from 'react'
import { useLang } from '../i18n'
import { Button, PageHeader, StatTile, Card, Alert } from '../components/ui'

const API = '/api'
function getApiKey() { return localStorage.getItem('trading_api_key') || '' }
async function apiFetch(path, opts = {}) {
  const key = getApiKey()
  const headers = { 'Content-Type': 'application/json', ...(key ? { 'X-API-Key': key } : {}), ...opts.headers }
  const res = await fetch(`${API}${path}`, { ...opts, headers })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

/* ── Telegram bot settings ───────────────────────────────────────────────── */
function TelegramSection() {
  const [cfg, setCfg]       = useState(null)
  const [token, setToken]   = useState('')
  const [chatId, setChatId] = useState('')
  const [msg, setMsg]       = useState(null)   // { ok, text }
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)

  const flash = (ok, text) => { setMsg({ ok, text }); setTimeout(() => setMsg(null), 4000) }

  const load = useCallback(async () => {
    try { setCfg(await apiFetch('/notifications/config')) } catch { /* ignore */ }
  }, [])

  useEffect(() => { load() }, [load])

  const save = async () => {
    if (!token && !chatId) return
    setSaving(true)
    try {
      const res = await apiFetch('/notifications/config', {
        method: 'POST',
        body: JSON.stringify({ token: token || undefined, chat_id: chatId || undefined }),
      })
      setToken(''); setChatId('')
      await load()
      flash(true, res.enabled ? '已保存，Telegram 已启用' : '已保存（未完整配置）')
    } catch (e) {
      flash(false, '保存失败：' + e.message)
    } finally { setSaving(false) }
  }

  const test = async () => {
    setTesting(true)
    try {
      await apiFetch('/notifications/test', { method: 'POST' })
      flash(true, '✅ 测试消息已发送，请检查 Telegram')
    } catch (e) {
      flash(false, '❌ 发送失败：' + e.message)
    } finally { setTesting(false) }
  }

  const clear = async () => {
    try {
      await apiFetch('/notifications/config', { method: 'DELETE' })
      await load()
      flash(true, '已清除 Telegram 配置')
    } catch { flash(false, '清除失败') }
  }

  const enabled = cfg?.live_enabled

  return (
    <Card style={{ padding: '20px 22px' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 16, flexWrap: 'wrap', gap: 8 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span className="label">Telegram 告警机器人</span>
          <span style={{
            fontSize: 10, fontWeight: 700, padding: '2px 8px', borderRadius: 10,
            background: enabled ? 'rgba(0,217,163,0.12)' : 'rgba(255,255,255,0.05)',
            color: enabled ? 'var(--green)' : 'var(--t3)',
            border: `1px solid ${enabled ? 'rgba(0,217,163,0.25)' : 'var(--border)'}`,
          }}>
            {enabled ? '● 已启用' : '○ 未启用'}
          </span>
        </div>
        {enabled && (
          <Button variant="ghost" size="sm" onClick={test} disabled={testing}>
            {testing ? '发送中…' : '📨 发送测试'}
          </Button>
        )}
      </div>

      {cfg && enabled && (
        <div style={{ display: 'flex', gap: 16, marginBottom: 16, flexWrap: 'wrap' }}>
          <div style={{ fontSize: 12, color: 'var(--t3)' }}>
            Chat ID：<span style={{ color: 'var(--t1)', fontWeight: 600 }}>{cfg.chat_id}</span>
          </div>
          <div style={{ fontSize: 12, color: 'var(--t3)' }}>
            Token：<span style={{ color: 'var(--t2)' }}>…{cfg.token_suffix}</span>
          </div>
        </div>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, marginBottom: 12 }}>
        <div>
          <div style={{ fontSize: 11, color: 'var(--t3)', marginBottom: 5 }}>Bot Token</div>
          <input
            type="password"
            value={token}
            onChange={e => setToken(e.target.value)}
            placeholder={cfg?.token_set ? `当前已设置（…${cfg.token_suffix}）` : '从 @BotFather 获取'}
            style={{
              width: '100%', boxSizing: 'border-box',
              background: 'var(--bg2)', border: '1px solid var(--border)', borderRadius: 8,
              color: 'var(--t1)', fontSize: 12, padding: '8px 12px', outline: 'none',
            }}
          />
        </div>
        <div>
          <div style={{ fontSize: 11, color: 'var(--t3)', marginBottom: 5 }}>Chat ID</div>
          <input
            type="text"
            value={chatId}
            onChange={e => setChatId(e.target.value)}
            placeholder={cfg?.chat_id || '从 @userinfobot 获取'}
            style={{
              width: '100%', boxSizing: 'border-box',
              background: 'var(--bg2)', border: '1px solid var(--border)', borderRadius: 8,
              color: 'var(--t1)', fontSize: 12, padding: '8px 12px', outline: 'none',
            }}
          />
        </div>
      </div>

      <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
        <Button variant="primary" size="sm" onClick={save} disabled={saving || (!token && !chatId)}>
          {saving ? '保存中…' : '保存配置'}
        </Button>
        {enabled && (
          <Button variant="ghost" size="sm" onClick={clear} style={{ color: 'var(--red)' }}>
            清除
          </Button>
        )}
        {msg && (
          <span style={{ fontSize: 12, color: msg.ok ? 'var(--green)' : 'var(--red)' }}>
            {msg.text}
          </span>
        )}
      </div>

      <div style={{ marginTop: 14, padding: '10px 14px', borderRadius: 8, background: 'var(--surface)', fontSize: 11, color: 'var(--t3)', lineHeight: 1.8 }}>
        获取方式：①&nbsp;在 Telegram 搜索 <b style={{color:'var(--t2)'}}>@BotFather</b> → /newbot → 复制 Token<br/>
        ②&nbsp;搜索 <b style={{color:'var(--t2)'}}>@userinfobot</b> → /start → 复制 Chat ID（你的用户 ID）<br/>
        告警触发：暂停/恢复、成交、策略错误、损失预警、健康状态变化
      </div>
    </Card>
  )
}

/* ── Alert rules section ─────────────────────────────────────────────────── */
function AlertRulesSection({ t }) {
  const [rules, setRules] = useState([])
  const [form, setForm] = useState({ exchange: 'binance', symbol: 'BTC-USDT', type: 'price_above', price: '' })
  const [adding, setAdding] = useState(false)
  const [showForm, setShowForm] = useState(false)
  const set = (k, v) => setForm(p => ({ ...p, [k]: v }))

  const load = useCallback(async () => {
    try { setRules(await apiFetch('/alerts')) } catch { /* ignore */ }
  }, [])

  useEffect(() => { load() }, [load])

  const handleAdd = async () => {
    if (!form.symbol || !form.price) return
    setAdding(true)
    try {
      await apiFetch('/alerts', { method: 'POST', body: JSON.stringify({ ...form, price: Number(form.price) }) })
      setForm(p => ({ ...p, price: '' }))
      setShowForm(false)
      load()
    } catch { /* ignore */ } finally { setAdding(false) }
  }

  const handleDelete = async (id) => {
    try { await apiFetch(`/alerts/${id}`, { method: 'DELETE' }); load() } catch { /* ignore */ }
  }

  const handleReset = async (id) => {
    try { await apiFetch(`/alerts/${id}/reset`, { method: 'POST' }); load() } catch { /* ignore */ }
  }

  return (
    <Card style={{ overflow: 'hidden' }}>
      <div className="card-header">
        <span className="section-title">{t('alerts_title')}</span>
        <Button variant="ghost" size="sm" onClick={() => setShowForm(p => !p)}>
          {showForm ? '—' : t('alerts_add')}
        </Button>
      </div>

      {showForm && (
        <div style={{ padding: '12px 20px', borderBottom: '1px solid var(--border)', background: 'var(--surface)' }}>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))', gap: 10, marginBottom: 10 }}>
            <div>
              <label className="label">{t('alerts_exchange')}</label>
              <select value={form.exchange} onChange={e => set('exchange', e.target.value)} style={{ width: '100%', marginTop: 5 }}>
                <option value="binance">Binance</option>
                <option value="okx">OKX</option>
              </select>
            </div>
            <div>
              <label className="label">{t('alerts_symbol')}</label>
              <input value={form.symbol} onChange={e => set('symbol', e.target.value.toUpperCase())}
                style={{ width: '100%', marginTop: 5 }} placeholder="BTC-USDT" />
            </div>
            <div>
              <label className="label">{t('alerts_type')}</label>
              <select value={form.type} onChange={e => set('type', e.target.value)} style={{ width: '100%', marginTop: 5 }}>
                <option value="price_above">{t('alerts_above')}</option>
                <option value="price_below">{t('alerts_below')}</option>
              </select>
            </div>
            <div>
              <label className="label">{t('alerts_price')}</label>
              <input type="number" value={form.price} onChange={e => set('price', e.target.value)}
                style={{ width: '100%', marginTop: 5 }} placeholder="0.00" />
            </div>
          </div>
          <Button variant="primary" size="sm" onClick={handleAdd} disabled={adding || !form.price}>
            {adding ? '…' : t('alerts_add')}
          </Button>
        </div>
      )}

      {rules.length === 0 ? (
        <div style={{ padding: '28px 22px', color: 'var(--t4)', fontSize: 13, textAlign: 'center' }}>
          <div style={{ marginBottom: 6 }}>{t('alerts_no_rules')}</div>
          <div style={{ fontSize: 11, color: 'var(--t4)' }}>{t('alerts_no_rules_sub')}</div>
        </div>
      ) : (
        <div className="table-wrap">
          <table>
            <thead><tr>
              <th>{t('alerts_exchange')}</th>
              <th>{t('alerts_symbol')}</th>
              <th>{t('alerts_type')}</th>
              <th style={{ textAlign: 'right' }}>{t('alerts_price')}</th>
              <th>{t('th_status')}</th>
              <th style={{ textAlign: 'right' }}></th>
            </tr></thead>
            <tbody>
              {rules.map(r => (
                <tr key={r.id}>
                  <td style={{ textTransform: 'capitalize', color: 'var(--t2)' }}>{r.exchange}</td>
                  <td style={{ fontWeight: 600 }}>{r.symbol}</td>
                  <td style={{ fontSize: 11, color: 'var(--t3)' }}>
                    {r.type === 'price_above' ? '↑ ' + t('alerts_above') : '↓ ' + t('alerts_below')}
                  </td>
                  <td className="num" style={{ textAlign: 'right' }}>
                    {Number(r.price).toLocaleString(undefined, { minimumFractionDigits: 2 })}
                  </td>
                  <td>
                    <span className={`badge ${r.triggered ? 'badge-filled' : 'badge-open'}`}>
                      {r.triggered ? t('alerts_triggered') : t('alerts_active')}
                    </span>
                  </td>
                  <td style={{ textAlign: 'right' }}>
                    <div style={{ display: 'flex', gap: 6, justifyContent: 'flex-end' }}>
                      {r.triggered && (
                        <Button size="xs" variant="ghost" onClick={() => handleReset(r.id)}>
                          {t('alerts_reset')}
                        </Button>
                      )}
                      <Button size="xs" variant="red" onClick={() => handleDelete(r.id)}>
                        {t('alerts_delete')}
                      </Button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  )
}

/* ── Strategy log viewer ─────────────────────────────────────────────────── */
function LogViewer({ t }) {
  const [logs, setLogs] = useState([])
  const [stratId, setStratId] = useState('')
  const [level, setLevel] = useState('')
  const [limit, setLimit] = useState(100)
  const [loading, setLoading] = useState(false)
  const [autoRefresh, setAutoRefresh] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const params = new URLSearchParams({ limit })
      if (stratId) params.set('strategy_id', stratId)
      if (level)   params.set('level', level)
      const data = await apiFetch(`/logs?${params}`)
      setLogs(Array.isArray(data) ? data : [])
    } catch { /* ignore */ } finally { setLoading(false) }
  }, [stratId, level, limit])

  useEffect(() => { load() }, [load])
  useEffect(() => {
    if (!autoRefresh) return
    const iv = setInterval(load, 5000)
    return () => clearInterval(iv)
  }, [autoRefresh, load])

  const levelColor = l => {
    if (l === 'ERROR' || l === 'CRITICAL') return 'var(--red)'
    if (l === 'WARNING') return 'var(--yellow)'
    if (l === 'DEBUG') return 'var(--t4)'
    return 'var(--t2)'
  }

  return (
    <Card style={{ overflow: 'hidden' }}>
      <div className="card-header">
        <span className="section-title">Strategy Logs</span>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
          <input style={{ width: 120, fontSize: 11, padding: '4px 8px' }}
            placeholder="Strategy ID" value={stratId} onChange={e => setStratId(e.target.value)} />
          <select style={{ width: 100, fontSize: 11, padding: '4px 8px' }} value={level} onChange={e => setLevel(e.target.value)}>
            <option value="">All Levels</option>
            <option value="DEBUG">DEBUG</option>
            <option value="INFO">INFO</option>
            <option value="WARNING">WARNING</option>
            <option value="ERROR">ERROR</option>
          </select>
          <Button variant="ghost" size="sm" onClick={load} disabled={loading}>
            {loading ? '…' : t('bt_refresh')}
          </Button>
          <label style={{ display: 'flex', alignItems: 'center', gap: 5, cursor: 'pointer', fontSize: 11, color: autoRefresh ? 'var(--green)' : 'var(--t3)' }}>
            <input type="checkbox" checked={autoRefresh} onChange={e => setAutoRefresh(e.target.checked)} style={{ accentColor: 'var(--green)' }} />
            Auto
          </label>
        </div>
      </div>
      {logs.length === 0 ? (
        <div style={{ padding: '24px', color: 'var(--t4)', fontSize: 12, textAlign: 'center' }}>No logs yet</div>
      ) : (
        <div style={{ maxHeight: 360, overflowY: 'auto', overflowX: 'hidden', fontFamily: 'monospace', fontSize: 11 }}>
          {logs.map((log, i) => (
            <div key={i} style={{
              padding: '4px 16px', borderBottom: '1px solid var(--border)',
              display: 'flex', gap: 12, alignItems: 'flex-start',
              background: log.level === 'ERROR' || log.level === 'CRITICAL' ? 'rgba(255,60,92,0.04)' : 'transparent',
            }}>
              <span style={{ color: 'var(--t4)', flexShrink: 0, fontSize: 10 }}>
                {new Date(log.ts * 1000).toLocaleTimeString()}
              </span>
              <span style={{ flexShrink: 0, width: 60, fontWeight: 700, color: levelColor(log.level), fontSize: 10 }}>
                {log.level}
              </span>
              {log.strategy_id && (
                <span style={{ flexShrink: 0, color: 'var(--accent)', fontSize: 10, maxWidth: 100, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  [{log.strategy_id}]
                </span>
              )}
              <span style={{ color: 'var(--t1)', wordBreak: 'break-all', lineHeight: 1.4 }}>{log.message}</span>
            </div>
          ))}
        </div>
      )}
    </Card>
  )
}

/* ── Operational health ──────────────────────────────────────────────────── */
const HEALTH_COLOR = {
  ok: 'var(--green)', degraded: 'var(--yellow)', critical: 'var(--red)',
  paused: 'var(--t3)', idle: 'var(--t4)',
}
const COMPONENT_LABEL = {
  connectors: 'Connectors', feeds: 'Market Feeds',
  event_loop: 'Event Loop', event_queue: 'Event Queue',
}

function HealthSection({ health }) {
  if (!health) return null
  const overall = health.status || 'idle'
  const color = HEALTH_COLOR[overall] || 'var(--t3)'
  return (
    <Card style={{ overflow: 'hidden' }}>
      <div className="card-header">
        <span className="section-title">System Health</span>
        <span style={{
          display: 'inline-flex', alignItems: 'center', gap: 6,
          padding: '3px 12px', borderRadius: 20, fontSize: 11, fontWeight: 700,
          textTransform: 'uppercase', letterSpacing: '0.05em', color,
          background: overall === 'ok' ? 'rgba(0,217,163,0.12)' : overall === 'degraded' ? 'rgba(255,184,0,0.12)' : overall === 'critical' ? 'rgba(255,60,92,0.12)' : 'var(--surface)',
          border: `1px solid ${overall === 'ok' ? 'rgba(0,217,163,0.3)' : overall === 'degraded' ? 'rgba(255,184,0,0.3)' : overall === 'critical' ? 'rgba(255,60,92,0.3)' : 'var(--border)'}`,
        }}>
          <span style={{ width: 6, height: 6, borderRadius: '50%', background: 'currentColor' }} />
          {overall}
        </span>
      </div>
      <div style={{ padding: '4px 12px 12px', display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))', gap: 10, marginTop: 4 }}>
        {(health.components || []).map(c => {
          const cc = HEALTH_COLOR[c.status] || 'var(--t3)'
          return (
            <div key={c.name} style={{
              padding: '12px 14px', borderRadius: 10, background: 'var(--surface)',
              border: `1px solid ${c.status === 'ok' ? 'rgba(0,217,163,0.2)' : c.status === 'degraded' ? 'rgba(255,184,0,0.2)' : c.status === 'critical' ? 'rgba(255,60,92,0.2)' : 'var(--border)'}`,
            }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 6 }}>
                <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--t1)' }}>
                  {COMPONENT_LABEL[c.name] || c.name}
                </span>
                <span style={{ fontSize: 9, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em', color: cc }}>
                  {c.status}
                </span>
              </div>
              <div style={{ fontSize: 11, color: 'var(--t3)', lineHeight: 1.4 }}>{c.detail}</div>
            </div>
          )
        })}
      </div>
    </Card>
  )
}

function fmtUptime(s) {
  if (!s) return '—'
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60
  if (h > 0) return `${h}h ${m}m`
  if (m > 0) return `${m}m ${sec}s`
  return `${sec}s`
}

function StatusDot({ state }) {
  const c = state === 'connected' ? 'var(--green)' : state === 'error' ? 'var(--red)' : 'var(--border2)'
  return <span style={{ display: 'inline-block', width: 8, height: 8, borderRadius: '50%', background: c, marginRight: 6, flexShrink: 0 }} />
}

export default function SystemPage({ wsConnected }) {
  const { t } = useLang()
  const [status, setStatus]     = useState(null)
  const [health, setHealth]     = useState(null)
  const [latency, setLatency]   = useState(null)
  const [dbInfo, setDbInfo]     = useState(null)
  const [loading, setLoading]   = useState({})
  const [msg, setMsg]           = useState(null)
  const [showRestart, setShowRestart] = useState(false)

  const load = useCallback(async () => {
    try { setStatus(await apiFetch('/system/status')) } catch { /* ignore */ }
    try { setHealth(await apiFetch('/health/detail')) } catch { /* ignore */ }
    try { setLatency(await apiFetch('/latency')) } catch { /* ignore */ }
    try { setDbInfo(await apiFetch('/data/db-size')) } catch { /* ignore */ }
  }, [])

  useEffect(() => { load() }, [load])
  useEffect(() => {
    const iv = setInterval(load, 3000)
    return () => clearInterval(iv)
  }, [load])

  const flash = (text, ok = true) => {
    setMsg({ text, ok })
    setTimeout(() => setMsg(null), 3500)
  }

  const act = async (key, fn) => {
    setLoading(l => ({ ...l, [key]: true }))
    try { await fn(); await load() }
    catch (e) { flash(e.message, false) }
    finally { setLoading(l => ({ ...l, [key]: false })) }
  }

  const enginePause  = () => act('pause',   () => apiFetch('/engine/pause',  { method: 'POST' }).then(() => flash(t('sys_paused'))))
  const engineResume = () => act('resume',  () => apiFetch('/engine/resume', { method: 'POST' }).then(() => flash(t('sys_resumed'))))
  const connectEx    = ex => act(`conn_${ex}`,    () => apiFetch(`/connectors/${ex}/connect`,    { method: 'POST' }).then(() => flash(`${ex} connected`)))
  const disconnectEx = ex => act(`disconn_${ex}`, () => apiFetch(`/connectors/${ex}/disconnect`, { method: 'POST' }).then(() => flash(`${ex} disconnected`)))
  const doRestart    = () => act('restart', async () => {
    await apiFetch('/system/restart', { method: 'POST' })
    flash(t('sys_restarting'))
    setShowRestart(false)
  })

  const connectors = status?.connector_states || {}
  const engineActive = status?.active
  const exchanges = Object.keys(connectors)

  return (
    <div className="page">

      <PageHeader title={t('sys_title')}>
        {msg && (
          <span style={{ fontSize: 12, color: msg.ok ? 'var(--green)' : 'var(--red)', fontWeight: 600 }}>
            {msg.text}
          </span>
        )}
      </PageHeader>

      {/* Top row: engine + connectivity */}
      <div className="grid-2">

        {/* Engine status */}
        <Card style={{ padding: '20px 22px' }}>
          <div className="label" style={{ marginBottom: 16 }}>{t('sys_engine')}</div>

          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 18 }}>
            <span style={{
              display: 'inline-flex', alignItems: 'center', gap: 6,
              padding: '4px 12px', borderRadius: 20,
              background: engineActive ? 'rgba(0,217,163,0.1)' : 'rgba(255,60,92,0.1)',
              border: `1px solid ${engineActive ? 'rgba(0,217,163,0.25)' : 'rgba(255,60,92,0.25)'}`,
              fontSize: 11, fontWeight: 700,
              color: engineActive ? 'var(--green)' : 'var(--red)',
            }}>
              <span style={{ width: 6, height: 6, borderRadius: '50%', background: 'currentColor' }} />
              {engineActive ? t('sys_active') : t('sys_paused_state')}
            </span>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(110px, 1fr))', gap: 8, marginBottom: 18 }}>
            <StatTile label={t('sys_uptime')}     value={fmtUptime(status?.uptime_seconds)} />
            <StatTile label={t('sys_strategies')} value={status?.strategy_count ?? '—'} />
            <StatTile label={t('sys_symbols')}    value={status?.symbol_count ?? '—'} />
            <StatTile label="WebSocket"            value={wsConnected ? t('ws_live') : t('ws_offline')} />
          </div>

          <div style={{ display: 'flex', gap: 10 }}>
            {engineActive ? (
              <Button variant="yellow" size="sm" onClick={enginePause} disabled={loading.pause}>
                {loading.pause ? '…' : `⏸ ${t('sys_btn_pause')}`}
              </Button>
            ) : (
              <Button variant="green" size="sm" onClick={engineResume} disabled={loading.resume}>
                {loading.resume ? '…' : `▶ ${t('sys_btn_resume')}`}
              </Button>
            )}
          </div>
        </Card>

        {/* Connectivity */}
        <Card style={{ padding: '20px 22px' }}>
          <div className="label" style={{ marginBottom: 16 }}>{t('sys_connectivity')}</div>

          <div style={{ marginBottom: 16 }}>
            <div style={{ fontSize: 11, color: 'var(--t3)', marginBottom: 8 }}>{t('sys_ws_status')}</div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '10px 12px', background: 'var(--surface)', borderRadius: 8 }}>
              <span className={`dot ${wsConnected ? 'dot-green blink' : 'dot-muted'}`} />
              <span style={{ fontSize: 13, color: wsConnected ? 'var(--green)' : 'var(--t3)', fontWeight: 600 }}>
                {wsConnected ? t('ws_live') : t('ws_offline')}
              </span>
            </div>
          </div>

          <div>
            <div style={{ fontSize: 11, color: 'var(--t3)', marginBottom: 8 }}>{t('sys_monitored_symbols')}</div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
              {(status?.symbols || []).map(s => (
                <span key={s} style={{
                  padding: '3px 10px', borderRadius: 6,
                  background: 'rgba(59,123,255,0.1)', border: '1px solid rgba(59,123,255,0.2)',
                  fontSize: 11, fontWeight: 600, color: 'var(--accent)',
                }}>{s}</span>
              ))}
              {!status?.symbols?.length && <span style={{ fontSize: 12, color: 'var(--t4)' }}>—</span>}
            </div>
          </div>
        </Card>
      </div>

      {/* Operational health */}
      <HealthSection health={health} />

      {/* Exchange connectors */}
      <Card style={{ overflow: 'hidden' }}>
        <div className="card-header">
          <span className="section-title">{t('sys_connectors')}</span>
        </div>
        {exchanges.length === 0 ? (
          <div style={{ padding: '20px 22px', color: 'var(--t4)', fontSize: 13 }}>{t('sys_no_connectors')}</div>
        ) : (
          <div style={{ padding: '0 12px 12px', display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))', gap: 10, marginTop: 4 }}>
            {exchanges.map(ex => {
              const state = connectors[ex]
              const isConn = state === 'connected'
              return (
                <div key={ex} style={{
                  padding: '14px 16px', borderRadius: 10,
                  background: 'var(--surface)',
                  border: `1px solid ${isConn ? 'rgba(0,217,163,0.18)' : 'var(--border)'}`,
                  display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10,
                }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <StatusDot state={state} />
                    <div>
                      <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--t1)', textTransform: 'capitalize' }}>{ex}</div>
                      <div style={{ fontSize: 10, fontWeight: 600, marginTop: 2,
                                    color: isConn ? 'var(--green)' : state === 'error' ? 'var(--red)' : 'var(--t4)' }}>
                        {state === 'connected' ? t('sys_connected') : state === 'error' ? t('sys_error') : t('sys_disconnected')}
                      </div>
                    </div>
                  </div>
                  {isConn ? (
                    <Button size="xs" variant="red" onClick={() => disconnectEx(ex)} disabled={loading[`disconn_${ex}`]}>
                      {loading[`disconn_${ex}`] ? '…' : t('sys_disconnect')}
                    </Button>
                  ) : (
                    <Button size="xs" variant="green" onClick={() => connectEx(ex)} disabled={loading[`conn_${ex}`]}>
                      {loading[`conn_${ex}`] ? '…' : t('sys_connect')}
                    </Button>
                  )}
                </div>
              )
            })}
          </div>
        )}
      </Card>

      {/* Telegram bot */}
      <TelegramSection />

      {/* Alert rules */}
      <AlertRulesSection t={t} />

      {/* Latency + DB */}
      <div className="grid-2">
        {latency && latency.stats?.length > 0 && (
          <Card style={{ padding: '18px 20px' }}>
            <div className="label" style={{ marginBottom: 14 }}>
              {t('risk_latency_title')}
              {latency.has_alerts && <span style={{ marginLeft: 8, color: 'var(--red)' }}>⚠ Alert</span>}
            </div>
            <div className="table-wrap">
              <table>
                <thead><tr>
                  <th>Exchange</th><th>Type</th>
                  <th style={{ textAlign: 'right' }}>p50</th>
                  <th style={{ textAlign: 'right' }}>p99</th>
                  <th style={{ textAlign: 'right' }}>Max</th>
                </tr></thead>
                <tbody>
                  {latency.stats.map((s, i) => (
                    <tr key={i}>
                      <td style={{ fontSize: 11, textTransform: 'capitalize' }}>{s.exchange}</td>
                      <td style={{ fontSize: 10, color: 'var(--t3)' }}>{s.category?.toUpperCase()}</td>
                      <td className="num" style={{ textAlign: 'right', fontSize: 11 }}>{s.p50_ms}ms</td>
                      <td className="num" style={{ textAlign: 'right', fontSize: 11, fontWeight: 600, color: s.alert ? 'var(--red)' : s.p99_ms > s.alert_threshold_ms * 0.7 ? 'var(--yellow)' : 'var(--green)' }}>
                        {s.p99_ms}ms
                      </td>
                      <td className="num" style={{ textAlign: 'right', fontSize: 11, color: 'var(--t3)' }}>{s.max_ms}ms</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        )}

        {dbInfo && (
          <Card style={{ padding: '18px 20px' }}>
            <div className="label" style={{ marginBottom: 14 }}>Database</div>
            <div style={{ marginBottom: 10 }}>
              <span style={{ fontSize: 13, fontWeight: 700, color: 'var(--t1)' }}>{dbInfo.size_mb} MB</span>
              <span style={{ fontSize: 11, color: 'var(--t3)', marginLeft: 8 }}>SQLite</span>
            </div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 12 }}>
              {Object.entries(dbInfo.tables || {}).map(([tbl, cnt]) => (
                <div key={tbl} style={{ padding: '4px 8px', borderRadius: 6, fontSize: 10, background: 'var(--bg2)', border: '1px solid var(--border)' }}>
                  <span style={{ color: 'var(--t3)' }}>{tbl}</span>
                  <span className="num" style={{ color: 'var(--t1)', fontWeight: 600, marginLeft: 5 }}>{cnt.toLocaleString()}</span>
                </div>
              ))}
            </div>
            <Button variant="ghost" size="sm" style={{ fontSize: 10 }}
              onClick={() => act('purge', () => apiFetch('/data/purge', { method: 'POST' }).then(() => { flash('DB purged'); load() }))}>
              🗑 Purge Old Data
            </Button>
          </Card>
        )}
      </div>

      {/* Strategy logs */}
      <LogViewer t={t} />

      {/* Danger zone */}
      <Card style={{ padding: '20px 22px', border: '1px solid rgba(255,60,92,0.15)' }}>
        <div className="label" style={{ color: 'var(--red)', marginBottom: 14 }}>{t('sys_danger_zone')}</div>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 12 }}>
          <div>
            <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--t1)', marginBottom: 4 }}>{t('sys_restart_title')}</div>
            <div style={{ fontSize: 12, color: 'var(--t3)' }}>{t('sys_restart_desc')}</div>
          </div>
          <Button variant="red" size="sm" onClick={() => setShowRestart(true)} disabled={loading.restart}>
            {t('sys_restart_btn')}
          </Button>
        </div>
      </Card>

      {/* Restart confirm modal */}
      {showRestart && (
        <div className="modal-backdrop" onClick={e => e.target === e.currentTarget && setShowRestart(false)}>
          <div className="modal" style={{ maxWidth: 380 }}>
            <div style={{ fontSize: 16, fontWeight: 700, color: 'var(--t1)', marginBottom: 10 }}>
              {t('sys_restart_confirm_title')}
            </div>
            <div style={{ fontSize: 13, color: 'var(--t3)', marginBottom: 24, lineHeight: 1.6 }}>
              {t('sys_restart_confirm_desc')}
            </div>
            <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end' }}>
              <Button variant="ghost" onClick={() => setShowRestart(false)}>{t('cancel')}</Button>
              <Button variant="red" onClick={doRestart} disabled={loading.restart}>
                {loading.restart ? '…' : t('sys_restart_btn')}
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
