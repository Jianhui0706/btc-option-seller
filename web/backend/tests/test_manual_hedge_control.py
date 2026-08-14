from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src import api


PARENT = {"id": 7, "state": "OPEN", "spread_type": "CALL_CREDIT"}


class FakeRepo:
    conn = None

    def __init__(self, parents=None):
        self.parents = list(parents if parents is not None else [PARENT])
        self.hedges = {
            parent["id"]: {"state": "HEDGE_INACTIVE", "futures_usd": 0.0}
            for parent in self.parents
        }
        self.transitions = []

    async def open_parents(self):
        return [p for p in self.parents
                if p["state"] in ("OPEN", "PARTIALLY_CLOSED")]

    async def active_parents(self):
        return self.parents

    async def get_hedge(self, parent_id):
        return self.hedges.get(parent_id)

    async def ensure_hedge_row(self, parent_id):
        self.hedges.setdefault(
            parent_id, {"state": "HEDGE_INACTIVE", "futures_usd": 0.0})

    async def transition_hedge(self, parent_id, state, *args, **kwargs):
        self.transitions.append((parent_id, state, kwargs))
        self.hedges[parent_id]["state"] = state

    async def set_hedge_manual_hold(self, parent_id, enabled):
        self.hedges[parent_id]["manual_hold"] = 1 if enabled else 0


class FakeQueue:
    def __init__(self):
        self.commands = []
        self.hedge_release_in_flight = False

    def hedge_rebalance(self, parent_id, order_btc, net_delta,
                        *, spread_types=None):
        return SimpleNamespace(payload={
            "parent_id": parent_id,
            "order_btc": order_btc,
            "net_delta": net_delta,
            "spread_types": spread_types,
        })

    def put(self, command):
        self.commands.append(command)
        return True

    def hedge_release(self, parent_id, *, manual=False):
        return SimpleNamespace(payload={
            "parent_id": parent_id,
            "manual": manual,
        })


def make_request(repo=None):
    ctx = SimpleNamespace(
        repo=repo or FakeRepo(),
        queue=FakeQueue(),
        ws=object(),
        hedge_frozen=False,
        current_exec_task=None,
        current_command_kind=None,
        cfg=SimpleNamespace(
            env=SimpleNamespace(is_testnet=True),
            strategy=SimpleNamespace(currency="BTC"),
            hedge=SimpleNamespace(
                perp_instrument="BTC-PERPETUAL",
                perp_contract_usd=10.0,
            ),
        ),
    )

    async def force_close_active():
        return False

    ctx.force_close_active = force_close_active
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(ctx=ctx)))


@pytest.mark.asyncio
async def test_manual_hedge_activates_and_queues_exact_neutralization(
        monkeypatch):
    request = make_request()

    async def flat_perp(*args, **kwargs):
        return 0.0, 0.0

    async def portfolio(*args, **kwargs):
        return SimpleNamespace(
            controller_parent_id=7,
            option_delta=-0.42,
            futures_delta=0.0,
            net_delta=-0.42,
            index_price=64000.0,
        )

    async def no_event(*args, **kwargs):
        return None

    monkeypatch.setattr(api.futures, "current_perp_position", flat_perp)
    monkeypatch.setattr(api.hedge_mod, "portfolio_hedge_view", portfolio)
    monkeypatch.setattr(api.ev, "emit", no_event)

    result = await api.manual_hedge(request)

    ctx = request.app.state.ctx
    assert result == {
        "status": "queued",
        "net_delta": -0.42,
        "estimated_order_usd": 26880.0,
        "activated_parent_ids": [7],
        "auto_release": True,
    }
    assert ctx.repo.transitions[0][:2] == (7, "HEDGE_ACTIVE")
    assert ctx.repo.transitions[0][2]["reason"] == "MANUAL_HEDGE"
    assert len(ctx.queue.commands) == 1
    payload = ctx.queue.commands[0].payload
    assert payload["manual"] is True
    assert payload["spread_types"] == ["CALL_CREDIT"]
    assert payload["order_btc"] == pytest.approx(0.42)
    assert ctx.repo.hedges[7]["manual_hold"] == 0


@pytest.mark.asyncio
async def test_manual_hedge_rejects_without_open_strategy():
    request = make_request(FakeRepo(parents=[]))

    with pytest.raises(HTTPException) as exc_info:
        await api.manual_hedge(request)

    assert exc_info.value.status_code == 409
    assert "没有可对冲" in exc_info.value.detail


@pytest.mark.asyncio
async def test_manual_hedge_rejects_when_release_is_queued():
    request = make_request()
    request.app.state.ctx.queue.hedge_release_in_flight = True

    with pytest.raises(HTTPException) as exc_info:
        await api.manual_hedge(request)

    assert exc_info.value.status_code == 409
    assert "关对冲已在队列中" in exc_info.value.detail
    assert request.app.state.ctx.queue.commands == []


@pytest.mark.asyncio
async def test_manual_hedge_rejects_unattributed_perpetual(monkeypatch):
    request = make_request()

    async def manual_perp(*args, **kwargs):
        return 1000.0, 0.015

    monkeypatch.setattr(
        api.futures, "current_perp_position", manual_perp)

    with pytest.raises(HTTPException) as exc_info:
        await api.manual_hedge(request)

    assert exc_info.value.status_code == 409
    assert "不一致" in exc_info.value.detail
    assert request.app.state.ctx.repo.transitions == []


@pytest.mark.asyncio
async def test_manual_hedge_cannot_cancel_a_pending_release(monkeypatch):
    request = make_request()
    repo = request.app.state.ctx.repo
    repo.hedges[7] = {
        "state": "HEDGE_RELEASE_PENDING", "futures_usd": -2500.0,
    }

    async def matching_perp(*args, **kwargs):
        return -2500.0, -0.04

    monkeypatch.setattr(api.futures, "current_perp_position", matching_perp)

    with pytest.raises(HTTPException) as exc_info:
        await api.manual_hedge(request)

    assert exc_info.value.status_code == 409
    assert "关对冲" in exc_info.value.detail
    assert request.app.state.ctx.queue.commands == []
    assert repo.hedges[7]["state"] == "HEDGE_RELEASE_PENDING"


@pytest.mark.asyncio
async def test_manual_hedge_release_queues_only_managed_perpetual(monkeypatch):
    request = make_request()
    repo = request.app.state.ctx.repo
    repo.hedges[7] = {"state": "HEDGE_ACTIVE", "futures_usd": -2500.0}

    async def matching_perp(*args, **kwargs):
        return -2500.0, -0.04

    async def no_event(*args, **kwargs):
        return None

    monkeypatch.setattr(api.futures, "current_perp_position", matching_perp)
    monkeypatch.setattr(api.ev, "emit", no_event)

    result = await api.manual_hedge_release(request)

    assert result["status"] == "queued"
    assert result["managed_usd"] == -2500.0
    assert result["queued_parent_ids"] == [7]
    assert repo.transitions[-1][:2] == (7, "HEDGE_RELEASE_PENDING")
    assert repo.transitions[-1][2]["reason"] == "MANUAL_HEDGE_RELEASE"
    assert request.app.state.ctx.queue.commands[-1].payload == {
        "parent_id": 7,
        "manual": True,
    }
    assert repo.hedges[7]["manual_hold"] == 0


@pytest.mark.asyncio
async def test_manual_hedge_release_never_closes_unmanaged_perpetual(monkeypatch):
    request = make_request()

    async def manual_perp(*args, **kwargs):
        return 1000.0, 0.015

    monkeypatch.setattr(api.futures, "current_perp_position", manual_perp)

    with pytest.raises(HTTPException) as exc_info:
        await api.manual_hedge_release(request)

    assert exc_info.value.status_code == 409
    assert "不属于策略" in exc_info.value.detail
    assert request.app.state.ctx.queue.commands == []
