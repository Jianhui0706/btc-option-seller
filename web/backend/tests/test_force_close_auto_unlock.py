from types import SimpleNamespace

import pytest

from src.engine import risk


class FakeRepo:
    conn = None

    def __init__(self):
        self.flags = {}
        self.states = {}
        self.parents = []
        self.groups = {}
        self.parent_transitions = []
        self.group_transitions = []
        self.hedge_updates = []

    async def set_flag(self, key, value):
        self.flags[key] = value

    async def get_state(self, key, default=None):
        return self.states.get(key, default)

    async def set_state(self, key, value):
        self.states[key] = value

    async def active_parents(self):
        return self.parents

    async def groups_of(self, parent_id):
        return self.groups.get(parent_id, [])

    async def transition_parent(self, parent_id, state, *args, **kwargs):
        self.parent_transitions.append((parent_id, state))

    async def transition_group(self, group_id, state, **kwargs):
        self.group_transitions.append((group_id, state))

    async def get_hedge(self, parent_id):
        return {"parent_id": parent_id}

    async def set_hedge_futures(self, parent_id, usd, delta):
        self.hedge_updates.append((parent_id, usd, delta))

    async def record_fill(self, **kwargs):
        return None


def make_ctx(repo):
    return SimpleNamespace(
        repo=repo,
        cfg=SimpleNamespace(
            strategy=SimpleNamespace(currency="BTC", min_lot=0.1),
            hedge=SimpleNamespace(
                perp_instrument="BTC-PERPETUAL",
                ioc_ticks=5,
                perp_contract_usd=10.0,
            ),
        ),
        ws=object(),
        current_exec_task=None,
        hedge_frozen=False,
    )


async def no_op(*args, **kwargs):
    return None


@pytest.mark.asyncio
async def test_clean_force_close_pauses_trading_and_auto_unlocks(monkeypatch):
    repo = FakeRepo()
    ctx = make_ctx(repo)

    async def no_options(*args, **kwargs):
        return []

    async def flat_perp(*args, **kwargs):
        return 0.0, 0.0

    monkeypatch.setattr(risk.account, "cancel_all", no_op)
    monkeypatch.setattr(risk.account, "get_option_positions", no_options)
    monkeypatch.setattr(risk.futures, "flatten_perp", no_op)
    monkeypatch.setattr(risk.futures, "current_perp_position", flat_perp)
    monkeypatch.setattr(risk.ev, "emit", no_op)

    await risk.handle_force_close(
        ctx, {"source": "MANUAL", "reason": "test"})

    assert repo.flags["trading_enabled"] is False
    assert repo.flags["force_close_active"] is False
    assert repo.states[risk.AUTO_RESUME_STATE_KEY] == ""
    assert ctx.hedge_frozen is False


@pytest.mark.asyncio
async def test_clean_transient_risk_close_becomes_ready_to_auto_resume(
        monkeypatch):
    repo = FakeRepo()
    ctx = make_ctx(repo)

    async def no_options(*args, **kwargs):
        return []

    async def flat_perp(*args, **kwargs):
        return 0.0, 0.0

    monkeypatch.setattr(risk.account, "cancel_all", no_op)
    monkeypatch.setattr(risk.account, "get_option_positions", no_options)
    monkeypatch.setattr(risk.futures, "flatten_perp", no_op)
    monkeypatch.setattr(risk.futures, "current_perp_position", flat_perp)
    monkeypatch.setattr(risk.ev, "emit", no_op)

    await risk.handle_force_close(ctx, {
        "source": "RISK_ENGINE",
        "reason": "positions unavailable",
        "auto_resume": True,
    })

    assert repo.flags["trading_enabled"] is False
    assert repo.states[risk.AUTO_RESUME_STATE_KEY] == risk.AUTO_RESUME_READY


@pytest.mark.asyncio
async def test_incomplete_force_close_reports_error_without_lock(monkeypatch):
    repo = FakeRepo()
    repo.parents = [{
        "id": 37,
        "sell_instrument": "BTC-28AUG26-70000-C",
        "buy_instrument": "BTC-28AUG26-76000-C",
    }]
    repo.groups = {37: [{"id": 125, "state": "OPEN"}]}
    ctx = make_ctx(repo)
    events = []

    async def no_options(*args, **kwargs):
        return []

    async def remaining_perp(*args, **kwargs):
        return 20.0, 0.0

    async def capture_event(*args, **kwargs):
        events.append(kwargs)

    monkeypatch.setattr(risk.account, "cancel_all", no_op)
    monkeypatch.setattr(risk.account, "get_option_positions", no_options)
    monkeypatch.setattr(risk.futures, "flatten_perp", no_op)
    monkeypatch.setattr(
        risk.futures, "current_perp_position", remaining_perp)
    monkeypatch.setattr(risk.ev, "emit", capture_event)

    await risk.handle_force_close(
        ctx, {"source": "MANUAL", "reason": "test"})

    assert repo.flags["trading_enabled"] is False
    assert repo.flags["force_close_active"] is False
    assert repo.states[risk.AUTO_RESUME_STATE_KEY] == ""
    assert ctx.hedge_frozen is False
    assert repo.parent_transitions == []
    assert repo.group_transitions == []
    assert repo.hedge_updates == []
    assert any(event.get("stage") == "done"
               and event.get("clean") is False for event in events)


@pytest.mark.asyncio
async def test_rejected_option_close_keeps_trade_active_and_reports_exchange_error(
        monkeypatch):
    repo = FakeRepo()
    repo.parents = [{
        "id": 37,
        "sell_instrument": "BTC-28AUG26-70000-C",
        "buy_instrument": "BTC-28AUG26-76000-C",
    }]
    repo.groups = {37: [{"id": 125, "state": "OPEN"}]}
    ctx = make_ctx(repo)
    events = []
    position = SimpleNamespace(
        instrument="BTC-28AUG26-70000-C",
        size=-2.6,
        is_short=True,
    )

    async def option_still_open(*args, **kwargs):
        return [position]

    async def flat_perp(*args, **kwargs):
        return 0.0, 0.0

    async def rejected_close(*args, **kwargs):
        raise RuntimeError("[10019] locked_by_admin")

    async def capture_event(*args, **kwargs):
        events.append(kwargs)

    monkeypatch.setattr(risk.account, "cancel_all", no_op)
    monkeypatch.setattr(
        risk.account, "get_option_positions", option_still_open)
    monkeypatch.setattr(
        risk.adapter, "close_sell_leg_only", rejected_close)
    monkeypatch.setattr(risk.futures, "flatten_perp", no_op)
    monkeypatch.setattr(
        risk.futures, "current_perp_position", flat_perp)
    monkeypatch.setattr(risk.ev, "emit", capture_event)

    await risk.handle_force_close(
        ctx, {"source": "MANUAL", "reason": "test"})

    done = next(event for event in events if event.get("stage") == "done")
    assert done["clean"] is False
    assert done["incomplete"] == ["BTC-28AUG26-70000-C"]
    assert done["failures"] == [{
        "instrument": "BTC-28AUG26-70000-C",
        "error": "[10019] locked_by_admin",
        "closed_qty": 0.0,
        "requested_qty": 2.6,
    }]
    assert repo.parent_transitions == []
    assert repo.group_transitions == []
