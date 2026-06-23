/*
 * Shared UI primitives — the single source of truth for buttons, badges, stat
 * tiles, page headers, etc. They are thin wrappers over the class system in
 * index.css (.btn / .badge / .metric / .empty-state …) so all theming stays in
 * CSS variables. Prefer these over hand-rolled inline styles in pages: it keeps
 * the look consistent and means a token change updates everything at once.
 */
import { useLang } from '../i18n'

const cx = (...xs) => xs.filter(Boolean).join(' ')

/* Button — variant: primary | ghost | green | red | yellow | purple | buy | sell
 *          size:    sm | xs (omit for default) */
export function Button({ variant = 'ghost', size, className, children, ...props }) {
  return (
    <button className={cx('btn', `btn-${variant}`, size && `btn-${size}`, className)} {...props}>
      {children}
    </button>
  )
}

/* Badge — variant maps to .badge-* (buy/sell/long/short/open/filled/cancelled/
 * rejected/partially_filled). Pass a status string straight through. */
export function Badge({ variant, className, children, ...props }) {
  return (
    <span className={cx('badge', variant && `badge-${variant}`, className)} {...props}>
      {children}
    </span>
  )
}

/* StatTile — a labelled metric. `accent` adds the colored top bar (blue/green/
 * red/yellow); `color` recolors the value (e.g. var(--green)). */
export function StatTile({ label, value, sub, color, accent, className, style }) {
  return (
    <div className={cx('card', accent && `accent-${accent}`, className)}
         style={{ padding: '14px 16px 18px', ...style }}>
      <div className="label" style={{ marginBottom: 6 }}>{label}</div>
      <div className="metric" style={color ? { color } : undefined}>{value}</div>
      {sub != null && <div style={{ fontSize: 11, color: 'var(--t3)', marginTop: 4 }}>{sub}</div>}
    </div>
  )
}

/* PageHeader — title on the left, actions/status on the right. */
export function PageHeader({ title, children }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                  flexWrap: 'wrap', gap: 10 }}>
      <h2 className="page-title" style={{ margin: 0 }}>{title}</h2>
      {children != null && (
        <div className="page-header-actions"
          style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          {children}
        </div>
      )}
    </div>
  )
}

/* Card + CardHeader — the standard surface. */
export function Card({ className, children, style, ...props }) {
  return <div className={cx('card', className)} style={style} {...props}>{children}</div>
}

export function CardHeader({ title, sub, children }) {
  return (
    <div className="card-header">
      <span className="section-title">{title}</span>
      {sub != null && <span style={{ fontSize: 11, color: 'var(--t3)' }}>{sub}</span>}
      {children}
    </div>
  )
}

/* EmptyState — icon + title + sub, centered. */
export function EmptyState({ icon = '∅', title, sub }) {
  return (
    <div className="card">
      <div className="empty-state">
        <div className="empty-icon">{icon}</div>
        {title && <div className="empty-title">{title}</div>}
        {sub && <div className="empty-sub">{sub}</div>}
      </div>
    </div>
  )
}

/* Spinner — inline loading text (uses the i18n 'loading' key by default). */
export function Loading({ label }) {
  const { t } = useLang()
  return <span style={{ fontSize: 12, color: 'var(--t3)' }}>{label || t('loading') || 'Loading…'}</span>
}

/* Alert — inline status box (variant: error | success | warning | info) */
export function Alert({ variant = 'error', children, className, style }) {
  const cx2 = (...xs) => xs.filter(Boolean).join(' ')
  return (
    <div className={cx2('alert', `alert-${variant}`, className)} style={style}>
      {children}
    </div>
  )
}

/* ExchangeBadge — BN / OKX exchange chip */
export function ExchangeBadge({ exchange }) {
  const isBn = !exchange || exchange.startsWith('binance')
  return (
    <span style={{
      padding: '2px 7px', borderRadius: 4, fontSize: 10, fontWeight: 700,
      background: isBn ? 'rgba(240,185,11,0.12)' : 'rgba(0,100,220,0.12)',
      color: isBn ? '#f0b90b' : '#4488ee',
    }}>
      {isBn ? 'BN' : 'OKX'}
    </span>
  )
}

/* StratBadge — strategy ID chip */
export function StratBadge({ children }) {
  return (
    <span style={{
      fontSize: 10, fontWeight: 700, padding: '2px 7px', borderRadius: 4,
      background: 'rgba(59,123,255,0.1)', color: 'var(--accent)',
    }}>
      {children}
    </span>
  )
}
