from __future__ import annotations
import asyncio
import base64
import hashlib
import hmac
import json
import time
from decimal import Decimal
from typing import Optional

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


class OKXConnector(BaseConnector):
    """OKX USDT-Margined Swap connector (supports spot too via market_type=SPOT)."""

    _REST_BASE = "https://www.okx.com"
    _REST_BASE_TEST = "https://www.okx.com"   # OKX testnet uses same host + demo header
    _WS_PUBLIC = "wss://ws.okx.com:8443/ws/v5/public"
    _WS_PRIVATE = "wss://ws.okx.com:8443/ws/v5/private"
    _WS_PUBLIC_TEST = "wss://wspap.okx.com:8443/ws/v5/public?brokerId=9999"
    _WS_PRIVATE_TEST = "wss://wspap.okx.com:8443/ws/v5/private?brokerId=9999"

    def __init__(
        self,
        api_key: str,
        secret: str,
        passphrase: str,
        market_type: MarketType = MarketType.SWAP,
        testnet: bool = False,
    ):
        super().__init__(api_key, secret, market_type, testnet)
        self.passphrase = passphrase
        self._session: Optional[aiohttp.ClientSession] = None
        self._pub_ws: Optional[websockets.WebSocketClientProtocol] = None
        self._priv_ws: Optional[websockets.WebSocketClientProtocol] = None
        self._pub_subs: list[dict] = []
        self._pub_task: Optional[asyncio.Task] = None
        self._priv_task: Optional[asyncio.Task] = None
        self._priv_auth_warned: bool = False  # suppress repeated 60011 log spam
        # REST throttle — OKX trade endpoints allow ~60 req/2s; stay well under
        self._limiter = AsyncRateLimiter(rate_per_sec=15, capacity=30)
        self._last_time_sync: float = 0.0

    @property
    def exchange(self) -> Exchange:
        return Exchange.OKX_SPOT if self.market_type == MarketType.SPOT else Exchange.OKX

    @property
    def _rest_base(self) -> str:
        return self._REST_BASE_TEST if self.testnet else self._REST_BASE

    def _demo_header(self) -> dict:
        return {"x-simulated-trading": "1"} if self.testnet else {}

    # ── Auth ──────────────────────────────────────────────────────────────────

    def _sign_rest(self, method: str, path: str, body: str = "") -> dict:
        now = time.time() + self._time_offset_ms / 1000.0
        ts_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + f".{int(now*1000)%1000:03d}Z"
        prehash = ts_iso + method.upper() + path + body
        sig = base64.b64encode(
            hmac.new(self.secret.encode(), prehash.encode(), hashlib.sha256).digest()
        ).decode()
        return {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": sig,
            "OK-ACCESS-TIMESTAMP": ts_iso,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        }

    def _ws_login_msg(self) -> dict:
        ts = str(int(time.time() + self._time_offset_ms / 1000.0))
        prehash = ts + "GET" + "/users/self/verify"
        sig = base64.b64encode(
            hmac.new(self.secret.encode(), prehash.encode(), hashlib.sha256).digest()
        ).decode()
        return {
            "op": "login",
            "args": [{"apiKey": self.api_key, "passphrase": self.passphrase,
                      "timestamp": ts, "sign": sig}],
        }

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        self._running = True
        ssl_ctx = make_ssl_context()
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
            connector=aiohttp.TCPConnector(ssl=ssl_ctx),
        )
        self._last_msg_ts: float = time.time()
        self._WS_TIMEOUT_S: int = 45
        try:
            await self._sync_time()
        except Exception as e:
            self.logger.warning(f"Server time sync failed (using local clock): {e}")
        try:
            await self._load_symbol_rules()
            self.logger.info(f"Loaded trading rules for {len(self._rules)} symbols")
        except Exception as e:
            self.logger.warning(f"Symbol rules load failed (orders sent unquantized): {e}")
        self._pub_task = asyncio.create_task(self._pub_ws_loop())
        self._heartbeat_task: Optional[asyncio.Task] = asyncio.create_task(self._heartbeat_watchdog())
        if self.api_key:
            self._priv_task = asyncio.create_task(self._priv_ws_loop())
        self.logger.info("OKX connector connected")
        await self._emit(ConnectorReadyEvent(exchange=self.exchange))

    async def disconnect(self) -> None:
        self._running = False
        for task in (self._pub_task, self._priv_task, getattr(self, "_heartbeat_task", None)):
            if task:
                task.cancel()
        if self._session:
            await self._session.close()
        self.logger.info("OKX connector disconnected")

    # ── Subscriptions ────────────────────────────────────────────────────────

    async def subscribe_ticker(self, symbol: str) -> None:
        raw = self.to_exchange_symbol(symbol)
        sub = {"channel": "tickers", "instId": raw}
        if sub not in self._pub_subs:
            self._pub_subs.append(sub)
            if self._pub_ws:
                await self._pub_ws.send(json.dumps({"op": "subscribe", "args": [sub]}))

    async def subscribe_orderbook(self, symbol: str, depth: int = 20) -> None:
        raw = self.to_exchange_symbol(symbol)
        channel = "books5" if depth <= 5 else "books"
        sub = {"channel": channel, "instId": raw}
        if sub not in self._pub_subs:
            self._pub_subs.append(sub)
            if self._pub_ws:
                await self._pub_ws.send(json.dumps({"op": "subscribe", "args": [sub]}))

    # ── Public WS loop ────────────────────────────────────────────────────────

    async def _heartbeat_watchdog(self) -> None:
        while self._running:
            await asyncio.sleep(10)
            # Resync clock every ~30 min to keep REST signatures within OKX's window
            if self.api_key and time.time() - self._last_time_sync > 1800:
                try:
                    await self._sync_time()
                except Exception as e:
                    self.logger.debug(f"Time resync failed: {e}")
            if self._last_msg_ts and time.time() - self._last_msg_ts > self._WS_TIMEOUT_S:
                self.logger.warning(f"OKX WS heartbeat timeout ({self._WS_TIMEOUT_S}s) — forcing reconnect")
                for ws in (self._pub_ws, self._priv_ws):
                    if ws is not None:
                        try:
                            await ws.close()
                        except Exception:
                            pass

    async def _pub_ws_loop(self) -> None:
        url = self._WS_PUBLIC_TEST if self.testnet else self._WS_PUBLIC
        backoff = 1
        while self._running:
            try:
                async with websockets.connect(url, ping_interval=20,
                                              ssl=make_ssl_context()) as ws:
                    self._pub_ws = ws
                    backoff = 1
                    # Re-subscribe on reconnect
                    if self._pub_subs:
                        await ws.send(json.dumps({"op": "subscribe", "args": self._pub_subs}))
                    async for raw in ws:
                        await self._handle_pub_message(json.loads(raw))
            except websockets.exceptions.ConnectionClosed as e:
                self.logger.warning(f"OKX pub WS closed: {e}, retry in {backoff}s")
            except Exception as e:
                self.logger.error(f"OKX pub WS error: {e}, retry in {backoff}s")
            if self._running:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _handle_pub_message(self, msg: dict) -> None:
        self._last_msg_ts = time.time()
        if msg.get("event") == "error":
            self.logger.error(f"OKX pub WS error event: {msg}")
            return
        channel = msg.get("arg", {}).get("channel", "")
        data_list = msg.get("data", [])
        for d in data_list:
            if channel == "tickers":
                await self._handle_ticker(d)
            elif channel.startswith("books"):
                await self._handle_orderbook(d, msg.get("arg", {}).get("instId", ""))

    async def _handle_ticker(self, d: dict) -> None:
        symbol = self.from_exchange_symbol(d["instId"])
        fallback = d.get("last") or d.get("close") or "0"
        ticker = Ticker(
            exchange=self.exchange,
            symbol=symbol,
            bid=Decimal(d["bidPx"]) if d.get("bidPx") else Decimal(fallback),
            ask=Decimal(d["askPx"]) if d.get("askPx") else Decimal(fallback),
            last=Decimal(fallback),
            volume_24h=Decimal(d.get("vol24h", "0")),
            timestamp=int(d["ts"]) / 1000,
        )
        await self._emit(TickerEvent(ticker=ticker))

    async def _handle_orderbook(self, d: dict, inst_id: str) -> None:
        symbol = self.from_exchange_symbol(inst_id)
        bids = [(Decimal(p), Decimal(q)) for p, q, *_ in d.get("bids", []) if Decimal(q) > 0]
        asks = [(Decimal(p), Decimal(q)) for p, q, *_ in d.get("asks", []) if Decimal(q) > 0]
        ob = OrderBook(
            exchange=self.exchange,
            symbol=symbol,
            bids=sorted(bids, reverse=True),
            asks=sorted(asks),
            timestamp=int(d["ts"]) / 1000,
        )
        await self._emit(OrderBookEvent(orderbook=ob))

    # ── Private WS loop ───────────────────────────────────────────────────────

    async def _priv_ws_loop(self) -> None:
        url = self._WS_PRIVATE_TEST if self.testnet else self._WS_PRIVATE
        backoff = 1
        while self._running:
            try:
                async with websockets.connect(url, ping_interval=20,
                                              ssl=make_ssl_context()) as ws:
                    self._priv_ws = ws
                    backoff = 1
                    self._priv_auth_warned = False  # reset on each new connection
                    await ws.send(json.dumps(self._ws_login_msg()))
                    # Subscribe to orders and positions
                    inst_type = "SWAP" if self.market_type == MarketType.SWAP else "SPOT"
                    await ws.send(json.dumps({
                        "op": "subscribe",
                        "args": [
                            {"channel": "orders", "instType": inst_type},
                            {"channel": "positions", "instType": inst_type},
                            {"channel": "account"},
                        ],
                    }))
                    async for raw in ws:
                        await self._handle_priv_message(json.loads(raw))
            except websockets.exceptions.ConnectionClosed as e:
                self.logger.warning(f"OKX priv WS closed: {e}, retry in {backoff}s")
            except Exception as e:
                self.logger.error(f"OKX priv WS error: {e}, retry in {backoff}s")
            if self._running:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _handle_priv_message(self, msg: dict) -> None:
        self._last_msg_ts = time.time()
        if msg.get("event") in ("login", "subscribe"):
            self.logger.debug(f"OKX priv event: {msg.get('event')}")
            return
        if msg.get("event") == "error":
            code = str(msg.get("code", ""))
            if code == "60011":
                # Auth failure (IP whitelist / key permissions) — warn once, then silence
                if not self._priv_auth_warned:
                    self.logger.warning(
                        "OKX priv WS auth failed (60011 Please log in) — "
                        "check API key permissions / IP whitelist. "
                        "Market data is unaffected; private order/position push disabled."
                    )
                    self._priv_auth_warned = True
            else:
                self.logger.error(f"OKX priv WS error: {msg}")
            return
        channel = msg.get("arg", {}).get("channel", "")
        for d in msg.get("data", []):
            if channel == "orders":
                await self._emit(OrderUpdateEvent(order=self._parse_ws_order(d)))
            elif channel == "positions":
                await self._emit(PositionUpdateEvent(position=self._parse_ws_position(d)))
            elif channel == "account":
                for detail in d.get("details", []):
                    balance = Balance(
                        exchange=self.exchange,
                        asset=detail["ccy"],
                        free=Decimal(detail.get("availBal", "0")),
                        locked=Decimal(detail.get("frozenBal", "0")),
                    )
                    await self._emit(BalanceUpdateEvent(balance=balance))

    # ── REST helpers ──────────────────────────────────────────────────────────

    async def _request(
        self, method: str, path: str,
        params: Optional[dict] = None,
        body: Optional[dict] = None,
        weight: int = 1,
    ) -> dict:
        body_str = json.dumps(body) if body else ""
        full_path = path
        if params:
            from urllib.parse import urlencode
            full_path += "?" + urlencode(params)
        url = f"{self._rest_base}{full_path}"
        last_err = None
        for attempt in range(3):
            if self._limiter:
                await self._limiter.acquire(weight)
            headers = {**self._sign_rest(method, full_path, body_str), **self._demo_header()}
            try:
                async with self._session.request(
                    method, url, headers=headers, data=body_str or None
                ) as resp:
                    if resp.status == 429:
                        wait = min(int(resp.headers.get("Retry-After", 2 ** attempt)), 60)
                        self.logger.warning(f"OKX rate limited (HTTP 429), retry in {wait}s")
                        await asyncio.sleep(wait)
                        continue
                    data = await resp.json()
                    if data.get("code") == "50011" and attempt < 2:
                        self.logger.warning(f"OKX rate limit (50011), retry in {2**attempt}s")
                        await asyncio.sleep(2 ** attempt)
                        continue
                    if data.get("code") != "0":
                        raise RuntimeError(f"OKX {method} {path}: {data.get('code')} {data.get('msg')}")
                    return data
            except RuntimeError:
                raise
            except Exception as e:
                last_err = e
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
        raise RuntimeError(f"OKX request failed after 3 attempts: {last_err}")

    async def _sync_time(self) -> None:
        data = await self._request("GET", "/api/v5/public/time")
        server_ms = int(data["data"][0]["ts"])
        self._time_offset_ms = server_ms - int(time.time() * 1000)
        self._last_time_sync = time.time()
        self.logger.debug(f"OKX time offset: {self._time_offset_ms}ms")

    async def _load_symbol_rules(self) -> None:
        inst_type = "SWAP" if self.market_type == MarketType.SWAP else "SPOT"
        data = await self._request("GET", "/api/v5/public/instruments",
                                   params={"instType": inst_type})
        rules: dict[str, SymbolRule] = {}
        for s in data.get("data", []):
            sym = self.from_exchange_symbol(s.get("instId", ""))
            rules[sym] = SymbolRule(
                tick_size=Decimal(s.get("tickSz") or "0"),
                step_size=Decimal(s.get("lotSz") or "0"),
                min_qty=Decimal(s.get("minSz") or "0"),
                min_notional=Decimal("0"),  # OKX has no flat min-notional filter
                # SWAP sizes are in CONTRACTS; ctVal = coin amount per contract
                contract_val=Decimal(s.get("ctVal") or "0"),
            )
        self._rules = rules

    def _ctval(self, symbol: str) -> Decimal:
        """Coin amount per contract for OKX swaps; 1 for spot / when unknown."""
        r = self._rules.get(symbol)
        if r and r.contract_val > 0:
            return r.contract_val
        return Decimal("1")

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
        raw = self.to_exchange_symbol(symbol)
        # `quantity` is a COIN amount system-wide. OKX swap order size (`sz`) is in
        # CONTRACTS, so convert, quantize against lotSz/minSz (contract units), and
        # report the realized coin amount back so the rest of the system stays in coin.
        ctv = self._ctval(symbol)
        contracts = quantity / ctv
        contracts, price = self._quantize_order(symbol, side, contracts, price)
        quantity = contracts * ctv          # realized coin amount after lot rounding
        cid = client_order_id or gen_client_order_id()
        td_mode = "cross" if self.market_type == MarketType.SWAP else "cash"
        # OKX: use ordType="post_only" for maker-only orders (requires price)
        ord_type = "post_only" if (post_only and order_type == OrderType.LIMIT and price) \
                   else self._to_okx_order_type(order_type)
        body: dict = {
            "instId": raw,
            "tdMode": td_mode,
            "side": side.value,
            "ordType": ord_type,
            "sz": fmt_decimal(contracts),
            "clOrdId": cid,
        }
        if order_type == OrderType.LIMIT and price:
            body["px"] = fmt_decimal(price)
        if reduce_only:
            body["reduceOnly"] = "true"
        data = await self._request("POST", "/api/v5/trade/order", body=body)
        result = data["data"][0]
        if result.get("sCode") != "0":
            # Idempotency: duplicate clOrdId means a retried POST already placed
            # this order — fetch and return it instead of erroring out.
            if result.get("sCode") in ("51020", "51400") or "exist" in str(result.get("sMsg", "")).lower():
                self.logger.info(f"Duplicate clOrdId {cid} — fetching existing order")
                return await self._get_order_by_client_id(symbol, cid)
            raise RuntimeError(f"OKX order failed: {result.get('sMsg')}")
        return Order(
            exchange=self.exchange,
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            order_id=result["ordId"],
            client_order_id=result.get("clOrdId", "") or cid,
            status=OrderStatus.OPEN,
        )

    async def _get_order_by_client_id(self, symbol: str, cid: str) -> Order:
        raw = self.to_exchange_symbol(symbol)
        data = await self._request("GET", "/api/v5/trade/order",
                                   params={"instId": raw, "clOrdId": cid})
        return self._parse_rest_order(data["data"][0], symbol)

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        raw = self.to_exchange_symbol(symbol)
        try:
            data = await self._request(
                "POST", "/api/v5/trade/cancel-order",
                body={"instId": raw, "ordId": order_id},
            )
            return data["data"][0].get("sCode") == "0"
        except Exception as e:
            self.logger.error(f"OKX cancel order failed: {e}")
            return False

    async def get_order(self, symbol: str, order_id: str) -> Order:
        raw = self.to_exchange_symbol(symbol)
        data = await self._request(
            "GET", "/api/v5/trade/order",
            params={"instId": raw, "ordId": order_id},
        )
        return self._parse_rest_order(data["data"][0], symbol)

    async def get_open_orders(self, symbol: Optional[str] = None) -> list[Order]:
        params: dict = {}
        if symbol:
            params["instId"] = self.to_exchange_symbol(symbol)
        else:
            inst_type = "SWAP" if self.market_type == MarketType.SWAP else "SPOT"
            params["instType"] = inst_type
        data = await self._request("GET", "/api/v5/trade/orders-pending", params=params)
        return [
            self._parse_rest_order(o, self.from_exchange_symbol(o["instId"]))
            for o in data.get("data", [])
        ]

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> int:
        orders = await self.get_open_orders(symbol)
        if not orders:
            return 0
        cancel_reqs = [
            {"instId": self.to_exchange_symbol(o.symbol), "ordId": o.order_id}
            for o in orders if o.order_id
        ]
        # OKX batch cancel supports up to 20 per request
        count = 0
        for i in range(0, len(cancel_reqs), 20):
            batch = cancel_reqs[i:i+20]
            data = await self._request("POST", "/api/v5/trade/cancel-batch-orders", body=batch)
            count += sum(1 for r in data.get("data", []) if r.get("sCode") == "0")
        return count

    # ── Account ──────────────────────────────────────────────────────────────

    async def get_positions(self) -> list[Position]:
        if self.market_type == MarketType.SPOT or not self.api_key:
            return []
        inst_type = "SWAP"
        data = await self._request("GET", "/api/v5/account/positions",
                                   params={"instType": inst_type})
        return [self._parse_rest_position(p) for p in data.get("data", [])
                if Decimal(p.get("pos", "0")) != 0]

    async def get_balances(self) -> list[Balance]:
        if not self.api_key:
            return []
        data = await self._request("GET", "/api/v5/account/balance")
        balances = []
        for acct in data.get("data", []):
            for detail in acct.get("details", []):
                total = Decimal(detail.get("cashBal", "0"))
                if total == 0:
                    continue
                balances.append(Balance(
                    exchange=self.exchange,
                    asset=detail["ccy"],
                    free=Decimal(detail.get("availBal", "0")),
                    locked=Decimal(detail.get("frozenBal", "0")),
                ))
        return balances

    async def get_funding_rates(self, symbols: Optional[list[str]] = None) -> list[dict]:
        """Return current funding rates. OKX requires per-instrument queries (no bulk endpoint)."""
        if self.market_type not in (MarketType.SWAP,):
            return []
        if not symbols:
            return []
        result = []
        for sym in symbols:
            inst_id = self.to_exchange_symbol(sym)  # e.g. BTC-USDT-SWAP
            try:
                data = await self._request(
                    "GET", "/api/v5/public/funding-rate", params={"instId": inst_id}
                )
                for d in data.get("data", []):
                    result.append({
                        "symbol": self.from_exchange_symbol(d.get("instId", "")),
                        "funding_rate": float(d.get("fundingRate", 0)),
                        "next_funding_time": int(d.get("nextFundingTime", 0)) // 1000,
                    })
            except Exception as e:
                self.logger.warning(f"get_funding_rates({sym}) failed: {e}")
        return result

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        if self.market_type == MarketType.SPOT:
            return
        raw = self.to_exchange_symbol(symbol)
        await self._request("POST", "/api/v5/account/set-leverage", body={
            "instId": raw,
            "lever": str(leverage),
            "mgnMode": "cross",
        })
        self.logger.info(f"Set leverage {symbol} → {leverage}x")

    # ── Parsers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _to_okx_order_type(t: OrderType) -> str:
        return {
            OrderType.MARKET: "market",
            OrderType.LIMIT: "limit",
            OrderType.STOP_MARKET: "market",
        }[t]

    @staticmethod
    def _parse_okx_status(raw: str) -> OrderStatus:
        return {
            "live": OrderStatus.OPEN,
            "partially_filled": OrderStatus.PARTIALLY_FILLED,
            "filled": OrderStatus.FILLED,
            "cancelled": OrderStatus.CANCELLED,
        }.get(raw, OrderStatus.PENDING)

    def _parse_rest_order(self, d: dict, symbol: str) -> Order:
        ctv = self._ctval(symbol)  # contracts → coin
        return Order(
            exchange=self.exchange,
            symbol=symbol,
            side=OrderSide.BUY if d.get("side") == "buy" else OrderSide.SELL,
            order_type=OrderType.LIMIT if d.get("ordType") == "limit" else OrderType.MARKET,
            quantity=Decimal(d.get("sz", "0")) * ctv,
            price=Decimal(d["px"]) if d.get("px") and d["px"] != "" else None,
            order_id=d.get("ordId", ""),
            client_order_id=d.get("clOrdId", ""),
            status=self._parse_okx_status(d.get("state", "")),
            filled_qty=Decimal(d.get("fillSz", "0")) * ctv,
            avg_price=Decimal(d.get("avgPx") or "0"),
            fee=abs(Decimal(d.get("fee") or "0")),
            fee_ccy=d.get("feeCcy", ""),
        )

    def _parse_ws_order(self, d: dict) -> Order:
        return self._parse_rest_order(d, self.from_exchange_symbol(d.get("instId", "")))

    def _parse_rest_position(self, p: dict) -> Position:
        symbol = self.from_exchange_symbol(p.get("instId", ""))
        ctv = self._ctval(symbol)  # contracts → coin
        size = abs(Decimal(p.get("pos", "0"))) * ctv
        side = PositionSide.LONG if Decimal(p.get("pos", "0")) > 0 else PositionSide.SHORT
        return Position(
            exchange=self.exchange,
            symbol=symbol,
            side=side,
            size=size,
            entry_price=Decimal(p.get("avgPx") or "0"),
            mark_price=Decimal(p.get("markPx") or "0"),
            leverage=int(p.get("lever") or 1),
            unrealized_pnl=Decimal(p.get("upl") or "0"),
            margin=Decimal(p.get("margin") or "0"),
            liquidation_price=Decimal(p.get("liqPx") or "0"),
        )

    def _parse_ws_position(self, p: dict) -> Position:
        return self._parse_rest_position(p)
