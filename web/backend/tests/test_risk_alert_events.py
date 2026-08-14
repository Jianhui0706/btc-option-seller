from types import SimpleNamespace

import pytest

from src.engine import scanner


class FakeRepo:
    def __init__(self):
        self.conn = object()
        self.state = {}

    async def get_state(self, key, default=None):
        return self.state.get(key, default)

    async def set_state(self, key, value):
        self.state[key] = value


@pytest.mark.asyncio
async def test_risk_alert_is_named_and_throttled(monkeypatch):
    emitted = []

    async def fake_emit(conn, code, **payload):
        emitted.append((code, payload))

    monkeypatch.setattr(scanner.ev, "emit", fake_emit)
    repo = FakeRepo()
    ctx = SimpleNamespace(
        repo=repo,
        cfg=SimpleNamespace(risk=SimpleNamespace(auto_pause_enabled=False)),
    )
    breach = SimpleNamespace(reason="净 Delta +4.000 BTC 异常扩大(> 1.0)")

    assert await scanner.sync_risk_alert_event(ctx, breach, now_ms=1_000)
    assert not await scanner.sync_risk_alert_event(ctx, breach, now_ms=61_000)
    assert len(emitted) == 1
    assert emitted[0][0] == "RISK_ALERT"
    assert emitted[0][1]["response"] == "MONITOR_ONLY"

    assert await scanner.sync_risk_alert_event(
        ctx, breach, now_ms=1_000 + scanner.RISK_ALERT_REPEAT_MS)
    assert len(emitted) == 2


@pytest.mark.asyncio
async def test_risk_alert_emits_again_after_recovery(monkeypatch):
    emitted = []

    async def fake_emit(conn, code, **payload):
        emitted.append((code, payload))

    monkeypatch.setattr(scanner.ev, "emit", fake_emit)
    repo = FakeRepo()
    ctx = SimpleNamespace(
        repo=repo,
        cfg=SimpleNamespace(risk=SimpleNamespace(auto_pause_enabled=True)),
    )
    breach = SimpleNamespace(reason="维持保证金率 70.0% > 60%")

    await scanner.sync_risk_alert_event(ctx, breach, now_ms=1_000)
    await scanner.sync_risk_alert_event(ctx, None, now_ms=2_000)
    assert await scanner.sync_risk_alert_event(ctx, breach, now_ms=3_000)
    assert len(emitted) == 2
    assert emitted[-1][1]["response"] == "AUTO_FORCE_CLOSE"
