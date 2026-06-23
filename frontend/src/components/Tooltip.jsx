import { useState } from 'react'

/** Hover tooltip with question mark trigger */
export default function Tooltip({ text, children }) {
  const [show, setShow] = useState(false)
  return (
    <span style={{ position: 'relative', display: 'inline-flex', alignItems: 'center', gap: 4 }}>
      {children}
      <span
        onMouseEnter={() => setShow(true)}
        onMouseLeave={() => setShow(false)}
        style={{
          width: 14, height: 14, borderRadius: '50%', display: 'inline-flex',
          alignItems: 'center', justifyContent: 'center', fontSize: 9, fontWeight: 700,
          background: 'var(--border)', color: 'var(--t3)', cursor: 'help', flexShrink: 0,
        }}>?</span>
      {show && (
        <div style={{
          position: 'absolute', bottom: '100%', left: '50%', transform: 'translateX(-50%)',
          marginBottom: 6, zIndex: 1000, pointerEvents: 'none',
          background: '#1a2740', border: '1px solid var(--border)',
          borderRadius: 8, padding: '7px 10px', fontSize: 11, color: 'var(--t2)',
          width: 220, lineHeight: 1.5, boxShadow: '0 4px 16px rgba(0,0,0,0.4)',
          whiteSpace: 'normal',
        }}>
          {text}
        </div>
      )}
    </span>
  )
}

/** Inline term badge with tooltip */
export function Term({ label, tip, color }) {
  return (
    <Tooltip text={tip}>
      <span style={{ color: color || 'var(--t2)', borderBottom: '1px dashed currentColor', cursor: 'help' }}>
        {label}
      </span>
    </Tooltip>
  )
}

/** Confirmation dialog */
export function ConfirmDialog({ title, message, confirmLabel = 'Confirm', cancelLabel = 'Cancel',
                                dangerous = false, onConfirm, onCancel }) {
  return (
    <div className="modal-backdrop" onClick={e => e.target === e.currentTarget && onCancel()}>
      <div className="modal" style={{ maxWidth: 380 }}>
        <div style={{ fontSize: 16, fontWeight: 700, color: 'var(--t1)', marginBottom: 10 }}>
          {title}
        </div>
        <div style={{ fontSize: 13, color: 'var(--t3)', marginBottom: 24, lineHeight: 1.6 }}>
          {message}
        </div>
        <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end' }}>
          <button className="btn btn-ghost" onClick={onCancel}>{cancelLabel}</button>
          <button
            className={`btn ${dangerous ? 'btn-red' : 'btn-primary'}`}
            onClick={onConfirm}>
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  )
}
