import { useState } from 'react'
import { useLang } from '../i18n'
import { Button } from './ui'

function HamburgerIcon() {
  return (
    <svg viewBox="0 0 20 20" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
      <line x1="2" y1="5"  x2="18" y2="5"/>
      <line x1="2" y1="10" x2="18" y2="10"/>
      <line x1="2" y1="15" x2="18" y2="15"/>
    </svg>
  )
}

function useTheme() {
  const [theme, setThemeState] = useState(() => localStorage.getItem('theme') || 'dark')
  const toggle = () => {
    const next = theme === 'dark' ? 'light' : 'dark'
    localStorage.setItem('theme', next)
    document.documentElement.setAttribute('data-theme', next)
    setThemeState(next)
  }
  return [theme, toggle]
}

export default function TopBar({ page, status, wsConnected, balances, onHalt, onResume, risk, onMenuToggle }) {
  const { lang, setLang, t } = useLang()
  const [theme, toggleTheme] = useTheme()

  const pageNames = {
    dashboard:  t('nav_dashboard'),
    markets:    t('nav_markets'),
    positions:  t('nav_positions'),
    orders:     t('nav_orders'),
    trades:     t('nav_trades'),
    strategies: t('nav_strategies'),
    backtest:   t('nav_backtest'),
    stats:      t('nav_stats'),
    risk:       t('nav_risk'),
    dex:        t('nav_dex'),
    system:     t('nav_system'),
    logs:       'Logs',
  }

  const totalUsdt = balances.filter(b => b.asset === 'USDT').reduce((s, b) => s + b.total, 0)
  const halted    = risk?.halted
  const exchanges = status?.exchanges || []

  return (
    <header style={{
      height: 'var(--topbar-h)',
      flexShrink: 0,
      background: 'rgba(7,16,30,0.95)',
      backdropFilter: 'blur(16px)',
      borderBottom: '1px solid var(--border)',
      display: 'flex',
      alignItems: 'center',
      padding: '0 20px',
      gap: 14,
      position: 'sticky',
      top: 0,
      zIndex: 50,
    }}>

      {/* Hamburger — mobile only */}
      <button onClick={onMenuToggle}
        className="btn-icon"
        style={{ display: 'none' }}
        id="hamburger-btn">
        <HamburgerIcon />
      </button>

      {/* Page title */}
      <h1 style={{ fontSize: 15, fontWeight: 700, color: 'var(--t1)', flex: 1, minWidth: 0 }} className="text-truncate">
        {pageNames[page] || page}
      </h1>

      {/* Exchange dots — hide on small screens */}
      <div style={{ display: 'flex', gap: 14, alignItems: 'center' }} className="hide-mobile">
        {['binance', 'okx'].map(ex => {
          const on = exchanges.includes(ex)
          return (
            <div key={ex} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <span className={`dot ${on ? 'dot-green' : 'dot-muted'}`} />
              <span style={{ fontSize: 11, fontWeight: 500, color: on ? 'var(--t2)' : 'var(--t4)', textTransform: 'capitalize' }}>
                {ex}
              </span>
            </div>
          )
        })}
      </div>

      {/* Divider */}
      <div style={{ width: 1, height: 18, background: 'var(--border)' }} className="hide-mobile" />

      {/* WS status */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0 }}>
        <span className={`dot ${wsConnected ? 'dot-blue blink' : 'dot-muted'}`} />
        <span style={{ fontSize: 11, color: wsConnected ? 'var(--accent)' : 'var(--t3)' }} className="hide-mobile">
          {wsConnected ? t('ws_live') : t('ws_offline')}
        </span>
      </div>

      {/* Divider */}
      <div style={{ width: 1, height: 18, background: 'var(--border)' }} className="hide-mobile" />

      {/* Total equity — hide on small */}
      {totalUsdt > 0 && (
        <div className="hide-mobile" style={{ flexShrink: 0 }}>
          <span style={{ fontSize: 10, color: 'var(--t3)' }}>{t('total')} </span>
          <span className="num" style={{ fontSize: 13, fontWeight: 600, color: 'var(--t1)' }}>
            {totalUsdt.toLocaleString('en', { minimumFractionDigits: 2 })}
          </span>
          <span style={{ fontSize: 10, color: 'var(--t3)', marginLeft: 3 }}>USDT</span>
        </div>
      )}

      {totalUsdt > 0 && <div style={{ width: 1, height: 18, background: 'var(--border)' }} className="hide-mobile" />}

      {/* Theme toggle */}
      <button
        onClick={toggleTheme}
        title={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
        style={{
          width: 30, height: 30, borderRadius: 6,
          border: '1px solid var(--border)',
          background: 'rgba(128,128,128,0.06)',
          color: 'var(--t2)',
          cursor: 'pointer',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          transition: 'all 0.15s',
          flexShrink: 0,
        }}
        onMouseEnter={e => { e.currentTarget.style.borderColor = 'var(--border2)'; e.currentTarget.style.color = 'var(--t1)' }}
        onMouseLeave={e => { e.currentTarget.style.borderColor = 'var(--border)'; e.currentTarget.style.color = 'var(--t2)' }}
      >
        {theme === 'dark' ? (
          <svg viewBox="0 0 20 20" width="15" height="15" fill="currentColor">
            <path d="M10 2a8 8 0 100 16A8 8 0 0010 2zm0 14a6 6 0 110-12 6 6 0 010 12z" opacity="0"/>
            <circle cx="10" cy="10" r="4"/>
            <path d="M10 1v2M10 17v2M1 10h2M17 10h2M3.05 3.05l1.41 1.41M15.54 15.54l1.41 1.41M3.05 16.95l1.41-1.41M15.54 4.46l1.41-1.41" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" fill="none"/>
          </svg>
        ) : (
          <svg viewBox="0 0 20 20" width="15" height="15" fill="currentColor">
            <path d="M17.5 11.8A7.5 7.5 0 018.2 2.5a7.5 7.5 0 100 15 7.5 7.5 0 009.3-5.7z"/>
          </svg>
        )}
      </button>

      {/* Lang toggle */}
      <button
        onClick={() => setLang(l => l === 'zh' ? 'en' : 'zh')}
        style={{
          padding: '4px 10px', borderRadius: 6,
          border: '1px solid var(--border)',
          background: 'rgba(59,123,255,0.07)',
          color: 'var(--t2)',
          fontSize: 11, fontWeight: 700, cursor: 'pointer',
          letterSpacing: '0.03em',
          transition: 'all 0.15s',
          flexShrink: 0,
        }}
        onMouseEnter={e => { e.currentTarget.style.borderColor = 'var(--border2)'; e.currentTarget.style.color = 'var(--accent)' }}
        onMouseLeave={e => { e.currentTarget.style.borderColor = 'var(--border)'; e.currentTarget.style.color = 'var(--t2)' }}
      >
        {lang === 'zh' ? 'EN' : '中'}
      </button>

      <div style={{ width: 1, height: 18, background: 'var(--border)' }} />

      {/* Halt / Resume — always visible, more prominent */}
      {halted ? (
        <Button variant="green" size="sm" onClick={onResume} style={{ animation: 'pulse 1.5s infinite', fontWeight: 700 }}>
          ▶ {t('btn_resume')}
        </Button>
      ) : (
        <Button variant="red" size="sm" onClick={onHalt} style={{ opacity: 0.85 }}>
          ⏹ {t('btn_halt')}
        </Button>
      )}
    </header>
  )
}
