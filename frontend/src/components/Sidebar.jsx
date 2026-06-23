import { useLang } from '../i18n'

/* ── Icons ─────────────────────────────────────────────────────────────── */
const Icon = ({ d, size = 18, fill = false, stroke = true }) => (
  <svg viewBox="0 0 20 20" width={size} height={size}
    fill={fill ? 'currentColor' : 'none'}
    stroke={stroke ? 'currentColor' : 'none'}
    strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
    <path d={d} />
  </svg>
)

const Icons = {
  dashboard:  () => <svg viewBox="0 0 20 20" width="18" height="18" fill="currentColor"><rect x="1" y="1" width="7" height="7" rx="1.5"/><rect x="12" y="1" width="7" height="7" rx="1.5"/><rect x="1" y="12" width="7" height="7" rx="1.5"/><rect x="12" y="12" width="7" height="7" rx="1.5"/></svg>,
  markets:    () => <svg viewBox="0 0 20 20" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><polyline points="1,14 6,9 10,12 14,6 19,2"/><polyline points="14,2 19,2 19,7"/></svg>,
  positions:  () => <svg viewBox="0 0 20 20" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round"><rect x="2" y="3" width="16" height="4" rx="1"/><rect x="2" y="9" width="16" height="4" rx="1"/><rect x="2" y="15" width="10" height="4" rx="1"/></svg>,
  orders:     () => <svg viewBox="0 0 20 20" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round"><line x1="4" y1="5" x2="16" y2="5"/><line x1="4" y1="10" x2="16" y2="10"/><line x1="4" y1="15" x2="10" y2="15"/><circle cx="16" cy="15" r="3"/><line x1="14.8" y1="15" x2="17.2" y2="15"/><line x1="16" y1="13.8" x2="16" y2="16.2"/></svg>,
  trades:     () => <svg viewBox="0 0 20 20" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round"><rect x="2" y="2" width="16" height="16" rx="2"/><line x1="6" y1="7" x2="14" y2="7"/><line x1="6" y1="10" x2="14" y2="10"/><line x1="6" y1="13" x2="10" y2="13"/></svg>,
  strategies: () => <svg viewBox="0 0 20 20" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round"><polygon points="10,1 13,7 19,8 14.5,13 15.8,19 10,16 4.2,19 5.5,13 1,8 7,7"/></svg>,
  backtest:   () => <svg viewBox="0 0 20 20" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round"><polyline points="2,15 6,9 9,12 13,5 18,8"/><rect x="1" y="16" width="18" height="2" rx="1" fill="currentColor" stroke="none"/><circle cx="18" cy="8" r="2" fill="currentColor" stroke="none"/></svg>,
  stats:      () => <svg viewBox="0 0 20 20" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><rect x="2" y="10" width="3" height="8" rx="1"/><rect x="8.5" y="5" width="3" height="13" rx="1"/><rect x="15" y="2" width="3" height="16" rx="1"/></svg>,
  system:     () => <svg viewBox="0 0 20 20" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round"><rect x="2" y="3" width="16" height="11" rx="2"/><line x1="6" y1="17" x2="14" y2="17"/><line x1="10" y1="14" x2="10" y2="17"/><circle cx="10" cy="8.5" r="2.5"/></svg>,
  dex:        () => <svg viewBox="0 0 20 20" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><circle cx="10" cy="10" r="8"/><path d="M7 10h6M10 7l3 3-3 3"/></svg>,
  risk:       () => <svg viewBox="0 0 20 20" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M10 2L2 17h16L10 2z"/><line x1="10" y1="9" x2="10" y2="13"/><circle cx="10" cy="15.5" r="0.8" fill="currentColor" stroke="none"/></svg>,
  settings:   () => <svg viewBox="0 0 20 20" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round"><circle cx="10" cy="10" r="3"/><path d="M10 1v2M10 17v2M1 10h2M17 10h2M3.5 3.5l1.4 1.4M15.1 15.1l1.4 1.4M3.5 16.5l1.4-1.4M15.1 4.9l1.4-1.4"/></svg>,
  logs:       () => <svg viewBox="0 0 20 20" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round"><rect x="3" y="2" width="14" height="16" rx="2"/><line x1="7" y1="7" x2="13" y2="7"/><line x1="7" y1="10" x2="13" y2="10"/><line x1="7" y1="13" x2="10" y2="13"/></svg>,
}

export default function Sidebar({ page, onNavigate, onSettings, risk, open, onClose }) {
  const { t } = useLang()
  const halted = risk?.halted

  const NAV = [
    { key: 'dashboard',  label: t('nav_dashboard'),  Icon: Icons.dashboard },
    { key: 'markets',    label: t('nav_markets'),    Icon: Icons.markets },
    { key: 'positions',  label: t('nav_positions'),  Icon: Icons.positions, badge: t('live_badge') },
    { key: 'orders',     label: t('nav_orders'),     Icon: Icons.orders },
    { key: 'trades',     label: t('nav_trades'),     Icon: Icons.trades },
    { key: 'strategies', label: t('nav_strategies'), Icon: Icons.strategies },
    { key: 'backtest',   label: t('nav_backtest'),   Icon: Icons.backtest },
    { key: 'stats',      label: t('nav_stats'),      Icon: Icons.stats },
    { key: 'risk',       label: t('nav_risk'),       Icon: Icons.risk },
    { key: 'dex',        label: t('nav_dex'),        Icon: Icons.dex },
    { key: 'system',     label: t('nav_system'),     Icon: Icons.system },
    { key: 'logs',       label: 'Logs',              Icon: Icons.logs },
  ]

  return (
    <aside className={open ? 'sidebar-open' : ''} style={{
      width: 'var(--sidebar-w)',
      flexShrink: 0,
      background: 'linear-gradient(180deg, #07101e 0%, #050d1a 100%)',
      borderRight: '1px solid var(--border)',
      display: 'flex',
      flexDirection: 'column',
      height: '100%',
      position: 'sticky',
      top: 0,
    }}>

      {/* Logo */}
      <div style={{ padding: '20px 18px 16px', borderBottom: '1px solid var(--border)', flexShrink: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 11, justifyContent: 'space-between' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 11 }}>
            <div style={{
              width: 36, height: 36, borderRadius: 10,
              background: 'linear-gradient(135deg, #3b7bff 0%, #7c3aed 100%)',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              fontSize: 17, flexShrink: 0,
              boxShadow: '0 4px 16px rgba(59,123,255,0.4)',
            }}>⚡</div>
            <div>
              <div style={{ fontWeight: 700, fontSize: 14, color: 'var(--t1)', letterSpacing: '-0.02em' }}>
                AutoTrader
              </div>
              <div style={{ fontSize: 10, color: 'var(--t3)', marginTop: 1 }}>v1.0 · Pro</div>
            </div>
          </div>
          {/* Mobile close button */}
          <button onClick={onClose}
            style={{ background: 'none', border: 'none', color: 'var(--t2)', cursor: 'pointer', padding: 4 }}
            className="btn-icon mobile-only">
            <svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2"><line x1="2" y1="2" x2="14" y2="14"/><line x1="14" y1="2" x2="2" y2="14"/></svg>
          </button>
        </div>
      </div>

      {/* Nav */}
      <nav style={{ flex: 1, padding: '10px 10px', overflowY: 'auto', overflowX: 'hidden' }}>
        <div style={{
          fontSize: 9, fontWeight: 700, letterSpacing: '0.12em', color: 'var(--t3)',
          textTransform: 'uppercase', padding: '8px 10px 8px',
        }}>{t('nav_label')}</div>

        {NAV.map(item => {
          const active = page === item.key
          return (
            <button key={item.key} onClick={() => onNavigate(item.key)}
              style={{
                width: '100%', display: 'flex', alignItems: 'center', gap: 11,
                padding: '9px 12px', borderRadius: 10, marginBottom: 2,
                background: active
                  ? 'linear-gradient(90deg,rgba(59,123,255,0.16) 0%,rgba(59,123,255,0.04) 100%)'
                  : 'transparent',
                border: `1px solid ${active ? 'rgba(59,123,255,0.2)' : 'transparent'}`,
                cursor: 'pointer',
                color: active ? 'var(--t1)' : 'var(--t2)',
                fontSize: 13, fontWeight: active ? 600 : 400,
                transition: 'all 0.15s',
                position: 'relative', textAlign: 'left',
              }}
              onMouseEnter={e => { if (!active) {
                const light = document.documentElement.getAttribute('data-theme') === 'light'
                e.currentTarget.style.background = light ? 'rgba(0,0,0,0.06)' : 'rgba(255,255,255,0.04)'
                e.currentTarget.style.color = 'var(--t1)'
              }}}
              onMouseLeave={e => { if (!active) { e.currentTarget.style.background = 'transparent'; e.currentTarget.style.color = 'var(--t2)' } }}
            >
              {active && (
                <span style={{
                  position: 'absolute', left: 0, top: '20%', bottom: '20%',
                  width: 3, borderRadius: 2,
                  background: 'linear-gradient(180deg,#3b7bff,#7c3aed)',
                }} />
              )}
              <span style={{ color: active ? 'var(--accent)' : 'inherit', opacity: active ? 1 : 0.75, flexShrink: 0 }}>
                <item.Icon />
              </span>
              <span style={{ flex: 1 }}>{item.label}</span>
              {item.badge && (
                <span style={{
                  fontSize: 9, fontWeight: 700, padding: '2px 6px', borderRadius: 10,
                  background: 'rgba(59,123,255,0.15)', color: 'var(--accent)',
                  border: '1px solid rgba(59,123,255,0.2)',
                }}>{item.badge}</span>
              )}
            </button>
          )
        })}
      </nav>

      {/* Bottom: risk + settings */}
      <div style={{ padding: '10px 10px', borderTop: '1px solid var(--border)', flexShrink: 0 }}>
        {risk && (
          <div style={{
            padding: '10px 14px', borderRadius: 10, marginBottom: 8,
            background: halted ? 'rgba(255,60,92,0.07)' : 'rgba(0,217,163,0.05)',
            border: `1px solid ${halted ? 'rgba(255,60,92,0.18)' : 'rgba(0,217,163,0.1)'}`,
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 6 }}>
              <span className={`dot ${halted ? 'dot-red' : 'dot-green'}`} />
              <span style={{ fontSize: 10, fontWeight: 700, color: halted ? 'var(--red)' : 'var(--green)' }}>
                {halted ? t('risk_halted') : t('risk_active')}
              </span>
            </div>
            <div style={{ fontSize: 11, color: 'var(--t2)' }}>
              {t('daily_pnl')}:&nbsp;
              <span className="num" style={{
                fontWeight: 700,
                color: (risk.daily_pnl_usdt || 0) >= 0 ? 'var(--green)' : 'var(--red)',
              }}>
                {(risk.daily_pnl_usdt || 0) >= 0 ? '+' : ''}{(risk.daily_pnl_usdt || 0).toFixed(2)}
              </span>
            </div>
          </div>
        )}

        <button onClick={onSettings} style={{
          width: '100%', display: 'flex', alignItems: 'center', gap: 11,
          padding: '9px 12px', borderRadius: 10,
          background: 'transparent', border: '1px solid transparent', cursor: 'pointer',
          color: 'var(--t3)', fontSize: 13, fontWeight: 400,
          transition: 'all 0.15s', textAlign: 'left',
        }}
          onMouseEnter={e => {
            const light = document.documentElement.getAttribute('data-theme') === 'light'
            e.currentTarget.style.background = light ? 'rgba(0,0,0,0.05)' : 'rgba(255,255,255,0.03)'
            e.currentTarget.style.color = 'var(--t2)'
          }}
          onMouseLeave={e => { e.currentTarget.style.background = 'transparent'; e.currentTarget.style.color = 'var(--t3)' }}
        >
          <span style={{ opacity: 0.5 }}><Icons.settings /></span>
          {t('nav_settings')}
        </button>
      </div>
    </aside>
  )
}
