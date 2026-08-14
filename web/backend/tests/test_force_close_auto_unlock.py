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
        self.close_reasons = []
        self.group_marks = []
        self.group_transition_details = []
        self.fills = []

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
        self.group_transition_details.append((group_id, state, kwargs))

    async def get_hedge(self, parent_id):
        return {"parent_id": parent_id}

    async def set_hedge_futures(self, parent_id, usd, delta):
        self.hedge_updates.append((parent_id, usd, delta))

    async def set_close_reason(self, parent_id, reason):
        self.close_reasons.append((parent_id, reason))

    async def update_group_mark(self, group_id, pnl, ratio):
        self.group_marks.append((group_id, pnl, ratio))

    async def record_fill(self, **kwargs):
        self.fills.append(kwargs)

    async def close_cashflows(self, parent_id):
        buy_cost = sum(
            fill["qty"] * fill["price"] for fill in self.fills
            if fill["parent_id"] == parent_id
            and fill["phase"] == "close" and fill["direction"] == "buy")
        sell_revenue = sum(
            fill["qty"] * fill["price"] for fill in self.fills
            if fill["parent_id"] == parent_id
            and fill["phase"] == "close" and fill["direction"] == "sell")
        return buy_cost, sell_revenue

    async def close_fees(self, parent_id):
        return sum(
            abs(fill["fee"]) for fill in self.fills
            if fill["parent_id"] == parent_id and fill["phase"] == "close")


def make_ctx(repo):
    return SimpleNamespace(
        repo=repo,
        cfg=SimpleNamespace(
            strategy=SimpleNamespace(currency="BTC", min_lot=0.1),
            execution=SimpleNamespace(
                n_exec_groups=5,
                attempt_waits=[3, 8, 15],
            ),
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
async def test_force_close_uses_grouped_maker_flow_and_closes_shorts_first(
        monkeypatch):
    repo = FakeRepo()
    ctx = make_ctx(repo)
    calls = []
    positions_reads = 0
    short = SimpleNamespace(
        instrument="BTC-28AUG26-70000-C", size=-2.3, is_short=True)
    long = SimpleNamespace(
        instrument="BTC-28AUG26-76000-C", size=2.3, is_short=False)

    async def option_positions(*args, **kwargs):
        nonlocal positions_reads
        positions_reads += 1
        # Return the protective long first to prove risk.py reorders the close.
        return [long, short] if positions_reads == 1 else []

    async def capture_short(*args, **kwargs):
        calls.append(("short", kwargs))
        return SimpleNamespace(
            closed_qty=kwargs["qty"], buy_back_avg=0.01,
            sell_out_avg=0.0, complete=True, liquidity_role="MAKER")

    async def capture_long(*args, **kwargs):
        calls.append(("long", kwargs))
        return SimpleNamespace(
            closed_qty=kwargs["qty"], buy_back_avg=0.0,
            sell_out_avg=0.005, complete=True, liquidity_role="MAKER")

    async def flat_perp(*args, **kwargs):
        return 0.0, 0.0

    monkeypatch.setattr(risk.account, "cancel_all", no_op)
    monkeypatch.setattr(risk.account, "get_option_positions", option_positions)
    monkeypatch.setattr(
        risk.adapter, "close_sell_leg_only", capture_short)
    monkeypatch.setattr(risk.adapter, "close_long_leg", capture_long)
    monkeypatch.setattr(risk.futures, "flatten_perp", no_op)
    monkeypatch.setattr(
        risk.futures, "current_perp_position", flat_perp)
    monkeypatch.setattr(risk.ev, "emit", no_op)

    await risk.handle_force_close(
        ctx, {"source": "MANUAL", "reason": "test"})

    assert [kind for kind, _ in calls] == ["short", "long"]
    assert all(kwargs["n_exec_groups"] == 5 for _, kwargs in calls)
    assert all(
        kwargs["attempt_waits"] == (3, 8, 15) for _, kwargs in calls)


@pytest.mark.asyncio
async def test_force_close_attributes_fills_fees_and_realized_pnl(monkeypatch):
    repo = FakeRepo()
    repo.parents = [{
        "id": 41,
        "state": "OPEN",
        "sell_instrument": "BTC-28AUG26-70000-C",
        "buy_instrument": "BTC-28AUG26-76000-C",
        "qty_total": 1.0,
        "net_credit_btc": 0.01,
    }]
    repo.groups = {41: [
        {"id": 201, "state": "OPEN", "qty": 0.5,
         "closed_qty": 0.0, "realized_pnl_btc": 0.0},
        {"id": 202, "state": "OPEN", "qty": 0.5,
         "closed_qty": 0.0, "realized_pnl_btc": 0.0},
    ]}
    ctx = make_ctx(repo)
    reads = 0
    short = SimpleNamespace(
        instrument="BTC-28AUG26-70000-C", size=-1.0, is_short=True)
    long = SimpleNamespace(
        instrument="BTC-28AUG26-76000-C", size=1.0, is_short=False)

    async def option_positions(*args, **kwargs):
        nonlocal reads
        reads += 1
        return [short, long] if reads == 1 else []

    async def close_short(*args, **kwargs):
        return SimpleNamespace(
            closed_qty=1.0, buy_back_avg=0.012, sell_out_avg=0.0,
            fee_btc=0.0001, complete=True, liquidity_role="TAKER")

    async def close_long(*args, **kwargs):
        return SimpleNamespace(
            closed_qty=1.0, buy_back_avg=0.0, sell_out_avg=0.003,
            fee_btc=0.00005, complete=True, liquidity_role="MAKER")

    async def flat_perp(*args, **kwargs):
        return 0.0, 0.0

    monkeypatch.setattr(risk.account, "cancel_all", no_op)
    monkeypatch.setattr(risk.account, "get_option_positions", option_positions)
    monkeypatch.setattr(risk.adapter, "close_sell_leg_only", close_short)
    monkeypatch.setattr(risk.adapter, "close_long_leg", close_long)
    monkeypatch.setattr(risk.futures, "flatten_perp", no_op)
    monkeypatch.setattr(risk.futures, "current_perp_position", flat_perp)
    monkeypatch.setattr(risk.ev, "emit", no_op)

    await risk.handle_force_close(
        ctx, {"source": "MANUAL", "reason": "test"})

    assert [(fill["parent_id"], fill["instrument"], fill["direction"])
            for fill in repo.fills] == [
        (41, "BTC-28AUG26-70000-C", "buy"),
        (41, "BTC-28AUG26-76000-C", "sell"),
    ]
    assert sum(fill["fee"] for fill in repo.fills) == pytest.approx(0.00015)
    details = {group_id: kwargs for group_id, _, kwargs
               in repo.group_transition_details}
    assert details[201]["closed_qty"] == 0.5
    assert details[202]["closed_qty"] == 0.5
    assert details[201]["realized_pnl"] == pytest.approx(0.000425)
    assert details[202]["realized_pnl"] == pytest.approx(0.000425)


@pytest.mark.asyncio
async def test_force_close_shared_instrument_fill_is_split_by_parent_quantity():
    repo = FakeRepo()
    parents = [
        {"id": 51, "sell_instrument": "BTC-SHARED-P",
         "buy_instrument": "BTC-LOW-P", "qty_total": 0.4},
        {"id": 52, "sell_instrument": "BTC-SHARED-P",
         "buy_instrument": "BTC-LOWER-P", "qty_total": 0.6},
    ]
    repo.groups = {
        51: [{"id": 301, "state": "OPEN", "qty": 0.4,
              "closed_qty": 0.0}],
        52: [{"id": 302, "state": "OPEN", "qty": 0.6,
              "closed_qty": 0.0}],
    }

    claims = await risk._force_close_claims(repo, parents)
    allocations = risk._allocate_force_close_fill(
        claims, "BTC-SHARED-P", "buy", 1.0)

    assert allocations == [(51, 0.4), (52, 0.6)]


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
async def test_clean_daily_loss_close_preserves_cross_day_latch(monkeypatch):
    repo = FakeRepo()
    repo.states[risk.AUTO_RESUME_STATE_KEY] = "DAILY_LOSS:2026-08-01"
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
        "reason": "日亏损 5.1% 超过限制 5%",
        "auto_resume": False,
    })

    assert repo.states[risk.AUTO_RESUME_STATE_KEY] == "DAILY_LOSS:2026-08-01"


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


@pytest.mark.asyncio
async def test_incomplete_account_close_finalizes_other_flat_parent(monkeypatch):
    repo = FakeRepo()
    repo.parents = [
        {
            "id": 37,
            "state": "OPEN",
            "sell_instrument": "BTC-28AUG26-70000-P",
            "buy_instrument": "BTC-28AUG26-68000-P",
        },
        {
            "id": 38,
            "state": "OPEN",
            "sell_instrument": "BTC-28AUG26-76000-C",
            "buy_instrument": "BTC-28AUG26-78000-C",
        },
    ]
    repo.groups = {
        37: [{"id": 125, "state": "OPEN"}],
        38: [{"id": 126, "state": "OPEN"}],
    }
    ctx = make_ctx(repo)
    events = []
    residual_put = SimpleNamespace(
        instrument="BTC-28AUG26-68000-P",
        size=4.8,
        is_short=False,
    )

    async def residual_options(*args, **kwargs):
        return [residual_put]

    async def flat_perp(*args, **kwargs):
        return 0.0, 0.0

    async def rejected_close(*args, **kwargs):
        raise RuntimeError("no bid")

    async def capture_event(*args, **kwargs):
        events.append(kwargs)

    monkeypatch.setattr(risk.account, "cancel_all", no_op)
    monkeypatch.setattr(
        risk.account, "get_option_positions", residual_options)
    monkeypatch.setattr(risk.adapter, "close_long_leg", rejected_close)
    monkeypatch.setattr(risk.futures, "flatten_perp", no_op)
    monkeypatch.setattr(
        risk.futures, "current_perp_position", flat_perp)
    monkeypatch.setattr(risk.ev, "emit", capture_event)

    await risk.handle_force_close(
        ctx, {"source": "MANUAL", "reason": "test"})

    assert repo.parent_transitions == [
        (38, "FORCE_CLOSED"),
        (37, "CLOSING"),
    ]
    assert repo.group_transitions == [(126, "FORCE_CLOSED")]
    assert repo.hedge_updates == [(38, 0.0, 0.0)]
    assert repo.close_reasons == [(
        37, "RESIDUAL_LONG_DECISION_REQUIRED")]
    assert repo.group_marks == []
    done = next(event for event in events if event.get("stage") == "done")
    assert done["clean"] is False
    assert done["closed_parent_ids"] == [38]
    assert done["quarantined_parent_ids"] == []
    assert done["residual_decision_parent_ids"] == [37]
