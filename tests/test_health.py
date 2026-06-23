"""Tests for the operational HealthMonitor (monitoring/health.py).

evaluate() is pure given an explicit snapshot, so most tests need no engine.
snapshot()/_maybe_alert() are exercised against small stubs.
"""
import asyncio
import time

from monitoring.health import HealthMonitor


class _StubEngine:
    """Minimal engine surface the monitor reads: states, queue, last_ticker, notifier."""
    def __init__(self, *, active=True, states=None, last_ticker=None, qsize=0, qmax=10000):
        self.is_active = active
        self._connector_states = states or {}
        self._last_ticker = last_ticker or {}
        self._notifier = None

        class _Q:
            def __init__(self, n, m):
                self._n, self.maxsize = n, m
            def qsize(self):
                return self._n
        self.event_queue = _Q(qsize, qmax)


def _mon(engine=None, **kw):
    return HealthMonitor(engine or _StubEngine(), **kw)


# ── evaluate(): happy path ─────────────────────────────────────────────────────

def test_all_healthy_is_ok():
    r = _mon().evaluate(
        active=True,
        connector_states={"binance": "connected", "okx": "connected"},
        feed_ages={"binance": 1.0, "okx": 2.0},
        loop_lag_s=0.01, queue_size=5, queue_max=10000,
    )
    assert r["status"] == "ok"
    assert {c["name"] for c in r["components"]} == {
        "connectors", "feeds", "event_loop", "event_queue"}
    assert all(c["status"] == "ok" for c in r["components"])


# ── Feeds ──────────────────────────────────────────────────────────────────────

def test_stale_feed_beyond_crit_is_critical():
    r = _mon(stale_feed_warn_s=10, stale_feed_crit_s=30).evaluate(
        active=True,
        connector_states={"binance": "connected"},
        feed_ages={"binance": 45.0},   # > 30s crit
        loop_lag_s=0.0, queue_size=0, queue_max=10000,
    )
    assert r["status"] == "critical"
    feeds = next(c for c in r["components"] if c["name"] == "feeds")
    assert feeds["status"] == "critical"


def test_stale_feed_between_warn_and_crit_is_degraded():
    r = _mon(stale_feed_warn_s=10, stale_feed_crit_s=30).evaluate(
        active=True,
        connector_states={"binance": "connected"},
        feed_ages={"binance": 15.0},   # warn < 15 < crit
        loop_lag_s=0.0, queue_size=0, queue_max=10000,
    )
    assert r["status"] == "degraded"


def test_connected_exchange_with_no_quote_ever_is_critical():
    r = _mon().evaluate(
        active=True,
        connector_states={"binance": "connected"},
        feed_ages={},                  # never received a tick
        loop_lag_s=0.0, queue_size=0, queue_max=10000,
    )
    feeds = next(c for c in r["components"] if c["name"] == "feeds")
    assert feeds["status"] == "critical"
    assert "never" in feeds["detail"]


def test_disconnected_exchange_feed_not_counted():
    # okx is down → its stale feed must NOT drive feeds critical (connectors covers it).
    r = _mon().evaluate(
        active=True,
        connector_states={"binance": "connected", "okx": "disconnected"},
        feed_ages={"binance": 1.0, "okx": 9999.0},
        loop_lag_s=0.0, queue_size=0, queue_max=10000,
    )
    feeds = next(c for c in r["components"] if c["name"] == "feeds")
    assert feeds["status"] == "ok"
    assert "okx" not in feeds["metrics"]["ages_s"]


# ── Connectors ─────────────────────────────────────────────────────────────────

def test_one_connector_down_is_degraded():
    r = _mon().evaluate(
        active=True,
        connector_states={"binance": "connected", "okx": "error"},
        feed_ages={"binance": 1.0}, loop_lag_s=0.0, queue_size=0, queue_max=10000,
    )
    conn = next(c for c in r["components"] if c["name"] == "connectors")
    assert conn["status"] == "degraded"
    assert "okx" in conn["detail"]


def test_all_connectors_down_is_critical():
    r = _mon().evaluate(
        active=True,
        connector_states={"binance": "error", "okx": "disconnected"},
        feed_ages={}, loop_lag_s=0.0, queue_size=0, queue_max=10000,
    )
    assert r["status"] == "critical"
    conn = next(c for c in r["components"] if c["name"] == "connectors")
    assert conn["status"] == "critical"


# ── Event loop ─────────────────────────────────────────────────────────────────

def test_loop_lag_thresholds():
    base = dict(active=True, connector_states={"binance": "connected"},
                feed_ages={"binance": 1.0}, queue_size=0, queue_max=10000)
    m = _mon(loop_lag_warn_s=0.5, loop_lag_crit_s=2.0)
    assert m.evaluate(loop_lag_s=0.1, **base)["status"] == "ok"
    assert m.evaluate(loop_lag_s=1.0, **base)["status"] == "degraded"
    assert m.evaluate(loop_lag_s=3.0, **base)["status"] == "critical"


# ── Event queue ────────────────────────────────────────────────────────────────

def test_queue_saturation_thresholds():
    base = dict(active=True, connector_states={"binance": "connected"},
                feed_ages={"binance": 1.0}, loop_lag_s=0.0, queue_max=100)
    m = _mon(queue_warn_pct=0.70, queue_crit_pct=0.90)
    assert m.evaluate(queue_size=10, **base)["status"] == "ok"
    assert m.evaluate(queue_size=75, **base)["status"] == "degraded"
    assert m.evaluate(queue_size=95, **base)["status"] == "critical"


# ── Paused engine ──────────────────────────────────────────────────────────────

def test_paused_engine_reports_paused_not_critical():
    r = _mon().evaluate(
        active=False,
        connector_states={"binance": "disconnected", "okx": "disconnected"},
        feed_ages={}, loop_lag_s=5.0, queue_size=9999, queue_max=10000,
    )
    assert r["status"] == "paused"
    # No component should be flagged critical while intentionally paused.
    assert all(c["status"] in ("ok", "idle") for c in r["components"])


# ── snapshot(): reads engine.last_ticker / queue / states ──────────────────────

def test_snapshot_derives_freshest_feed_age_per_exchange():
    now = time.time()
    eng = _StubEngine(
        active=True,
        states={"binance": "connected"},
        last_ticker={
            ("binance", "BTC-USDT"): (1, 1, 1, now - 50.0),  # stale
            ("binance", "ETH-USDT"): (1, 1, 1, now - 1.0),   # fresh → should win
        },
        qsize=3, qmax=10000,
    )
    r = _mon(eng).snapshot()
    feeds = next(c for c in r["components"] if c["name"] == "feeds")
    assert feeds["status"] == "ok"            # freshest (1s) wins, not the 50s one
    assert feeds["metrics"]["ages_s"]["binance"] < 5


# ── Alerting transitions ───────────────────────────────────────────────────────

class _FakeNotifier:
    def __init__(self):
        self.sent = []
    async def send(self, text):
        self.sent.append(text)


def _drain():
    """Let create_task'd notifier coroutines run to completion."""
    async def _noop():
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    asyncio.get_event_loop().run_until_complete(_noop())


def test_alert_fires_on_transition_to_critical_and_recovery():
    eng = _StubEngine()
    eng._notifier = _FakeNotifier()
    m = _mon(eng)

    async def scenario():
        crit = m.evaluate(active=True, connector_states={"binance": "error"},
                          feed_ages={}, loop_lag_s=0.0, queue_size=0, queue_max=10000)
        m._maybe_alert(crit)                 # ok → critical
        await asyncio.sleep(0)
        ok = m.evaluate(active=True, connector_states={"binance": "connected"},
                        feed_ages={"binance": 1.0}, loop_lag_s=0.0,
                        queue_size=0, queue_max=10000)
        m._maybe_alert(ok)                   # critical → ok (recovery)
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert any("CRITICAL" in s for s in eng._notifier.sent)
    assert any("recovered" in s for s in eng._notifier.sent)


def test_broadcast_pushes_health_update_on_transition_only():
    eng = _StubEngine()
    m = _mon(eng)
    pushed = []

    async def fake_broadcast(msg):
        pushed.append(msg)

    m.set_broadcast(fake_broadcast)

    async def scenario():
        deg = m.evaluate(active=True,
                         connector_states={"binance": "connected", "okx": "error"},
                         feed_ages={"binance": 1.0}, loop_lag_s=0.0,
                         queue_size=0, queue_max=10000)
        m._maybe_alert(deg)      # ok → degraded: 1 push
        await asyncio.sleep(0)
        m._maybe_alert(deg)      # unchanged: no push
        await asyncio.sleep(0)
        ok = m.evaluate(active=True, connector_states={"binance": "connected", "okx": "connected"},
                        feed_ages={"binance": 1.0, "okx": 1.0}, loop_lag_s=0.0,
                        queue_size=0, queue_max=10000)
        m._maybe_alert(ok)       # degraded → ok: 1 push
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert len(pushed) == 2
    assert all(p["type"] == "health_update" for p in pushed)
    assert pushed[0]["data"]["status"] == "degraded"
    assert pushed[1]["data"]["status"] == "ok"


# ── Self-healing ───────────────────────────────────────────────────────────────

class _HealEngine(_StubEngine):
    """Records reconnect calls so we can assert auto-heal targeted the right exchange."""
    def __init__(self, **kw):
        super().__init__(**kw)
        self.reconnects = []

    async def disconnect_exchange(self, ex):
        self.reconnects.append(("disconnect", ex.value))

    async def connect_exchange(self, ex):
        self.reconnects.append(("connect", ex.value))


def _report_binance_frozen(m):
    return m.evaluate(
        active=True,
        connector_states={"binance": "connected", "okx": "connected"},
        feed_ages={"binance": 9999.0, "okx": 1.0},   # binance feed frozen
        loop_lag_s=0.0, queue_size=0, queue_max=10000,
    )


def test_heal_targets_requires_sustained_critical():
    m = _mon(heal_after_crit_s=30, heal_cooldown_s=120)
    r = _report_binance_frozen(m)
    t0 = 1000.0
    assert m._heal_targets(r, t0) == []          # first sighting starts the timer
    assert m._heal_targets(r, t0 + 10) == []     # only 10s critical
    assert m._heal_targets(r, t0 + 31) == ["binance"]   # sustained past threshold


def test_heal_targets_respects_cooldown():
    m = _mon(heal_after_crit_s=30, heal_cooldown_s=120)
    r = _report_binance_frozen(m)
    t0 = 1000.0
    m._feed_crit_since["binance"] = t0 - 60      # already long-critical
    m._last_heal["binance"] = t0 - 10            # reconnected 10s ago
    assert m._heal_targets(r, t0) == []          # within cooldown → skip
    assert m._heal_targets(r, t0 + 120) == ["binance"]  # cooldown elapsed


def test_heal_timer_clears_when_feed_recovers():
    m = _mon(heal_after_crit_s=30)
    crit = _report_binance_frozen(m)
    m._heal_targets(crit, 1000.0)
    assert "binance" in m._feed_crit_since
    ok = m.evaluate(active=True,
                    connector_states={"binance": "connected", "okx": "connected"},
                    feed_ages={"binance": 1.0, "okx": 1.0},
                    loop_lag_s=0.0, queue_size=0, queue_max=10000)
    m._heal_targets(ok, 1005.0)
    assert "binance" not in m._feed_crit_since   # recovered → timer dropped


def test_auto_heal_disabled_never_targets():
    m = _mon(auto_heal=False)
    r = _report_binance_frozen(m)
    assert m._heal_targets(r, 9_999_999.0) == []


def test_heal_reconnects_only_the_frozen_connector():
    now = time.time()
    eng = _HealEngine(
        active=True,
        states={"binance": "connected", "okx": "connected"},
        last_ticker={
            ("binance", "BTC-USDT"): (1, 1, 1, now - 9999.0),  # frozen
            ("okx", "BTC-USDT"): (1, 1, 1, now - 1.0),         # fresh
        },
    )
    eng._notifier = _FakeNotifier()
    m = _mon(eng, heal_after_crit_s=30, heal_cooldown_s=120)
    m._feed_crit_since["binance"] = now - 60     # already sustained-critical

    asyncio.run(_run_heal(m, eng))

    assert ("disconnect", "binance") in eng.reconnects
    assert ("connect", "binance") in eng.reconnects
    assert all(ex != "okx" for _, ex in eng.reconnects)   # okx feed was fresh
    assert any("Auto-heal" in s for s in eng._notifier.sent)


async def _run_heal(m, eng):
    report = m.snapshot()
    await m._heal(report)
    await asyncio.sleep(0)   # let the notifier create_task run


def test_no_duplicate_alert_while_status_unchanged():
    eng = _StubEngine()
    eng._notifier = _FakeNotifier()
    m = _mon(eng)

    async def scenario():
        deg = m.evaluate(active=True,
                         connector_states={"binance": "connected", "okx": "error"},
                         feed_ages={"binance": 1.0}, loop_lag_s=0.0,
                         queue_size=0, queue_max=10000)
        m._maybe_alert(deg)      # ok → degraded: alerts once
        await asyncio.sleep(0)
        m._maybe_alert(deg)      # still degraded: no new alert
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert len([s for s in eng._notifier.sent if "DEGRADED" in s]) == 1
