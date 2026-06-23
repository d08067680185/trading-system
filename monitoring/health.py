"""
Operational health monitor.

Consolidates the live signals that decide whether the system can actually trade
RIGHT NOW — market-data freshness, event-loop responsiveness, event-queue
saturation and connector connectivity — into a single status
(ok / degraded / critical) with per-component detail.

This is deliberately distinct from two existing layers:
  * RiskManager gates *trades* on PnL / exposure — it says nothing about whether
    the data pipeline is alive.
  * The engine's per-order staleness guard silently blocks one order at a time
    on a frozen quote, but never raises its hand to say "the whole feed is dead".

Nothing else answers "is the pipeline healthy, and has it degraded?". A trading
system whose WebSocket has silently frozen will simply stop placing orders and
look idle — indistinguishable from "no opportunities" until you inspect it. This
module turns that silent failure into an explicit, alertable status.

Runs as a background heartbeat (same shape as MarginMonitor): every `interval_s`
it measures event-loop lag, evaluates health, and on a status *transition* fires
a throttled Telegram alert via ``engine._notifier`` and logs it.
"""
from __future__ import annotations
import asyncio
import logging
import time
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from core.engine import TradingEngine

logger = logging.getLogger("HealthMonitor")

# Ordering so we can take the worst across components.
_RANK = {"ok": 0, "idle": 0, "degraded": 1, "critical": 2}


def _worst(a: str, b: str) -> str:
    return a if _RANK.get(a, 0) >= _RANK.get(b, 0) else b


class HealthMonitor:
    def __init__(
        self,
        engine: "TradingEngine",
        *,
        stale_feed_warn_s: float = 10.0,
        stale_feed_crit_s: float = 30.0,
        loop_lag_warn_s: float = 0.5,
        loop_lag_crit_s: float = 2.0,
        queue_warn_pct: float = 0.70,
        queue_crit_pct: float = 0.90,
        interval_s: float = 5.0,
        auto_heal: bool = True,
        heal_after_crit_s: float = 30.0,
        heal_cooldown_s: float = 120.0,
        conn_backoff_base_s: float = 15.0,
        conn_backoff_cap_s: float = 300.0,
        storage=None,
    ):
        self._engine = engine
        self._storage = storage
        self._feed_warn = stale_feed_warn_s
        self._feed_crit = stale_feed_crit_s
        self._lag_warn = loop_lag_warn_s
        self._lag_crit = loop_lag_crit_s
        self._q_warn = queue_warn_pct
        self._q_crit = queue_crit_pct
        self._interval = interval_s
        # Self-healing: reconnect an exchange whose feed has been critical (frozen
        # while the connector still reports "connected" — the silent-WS-death case)
        # for at least heal_after_crit_s, rate-limited per exchange by heal_cooldown_s.
        self._auto_heal = auto_heal
        self._heal_after_crit_s = heal_after_crit_s
        self._heal_cooldown_s = heal_cooldown_s
        self._feed_crit_since: dict[str, float] = {}  # exchange → first-critical ts
        self._last_heal: dict[str, float] = {}        # exchange → last reconnect ts
        # Backoff state for reconnecting connectors stuck in error/disconnected:
        # retry delay doubles per failed attempt from base up to cap.
        self._conn_base = conn_backoff_base_s
        self._conn_cap = conn_backoff_cap_s
        self._conn_retry: dict[str, int] = {}         # exchange → consecutive attempts
        self._last_conn_heal: dict[str, float] = {}   # exchange → last reconnect attempt ts

        self._task: Optional[asyncio.Task] = None
        self._loop_lag_s: float = 0.0
        self._last_report: Optional[dict] = None
        self._last_status: str = "ok"
        self._last_alerts: dict[str, float] = {}   # status → last alert ts
        self._alert_cooldown = 300                 # 5 min between repeat alerts
        self._broadcast = None                     # optional async fn(msg: dict) for WS push

    def set_broadcast(self, fn) -> None:
        """Register an async callable used to push a `health_update` WS message on
        every status transition (e.g. ws_manager.broadcast). Optional."""
        self._broadcast = fn

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())
        logger.info("HealthMonitor started")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def get_report(self) -> dict:
        """Latest computed report (recomputes on demand if the loop hasn't run yet)."""
        return self._last_report or self.snapshot()

    # ── Pure evaluation (no engine access → unit-testable) ─────────────────────

    def evaluate(
        self,
        *,
        active: bool,
        connector_states: dict[str, str],
        feed_ages: dict[str, Optional[float]],
        loop_lag_s: float,
        queue_size: int,
        queue_max: int,
    ) -> dict:
        """Build a health report from an explicit snapshot of system state.

        ``feed_ages`` maps an exchange value to the age (seconds) of its freshest
        ticker across all symbols, or None if no quote has ever arrived.
        """
        now = time.time()
        components: list[dict] = []

        # ── Connectors ──────────────────────────────────────────────────────
        connected = [ex for ex, st in connector_states.items() if st == "connected"]
        if not connector_states:
            conn_status, conn_detail = "idle", "no connectors registered"
        elif not active:
            conn_status, conn_detail = "idle", "engine paused"
        elif not connected:
            conn_status = "critical"
            conn_detail = "no connectors connected"
        elif len(connected) < len(connector_states):
            down = [ex for ex, st in connector_states.items() if st != "connected"]
            conn_status = "degraded"
            conn_detail = f"down: {', '.join(sorted(down))}"
        else:
            conn_status, conn_detail = "ok", "all connected"
        components.append({
            "name": "connectors", "status": conn_status, "detail": conn_detail,
            "metrics": {"states": dict(connector_states)},
        })

        # ── Market-data feeds (only meaningful while active) ────────────────
        if not active:
            components.append({
                "name": "feeds", "status": "idle", "detail": "engine paused",
                "metrics": {"ages_s": {}},
            })
        else:
            feed_status = "ok"
            worst_ex: Optional[str] = None
            ages_out: dict[str, Optional[float]] = {}
            crit_ex: list[str] = []
            deg_ex: list[str] = []
            for ex in connected:
                age = feed_ages.get(ex)
                ages_out[ex] = None if age is None else round(age, 1)
                if age is None or age > self._feed_crit:
                    this = "critical"
                    crit_ex.append(ex)
                elif age > self._feed_warn:
                    this = "degraded"
                    deg_ex.append(ex)
                else:
                    this = "ok"
                if _RANK[this] > _RANK[feed_status]:
                    feed_status, worst_ex = this, ex
            if feed_status == "ok":
                detail = "all feeds fresh" if connected else "no connected exchange"
            else:
                a = feed_ages.get(worst_ex)
                a_str = "never" if a is None else f"{a:.1f}s old"
                detail = f"{worst_ex} feed {a_str}"
            components.append({
                "name": "feeds", "status": feed_status, "detail": detail,
                "metrics": {"ages_s": ages_out, "critical": crit_ex, "degraded": deg_ex},
            })

        # ── Event-loop responsiveness ───────────────────────────────────────
        if not active:
            loop_status, loop_detail = "idle", "engine paused"
        elif loop_lag_s > self._lag_crit:
            loop_status = "critical"
            loop_detail = f"loop lag {loop_lag_s * 1000:.0f}ms"
        elif loop_lag_s > self._lag_warn:
            loop_status = "degraded"
            loop_detail = f"loop lag {loop_lag_s * 1000:.0f}ms"
        else:
            loop_status = "ok"
            loop_detail = f"loop lag {loop_lag_s * 1000:.0f}ms"
        components.append({
            "name": "event_loop", "status": loop_status, "detail": loop_detail,
            "metrics": {"lag_ms": round(loop_lag_s * 1000, 1)},
        })

        # ── Event-queue saturation ──────────────────────────────────────────
        q_pct = (queue_size / queue_max) if queue_max else 0.0
        if not active:
            q_status, q_detail = "idle", "engine paused"
        elif q_pct >= self._q_crit:
            q_status = "critical"
            q_detail = f"queue {queue_size}/{queue_max} ({q_pct:.0%})"
        elif q_pct >= self._q_warn:
            q_status = "degraded"
            q_detail = f"queue {queue_size}/{queue_max} ({q_pct:.0%})"
        else:
            q_status = "ok"
            q_detail = f"queue {queue_size}/{queue_max} ({q_pct:.0%})"
        components.append({
            "name": "event_queue", "status": q_status, "detail": q_detail,
            "metrics": {"size": queue_size, "max": queue_max, "pct": round(q_pct, 3)},
        })

        # ── Overall ─────────────────────────────────────────────────────────
        if not active:
            overall = "paused"
        else:
            overall = "ok"
            for c in components:
                overall = _worst(overall, c["status"])

        return {
            "status": overall,
            "ts": now,
            "active": active,
            "loop_lag_ms": round(loop_lag_s * 1000, 1),
            "components": components,
        }

    # ── Engine-bound snapshot ──────────────────────────────────────────────────

    def snapshot(self) -> dict:
        eng = self._engine
        states = dict(getattr(eng, "_connector_states", {}))
        queue = getattr(eng, "event_queue", None)
        qsize = queue.qsize() if queue is not None else 0
        qmax = (queue.maxsize if queue is not None else 0) or 0
        return self.evaluate(
            active=eng.is_active,
            connector_states=states,
            feed_ages=self._feed_ages(),
            loop_lag_s=self._loop_lag_s,
            queue_size=qsize,
            queue_max=qmax,
        )

    def _feed_ages(self) -> dict[str, Optional[float]]:
        """Freshest ticker age (seconds) per exchange from engine._last_ticker."""
        now = time.time()
        ages: dict[str, Optional[float]] = {}
        last = getattr(self._engine, "_last_ticker", {})
        for (ex, _sym), entry in last.items():
            age = now - entry[3]
            if ex not in ages or (ages[ex] is not None and age < ages[ex]):
                ages[ex] = age
        return ages

    # ── Background heartbeat ───────────────────────────────────────────────────

    async def _loop(self) -> None:
        await asyncio.sleep(self._interval)   # let connectors settle before first eval
        while True:
            t0 = time.monotonic()
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                raise
            # Overshoot beyond the requested sleep = how long the loop was busy /
            # blocked = event-loop lag. The single most honest liveness signal.
            self._loop_lag_s = max(0.0, (time.monotonic() - t0) - self._interval)
            try:
                report = self.snapshot()
                self._last_report = report
                self._maybe_alert(report)
                await self._heal(report)
            except Exception as e:
                logger.warning(f"HealthMonitor evaluation error: {e}")

    def _maybe_alert(self, report: dict) -> None:
        status = report["status"]
        prev = self._last_status

        if status == prev:
            # Still broken — re-alert occasionally so a lingering outage isn't forgotten.
            if status == "critical" and self._should_alert("critical", time.time()):
                self._send_alert(report, repeat=True)
            return

        self._last_status = status
        # Push the new status to any connected UI immediately (don't wait for its poll).
        if self._broadcast is not None:
            asyncio.create_task(self._broadcast({"type": "health_update", "data": report}))
        notifier = getattr(self._engine, "_notifier", None)

        if status in ("degraded", "critical"):
            if self._should_alert(status, time.time()):
                self._send_alert(report)
        elif status == "ok" and prev in ("degraded", "critical"):
            self._last_alerts.clear()
            if notifier:
                asyncio.create_task(notifier.send("✅ System health recovered to OK"))

    def _send_alert(self, report: dict, repeat: bool = False) -> None:
        notifier = getattr(self._engine, "_notifier", None)
        if not notifier:
            return
        bad = [c for c in report["components"] if c["status"] in ("degraded", "critical")]
        lines = "\n".join(f"• {c['name']}: {c['status']} — {c['detail']}" for c in bad)
        icon = "🚨" if report["status"] == "critical" else "⚠️"
        prefix = "STILL " if repeat else ""
        asyncio.create_task(notifier.send(
            f"{icon} {prefix}System health {report['status'].upper()}\n{lines}"
        ))

    def _should_alert(self, key: str, now: float) -> bool:
        last = self._last_alerts.get(key, 0)
        if now - last >= self._alert_cooldown:
            self._last_alerts[key] = now
            return True
        return False

    # ── Self-healing ───────────────────────────────────────────────────────────

    def _heal_targets(self, report: dict, now: float) -> list[str]:
        """Exchanges whose feed has stayed critical long enough to warrant a
        reconnect, respecting per-exchange cooldown. Pure: only reads `report`
        and internal timers, so it is unit-testable without an engine."""
        if not self._auto_heal:
            return []
        feeds = next((c for c in report["components"] if c["name"] == "feeds"), None)
        crit = set(feeds["metrics"].get("critical", [])) if feeds else set()

        # Drop timers for exchanges that have recovered.
        for ex in list(self._feed_crit_since):
            if ex not in crit:
                self._feed_crit_since.pop(ex, None)

        targets = []
        for ex in crit:
            self._feed_crit_since.setdefault(ex, now)
            if now - self._feed_crit_since[ex] < self._heal_after_crit_s:
                continue  # not stale long enough — could be a transient blip
            if now - self._last_heal.get(ex, 0) < self._heal_cooldown_s:
                continue  # reconnected recently — give the feed time to recover
            targets.append(ex)
        return targets

    def _conn_heal_targets(self, report: dict, now: float) -> list[str]:
        """Exchanges stuck in error/disconnected (and NOT manually taken down) that are
        due for a reconnect under per-exchange exponential backoff. Pure logic."""
        if not self._auto_heal:
            return []
        conn = next((c for c in report["components"] if c["name"] == "connectors"), None)
        states = conn["metrics"].get("states", {}) if conn else {}
        manual = {getattr(e, "value", e)
                  for e in getattr(self._engine, "_manually_disconnected", set())}

        targets = []
        for ex, st in states.items():
            if st == "connected":
                self._conn_retry.pop(ex, None)        # recovered → reset backoff
                self._last_conn_heal.pop(ex, None)
                continue
            if ex in manual:
                continue  # operator took it down on purpose — don't fight them
            retries = self._conn_retry.get(ex, 0)
            delay = min(self._conn_cap, self._conn_base * (2 ** retries))
            if now - self._last_conn_heal.get(ex, 0.0) < delay:
                continue
            targets.append(ex)
        return targets

    async def _heal(self, report: dict) -> None:
        now = time.time()
        await self._heal_feeds(report, now)
        await self._heal_connectors(report, now)

    async def _heal_feeds(self, report: dict, now: float) -> None:
        targets = self._heal_targets(report, now)
        if not targets:
            return
        from core.types import Exchange
        for ex in targets:
            self._last_heal[ex] = now
            self._feed_crit_since.pop(ex, None)
            sustained = self._heal_after_crit_s
            logger.warning(f"Auto-heal: feed for {ex} frozen — reconnecting connector")
            try:
                ex_enum = Exchange(ex)
                await self._engine.disconnect_exchange(ex_enum)
                await self._engine.connect_exchange(ex_enum)
                outcome, detail = "success", f"frozen >{sustained:.0f}s"
                logger.info(f"Auto-heal: reconnected {ex}")
                self._notify(f"🔧 Auto-heal: {ex} feed was frozen (>{sustained:.0f}s) — "
                             f"connector reconnected")
            except Exception as e:
                outcome, detail = "failed", str(e)
                logger.error(f"Auto-heal: reconnect {ex} failed: {e}")
                self._notify(f"⚠️ Auto-heal: {ex} reconnect FAILED — {e}")
            self._record_action("feed_reconnect", ex,
                                f"market-data feed frozen >{sustained:.0f}s", outcome, detail)

    async def _heal_connectors(self, report: dict, now: float) -> None:
        targets = self._conn_heal_targets(report, now)
        if not targets:
            return
        from core.types import Exchange
        for ex in targets:
            self._last_conn_heal[ex] = now
            attempt = self._conn_retry.get(ex, 0) + 1
            self._conn_retry[ex] = attempt
            logger.warning(f"Auto-heal: connector {ex} down — reconnect attempt {attempt}")
            try:
                await self._engine.connect_exchange(Exchange(ex))
                outcome, detail = "success", f"attempt {attempt}"
                logger.info(f"Auto-heal: connector {ex} reconnected (attempt {attempt})")
                self._notify(f"🔧 Auto-heal: {ex} connector was down — "
                             f"reconnected (attempt {attempt})")
            except Exception as e:
                outcome, detail = "failed", f"attempt {attempt}: {e}"
                logger.error(f"Auto-heal: connector {ex} reconnect failed "
                             f"(attempt {attempt}): {e}")
                self._notify(f"⚠️ Auto-heal: {ex} reconnect attempt {attempt} FAILED — {e}")
            self._record_action("connector_reconnect", ex,
                                "connector state was error/disconnected", outcome, detail)

    def _notify(self, text: str) -> None:
        notifier = getattr(self._engine, "_notifier", None)
        if notifier:
            asyncio.create_task(notifier.send(text))

    def _record_action(self, action: str, target: str, reason: str,
                       outcome: str, detail: Optional[str] = None) -> None:
        """Fire-and-forget audit write; no-op if no storage is wired."""
        if self._storage is None:
            return
        try:
            asyncio.create_task(
                self._storage.record_auto_action(action, target, reason, outcome, detail))
        except RuntimeError:
            pass  # no running loop
