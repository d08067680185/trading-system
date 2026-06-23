from __future__ import annotations
import json
import os
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Optional

import yaml
from fastapi import FastAPI, APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from core.types import (
    Exchange, OrderSide, OrderType,
    TickerEvent, OrderUpdateEvent, PositionUpdateEvent,
    BalanceUpdateEvent, ConnectorReadyEvent, ConnectorErrorEvent,
)
from core.engine import TradingEngine

app = FastAPI(title="Trading System", version="1.0.0")

# CORS — restrict to configured origins (defaults to same-origin only in production)
_cors_origins = os.environ.get("CORS_ORIGINS", "").split(",")
_cors_origins = [o.strip() for o in _cors_origins if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins if _cors_origins else ["http://localhost:5173", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-API-Key"],
)

router = APIRouter(prefix="/api")
_engine: Optional[TradingEngine] = None
_api_key: str = ""   # empty = no auth

# In-memory alert rules: id -> rule dict
_alert_rules: dict[str, dict] = {}

# Rate limiting: ip -> list of request timestamps
_rate_limits: dict[str, list] = {}
_RATE_WINDOW = 60   # seconds
_RATE_MAX    = 120  # requests per window


def set_api_key(key: str) -> None:
    global _api_key
    _api_key = key


@app.get("/health")
async def health_check():
    """Readiness probe — checks DB and engine; no auth required."""
    t0 = time.time()
    checks: dict[str, object] = {}

    # Database check
    try:
        storage = _data_storage
        if storage and storage._db:
            async with storage._db.execute("SELECT 1") as cur:
                await cur.fetchone()
            checks["db"] = "ok"
        else:
            checks["db"] = "not_initialized"
    except Exception as e:
        checks["db"] = f"error: {e}"

    # Engine / connectors
    eng = _engine
    if eng:
        checks["engine"] = "active" if eng.is_active else "paused"
        checks["connectors"] = dict(eng._connector_states)
        checks["strategy_count"] = len(eng.strategies)
    else:
        checks["engine"] = "not_initialized"

    overall = "ok" if checks.get("db") == "ok" else "degraded"
    return {"status": overall, "ts": t0, "latency_ms": round((time.time() - t0) * 1000, 1), **checks}


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    """Per-IP rate limiting for /api routes."""
    if request.url.path.startswith("/api"):
        ip = request.client.host if request.client else "unknown"
        now = time.time()
        cutoff = now - _RATE_WINDOW
        times = [t for t in _rate_limits.get(ip, []) if t > cutoff]
        if len(times) >= _RATE_MAX:
            return JSONResponse({"detail": "Rate limit exceeded. Try again later."}, status_code=429)
        times.append(now)
        _rate_limits[ip] = times
        # Prune stale IPs periodically
        if len(_rate_limits) > 5000:
            stale_cutoff = now - _RATE_WINDOW * 2
            _rate_limits.clear()  # simple reset; avoids O(n) per-request scanning
    return await call_next(request)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Reject requests missing a valid X-API-Key header when auth is configured."""
    if _api_key and request.url.path.startswith("/api"):
        provided = request.headers.get("X-API-Key", "")
        if provided != _api_key:
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    return await call_next(request)


# ── WebSocket connection manager ──────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self._clients: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self._clients:
            self._clients.remove(ws)

    async def broadcast(self, msg: dict) -> None:
        dead = []
        for ws in self._clients:
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


ws_manager = ConnectionManager()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    if _api_key:
        provided = websocket.query_params.get("key", "") or websocket.headers.get("X-API-Key", "")
        if provided != _api_key:
            await websocket.close(code=4001)
            return
    await ws_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()   # keep-alive / ping
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)


# ── Event serializer (called by engine event listener) ────────────────────────

def serialize_event(event) -> Optional[dict]:
    if isinstance(event, TickerEvent):
        t = event.ticker
        return {"type": "ticker", "data": {
            "exchange": t.exchange.value,
            "symbol": t.symbol,
            "bid": float(t.bid),
            "ask": float(t.ask),
            "last": float(t.last),
            "spread_bps": float(t.spread_bps),
            "ts": t.timestamp,
        }}
    if isinstance(event, OrderUpdateEvent):
        o = event.order
        return {"type": "order_update", "data": {
            "exchange": o.exchange.value,
            "symbol": o.symbol,
            "order_id": o.order_id,
            "side": o.side.value,
            "order_type": o.order_type.value,
            "quantity": float(o.quantity),
            "price": float(o.price) if o.price else None,
            "filled_qty": float(o.filled_qty),
            "avg_price": float(o.avg_price),
            "status": o.status.value,
            "fee": float(o.fee),
        }}
    if isinstance(event, PositionUpdateEvent):
        p = event.position
        return {"type": "position_update", "data": {
            "exchange": p.exchange.value,
            "symbol": p.symbol,
            "side": p.side.value,
            "size": float(p.size),
            "entry_price": float(p.entry_price),
            "mark_price": float(p.mark_price),
            "leverage": p.leverage,
            "unrealized_pnl": float(p.unrealized_pnl),
            "notional": float(p.notional),
            "liquidation_price": float(p.liquidation_price),
        }}
    if isinstance(event, BalanceUpdateEvent):
        b = event.balance
        return {"type": "balance_update", "data": {
            "exchange": b.exchange.value,
            "asset": b.asset,
            "free": float(b.free),
            "locked": float(b.locked),
            "total": float(b.total),
        }}
    if isinstance(event, ConnectorReadyEvent):
        return {"type": "connector_ready", "data": {"exchange": event.exchange.value}}
    if isinstance(event, ConnectorErrorEvent):
        return {"type": "connector_error", "data": {"exchange": event.exchange.value, "error": event.error}}
    return None


# ── Engine helpers ────────────────────────────────────────────────────────────

_last_regimes: dict[str, str] = {}        # symbol → last broadcast regime
_last_regime_ts: dict[str, float] = {}   # symbol → timestamp of last broadcast


def set_engine(engine: TradingEngine) -> None:
    global _engine
    _engine = engine

    async def _broadcast(event):
        msg = serialize_event(event)
        if msg:
            await ws_manager.broadcast(msg)
            if msg["type"] == "ticker":
                # Price alert checks
                if _alert_rules:
                    await _check_price_alerts(msg["data"], engine)
                # Regime change broadcast
                if engine.regime_detector:
                    _broadcast_regime_if_changed(msg["data"]["symbol"], engine)
                # Check extended (non-price) alerts every 30th ticker to avoid overhead
                if _alert_rules and not hasattr(_broadcast, '_ext_alert_counter'):
                    _broadcast._ext_alert_counter = 0
                _broadcast._ext_alert_counter = getattr(_broadcast, '_ext_alert_counter', 0) + 1
                if _alert_rules and _broadcast._ext_alert_counter % 30 == 0:
                    import asyncio as _asyncio
                    _asyncio.create_task(_check_extended_alerts(engine))

    engine.add_event_listener(_broadcast)


async def _check_price_alerts(ticker_data: dict, engine: TradingEngine) -> None:
    """Fire any matching alert rules against the latest ticker price."""
    import asyncio
    symbol   = ticker_data.get("symbol", "")
    exchange = ticker_data.get("exchange", "")
    price    = ticker_data.get("last", 0)
    if not price:
        return
    notifier = getattr(engine, "_notifier", None)
    for rule_id, rule in list(_alert_rules.items()):
        if rule.get("triggered"):
            continue
        if rule.get("symbol") != symbol or rule.get("exchange") != exchange:
            continue
        threshold = rule.get("threshold", 0)
        rule_type = rule.get("type", "")
        triggered = (
            (rule_type == "price_above" and price >= threshold) or
            (rule_type == "price_below" and price <= threshold)
        )
        if triggered:
            rule["triggered"] = True
            rule["triggered_at"] = time.time()
            rule["triggered_price"] = price
            alert_msg = rule.get("message") or f"{rule_type}: {threshold}"
            if notifier:
                asyncio.create_task(notifier.alert_price(symbol, exchange, price, alert_msg))
            # Push WS notification to all connected clients
            asyncio.create_task(ws_manager.broadcast({
                "type": "alert_triggered",
                "data": {
                    "id": rule_id,
                    "symbol": symbol,
                    "exchange": exchange,
                    "price": price,
                    "threshold": threshold,
                    "alert_type": rule_type,
                    "message": alert_msg,
                    "ts": time.time(),
                },
            }))


async def _check_extended_alerts(engine: TradingEngine) -> None:
    """Check non-price alert rules: funding rate, strategy loss, position timeout."""
    import asyncio
    for rule_id, rule in list(_alert_rules.items()):
        if rule.get("triggered"):
            continue
        rule_type = rule.get("type", "")
        threshold = rule.get("threshold", 0)
        triggered = False
        trigger_val = 0.0

        if rule_type == "funding_above":
            # Check if any symbol's funding rate exceeds threshold
            sym = rule.get("symbol")
            if engine.regime_detector:
                pass  # funding rates come from strategy data
            # Get from live collector data
            for strat in engine.strategies:
                rates = getattr(strat, '_latest_rates', {})
                for s, rate_info in rates.items():
                    if sym and s != sym:
                        continue
                    rate = rate_info.get('rate_ann_bps', 0) if isinstance(rate_info, dict) else 0
                    if abs(rate) >= threshold:
                        triggered = True
                        trigger_val = rate
                        break

        elif rule_type == "strategy_loss_above":
            sid = rule.get("strategy_id")
            for strat in engine.strategies:
                if sid and strat.strategy_id != sid:
                    continue
                loss = -strat._realized_pnl  # positive = loss
                if loss >= threshold:
                    triggered = True
                    trigger_val = loss
                    break

        elif rule_type == "position_timeout_h":
            # Check if any position has been held longer than threshold hours
            for strat in engine.strategies:
                open_positions = getattr(strat, '_open_positions', {})
                for pos_key, pos_info in open_positions.items():
                    entry_ts = pos_info.get('entry_ts', time.time()) if isinstance(pos_info, dict) else time.time()
                    held_h = (time.time() - entry_ts) / 3600
                    if held_h >= threshold:
                        triggered = True
                        trigger_val = held_h
                        break

        if triggered:
            rule["triggered"] = True
            rule["triggered_at"] = time.time()
            rule["triggered_price"] = trigger_val
            alert_msg = rule.get("message") or f"{rule_type}: {trigger_val:.2f} (threshold {threshold})"
            notifier = getattr(engine, "_notifier", None)
            if notifier:
                asyncio.create_task(notifier.send(f"⚠️ Alert: {alert_msg}", tag=f"alert_{rule_id}"))
            asyncio.create_task(ws_manager.broadcast({
                "type": "alert_triggered",
                "data": {
                    "id": rule_id, "alert_type": rule_type,
                    "value": trigger_val, "threshold": threshold,
                    "message": alert_msg, "ts": time.time(),
                },
            }))


_REGIME_BROADCAST_COOLDOWN = 60.0   # seconds between broadcasts per symbol


def _broadcast_regime_if_changed(symbol: str, engine: TradingEngine) -> None:
    """Broadcast a regime_update WS event when the market regime changes (10s cooldown)."""
    import asyncio
    snap = engine.regime_detector.get(symbol)
    if snap is None:
        return
    prev = _last_regimes.get(symbol)
    if prev == snap.regime:
        return
    now = time.time()
    if now - _last_regime_ts.get(symbol, 0) < _REGIME_BROADCAST_COOLDOWN:
        # Update state silently (don't broadcast yet)
        _last_regimes[symbol] = snap.regime
        return
    _last_regimes[symbol] = snap.regime
    _last_regime_ts[symbol] = now
    asyncio.create_task(ws_manager.broadcast({
            "type": "regime_update",
            "data": {
                "symbol": symbol,
                "regime": snap.regime,
                "prev_regime": prev,
                "realized_vol_ann": snap.realized_vol_ann,
                "vol_percentile": snap.vol_percentile,
                "pos_size_mult": snap.pos_size_mult,
                "threshold_mult": snap.threshold_mult,
                "ts": time.time(),
            },
        }))


def get_engine() -> TradingEngine:
    if _engine is None:
        raise RuntimeError("Engine not initialized")
    return _engine


# ── Request models ────────────────────────────────────────────────────────────

class OrderRequest(BaseModel):
    exchange: str
    symbol: str
    side: str
    order_type: str
    quantity: float
    price: Optional[float] = None
    reduce_only: bool = False


class ParamUpdate(BaseModel):
    params: dict


class RiskUpdate(BaseModel):
    max_position_usdt: Optional[float] = None
    max_order_usdt: Optional[float] = None
    max_daily_loss_usdt: Optional[float] = None
    max_open_orders: Optional[int] = None
    enabled: Optional[bool] = None
    max_drawdown_pct: Optional[float] = None
    max_symbol_concentration_pct: Optional[float] = None


class EngineUpdate(BaseModel):
    symbols: Optional[list[str]] = None


class ExchangeConfigUpdate(BaseModel):
    api_key: Optional[str] = None
    secret: Optional[str] = None
    passphrase: Optional[str] = None   # OKX only
    market_type: Optional[str] = None  # futures/spot/swap
    testnet: Optional[bool] = None


# ── API Routes ────────────────────────────────────────────────────────────────

@router.get("/status")
async def get_status():
    return get_engine().status()


# ── System control ────────────────────────────────────────────────────────────

@router.get("/system/status")
async def system_status():
    """Full system health: uptime, connector states, strategy count."""
    return get_engine().get_system_status()


@router.post("/engine/pause")
async def engine_pause():
    """Disconnect all connectors but keep the server running."""
    eng = get_engine()
    if not eng._active:
        return {"ok": True, "state": "already_paused"}
    await eng.pause()
    await ws_manager.broadcast({"type": "engine_state", "data": {"active": False}})
    return {"ok": True, "state": "paused"}


@router.post("/engine/resume")
async def engine_resume():
    """Reconnect all connectors and resume trading."""
    eng = get_engine()
    if eng._active:
        return {"ok": True, "state": "already_active"}
    await eng.resume()
    await ws_manager.broadcast({"type": "engine_state", "data": {"active": True}})
    return {"ok": True, "state": "active"}


@router.post("/connectors/{exchange}/connect")
async def connector_connect(exchange: str):
    """Connect (or reconnect) a single exchange."""
    eng = get_engine()
    try:
        ex = Exchange(exchange)
        if ex not in eng.connectors:
            raise HTTPException(400, f"Exchange '{exchange}' is not registered in the engine")
        await eng.connect_exchange(ex)
        await ws_manager.broadcast({"type": "connector_ready", "data": {"exchange": exchange}})
        return {"ok": True, "exchange": exchange, "state": "connected"}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/connectors/{exchange}/disconnect")
async def connector_disconnect(exchange: str):
    """Disconnect a single exchange."""
    eng = get_engine()
    try:
        ex = Exchange(exchange)
        if ex not in eng.connectors:
            raise HTTPException(400, f"Exchange '{exchange}' is not registered in the engine")
        await eng.disconnect_exchange(ex)
        await ws_manager.broadcast({"type": "connector_error", "data": {"exchange": exchange, "error": "manually disconnected"}})
        return {"ok": True, "exchange": exchange, "state": "disconnected"}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/system/restart")
async def system_restart():
    """Gracefully restart the trading system process."""
    import asyncio, os, sys
    async def _do_restart():
        await asyncio.sleep(0.5)
        os.execv(sys.executable, [sys.executable] + sys.argv)
    asyncio.create_task(_do_restart())
    return {"ok": True, "message": "Restarting in 500ms…"}


@router.get("/positions")
async def get_positions(exchange: Optional[str] = None):
    try:
        ex = Exchange(exchange) if exchange else None
    except ValueError:
        raise HTTPException(400, f"Unknown exchange: {exchange}")
    positions = await get_engine().get_positions(ex)
    return [
        {
            "exchange": p.exchange.value,
            "symbol": p.symbol,
            "side": p.side.value,
            "size": float(p.size),
            "entry_price": float(p.entry_price),
            "mark_price": float(p.mark_price),
            "unrealized_pnl": float(p.unrealized_pnl),
            "notional": float(p.notional),
            "leverage": p.leverage,
            "liquidation_price": float(p.liquidation_price),
        }
        for p in positions
    ]


@router.post("/positions/close")
async def close_position(exchange: str, symbol: str, size: Optional[float] = None):
    """Market-close a position (or partial if size given). reduce_only=True."""
    from decimal import Decimal as D
    from core.types import Exchange as Ex, OrderSide, OrderType, PositionSide
    eng = get_engine()
    positions = await eng.get_positions()
    pos = next((p for p in positions if p.exchange.value == exchange and p.symbol == symbol), None)
    if not pos:
        raise HTTPException(404, f"No open position: {exchange}/{symbol}")
    qty = D(str(size)) if size else pos.size
    side = OrderSide.SELL if pos.side == PositionSide.LONG else OrderSide.BUY
    connector = eng.connectors.get(Ex(exchange))
    if not connector:
        raise HTTPException(404, f"Connector not found: {exchange}")
    order = await connector.place_order(
        exchange=Ex(exchange), symbol=symbol, side=side,
        order_type=OrderType.MARKET, quantity=qty,
        reduce_only=True, strategy_id="manual",
    )
    return {"closed": True, "order_id": order.order_id if order else None, "qty": float(qty)}


@router.post("/positions/close-all")
async def close_all_positions():
    """Market-close every open position."""
    from decimal import Decimal as D
    from core.types import Exchange as Ex, OrderSide, OrderType, PositionSide
    eng = get_engine()
    positions = await eng.get_positions()
    results = []
    for pos in positions:
        try:
            side = OrderSide.SELL if pos.side == PositionSide.LONG else OrderSide.BUY
            connector = eng.connectors.get(pos.exchange)
            if not connector:
                results.append({"exchange": pos.exchange.value, "symbol": pos.symbol, "ok": False, "error": "no connector"})
                continue
            order = await connector.place_order(
                exchange=pos.exchange, symbol=pos.symbol, side=side,
                order_type=OrderType.MARKET, quantity=pos.size,
                reduce_only=True, strategy_id="manual",
            )
            results.append({"exchange": pos.exchange.value, "symbol": pos.symbol, "ok": True, "order_id": order.order_id if order else None})
        except Exception as e:
            results.append({"exchange": pos.exchange.value, "symbol": pos.symbol, "ok": False, "error": str(e)})
    return {"closed": len([r for r in results if r["ok"]]), "results": results}


@router.delete("/orders/all")
async def cancel_all_orders(exchange: Optional[str] = None):
    """Cancel all open orders, optionally filtered by exchange."""
    eng = get_engine()
    orders = await eng.get_orders()
    if exchange:
        orders = [o for o in orders if o.exchange.value == exchange]
    results = []
    for order in orders:
        try:
            connector = eng.connectors.get(order.exchange)
            if connector:
                ok = await connector.cancel_order(order.exchange, order.symbol, order.order_id)
                results.append({"order_id": order.order_id, "ok": ok})
        except Exception as e:
            results.append({"order_id": order.order_id, "ok": False, "error": str(e)})
    return {"cancelled": len([r for r in results if r["ok"]]), "results": results}


@router.get("/balances")
async def get_balances(exchange: Optional[str] = None):
    try:
        ex = Exchange(exchange) if exchange else None
    except ValueError:
        raise HTTPException(400, f"Unknown exchange: {exchange}")
    balances = await get_engine().get_balances(ex)
    return [
        {
            "exchange": b.exchange.value,
            "asset": b.asset,
            "free": float(b.free),
            "locked": float(b.locked),
            "total": float(b.total),
        }
        for b in balances
    ]


@router.get("/orders")
async def get_open_orders(exchange: Optional[str] = None, symbol: Optional[str] = None):
    eng = get_engine()
    results = []
    for ex, conn in eng.connectors.items():
        if exchange and ex.value != exchange:
            continue
        orders = await conn.get_open_orders(symbol)
        for o in orders:
            results.append({
                "exchange": o.exchange.value,
                "symbol": o.symbol,
                "order_id": o.order_id,
                "strategy_id": o.strategy_id,
                "side": o.side.value,
                "order_type": o.order_type.value,
                "quantity": float(o.quantity),
                "price": float(o.price) if o.price else None,
                "filled_qty": float(o.filled_qty),
                "avg_price": float(o.avg_price) if o.avg_price else None,
                "status": o.status.value,
                "created_at": o.created_at,
            })
    return results


@router.post("/orders")
async def place_order(req: OrderRequest):
    try:
        order = await get_engine().place_order(
            exchange=Exchange(req.exchange),
            symbol=req.symbol,
            side=OrderSide(req.side),
            order_type=OrderType(req.order_type),
            quantity=Decimal(str(req.quantity)),
            price=Decimal(str(req.price)) if req.price else None,
            reduce_only=req.reduce_only,
        )
        if not order:
            raise HTTPException(400, "Order blocked: check risk settings or connector status")
        return {"order_id": order.order_id, "status": order.status.value}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, str(e))


@router.delete("/orders/{exchange}/{symbol}/{order_id}")
async def cancel_order(exchange: str, symbol: str, order_id: str):
    ok = await get_engine().cancel_order(Exchange(exchange), symbol, order_id)
    return {"cancelled": ok}


@router.get("/risk")
async def get_risk():
    return get_engine().risk_manager.status()


@router.post("/risk/halt")
async def halt_trading(reason: str = "manual"):
    get_engine().risk_manager.halt(reason)
    await ws_manager.broadcast({"type": "risk_update", "data": get_engine().risk_manager.status()})
    return {"halted": True}


@router.post("/risk/resume")
async def resume_trading():
    get_engine().risk_manager.resume()
    await ws_manager.broadcast({"type": "risk_update", "data": get_engine().risk_manager.status()})
    return {"halted": False}


# ── Settings ──────────────────────────────────────────────────────────────────

@router.get("/settings")
async def get_settings():
    eng = get_engine()
    cfg = eng.config
    return {
        "risk": {
            "max_position_usdt": float(cfg.risk.max_position_usdt),
            "max_order_usdt": float(cfg.risk.max_order_usdt),
            "max_daily_loss_usdt": float(cfg.risk.max_daily_loss_usdt),
            "max_open_orders": cfg.risk.max_open_orders,
            "enabled": cfg.risk.enabled,
            "max_drawdown_pct": float(cfg.risk.max_drawdown_pct),
            "max_symbol_concentration_pct": float(cfg.risk.max_symbol_concentration_pct),
        },
        "engine": {
            "symbols": cfg.engine.symbols,
            "orderbook_depth": cfg.engine.orderbook_depth,
        },
        "exchanges": {
            ex.value: {
                "api_key_set": bool(ecfg.api_key),
                "api_key_hint": f"****{ecfg.api_key[-4:]}" if ecfg.api_key else "",
                "market_type": ecfg.market_type.value,
                "testnet": ecfg.testnet,
            }
            for ex, ecfg in cfg.exchanges.items()
        },
    }


@router.post("/settings/risk")
async def update_risk_settings(data: RiskUpdate):
    eng = get_engine()
    risk_cfg = eng.config.risk
    risk_mgr = eng.risk_manager.config
    if data.max_position_usdt is not None:
        risk_cfg.max_position_usdt = risk_mgr.max_position_usdt = Decimal(str(data.max_position_usdt))
    if data.max_order_usdt is not None:
        risk_cfg.max_order_usdt = risk_mgr.max_order_usdt = Decimal(str(data.max_order_usdt))
    if data.max_daily_loss_usdt is not None:
        risk_cfg.max_daily_loss_usdt = risk_mgr.max_daily_loss_usdt = Decimal(str(data.max_daily_loss_usdt))
    if data.max_open_orders is not None:
        risk_cfg.max_open_orders = risk_mgr.max_open_orders = data.max_open_orders
    if data.enabled is not None:
        risk_cfg.enabled = risk_mgr.enabled = data.enabled
    if data.max_drawdown_pct is not None:
        risk_cfg.max_drawdown_pct = risk_mgr.max_drawdown_pct = Decimal(str(data.max_drawdown_pct))
    if data.max_symbol_concentration_pct is not None:
        risk_cfg.max_symbol_concentration_pct = risk_mgr.max_symbol_concentration_pct = Decimal(str(data.max_symbol_concentration_pct))

    # Persist to config.yaml so settings survive restarts
    config_path = Path("config.yaml")
    raw = yaml.safe_load(config_path.read_text())
    r = raw.setdefault("risk", {})
    r["max_position_usdt"]  = float(risk_cfg.max_position_usdt)
    r["max_order_usdt"]     = float(risk_cfg.max_order_usdt)
    r["max_daily_loss_usdt"] = float(risk_cfg.max_daily_loss_usdt)
    r["max_open_orders"]    = risk_cfg.max_open_orders
    r["enabled"]            = risk_cfg.enabled
    r["max_drawdown_pct"]   = float(risk_cfg.max_drawdown_pct)
    r["max_symbol_concentration_pct"] = float(risk_cfg.max_symbol_concentration_pct)
    config_path.write_text(yaml.dump(raw, default_flow_style=False, allow_unicode=True))

    await ws_manager.broadcast({"type": "risk_update", "data": eng.risk_manager.status()})
    return {"updated": True, **{k: v for k, v in data.model_dump().items() if v is not None}}


@router.post("/settings/engine")
async def update_engine_settings(data: EngineUpdate):
    eng = get_engine()
    if data.symbols is not None:
        def _norm(s: str) -> str:
            s = s.upper()
            return s[:-4] + "-USDT" if s.endswith("USDT") and "-" not in s else s
        new_syms = [_norm(s) for s in data.symbols]
        old_syms = set(eng.config.engine.symbols)
        added   = set(new_syms) - old_syms
        removed = old_syms - set(new_syms)
        eng.config.engine.symbols = new_syms
        for conn in eng.connectors.values():
            for sym in added:
                await conn.subscribe_ticker(sym)
                await conn.subscribe_orderbook(sym, depth=eng.config.engine.orderbook_depth)
            for sym in removed:
                try:
                    await conn.unsubscribe_ticker(sym)
                    await conn.unsubscribe_orderbook(sym)
                except Exception:
                    pass  # connector may not implement unsubscribe
    return {"updated": True, "symbols": eng.config.engine.symbols}


@router.post("/settings/symbols")
async def update_symbols(symbols: list[str]):
    """Hot-add symbols to the engine without restart. Subscribes WS feeds."""
    if not symbols:
        raise HTTPException(400, "symbols list cannot be empty")
    eng = get_engine()
    added = []
    for sym in symbols:
        if sym not in eng.config.engine.symbols:
            eng.config.engine.symbols.append(sym)
            # Subscribe on all connectors
            for connector in eng.connectors.values():
                try:
                    await connector.subscribe_ticker(sym)
                    await connector.subscribe_orderbook(sym, depth=20)
                except Exception:
                    pass
            added.append(sym)
    return {"added": added, "all_symbols": eng.config.engine.symbols}


@router.get("/settings/symbols")
async def get_symbols():
    eng = get_engine()
    return {"symbols": eng.config.engine.symbols}


@router.post("/settings/exchange/{exchange_name}")
async def update_exchange_config(exchange_name: str, data: ExchangeConfigUpdate):
    import yaml
    from pathlib import Path
    from core.types import MarketType as MT
    from connectors.binance import BinanceConnector
    from connectors.okx import OKXConnector

    eng = get_engine()
    try:
        ex = Exchange(exchange_name)
    except ValueError:
        raise HTTPException(400, f"Unknown exchange: {exchange_name}")

    cfg = eng.config.exchanges.get(ex)
    if not cfg:
        raise HTTPException(404, f"Exchange {exchange_name} not configured")

    # Update in-memory config (only non-empty values)
    if data.api_key:     cfg.api_key = data.api_key
    if data.secret:      cfg.secret  = data.secret
    if data.passphrase:  cfg.passphrase = data.passphrase
    if data.market_type: cfg.market_type = MT(data.market_type)
    if data.testnet is not None: cfg.testnet = data.testnet

    # Persist to config.yaml
    config_path = Path("config.yaml")
    raw = yaml.safe_load(config_path.read_text())
    ex_raw = raw.setdefault("exchanges", {}).setdefault(exchange_name, {})
    if data.api_key:     ex_raw["api_key"]     = data.api_key
    if data.secret:      ex_raw["secret"]      = data.secret
    if data.passphrase:  ex_raw["passphrase"]  = data.passphrase
    if data.market_type: ex_raw["market_type"] = data.market_type
    if data.testnet is not None: ex_raw["testnet"] = data.testnet
    config_path.write_text(yaml.dump(raw, default_flow_style=False, allow_unicode=True))

    # Disconnect old connector
    old = eng.connectors.get(ex)
    if old:
        await old.disconnect()

    # Create & connect new connector with updated credentials
    if ex == Exchange.BINANCE:
        new_conn = BinanceConnector(cfg.api_key, cfg.secret, cfg.market_type, cfg.testnet)
    else:
        new_conn = OKXConnector(cfg.api_key, cfg.secret, cfg.passphrase, cfg.market_type, cfg.testnet)

    eng.add_connector(ex, new_conn)
    await new_conn.connect()
    for symbol in eng.config.engine.symbols:
        await new_conn.subscribe_ticker(symbol)
        await new_conn.subscribe_orderbook(symbol, depth=eng.config.engine.orderbook_depth)

    await ws_manager.broadcast({"type": "connector_ready", "data": {"exchange": exchange_name}})
    return {"updated": True, "exchange": exchange_name, "reconnected": True}


@router.get("/strategies/summary")
async def get_strategies_summary():
    """Per-strategy cumulative PnL summary from DB (survives restarts)."""
    storage = get_storage()
    return await storage.get_strategy_summary()


@router.get("/strategies/{strategy_id}/pnl-history")
async def get_strategy_pnl_history(strategy_id: str, days: int = 30):
    storage = get_storage()
    return await storage.get_strategy_pnl_history(strategy_id, days)


@router.get("/strategies")
async def list_strategies():
    eng = get_engine()
    return [
        {
            "strategy_id": s.strategy_id,
            "enabled": s._enabled,
            "custom": getattr(s, "_is_custom", False),
            "source_file": getattr(s, "_source_file", None),
            "realized_pnl_usdt": round(getattr(s, "_realized_pnl", 0.0), 4),
            "trade_count": getattr(s, "_trade_count", 0),
            "uptime_h": round((time.time() - getattr(s, "_start_time", time.time())) / 3600, 1),
            **s.get_status(),
        }
        for s in eng.strategies
    ]


# ── Custom strategy management ────────────────────────────────────────────────

def _validate_strategy_source(content: bytes) -> list[str]:
    """Import strategy source in isolation; return the BaseStrategy subclass ids it
    defines (same id scheme as the loader). Raises HTTPException(400) on a
    syntax/import error or if no BaseStrategy subclass is found. Used by the
    upload, save and validate endpoints so all three apply the identical check."""
    import tempfile, os, importlib.util, inspect
    from strategies.base import BaseStrategy
    from strategies.loader import _class_to_id
    with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        spec = importlib.util.spec_from_file_location("_strategy_check", tmp_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        ids = [
            _class_to_id(name)
            for name, obj in inspect.getmembers(mod, inspect.isclass)
            if issubclass(obj, BaseStrategy) and obj is not BaseStrategy
            and obj.__module__ == spec.name
        ]
        if not ids:
            raise HTTPException(400, "Source must define at least one class that inherits BaseStrategy")
        return ids
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, f"Syntax/import error: {exc}")
    finally:
        os.unlink(tmp_path)


def _safe_custom_filename(filename: str) -> str:
    """Validate/normalize a custom-strategy filename; raises HTTPException on a bad
    name (path traversal, private `_` prefix, non-.py)."""
    fn = filename.strip()
    if not fn.endswith(".py"):
        fn += ".py"
    if "/" in fn or "\\" in fn or fn.startswith("_") or fn == ".py":
        raise HTTPException(400, "Invalid filename")
    return fn


class CustomStrategySource(BaseModel):
    filename: str
    content: str


class CustomStrategyValidate(BaseModel):
    content: str


@router.get("/strategies/custom/template")
async def download_template():
    """Download the strategy template file."""
    from strategies.loader import CUSTOM_DIR
    from fastapi.responses import FileResponse
    tpl = CUSTOM_DIR / "_template.py"
    if not tpl.exists():
        raise HTTPException(404, "Template file not found")
    return FileResponse(
        path=str(tpl),
        media_type="text/x-python",
        filename="my_strategy.py",
    )


@router.get("/strategies/custom")
async def list_custom_files():
    """List .py files in strategies/custom/ and any load errors."""
    from strategies.loader import scan, CUSTOM_DIR
    found, errors = scan()
    # Build file → [strategy_ids] map
    file_to_ids: dict[str, list[str]] = {}
    for sid, (_, path) in found.items():
        fname = Path(path).name
        file_to_ids.setdefault(fname, []).append(sid)

    files = []
    for py_file in sorted(CUSTOM_DIR.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        files.append({
            "filename": py_file.name,
            "strategy_ids": file_to_ids.get(py_file.name, []),
            "loaded": py_file.name in file_to_ids,
            "error": errors.get(py_file.name),
        })
    return {"files": files, "errors": errors}


@router.post("/strategies/custom/upload")
async def upload_custom_strategy(file: UploadFile = File(...)):
    """Upload a .py strategy file to strategies/custom/."""
    from strategies.loader import CUSTOM_DIR
    if not file.filename.endswith(".py"):
        raise HTTPException(400, "Only .py files are accepted")
    if "/" in file.filename or "\\" in file.filename:
        raise HTTPException(400, "Invalid filename")
    dest = CUSTOM_DIR / file.filename
    content = await file.read()
    # Dry-run scan: reject syntax errors / files with no BaseStrategy subclass.
    _validate_strategy_source(content)
    dest.write_bytes(content)
    return {"uploaded": file.filename, "path": str(dest)}


@router.get("/strategies/custom/{filename}/source")
async def get_custom_source(filename: str):
    """Read a custom strategy file's source (for editing in the UI)."""
    from strategies.loader import CUSTOM_DIR
    if not filename.endswith(".py") or "/" in filename or "\\" in filename:
        raise HTTPException(400, "Invalid filename")
    p = CUSTOM_DIR / filename
    if not p.exists():
        raise HTTPException(404, f"{filename} not found")
    return {"filename": filename, "content": p.read_text()}


@router.post("/strategies/custom/validate")
async def validate_custom_strategy(req: CustomStrategyValidate):
    """Check source compiles and defines a BaseStrategy subclass — without saving.
    Returns {ok, strategy_ids, error} so the editor can lint before save."""
    try:
        ids = _validate_strategy_source(req.content.encode())
    except HTTPException as e:
        return {"ok": False, "strategy_ids": [], "error": e.detail}
    return {"ok": True, "strategy_ids": ids, "error": None}


@router.post("/strategies/custom/save")
async def save_custom_strategy(req: CustomStrategySource):
    """Write strategy source authored/edited in the UI to strategies/custom/.
    Validated first (raises 400 on failure); call /strategies/reload to load it."""
    from strategies.loader import CUSTOM_DIR
    fn = _safe_custom_filename(req.filename)
    ids = _validate_strategy_source(req.content.encode())   # raises 400 on bad source
    (CUSTOM_DIR / fn).write_text(req.content)
    return {"saved": fn, "strategy_ids": ids}


@router.post("/strategies/reload")
async def reload_custom_strategies():
    """Hot-reload all .py files from strategies/custom/ into the running engine."""
    from strategies.loader import scan
    eng = get_engine()
    found, errors = scan()
    result = eng.reload_custom_strategies(found)
    # Register newly loaded classes in the backtest registry too
    for sid, (cls, _) in found.items():
        register_strategy(sid, cls)
    return {
        "reloaded": result,
        "errors": errors,
        "total_strategies": len(eng.strategies),
    }


@router.delete("/strategies/custom/{filename}")
async def delete_custom_strategy(filename: str):
    """Delete a custom strategy file and remove it from the engine."""
    from strategies.loader import CUSTOM_DIR, _class_to_id, scan
    import inspect
    from strategies.base import BaseStrategy as BS

    if not filename.endswith(".py") or "/" in filename or "\\" in filename:
        raise HTTPException(400, "Invalid filename")
    dest = CUSTOM_DIR / filename
    if not dest.exists():
        raise HTTPException(404, f"{filename} not found")

    # Find strategy_ids defined in this file before deleting
    ids_to_remove: list[str] = []
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("_del_scan", dest)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for name, obj in inspect.getmembers(mod, inspect.isclass):
            if issubclass(obj, BS) and obj is not BS:
                ids_to_remove.append(_class_to_id(name))
    except Exception:
        pass

    dest.unlink()
    eng = get_engine()
    removed = [sid for sid in ids_to_remove if eng.remove_strategy(sid)]
    for sid in removed:
        custom_ids = getattr(eng, "_custom_strategy_ids", set())
        custom_ids.discard(sid)
    return {"deleted": filename, "strategies_removed": removed}


@router.post("/strategies/{strategy_id}/params")
async def update_params(strategy_id: str, update: ParamUpdate):
    # Validate param types against the strategy's existing params
    for s in get_engine().strategies:
        if s.strategy_id == strategy_id:
            errors: list[str] = []
            for key, val in update.params.items():
                if key not in s.params:
                    errors.append(f"Unknown param: {key}")
                    continue
                expected = type(s.params[key])
                # Allow int/float coercion
                if expected is float and isinstance(val, int):
                    update.params[key] = float(val)
                elif expected is int and isinstance(val, float) and val == int(val):
                    update.params[key] = int(val)
                elif not isinstance(val, expected):
                    errors.append(f"Param '{key}' must be {expected.__name__}, got {type(val).__name__}")
            if errors:
                raise HTTPException(422, {"detail": errors})
            s.update_params(update.params)
            return {"updated": True, "params": s.params}
    raise HTTPException(404, f"Strategy {strategy_id} not found")


@router.post("/strategies/{strategy_id}/enable")
async def enable_strategy(strategy_id: str):
    for s in get_engine().strategies:
        if s.strategy_id == strategy_id:
            s.enable()
            return {"enabled": True}
    raise HTTPException(404, "Strategy not found")


@router.post("/strategies/{strategy_id}/disable")
async def disable_strategy(strategy_id: str):
    for s in get_engine().strategies:
        if s.strategy_id == strategy_id:
            s.disable()
            return {"enabled": False}
    raise HTTPException(404, "Strategy not found")


@router.post("/strategies/{strategy_id}/pause")
async def pause_strategy(strategy_id: str):
    """Pause a strategy (stops new signals but keeps state). Different from disable."""
    eng = get_engine()
    strat = next((s for s in eng.strategies if s.strategy_id == strategy_id), None)
    if not strat:
        raise HTTPException(404, "Strategy not found")
    strat._paused = True
    strat.logger.info(f"Strategy {strategy_id} paused via API")
    return {"paused": True, "strategy_id": strategy_id}


@router.post("/strategies/{strategy_id}/resume-pause")
async def resume_strategy_pause(strategy_id: str):
    """Resume a paused strategy."""
    eng = get_engine()
    strat = next((s for s in eng.strategies if s.strategy_id == strategy_id), None)
    if not strat:
        raise HTTPException(404, "Strategy not found")
    strat._paused = False
    strat.logger.info(f"Strategy {strategy_id} unpaused via API")
    return {"paused": False, "strategy_id": strategy_id}


# ── Phase 2: DEX / Chain endpoints ───────────────────────────────────────────

_dex_connectors: dict[str, object] = {}   # chain → UniswapV3Connector
_dex_wallets: dict[str, object] = {}      # chain → EVMWallet


def set_dex_services(connectors: dict, wallets: dict) -> None:
    """Called from main.py after chain initialisation."""
    _dex_connectors.update(connectors)
    _dex_wallets.update(wallets)


@router.get("/dex/chains")
async def list_chains():
    """List configured chains and their connection status.

    For connected chains with a wallet, best-effort enriches each entry with the
    wallet address, ETH balance and current gas price. A failing RPC on one chain
    never breaks the whole list.
    """
    eng = get_engine()
    cfg = eng.config
    result = []
    for name, chain_cfg in cfg.chains.items():
        entry = {
            "chain": name,
            "name": name,
            "chain_id": chain_cfg.chain_id,
            "enabled": chain_cfg.enabled,
            "connected": name in _dex_connectors,
            "has_wallet": bool(chain_cfg.private_key),
            "wallet": None,
            "gas_gwei": None,
        }
        wallet = _dex_wallets.get(name)
        if wallet:
            wallet_info = {"address": wallet.address, "eth_balance_ether": None}
            try:
                wallet_info["eth_balance_ether"] = float(await wallet.get_eth_balance())
            except Exception:
                pass
            entry["wallet"] = wallet_info
        conn = _dex_connectors.get(name)
        if conn is not None and getattr(conn, "_w3", None) is not None:
            try:
                gas_wei = await conn._w3.eth.gas_price
                entry["gas_gwei"] = round(gas_wei / 1e9, 3)
            except Exception:
                pass
        result.append(entry)
    return result


@router.post("/dex/connect/{chain}")
async def connect_chain(chain: str):
    """Connect to a chain's DEX (Uniswap V3)."""
    eng = get_engine()
    chain_cfg = eng.config.chains.get(chain)
    if not chain_cfg:
        raise HTTPException(404, f"Chain '{chain}' not configured")
    if not chain_cfg.rpc_url:
        raise HTTPException(400, "rpc_url not configured for this chain")
    try:
        from connectors.dex.uniswap_v3 import UniswapV3Connector
        from connectors.onchain.wallet import EVMWallet
        conn = UniswapV3Connector(chain=chain, rpc_url=chain_cfg.rpc_url)
        await conn.connect()
        _dex_connectors[chain] = conn
        if chain_cfg.private_key:
            wallet = EVMWallet(
                private_key=chain_cfg.private_key,
                rpc_url=chain_cfg.rpc_url,
                chain_id=chain_cfg.chain_id,
            )
            await wallet.connect()
            _dex_wallets[chain] = wallet
        return {"connected": True, "chain": chain, "has_wallet": chain in _dex_wallets}
    except Exception as e:
        raise HTTPException(500, str(e))


class DexQuoteRequest(BaseModel):
    chain: str
    token_in: str
    token_out: str
    amount_in: float
    fee_tier: int = 3000


class DexSwapRequest(BaseModel):
    chain: str
    token_in: str
    token_out: str
    amount_in: float
    fee_tier: int = 3000
    slippage_bps: int = 50


@router.post("/dex/quote")
async def get_dex_quote(req: DexQuoteRequest):
    """Get a Uniswap V3 swap quote (read-only, no gas)."""
    conn = _dex_connectors.get(req.chain)
    if not conn:
        raise HTTPException(400, f"Chain '{req.chain}' not connected. POST /api/dex/connect/{req.chain} first.")
    try:
        from decimal import Decimal
        quote = await conn.get_quote(req.token_in, req.token_out, Decimal(str(req.amount_in)), req.fee_tier)
        return {
            "chain": quote.chain, "dex": quote.dex,
            "token_in": quote.token_in, "token_out": quote.token_out,
            "amount_in": float(quote.amount_in), "amount_out": float(quote.amount_out),
            "effective_price": float(quote.effective_price),
            "price_impact_pct": float(quote.price_impact_pct),
            "gas_estimate": quote.gas_estimate,
            "fee_tier": quote.fee_tier,
        }
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/dex/swap")
async def execute_dex_swap(req: DexSwapRequest):
    """Execute a Uniswap V3 swap. Requires wallet configured for this chain."""
    conn = _dex_connectors.get(req.chain)
    if not conn:
        raise HTTPException(400, f"Chain '{req.chain}' not connected.")
    wallet = _dex_wallets.get(req.chain)
    if not wallet:
        raise HTTPException(400, f"No wallet configured for chain '{req.chain}'. Set private_key in config.")
    try:
        from decimal import Decimal
        amount = Decimal(str(req.amount_in))
        quote = await conn.get_quote(req.token_in, req.token_out, amount, req.fee_tier)
        result = await conn.swap(quote, wallet, slippage_bps=req.slippage_bps, auto_approve=True)
        return {
            "tx_hash": result.tx_hash,
            "chain": result.chain,
            "dex": result.dex,
            "token_in": result.token_in,
            "token_out": result.token_out,
            "amount_in": float(result.amount_in),
            "amount_out": float(result.amount_out),
            "gas_used": result.gas_used,
            "gas_price_gwei": result.gas_price_gwei,
            "success": result.success,
        }
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/dex/wallet/{chain}")
async def get_wallet_info(chain: str):
    """Get wallet address and ETH balance for a chain."""
    wallet = _dex_wallets.get(chain)
    if not wallet:
        raise HTTPException(404, f"No wallet for chain '{chain}'")
    try:
        eth_balance = await wallet.get_eth_balance()
        return {
            "chain": chain,
            "address": wallet.address,
            "eth_balance": float(eth_balance),
            "eth_balance_ether": float(eth_balance),
        }
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/dex/wallet/{chain}/token/{token_address}")
async def get_token_balance(chain: str, token_address: str):
    """Get ERC20 token balance for a wallet on a chain."""
    wallet = _dex_wallets.get(chain)
    if not wallet:
        raise HTTPException(404, f"No wallet for chain '{chain}'")
    try:
        balance = await wallet.get_token_balance(token_address)
        return {"chain": chain, "address": wallet.address, "token": token_address, "balance": float(balance)}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Phase 3: Data endpoints ───────────────────────────────────────────────────

_data_storage = None
_data_fetcher  = None
_backtest_engine = None
_optimizer = None


def get_storage():
    global _data_storage
    if _data_storage is None:
        raise HTTPException(503, "Data storage not initialized")
    return _data_storage


def get_backtest():
    global _backtest_engine
    if _backtest_engine is None:
        raise HTTPException(503, "Backtest engine not initialized")
    return _backtest_engine


def get_optimizer():
    global _optimizer
    if _optimizer is None:
        raise HTTPException(503, "Optimizer not initialized")
    return _optimizer


def set_data_services(storage, fetcher, bt_engine, optimizer) -> None:
    global _data_storage, _data_fetcher, _backtest_engine, _optimizer
    _data_storage   = storage
    _data_fetcher   = fetcher
    _backtest_engine = bt_engine
    _optimizer      = optimizer


@router.get("/data/symbols")
async def data_symbols():
    """List historical data available for backtesting."""
    return await get_storage().get_symbols()


@router.get("/data/ohlcv/{exchange}/{symbol}")
async def get_ohlcv(exchange: str, symbol: str, interval: str = "1h",
                    limit: int = 500, start_ts: Optional[int] = None):
    rows = await get_storage().get_ohlcv(exchange, symbol, interval,
                                          start_ts=start_ts, limit=limit)
    return [{"ts": r.ts, "open": r.open, "high": r.high,
             "low": r.low, "close": r.close, "volume": r.volume} for r in rows]


@router.get("/data/equity")
async def get_equity_curve(limit: int = 1440):
    return await get_storage().get_equity_curve(limit)


@router.get("/data/trades")
async def get_data_trades(exchange: Optional[str] = None, symbol: Optional[str] = None,
                          strategy_id: Optional[str] = None, limit: int = 200):
    return await get_storage().get_trades(exchange=exchange, symbol=symbol,
                                          strategy_id=strategy_id, limit=limit)


@router.get("/arb-triggers")
async def get_arb_triggers(limit: int = 100):
    """Recent spread-arb trigger events with outcomes (quality tracking)."""
    return await get_storage().get_arb_triggers(limit=limit)


@router.get("/arb-triggers/stats")
async def get_arb_trigger_stats(hours: float = 168.0):
    """Aggregate trigger quality: completion rate, per-outcome counts/avgs."""
    return await get_storage().get_arb_trigger_stats(hours=hours)


class FetchRequest(BaseModel):
    exchange: str
    symbol: str
    interval: str = "1h"
    days: int = 365


@router.post("/data/fetch")
async def fetch_historical(req: FetchRequest):
    """Download historical OHLCV from exchange (public, no auth needed)."""
    global _data_fetcher
    if _data_fetcher is None:
        raise HTTPException(503, "Data fetcher not initialized")
    try:
        n = await _data_fetcher.fetch(req.exchange, req.symbol, req.interval, req.days)
        return {"stored": n, "exchange": req.exchange, "symbol": req.symbol,
                "interval": req.interval, "days": req.days}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Phase 3: Backtest endpoints ───────────────────────────────────────────────

class BacktestRequest(BaseModel):
    strategy_id: str
    exchange: str = "binance"
    symbol: str = "BTC-USDT"
    interval: str = "1h"
    start_ts: int        # unix seconds
    end_ts: int
    initial_capital: float = 10_000.0
    params: dict = Field(default_factory=dict)
    slippage_bps: int = 0
    funding_rate_pct: float = 0.0
    taker_fee_bps: Optional[float] = None   # None = engine default (4.0)
    maker_fee_bps: Optional[float] = None   # None = engine default (2.0)


@router.post("/backtest/run")
async def run_backtest(req: BacktestRequest):
    """Create and run a backtest job. Returns job_id; poll /backtest/{job_id} for results."""
    bt = get_backtest()
    strategy_class = _resolve_strategy(req.strategy_id)
    if not strategy_class:
        raise HTTPException(404, f"Strategy '{req.strategy_id}' not registered")
    job = bt.create_job(
        strategy_class=strategy_class,
        strategy_id=req.strategy_id,
        params=req.params,
        exchange=req.exchange,
        symbol=req.symbol,
        interval=req.interval,
        start_ts=req.start_ts,
        end_ts=req.end_ts,
        initial_capital=req.initial_capital,
        slippage_bps=req.slippage_bps,
        funding_rate_pct=req.funding_rate_pct,
        taker_fee_bps=req.taker_fee_bps,
        maker_fee_bps=req.maker_fee_bps,
    )
    return {"job_id": job.job_id, "status": job.status}


@router.get("/backtest/jobs")
async def list_backtest_jobs():
    return get_backtest().list_jobs()


@router.get("/backtest/jobs/history")
async def get_backtest_history(limit: int = 50):
    """Return persisted backtest job results from DB."""
    return await get_storage().get_backtest_jobs(limit=limit)


@router.get("/backtest/{job_id}")
async def get_backtest_result(job_id: str):
    job = get_backtest().get_job(job_id)
    if not job:
        raise HTTPException(404, f"Job {job_id} not found")
    return {
        "job_id": job.job_id, "status": job.status,
        "progress": job.progress, "error": job.error,
        "strategy_id": job.strategy_id, "symbol": job.symbol,
        "interval": job.interval, "params": job.params,
        "result": job.result,
    }


# ── Phase 3: Optimizer endpoints ──────────────────────────────────────────────

class GridSearchRequest(BaseModel):
    strategy_id: str
    param_grid: dict        # {"min_spread_bps": [3, 5, 8, 12]}
    exchange: str = "binance"
    symbol: str = "BTC-USDT"
    interval: str = "1h"
    start_ts: int
    end_ts: int
    initial_capital: float = 10_000.0
    metric: str = "sharpe_ratio"


class BayesianRequest(BaseModel):
    strategy_id: str
    param_bounds: dict      # {"min_spread_bps": [2.0, 15.0]}
    exchange: str = "binance"
    symbol: str = "BTC-USDT"
    interval: str = "1h"
    start_ts: int
    end_ts: int
    n_calls: int = 30
    initial_capital: float = 10_000.0
    metric: str = "sharpe_ratio"


@router.post("/optimizer/grid")
async def run_grid_search(req: GridSearchRequest):
    opt = get_optimizer()
    strategy_class = _resolve_strategy(req.strategy_id)
    if not strategy_class:
        raise HTTPException(404, f"Strategy '{req.strategy_id}' not registered")
    result = opt.grid_search(
        strategy_class=strategy_class,
        strategy_id=req.strategy_id,
        param_grid=req.param_grid,
        exchange=req.exchange, symbol=req.symbol, interval=req.interval,
        start_ts=req.start_ts, end_ts=req.end_ts,
        initial_capital=req.initial_capital, metric=req.metric,
    )
    return {"job_id": result.job_id, "status": result.status, "runs": result.runs}


@router.post("/optimizer/bayesian")
async def run_bayesian(req: BayesianRequest):
    opt = get_optimizer()
    strategy_class = _resolve_strategy(req.strategy_id)
    if not strategy_class:
        raise HTTPException(404, f"Strategy '{req.strategy_id}' not registered")
    result = opt.bayesian_search(
        strategy_class=strategy_class,
        strategy_id=req.strategy_id,
        param_bounds={k: tuple(v) for k, v in req.param_bounds.items()},
        exchange=req.exchange, symbol=req.symbol, interval=req.interval,
        start_ts=req.start_ts, end_ts=req.end_ts,
        n_calls=req.n_calls, initial_capital=req.initial_capital, metric=req.metric,
    )
    return {"job_id": result.job_id, "status": result.status}


@router.get("/optimizer/jobs")
async def list_optimizer_jobs():
    return get_optimizer().list_jobs()


@router.get("/optimizer/{job_id}")
async def get_optimizer_result(job_id: str):
    result = get_optimizer().get_job(job_id)
    if not result:
        raise HTTPException(404, f"Optimizer job {job_id} not found")
    return result.to_dict()


# ── Stats endpoint ────────────────────────────────────────────────────────────

@router.get("/funding-rates")
async def get_funding_rates():
    """Fetch current funding rates from all futures/swap connectors."""
    from core.types import Exchange as Ex
    eng = get_engine()
    results = []
    futures_exs = [Ex.BINANCE, Ex.OKX]
    for ex in futures_exs:
        conn = eng.connectors.get(ex)
        if not conn:
            continue
        try:
            # Pass known symbols so OKX can query per instrument (no bulk endpoint)
            import inspect
            sig = inspect.signature(conn.get_funding_rates)
            if "symbols" in sig.parameters:
                rates = await conn.get_funding_rates(symbols=eng.config.engine.symbols)
            else:
                rates = await conn.get_funding_rates()
            for r in rates:
                results.append({
                    "exchange": ex.value,
                    "symbol": r.get("symbol", ""),
                    "funding_rate": r.get("funding_rate", 0),
                    "next_funding_time": r.get("next_funding_time"),
                    "annualized_pct": round(float(r.get("funding_rate", 0)) * 3 * 365 * 100, 2),
                })
        except Exception as e:
            results.append({"exchange": ex.value, "error": str(e)})
    return results


@router.get("/stats")
async def get_stats():
    """Aggregate performance stats: total trades, volume, fees, equity, drawdown."""
    storage = get_storage()
    return await storage.get_stats()


@router.post("/stats/reset-equity-baseline")
async def reset_equity_baseline():
    """Reset equity tracking baseline to current balance. Clears historical snapshots."""
    storage = get_storage()
    # Delete all equity snapshots - fresh start from now
    async with storage._db.execute("DELETE FROM equity_snapshots") as cur:
        deleted = cur.rowcount
    await storage._db.commit()
    return {"reset": True, "deleted_snapshots": deleted}


# ── Notifications ─────────────────────────────────────────────────────────────

@router.post("/notifications/test")
async def test_notifications():
    """Send a test Telegram message to verify configuration."""
    eng = get_engine()
    notifier = getattr(eng, "_notifier", None)
    if notifier is None or not notifier.enabled:
        raise HTTPException(400, "Telegram not configured (set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID)")
    ok = await notifier.test()
    return {"sent": ok}


@router.get("/notifications/status")
async def notifications_status():
    eng = get_engine()
    notifier = getattr(eng, "_notifier", None)
    return {"enabled": notifier is not None and notifier.enabled}


# ── Strategy registry ─────────────────────────────────────────────────────────

_strategy_registry: dict[str, type] = {}


def register_strategy(strategy_id: str, cls: type) -> None:
    _strategy_registry[strategy_id] = cls


def _resolve_strategy(strategy_id: str):
    if strategy_id in _strategy_registry:
        return _strategy_registry[strategy_id]
    if _engine is not None:
        for s in _engine.strategies:
            if s.strategy_id == strategy_id:
                return type(s)
    return None


# ── Walk-forward endpoint ─────────────────────────────────────────────────────

class WalkForwardRequest(BaseModel):
    strategy_id: str
    param_grid: dict
    exchange: str
    symbol: str
    interval: str = "1h"
    start_ts: int
    end_ts: int
    n_folds: int = Field(default=5, ge=2, le=20)
    train_frac: float = Field(default=0.7, ge=0.5, le=0.9)
    method: str = "grid"
    initial_capital: float = 10_000.0
    metric: str = "sharpe_ratio"


@router.post("/optimizer/walk-forward")
async def run_walk_forward(req: WalkForwardRequest):
    """Run walk-forward validation: optimize in-sample, test out-of-sample across N folds."""
    opt = get_optimizer()
    cls = _resolve_strategy(req.strategy_id)
    if not cls:
        raise HTTPException(404, f"Strategy '{req.strategy_id}' not found")
    result = opt.walk_forward(
        strategy_class=cls, strategy_id=req.strategy_id,
        param_grid=req.param_grid,
        exchange=req.exchange, symbol=req.symbol, interval=req.interval,
        start_ts=req.start_ts, end_ts=req.end_ts,
        n_folds=req.n_folds, train_frac=req.train_frac,
        method=req.method, initial_capital=req.initial_capital, metric=req.metric,
    )
    return result.to_dict()


@router.get("/optimizer/walk-forward/jobs")
async def list_wf_jobs():
    return get_optimizer().list_wf_jobs()


@router.get("/optimizer/walk-forward/{job_id}")
async def get_wf_result(job_id: str):
    result = get_optimizer().get_wf_job(job_id)
    if not result:
        raise HTTPException(404, "Walk-forward job not found")
    return result.to_dict()


# ── Data export ───────────────────────────────────────────────────────────────

@router.get("/data/trades/export")
async def export_trades(
    exchange: Optional[str] = None,
    symbol: Optional[str] = None,
    strategy_id: Optional[str] = None,
    limit: int = 10000,
):
    """Export trades as CSV file download."""
    import io
    trades = await get_storage().get_trades(
        exchange=exchange, symbol=symbol, strategy_id=strategy_id, limit=limit
    )
    buf = io.StringIO()
    buf.write("ts,strategy_id,exchange,symbol,side,order_type,quantity,price,fee,order_id,status\n")
    for t in trades:
        buf.write(
            f"{t.get('ts','')},{t.get('strategy_id','')},{t.get('exchange','')},{t.get('symbol','')},"
            f"{t.get('side','')},{t.get('order_type','')},{t.get('quantity','')},{t.get('price','')},"
            f"{t.get('fee','')},{t.get('order_id','')},{t.get('status','')}\n"
        )
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=trades.csv"},
    )


@router.get("/data/equity/export")
async def export_equity(limit: int = 100000):
    """Export equity curve as CSV file download."""
    import io
    rows = await get_storage().get_equity_curve(limit=limit)
    buf = io.StringIO()
    buf.write("ts,total_usdt,daily_pnl\n")
    for r in rows:
        buf.write(f"{r.get('ts','')},{r.get('total_usdt','')},{r.get('daily_pnl','')}\n")
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=equity_curve.csv"},
    )


# ── Data integrity ────────────────────────────────────────────────────────────

@router.get("/data/db-size")
async def get_db_size():
    storage = get_storage()
    return await storage.get_db_size()


@router.post("/data/purge")
async def purge_old_data(ticks_days: int = 7, logs_days: int = 30, attribution_days: int = 90):
    """Manually trigger DB cleanup. Normally runs automatically every 24h."""
    storage = get_storage()
    return await storage.purge_old_data(ticks_days=ticks_days, logs_days=logs_days,
                                         attribution_days=attribution_days)


@router.get("/data/integrity/{exchange}/{symbol}")
async def check_data_integrity(exchange: str, symbol: str, interval: str = "1h",
                                start_ts: Optional[int] = None, end_ts: Optional[int] = None):
    """Check OHLCV data for gaps and coverage."""
    global _data_fetcher
    if _data_fetcher is None:
        raise HTTPException(503, "Data fetcher not initialized")
    return await _data_fetcher.check_integrity(exchange, symbol, interval, start_ts, end_ts)


@router.post("/data/fill-gaps/{exchange}/{symbol}")
async def fill_data_gaps(exchange: str, symbol: str, interval: str = "1h",
                         market_type: str = "futures", max_gaps: int = 20):
    """Auto-fill detected data gaps by fetching missing candles from the exchange."""
    global _data_fetcher
    if _data_fetcher is None:
        raise HTTPException(503, "Data fetcher not initialized")
    result = await _data_fetcher.fill_gaps(exchange, symbol, interval, market_type, max_gaps)
    return result


@router.post("/data/backup")
async def backup_database():
    """Create a backup copy of the database file."""
    storage = get_storage()
    path = await storage.backup()
    return {"backup_path": path}


# ── Price alert rules ─────────────────────────────────────────────────────────

class AlertRuleRequest(BaseModel):
    symbol: Optional[str] = None
    exchange: Optional[str] = None
    type: str   # price_above | price_below | funding_above | strategy_loss_above | position_timeout_h
    threshold: float
    message: Optional[str] = None
    strategy_id: Optional[str] = None  # for strategy_loss_above


@router.get("/alerts")
async def list_alerts():
    return list(_alert_rules.values())


@router.post("/alerts")
async def create_alert(req: AlertRuleRequest):
    _valid_types = ("price_above", "price_below", "funding_above", "strategy_loss_above", "position_timeout_h")
    if req.type not in _valid_types:
        raise HTTPException(400, f"type must be one of: {', '.join(_valid_types)}")
    rule_id = uuid.uuid4().hex[:10]
    rule = {
        "id": rule_id,
        "symbol": req.symbol,
        "exchange": req.exchange,
        "type": req.type,
        "threshold": req.threshold,
        "message": req.message or f"{req.type.replace('_', ' ')} {req.threshold}",
        "strategy_id": req.strategy_id,
        "triggered": False,
        "triggered_at": None,
        "triggered_price": None,
        "created_at": time.time(),
    }
    _alert_rules[rule_id] = rule
    return rule


@router.delete("/alerts/{rule_id}")
async def delete_alert(rule_id: str):
    if rule_id not in _alert_rules:
        raise HTTPException(404, "Alert rule not found")
    del _alert_rules[rule_id]
    return {"deleted": True}


@router.post("/alerts/{rule_id}/reset")
async def reset_alert(rule_id: str):
    """Re-arm a triggered alert rule."""
    rule = _alert_rules.get(rule_id)
    if not rule:
        raise HTTPException(404, "Alert rule not found")
    rule["triggered"] = False
    rule["triggered_at"] = None
    rule["triggered_price"] = None
    return rule


# ── Strategy logs ─────────────────────────────────────────────────────────────

@router.get("/logs")
async def get_strategy_logs(
    strategy_id: Optional[str] = None,
    level: Optional[str] = None,
    limit: int = 200,
):
    return await get_storage().get_logs(strategy_id=strategy_id, level=level, limit=limit)


# ── Quant services registry ───────────────────────────────────────────────────

_position_sizer  = None
_portfolio_risk  = None
_attributor      = None
_regime_detector = None
_microstructure  = None
_exec_algos      = None
_reconciler      = None
_margin_monitor  = None
_factor_monitor  = None
_capacity_analyzer = None
_tca             = None
_stress_tester   = None
_health_monitor  = None


def set_quant_services(
    position_sizer, portfolio_risk, attributor,
    regime_detector, microstructure, exec_algos,
    reconciler, margin_monitor, factor_monitor, capacity_analyzer,
    tca=None, stress_tester=None, health_monitor=None,
) -> None:
    global _position_sizer, _portfolio_risk, _attributor, _regime_detector
    global _microstructure, _exec_algos, _reconciler, _margin_monitor
    global _factor_monitor, _capacity_analyzer, _tca, _stress_tester
    global _health_monitor
    _position_sizer    = position_sizer
    _portfolio_risk    = portfolio_risk
    _attributor        = attributor
    _regime_detector   = regime_detector
    _microstructure    = microstructure
    _exec_algos        = exec_algos
    _reconciler        = reconciler
    _margin_monitor    = margin_monitor
    _factor_monitor    = factor_monitor
    _capacity_analyzer = capacity_analyzer
    _tca               = tca
    _stress_tester     = stress_tester
    _health_monitor    = health_monitor


# ── Portfolio risk endpoints ──────────────────────────────────────────────────

@router.get("/portfolio/risk")
async def get_portfolio_risk():
    if _portfolio_risk is None:
        raise HTTPException(503, "Portfolio risk module not initialized")
    return _portfolio_risk.status()


@router.get("/portfolio/var")
async def get_portfolio_var(confidence: float = 0.95):
    if _portfolio_risk is None:
        raise HTTPException(503, "Portfolio risk module not initialized")
    return {
        "var": round(_portfolio_risk.portfolio_var(confidence), 4),
        "cvar": round(_portfolio_risk.portfolio_cvar(confidence), 4),
        "confidence": confidence,
        "sharpe": round(_portfolio_risk.portfolio_sharpe(), 3),
    }


@router.get("/portfolio/correlation")
async def get_correlation_matrix():
    if _portfolio_risk is None:
        raise HTTPException(503, "Portfolio risk module not initialized")
    return {
        "matrix": _portfolio_risk.correlation_matrix(),
        "high_correlation_pairs": [
            {"s1": p[0], "s2": p[1], "corr": round(p[2], 3)}
            for p in _portfolio_risk.high_correlation_pairs()
        ],
    }


# ── Position sizer endpoints ──────────────────────────────────────────────────

@router.get("/position-sizer/status")
async def get_position_sizer_status():
    if _position_sizer is None:
        raise HTTPException(503, "Position sizer not initialized")
    return _position_sizer.status()


@router.get("/position-sizer/size")
async def get_position_size(symbol: str, capital_usdt: float):
    if _position_sizer is None:
        raise HTTPException(503, "Position sizer not initialized")
    return {
        "symbol": symbol,
        "capital_usdt": capital_usdt,
        "recommended_size_usdt": round(_position_sizer.get_size_usdt(symbol, capital_usdt), 2),
        "vol_ann": round(_position_sizer.get_vol(symbol), 4),
        "vol_regime_mult": round(_position_sizer.get_vol_regime_multiplier(symbol), 3),
    }


# ── Regime detector endpoints ─────────────────────────────────────────────────

@router.get("/regime")
async def get_regime():
    if _regime_detector is None:
        raise HTTPException(503, "Regime detector not initialized")
    return _regime_detector.all_snapshots()


@router.get("/regime/{symbol}")
async def get_symbol_regime(symbol: str):
    if _regime_detector is None:
        raise HTTPException(503, "Regime detector not initialized")
    snap = _regime_detector.get(symbol)
    if not snap:
        return {"symbol": symbol, "regime": "unknown", "message": "Insufficient data"}
    return {
        "symbol": symbol,
        "regime": snap.regime,
        "realized_vol_ann": snap.realized_vol_ann,
        "vol_percentile": snap.vol_percentile,
        "pos_size_mult": snap.pos_size_mult,
        "threshold_mult": snap.threshold_mult,
    }


# ── Microstructure endpoints ──────────────────────────────────────────────────

@router.get("/microstructure")
async def get_microstructure():
    if _microstructure is None:
        raise HTTPException(503, "Microstructure module not initialized")
    return _microstructure.all_snapshots()


# ── Reconciler endpoints ──────────────────────────────────────────────────────

@router.get("/reconciler/status")
async def get_reconciler_status():
    if _reconciler is None:
        raise HTTPException(503, "Reconciler not initialized")
    return _reconciler.status()


@router.post("/reconciler/run")
async def trigger_reconciliation():
    if _reconciler is None:
        raise HTTPException(503, "Reconciler not initialized")
    report = await _reconciler.reconcile_now()
    return report


# ── Margin monitor endpoints ──────────────────────────────────────────────────

@router.get("/margin")
async def get_margin_status():
    if _margin_monitor is None:
        raise HTTPException(503, "Margin monitor not initialized")
    return {"positions": _margin_monitor.get_statuses()}


# ── Factor exposure endpoints ─────────────────────────────────────────────────

@router.get("/factor-exposure")
async def get_factor_exposure():
    if _factor_monitor is None:
        raise HTTPException(503, "Factor exposure monitor not initialized")
    snap = _factor_monitor.get_snapshot()
    if snap is None:
        return {"status": "no_data", "message": "No positions yet"}
    return snap


# ── PnL attribution endpoints ─────────────────────────────────────────────────

@router.get("/attribution/summary")
async def get_attribution_summary(days: int = 30):
    storage = get_storage()
    return await storage.get_attribution_summary(days)


@router.get("/attribution/recent")
async def get_attribution_recent(
    strategy_id: Optional[str] = None,
    limit: int = 100,
):
    storage = get_storage()
    return await storage.get_attribution(strategy_id=strategy_id, limit=limit)


# ── Funding rate history endpoints ────────────────────────────────────────────

@router.get("/funding-history")
async def get_funding_history(
    exchange: Optional[str] = None,
    symbol: Optional[str] = None,
    limit: int = 200,
):
    storage = get_storage()
    return await storage.get_funding_rates(exchange=exchange, symbol=symbol, limit=limit)


@router.get("/funding-history/stats")
async def get_funding_stats(
    exchange: Optional[str] = None,
    symbol: Optional[str] = None,
    days: int = 30,
):
    storage = get_storage()
    if exchange and symbol:
        return await storage.get_funding_rate_stats(exchange, symbol, days)
    # No filter — aggregate across all available data
    rates = await storage.get_funding_rates(exchange=exchange, symbol=symbol, limit=10000)
    if not rates:
        return {"count": 0, "mean": 0.0, "std": 0.0, "min": 0.0, "max": 0.0, "annualized_mean": 0.0}
    values = [r["rate"] for r in rates if r.get("rate") is not None]
    if not values:
        return {"count": 0, "mean": 0.0, "std": 0.0, "min": 0.0, "max": 0.0, "annualized_mean": 0.0}
    import statistics
    mean = statistics.mean(values)
    return {
        "count": len(values),
        "mean": round(mean, 8),
        "std": round(statistics.stdev(values) if len(values) > 1 else 0.0, 8),
        "min": round(min(values), 8),
        "max": round(max(values), 8),
        "annualized_mean": round(mean * 3 * 365, 4),
    }


@router.get("/funding-harvest")
async def get_funding_harvest(
    exchange: str = "binance",
    days: int = 30,
    fee_bps_per_leg: float = 4.0,
    min_periods: int = 10,
    min_span_days: float = 7.0,
    min_favorable_pct: float = 0.0,
    top_n: int = 50,
):
    """Rank symbols by how profitable a delta-neutral funding harvest would have
    been over the stored funding_rates history. Polled rates are deduped to real
    settlements (next_funding_time) so oversampling doesn't bias the mean.
    `favorable_pct` (how often funding stayed on the committed side) is the key
    risk discriminator. `min_span_days` / `min_periods` drop short-history noise;
    raise `min_favorable_pct` to keep only persistent funding."""
    from backtest.funding_harvest import FundingHarvestAnalyzer
    analyzer = FundingHarvestAnalyzer(get_storage())
    return await analyzer.scan(
        exchange=exchange, days=days, fee_bps_per_leg=fee_bps_per_leg,
        min_periods=min_periods, min_span_days=min_span_days,
        min_favorable_pct=min_favorable_pct, top_n=top_n,
    )


@router.get("/funding-harvest/backtest")
async def get_funding_harvest_backtest(
    symbol: str,
    days: int = 30,
    interval: str = "1h",
    initial_capital: float = 10_000.0,
    fee_bps_per_leg: float = 2.0,
):
    """Basis-aware backtest of a single-exchange delta-neutral funding harvest:
    fetches the symbol's perp + spot OHLCV from Binance and replays it alongside
    the stored funding settlements, separating funding carry from basis PnL and
    fees. Perp-only alts (no spot market) return a 422 — they can't be spot-hedged."""
    from backtest.funding_harvest_sim import HarvestBacktestRunner
    try:
        res = await HarvestBacktestRunner(get_storage()).run(
            symbol, days=days, interval=interval,
            initial_capital=initial_capital, fee_bps_per_leg=fee_bps_per_leg,
        )
    except ValueError as e:
        raise HTTPException(422, str(e))
    return res.to_dict()


# ── Execution algorithms endpoints ────────────────────────────────────────────

class TwapRequest(BaseModel):
    strategy_id: str
    exchange: str
    symbol: str
    side: str
    total_qty: float
    duration_s: int = 300
    n_slices: int = 10
    qty_precision: int = 6


class VwapRequest(BaseModel):
    strategy_id: str
    exchange: str
    symbol: str
    side: str
    total_qty: float
    duration_s: int = 300
    n_slices: int = 10
    qty_precision: int = 6


@router.post("/execution/twap")
async def place_twap_order(req: TwapRequest):
    if _exec_algos is None:
        raise HTTPException(503, "Execution algorithms not initialized")
    from core.types import Exchange, OrderSide
    from decimal import Decimal
    try:
        order = _exec_algos.place_twap(
            strategy_id=req.strategy_id,
            exchange=Exchange(req.exchange),
            symbol=req.symbol,
            side=OrderSide(req.side),
            total_qty=Decimal(str(req.total_qty)),
            duration_s=req.duration_s,
            n_slices=req.n_slices,
            qty_precision=req.qty_precision,
        )
        return order.to_dict()
    except Exception as e:
        raise HTTPException(400, str(e))


@router.post("/execution/vwap")
async def place_vwap_order(req: VwapRequest):
    if _exec_algos is None:
        raise HTTPException(503, "Execution algorithms not initialized")
    from core.types import Exchange, OrderSide
    from decimal import Decimal
    try:
        order = _exec_algos.place_vwap(
            strategy_id=req.strategy_id,
            exchange=Exchange(req.exchange),
            symbol=req.symbol,
            side=OrderSide(req.side),
            total_qty=Decimal(str(req.total_qty)),
            duration_s=req.duration_s,
            n_slices=req.n_slices,
            qty_precision=req.qty_precision,
        )
        return order.to_dict()
    except Exception as e:
        raise HTTPException(400, str(e))


@router.get("/execution/orders")
async def list_algo_orders():
    if _exec_algos is None:
        raise HTTPException(503, "Execution algorithms not initialized")
    return _exec_algos.list_orders()


@router.get("/execution/orders/{algo_id}")
async def get_algo_order(algo_id: str):
    if _exec_algos is None:
        raise HTTPException(503, "Execution algorithms not initialized")
    order = _exec_algos.get_order(algo_id)
    if not order:
        raise HTTPException(404, f"Algo order {algo_id} not found")
    return order.to_dict()


@router.delete("/execution/orders/{algo_id}")
async def cancel_algo_order(algo_id: str):
    if _exec_algos is None:
        raise HTTPException(503, "Execution algorithms not initialized")
    ok = await _exec_algos.cancel(algo_id)
    return {"cancelled": ok}


# ── Capacity analysis endpoints ───────────────────────────────────────────────

class CapacityRequest(BaseModel):
    strategy_id: str
    exchange: str = "binance"
    symbol: str = "BTC-USDT"
    interval: str = "1h"
    start_ts: int
    end_ts: int
    capital_levels: Optional[list[float]] = None
    params: dict = Field(default_factory=dict)


@router.post("/capacity/run")
async def run_capacity_analysis(req: CapacityRequest):
    if _capacity_analyzer is None:
        raise HTTPException(503, "Capacity analyzer not initialized")
    cls = _resolve_strategy(req.strategy_id)
    if not cls:
        raise HTTPException(404, f"Strategy '{req.strategy_id}' not registered")
    result = _capacity_analyzer.run(
        strategy_class=cls,
        strategy_id=req.strategy_id,
        base_params=req.params,
        exchange=req.exchange,
        symbol=req.symbol,
        interval=req.interval,
        start_ts=req.start_ts,
        end_ts=req.end_ts,
        capital_levels=req.capital_levels,
    )
    return result.to_dict()


@router.get("/capacity/jobs")
async def list_capacity_jobs():
    if _capacity_analyzer is None:
        raise HTTPException(503, "Capacity analyzer not initialized")
    return _capacity_analyzer.list_jobs()


@router.get("/capacity/{job_id}")
async def get_capacity_job(job_id: str):
    if _capacity_analyzer is None:
        raise HTTPException(503, "Capacity analyzer not initialized")
    job = _capacity_analyzer.get_job(job_id)
    if not job:
        raise HTTPException(404, "Capacity job not found")
    return job.to_dict()


# ── Stress test endpoints ─────────────────────────────────────────────────────

@router.get("/stress/scenarios")
async def get_stress_scenarios():
    if _stress_tester is None:
        raise HTTPException(503, "Stress tester not initialized")
    positions = await get_engine().get_positions()
    pos_list = [
        {
            "exchange": p.exchange.value, "symbol": p.symbol, "side": p.side.value,
            "size": float(p.size), "notional": float(p.notional),
            "mark_price": float(p.mark_price),
        }
        for p in positions
    ]
    return _stress_tester.run_scenarios(pos_list)


@router.get("/stress/monte-carlo")
async def get_monte_carlo_var(n_simulations: int = 10000, confidence: float = 0.95):
    if _stress_tester is None:
        raise HTTPException(503, "Stress tester not initialized")
    result = _stress_tester.monte_carlo_var(n_simulations=n_simulations, confidence=confidence)
    return _stress_tester._mc_to_dict(result)


@router.get("/stress/report")
async def get_stress_report():
    if _stress_tester is None:
        raise HTTPException(503, "Stress tester not initialized")
    eng = get_engine()
    positions = await eng.get_positions()
    pos_list = [
        {
            "exchange": p.exchange.value, "symbol": p.symbol, "side": p.side.value,
            "size": float(p.size), "notional": float(p.notional),
            "mark_price": float(p.mark_price),
        }
        for p in positions
    ]
    balances = await eng.get_balances()
    equity = sum(float(b.total) for b in balances if b.asset == "USDT")
    return _stress_tester.full_report(pos_list, equity_usdt=equity)


# ── Latency monitoring endpoints ─────────────────────────────────────────────

@router.get("/latency")
async def get_latency_stats(exchange: Optional[str] = None):
    try:
        from monitoring.latency import get_monitor
        mon = get_monitor()
        if mon is None:
            return {"status": "not_initialized", "stats": []}
        stats = mon.get_stats_dict()
        if exchange:
            stats = [s for s in stats if s["exchange"] == exchange]
        return {"stats": stats, "has_alerts": mon.has_alerts()}
    except ImportError:
        raise HTTPException(503, "Latency monitor not available")


@router.get("/health/detail")
async def get_health_detail():
    """Consolidated operational health: feeds, event loop, queue, connectors.

    Unlike /health (a minimal DB+engine readiness probe), this reports the live
    health of the trading pipeline with a single ok/degraded/critical status and
    per-component detail."""
    if _health_monitor is None:
        raise HTTPException(503, "Health monitor not initialized")
    return _health_monitor.get_report()


# ── TCA endpoints ────────────────────────────────────────────────────────────

@router.get("/tca/stats")
async def get_tca_stats(strategy_id: Optional[str] = None):
    if _tca is None:
        raise HTTPException(503, "TCA not initialized")
    return _tca.all_stats_dict() if not strategy_id else (
        list(_tca.get_stats(strategy_id).values())
    )


@router.get("/tca/fills")
async def get_tca_fills(strategy_id: Optional[str] = None, limit: int = 50):
    if _tca is None:
        raise HTTPException(503, "TCA not initialized")
    return _tca.get_recent_fills(strategy_id=strategy_id, limit=limit)


# ── Mount router + static files ───────────────────────────────────────────────

app.include_router(router)

# Serve built frontend — only if dist exists (skipped in dev)
_dist = Path(__file__).parent.parent / "frontend" / "dist"
if _dist.exists():
    app.mount("/", StaticFiles(directory=str(_dist), html=True), name="static")
