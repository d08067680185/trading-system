import { useEffect, useState } from 'react'

/* ── Single toast ──────────────────────────────────────────────────────────── */
function ToastItem({ toast, onRemove }) {
  const [visible, setVisible] = useState(false)

  useEffect(() => {
    requestAnimationFrame(() => setVisible(true))
    // duration: 0 = persistent (no auto-dismiss); undefined = default 4s
    if (toast.duration === 0) return
    const t = setTimeout(() => {
      setVisible(false)
      setTimeout(onRemove, 300)
    }, toast.duration ?? 4000)
    return () => clearTimeout(t)
  }, [])

  const colors = {
    fill_buy:   { bg: 'rgba(0,217,163,0.12)', border: 'rgba(0,217,163,0.3)', icon: '🟢' },
    fill_sell:  { bg: 'rgba(255,60,92,0.10)', border: 'rgba(255,60,92,0.3)',  icon: '🔴' },
    halt:       { bg: 'rgba(255,60,92,0.12)', border: 'rgba(255,60,92,0.4)',  icon: '🚨' },
    warning:    { bg: 'rgba(240,185,11,0.10)', border: 'rgba(240,185,11,0.3)', icon: '⚠️' },
    warn:       { bg: 'rgba(240,185,11,0.10)', border: 'rgba(240,185,11,0.3)', icon: '⚠️' },
    info:       { bg: 'rgba(59,123,255,0.10)', border: 'rgba(59,123,255,0.3)', icon: 'ℹ️' },
    error:      { bg: 'rgba(255,60,92,0.10)', border: 'rgba(255,60,92,0.3)',  icon: '❌' },
  }
  const style = colors[toast.type] || colors.info

  return (
    <div style={{
      padding: '10px 14px',
      borderRadius: 10,
      background: style.bg,
      border: `1px solid ${style.border}`,
      backdropFilter: 'blur(12px)',
      display: 'flex', alignItems: 'flex-start', gap: 10,
      minWidth: 260, maxWidth: 340,
      boxShadow: '0 8px 24px rgba(0,0,0,0.3)',
      transform: visible ? 'translateX(0)' : 'translateX(110%)',
      opacity: visible ? 1 : 0,
      transition: 'transform 0.25s cubic-bezier(0.34,1.56,0.64,1), opacity 0.25s ease',
      cursor: 'pointer',
    }} onClick={onRemove}>
      <span style={{ fontSize: 16, lineHeight: 1.4, flexShrink: 0 }}>{style.icon}</span>
      <div style={{ flex: 1, minWidth: 0 }}>
        {toast.title && (
          <div style={{ fontSize: 12, fontWeight: 700, color: 'var(--t1)', marginBottom: 2 }}>
            {toast.title}
          </div>
        )}
        <div style={{ fontSize: 11, color: 'var(--t2)', lineHeight: 1.5 }}>{toast.message}</div>
      </div>
    </div>
  )
}

/* ── Container ────────────────────────────────────────────────────────────── */
export default function ToastContainer({ toasts, onRemove }) {
  if (!toasts.length) return null
  return (
    <div style={{
      position: 'fixed', bottom: 24, right: 24,
      display: 'flex', flexDirection: 'column', gap: 8,
      zIndex: 9999, pointerEvents: 'none',
    }}>
      {toasts.map(t => (
        <div key={t.id} style={{ pointerEvents: 'auto' }}>
          <ToastItem toast={t} onRemove={() => onRemove(t.id)} />
        </div>
      ))}
    </div>
  )
}
