import { useState, useEffect, useCallback, useRef, useMemo, lazy, Suspense } from 'react'
import { LangProvider, useLang } from './i18n'
import Sidebar from './components/Sidebar'
import TopBar from './components/TopBar'
import SettingsModal from './components/SettingsModal'
import ToastContainer from './components/Toast'

// Pages are lazy-loaded so chart-heavy views (Markets/Backtest/Stats/Dex) split
// into their own chunks and the initial bundle stays small.
const DashboardPage  = lazy(() => import('./pages/DashboardPage'))
const MarketsPage    = lazy(() => import('./pages/MarketsPage'))
const PositionsPage  = lazy(() => import('./pages/PositionsPage'))
const OrdersPage     = lazy(() => import('./pages/OrdersPage'))
const StrategiesPage = lazy(() => import('./pages/StrategiesPage'))
const BacktestPage   = lazy(() => import('./pages/BacktestPage'))
const SystemPage     = lazy(() => import('./pages/SystemPage'))
const StatsPage      = lazy(() => import('./pages/StatsPage'))
const TradesPage     = lazy(() => import('./pages/TradesPage'))
const DexPage        = lazy(() => import('./pages/DexPage'))
const RiskPage       = lazy(() => import('./pages/RiskPage'))
const LogsPage       = lazy(() => import('./pages/LogsPage'))
const FuturesPage    = lazy(() => import('./pages/FuturesPage'))

const API = '/api'
function getApiKey() { return localStorage.getItem('trading_api_key') || '' }

async function apiFetch(path, opts = {}) {
  const key = getApiKey()
  const headers = { 'Content-Type': 'application/json', ...(key ? { 'X-API-Key': key } : {}) }
  const res = await fetch(`${API}${path}`, { headers, ...opts })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

const MAX_HISTORY = 100

export default function App() {
  return (
    <LangProvider>
      <AppContent />
    </LangProvider>
  )
}

function AppContent() {
  const { t } = useLang()
  const [page, setPage]               = useState('dashboard')
  const [showSettings, setShowSettings] = useState(false)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [wsConnected, setWsConnected] = useState(false)
  const [status, setStatus]           = useState(null)
  const [tickers, setTickers]         = useState({})
  const [positions, setPositions]     = useState({})
  const [orders, setOrders]           = useState({})
  const [balances, setBalances]       = useState([])
  const [risk, setRisk]               = useState(null)
  const [strategies, setStrategies]   = useState([])
  const [priceHistory, setPriceHistory] = useState({})
  const [symbols, setSymbols]         = useState(['BTC-USDT', 'ETH-USDT'])
  const [toasts, setToasts]           = useState([])
  const [equityRefresh, setEquityRefresh] = useState(0)
  const [regimes, setRegimes]         = useState({})   // symbol → regime snapshot
  const wsRef = useRef(null)
  const equityRefreshTimeRef = useRef(0)
  const regimeToastTimeRef = useRef({})  // symbol → last toast timestamp
  const wsConnectedAtRef = useRef(0)     // timestamp when WS first connected

  const addToast = useCallback((toast) => {
    const id = Date.now() + Math.random()
    setToasts(prev => [...prev.slice(-4), { id, ...toast }])
  }, [])
  const removeToast = useCallback((id) => setToasts(prev => prev.filter(t => t.id !== id)), [])

  // Close sidebar when navigating on mobile
  const navigate = (p) => { setPage(p); setSidebarOpen(false) }

  const loadAll = useCallback(async () => {
    try {
      const [posData, balData, riskData, stratData, statusData, settingsData] = await Promise.allSettled([
        apiFetch('/positions'), apiFetch('/balances'), apiFetch('/risk'),
        apiFetch('/strategies'), apiFetch('/status'), apiFetch('/settings'),
      ])
      if (posData.status === 'fulfilled') {
        const map = {}
        posData.value.forEach(p => { map[`${p.exchange}:${p.symbol}`] = p })
        setPositions(map)
      }
      if (balData.status === 'fulfilled')  setBalances(balData.value)
      if (riskData.status === 'fulfilled') setRisk(riskData.value)
      if (stratData.status === 'fulfilled') setStrategies(stratData.value)
      if (statusData.status === 'fulfilled') setStatus(statusData.value)
      if (settingsData.status === 'fulfilled' && settingsData.value?.engine?.symbols)
        setSymbols(settingsData.value.engine.symbols)
    } catch (e) { console.error('Load failed:', e) }
  }, [])

  const connectWs = useCallback(() => {
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
    const key = getApiKey()
    const url = `${proto}://${window.location.host}/ws${key ? `?key=${encodeURIComponent(key)}` : ''}`
    const ws = new WebSocket(url)
    wsRef.current = ws

    ws.onopen  = () => { setWsConnected(true); wsConnectedAtRef.current = Date.now(); loadAll() }
    ws.onclose = () => { setWsConnected(false); setTimeout(connectWs, 3000) }

    ws.onmessage = (e) => {
      const msg = JSON.parse(e.data)
      switch (msg.type) {
        case 'ticker': {
          const key = `${msg.data.exchange}:${msg.data.symbol}`
          setTickers(prev => ({ ...prev, [key]: msg.data }))
          if (msg.data.last)
            setPriceHistory(prev => {
              const arr = prev[key] || []
              const next = [...arr, Number(msg.data.last)]
              return { ...prev, [key]: next.length > MAX_HISTORY ? next.slice(-MAX_HISTORY) : next }
            })
          break
        }
        case 'position_update': {
          const key = `${msg.data.exchange}:${msg.data.symbol}`
          setPositions(prev => {
            if (msg.data.size === 0) { const n = { ...prev }; delete n[key]; return n }
            return { ...prev, [key]: msg.data }
          })
          break
        }
        case 'order_update': {
          const id = msg.data.order_id
          setOrders(prev => {
            if (['filled','cancelled','canceled','rejected'].includes(msg.data.status)) {
              const n = { ...prev }; delete n[id]; return n
            }
            return { ...prev, [id]: msg.data }
          })
          if (msg.data.status === 'filled') {
            const isBuy = msg.data.side === 'buy'
            const price = msg.data.avg_price || msg.data.price || 0
            addToast({
              type: isBuy ? 'fill_buy' : 'fill_sell',
              title: t('toast_fill_title', msg.data.symbol),
              message: `${isBuy ? t('side_buy_badge') : t('side_sell_badge')} ${Number(msg.data.filled_qty).toFixed(4)} @ ${Number(price).toLocaleString('en', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}${msg.data.strategy_id ? `\n${t('toast_strategy_label')} ${msg.data.strategy_id}` : ''}`,
              duration: 6000,
            })
          }
          break
        }
        case 'balance_update':
          setBalances(prev => {
            const k = `${msg.data.exchange}:${msg.data.asset}`
            return [...prev.filter(b => `${b.exchange}:${b.asset}` !== k), msg.data]
          })
          if (Date.now() - equityRefreshTimeRef.current > 30000) {
            equityRefreshTimeRef.current = Date.now()
            setEquityRefresh(n => n + 1)
          }
          break
        case 'risk_update':
          setRisk(prev => {
            if (msg.data.halted && !prev?.halted)
              addToast({ type: 'halt', title: t('toast_halt_title'), message: t('toast_halt_msg'), duration: 0 })
            else if (!msg.data.halted && prev?.halted)
              addToast({ type: 'info', title: t('toast_resume_title'), message: t('toast_resume_msg') })
            return msg.data
          })
          break
        case 'connector_ready':
          setStatus(prev => prev ? { ...prev, exchanges: [...new Set([...(prev.exchanges||[]), msg.data.exchange])] } : prev)
          break
        case 'connector_error':
          setStatus(prev => prev ? { ...prev, exchanges: (prev.exchanges||[]).filter(ex => ex !== msg.data.exchange) } : prev)
          break
        case 'engine_state':
          setStatus(prev => prev ? { ...prev, active: msg.data.active } : prev)
          break
        case 'alert_triggered':
          addToast({
            type: 'info',
            title: `Alert: ${msg.data.symbol}`,
            message: `${msg.data.message} @ ${Number(msg.data.price).toLocaleString('en', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`,
            duration: 8000,
          })
          break
        case 'regime_update':
          setRegimes(prev => ({ ...prev, [msg.data.symbol]: msg.data }))
          if (msg.data.prev_regime && msg.data.prev_regime !== msg.data.regime) {
            const sym = msg.data.symbol
            const now = Date.now()
            const stableMs = now - wsConnectedAtRef.current
            const lastTs = regimeToastTimeRef.current[sym] || 0
            // Skip first 60s after connect (startup calibration) + 60s cooldown per symbol
            if (stableMs > 60000 && now - lastTs > 60000) {
              regimeToastTimeRef.current[sym] = now
              const isWorsen = ['extreme', 'high'].includes(msg.data.regime)
              addToast({
                type: isWorsen ? 'warn' : 'info',
                title: `Regime: ${sym}`,
                message: `${msg.data.prev_regime?.toUpperCase()} → ${msg.data.regime?.toUpperCase()}`,
                duration: 5000,
              })
            }
          }
          break
        case 'health_update': {
          const st = msg.data?.status
          const bad = (msg.data?.components || []).filter(c => c.status === 'degraded' || c.status === 'critical')
          const detail = bad.map(c => `${c.name}: ${c.detail}`).join('\n')
          if (st === 'critical')
            addToast({ type: 'halt', title: t('toast_health_critical'), message: detail, duration: 0 })
          else if (st === 'degraded')
            addToast({ type: 'warn', title: t('toast_health_degraded'), message: detail, duration: 8000 })
          else if (st === 'ok')
            addToast({ type: 'info', title: t('toast_health_ok'), message: t('toast_health_ok_msg'), duration: 5000 })
          break
        }
      }
    }
  }, [loadAll])

  useEffect(() => {
    connectWs()
    const poll = setInterval(loadAll, 15000)
    return () => { clearInterval(poll); wsRef.current?.close() }
  }, [connectWs, loadAll])

  const handleHalt   = async () => { await apiFetch('/risk/halt',   { method: 'POST' }); setRisk(r => r ? { ...r, halted: true }  : r) }
  const handleResume = async () => { await apiFetch('/risk/resume', { method: 'POST' }); setRisk(r => r ? { ...r, halted: false } : r) }

  const positionsList = useMemo(() => Object.values(positions), [positions])
  const ordersList    = useMemo(() => Object.values(orders), [orders])
  const pageProps     = useMemo(
    () => ({ tickers, positions: positionsList, orders: ordersList, balances, risk, priceHistory, symbols, strategies, equityRefresh, regimes }),
    [tickers, positionsList, ordersList, balances, risk, priceHistory, symbols, strategies, equityRefresh, regimes]
  )

  return (
    <div style={{ display: 'flex', height: '100dvh', overflow: 'hidden', background: 'var(--bg)' }}>

        {/* Mobile overlay */}
        <div
          className={`sidebar-overlay ${sidebarOpen ? 'visible' : ''}`}
          onClick={() => setSidebarOpen(false)}
        />

        <Sidebar
          page={page}
          onNavigate={navigate}
          onSettings={() => { setSidebarOpen(false); setShowSettings(true) }}
          risk={risk}
          open={sidebarOpen}
          onClose={() => setSidebarOpen(false)}
        />

        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden', minWidth: 0 }}>
          <TopBar
            page={page}
            status={status}
            wsConnected={wsConnected}
            balances={balances}
            risk={risk}
            onHalt={handleHalt}
            onResume={handleResume}
            onMenuToggle={() => setSidebarOpen(o => !o)}
          />

          <main style={{ flex: 1, overflowY: 'auto', overflowX: 'hidden' }}>
            <Suspense fallback={<div style={{ padding: 40, color: 'var(--t2)', fontSize: 13 }}>Loading…</div>}>
              {page === 'dashboard'  && <DashboardPage  {...pageProps} />}
              {page === 'markets'    && <MarketsPage    {...pageProps} />}
              {page === 'positions'  && <PositionsPage  positions={positionsList} tickers={tickers} />}
              {page === 'orders'     && <OrdersPage     orders={ordersList} symbols={symbols} />}
              {page === 'strategies' && <StrategiesPage strategies={strategies} />}
              {page === 'futures'    && <FuturesPage strategies={strategies} positions={positions} tickers={tickers} onUpdate={loadAll} />}
              {page === 'backtest'   && <BacktestPage />}
              {page === 'trades'     && <TradesPage />}
              {page === 'stats'      && <StatsPage />}
              {page === 'risk'       && <RiskPage risk={risk} />}
              {page === 'system'     && <SystemPage wsConnected={wsConnected} />}
              {page === 'dex'        && <DexPage />}
              {page === 'logs'       && <LogsPage />}
            </Suspense>
          </main>
        </div>

        {showSettings && <SettingsModal onClose={() => { setShowSettings(false); loadAll() }} />}
        <ToastContainer toasts={toasts} onRemove={removeToast} />

        {/* Mobile bottom bar — always visible on small screens */}
        <div className="mobile-bottom-bar">
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <span className={`dot ${wsConnected ? 'dot-green blink' : 'dot-muted'}`} />
            <span style={{ fontSize: 11, color: 'var(--t2)' }}>
              {risk?.halted ? '⏹ HALTED' : wsConnected ? 'Live' : 'Offline'}
            </span>
            {balances.filter(b => b.asset === 'USDT').reduce((s, b) => s + b.total, 0) > 0 && (
              <span className="num" style={{ fontSize: 12, fontWeight: 700, color: 'var(--t1)', marginLeft: 4 }}>
                {balances.filter(b => b.asset === 'USDT').reduce((s, b) => s + b.total, 0).toFixed(2)} U
              </span>
            )}
          </div>
          {risk?.halted ? (
            <button className="btn btn-green btn-sm" onClick={handleResume} style={{ fontSize: 12 }}>
              ▶ Resume
            </button>
          ) : (
            <button className="btn btn-red btn-sm" onClick={handleHalt} style={{ fontSize: 12, fontWeight: 700 }}>
              ⏹ Emergency Stop
            </button>
          )}
        </div>
      </div>
  )
}
