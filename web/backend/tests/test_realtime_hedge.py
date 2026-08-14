"""逐笔 BTC 指数价激活动态对冲。"""

import asyncio
from types import SimpleNamespace

import pytest

from src.engine import realtime_hedge
from src.engine.hedge import DeltaView


class FakeRepo:
    def __init__(self, parent):
        self.parent = parent
        self.state = "HEDGE_INACTIVE"

    async def open_parents(self):
        return [self.parent]

    async def get_hedge(self, parent_id):
        return {"state": self.state}


class FakeWS:
    def __init__(self):
        self.subscription = None

    async def subscribe(self, channels, callback):
        self.subscription = (channels, callback)
        return channels


def _ctx(spread_type):
    parent = {
        "id": 7,
        "signal_id": "RT-7",
        "spread_type": spread_type,
        "open_index_price": 66_000.0,
        "bep_price": 70_000.0 if spread_type == "CALL_CREDIT" else 62_000.0,
    }
    return SimpleNamespace(
        repo=FakeRepo(parent),
        ws=FakeWS(),
        hedge_frozen=False,
        hedge_snapshot=None,
        runtime_bep_activation_progress=0.20,
        realtime_index_price=None,
        realtime_index_ts=None,
        cfg=SimpleNamespace(
            strategy=SimpleNamespace(currency="BTC"),
            debug=SimpleNamespace(bep_override=None),
            hedge=SimpleNamespace(bep_activation_progress=0.20),
        ),
        force_close_active=_false,
    )


async def _false():
    return False


@pytest.mark.asyncio
async def test_start_subscribes_to_btc_realtime_index():
    ctx = _ctx("CALL_CREDIT")
    monitor = realtime_hedge.RealtimeHedgeMonitor(ctx)

    await monitor.start()

    channels, callback = ctx.ws.subscription
    assert channels == ["deribit_price_index.btc_usd"]
    assert callback == monitor.on_tick
    await monitor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("spread_type", "safe_price", "triggering_price"),
    [
        ("CALL_CREDIT", 66_799.0, 66_800.0),
        ("PUT_CREDIT", 65_201.0, 65_200.0),
    ],
)
async def test_tick_activates_call_and_put_at_configured_line(
        monkeypatch, spread_type, safe_price, triggering_price):
    ctx = _ctx(spread_type)
    monitor = realtime_hedge.RealtimeHedgeMonitor(ctx)
    calls = []
    account_calls = []

    async def fake_snapshot(_ctx):
        return DeltaView(0.01, 0.0, 0.01, 1.0, triggering_price)

    async def fake_scan(_ctx, parent, price, timestamp, **kwargs):
        calls.append((parent["id"], price, timestamp, kwargs))
        _ctx.repo.state = "HEDGE_ACTIVE"

    async def fake_account_scan(_ctx):
        account_calls.append(True)
        return None

    monkeypatch.setattr(realtime_hedge.hedge, "snapshot_delta", fake_snapshot)
    monkeypatch.setattr(realtime_hedge.hedge, "scan_hedge", fake_scan)
    monkeypatch.setattr(
        realtime_hedge.hedge, "scan_account_hedge", fake_account_scan)
    await monitor._refresh_targets()
    monitor._refresh_due_at = float("inf")

    monitor.on_tick(monitor.channel, {"price": safe_price, "timestamp": 100})
    await asyncio.sleep(0)
    assert calls == []

    monitor.on_tick(
        monitor.channel, {"price": triggering_price, "timestamp": 200})
    tasks = list(monitor._activation_tasks.values())
    await asyncio.gather(*tasks)

    assert len(calls) == 1
    parent_id, price, timestamp, kwargs = calls[0]
    assert (parent_id, price, timestamp) == (7, triggering_price, 200)
    assert kwargs["price_source"] == "realtime_index"
    assert kwargs["allow_release"] is False
    assert kwargs["allow_rebalance"] is False
    assert account_calls == [True]
    assert ctx.hedge_snapshot["observed_price"] == triggering_price
    assert ctx.hedge_snapshot["price_source"] == "realtime_index"
    await monitor.close()


def test_invalid_tick_is_ignored():
    ctx = _ctx("CALL_CREDIT")
    monitor = realtime_hedge.RealtimeHedgeMonitor(ctx)

    monitor.on_tick(monitor.channel, {"price": 0})
    monitor.on_tick(monitor.channel, {"unexpected": 66_800})

    assert ctx.realtime_index_price is None
    assert monitor._activation_tasks == {}
