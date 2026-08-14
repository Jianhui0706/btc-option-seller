"""提前对冲激活价的状态流转测试。"""

from types import SimpleNamespace

import pytest

from src.engine import hedge
from src.engine.hedge import DeltaView, scan_hedge
from src.monitor import events as ev


class FakeRepo:
    def __init__(self, state="HEDGE_INACTIVE"):
        self.hedge = {
            "state": state,
            "release_pending_bar_ts": 100,
            "manual_hold": 0,
        }
        self.transitions = []

    async def get_hedge(self, parent_id):
        return self.hedge.copy()

    async def ensure_hedge_row(self, parent_id):
        return None

    async def get_signal_payload(self, signal_id):
        return {}

    async def transition_hedge(self, parent_id, state, event_code, **payload):
        self.hedge["state"] = state
        self.transitions.append((state, event_code, payload))

    async def set_release_pending_bar(self, parent_id, bar_ts):
        self.hedge["release_pending_bar_ts"] = bar_ts


class FakeQueue:
    def __init__(self):
        self.items = []

    def hedge_release(self, parent_id, *, manual=False, grouped=False):
        return ("release", parent_id, manual, grouped)

    def put(self, item):
        self.items.append(item)


def _ctx(state="HEDGE_INACTIVE"):
    return SimpleNamespace(
        repo=FakeRepo(state),
        queue=FakeQueue(),
        cfg=SimpleNamespace(
            hedge=SimpleNamespace(
                delta_account_risk_pct=0.02,
                delta_trigger_pct=0.10,
                bep_activation_progress=0.50,
                call_release_pct=0.98,
                put_release_pct=1.02,
                perp_contract_usd=10.0,
            ),
            debug=SimpleNamespace(bep_override=None, trigger_override=None),
        ),
    )


def _parent():
    return {
        "id": 1,
        "signal_id": "CALL-1",
        "spread_type": "CALL_CREDIT",
        "open_index_price": 66000.0,
        "bep_price": 70330.0,
    }


def _view():
    return DeltaView(
        option_delta=0.001,
        futures_delta=0.0,
        net_delta=0.001,
        equity_btc=1.0,
        index_price=68165.0,
    )


@pytest.mark.asyncio
async def test_midpoint_activation_does_not_immediately_enter_release_pending():
    ctx = _ctx()

    await scan_hedge(ctx, _parent(), 68165.0, 200, view=_view())

    assert [item[:2] for item in ctx.repo.transitions] == [
        ("HEDGE_ACTIVE", ev.BEP_HEDGE_ACTIVATED),
    ]
    payload = ctx.repo.transitions[0][2]
    assert payload["activation_price"] == pytest.approx(68165.0)
    assert payload["bep"] == pytest.approx(70330.0)
    assert ctx.queue.items == []
    assert ctx.hedge_grouped_open_pending == {1}


@pytest.mark.asyncio
async def test_release_pending_is_cancelled_at_midpoint_not_only_at_bep():
    ctx = _ctx("HEDGE_RELEASE_PENDING")

    await scan_hedge(ctx, _parent(), 68165.0, 200, view=_view())

    assert [item[:2] for item in ctx.repo.transitions] == [
        ("HEDGE_ACTIVE", ev.HEDGE_RELEASE_CANCELLED),
    ]
    assert ctx.queue.items == []


@pytest.mark.asyncio
async def test_manual_hold_blocks_automatic_release_in_safe_zone():
    ctx = _ctx("HEDGE_ACTIVE")
    ctx.repo.hedge["manual_hold"] = 1

    # CALL 撤销区内，但人工持有必须等待用户点击取消对冲。
    moving_delta = DeltaView(
        option_delta=0.05, futures_delta=0.0, net_delta=0.05,
        equity_btc=1.0, index_price=67000.0)
    await scan_hedge(ctx, _parent(), 67000.0, 200, view=moving_delta)

    assert ctx.repo.transitions == []
    assert ctx.queue.items == []


@pytest.mark.asyncio
async def test_one_click_mode_without_manual_hold_completes_auto_release_flow():
    ctx = _ctx("HEDGE_ACTIVE")
    ctx.repo.hedge["manual_hold"] = 0

    # 旧版会被 manual_hold 卡住；新版一键对冲成交后在安全区进入自动撤销。
    await scan_hedge(ctx, _parent(), 66000.0, 200, view=_view())
    assert ctx.repo.transitions[-1][:2] == (
        "HEDGE_RELEASE_PENDING", ev.HEDGE_RELEASE_PENDING)
    assert ctx.queue.items == []

    # 下一根完整复核 Bar 仍在安全区，自动把 HEDGE_RELEASE 命令入队。
    await scan_hedge(ctx, _parent(), 66000.0, 200 + 180_000, view=_view())
    assert ctx.queue.items == [("release", 1, False, True)]


@pytest.mark.asyncio
async def test_manual_release_cooldown_blocks_reactivation_for_ten_seconds(
        monkeypatch):
    now = 2_000_000
    ctx = _ctx()
    ctx.hedge_rearm_after = {1: now + 10_000}
    monkeypatch.setattr(hedge.market_data, "now_ms", lambda: now + 9_999)

    await scan_hedge(ctx, _parent(), 68165.0, 200, view=_view())
    assert ctx.repo.transitions == []

    monkeypatch.setattr(hedge.market_data, "now_ms", lambda: now + 10_001)
    await scan_hedge(ctx, _parent(), 68165.0, 201, view=_view())
    assert ctx.repo.transitions[-1][:2] == (
        "HEDGE_ACTIVE", ev.BEP_HEDGE_ACTIVATED)
