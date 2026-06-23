import { useState, useEffect } from 'react'
import { useLang } from '../i18n'

const API = '/api'
function getApiKey() { return localStorage.getItem('trading_api_key') || '' }

async function apiFetch(path, opts = {}) {
  const key = getApiKey()
  const headers = { 'Content-Type': 'application/json', ...(key ? { 'X-API-Key': key } : {}) }
  const res = await fetch(`${API}${path}`, { headers, ...opts })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

/* ── Shared UI primitives ────────────────────────────────────────────────── */

function Tab({ label, active, onClick }) {
  return (
    <button onClick={onClick} style={{
      padding: '10px 16px', fontSize: 12, fontWeight: 600,
      background: 'none', border: 'none', cursor: 'pointer',
      color: active ? 'var(--t1)' : 'var(--t3)',
      borderBottom: `2px solid ${active ? 'var(--accent)' : 'transparent'}`,
      transition: 'all 0.15s', whiteSpace: 'nowrap',
    }}>
      {label}
    </button>
  )
}

function Field({ label, hint, children }) {
  return (
    <div style={{ marginBottom: 16 }}>
      <label className="label">{label}</label>
      <div style={{ marginTop: 6 }}>{children}</div>
      {hint && <p style={{ fontSize: 11, color: 'var(--t3)', marginTop: 4 }}>{hint}</p>}
    </div>
  )
}

function SaveButton({ onClick, saving, saved, t, label }) {
  return (
    <button className="btn btn-primary" onClick={onClick} disabled={saving}
      style={{ width: '100%', justifyContent: 'center', marginTop: 4 }}>
      {saving ? t('saving') : saved ? `✓ ${t('saved')}` : (label || t('save'))}
    </button>
  )
}

function FlashMsg({ type, text }) {
  if (!text) return null
  const isOk = type === 'ok'
  return (
    <div style={{
      padding: '8px 12px', borderRadius: 8, marginBottom: 12, fontSize: 12,
      background: isOk ? 'rgba(0,217,163,0.08)' : 'rgba(255,60,92,0.08)',
      border: `1px solid ${isOk ? 'rgba(0,217,163,0.2)' : 'rgba(255,60,92,0.2)'}`,
      color: isOk ? 'var(--green)' : 'var(--red)',
    }}>
      {text}
    </div>
  )
}

/* ── Security Tab ────────────────────────────────────────────────────────── */
function SecurityTab({ t }) {
  const [apiKey, setApiKey] = useState(localStorage.getItem('trading_api_key') || '')
  const [saved, setSaved] = useState(false)
  const [tgStatus, setTgStatus] = useState(null)
  const [testing, setTesting] = useState(false)

  const saveKey = () => {
    localStorage.setItem('trading_api_key', apiKey.trim())
    setSaved(true)
    setTimeout(() => setSaved(false), 2000)
  }

  const testTelegram = async () => {
    setTesting(true); setTgStatus(null)
    try {
      await apiFetch('/notifications/test', { method: 'POST' })
      setTgStatus('ok')
    } catch (e) { setTgStatus('error:' + e.message) }
    finally { setTesting(false) }
  }

  return (
    <div>
      <Field label={t('security_api_key_label')} hint={t('security_api_key_hint')}>
        <div style={{ display: 'flex', gap: 8 }}>
          <input
            type="password"
            value={apiKey}
            onChange={e => setApiKey(e.target.value)}
            placeholder={t('security_api_key_placeholder')}
            style={{ flex: 1, fontFamily: 'monospace' }}
            autoComplete="off"
          />
          <button className="btn btn-primary" onClick={saveKey} style={{ flexShrink: 0 }}>
            {saved ? '✓' : t('save')}
          </button>
        </div>
      </Field>

      <div style={{
        padding: '12px 14px', borderRadius: 8, marginTop: 4, marginBottom: 20,
        background: 'rgba(59,123,255,0.05)', border: '1px solid rgba(59,123,255,0.15)',
        fontSize: 11, color: 'var(--t2)', lineHeight: 1.6,
      }}>
        {t('security_api_key_info')}
      </div>

      <div style={{ borderTop: '1px solid var(--border)', paddingTop: 20 }}>
        <div className="label" style={{ marginBottom: 8 }}>{t('security_telegram_label')}</div>
        <p style={{ fontSize: 11, color: 'var(--t3)', marginBottom: 12 }}>{t('security_telegram_hint')}</p>
        <button className="btn btn-ghost" onClick={testTelegram} disabled={testing} style={{ width: '100%', justifyContent: 'center' }}>
          {testing ? t('saving') : t('security_telegram_test')}
        </button>
        {tgStatus === 'ok' && <FlashMsg type="ok" text={t('security_telegram_ok')} />}
        {tgStatus?.startsWith('error:') && <FlashMsg type="error" text={tgStatus.slice(6)} />}
      </div>
    </div>
  )
}

/* ── Risk Tab ────────────────────────────────────────────────────────────── */
function RiskTab({ settings, onReload, t }) {
  const [form, setForm] = useState({})
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => { if (settings?.risk) setForm({ ...settings.risk }) }, [settings])

  const set = (k, v) => setForm(p => ({ ...p, [k]: v }))

  const handleSave = async () => {
    setSaving(true); setError(null)
    try {
      await apiFetch('/settings/risk', { method: 'POST', body: JSON.stringify(form) })
      onReload()
      setSaved(true); setTimeout(() => setSaved(false), 2000)
    } catch (e) { setError(e.message) }
    finally { setSaving(false) }
  }

  return (
    <div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))', gap: '0 16px' }}>
        <Field label={t('max_pos')}>
          <input type="number" value={form.max_position_usdt ?? 0} min="0" step="100"
            onChange={e => set('max_position_usdt', Number(e.target.value))} style={{ width: '100%' }} />
        </Field>
        <Field label={t('max_order')}>
          <input type="number" value={form.max_order_usdt ?? 0} min="0" step="50"
            onChange={e => set('max_order_usdt', Number(e.target.value))} style={{ width: '100%' }} />
        </Field>
        <Field label={t('max_daily_loss')}>
          <input type="number" value={form.max_daily_loss_usdt ?? 0} min="0" step="50"
            onChange={e => set('max_daily_loss_usdt', Number(e.target.value))} style={{ width: '100%' }} />
        </Field>
        <Field label={t('max_open_orders_label')}>
          <input type="number" value={form.max_open_orders ?? 0} min="1" step="1"
            onChange={e => set('max_open_orders', Number(e.target.value))} style={{ width: '100%' }} />
        </Field>
        <Field label={t('max_drawdown_label')} hint="0 = disabled">
          <input type="number" value={form.max_drawdown_pct ?? 0} min="0" max="100" step="1"
            onChange={e => set('max_drawdown_pct', Number(e.target.value))} style={{ width: '100%' }} />
        </Field>
        <Field label={t('max_concentration_label')} hint="0 = disabled">
          <input type="number" value={form.max_symbol_concentration_pct ?? 0} min="0" max="100" step="5"
            onChange={e => set('max_symbol_concentration_pct', Number(e.target.value))} style={{ width: '100%' }} />
        </Field>
        <Field label={t('max_rolling_7d_label')} hint="0 = disabled">
          <input type="number" value={form.max_rolling_7d_loss_usdt ?? 0} min="0" step="10"
            onChange={e => set('max_rolling_7d_loss_usdt', Number(e.target.value))} style={{ width: '100%' }} />
        </Field>
        <Field label={t('max_rolling_30d_label')} hint="0 = disabled">
          <input type="number" value={form.max_rolling_30d_loss_usdt ?? 0} min="0" step="20"
            onChange={e => set('max_rolling_30d_loss_usdt', Number(e.target.value))} style={{ width: '100%' }} />
        </Field>
      </div>
      <Field label={t('risk_switch')}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <label className="toggle">
            <input type="checkbox" checked={!!form.enabled} onChange={() => set('enabled', !form.enabled)} />
            <span className="toggle-track"><span className="toggle-thumb" /></span>
          </label>
          <span style={{ fontSize: 13, color: form.enabled ? 'var(--green)' : 'var(--t3)' }}>
            {form.enabled ? t('risk_on') : t('risk_off')}
          </span>
        </div>
      </Field>
      <FlashMsg type="error" text={error} />
      <SaveButton onClick={handleSave} saving={saving} saved={saved} t={t} />
    </div>
  )
}

/* ── Engine Tab ──────────────────────────────────────────────────────────── */
function EngineTab({ settings, onReload, t }) {
  const [symbols, setSymbols] = useState([])
  const [input, setInput] = useState('')
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => { if (settings?.engine?.symbols) setSymbols([...settings.engine.symbols]) }, [settings])

  const add = () => {
    const s = input.trim().toUpperCase()
    if (!s) return
    const normalized = s.includes('-') ? s : s.replace(/USDT$/, '-USDT')
    if (!symbols.includes(normalized)) setSymbols(p => [...p, normalized])
    setInput('')
  }

  const handleSave = async () => {
    setSaving(true); setError(null)
    try {
      await apiFetch('/settings/engine', { method: 'POST', body: JSON.stringify({ symbols }) })
      onReload()
      setSaved(true); setTimeout(() => setSaved(false), 2000)
    } catch (e) { setError(e.message) }
    finally { setSaving(false) }
  }

  return (
    <div>
      <Field label={t('symbols_label')} hint={t('enter_symbol')}>
        <div style={{ display: 'flex', gap: 8, marginBottom: 10 }}>
          <input value={input} onChange={e => setInput(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && add()}
            placeholder="BTC-USDT / SOLUSDT" style={{ flex: 1 }} />
          <button className="btn btn-ghost" onClick={add} style={{ whiteSpace: 'nowrap', flexShrink: 0 }}>+</button>
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          {symbols.map(s => (
            <div key={s} style={{
              display: 'flex', alignItems: 'center', justifyContent: 'space-between',
              padding: '9px 12px', borderRadius: 8,
              background: 'var(--surface)', border: '1px solid var(--border)',
            }}>
              <span style={{ fontFamily: 'monospace', fontWeight: 600, color: 'var(--t1)', fontSize: 13 }}>{s}</span>
              <button onClick={() => setSymbols(p => p.filter(x => x !== s))}
                style={{ background: 'none', border: 'none', color: 'var(--t3)', cursor: 'pointer', fontSize: 16, lineHeight: 1, padding: '0 2px' }}>
                ×
              </button>
            </div>
          ))}
        </div>
      </Field>
      <FlashMsg type="error" text={error} />
      <SaveButton onClick={handleSave} saving={saving} saved={saved} t={t} />
    </div>
  )
}

/* ── Exchange form ───────────────────────────────────────────────────────── */
function ExchangeForm({ name, current, onSaved, t }) {
  const isOkx = name === 'okx'
  const [form, setForm] = useState({ market_type: current?.market_type || '', testnet: current?.testnet || false })
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState(null)
  const [expanded, setExpanded] = useState(false)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState(null)
  const set = (k, v) => setForm(p => ({ ...p, [k]: v }))

  const handleTest = async () => {
    setTesting(true); setTestResult(null)
    try {
      const res = await apiFetch(`/connectors/${name}/connect`, { method: 'POST' })
      setTestResult({ ok: true, msg: `Connected ✓ (${name})` })
    } catch (e) {
      setTestResult({ ok: false, msg: e.message || 'Connection failed' })
    } finally { setTesting(false); setTimeout(() => setTestResult(null), 4000) }
  }

  const handleSave = async () => {
    const payload = {}
    if (form.api_key?.trim())    payload.api_key    = form.api_key.trim()
    if (form.secret?.trim())     payload.secret     = form.secret.trim()
    if (form.passphrase?.trim()) payload.passphrase = form.passphrase.trim()
    if (form.market_type)        payload.market_type = form.market_type
    payload.testnet = form.testnet

    setSaving(true); setError(null)
    try {
      await apiFetch(`/settings/exchange/${name}`, { method: 'POST', body: JSON.stringify(payload) })
      onSaved()
      setSaved(true); setTimeout(() => setSaved(false), 3000)
      setForm(p => ({ ...p, api_key: '', secret: '', passphrase: '' }))
    } catch (e) { setError(e.message) }
    finally { setSaving(false) }
  }

  return (
    <div style={{ border: '1px solid var(--border)', borderRadius: 10, overflow: 'hidden', marginBottom: 12 }}>
      {/* Collapsed header */}
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '12px 16px', background: 'var(--surface)', cursor: 'pointer',
      }} onClick={() => setExpanded(p => !p)}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <span style={{
            width: 8, height: 8, borderRadius: '50%', flexShrink: 0,
            background: current?.api_key_set ? 'var(--green)' : 'var(--red)',
            boxShadow: current?.api_key_set ? '0 0 6px var(--green)' : 'none',
          }} />
          <span style={{ fontWeight: 700, textTransform: 'capitalize', color: 'var(--t1)', fontSize: 13 }}>{name}</span>
          <span style={{
            fontSize: 10, padding: '2px 8px', borderRadius: 20,
            background: 'rgba(59,123,255,0.1)', color: 'var(--accent)',
            border: '1px solid rgba(59,123,255,0.2)',
          }}>{current?.market_type}</span>
          {current?.testnet && (
            <span style={{
              fontSize: 10, padding: '2px 8px', borderRadius: 20,
              background: 'rgba(245,158,11,0.1)', color: 'var(--yellow)',
              border: '1px solid rgba(245,158,11,0.2)',
            }}>testnet</span>
          )}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ fontSize: 11, color: current?.api_key_set ? 'var(--t3)' : 'var(--red)' }} className="hide-mobile">
            {current?.api_key_hint}
          </span>
          <span style={{ color: 'var(--t3)', fontSize: 12 }}>{expanded ? '▲' : '▼'}</span>
        </div>
      </div>

      {/* Expanded form */}
      {expanded && (
        <div style={{ padding: 16, borderTop: '1px solid var(--border)', background: 'var(--bg2)' }}>
          <div style={{
            padding: '10px 12px', borderRadius: 8, marginBottom: 16,
            background: 'rgba(59,123,255,0.06)', border: '1px solid rgba(59,123,255,0.15)',
            fontSize: 11, color: 'var(--accent)',
          }}>
            {t('leave_blank')}
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))', gap: '0 16px' }}>
            <Field label={t('api_key')}>
              <input value={form.api_key || ''} onChange={e => set('api_key', e.target.value)}
                placeholder={current?.api_key_hint || t('api_key')} style={{ width: '100%' }} />
            </Field>
            <Field label={t('api_secret')}>
              <input type="password" value={form.secret || ''} onChange={e => set('secret', e.target.value)}
                placeholder={t('api_secret')} style={{ width: '100%', fontFamily: 'monospace' }} />
            </Field>
            {isOkx && (
              <Field label={t('passphrase')}>
                <input type="password" value={form.passphrase || ''} onChange={e => set('passphrase', e.target.value)}
                  placeholder={t('passphrase')} style={{ width: '100%', fontFamily: 'monospace' }} />
              </Field>
            )}
            <Field label={t('market_type')}>
              <select value={form.market_type} onChange={e => set('market_type', e.target.value)} style={{ width: '100%' }}>
                {(name === 'binance' ? ['futures', 'spot'] : ['swap', 'spot'])
                  .map(v => <option key={v} value={v}>{v}</option>)}
              </select>
            </Field>
          </div>

          <Field label={t('testnet')}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <label className="toggle">
                <input type="checkbox" checked={!!form.testnet} onChange={() => set('testnet', !form.testnet)} />
                <span className="toggle-track"><span className="toggle-thumb" /></span>
              </label>
              <span style={{ fontSize: 13, color: form.testnet ? 'var(--yellow)' : 'var(--t3)' }}>
                {form.testnet ? t('risk_on') : t('risk_off')}
              </span>
            </div>
          </Field>

          <FlashMsg type="error" text={error} />

          {/* Test + Save row */}
          <div style={{ display: 'flex', gap: 8, marginTop: 4 }}>
            <button className="btn btn-ghost" onClick={handleTest} disabled={testing}
              style={{ flex: 1, justifyContent: 'center' }}>
              {testing ? '…' : '🔌 Test Connection'}
            </button>
            <SaveButton onClick={handleSave} saving={saving} saved={saved} t={t}
              label={`${t('save')} ${name.toUpperCase()}`} />
          </div>
          {testResult && (
            <div style={{
              marginTop: 8, padding: '7px 12px', borderRadius: 7, fontSize: 11, fontWeight: 600,
              background: testResult.ok ? 'rgba(0,217,163,0.08)' : 'rgba(255,60,92,0.08)',
              color: testResult.ok ? 'var(--green)' : 'var(--red)',
              border: `1px solid ${testResult.ok ? 'rgba(0,217,163,0.2)' : 'rgba(255,60,92,0.2)'}`,
            }}>
              {testResult.msg}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function ExchangeTab({ settings, onReload, t }) {
  const exchanges = settings?.exchanges || {}
  return (
    <div>
      {Object.entries(exchanges).map(([name, cfg]) => (
        <ExchangeForm key={name} name={name} current={cfg} onSaved={onReload} t={t} />
      ))}
    </div>
  )
}

/* ── Main modal ──────────────────────────────────────────────────────────── */
export default function SettingsModal({ onClose }) {
  const { t } = useLang()
  const hasKey = !!localStorage.getItem('trading_api_key')
  const [activeTab, setActiveTab] = useState(hasKey ? 'exchange' : 'security')
  const [settings, setSettings] = useState(null)
  const [loadErr, setLoadErr] = useState(false)

  const load = async () => {
    setLoadErr(false)
    try { setSettings(await apiFetch('/settings')) }
    catch (e) { setLoadErr(true); console.error('Failed to load settings:', e) }
  }

  useEffect(() => { load() }, [])

  const tabs = [
    { key: 'exchange', label: t('tab_exchange') },
    { key: 'risk',     label: t('tab_risk') },
    { key: 'engine',   label: t('tab_engine') },
    { key: 'security', label: t('tab_security') },
  ]

  return (
    <div className="modal-backdrop" onClick={e => e.target === e.currentTarget && onClose()}>
      <div className="modal" style={{ padding: 0, display: 'flex', flexDirection: 'column', maxWidth: 580 }}>
        {/* Header */}
        <div style={{
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          padding: '16px 20px', borderBottom: '1px solid var(--border)', flexShrink: 0,
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <div style={{
              width: 30, height: 30, borderRadius: 8,
              background: 'linear-gradient(135deg, var(--accent), #7c3aed)',
              display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 14,
            }}>⚙</div>
            <span style={{ fontWeight: 700, fontSize: 15, color: 'var(--t1)' }}>{t('settings_title')}</span>
          </div>
          <button onClick={onClose} className="btn-icon" style={{ fontSize: 20, color: 'var(--t3)' }}>×</button>
        </div>

        {/* Tabs */}
        <div style={{
          display: 'flex', borderBottom: '1px solid var(--border)',
          padding: '0 8px', overflowX: 'auto', overflowY: 'hidden', flexShrink: 0,
        }}>
          {tabs.map(tab => <Tab key={tab.key} label={tab.label} active={activeTab === tab.key} onClick={() => setActiveTab(tab.key)} />)}
        </div>

        {/* Content */}
        <div style={{ flex: 1, overflowY: 'auto', overflowX: 'hidden', padding: 20, minHeight: 0 }}>
          {activeTab === 'security' ? (
            <SecurityTab t={t} />
          ) : !settings && loadErr ? (
            <div style={{ color: 'var(--t3)', fontSize: 13, textAlign: 'center', padding: 32 }}>
              <div style={{ marginBottom: 8, color: 'var(--red)' }}>⚠ {t('security_api_key_label')} 未设置</div>
              <div style={{ fontSize: 11 }}>请切换到「安全」标签输入 API Key</div>
            </div>
          ) : !settings ? (
            <div style={{ color: 'var(--t3)', fontSize: 13, textAlign: 'center', padding: 32 }}>{t('loading')}…</div>
          ) : activeTab === 'exchange' ? (
            <ExchangeTab settings={settings} onReload={load} t={t} />
          ) : activeTab === 'risk' ? (
            <RiskTab settings={settings} onReload={load} t={t} />
          ) : (
            <EngineTab settings={settings} onReload={load} t={t} />
          )}
        </div>
      </div>
    </div>
  )
}
