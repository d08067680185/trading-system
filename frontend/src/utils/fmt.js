/** Global formatting utilities */

export function fmtUsdt(v, decimals = 2) {
  if (v == null || isNaN(v)) return '—'
  const n = Number(v)
  if (Math.abs(n) >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`
  if (Math.abs(n) >= 10_000)    return `${(n / 1_000).toFixed(1)}K`
  return n.toFixed(decimals)
}

export function fmtPnl(v, decimals = 4) {
  if (v == null || isNaN(v)) return '—'
  const n = Number(v)
  const sign = n >= 0 ? '+' : ''
  return `${sign}${fmtUsdt(n, decimals)}`
}

export function fmtPct(v, decimals = 2) {
  if (v == null || isNaN(v)) return '—'
  return `${Number(v).toFixed(decimals)}%`
}

export function fmtBps(v) {
  if (v == null || isNaN(v)) return '—'
  return `${Number(v).toFixed(1)} bps`
}

export function fmtMs(v) {
  if (v == null || isNaN(v)) return '—'
  return `${Number(v).toFixed(1)}ms`
}

export function fmtDate(ts) {
  if (!ts) return '—'
  return new Date(ts * 1000).toLocaleDateString()
}

export function fmtDateTime(ts) {
  if (!ts) return '—'
  return new Date(ts * 1000).toLocaleString()
}

export function fmtRelTime(ts) {
  if (!ts) return '—'
  const diff = Date.now() / 1000 - ts
  if (diff < 60)   return `${Math.floor(diff)}s ago`
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
  return `${Math.floor(diff / 86400)}d ago`
}

export function pnlColor(v) {
  if (v == null || isNaN(v)) return 'var(--t3)'
  return Number(v) >= 0 ? 'var(--green)' : 'var(--red)'
}

/** Tooltip component (simple title-based) */
export function withTooltip(element, tip) {
  return { ...element, title: tip }
}

/** Glossary for professional terms */
export const GLOSSARY = {
  bps:    'Basis Points: 1 bps = 0.01%. Used to measure spreads and fees.',
  obi:    'Order Book Imbalance: positive = more buy pressure, negative = more sell pressure.',
  sharpe: 'Sharpe Ratio: risk-adjusted return. >1 = good, >2 = excellent.',
  var:    'Value at Risk: maximum expected loss at given confidence level.',
  cvar:   'Conditional VaR (Expected Shortfall): average loss beyond VaR.',
  dsr:    'Deflated Sharpe: Sharpe ratio corrected for multiple-testing bias.',
  obi_full: 'Order Book Imbalance = (bid_depth - ask_depth) / (bid_depth + ask_depth). Range: -1 to +1.',
  kelly:  "Kelly Criterion: optimal position size based on win rate and payoff ratio.",
}
