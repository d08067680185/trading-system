import { useState, useEffect, useCallback } from 'react'
import { useLang } from '../i18n'
import { Button, PageHeader } from '../components/ui'

function getApiKey() { return localStorage.getItem('trading_api_key') || '' }
function authHeaders() { const k = getApiKey(); return k ? { 'X-API-Key': k } : {} }

async function apiFetch(path, opts = {}) {
  const res = await fetch(`/api${path}`, {
    headers: { 'Content-Type': 'application/json', ...authHeaders(), ...opts.headers },
    ...opts,
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

/* ── Chain card ──────────────────────────────────────────────────────────── */
function ChainCard({ chain, onConnect, t }) {
  const [loading, setLoading] = useState(false)
  const connected = chain.connected

  const handleConnect = async () => {
    setLoading(true)
    // Backend keys chains by NAME (e.g. "arbitrum"), not numeric chain_id.
    try { await onConnect(chain.chain, !connected) }
    finally { setLoading(false) }
  }

  return (
    <div className="card card-glow" style={{ padding: '18px 20px' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 12 }}>
        <div>
          <div style={{ fontWeight: 700, fontSize: 14, color: 'var(--t1)', marginBottom: 4 }}>
            {chain.name || chain.chain_id}
          </div>
          <div style={{ fontSize: 10, color: 'var(--t3)' }}>Chain ID: {chain.chain_id}</div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <span className={`dot ${connected ? 'dot-green blink' : 'dot-red'}`} />
          <span style={{ fontSize: 10, color: connected ? 'var(--green)' : 'var(--t3)' }}>
            {connected ? t('dex_connected') : t('dex_disconnected')}
          </span>
        </div>
      </div>

      {connected && chain.wallet && (
        <div style={{ fontSize: 11, color: 'var(--t2)', marginBottom: 12, fontFamily: 'monospace',
          background: 'var(--bg2)', borderRadius: 6, padding: '6px 10px', wordBreak: 'break-all' }}>
          {chain.wallet.address}
        </div>
      )}

      {connected && chain.wallet && (
        <div style={{ display: 'flex', gap: 16, marginBottom: 12 }}>
          <div>
            <div style={{ fontSize: 9, color: 'var(--t3)', marginBottom: 3 }}>ETH Balance</div>
            <div className="num" style={{ fontSize: 14, fontWeight: 700, color: 'var(--t1)' }}>
              {Number(chain.wallet.eth_balance_ether || 0).toFixed(4)}
            </div>
          </div>
          <div>
            <div style={{ fontSize: 9, color: 'var(--t3)', marginBottom: 3 }}>Gas (Gwei)</div>
            <div className="num" style={{ fontSize: 14, fontWeight: 700, color: 'var(--yellow)' }}>
              {Number(chain.gas_gwei || 0).toFixed(2)}
            </div>
          </div>
        </div>
      )}

      <Button variant={connected ? 'ghost' : 'primary'} className="btn-full"
        onClick={handleConnect} disabled={loading}>
        {loading ? '…' : connected ? t('dex_disconnect_btn') : t('dex_connect_btn')}
      </Button>
    </div>
  )
}

/* ── Quote panel ─────────────────────────────────────────────────────────── */
function QuotePanel({ chains, t }) {
  const [form, setForm] = useState({
    chain: '', token_in: '', token_out: '', amount_usdt: '100', fee_tier: 3000,
  })
  const [quote, setQuote] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [swapping, setSwapping] = useState(false)
  const [swapResult, setSwapResult] = useState(null)

  const connectedChains = chains.filter(c => c.connected)

  const handleQuote = async () => {
    setLoading(true); setError(null); setQuote(null)
    try {
      const q = await apiFetch('/dex/quote', {
        method: 'POST',
        body: JSON.stringify({
          chain: form.chain, token_in: form.token_in, token_out: form.token_out,
          amount_in: Number(form.amount_usdt), fee_tier: Number(form.fee_tier),
        }),
      })
      setQuote(q)
    } catch (e) { setError(e.message) }
    finally { setLoading(false) }
  }

  const handleSwap = async () => {
    if (!quote) return
    setSwapping(true); setSwapResult(null)
    try {
      const r = await apiFetch('/dex/swap', {
        method: 'POST',
        body: JSON.stringify({
          chain: form.chain, token_in: form.token_in, token_out: form.token_out,
          amount_in: Number(form.amount_usdt), fee_tier: Number(form.fee_tier),
          slippage_bps: 50,
        }),
      })
      setSwapResult(r)
    } catch (e) { setError(e.message) }
    finally { setSwapping(false) }
  }

  return (
    <div className="card">
      <div className="card-header">
        <span className="section-title">{t('dex_quote_title')}</span>
      </div>
      <div style={{ padding: '14px 20px', display: 'flex', flexDirection: 'column', gap: 10 }}>
        <div className="grid-3" style={{ gap: 10 }}>
          <div>
            <label style={{ fontSize: 10, color: 'var(--t3)', display: 'block', marginBottom: 4 }}>{t('dex_chain')}</label>
            <select className="select" value={form.chain}
              onChange={e => setForm(p => ({ ...p, chain: e.target.value }))}>
              <option value="">— {t('dex_select_chain')} —</option>
              {connectedChains.map(c => (
                <option key={c.chain} value={c.chain}>{c.name || c.chain}</option>
              ))}
            </select>
          </div>
          <div>
            <label style={{ fontSize: 10, color: 'var(--t3)', display: 'block', marginBottom: 4 }}>{t('dex_fee_tier')}</label>
            <select className="select" value={form.fee_tier}
              onChange={e => setForm(p => ({ ...p, fee_tier: Number(e.target.value) }))}>
              <option value={500}>0.05%</option>
              <option value={3000}>0.30%</option>
              <option value={10000}>1.00%</option>
            </select>
          </div>
          <div>
            <label style={{ fontSize: 10, color: 'var(--t3)', display: 'block', marginBottom: 4 }}>{t('dex_amount_usdt')}</label>
            <input className="input" type="number" value={form.amount_usdt}
              onChange={e => setForm(p => ({ ...p, amount_usdt: e.target.value }))} />
          </div>
        </div>
        <div style={{ display: 'flex', gap: 10 }}>
          <div style={{ flex: 1 }}>
            <label style={{ fontSize: 10, color: 'var(--t3)', display: 'block', marginBottom: 4 }}>{t('dex_token_in')}</label>
            <input className="input" placeholder="0xTokenAddress or 'ETH'" value={form.token_in}
              onChange={e => setForm(p => ({ ...p, token_in: e.target.value }))} />
          </div>
          <div style={{ flex: 1 }}>
            <label style={{ fontSize: 10, color: 'var(--t3)', display: 'block', marginBottom: 4 }}>{t('dex_token_out')}</label>
            <input className="input" placeholder="0xTokenAddress or 'USDC'" value={form.token_out}
              onChange={e => setForm(p => ({ ...p, token_out: e.target.value }))} />
          </div>
        </div>

        <Button variant="primary" className="btn-full" onClick={handleQuote} disabled={loading || !form.chain || !form.token_in || !form.token_out}>
          {loading ? t('dex_quoting') : t('dex_get_quote_btn')}
        </Button>

        {error && <div style={{ fontSize: 12, color: 'var(--red)', padding: '8px 12px', background: 'rgba(255,60,92,0.08)', borderRadius: 6 }}>{error}</div>}

        {quote && (
          <div style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 8, padding: '14px 16px' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 10 }}>
              <span style={{ fontSize: 11, color: 'var(--t3)' }}>{t('dex_effective_price')}</span>
              <span className="num" style={{ fontWeight: 700, color: 'var(--t1)' }}>
                {Number(quote.effective_price || 0).toFixed(6)}
              </span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 10 }}>
              <span style={{ fontSize: 11, color: 'var(--t3)' }}>{t('dex_price_impact')}</span>
              <span className="num" style={{ color: Math.abs(quote.price_impact_pct || 0) > 1 ? 'var(--red)' : 'var(--green)' }}>
                {(quote.price_impact_pct || 0).toFixed(3)}%
              </span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 14 }}>
              <span style={{ fontSize: 11, color: 'var(--t3)' }}>{t('dex_gas_est')}</span>
              <span className="num" style={{ color: 'var(--yellow)' }}>
                ~{Number(quote.gas_estimate || 0).toLocaleString()} gas
              </span>
            </div>
            <Button variant="primary" className="btn-full" onClick={handleSwap} disabled={swapping}>
              {swapping ? t('dex_swapping') : t('dex_execute_swap_btn')}
            </Button>
            {swapResult && (
              <div style={{ marginTop: 10, fontSize: 11, color: 'var(--green)', textAlign: 'center', fontFamily: 'monospace' }}>
                {t('dex_swap_ok')} tx: {swapResult.tx_hash?.slice(0, 20)}…
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

/* ── Token balance lookup ─────────────────────────────────────────────────── */
function TokenBalancePanel({ chains, t }) {
  const [chain, setChain] = useState('')
  const [tokenAddr, setTokenAddr] = useState('')
  const [result, setResult] = useState(null)
  const [loading, setLoading] = useState(false)

  const connectedChains = chains.filter(c => c.connected)

  const handleCheck = async () => {
    if (!chain) return
    setLoading(true); setResult(null)
    try {
      const path = tokenAddr
        ? `/dex/wallet/${chain}/token/${tokenAddr}`
        : `/dex/wallet/${chain}`
      const r = await apiFetch(path)
      setResult(r)
    } catch (e) { setResult({ error: e.message }) }
    finally { setLoading(false) }
  }

  return (
    <div className="card">
      <div className="card-header">
        <span className="section-title">{t('dex_wallet_title')}</span>
      </div>
      <div style={{ padding: '14px 20px', display: 'flex', flexDirection: 'column', gap: 10 }}>
        <div style={{ display: 'flex', gap: 10 }}>
          <select className="select" style={{ flex: '0 0 140px' }} value={chain}
            onChange={e => setChain(e.target.value)}>
            <option value="">— chain —</option>
            {connectedChains.map(c => <option key={c.chain} value={c.chain}>{c.name || c.chain}</option>)}
          </select>
          <input className="input" style={{ flex: 1 }} placeholder={t('dex_token_addr_placeholder')}
            value={tokenAddr} onChange={e => setTokenAddr(e.target.value)} />
          <Button variant="primary" onClick={handleCheck} disabled={loading || !chain}>
            {loading ? '…' : t('dex_check_btn')}
          </Button>
        </div>
        {result && !result.error && (
          <div style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 8, padding: '12px 16px', fontSize: 12 }}>
            {result.address && <div style={{ color: 'var(--t3)', fontFamily: 'monospace', marginBottom: 8, wordBreak: 'break-all' }}>{result.address}</div>}
            {result.eth_balance_ether != null && (
              <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                <span style={{ color: 'var(--t3)' }}>ETH</span>
                <span className="num" style={{ fontWeight: 700 }}>{Number(result.eth_balance_ether).toFixed(6)}</span>
              </div>
            )}
            {result.balance != null && (
              <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 6 }}>
                <span style={{ color: 'var(--t3)' }}>{result.symbol || 'Token'}</span>
                <span className="num" style={{ fontWeight: 700 }}>{Number(result.balance).toFixed(6)}</span>
              </div>
            )}
          </div>
        )}
        {result?.error && (
          <div style={{ fontSize: 12, color: 'var(--red)' }}>{result.error}</div>
        )}
      </div>
    </div>
  )
}

/* ── Main page ───────────────────────────────────────────────────────────── */
export default function DexPage() {
  const { t } = useLang()
  const [chains, setChains] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const loadChains = useCallback(async () => {
    try {
      const data = await apiFetch('/dex/chains')
      setChains(Array.isArray(data) ? data : [])
    } catch (e) { setError(e.message) }
    finally { setLoading(false) }
  }, [])

  useEffect(() => { loadChains() }, [loadChains])

  const handleConnect = async (chainId, shouldConnect) => {
    if (shouldConnect) {
      await apiFetch(`/dex/connect/${chainId}`, { method: 'POST' })
    }
    await loadChains()
  }

  if (loading) return <div className="page"><div style={{ color: 'var(--t3)', padding: 40, textAlign: 'center' }}>{t('loading')}</div></div>

  return (
    <div className="page">
      <PageHeader title={t('dex_title')}>
        <Button variant="ghost" size="sm" onClick={loadChains}>{t('bt_refresh')}</Button>
      </PageHeader>

      {error && <div style={{ color: 'var(--red)', fontSize: 12, padding: '10px 0' }}>{error}</div>}

      {chains.length === 0 ? (
        <div className="card">
          <div className="empty-state">
            <div className="empty-icon">⛓</div>
            <div className="empty-title">{t('dex_no_chains')}</div>
            <div className="empty-sub">{t('dex_no_chains_sub')}</div>
          </div>
        </div>
      ) : (
        <>
          <div className="grid-3">
            {chains.map(chain => (
              <ChainCard key={chain.chain_id} chain={chain} onConnect={handleConnect} t={t} />
            ))}
          </div>

          {chains.some(c => c.connected) && (
            <>
              <QuotePanel chains={chains} t={t} />
              <TokenBalancePanel chains={chains} t={t} />
            </>
          )}
        </>
      )}
    </div>
  )
}
