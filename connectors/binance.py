from __future__ import annotations
import asyncio
import hashlib
import hmac
import json
import time
from decimal import Decimal
from typing import Optional
from urllib.parse import urlencode

import aiohttp
import websockets
import websockets.exceptions

from core.types import (
    Balance, Exchange, MarketType, Order, OrderBook, OrderSide,
    OrderStatus, OrderType, Position, PositionSide, Ticker,
    TickerEvent, OrderBookEvent, OrderUpdateEvent,
    PositionUpdateEvent, BalanceUpdateEvent, ConnectorReadyEvent,
)
from connectors.base import (
    BaseConnector, make_ssl_context, AsyncRateLimiter, SymbolRule,
    fmt_decimal, gen_client_order_id,
)


class BinanceConnector(BaseConnector):
    """Binance USDT-M Futures connector (supports spot and Portfolio Margin via portfolio_margin=True)."""

    _REST_FUTURES = "https://fapi.binance.com"
    _REST_SPOT    = "https://api.binance.com"
    _REST_PAPI    = "https://papi.binance.com"   # Portfolio Margin API
    _WS_FUTURES   = "wss://fstream.binance.com/ws"
    _WS_SPOT      = "wss://stream.binance.com:9443/ws"

    _REST_FUTURES_TEST = "https://testnet.binancefuture.com"
    _WS_FUTURES_TEST   = "wss://stream.binancefuture.com/ws"

    def __init__(
        self,
        api_key: str,
        secret: str,
        market_type: MarketType = MarketType.FUTURES,
        testnet: bool = False,
        portfolio_margin: bool = False,
    ):
        super().__init__(api_key, secret, market_type, testnet)
        # Portfolio Margin (统一账户/组合保证金) uses papi.binance.com instead of fapi
        self.portfolio_margin = portfolio_margin and market_type == MarketType.FUTURES
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._listen_key: Optional[str] = None
        self._subscribed_streams: list[str] = []
        self._ws_task: Optional[asyncio.Task] = None
        self._keepalive_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._last_msg_ts: float = 0.0
        self._WS_TIMEOUT_S: int = 45  # force reconnect if no message for this many seconds
        # REST throttle: Binance futures allows ~2400 weight/min; cap well under it
        self._limiter = AsyncRateLimiter(rate_per_sec=20, capacity=40)
        self._recv_window: int = 5000  # ms; reject if request older than this at the server

    @property
    def exchange(self) -> Exchange:
        return Exchange.BINANCE_SPOT if self.market_type == MarketType.SPOT else Exchange.BINANCE

    @property
    def _rest_base(self) -> str:
        if self.market_type == MarketType.FUTURES:
            return self._REST_FUTURES_TEST if self.testnet else self._REST_FUTURES
        return self._REST_SPOT

    @property
    def _ws_base(self) -> str:
        if self.market_type == MarketType.FUTURES:
            return self._WS_FUTURES_TEST if self.testnet else self._WS_FUTURES
        return self._WS_SPOT

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        self._running = True
        ssl_ctx = make_ssl_context()
        self._session = aiohttp.ClientSession(
            headers={"X-MBX-APIKEY": self.api_key},
            timeout=aiohttp.ClientTimeout(total=10),
            connector=aiohttp.TCPConnector(ssl=ssl_ctx),
        )
        try:
            await self._sync_time()
        except Exception as e:
            self.logger.warning(f"Server time sync failed (using local clock): {e}")
        try:
            await self._load_symbol_rules()
            self.logger.info(f"Loaded trading rules for {len(self._rules)} symbols")
        except Exception as e:
            self.logger.warning(f"Symbol rules load failed (orders sent unquantized): {e}")
        if self.api_key:
            try:
                self._listen_key = await self._get_listen_key()
                if self._listen_key not in self._subscribed_streams:
                    self._subscribed_streams.append(self._listen_key)
            except Exception as e:
                self.logger.warning(f"Listen key failed (no trading): {e}")
        self._last_msg_ts = time.time()
        self._ws_task = asyncio.create_task(self._ws_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_watchdog())
        if self.api_key:
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())
        self.logger.info("Binance connector connected")
        await self._emit(ConnectorReadyEvent(exchange=self.exchange))

    async def disconnect(self) -> None:
        self._running = False
        for task in (self._ws_task, self._keepalive_task, self._heartbeat_task):
            if task:
                task.cancel()
        if self._session:
            await self._session.close()
        self.logger.info("Binance connector disconnected")

    # ── Subscriptions ────────────────────────────────────────────────────────

    async def subscribe_ticker(self, symbol: str) -> None:
        raw = self.to_exchange_symbol(symbol).lower()
        stream = f"{raw}@bookTicker"
        if stream not in self._subscribed_streams:
            self._subscribed_streams.append(stream)
            await self._ws_subscribe([stream])

    async def subscribe_orderbook(self, symbol: str, depth: int = 20) -> None:
        raw = self.to_exchange_symbol(symbol).lower()
        stream = f"{raw}@depth{depth}@100ms"
        if stream not in self._subscribed_streams:
            self._subscribed_streams.append(stream)
            await self._ws_subscribe([stream])

    async def unsubscribe_ticker(self, symbol: str) -> None:
        raw = self.to_exchange_symbol(symbol).lower()
        stream = f"{raw}@bookTicker"
        self._subscribed_streams = [s for s in self._subscribed_streams if s != stream]
        await self._ws_unsubscribe([stream])

    async def unsubscribe_orderbook(self, symbol: str) -> None:
        raw = self.to_exchange_symbol(symbol).lower()
        for depth in (5, 10, 20, 50):
            stream = f"{raw}@depth{depth}@100ms"
            self._subscribed_streams = [s for s in self._subscribed_streams if s != stream]
        await self._ws_unsubscribe([f"{raw}@depth"])

    async def _ws_subscribe(self, streams: list[str]) -> None:
        """Send SUBSCRIBE message to live WS connection if available."""
        if self._ws is None:
            return
        try:
            msg = {"method": "SUBSCRIBE", "params": streams, "id": int(time.time() * 1000) % 100000}
            await self._ws.send(json.dumps(msg))
        except Exception as e:
            self.logger.warning(f"WS subscribe send failed (will apply on reconnect): {e}")

    async def _ws_unsubscribe(self, streams: list[str]) -> None:
        if self._ws is None:
            return
        try:
            msg = {"method": "UNSUBSCRIBE", "params": streams, "id": int(time.time() * 1000) % 100000}
            await self._ws.send(json.dumps(msg))
        except Exception as e:
            self.logger.debug(f"WS unsubscribe send failed: {e}")

    # ── WebSocket internals ───────────────────────────────────────────────────

    async def _ws_loop(self) -> None:
        """Always connect to the /stream endpoint and subscribe via SUBSCRIBE messages.
        This avoids the race condition where subscribe_ticker is called before the WS
        connection is established, and ensures reconnection re-subscribes cleanly."""
        backoff = 1
        # Use /stream endpoint for both single and combined (supports dynamic SUBSCRIBE)
        base = self._ws_base.replace("/ws", "/stream", 1) if "/ws" in self._ws_base else self._ws_base
        url = base  # connect bare; subscribe via messages
        while self._running:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=10,
                                              ssl=make_ssl_context()) as ws:
                    self._ws = ws
                    backoff = 1
                    # Subscribe to all current streams immediately after connect
                    streams = [s for s in self._subscribed_streams if s]
                    if streams:
                        msg = {"method": "SUBSCRIBE", "params": streams, "id": 1}
                        await ws.send(json.dumps(msg))
                    self.logger.info(f"WS connected, subscribed to {len(streams)} streams")
                    async for raw in ws:
                        await self._handle_ws_message(json.loads(raw))
            except websockets.exceptions.ConnectionClosed as e:
                self.logger.warning(f"WS closed: {e}, reconnecting in {backoff}s")
            except Exception as e:
                self.logger.error(f"WS error: {e}, reconnecting in {backoff}s")
            if self._running:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _heartbeat_watchdog(self) -> None:
        """Force reconnect if no WS message received within timeout window."""
        while self._running:
            await asyncio.sleep(10)
            if self._last_msg_ts and time.time() - self._last_msg_ts > self._WS_TIMEOUT_S:
                self.logger.warning(f"WS heartbeat timeout ({self._WS_TIMEOUT_S}s) — forcing reconnect")
                if self._ws is not None:
                    try:
                        await self._ws.close()
                    except Exception:
                        pass

    async def _handle_ws_message(self, msg: dict) -> None:
        self._last_msg_ts = time.time()
        # Combined stream wraps messages in {"stream":"...", "data":{...}}
        stream_name = msg.get("stream", "")
        data = msg.get("data", msg)
        event_type = data.get("e", "")

        if event_type == "bookTicker" or (not event_type and "b" in data and "a" in data and "s" in data):
            await self._handle_book_ticker(data)
        elif event_type == "depthUpdate":
            await self._handle_depth(data)
        elif not event_type and "bids" in data and "asks" in data:
            # Partial depth snapshot (@depth20@100ms format):
            # {"lastUpdateId":…, "bids":[[price,qty],…], "asks":[[price,qty],…]}
            # Symbol comes from the outer "stream" field, e.g. "btcusdt@depth20@100ms"
            if stream_name:
                raw_sym = stream_name.split("@")[0].upper()
                await self._handle_depth_snapshot(raw_sym, data)
        # Futures user-data events
        elif event_type == "ORDER_TRADE_UPDATE":
            await self._handle_order_update(data.get("o", {}))
        elif event_type == "ACCOUNT_UPDATE":
            await self._handle_account_update(data.get("a", {}))
        # Spot user-data events
        elif event_type == "executionReport":
            await self._handle_order_update(data)          # fields match directly
        elif event_type == "outboundAccountPosition":
            await self._handle_spot_balance_update(data)

    async def _handle_book_ticker(self, d: dict) -> None:
        symbol = self.from_exchange_symbol(d["s"])
        ticker = Ticker(
            exchange=self.exchange,
            symbol=symbol,
            bid=Decimal(d["b"]),
            ask=Decimal(d["a"]),
            last=Decimal(d.get("c") or d["b"]),
            volume_24h=Decimal(d.get("v") or "0"),
            timestamp=time.time(),
        )
        await self._emit(TickerEvent(ticker=ticker))

    async def _handle_depth_snapshot(self, raw_symbol: str, d: dict) -> None:
        """Handle partial depth snapshot from @depth{N}@100ms stream."""
        symbol = self.from_exchange_symbol(raw_symbol)
        bids = [(Decimal(row[0]), Decimal(row[1])) for row in d.get("bids", []) if Decimal(row[1]) > 0]
        asks = [(Decimal(row[0]), Decimal(row[1])) for row in d.get("asks", []) if Decimal(row[1]) > 0]
        if not bids and not asks:
            return
        ob = OrderBook(
            exchange=self.exchange,
            symbol=symbol,
            bids=sorted(bids, reverse=True),
            asks=sorted(asks),
            timestamp=time.time(),
        )
        await self._emit(OrderBookEvent(orderbook=ob))

    async def _handle_depth(self, d: dict) -> None:
        symbol = self.from_exchange_symbol(d["s"])
        bids = [(Decimal(row[0]), Decimal(row[1])) for row in d["b"] if Decimal(row[1]) > 0]
        asks = [(Decimal(row[0]), Decimal(row[1])) for row in d["a"] if Decimal(row[1]) > 0]
        # "T" (transaction time) exists on futures; spot only has "E" (event time)
        ts_ms = d.get("T") or d.get("E") or 0
        ob = OrderBook(
            exchange=self.exchange,
            symbol=symbol,
            bids=sorted(bids, reverse=True),
            asks=sorted(asks),
            timestamp=ts_ms / 1000,
        )
        await self._emit(OrderBookEvent(orderbook=ob))

    async def _handle_order_update(self, o: dict) -> None:
        # avg_price: futures has "ap"; spot derives it from Z (cumQuoteQty) / z (cumQty)
        ap = o.get("ap", "")
        if ap and ap != "0":
            avg_price = Decimal(ap)
        else:
            filled = Decimal(o.get("z", "0"))
            cum_quote = Decimal(o.get("Z", "0"))
            avg_price = (cum_quote / filled) if filled > 0 else Decimal("0")

        order = Order(
            exchange=self.exchange,
            symbol=self.from_exchange_symbol(o.get("s", "")),
            side=OrderSide.BUY if o.get("S") == "BUY" else OrderSide.SELL,
            order_type=self._parse_order_type(o.get("o", "")),
            quantity=Decimal(o.get("q", "0")),
            price=Decimal(o["p"]) if o.get("p") and o["p"] != "0" else None,
            order_id=str(o.get("i", "")),
            client_order_id=o.get("c", ""),
            status=self._parse_order_status(o.get("X", "")),
            filled_qty=Decimal(o.get("z", "0")),
            avg_price=avg_price,
            fee=abs(Decimal(o.get("n", "0"))),  # spot commission can be negative
            fee_ccy=o.get("N", "") or "",       # commission asset (may be BNB)
        )
        await self._emit(OrderUpdateEvent(order=order))

    async def _handle_spot_balance_update(self, data: dict) -> None:
        # outboundAccountPosition: B array has {"a": asset, "f": free, "l": locked}
        for b in data.get("B", []):
            balance = Balance(
                exchange=self.exchange,
                asset=b["a"],
                free=Decimal(b["f"]),
                locked=Decimal(b["l"]),
            )
            await self._emit(BalanceUpdateEvent(balance=balance))

    async def _handle_account_update(self, a: dict) -> None:
        for b in a.get("B", []):
            balance = Balance(
                exchange=self.exchange,
                asset=b["a"],
                free=Decimal(b["wb"]),
                locked=Decimal("0"),
            )
            await self._emit(BalanceUpdateEvent(balance=balance))
        for p in a.get("P", []):
            size = abs(Decimal(p.get("pa", "0")))
            side = PositionSide.LONG if Decimal(p.get("pa", "0")) >= 0 else PositionSide.SHORT
            pos = Position(
                exchange=self.exchange,
                symbol=self.from_exchange_symbol(p.get("s", "")),
                side=side,
                size=size,
                entry_price=Decimal(p.get("ep", "0")),
                mark_price=Decimal(p.get("mp", "0")),
                leverage=1,
                unrealized_pnl=Decimal(p.get("up", "0")),
                margin=Decimal(p.get("iw", "0")),
                liquidation_price=Decimal(p.get("bep", "0")),  # bep = breakeven price (closest to liq in ACCOUNT_UPDATE)
            )
            await self._emit(PositionUpdateEvent(position=pos))

    async def _keepalive_loop(self) -> None:
        while self._running:
            await asyncio.sleep(1800)  # 30 min
            try:
                await self._sync_time()  # correct clock drift to keep signatures valid
            except Exception as e:
                self.logger.debug(f"Time resync failed: {e}")
            try:
                if self._listen_key:
                    if self.market_type == MarketType.FUTURES:
                        path = "/fapi/v1/listenKey"
                    else:
                        path = "/api/v3/userDataStream"
                    await self._request("PUT", path,
                                        params={"listenKey": self._listen_key}, signed=False)
            except Exception as e:
                # Expected while the API key lacks user-data-stream access — keep
                # the ERROR channel for actionable failures only
                self.logger.warning(f"Listen key keepalive failed: {e}")
                self._listen_key = None  # force re-auth on next reconnect

    # ── REST helpers ──────────────────────────────────────────────────────────

    def _sign(self, params: dict) -> dict:
        params["timestamp"] = int(time.time() * 1000) + self._time_offset_ms
        params.setdefault("recvWindow", self._recv_window)
        query = urlencode(params)
        sig = hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        params["signature"] = sig
        return params

    async def _request(
        self, method: str, path: str,
        params: Optional[dict] = None,
        body: Optional[dict] = None,
        signed: bool = True,
        weight: int = 1,
        base_url: Optional[str] = None,
    ) -> dict:
        params = params or {}
        url = f"{base_url or self._rest_base}{path}"
        last_err = None
        for attempt in range(3):
            if self._limiter:
                await self._limiter.acquire(weight)
            # Sign fresh each attempt so the timestamp stays within recvWindow;
            # any newClientOrderId in `params` is preserved → idempotent retry.
            send_params = self._sign(dict(params)) if signed else params
            _t0 = time.time()
            try:
                async with self._session.request(
                    method, url, params=send_params, json=body if method != "GET" else None
                ) as resp:
                    if resp.status in (429, 418):
                        wait = min(int(resp.headers.get("Retry-After", 2 ** attempt)), 60)
                        self.logger.warning(f"Binance rate limited ({resp.status}), retry in {wait}s")
                        await asyncio.sleep(wait)
                        continue
                    data = await resp.json()
                    _latency_ms = (time.time() - _t0) * 1000
                    try:
                        from monitoring.latency import get_monitor
                        mon = get_monitor()
                        if mon:
                            mon.record_rest(self.exchange.value, _latency_ms)
                    except Exception:
                        pass
                    if resp.status >= 400:
                        raise RuntimeError(f"Binance {method} {path}: {resp.status} {data}")
                    return data
            except RuntimeError:
                raise
            except Exception as e:
                last_err = e
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
        raise RuntimeError(f"Binance request failed after 3 attempts: {last_err}")

    async def _get_listen_key(self) -> str:
        if self.market_type == MarketType.FUTURES:
            if self.portfolio_margin:
                data = await self._request("POST", "/papi/v1/listenKey",
                                           signed=False, base_url=self._REST_PAPI)
            else:
                data = await self._request("POST", "/fapi/v1/listenKey", signed=False)
        else:
            data = await self._request("POST", "/api/v3/userDataStream", signed=False)
        return data["listenKey"]

    async def _sync_time(self) -> None:
        prefix = "/fapi/v1" if self.market_type == MarketType.FUTURES else "/api/v3"
        data = await self._request("GET", f"{prefix}/time", signed=False)
        self._time_offset_ms = int(data["serverTime"]) - int(time.time() * 1000)
        self.logger.debug(f"Binance time offset: {self._time_offset_ms}ms")

    async def _load_symbol_rules(self) -> None:
        prefix = "/fapi/v1" if self.market_type == MarketType.FUTURES else "/api/v3"
        data = await self._request("GET", f"{prefix}/exchangeInfo", signed=False, weight=10)
        rules: dict[str, SymbolRule] = {}
        for s in data.get("symbols", []):
            tick = step = min_qty = min_notional = Decimal("0")
            for f in s.get("filters", []):
                ft = f.get("filterType")
                if ft == "PRICE_FILTER":
                    tick = Decimal(f["tickSize"])
                elif ft == "LOT_SIZE":
                    step = Decimal(f["stepSize"])
                    min_qty = Decimal(f["minQty"])
                elif ft in ("MIN_NOTIONAL", "NOTIONAL"):
                    min_notional = Decimal(f.get("notional") or f.get("minNotional") or "0")
            sym = self.from_exchange_symbol(s["symbol"])
            rules[sym] = SymbolRule(tick, step, min_qty, min_notional)
        self._rules = rules

    # ── Trading ──────────────────────────────────────────────────────────────

    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        quantity: Decimal,
        price: Optional[Decimal] = None,
        reduce_only: bool = False,
        post_only: bool = False,
        client_order_id: Optional[str] = None,
    ) -> Order:
        raw_symbol = self.to_exchange_symbol(symbol)
        quantity, price = self._quantize_order(symbol, side, quantity, price)
        cid = client_order_id or gen_client_order_id()
        params: dict = {
            "symbol": raw_symbol,
            "side": side.value.upper(),
            "type": self._to_binance_order_type(order_type),
            "quantity": fmt_decimal(quantity),
            "newClientOrderId": cid,
        }
        if order_type == OrderType.LIMIT and price:
            params["price"] = fmt_decimal(price)
            # GTX = post-only (cancel if would take liquidity); GTC = normal limit
            params["timeInForce"] = "GTX" if post_only else "GTC"
        if reduce_only and self.market_type == MarketType.FUTURES:
            params["reduceOnly"] = "true"

        if self.market_type == MarketType.FUTURES and self.portfolio_margin:
            papi_base = self._REST_PAPI
            order_path = "/papi/v1/um/order"
        elif self.market_type == MarketType.FUTURES:
            papi_base = None
            order_path = "/fapi/v1/order"
        else:
            papi_base = None
            order_path = "/api/v3/order"
        try:
            data = await self._request("POST", order_path, params=params, base_url=papi_base)
        except RuntimeError as e:
            if "duplicate" in str(e).lower():
                self.logger.info(f"Duplicate clientOrderId {cid} — fetching existing order")
                return await self._get_order_by_client_id(symbol, cid)
            raise
        return self._parse_order_response(data, symbol)

    async def _get_order_by_client_id(self, symbol: str, cid: str) -> Order:
        raw = self.to_exchange_symbol(symbol)
        if self.market_type == MarketType.FUTURES and self.portfolio_margin:
            data = await self._request("GET", "/papi/v1/um/order",
                                       params={"symbol": raw, "origClientOrderId": cid},
                                       base_url=self._REST_PAPI)
        else:
            prefix = "/fapi/v1" if self.market_type == MarketType.FUTURES else "/api/v3"
            data = await self._request("GET", f"{prefix}/order",
                                       params={"symbol": raw, "origClientOrderId": cid})
        return self._parse_order_response(data, symbol)

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        raw = self.to_exchange_symbol(symbol)
        try:
            if self.market_type == MarketType.FUTURES and self.portfolio_margin:
                await self._request("DELETE", "/papi/v1/um/order",
                                    params={"symbol": raw, "orderId": order_id},
                                    base_url=self._REST_PAPI)
            else:
                prefix = "/fapi/v1" if self.market_type == MarketType.FUTURES else "/api/v3"
                await self._request("DELETE", f"{prefix}/order",
                                    params={"symbol": raw, "orderId": order_id})
            return True
        except Exception as e:
            self.logger.error(f"Cancel order failed: {e}")
            return False

    async def get_order(self, symbol: str, order_id: str) -> Order:
        raw = self.to_exchange_symbol(symbol)
        if self.market_type == MarketType.FUTURES and self.portfolio_margin:
            data = await self._request("GET", "/papi/v1/um/order",
                                       params={"symbol": raw, "orderId": order_id},
                                       base_url=self._REST_PAPI)
        else:
            prefix = "/fapi/v1" if self.market_type == MarketType.FUTURES else "/api/v3"
            data = await self._request("GET", f"{prefix}/order",
                                       params={"symbol": raw, "orderId": order_id})
        return self._parse_order_response(data, symbol)

    async def get_open_orders(self, symbol: Optional[str] = None) -> list[Order]:
        params = {}
        if symbol:
            params["symbol"] = self.to_exchange_symbol(symbol)
        if self.market_type == MarketType.FUTURES and self.portfolio_margin:
            data = await self._request("GET", "/papi/v1/um/openOrders",
                                       params=params, base_url=self._REST_PAPI)
        else:
            prefix = "/fapi/v1" if self.market_type == MarketType.FUTURES else "/api/v3"
            data = await self._request("GET", f"{prefix}/openOrders", params=params)
        return [self._parse_order_response(o, self.from_exchange_symbol(o["symbol"])) for o in data]

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> int:
        if not symbol:
            orders = await self.get_open_orders()
            symbols = {o.symbol for o in orders}
            count = 0
            for s in symbols:
                count += await self.cancel_all_orders(s)
            return count
        raw = self.to_exchange_symbol(symbol)
        if self.market_type == MarketType.FUTURES and self.portfolio_margin:
            data = await self._request("DELETE", "/papi/v1/um/allOpenOrders",
                                       params={"symbol": raw}, base_url=self._REST_PAPI)
        else:
            prefix = "/fapi/v1" if self.market_type == MarketType.FUTURES else "/api/v3"
            data = await self._request("DELETE", f"{prefix}/allOpenOrders",
                                       params={"symbol": raw})
        return len(data) if isinstance(data, list) else 1

    # ── Account ──────────────────────────────────────────────────────────────

    async def get_positions(self) -> list[Position]:
        if self.market_type == MarketType.SPOT or not self.api_key:
            return []
        if self.portfolio_margin:
            data = await self._request("GET", "/papi/v1/um/positionRisk",
                                       base_url=self._REST_PAPI)
        else:
            data = await self._request("GET", "/fapi/v2/positionRisk")
        positions = []
        for p in data:
            size = abs(Decimal(p["positionAmt"]))
            if size == 0:
                continue
            side = PositionSide.LONG if Decimal(p["positionAmt"]) > 0 else PositionSide.SHORT
            positions.append(Position(
                exchange=self.exchange,
                symbol=self.from_exchange_symbol(p["symbol"]),
                side=side,
                size=size,
                entry_price=Decimal(p["entryPrice"]),
                mark_price=Decimal(p["markPrice"]),
                leverage=int(p["leverage"]),
                unrealized_pnl=Decimal(p["unRealizedProfit"]),
                margin=Decimal(p["isolatedMargin"]),
                liquidation_price=Decimal(p.get("liquidationPrice", "0")),
            ))
        return positions

    async def get_balances(self) -> list[Balance]:
        if not self.api_key:
            return []
        if self.market_type == MarketType.FUTURES:
            if self.portfolio_margin:
                # PAPI balance uses totalWalletBalance instead of balance
                data = await self._request("GET", "/papi/v1/balance",
                                           base_url=self._REST_PAPI)
                return [Balance(
                    exchange=self.exchange,
                    asset=b["asset"],
                    free=Decimal(b["availableBalance"]),
                    locked=Decimal(b["totalWalletBalance"]) - Decimal(b["availableBalance"]),
                ) for b in data if Decimal(b.get("totalWalletBalance", "0")) > 0]
            data = await self._request("GET", "/fapi/v2/balance")
            return [Balance(
                exchange=self.exchange,
                asset=b["asset"],
                free=Decimal(b["availableBalance"]),
                locked=Decimal(b["balance"]) - Decimal(b["availableBalance"]),
            ) for b in data]
        data = await self._request("GET", "/api/v3/account")
        return [Balance(
            exchange=self.exchange,
            asset=b["asset"],
            free=Decimal(b["free"]),
            locked=Decimal(b["locked"]),
        ) for b in data.get("balances", []) if Decimal(b["free"]) + Decimal(b["locked"]) > 0]

    async def get_funding_rates(self) -> list[dict]:
        """Return current funding rates for all futures symbols."""
        if self.market_type != MarketType.FUTURES:
            return []
        try:
            data = await self._request("GET", "/fapi/v1/premiumIndex")
            return [
                {
                    "symbol": self.from_exchange_symbol(d["symbol"]),
                    "funding_rate": float(d.get("lastFundingRate", 0)),
                    "next_funding_time": d.get("nextFundingTime", 0) // 1000,
                }
                for d in (data if isinstance(data, list) else [data])
            ]
        except Exception as e:
            self.logger.warning(f"get_funding_rates failed: {e}")
            return []

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        if self.market_type != MarketType.FUTURES:
            return
        raw = self.to_exchange_symbol(symbol)
        if self.portfolio_margin:
            await self._request("POST", "/papi/v1/um/leverage",
                                 params={"symbol": raw, "leverage": leverage},
                                 base_url=self._REST_PAPI)
        else:
            await self._request("POST", "/fapi/v1/leverage",
                                 params={"symbol": raw, "leverage": leverage})
        self.logger.info(f"Set leverage {symbol} → {leverage}x")

    # ── Parsers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _to_binance_order_type(t: OrderType) -> str:
        return {
            OrderType.MARKET: "MARKET",
            OrderType.LIMIT: "LIMIT",
            OrderType.STOP_MARKET: "STOP_MARKET",
        }[t]

    @staticmethod
    def _parse_order_type(raw: str) -> OrderType:
        return {
            "MARKET": OrderType.MARKET,
            "LIMIT": OrderType.LIMIT,
            "STOP_MARKET": OrderType.STOP_MARKET,
        }.get(raw, OrderType.LIMIT)

    @staticmethod
    def _parse_order_status(raw: str) -> OrderStatus:
        return {
            "NEW": OrderStatus.OPEN,
            "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
            "FILLED": OrderStatus.FILLED,
            "CANCELED": OrderStatus.CANCELLED,
            "REJECTED": OrderStatus.REJECTED,
            "EXPIRED": OrderStatus.CANCELLED,
        }.get(raw, OrderStatus.PENDING)

    def _parse_order_response(self, d: dict, symbol: str) -> Order:
        return Order(
            exchange=self.exchange,
            symbol=symbol,
            side=OrderSide.BUY if d.get("side") == "BUY" else OrderSide.SELL,
            order_type=self._parse_order_type(d.get("type", "")),
            quantity=Decimal(str(d.get("origQty", "0"))),
            price=Decimal(str(d["price"])) if d.get("price") and str(d["price"]) != "0" else None,
            order_id=str(d.get("orderId", "")),
            client_order_id=d.get("clientOrderId", ""),
            status=self._parse_order_status(d.get("status", "")),
            filled_qty=Decimal(str(d.get("executedQty", "0"))),
            avg_price=Decimal(str(d.get("avgPrice", "0"))),
        )
