import { useEffect, useRef, useState, useCallback } from 'react'
import { createChart, CandlestickSeries, HistogramSeries } from 'lightweight-charts'

function getApiKey() { return localStorage.getItem('trading_api_key') || '' }

const INTERVALS = [
  { label: '1m',  value: '1m',  secs: 60 },
  { label: '5m',  value: '5m',  secs: 300 },
  { label: '15m', value: '15m', secs: 900 },
  { label: '1h',  value: '1h',  secs: 3600 },
  { label: '4h',  value: '4h',  secs: 14400 },
  { label: '1d',  value: '1d',  secs: 86400 },
]

const CHART_DARK = {
  layout: { background: { color: '#080e1c' }, textColor: '#5a7fa8' },
  grid: { vertLines: { color: '#0d1b2e' }, horzLines: { color: '#0d1b2e' } },
  crosshair: { vertLine: { color: '#3b7bff44' }, horzLine: { color: '#3b7bff44' } },
  timeScale: { borderColor: '#192840', timeVisible: true, secondsVisible: false },
  rightPriceScale: { borderColor: '#192840' },
}
const CHART_LIGHT = {
  layout: { background: { color: '#f7f9fc' }, textColor: '#374e6a' },
  grid: { vertLines: { color: '#d0dbe8' }, horzLines: { color: '#d0dbe8' } },
  crosshair: { vertLine: { color: '#2563eb44' }, horzLine: { color: '#2563eb44' } },
  timeScale: { borderColor: '#d0dbe8', timeVisible: true, secondsVisible: false },
  rightPriceScale: { borderColor: '#d0dbe8' },
}
function getChartTheme() {
  return document.documentElement.getAttribute('data-theme') === 'light' ? CHART_LIGHT : CHART_DARK
}

function candleTime(ts, secs) {
  return Math.floor(ts / secs) * secs
}

export default function KlineChart({ exchange, symbol, ticker, height = 420 }) {
  const containerRef = useRef(null)
  const chartRef     = useRef(null)
  const candleRef    = useRef(null)
  const volumeRef    = useRef(null)
  const liveRef      = useRef(null)   // current in-progress candle

  const [interval, setInterval_] = useState('1h')
  const [loading, setLoading]    = useState(false)
  const [empty, setEmpty]        = useState(false)
  const [downloading, setDownloading] = useState(false)
  const [dlMsg, setDlMsg]        = useState(null)

  const intervalSecs = INTERVALS.find(i => i.value === interval)?.secs || 3600

  // ── Init chart once ────────────────────────────────────────────────────────
  useEffect(() => {
    if (!containerRef.current) return
    const chart = createChart(containerRef.current, {
      ...getChartTheme(),
      width: containerRef.current.clientWidth,
      height: height - 48,  // leave room for toolbar
      handleScroll: true,
      handleScale: true,
    })

    const candle = chart.addSeries(CandlestickSeries, {
      upColor:        '#00d9a3',
      downColor:      '#ff3c5c',
      borderUpColor:  '#00d9a3',
      borderDownColor:'#ff3c5c',
      wickUpColor:    '#00d9a3',
      wickDownColor:  '#ff3c5c',
    })

    const volume = chart.addSeries(HistogramSeries, {
      color:     '#3b7bff44',
      priceFormat: { type: 'volume' },
      priceScaleId: 'volume',
    })
    chart.priceScale('volume').applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } })

    chartRef.current  = chart
    candleRef.current = candle
    volumeRef.current = volume

    const ro = new ResizeObserver(() => {
      chart.applyOptions({ width: containerRef.current?.clientWidth || 600 })
    })
    ro.observe(containerRef.current)

    const themeObs = new MutationObserver(() => {
      chart.applyOptions(getChartTheme())
    })
    themeObs.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })

    return () => {
      themeObs.disconnect()
      ro.disconnect()
      chart.remove()
      chartRef.current = candleRef.current = volumeRef.current = null
    }
  }, [])  // only once — height doesn't need to trigger recreate

  // ── Fetch historical OHLCV ─────────────────────────────────────────────────
  const fetchData = useCallback(async () => {
    if (!candleRef.current) return
    setLoading(true)
    setEmpty(false)
    liveRef.current = null
    try {
      const sym = encodeURIComponent(symbol)
      const url = `/api/data/ohlcv/${exchange}/${sym}?interval=${interval}&limit=500`
      const key = getApiKey()
      const res = await fetch(url, { headers: key ? { 'X-API-Key': key } : {} })
      const rows = await res.json()

      if (!rows.length) { setEmpty(true); return }

      const cData = rows.map(r => ({ time: r.ts, open: r.open, high: r.high, low: r.low, close: r.close }))
      const vData = rows.map(r => ({ time: r.ts, value: r.volume, color: r.close >= r.open ? '#00d9a344' : '#ff3c5c44' }))

      candleRef.current.setData(cData)
      volumeRef.current.setData(vData)
      chartRef.current.timeScale().fitContent()

      // Seed live candle from last bar
      const last = rows[rows.length - 1]
      liveRef.current = { time: last.ts, open: last.open, high: last.high, low: last.low, close: last.close, volume: last.volume }
    } catch (e) {
      setEmpty(true)
    } finally {
      setLoading(false)
    }
  }, [exchange, symbol, interval])

  useEffect(() => { fetchData() }, [fetchData])

  // ── Real-time candle update from ticker ────────────────────────────────────
  useEffect(() => {
    if (!ticker?.last || !candleRef.current) return
    const price  = Number(ticker.last)
    const now    = Math.floor(Date.now() / 1000)
    const cTime  = candleTime(now, intervalSecs)

    if (!liveRef.current || liveRef.current.time !== cTime) {
      // New candle
      liveRef.current = { time: cTime, open: price, high: price, low: price, close: price, volume: 0 }
    } else {
      const c = liveRef.current
      liveRef.current = {
        ...c,
        high:  Math.max(c.high, price),
        low:   Math.min(c.low, price),
        close: price,
      }
    }

    try {
      candleRef.current.update(liveRef.current)
      volumeRef.current.update({
        time:  liveRef.current.time,
        value: liveRef.current.volume || 0,
        color: liveRef.current.close >= liveRef.current.open ? '#00d9a344' : '#ff3c5c44',
      })
    } catch (_) {}
  }, [ticker?.last, intervalSecs])

  // ── Download historical data ───────────────────────────────────────────────
  const handleDownload = async () => {
    setDownloading(true)
    setDlMsg(null)
    try {
      const key = getApiKey()
      const res = await fetch('/api/data/fetch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...(key ? { 'X-API-Key': key } : {}) },
        body: JSON.stringify({ exchange, symbol, interval, days: 30 }),
      })
      const d = await res.json()
      setDlMsg(`Downloaded ${d.stored ?? '?'} bars`)
      await fetchData()
    } catch (e) {
      setDlMsg('Download failed')
    } finally {
      setDownloading(false)
    }
  }

  return (
    <div style={{ background: 'var(--card)', borderTop: '1px solid var(--border)' }}>
      {/* Toolbar */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '8px 16px', borderBottom: '1px solid var(--border)' }}>
        <span style={{ fontSize: 11, fontWeight: 700, color: 'var(--t3)', textTransform: 'uppercase', marginRight: 4 }}>
          {exchange.toUpperCase()}
        </span>
        {INTERVALS.map(iv => (
          <button key={iv.value} onClick={() => setInterval_(iv.value)}
            style={{
              padding: '3px 10px', borderRadius: 5, fontSize: 11, fontWeight: 600,
              cursor: 'pointer', border: 'none',
              background: interval === iv.value ? 'var(--accent-dim)' : 'transparent',
              color: interval === iv.value ? 'var(--accent)' : 'var(--t3)',
              transition: 'all 0.12s',
            }}>
            {iv.label}
          </button>
        ))}
        {dlMsg && <span style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--green)' }}>{dlMsg}</span>}
      </div>

      {/* Chart area */}
      <div style={{ position: 'relative', height: height - 48 }}>
        <div ref={containerRef} style={{ width: '100%', height: '100%' }} />

        {loading && (
          <div style={{
            position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center',
            background: 'var(--card)',
            opacity: 0.8,
          }}>
            <span style={{ fontSize: 13, color: 'var(--t3)' }}>Loading…</span>
          </div>
        )}

        {!loading && empty && (
          <div style={{
            position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column',
            alignItems: 'center', justifyContent: 'center', gap: 12,
          }}>
            <div style={{ fontSize: 28, opacity: 0.3 }}>📊</div>
            <div style={{ fontSize: 13, color: 'var(--t3)' }}>No historical data for {symbol} {interval}</div>
            <button onClick={handleDownload} disabled={downloading}
              style={{
                padding: '7px 18px', borderRadius: 8, border: '1px solid var(--accent-glow)',
                background: 'var(--accent-dim)', color: 'var(--accent)',
                fontSize: 12, fontWeight: 600, cursor: 'pointer', opacity: downloading ? 0.5 : 1,
              }}>
              {downloading ? 'Downloading…' : '↓ Download 30 days'}
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
