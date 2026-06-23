import { useState, useEffect, useCallback, useRef } from 'react'
import { useLang } from '../i18n'
import { ConfirmDialog } from '../components/Tooltip'
import { Button, PageHeader } from '../components/ui'

function getApiKey() { return localStorage.getItem('trading_api_key') || '' }

const STRAT_DESC = {
  en: {
    arb_spread:     'Monitors bid/ask spread between Binance and OKX. Opens trades when spread exceeds threshold.',
    funding_arb:    'Exploits funding rate differentials. Goes long on negative funding, short on positive.',
    cash_carry:     'Delta-neutral: buys spot + shorts equal futures. Earns funding payments risk-free.',
    spot_grid_btc:  'Grid trading on BTC spot. Places buy/sell limit orders at each price level. Set grid_low & grid_high to activate.',
    market_maker:   'Places limit orders on both sides. Earns spread by providing liquidity.',
    trading_comp:   'Repeatedly buys then sells a fixed USDT amount. Generates volume for trading competitions.',
    trend_follow:   'Follows price momentum using moving averages. Enters when trend is confirmed.',
  },
  zh: {
    arb_spread:     '监控币安与OKX之间的买卖价差，当价差超过阈值时开仓套利。',
    funding_arb:    '利用资金费率差异，做多负资金费率合约，做空正资金费率合约。',
    cash_carry:     'Delta中性套利：买入现货+做空等额合约，无方向风险地收取资金费率。',
    spot_grid_btc:  'BTC现货网格交易，在价格区间内自动高卖低买。设置grid_low和grid_high后激活。',
    market_maker:   '在盘口两侧挂限价单，通过提供流动性赚取价差收益。',
    trading_comp:   '反复买入再卖出固定USDT金额，为交易竞赛刷取成交量。',
    trend_follow:   '使用均线系统跟踪价格趋势，趋势确认后入场。',
  },
}

const STRAT_NAMES = {
  arb_spread:    'Spread Arbitrage / 价差套利',
  funding_arb:   'Funding Rate Arb / 资金费率套利',
  cash_carry:    'Cash & Carry / 现货合约中性',
  spot_grid_btc: 'Spot Grid BTC / BTC现货网格',
  market_maker:  'Market Maker / 市场做市',
  trading_comp:  'Trading Competition / 交易竞赛刷量',
}

const DEFAULT_STRATS = [
  {
    id: 'arb_spread', name: STRAT_NAMES.arb_spread, enabled: false,
    params: { min_profit_bps: 5, fee_bps: 4, order_size_usdt: 25, cooldown_s: 30, max_position_usdt: 50, leg_timeout_s: 8, max_mismatches: 3, use_maker_leg: true, maker_timeout_s: 3 },
    stats: null,
  },
  {
    id: 'funding_arb', name: STRAT_NAMES.funding_arb, enabled: false,
    params: { symbols: ['BTC-USDT', 'ETH-USDT'], min_rate_diff: 50, position_usdt: 25, check_interval_s: 300, exit_rate_diff: 10, max_hold_hours: 24, min_hold_hours: 1 },
    stats: null,
  },
  {
    id: 'cash_carry', name: STRAT_NAMES.cash_carry, enabled: false,
    params: { symbols: ['BTC-USDT', 'ETH-USDT'], spot_exchange: 'binance_spot', futures_exchange: 'binance', min_rate_8h: 0.0003, exit_rate_8h: 0.0001, position_usdt: 25, check_interval_s: 300, max_hold_hours: 72, min_hold_hours: 8 },
    stats: null,
  },
  {
    id: 'spot_grid_btc', name: STRAT_NAMES.spot_grid_btc, enabled: false,
    params: { exchange: 'binance_spot', symbol: 'BTC-USDT', grid_low: 0, grid_high: 0, grid_levels: 10, order_usdt: 10, qty_precision: 5, price_precision: 0 },
    stats: null,
  },
  {
    id: 'market_maker', name: STRAT_NAMES.market_maker, enabled: false,
    params: { exchange: 'binance_spot', symbol: 'BTC-USDT', spread_bps: 10, order_usdt: 50, max_inventory_usdt: 200, inventory_skew_bps: 10, requote_interval_s: 5, vol_window: 30, vol_spread_mult: 2, min_spread_bps: 5, max_spread_bps: 50, qty_precision: 5, price_precision: 0 },
    stats: null,
  },
  {
    id: 'trading_comp', name: STRAT_NAMES.trading_comp, enabled: false,
    params: { exchange: 'binance_spot', symbol: 'BTC-USDT', order_usdt: 50, cycle_interval_s: 60, max_cycles: 0, qty_precision: 5 },
    stats: null,
  },
]

const EXCHANGE_ALL = ['binance', 'binance_spot', 'okx', 'okx_spot']

const PARAM_META = {
  min_profit_bps:    { type: 'number',  en: { label: 'Min Profit (bps)',       hint: 'Arb trigger threshold' },       zh: { label: '最小利润 (bps)',    hint: '套利触发阈值' } },
  fee_bps:           { type: 'number',  en: { label: 'Fee (bps)',              hint: '' },                            zh: { label: '手续费 (bps)',      hint: '' } },
  order_size_usdt:   { type: 'number',  en: { label: 'Order Size (USDT)',      hint: '' },                            zh: { label: '单笔金额 (USDT)',   hint: '' } },
  cooldown_s:        { type: 'number',  en: { label: 'Cooldown (sec)',         hint: '' },                            zh: { label: '冷却时间 (秒)',      hint: '' } },
  max_position_usdt: { type: 'number',  en: { label: 'Max Position (USDT)',    hint: '' },                            zh: { label: '最大持仓 (USDT)',   hint: '' } },
  arb_timeout_s:     { type: 'number',  en: { label: 'Arb Timeout (sec)',      hint: '' },                            zh: { label: '套利超时 (秒)',      hint: '' } },
  min_rate_diff:     { type: 'number',  en: { label: 'Min Rate Diff (bps)',    hint: 'Funding rate arb trigger' },    zh: { label: '最小费率差 (bps)',   hint: '资金费率套利触发阈值' } },
  position_usdt:     { type: 'number',  en: { label: 'Position (USDT)',        hint: '' },                            zh: { label: '持仓金额 (USDT)',   hint: '' } },
  check_interval_s:  { type: 'number',  en: { label: 'Check Interval (sec)',   hint: '' },                            zh: { label: '检查间隔 (秒)',      hint: '' } },
  exit_rate_diff:    { type: 'number',  en: { label: 'Exit Rate Diff (bps)',   hint: '' },                            zh: { label: '平仓费率差 (bps)',   hint: '' } },
  max_hold_hours:    { type: 'number',  en: { label: 'Max Hold (hours)',       hint: '' },                            zh: { label: '最长持仓 (小时)',    hint: '' } },
  min_hold_hours:    { type: 'number',  en: { label: 'Min Hold (hours)',       hint: '' },                            zh: { label: '最短持仓 (小时)',    hint: '' } },
  take_profit_bps:   { type: 'number',  en: { label: 'Take Profit (bps)',      hint: '0 = disabled' },                zh: { label: '止盈 (bps)',        hint: '0 = 不止盈' } },
  min_rate_8h:       { type: 'number',  en: { label: 'Entry 8h Rate',          hint: 'Min funding rate to enter' },   zh: { label: '开仓8h费率',        hint: '触发现货合约套利的最低资金费率' } },
  exit_rate_8h:      { type: 'number',  en: { label: 'Exit 8h Rate',           hint: 'Exit when below this rate' },   zh: { label: '平仓8h费率',        hint: '低于此值时平仓' } },
  grid_low:          { type: 'number',  en: { label: 'Grid Low',               hint: '0 = inactive' },                zh: { label: '网格下限',           hint: '0 = 未激活' } },
  grid_high:         { type: 'number',  en: { label: 'Grid High',              hint: '0 = inactive' },                zh: { label: '网格上限',           hint: '0 = 未激活' } },
  order_usdt:        { type: 'number',  en: { label: 'Per-grid Amount (USDT)', hint: '' },                            zh: { label: '每格金额 (USDT)',   hint: '' } },
  grid_levels:       { type: 'integer', en: { label: 'Grid Levels',            hint: 'Number of price levels' },      zh: { label: '网格数量',           hint: '价格区间分割层数' } },
  qty_precision:     { type: 'integer', en: { label: 'Qty Precision',          hint: 'Decimal places' },              zh: { label: '数量精度',           hint: '小数位数' } },
  price_precision:   { type: 'integer', en: { label: 'Price Precision',        hint: 'Decimal places' },              zh: { label: '价格精度',           hint: '小数位数' } },
  symbols:           { type: 'array',   en: { label: 'Symbol List',            hint: 'Comma-separated, e.g. BTC-USDT,ETH-USDT' }, zh: { label: '交易对列表', hint: '逗号分隔，如 BTC-USDT,ETH-USDT' } },
  spot_exchange:     { type: 'select',  en: { label: 'Spot Exchange',          hint: '' },                            zh: { label: '现货交易所',          hint: '' }, options: ['binance_spot', 'okx_spot'] },
  futures_exchange:  { type: 'select',  en: { label: 'Futures Exchange',       hint: '' },                            zh: { label: '合约交易所',          hint: '' }, options: ['binance', 'okx'] },
  exchange:          { type: 'select',  en: { label: 'Exchange',               hint: '' },                            zh: { label: '交易所',              hint: '' }, options: EXCHANGE_ALL },
  symbol:            { type: 'text',    en: { label: 'Symbol',                 hint: 'e.g. BTC-USDT' },               zh: { label: '交易对',              hint: '如 BTC-USDT' } },
  spread_bps:        { type: 'number',  en: { label: 'Target Spread (bps)',    hint: 'Base spread, auto-widens on vol' }, zh: { label: '目标价差 (bps)', hint: '基础买卖价差，波动时自动放宽' } },
  max_inventory_usdt:{ type: 'number',  en: { label: 'Max Inventory (USDT)',   hint: 'Pause adding inventory when exceeded (MM: one-sided quotes; Grid: buys)' }, zh: { label: '最大库存 (USDT)', hint: '超过此值时停止增加库存（做市：暂停单边报价；网格：暂停买入）' } },
  inventory_skew_bps:{ type: 'number',  en: { label: 'Inventory Skew (bps)',   hint: 'Max mid shift at full inventory' }, zh: { label: '库存偏移 (bps)', hint: '满仓时中间价最大偏移量' } },
  requote_interval_s:{ type: 'number',  en: { label: 'Requote Interval (sec)', hint: 'Cancel and re-quote frequency' }, zh: { label: '刷新间隔 (秒)', hint: '撤单重报频率' } },
  vol_window:        { type: 'integer', en: { label: 'Vol Window (ticks)',      hint: 'Tick history for vol calc' },    zh: { label: '波动窗口 (ticks)',    hint: '计算波动率的历史tick数' } },
  vol_spread_mult:   { type: 'number',  en: { label: 'Vol Spread Mult',        hint: 'Extra spread per 1bps vol' },    zh: { label: '波动价差倍数',         hint: '每1bps波动增加的价差倍数' } },
  min_spread_bps:    { type: 'number',  en: { label: 'Min Spread (bps)',       hint: 'Spread floor' },                 zh: { label: '最小价差 (bps)',      hint: '价差下限' } },
  max_spread_bps:    { type: 'number',  en: { label: 'Max Spread (bps)',       hint: 'Spread ceiling on high vol' },   zh: { label: '最大价差 (bps)',      hint: '价差上限，高波动时触发' } },
  cycle_interval_s:  { type: 'number',  en: { label: 'Cycle Interval (sec)',   hint: 'Wait after each buy-sell cycle' }, zh: { label: '循环间隔 (秒)', hint: '每次买卖结束后等待时间' } },
  max_cycles:        { type: 'integer', en: { label: 'Max Cycles',             hint: '0 = unlimited' },                zh: { label: '最大循环次数',          hint: '0 = 无限循环' } },
  // SpreadArb — leg safety
  leg_timeout_s:     { type: 'number',  en: { label: 'Leg Timeout (sec)',      hint: 'Hedge unfilled leg after this' },   zh: { label: '腿超时 (秒)',           hint: '超时后反向平仓' } },
  max_mismatches:    { type: 'integer', en: { label: 'Max Mismatches',         hint: 'Pause symbol after N failures' },   zh: { label: '最大腿失配次数',         hint: '超过后暂停该交易对' } },
  use_maker_leg:     { type: 'boolean', en: { label: 'Use Maker Leg',          hint: 'Post-Only on buy side (saves fee)' }, zh: { label: '使用Maker单',          hint: '买腿用限价单省手续费' } },
  maker_timeout_s:   { type: 'number',  en: { label: 'Maker Timeout (sec)',    hint: 'Fall back to market if unfilled' }, zh: { label: 'Maker超时 (秒)',        hint: '未成交后降级为市价单' } },
  // Grid  (max_inventory_usdt is shared with MarketMaker, defined above)
  trailing_grid:     { type: 'boolean', en: { label: 'Trailing Grid',          hint: 'Shift grid range with price' },     zh: { label: '移动网格',              hint: '跟随价格移动网格区间' } },
  // MarketMaker
  adverse_vol_mult:  { type: 'number',  en: { label: 'Adverse Vol Mult',       hint: 'Widen spread on vol spike' },       zh: { label: '对冲波动倍数',           hint: '波动飙升时放宽价差倍数' } },
  adverse_obi_thresh:{ type: 'number',  en: { label: 'OBI Threshold',          hint: 'Pause one side if |OBI| > this' }, zh: { label: 'OBI阈值',               hint: '订单流不平衡超此值时单边暂停' } },
}

function normalizeStrat(s) {
  const id = s.id || s.strategy_id
  return { ...s, id, name: s.name || STRAT_NAMES[id] || id }
}

// Fields to exclude from the dynamic stats bar
const STATS_SKIP = new Set([
  'strategy_id', 'id', 'name', 'enabled', 'params', 'description', 'stats',
  'last_spreads_bps', 'latest_rates', 'custom', 'source_file',
  // Internal tracking fields — too verbose or redundant to show in stats bar
  'paused_symbols', 'mismatch_counts',
  'legs', 'basis_history',
  // Already shown in global risk panel
  'halted', 'halt_reason', 'consecutive_losses',
  // Rate data — too verbose as raw numbers
  'latest_rates', 'latest_rates_8h',
  // Large nested dicts
  'open_positions',
])

function fmtStatValue(v) {
  if (typeof v === 'boolean') return v ? '✓ on' : '— off'
  if (Array.isArray(v)) return v.length === 0 ? '—' : v.length
  if (typeof v === 'object' && v !== null) {
    const n = Object.keys(v).length
    return n === 0 ? '—' : n
  }
  if (typeof v === 'number') return Number.isInteger(v) ? v : v.toFixed(2)
  if (v === null || v === undefined || v === '') return '—'
  return String(v)
}

function StatItem({ label, value }) {
  return (
    <div>
      <div style={{ fontSize: 9, color: 'var(--t2)', marginBottom: 3, textTransform: 'uppercase', letterSpacing: '0.06em' }}>{label}</div>
      <div className="num" style={{ fontSize: 13, fontWeight: 600, color: 'var(--t1)' }}>{value}</div>
    </div>
  )
}

const STATE_LABEL = {
  idle:         { en: 'Idle',             zh: '空闲',       color: 'var(--t3)' },
  placing_buy:  { en: 'Placing buy…',     zh: '下单买入中…', color: '#f0b90b' },
  buying:       { en: 'Awaiting fill',    zh: '等待买单成交', color: '#f0b90b' },
  placing_sell: { en: 'Placing sell…',    zh: '下单卖出中…', color: 'var(--accent)' },
  selling:      { en: 'Awaiting fill',    zh: '等待卖单成交', color: 'var(--accent)' },
  cooling:      { en: 'Cooling',          zh: '冷却中',      color: 'var(--t2)' },
  done:         { en: 'Done',             zh: '已完成',      color: 'var(--green)' },
}

function TradingCompCard({ strategy, onToggle, onParamChange, t, lang }) {
  const [localSymbol, setLocalSymbol] = useState(strategy.params?.symbol || 'BTC-USDT')
  const [localAmount, setLocalAmount] = useState(strategy.params?.order_usdt || 50)
  const [localInterval, setLocalInterval] = useState(strategy.params?.cycle_interval_s || 60)
  const [localMaxCycles, setLocalMaxCycles] = useState(strategy.params?.max_cycles || 0)
  const isActive = strategy.enabled
  const state = strategy.state || 'idle'
  const stateInfo = STATE_LABEL[state] || STATE_LABEL.idle

  const applyAndStart = () => {
    onParamChange(strategy.id, 'symbol', localSymbol.toUpperCase().trim())
    onParamChange(strategy.id, 'order_usdt', parseFloat(localAmount) || 50)
    onParamChange(strategy.id, 'cycle_interval_s', parseFloat(localInterval) || 60)
    onParamChange(strategy.id, 'max_cycles', parseInt(localMaxCycles, 10) || 0)
    setTimeout(() => onToggle(strategy.id, true), 200)
  }

  return (
    <div className="card" style={{
      overflow: 'hidden',
      border: `1px solid ${isActive ? 'rgba(59,123,255,0.3)' : 'var(--border)'}`,
      transition: 'border-color 0.2s',
    }}>
      {/* Header */}
      <div style={{
        padding: '18px 20px',
        background: isActive ? 'linear-gradient(90deg,rgba(59,123,255,0.08) 0%,transparent 80%)' : 'none',
        display: 'flex', alignItems: 'center', gap: 14,
      }}>
        <div style={{
          width: 10, height: 10, borderRadius: '50%', flexShrink: 0,
          background: isActive ? 'var(--green)' : 'var(--border2)',
          boxShadow: isActive ? '0 0 8px var(--green)' : 'none',
          transition: 'all 0.3s',
        }} />
        <div style={{ flex: 1 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 4, flexWrap: 'wrap' }}>
            <span style={{ fontWeight: 700, fontSize: 14, color: 'var(--t1)' }}>
              {STRAT_NAMES.trading_comp}
            </span>
            <span style={{
              fontSize: 9, fontWeight: 700, padding: '2px 8px', borderRadius: 10,
              letterSpacing: '0.05em', textTransform: 'uppercase',
              background: 'rgba(240,185,11,0.1)', color: '#f0b90b',
              border: '1px solid rgba(240,185,11,0.25)',
            }}>{t('comp_tool_badge')}</span>
            {isActive && (
              <span style={{
                fontSize: 11, fontWeight: 600,
                color: stateInfo.color,
              }}>● {stateInfo[lang] || stateInfo.en}</span>
            )}
          </div>
          <div style={{ fontSize: 12, color: 'var(--t3)' }}>
            {STRAT_DESC[lang]?.trading_comp || STRAT_DESC.en.trading_comp}
          </div>
        </div>
      </div>

      {/* Config + stats */}
      <div style={{ padding: '16px 20px', borderTop: '1px solid var(--border)', background: 'var(--surface)' }}>
        {/* Quick-config inputs */}
        <div style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fill, minmax(160px, 1fr))',
          gap: 12, marginBottom: 16,
        }}>
          <div>
            <label className="label">{t('comp_symbol_label')}</label>
            <input type="text" value={localSymbol}
              onChange={e => setLocalSymbol(e.target.value)}
              disabled={isActive}
              style={{ width: '100%', marginTop: 6, opacity: isActive ? 0.5 : 1 }}
              placeholder="BTC-USDT" />
          </div>
          <div>
            <label className="label">{t('comp_amount_label')}</label>
            <input type="number" min="1" step="any" value={localAmount}
              onChange={e => setLocalAmount(e.target.value)}
              disabled={isActive}
              style={{ width: '100%', marginTop: 6, opacity: isActive ? 0.5 : 1 }} />
          </div>
          <div>
            <label className="label">{t('comp_interval_label')}</label>
            <input type="number" min="1" step="1" value={localInterval}
              onChange={e => setLocalInterval(e.target.value)}
              disabled={isActive}
              style={{ width: '100%', marginTop: 6, opacity: isActive ? 0.5 : 1 }} />
          </div>
          <div>
            <label className="label">{t('comp_max_cycles_label')}</label>
            <input type="number" min="0" step="1" value={localMaxCycles}
              onChange={e => setLocalMaxCycles(e.target.value)}
              disabled={isActive}
              style={{ width: '100%', marginTop: 6, opacity: isActive ? 0.5 : 1 }} />
          </div>
        </div>

        {/* Stats row (visible when active or after at least one cycle) */}
        {(isActive || (strategy.cycles_completed > 0)) && (
          <div style={{
            display: 'flex', gap: 24, flexWrap: 'wrap', marginBottom: 16,
            padding: '12px 16px', borderRadius: 10,
            background: 'var(--bg2)', border: '1px solid var(--border)',
          }}>
            <div>
              <div style={{ fontSize: 9, color: 'var(--t2)', marginBottom: 3, textTransform: 'uppercase', letterSpacing: '0.06em' }}>{t('comp_cycles_done')}</div>
              <div className="num" style={{ fontSize: 20, fontWeight: 700, color: 'var(--t1)' }}>{strategy.cycles_completed ?? 0}</div>
            </div>
            <div>
              <div style={{ fontSize: 9, color: 'var(--t2)', marginBottom: 3, textTransform: 'uppercase', letterSpacing: '0.06em' }}>{t('comp_total_vol')}</div>
              <div className="num" style={{ fontSize: 20, fontWeight: 700, color: 'var(--accent)' }}>
                {((strategy.total_volume_usdt ?? 0)).toFixed(2)} USDT
              </div>
            </div>
            <div>
              <div style={{ fontSize: 9, color: 'var(--t2)', marginBottom: 3, textTransform: 'uppercase', letterSpacing: '0.06em' }}>{t('comp_total_fees')}</div>
              <div className="num" style={{ fontSize: 20, fontWeight: 700, color: 'var(--red)' }}>
                -{((strategy.total_fees_usdt ?? 0)).toFixed(4)} USDT
              </div>
            </div>
            {strategy.last_cycle_pnl !== undefined && strategy.cycles_completed > 0 && (
              <div>
                <div style={{ fontSize: 9, color: 'var(--t2)', marginBottom: 3, textTransform: 'uppercase', letterSpacing: '0.06em' }}>{t('comp_last_pnl')}</div>
                <div className="num" style={{
                  fontSize: 16, fontWeight: 600,
                  color: strategy.last_cycle_pnl >= 0 ? 'var(--green)' : 'var(--red)',
                }}>
                  {strategy.last_cycle_pnl >= 0 ? '+' : ''}{(strategy.last_cycle_pnl ?? 0).toFixed(6)} USDT
                </div>
              </div>
            )}
            {state === 'cooling' && strategy.cool_remaining_s > 0 && (
              <div>
                <div style={{ fontSize: 9, color: 'var(--t2)', marginBottom: 3, textTransform: 'uppercase', letterSpacing: '0.06em' }}>{t('comp_next_buy')}</div>
                <div className="num" style={{ fontSize: 16, fontWeight: 600, color: 'var(--t2)' }}>
                  {strategy.cool_remaining_s.toFixed(0)}s
                </div>
              </div>
            )}
          </div>
        )}

        {/* Start / Stop button */}
        <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
          {!isActive ? (
            <Button variant="primary" onClick={applyAndStart}>
              {t('comp_start_btn')}
            </Button>
          ) : (
            <Button variant="red" onClick={() => onToggle(strategy.id, false)}>
              {t('comp_stop_btn')}
            </Button>
          )}
          <span style={{ fontSize: 11, color: 'var(--t3)' }}>
            {isActive
              ? t('comp_running_status', strategy.params?.symbol || localSymbol, strategy.params?.order_usdt || localAmount)
              : t('comp_start_hint')}
          </span>
        </div>
      </div>
    </div>
  )
}

function PerformanceStrip({ strategies, lang }) {
  const [summary, setSummary] = useState([])
  useEffect(() => {
    const key = localStorage.getItem('trading_api_key') || ''
    fetch('/api/strategies/summary', { headers: key ? { 'X-API-Key': key } : {} })
      .then(r => r.ok ? r.json() : [])
      .then(d => Array.isArray(d) ? setSummary(d) : setSummary([]))
      .catch(() => {})
  }, [strategies])

  // Merge live realized_pnl with DB summary
  const merged = strategies
    .filter(s => s.id !== 'trading_comp')
    .map(s => {
      const db = summary.find(r => r.strategy_id === s.id) || {}
      const livePnl = s.realized_pnl_usdt || 0
      return {
        id: s.id,
        name: s.name || s.id,
        enabled: s.enabled,
        livePnl,
        totalPnl: db.total_pnl ?? livePnl,
        trades: db.total_trades ?? (s.trade_count || 0),
        bestDay: db.best_day ?? null,
        worstDay: db.worst_day ?? null,
      }
    })
    .sort((a, b) => b.totalPnl - a.totalPnl)

  if (merged.every(s => s.totalPnl === 0 && s.trades === 0)) return null

  const pnlColor = v => v > 0 ? 'var(--green)' : v < 0 ? 'var(--red)' : 'var(--t3)'
  const fmtPnl = v => v == null ? '—' : `${v >= 0 ? '+' : ''}${v.toFixed(4)}`

  return (
    <div className="card" style={{ padding: '14px 18px', overflow: 'hidden' }}>
      <div style={{ fontSize: 10, fontWeight: 700, color: 'var(--t3)', letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 12 }}>
        {lang === 'zh' ? '策略绩效排行' : 'Strategy Performance'}
      </div>
      <div style={{ display: 'flex', gap: 10, overflowX: 'auto', overflowY: 'hidden', paddingBottom: 4 }}>
        {merged.map(s => (
          <div key={s.id} style={{
            flexShrink: 0, padding: '10px 14px', borderRadius: 8, minWidth: 140,
            background: s.enabled ? 'rgba(59,123,255,0.06)' : 'var(--surface)',
            border: `1px solid ${s.enabled ? 'rgba(59,123,255,0.2)' : 'var(--border)'}`,
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 8 }}>
              <span className={`dot ${s.enabled ? 'dot-green' : 'dot-muted'}`} style={{ width: 6, height: 6 }} />
              <span style={{ fontSize: 10, fontWeight: 700, color: 'var(--t1)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 100 }}>
                {s.name.split('/')[0].trim()}
              </span>
            </div>
            <div style={{ marginBottom: 5 }}>
              <div style={{ fontSize: 9, color: 'var(--t3)', marginBottom: 2 }}>{lang === 'zh' ? '累计盈亏' : 'Total P&L'}</div>
              <span className="num" style={{ fontSize: 14, fontWeight: 700, color: pnlColor(s.totalPnl) }}>
                {fmtPnl(s.totalPnl)}
              </span>
              <span style={{ fontSize: 9, color: 'var(--t3)', marginLeft: 3 }}>U</span>
            </div>
            <div style={{ display: 'flex', gap: 10 }}>
              <div>
                <div style={{ fontSize: 9, color: 'var(--t3)' }}>{lang === 'zh' ? '交易次数' : 'Trades'}</div>
                <span className="num" style={{ fontSize: 11, color: 'var(--t2)' }}>{s.trades}</span>
              </div>
              {s.bestDay != null && s.bestDay !== 0 && (
                <div>
                  <div style={{ fontSize: 9, color: 'var(--t3)' }}>{lang === 'zh' ? '最佳日' : 'Best Day'}</div>
                  <span className="num" style={{ fontSize: 11, color: 'var(--green)' }}>+{s.bestDay.toFixed(2)}</span>
                </div>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

function StrategyPnLChart({ strategyId }) {
  const [data, setData] = useState([])
  useEffect(() => {
    fetch(`/api/strategies/${strategyId}/pnl-history?days=30`)
      .then(r => r.ok ? r.json() : [])
      .then(d => setData(Array.isArray(d) ? d : []))
      .catch(() => {})
  }, [strategyId])

  if (data.length < 2) return null
  const values = data.map(d => d.daily_pnl || 0)
  const cumulative = values.reduce((acc, v, i) => {
    acc.push((acc[i - 1] || 0) + v)
    return acc
  }, [])
  const w = 300, h = 48
  const min = Math.min(...cumulative, 0)
  const max = Math.max(...cumulative, 0.001)
  const range = max - min || 0.001
  const pts = cumulative.map((v, i) => [
    (i / (cumulative.length - 1)) * w,
    h - ((v - min) / range) * (h - 4) - 2,
  ])
  const line = pts.map((p, i) => `${i === 0 ? 'M' : 'L'}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ')
  const isUp = cumulative[cumulative.length - 1] >= 0
  const c = isUp ? 'var(--green)' : 'var(--red)'
  const total = cumulative[cumulative.length - 1]

  return (
    <div style={{
      padding: '8px 20px 10px',
      borderTop: '1px solid var(--border)',
      display: 'flex', alignItems: 'center', gap: 16,
      background: 'var(--surface)',
    }}>
      <div style={{ fontSize: 9, color: 'var(--t3)', textTransform: 'uppercase', letterSpacing: '0.06em', whiteSpace: 'nowrap' }}>
        30d PnL
      </div>
      <svg width={w} height={h} style={{ flex: 1, maxWidth: 300 }}>
        <path d={line} fill="none" stroke={c} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
      <div className="num" style={{ fontSize: 12, fontWeight: 700, color: c, whiteSpace: 'nowrap', minWidth: 70, textAlign: 'right' }}>
        {total >= 0 ? '+' : ''}{total.toFixed(4)} U
      </div>
    </div>
  )
}

function StrategyCard({ strategy, onToggle, onParamChange, onDelete, onEdit, t, lang }) {
  const [expanded, setExpanded] = useState(false)
  const isActive = strategy.enabled
  const isCustom = strategy.custom
  const desc = STRAT_DESC[lang]?.[strategy.id] || STRAT_DESC.en[strategy.id] || strategy.description || ''
  const hasParams = strategy.params && Object.keys(strategy.params).length > 0
  const isGridInactive = hasParams
    && 'grid_low' in strategy.params && 'grid_high' in strategy.params
    && (strategy.params.grid_low === 0 || !strategy.params.grid_low)
    && (strategy.params.grid_high === 0 || !strategy.params.grid_high)

  return (
    <div className="card" style={{
      overflow: 'hidden',
      border: `1px solid ${isActive ? 'rgba(59,123,255,0.22)' : isCustom ? 'rgba(124,58,237,0.2)' : 'var(--border)'}`,
      transition: 'border-color 0.2s',
    }}>
      {/* Card header */}
      <div style={{
        padding: '18px 20px',
        background: isActive ? 'linear-gradient(90deg, rgba(59,123,255,0.06) 0%, transparent 80%)' : 'none',
        display: 'flex', alignItems: 'flex-start', gap: 14,
      }}>
        <div style={{
          width: 10, height: 10, borderRadius: '50%', flexShrink: 0, marginTop: 4,
          background: isActive ? 'var(--green)' : 'var(--border2)',
          boxShadow: isActive ? '0 0 8px var(--green)' : 'none',
          transition: 'all 0.3s',
        }} />

        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 5, flexWrap: 'wrap' }}>
            <span style={{ fontWeight: 700, fontSize: 14, color: 'var(--t1)' }}>{strategy.name}</span>
            {isCustom && (
              <span style={{
                fontSize: 9, fontWeight: 700, padding: '2px 7px', borderRadius: 10,
                letterSpacing: '0.05em', textTransform: 'uppercase',
                background: 'rgba(124,58,237,0.12)', color: '#a78bfa',
                border: '1px solid rgba(124,58,237,0.2)',
              }}>CUSTOM</span>
            )}
            <span style={{
              fontSize: 9, fontWeight: 700, padding: '2px 7px', borderRadius: 10,
              letterSpacing: '0.05em', textTransform: 'uppercase',
              background: isActive ? 'rgba(0,217,163,0.12)' : 'var(--surface)',
              color: isActive ? 'var(--green)' : 'var(--t3)',
            }}>
              {isActive ? t('strat_active') : t('strat_inactive')}
            </span>
          </div>
          {desc && <div style={{ fontSize: 12, color: 'var(--t3)', lineHeight: 1.6, marginBottom: 4 }}>{desc}</div>}

          {/* Grid not activated warning */}
          {isGridInactive && (
            <div style={{
              display: 'inline-flex', alignItems: 'center', gap: 5,
              fontSize: 10, fontWeight: 600, padding: '3px 8px', borderRadius: 6, marginTop: 2,
              background: 'rgba(240,185,11,0.08)', color: '#f0b90b',
              border: '1px solid rgba(240,185,11,0.2)',
            }}>
              {t('grid_inactive_warn')}
            </div>
          )}

          {/* Per-strategy PnL stats */}
          {(strategy.trade_count > 0 || strategy.realized_pnl_usdt !== 0) && (
            <div style={{ display: 'flex', gap: 16, marginTop: 6, flexWrap: 'wrap' }}>
              <span style={{ fontSize: 11, color: 'var(--t3)' }}>
                {t('trade_count_label', strategy.trade_count)}
              </span>
              <span style={{ fontSize: 11, color: 'var(--t3)' }}>
                {t('realized_pnl_label')}{' '}
                <b className="num" style={{ color: strategy.realized_pnl_usdt >= 0 ? 'var(--green)' : 'var(--red)' }}>
                  {strategy.realized_pnl_usdt >= 0 ? '+' : ''}{strategy.realized_pnl_usdt?.toFixed(4)} USDT
                </b>
              </span>
              {strategy.uptime_h > 0 && (
                <span style={{ fontSize: 11, color: 'var(--t3)' }}>
                  {t('uptime_label', strategy.uptime_h)}
                </span>
              )}
            </div>
          )}
        </div>

        <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexShrink: 0 }}>
          {hasParams && (
            <Button size="xs" variant={expanded ? 'primary' : 'ghost'} onClick={() => setExpanded(x => !x)}>
              {expanded ? t('hide') : t('params')}
            </Button>
          )}
          {isCustom && strategy.source_file && (
            <Button variant="purple" size="xs" onClick={() => onEdit?.(strategy.source_file)}
                    title={t('edit_custom_title')}>
              {t('edit_btn')}
            </Button>
          )}
          {isCustom && (
            <Button variant="red" size="xs" disabled={isActive}
                    onClick={() => !isActive && onDelete(strategy.source_file)}
                    title={isActive ? t('delete_disabled_title') : t('delete_custom_title')}>
              ✕
            </Button>
          )}
          <label className="toggle">
            <input type="checkbox" checked={isActive} onChange={() => onToggle(strategy.id, !isActive)} />
            <span className="toggle-track"><span className="toggle-thumb" /></span>
          </label>
        </div>
      </div>

      {/* Live stats bar — grid layout, labels row + values row, horizontally scrollable */}
      {isActive && (() => {
        const entries = Object.entries(strategy).filter(([k]) => !STATS_SKIP.has(k))
        if (entries.length === 0) return null
        return (
          <div style={{
            borderTop: '1px solid var(--border)',
            background: 'var(--surface)',
            padding: '10px 20px 12px',
            overflowX: 'auto',
            overflowY: 'hidden',
          }}>
            <div style={{
              display: 'grid',
              gridAutoFlow: 'column',
              gridTemplateRows: 'auto auto',
              columnGap: 28,
              rowGap: 4,
              width: 'max-content',
            }}>
              {entries.flatMap(([k, v]) => [
                <div key={`${k}-l`} style={{ fontSize: 9, color: 'var(--t2)', textTransform: 'uppercase', letterSpacing: '0.06em', whiteSpace: 'nowrap' }}>
                  {k.replace(/_/g, ' ')}
                </div>,
                <div key={`${k}-v`} className="num" style={{ fontSize: 13, fontWeight: 600, color: 'var(--t1)', whiteSpace: 'nowrap' }}>
                  {fmtStatValue(v)}
                </div>,
              ])}
            </div>
          </div>
        )
      })()}

      {/* 30-day PnL mini chart */}
      <StrategyPnLChart strategyId={strategy.strategy_id || strategy.id} />

      {/* Params editor */}
      {expanded && hasParams && (
        <div style={{ padding: '16px 20px', borderTop: '1px solid var(--border)', background: 'var(--surface)' }}>
          <div style={{
            fontSize: 10, fontWeight: 700, color: 'var(--t3)',
            letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 14,
          }}>{t('parameters')}</div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))', gap: 12 }}>
            {Object.entries(strategy.params).map(([k, v]) => {
              const meta = PARAM_META[k] || {}
              const pType = meta.type || (typeof v === 'number' ? 'number' : 'text')
              const lx = (meta[lang] || meta.en) || {}
              const label = lx.label || k.replace(/_/g, ' ')
              const hint = lx.hint || ''
              // Auto-detect boolean if no meta defined
              const effectiveType = pType !== 'text' ? pType : (typeof v === 'boolean' ? 'boolean' : pType)
              let inputEl
              if (effectiveType === 'boolean') {
                inputEl = (
                  <div style={{ marginTop: 8, display: 'flex', alignItems: 'center', gap: 10 }}>
                    <label className="toggle" style={{ transform: 'scale(0.85)', transformOrigin: 'left' }}>
                      <input type="checkbox" checked={!!v} onChange={e => onParamChange(strategy.id, k, e.target.checked)} />
                      <span className="toggle-track"><span className="toggle-thumb" /></span>
                    </label>
                    <span style={{ fontSize: 11, color: v ? 'var(--green)' : 'var(--t3)' }}>{v ? 'ON' : 'OFF'}</span>
                  </div>
                )
              } else if (effectiveType === 'select') {
                inputEl = (
                  <select value={v} onChange={e => onParamChange(strategy.id, k, e.target.value)}
                    style={{ width: '100%', marginTop: 6, background: 'var(--bg2)', color: 'var(--t1)', border: '1px solid var(--border)', borderRadius: 6, padding: '6px 8px', fontSize: 12 }}>
                    {(meta.options || []).map(opt => <option key={opt} value={opt}>{opt}</option>)}
                  </select>
                )
              } else if (effectiveType === 'array') {
                const display = Array.isArray(v) ? v.join(', ') : String(v)
                inputEl = (
                  <input type="text" defaultValue={display} style={{ width: '100%', marginTop: 6 }}
                    onBlur={e => onParamChange(strategy.id, k,
                      e.target.value.split(',').map(s => s.trim()).filter(Boolean)
                    )} />
                )
              } else if (effectiveType === 'integer') {
                inputEl = (
                  <input type="number" step="1" defaultValue={v} style={{ width: '100%', marginTop: 6 }}
                    onBlur={e => onParamChange(strategy.id, k, parseInt(e.target.value, 10))} />
                )
              } else if (effectiveType === 'number') {
                inputEl = (
                  <input type="number" step="any" defaultValue={v} style={{ width: '100%', marginTop: 6 }}
                    onBlur={e => onParamChange(strategy.id, k, parseFloat(e.target.value))} />
                )
              } else {
                inputEl = (
                  <input type="text" defaultValue={String(v)} style={{ width: '100%', marginTop: 6 }}
                    onBlur={e => onParamChange(strategy.id, k, e.target.value)} />
                )
              }
              return (
                <div key={k}>
                  <label className="label">{label}</label>
                  {inputEl}
                  {hint && <div style={{ fontSize: 10, color: 'var(--t3)', marginTop: 4 }}>{hint}</div>}
                </div>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}

function CustomStrategyEditor({ initial, onClose, onSaved, t }) {
  // initial: { filename, content } — empty filename = new strategy
  const [filename, setFilename] = useState(initial.filename || '')
  const [code, setCode] = useState(initial.content || '')
  const [check, setCheck] = useState(null)   // { ok, strategy_ids, error } | { busy }
  const [saving, setSaving] = useState(false)
  const headers = (json) => {
    const k = getApiKey()
    return { ...(json ? { 'Content-Type': 'application/json' } : {}), ...(k ? { 'X-API-Key': k } : {}) }
  }

  const validate = async () => {
    setCheck({ busy: true })
    try {
      const r = await fetch('/api/strategies/custom/validate', {
        method: 'POST', headers: headers(true), body: JSON.stringify({ content: code }),
      })
      setCheck(await r.json())
    } catch (e) { setCheck({ ok: false, error: String(e.message || e), strategy_ids: [] }) }
  }

  const save = async () => {
    if (!filename.trim()) { setCheck({ ok: false, error: t('fname_required'), strategy_ids: [] }); return }
    setSaving(true)
    try {
      const r = await fetch('/api/strategies/custom/save', {
        method: 'POST', headers: headers(true),
        body: JSON.stringify({ filename: filename.trim(), content: code }),
      })
      const d = await r.json()
      if (!r.ok) { setCheck({ ok: false, error: d.detail || 'save failed', strategy_ids: [] }); return }
      onSaved(d)   // parent reloads + flashes + closes
    } catch (e) { setCheck({ ok: false, error: String(e.message || e), strategy_ids: [] }) }
    finally { setSaving(false) }
  }

  return (
    <div onClick={onClose} style={{
      position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.55)', zIndex: 1000,
      display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 20,
    }}>
      <div onClick={e => e.stopPropagation()} style={{
        background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 12,
        width: 'min(900px, 96vw)', maxHeight: '92vh', display: 'flex', flexDirection: 'column',
        boxShadow: '0 16px 48px rgba(0,0,0,0.5)',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '14px 18px', borderBottom: '1px solid var(--border)' }}>
          <span style={{ fontWeight: 700, fontSize: 14, color: '#a78bfa' }}>
            {initial.filename ? t('edit_strategy_title') : t('new_strategy_title')}
          </span>
          <input
            value={filename}
            onChange={e => setFilename(e.target.value)}
            placeholder="my_strategy.py"
            disabled={!!initial.filename}
            style={{
              flex: 1, padding: '6px 10px', borderRadius: 7, border: '1px solid var(--border)',
              background: 'var(--surface)', color: 'var(--t1)', fontSize: 12, fontFamily: 'monospace',
            }} />
          <Button variant="ghost" size="sm" onClick={onClose}>{t('close') || 'Close'}</Button>
        </div>

        <textarea
          value={code}
          onChange={e => { setCode(e.target.value); setCheck(null) }}
          spellCheck={false}
          style={{
            flex: 1, minHeight: 360, resize: 'vertical', padding: 14, border: 'none', outline: 'none',
            background: 'var(--surface)', color: 'var(--t1)', fontSize: 12.5, lineHeight: 1.5,
            fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', whiteSpace: 'pre', overflow: 'auto',
          }} />

        <div style={{ padding: '10px 18px', borderTop: '1px solid var(--border)', display: 'flex', alignItems: 'center', gap: 12 }}>
          <Button variant="ghost" size="sm" onClick={validate} disabled={check?.busy}>
            {check?.busy ? t('validating_btn') : t('validate_btn')}
          </Button>
          <Button variant="purple" size="sm" onClick={save} disabled={saving}>
            {saving ? t('saving_btn') : t('save_reload_btn')}
          </Button>

          {check && !check.busy && (
            check.ok
              ? <span style={{ fontSize: 12, color: 'var(--green)' }}>✓ {t('valid_defines', (check.strategy_ids || []).join(', '))}</span>
              : <span style={{ fontSize: 12, color: 'var(--red)' }}>✗ {check.error}</span>
          )}
          <span style={{ marginLeft: 'auto', fontSize: 10, color: 'var(--t3)' }}>{t('editor_hint')}</span>
        </div>
      </div>
    </div>
  )
}

export default function StrategiesPage({ strategies = [] }) {
  const { lang, t } = useLang()
  const fileInputRef = useRef(null)

  const [localStrats, setLocalStrats] = useState(() =>
    (strategies.length ? strategies : DEFAULT_STRATS).map(normalizeStrat)
  )
  const [reloading, setReloading] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [flash, setFlash] = useState(null)   // { text, ok }
  const [confirm, setConfirm] = useState(null)  // { title, message, onConfirm, dangerous }
  const [editor, setEditor] = useState(null)  // { filename, content } | null

  const showFlash = (text, ok = true) => {
    setFlash({ text, ok })
    setTimeout(() => setFlash(null), 3500)
  }

  useEffect(() => {
    if (strategies.length) setLocalStrats(strategies.map(normalizeStrat))
  }, [strategies])

  // Poll every 5s when any strategy is active to get fresh stats
  const fetchStrategies = useCallback(async () => {
    try {
      const key = getApiKey()
      const data = await fetch('/api/strategies', { headers: key ? { 'X-API-Key': key } : {} }).then(r => r.json())
      if (Array.isArray(data)) setLocalStrats(data.map(normalizeStrat))
    } catch { /* ignore when backend offline */ }
  }, [])

  const hasActive = localStrats.some(s => s.enabled)
  useEffect(() => {
    if (!hasActive) return
    const iv = setInterval(fetchStrategies, 5000)
    return () => clearInterval(iv)
  }, [hasActive, fetchStrategies])

  const toggleStrategy = (id, enabled) => {
    const strat = localStrats.find(x => x.id === id)
    // Confirm before stopping an active strategy
    if (!enabled && strat?.enabled) {
      setConfirm({
        title: lang === 'zh' ? '确认停止策略？' : 'Stop Strategy?',
        message: lang === 'zh'
          ? `停止 "${strat.name || id}" 将取消所有挂单并暂停交易。`
          : `Stopping "${strat.name || id}" will cancel pending actions and pause trading.`,
        confirmLabel: lang === 'zh' ? '确认停止' : 'Stop Strategy',
        dangerous: true,
        onConfirm: async () => {
          setConfirm(null)
          setLocalStrats(s => s.map(x => x.id === id ? { ...x, enabled: false } : x))
          try {
            const key = getApiKey()
            await fetch(`/api/strategies/${id}/disable`, { method: 'POST', headers: key ? { 'X-API-Key': key } : {} })
          } catch { /* optimistic */ }
        },
      })
      return
    }
    setLocalStrats(s => s.map(x => x.id === id ? { ...x, enabled } : x))
    const key = getApiKey()
    fetch(`/api/strategies/${id}/${enabled ? 'enable' : 'disable'}`, { method: 'POST', headers: key ? { 'X-API-Key': key } : {} }).catch(() => {})
  }

  const updateParam = async (id, key, value) => {
    setLocalStrats(s => s.map(x => x.id === id ? { ...x, params: { ...x.params, [key]: value } } : x))
    try {
      const current = localStrats.find(x => x.id === id)?.params || {}
      const apiKey = getApiKey()
      await fetch(`/api/strategies/${id}/params`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...(apiKey ? { 'X-API-Key': apiKey } : {}) },
        body: JSON.stringify({ params: { ...current, [key]: value } }),
      })
    } catch { /* ignore */ }
  }

  const openNewStrategy = async () => {
    let content = ''
    try {
      const key = getApiKey()
      content = await fetch('/api/strategies/custom/template', { headers: key ? { 'X-API-Key': key } : {} }).then(r => r.text())
    } catch { /* start blank if template unavailable */ }
    setEditor({ filename: '', content })
  }

  const openEditStrategy = async (sourceFile) => {
    if (!sourceFile) { showFlash(t('cannot_determine_file'), false); return }
    try {
      const key = getApiKey()
      const d = await fetch(`/api/strategies/custom/${sourceFile}/source`, { headers: key ? { 'X-API-Key': key } : {} }).then(r => r.json())
      setEditor({ filename: d.filename, content: d.content })
    } catch (e) { showFlash(String(e.message || e), false) }
  }

  const onEditorSaved = async (d) => {
    setEditor(null)
    showFlash(t('saved_strategy', d.saved))
    await handleReload()
  }

  const handleUpload = async (e) => {
    const file = e.target.files?.[0]
    if (!file) return
    e.target.value = ''
    setUploading(true)
    try {
      const form = new FormData()
      form.append('file', file)
      const apiKey = getApiKey()
      const res = await fetch('/api/strategies/custom/upload', { method: 'POST', body: form, headers: apiKey ? { 'X-API-Key': apiKey } : {} })
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }))
        showFlash(err.detail || 'Upload failed', false)
        return
      }
      showFlash(t('upload_success', file.name))
      await handleReload()
    } catch (err) {
      showFlash(err.message, false)
    } finally {
      setUploading(false)
    }
  }

  const handleReload = async () => {
    setReloading(true)
    try {
      const key = getApiKey()
      const res = await fetch('/api/strategies/reload', { method: 'POST', headers: key ? { 'X-API-Key': key } : {} })
      const data = await res.json()
      if (data.errors && Object.keys(data.errors).length > 0) {
        const errMsg = Object.entries(data.errors).map(([f, e]) => `${f}: ${e}`).join('; ')
        showFlash(t('load_error', errMsg), false)
      } else {
        const added = Object.keys(data.reloaded).length
        showFlash(added > 0 ? t('loaded_strategies', added) : t('custom_dir_empty'))
      }
      await fetchStrategies()
    } catch (err) {
      showFlash(err.message, false)
    } finally {
      setReloading(false)
    }
  }

  const handleDelete = (sourceFile) => {
    if (!sourceFile) { showFlash(t('cannot_determine_file'), false); return }
    setConfirm({
      title: lang === 'zh' ? '确认删除策略文件？' : 'Delete Strategy File?',
      message: lang === 'zh'
        ? `将永久删除 "${sourceFile}"，此操作不可撤销。`
        : `"${sourceFile}" will be permanently deleted. This cannot be undone.`,
      confirmLabel: lang === 'zh' ? '确认删除' : 'Delete',
      dangerous: true,
      onConfirm: async () => {
        setConfirm(null)
        await _doDelete(sourceFile)
      },
    })
  }

  const _doDelete = async (sourceFile) => {
    try {
      const key = getApiKey()
      const res = await fetch(`/api/strategies/custom/${sourceFile}`, { method: 'DELETE', headers: key ? { 'X-API-Key': key } : {} })
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }))
        showFlash(err.detail || 'Delete failed', false)
        return
      }
      showFlash(t('file_deleted', sourceFile))
      await fetchStrategies()
    } catch (err) {
      showFlash(err.message, false)
    }
  }

  const activeCount = localStrats.filter(s => s.enabled).length
  const customCount = localStrats.filter(s => s.custom).length

  return (
    <div className="page">
      {/* Custom strategy code editor */}
      {editor && (
        <CustomStrategyEditor
          initial={editor}
          onClose={() => setEditor(null)}
          onSaved={onEditorSaved}
          t={t}
        />
      )}

      {/* Confirmation dialog */}
      {confirm && (
        <ConfirmDialog
          title={confirm.title}
          message={confirm.message}
          confirmLabel={confirm.confirmLabel}
          dangerous={confirm.dangerous}
          onConfirm={confirm.onConfirm}
          onCancel={() => setConfirm(null)}
        />
      )}

      <PageHeader title={t('strategies_title')}>
        {flash && <span style={{ fontSize: 12, fontWeight: 600, color: flash.ok ? 'var(--green)' : 'var(--red)' }}>{flash.text}</span>}
        {activeCount > 0 && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <span className="dot dot-green blink" />
            <span style={{ fontSize: 12, color: 'var(--green)' }}>{t('strats_running', activeCount)}</span>
          </div>
        )}
        <span style={{ fontSize: 12, color: 'var(--t2)' }}>{t('strats_count', localStrats.length)}</span>
        <Button variant="ghost" size="sm" onClick={handleReload} disabled={reloading}>
          {reloading ? t('reloading_btn') : t('hot_reload_btn')}
        </Button>
        <Button variant="purple" size="sm" onClick={openNewStrategy}>
          {t('write_strategy_btn')}
        </Button>
        <Button variant="purple" size="sm" onClick={() => fileInputRef.current?.click()} disabled={uploading}>
          {uploading ? t('uploading_btn') : t('upload_strategy_btn')}
        </Button>
        <input ref={fileInputRef} type="file" accept=".py" style={{ display: 'none' }} onChange={handleUpload} />
      </PageHeader>

      {/* Performance comparison strip */}
      <PerformanceStrip strategies={localStrats} lang={lang} />

      {/* Custom strategy hint */}
      {customCount === 0 && (
        <div style={{
          padding: '12px 16px', borderRadius: 10,
          background: 'rgba(124,58,237,0.05)', border: '1px dashed rgba(124,58,237,0.2)',
          fontSize: 12, color: 'var(--t2)', lineHeight: 1.7,
        }}>
          <strong style={{ color: '#a78bfa' }}>{t('custom_hint_title')}</strong>
          {lang === 'zh'
            ? <>：点击"{t('upload_strategy_btn')}"上传 .py 文件，或将文件放入 <code style={{ color: 'var(--accent)' }}>strategies/custom/</code> 目录后点击"{t('hot_reload_btn')}"。可{' '}</>
            : <>: click "{t('upload_strategy_btn')}" to upload a .py file, or place it in <code style={{ color: 'var(--accent)' }}>strategies/custom/</code> and click "{t('hot_reload_btn')}". You can{' '}</>
          }
          <a
            href="/api/strategies/custom/template"
            download="my_strategy.py"
            style={{ color: 'var(--accent)', textDecoration: 'none' }}
          >{t('download_template_link')}</a>。
        </div>
      )}

      {localStrats.length === 0 ? (
        <div className="card">
          <div className="empty-state">
            <div className="empty-icon">⚡</div>
            <div className="empty-title">{t('no_strategies')}</div>
            <div className="empty-sub">{t('no_strategies_sub')}</div>
          </div>
        </div>
      ) : localStrats.map(s => (
        s.id === 'trading_comp'
          ? <TradingCompCard key={s.id} strategy={s} onToggle={toggleStrategy} onParamChange={updateParam} t={t} lang={lang} />
          : <StrategyCard
              key={s.id} strategy={s}
              onToggle={toggleStrategy}
              onParamChange={updateParam}
              onDelete={handleDelete}
              onEdit={openEditStrategy}
              t={t} lang={lang}
            />
      ))}
    </div>
  )
}
