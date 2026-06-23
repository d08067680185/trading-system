# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Orientation (read first)

A multi-exchange quant trading system: **Binance USDT-M futures + OKX swap (+ spot)**, a FastAPI
backend that also serves a built React frontend, all on SQLite. It runs as **one asyncio process**:
exchange connectors push typed events (`core/types.py`) onto a single `asyncio.Queue`; the engine's
event loop fans each to the strategies; a strategy reacts and trades; the order is gated by
`RiskManager` then sent to a connector; fills come back as events. Market data, strategy compute,
order execution, the REST/WS API, and backtests **all share that one event loop** — so the cardinal
rule is *never block it* (the `HealthMonitor`'s event-loop-lag metric exists because this matters).

Mental model for where things live and why:
- **Engine is the hub** (`core/engine.py`): `_process_event` is the single per-event handler used by
  both the live loop and the backtest replay driver — that shared path is what makes backtests faithful.
- **Strategies never touch connectors** — they return `Signal`s or call `engine.place_order` (see
  Strategy pattern). 6 built-ins + DEX, mostly arbitrage.
- **The exchange is the source of truth.** Private user-data WS is unavailable (API-key perms), so order
  state is rebuilt via a REST fill-poller + a periodic reconciler; treat local state as a cache.
- **Edge reality:** BTC/ETH cross-exchange and funding arb are *structurally* unprofitable here
  (documented below); the live opportunity is funding harvest on high-funding alts.
- **Money is `Decimal`** end to end; events/orders are the typed dataclasses in `core/types.py`.

The rest of this file drills into each subsystem; the data-flow diagram below is the map.

## Commands

### Backend
```bash
# Run (serves API + static frontend on :8080)
venv/bin/python main.py

# Kill all running instances (use this, not pkill -f "venv/bin/python main.py")
pkill -f "main.py"

# Tests — always scope to tests/ (root test_connection.py calls sys.exit and
# must NOT be collected). There is no pytest-asyncio; async tests wrap their
# body in asyncio.run(), and grid/optimizer helpers must run inside a loop.
venv/bin/python -m pytest tests/ -q                        # all tests
venv/bin/python -m pytest tests/test_risk_manager.py -q    # single file
venv/bin/python -m pytest tests/ -k "test_rolling" -q      # by name pattern

# Install deps into venv
venv/bin/pip install -r requirements.txt
```

### Frontend
```bash
cd frontend
npm run dev      # dev server on :5173 (hot-reload, proxies /api to :8080)
npm run build    # production build → frontend/dist/ (served by FastAPI)
```

### Docker
```bash
docker compose up -d        # production
docker compose logs -f      # tail logs
```

### API auth
`TRADING_API_KEY` is set in `.env` — every `/api/*` request needs the header
`X-API-Key: $TRADING_API_KEY` (read it from `.env`); `/health` and `/` are open.
WebSocket `/ws` accepts `?key=` query param. The frontend stores the key in
localStorage (System → Security page, enter once).

```bash
KEY=$(grep TRADING_API_KEY .env | cut -d= -f2)
curl -H "X-API-Key: $KEY" http://localhost:8080/api/system/status
```

### Emergency halt (out-of-band, no UI needed)
```bash
venv/bin/python scripts/emergency_halt.py            # SIGUSR1 signal halt
venv/bin/python scripts/emergency_halt.py --db-only  # DB flag (if process hangs)
venv/bin/python scripts/emergency_halt.py --status
venv/bin/python scripts/emergency_halt.py --resume
```

## Architecture

### Data flow
```
Exchange WS → BinanceConnector / OKXConnector
                  ↓  (put events into asyncio.Queue)
            TradingEngine._event_loop()
                  ↓  (fan-out to strategies + quant modules)
            BaseStrategy.on_ticker() / on_order_update() / ...
                  ↓  (emit Signal)
            TradingEngine._execute_signal()
                  ↓  (RiskManager.check_signal → gate)
            Connector.place_order()
                  ↓  (OrderUpdateEvent back into queue)
            RiskManager.record_order_update() → PnL / TCA
```

All events (`TickerEvent`, `OrderUpdateEvent`, `PositionUpdateEvent`, etc.) are typed dataclasses defined in `core/types.py`. Every module that emits or consumes events must use these types.

### Key modules

| Path | Role |
|------|------|
| `main.py` | Entry point — wires all modules, starts uvicorn + engine concurrently |
| `core/engine.py` | Central hub: connector registry, strategy dispatch, event loop |
| `core/types.py` | All shared dataclasses and enums (Exchange, Order, Signal, events) |
| `config/manager.py` | Loads `config.yaml`, merges `.env` env vars, returns typed `AppConfig` |
| `connectors/base.py` | `BaseConnector` ABC + `symbol_to_exchange()` / `symbol_from_exchange()` helpers |
| `connectors/binance.py` / `okx.py` | Exchange connectors (REST + public WS + private WS) |
| `risk/manager.py` | `RiskManager` — all order gates, daily/rolling PnL, drawdown halt |
| `data/storage.py` | aiosqlite wrapper — all DB reads/writes go through here |
| `api/main.py` | FastAPI app + WebSocket manager; ~2350 lines, one file |
| `strategies/base.py` | `BaseStrategy` ABC — `on_ticker`/`on_order_update`/… handlers; `_now()`, `_is_backtest()`, regime helpers |

### Strategy pattern
Strategies inherit `BaseStrategy` and implement event handlers (`on_ticker`, `on_order_update`, …). There are **two ways to trade**, both routed through `TradingEngine._execute_signal` → `RiskManager.check_signal` → connector (never call a connector directly):
- **Return** a `list[Signal]` from `on_ticker`/`on_orderbook` — the engine executes each (simple strategies).
- **`await self.engine.place_order(...)`** directly — for multi-leg / async flows that need the returned `Order` immediately (grid, spread_arb, funding_rate, cash_carry). `on_order_update` returns nothing; it reacts to fills (e.g. grid re-quotes, arb leg resolution).

There is no `emit_signal()` method. Use `self._now()` (not `time.time()`) for cooldowns/timeouts so the strategy paces correctly under backtest replay, and `self._is_backtest()` to disable wall-clock-only safety timers.

**Custom strategies** live in `strategies/custom/*.py` (loaded by `strategies/loader.py`, class name → snake_case id). Author them either by dropping a file there + `POST /api/strategies/reload`, or in the UI: Strategies page → "Write Strategy" opens an in-browser editor (Edit on a custom card loads its source). Endpoints share one validator (`api/main._validate_strategy_source`, dry-run import requiring a BaseStrategy subclass): `GET …/custom/{file}/source`, `POST …/custom/validate` (lint, no save), `POST …/custom/save` (write), `POST …/custom/upload` (file). Save does not hot-load — call `/strategies/reload` after (the editor's "Save & Reload" does both).

Access regime/position-sizing helpers via:
- `self.regime_pos_mult(symbol)` → float multiplier (1.0 if unavailable)
- `self.regime_threshold_mult(symbol)` → float multiplier

### Config & secrets
- `config.yaml` — structure/defaults only, **no secrets**
- `.env` — API keys (loaded automatically by `config/manager.py` via `os.environ.setdefault`)
- All exchange secrets accessed as: `BINANCE_API_KEY`, `BINANCE_SECRET`, `OKX_API_KEY`, `OKX_SECRET`, `OKX_PASSPHRASE`, `OKX_SPOT_*`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TRADING_API_KEY`
- `.env` is in `.gitignore`

### WebSocket (frontend ↔ backend)
`/ws` (no `/api` prefix) streams live events. `api/main.py` has a `ConnectionManager` that broadcasts serialized events to all connected clients. Event types: `ticker`, `order_update`, `position_update`, `balance_update`, `risk_update`, `connector_ready`, `connector_error`, `engine_state`, `alert_triggered`, `regime_update`, `health_update`.

`health_update` is pushed by `HealthMonitor` on every status *transition* (not periodic) — its
`data` is the full health report; App.jsx toasts on degraded/critical and on recovery. It is the
one WS event not emitted via the engine event listener (the monitor calls `ws_manager.broadcast`
directly, wired in `main.py` via `health_monitor.set_broadcast`).

Regime updates have a 60s per-symbol cooldown (`_REGIME_BROADCAST_COOLDOWN`) to avoid flooding on startup. The frontend also enforces a 60s grace period after WS connect before showing regime toasts.

### Quant modules (plugged into engine after construction)
All optional — engine works without them. Set on `engine.*` attributes in `main.py`:
- `engine.position_sizer` → `risk/position_sizer.py` (Kelly / vol-normalized sizing)
- `engine.portfolio_risk` → `risk/portfolio.py` (VaR, CVaR, correlation)
- `engine.regime_detector` → `signals/regime.py` (LOW/NORMAL/HIGH/EXTREME via realized vol)
- `engine.microstructure` → `signals/microstructure.py` (OBI, order book imbalance)
- `engine.attributor` → `risk/attribution.py` (PnL attribution by source)
- `engine.tca` → `risk/tca.py` (slippage, maker rate, execution score)

### Database tables (SQLite)
`ohlcv`, `ticks`, `trades`, `equity_snapshots`, `strategy_pnl`, `logs`, `funding_rates`, `pnl_attribution`. All writes async via `data/storage.py`. Daily purge: ticks > 7d, logs > 30d, attribution > 90d.

### Frontend
React 18 + Vite. All state lives in `App.jsx` (no Redux). Pages receive data via props from App. `i18n.jsx` handles EN/ZH strings (params are **functions**, e.g. `n => \`${n} running\``, not `{0}` placeholders). In production, FastAPI serves `frontend/dist/` as static files at `/`.

Pages: Dashboard, Markets, Positions, Orders, Trades, Strategies, Backtest, Stats, Risk, System, Dex.

**Design system:** all visual tokens are CSS variables in `index.css` (`--accent`/`--green`/`--t1..t4`/`--r-*` radius/`--shadow-*`) with a full class layer (`.btn`+variants, `.badge`+variants, `.card`/`.card-header`, `.metric`/`.label`, `.empty-state`, `.toggle`). `components/ui.jsx` wraps these as the canonical primitives — **prefer `<Button variant size>`, `<Badge>`, `<StatTile>`, `<PageHeader>`, `<Card>`/`<CardHeader>`, `<EmptyState>`, `<Loading>` over hand-rolled inline styles** so a token change updates everything. Button variants: primary/ghost/green/red/yellow/purple/buy/sell; sizes sm/xs. Migration to these is in progress (StrategiesPage + Markets funding screener done; Backtest/Risk/Orders/etc. still carry legacy inline styles).

### Connector hardening (production order placement)
`connectors/base.py` holds the shared machinery; both connectors wire it in `connect()`:
- **Precision**: `_load_symbol_rules()` fetches exchangeInfo/instruments into `self._rules` (`SymbolRule`). `_quantize_order()` snaps qty→stepSize / price→tickSize (buy floors, sell ceils) and rejects sub-min orders locally.
- **Units**: system-wide quantities are **coin amounts**. OKX swap `sz` is in **contracts** — `OKXConnector` converts coin↔contracts at the boundary via `_ctval(symbol)` (= `ctVal`); all parsed orders/positions are multiplied back to coin.
- **Idempotency**: every order carries a `client_order_id`. The engine generates one per signal (`gen_client_order_id`) and reuses it across **both** retry layers; a duplicate-id error triggers `_get_order_by_client_id` to fetch the already-placed order. Binance re-signs each attempt so the timestamp stays within `recvWindow`.
- **Clock**: `_sync_time()` keeps `_time_offset_ms`; resynced every ~30 min.
- **Rate limit**: `AsyncRateLimiter` token bucket (Binance 20/s, OKX 15/s) gates every REST call.

### Engine safety knobs (`EngineConfig` in config.yaml `engine:`)
- `max_quote_age_s` (default 10): `_execute_signal` blocks **new entries** on quotes older than this (staleness guard); `reduce_only` exits always pass.
- `order_ttl_s` (default 0 = off): stale-order reaper cancels resting orders older than this, **except** strategies with `keeps_resting_orders = True` (grid, market maker).
- `cancel_orders_on_shutdown` (default true): `engine.stop()` cancels all open orders before disconnecting.
- Fees in non-USDT (e.g. BNB) are converted to USDT in `_normalize_fee` using the cached ticker before flowing to PnL/TCA.

### Order fill poller (REST fallback)
Private WS streams are unavailable (OKX 60011 / Binance listenKey 410 — API key permissions), so fill
confirmations come from `TradingEngine._order_poll_loop`: every `engine.order_poll_interval_s` (default 3s,
0 = off) it RESTs non-terminal orders in `_open_orders` and injects `OrderUpdateEvent`s on state change.
Downstream consumers dedupe fills by order_id (engine `_processed_fill_ids`, risk manager
`_processed_fill_ids`), so WS/REST overlap is safe.

### Strategy observability
- `arb_triggers` table logs every spread-arb attempt (spread, mode, outcome, realized bps, duration).
  API: `GET /api/arb-triggers`, `GET /api/arb-triggers/stats?hours=168`.
- funding_arb `scan_all` mode ranks ALL Binance USDT perps by |funding rate| (24h-volume filtered),
  evaluates top N alongside static symbols. BTC/ETH funding diffs (~0.3bps/8h) can never clear the
  4-leg fee gate — alts are the only viable universe for this strategy.
- funding_arb executes legs as **post-only makers with market fallback** (`maker_legs`, default on):
  fee gate uses `maker_eff_fee_bps` (1.5/leg ≈ 9bps/8h entry gate vs 24bps taker — live scan
  2026-06-11 showed the best cross-exchange diff was 12bps/8h, so taker mode can never trade).
  Entries are promoted to `_open_arbs` only after BOTH legs execute (failed leg 2 → reverse leg 1);
  failed exits keep the arb registered and retry next poll (reduce_only makes retries safe).
  Scanned alts get their ticker feed subscribed at runtime via `engine.ensure_symbol_feed()` —
  without it the engine staleness guard blocks every scan_all entry (no cached quote → age None).

### Backtest engine semantics
- Limit orders fill only when a candle crosses the limit price (open-cross fills at open, intra-candle
  at the limit). Market orders fill at next candle open. Orders placed by strategy callbacks during fill
  processing are deferred to the next candle (prevents same-candle cascade loops).
- Single net position per exchange — layered grid inventory is NOT modeled; grid backtests understate
  trade counts and are only useful for relative comparison of parameter sets.
- Fees are maker/taker aware: a limit order that rests and fills at its price pays `maker_fee_bps`
  (default 2.0, no slippage); fills at candle open and all market orders pay `taker_fee_bps` (default 4.0).
  Override both per-job via the `/api/backtest/run` body. Grid/spread/funding strategies are maker-based,
  so omitting these now models their real cost instead of overcharging taker on every leg.

### Parity backtest path (backtest/live unification)
`backtest/runner.py` `BacktestRunner` is the redesigned path: it drives a **real** `TradingEngine`
(`engine.drain_events()` + `_process_event`, the same per-event handler the live loop uses) with the
**real** strategy and **real** `RiskManager` over `connectors/sim.py` `SimulatedConnector`. A strategy
therefore runs the identical `_process_event → on_ticker/on_order_update → place_order → _execute_signal`
code path in backtest and in production — fixing the legacy simulator's two divergences: layered grid
inventory IS modelled (the sim connector nets a position with average entry, like Binance/OKX one-way
mode, so many resting levels accumulate) and the risk gates actually run. Fill semantics (maker/taker,
slippage-on-taker-only, fee-once, reduce_only-never-flips) are ported verbatim from the legacy engine.
Per-bar loop is no-look-ahead: `sim.settle_bar` (fill orders resting from earlier bars) → `sim.emit_ticker`
→ `engine.drain_events` (strategy reacts) → drain create_task'd work (grid's async setup) →
`sim.promote_pending` (orders placed this bar rest for the next). `max_quote_age_s` is forced to 0 in
the runner (sim clock ≠ wall clock) and risk defaults to `permissive_risk()` (pass a real `RiskConfig`
to gate). Wiring: `BacktestEngine(storage, parity=...)` switches jobs to `_simulate_parity` with an
**identical** `BacktestMetrics.to_dict()` output, so the API/frontend contract is unchanged.
Gated by `config.yaml backtest.parity` (now `true`; `main.py` passes it in). Validated on real data
via `scripts/compare_backtest_parity.py` (buy-and-hold matched legacy bit-for-bit; grid reported
74 trades vs legacy's 7 — legacy understated layered fills/fees ~8×). Tests:
`tests/test_backtest_parity.py`.

Backtest mode flag: `BacktestRunner` sets `engine.is_backtest = True`. Strategies disable wall-clock
safety timers in replay via `BaseStrategy._is_backtest()` — currently the grid's runaway-fill-cascade
guard (a live-connector protection that false-fires when many distinct levels fill in the same
wall-clock instant under fast replay).

Engine clock: `engine.now()` returns wall-clock live, or `engine._sim_time` (set per bar by the runner)
in backtest. Strategies pace cooldowns/timeouts off `BaseStrategy._now()` (falls back to wall-clock if a
test stub lacks `now()`); without this, spread_arb's 30s cooldown would block every arb after the first
(a full replay runs in ms). spread_arb's `time.time()` cadence calls were moved to `self._now()`.

Cross-exchange backtest: `BacktestRunner.run_multi(strategy, streams={Exchange: candles, ...}, symbol)`
runs one `SimulatedConnector` per exchange over aligned OHLCV streams (single-exchange `run()` delegates
to it). Capital splits evenly; the equity curve sums every connector so cross-exchange legs net out.
Fidelity caveat: OHLCV has no order book, so bid/ask is a synthetic fixed `half_spread_bps` around the
bar open and microstructure depth checks are off unless injected — a spread-arb backtest is a
logic/cadence test and a coarse opportunity scan, not a precise PnL forecast. On real binance-vs-okx
BTC-USDT 1h, realistic-fee spread_arb executes 0 arbs (corroborating the structural-no-opportunity
finding); dropping fees/threshold to 0 confirms the detector fires and decays monotonically with the
threshold. Tests: `tests/test_backtest_arb.py`.

### Funding-harvest screener
`backtest/funding_harvest.py` mines the stored `funding_rates` table to rank symbols by how
profitable a delta-neutral funding harvest *would have been* (hold the perp on the funding-receiving
side, hedged). It does NOT backtest the live funding_arb strategy (which HTTP-polls exchanges and uses
real-time blocking maker loops — not replayable); it answers the strategy's economic question from
history instead. `analyze()`/`rank()` are pure; `FundingHarvestAnalyzer(storage).scan(...)` groups the
table by symbol. Polled rows are first deduped to **real settlements** via `next_funding_time` (the last
poll before each settlement wins; 8h time-bucket fallback) so oversampling a slow rate can't bias the
mean — `n_periods` is settlements, not polls. The funding **cadence is derived** from the median gap
between settlements (`settlements_per_year`; handles 1h/4h/8h funding — a 2h-funding alt annualizes 4×
an 8h one), not hardcoded. Key field `favorable_pct` (how often funding stayed on the committed side) is
the risk discriminator: high `net_annual_pct` + low `favorable_pct` = a rate that flipped a lot (trap).
`min_span_days` (default 7) + `min_periods` drop short-history noise; `min_favorable_pct` keeps only
persistent funding. Gross annualizes the MEAN rate; fee drag amortizes one delta-neutral round trip
(2 legs × in+out) over the window span. API: `GET /api/funding-harvest`
(exchange/days/fee_bps_per_leg/min_periods/min_span_days/min_favorable_pct/top_n). Frontend: Markets page
"Funding Harvest Screener" table (shows net%, favorable%, avg|rate|+cadence, side). Data reality: the DB
has ~799 binance symbols of funding history but only 2 on OKX, so cross-exchange funding arb is not
backtestable — the opportunity is single-exchange harvest on high-funding alts (e.g. HOME ~950% net ann
99% favorable, STG ~300% 100% favorable over 21d). Tests: `tests/test_funding_harvest.py`.

### Funding-harvest backtest (basis-aware)
`backtest/funding_harvest_sim.py` goes beyond the screener estimate: `simulate()` replays a real
delta-neutral harvest (short perp + long spot when funding is net positive, or the mirror) over aligned
perp+spot OHLCV plus the funding settlements, and reports **funding carry, basis PnL and fees
separately** — `net = funding + basis − fees`. Basis PnL (the price drift of the hedged pair) is the
risk the screener can't see; isolating it tells genuine carry from a basis that drifted your way.
`HarvestBacktestRunner(storage).run(symbol, days, ...)` loads the symbol's funding from the DB and
fetches its Binance perp (FAPI) + spot (API) klines for that window (`asyncio.to_thread`, certifi SSL);
perp-only alts (no spot to hedge) raise ValueError → API 422. Pure `simulate()` is unit-tested
(`tests/test_funding_harvest_sim.py`); the fetch path is network and exercised by
`scripts/harvest_backtest.py`. API: `GET /api/funding-harvest/backtest?symbol=&days=&fee_bps_per_leg=`.
Frontend: clicking a row in the Markets "Funding Harvest Screener" runs the backtest and expands a
funding/basis/fees/net/APR breakdown. Real run (binance, ~22d): STG +27.9% net (456% APR, $2773 funding,
basis +$23 — clean carry), HOME +26% (basis −$11), ID +10.8% but basis +$168 (luck, not carry); many
alts (SIREN/BTW/CLO) are perp-only and can't be spot-hedged.

### Operational health monitor
`monitoring/health.py` `HealthMonitor` runs as a background heartbeat (wired in `main.py`,
registered with the API via `set_quant_services`). It consolidates the signals that decide
whether the system can trade *right now* into one status (`ok`/`degraded`/`critical`, or
`paused`): market-feed freshness (freshest ticker age per connected exchange, from
`engine._last_ticker`), event-loop lag (heartbeat sleep overshoot), event-queue saturation
(`engine.event_queue` size/maxsize) and connector connectivity. On a status *transition* it
fires a throttled Telegram alert via `engine._notifier` (5-min cooldown, plus a recovery
notice). `evaluate()` is pure (explicit snapshot in → report out) so it is unit-tested without
an engine (`tests/test_health.py`). Feed thresholds default to `engine.max_quote_age_s`
(warn) and ×3 (crit), so health flags a stale feed around the point the per-order staleness
guard starts silently blocking entries. API: `GET /api/health/detail` (distinct from `/health`,
the minimal DB+engine readiness probe). Frontend: System page "System Health" card (polls 3s)
plus an instant `health_update` WS toast on transition (see WebSocket section).

**Self-healing** (`auto_heal_feeds`, config.yaml `engine:`, default on): when an exchange the
connector still reports as `connected` has had a *frozen* market-data feed (critical) for
`heal_after_crit_s` (30s), the monitor reconnects that connector (`disconnect_exchange` +
`connect_exchange`), rate-limited per exchange by `heal_cooldown_s` (120s), and Telegram-notifies.
This targets only the silent-WS-death case (connected but no ticks); it never touches a
manually-disconnected or errored connector, and reconnect is non-destructive (no order actions).
The decision logic (`_heal_targets`) is pure and unit-tested; per-exchange feed status is exposed
in the feeds component's `metrics.critical` / `metrics.degraded`.

### Known limitations
- OKX private WS returns error 60011 (auth failed) — this is an API key permissions issue, **does not affect market data**. One warning is logged per connection attempt, then silenced via `_priv_auth_warned` flag.
- Binance listen key returns 410 (no user data stream access) — same pattern, market data unaffected.
- Process name on macOS: the venv Python resolves to `/Library/Frameworks/Python.framework/.../Python`, so `pkill -f "main.py"` is required (not `venv/bin/python main.py`).
