"""逐策略虚拟目标合成账户 BTC-PERPETUAL 目标的测试。"""

from types import SimpleNamespace

import pytest

from src.engine import hedge


PARENTS = [
    {"id": 1, "signal_id": "CALL", "state": "OPEN",
     "spread_type": "CALL_CREDIT", "sell_instrument": "C-S",
     "buy_instrument": "C-B"},
    {"id": 2, "signal_id": "PUT", "state": "OPEN",
     "spread_type": "PUT_CREDIT", "sell_instrument": "P-S",
     "buy_instrument": "P-B"},
]
OPTIONS = [
    SimpleNamespace(instrument="C-S", delta=-0.06),
    SimpleNamespace(instrument="C-B", delta=0.01),
    SimpleNamespace(instrument="P-S", delta=0.05),
    SimpleNamespace(instrument="P-B", delta=-0.01),
]


class Repo:
    def __init__(self, states=None, managed=None, last=None):
        states = states or {1: "HEDGE_ACTIVE", 2: "HEDGE_INACTIVE"}
        managed = managed or {}
        self.hedges = {
            pid: {"state": state, "futures_usd": managed.get(pid, 0.0),
                  "last_rebalance_at": last}
            for pid, state in states.items()
        }
        self.actions = []

    async def open_parents(self):
        return [dict(p) for p in PARENTS]

    async def get_hedge(self, parent_id):
        return dict(self.hedges[parent_id])

    async def get_parent(self, parent_id):
        return next(dict(p) for p in PARENTS if p["id"] == parent_id)

    async def get_signal_payload(self, signal_id):
        return {}

    async def record_hedge_action(self, **payload):
        self.actions.append(payload)

    async def set_hedge_futures(self, parent_id, usd, delta_btc):
        self.hedges[parent_id]["futures_usd"] = usd


class Queue:
    def __init__(self):
        self.items = []

    def hedge_rebalance(self, parent_id, order_btc, net_delta,
                        *, degraded_retry=False, spread_types=None,
                        grouped=False):
        return {"parent_id": parent_id, "order_btc": order_btc,
                "net_delta": net_delta, "degraded_retry": degraded_retry,
                "spread_types": spread_types, "grouped": grouped}

    def put(self, item):
        self.items.append(item)
        return True


async def _false():
    return False


def _ctx(repo, *, minimum=10.0, scope="ACCOUNT_NET",
         put_min=None, call_min=None):
    def side(value):
        return SimpleNamespace(
            rebalance_outer_equity_pct=0.02,
            min_rebalance_usd=minimum if value is None else value,
            rebalance_cooldown_sec=360,
            emergency_trigger_multiplier=2.0)

    return SimpleNamespace(
        repo=repo, queue=Queue(), ws=object(), hedge_frozen=False,
        force_close_active=_false,
        cfg=SimpleNamespace(
            strategy=SimpleNamespace(currency="BTC"),
            debug=SimpleNamespace(bep_override=None, trigger_override=None),
            hedge=SimpleNamespace(
                delta_account_risk_pct=0.02, delta_trigger_pct=0.10,
                rebalance_outer_equity_pct=0.02,
                rebalance_inner_ratio=0.0,
                min_rebalance_usd=minimum,
                rebalance_cooldown_sec=360,
                emergency_trigger_multiplier=2.0,
                hedge_scope=scope,
                put=side(put_min), call=side(call_min),
                perp_contract_usd=10.0,
                bep_activation_progress=0.20,
                hedge_release_progress=0.05,
                call_release_pct=0.98, put_release_pct=1.02,
            ),
        ),
    )


def _summary():
    return SimpleNamespace(equity_btc=1.0)


@pytest.mark.asyncio
async def test_inactive_put_does_not_cancel_active_call_target():
    ctx = _ctx(Repo())
    view = await hedge.portfolio_hedge_view(
        ctx, parents=PARENTS, opts=OPTIONS, summary=_summary(),
        index_price=68000.0)

    assert view.option_delta == pytest.approx(-0.05)
    assert view.target_usd_by_parent == {1: 3400.0}
    assert view.net_delta == pytest.approx(-0.05)


@pytest.mark.asyncio
async def test_active_call_and_put_targets_are_calculated_separately_then_summed():
    repo = Repo(states={1: "HEDGE_ACTIVE", 2: "HEDGE_ACTIVE"})
    ctx = _ctx(repo)
    view = await hedge.portfolio_hedge_view(
        ctx, parents=PARENTS, opts=OPTIONS, summary=_summary(),
        index_price=68000.0)

    assert view.target_usd_by_parent == {1: 3400.0, 2: -2720.0}
    assert sum(view.target_usd_by_parent.values()) == pytest.approx(680.0)
    assert view.option_delta == pytest.approx(-0.01)


@pytest.mark.asyncio
async def test_release_pending_parent_is_not_an_active_target():
    repo = Repo(states={1: "HEDGE_RELEASE_PENDING", 2: "HEDGE_INACTIVE"})
    ctx = _ctx(repo)
    view = await hedge.portfolio_hedge_view(
        ctx, parents=PARENTS, opts=OPTIONS, summary=_summary(),
        index_price=68000.0)

    assert view is None


@pytest.mark.asyncio
async def test_account_rebalance_is_blocked_while_any_release_is_pending():
    repo = Repo(states={1: "HEDGE_ACTIVE", 2: "HEDGE_RELEASE_PENDING"})
    ctx = _ctx(repo, scope="SIDE_BOOK", put_min=10.0, call_min=10.0)

    view = await hedge.scan_account_hedge(
        ctx, parents=PARENTS, opts=OPTIONS, summary=_summary(),
        index_price=68000.0)

    assert view is not None
    assert ctx.queue.items == []


@pytest.mark.asyncio
async def test_managed_fill_allocation_preserves_exact_account_total():
    repo = Repo(
        states={1: "HEDGE_ACTIVE", 2: "HEDGE_ACTIVE"},
        managed={1: 100.0, 2: -50.0})
    ctx = _ctx(repo)
    view = await hedge.portfolio_hedge_view(
        ctx, parents=PARENTS, opts=OPTIONS, summary=_summary(),
        index_price=68000.0)

    await hedge._allocate_managed_fill(
        ctx, view, requested_usd=630.0, filled_usd=210.0)

    total = sum(row["futures_usd"] for row in repo.hedges.values())
    assert total == pytest.approx(260.0)
    assert all(row["futures_usd"] % 10 == 0
               for row in repo.hedges.values())


@pytest.mark.asyncio
async def test_cooldown_skips_normal_rebalance(monkeypatch):
    now = 2_000_000
    repo = Repo(last=now - 30_000)
    ctx = _ctx(repo)
    monkeypatch.setattr(hedge.market_data, "now_ms", lambda: now)
    # -0.03 超过 outer 0.02，但未超过 emergency 0.04。
    options = [SimpleNamespace(instrument="C-S", delta=-0.04),
               SimpleNamespace(instrument="C-B", delta=0.01)]

    await hedge.scan_account_hedge(
        ctx, parents=PARENTS, opts=options, summary=_summary(),
        index_price=68000.0)

    assert ctx.queue.items == []
    assert repo.actions[-1]["skipped_reason"] == "COOLDOWN"


@pytest.mark.asyncio
async def test_emergency_delta_bypasses_cooldown(monkeypatch):
    now = 2_000_000
    repo = Repo(last=now - 30_000)
    ctx = _ctx(repo)
    monkeypatch.setattr(hedge.market_data, "now_ms", lambda: now)
    options = [SimpleNamespace(instrument="C-S", delta=-0.11),
               SimpleNamespace(instrument="C-B", delta=0.01)]

    await hedge.scan_account_hedge(
        ctx, parents=PARENTS, opts=options, summary=_summary(),
        index_price=68000.0)

    assert len(ctx.queue.items) == 1
    assert ctx.queue.items[0]["order_btc"] == pytest.approx(0.1)


@pytest.mark.asyncio
async def test_new_activation_uses_grouped_open_but_maintenance_does_not(
        monkeypatch):
    repo = Repo(states={1: "HEDGE_ACTIVE", 2: "HEDGE_INACTIVE"})
    ctx = _ctx(repo, minimum=10.0)
    ctx.hedge_grouped_open_pending = {1}
    monkeypatch.setattr(hedge.market_data, "now_ms", lambda: 2_000_000)

    await hedge.scan_account_hedge(
        ctx, parents=PARENTS, opts=OPTIONS, summary=_summary(),
        index_price=68000.0)

    assert ctx.queue.items[0]["grouped"] is True
    assert ctx.hedge_grouped_open_pending == set()


@pytest.mark.asyncio
async def test_small_rebalance_is_explicitly_skipped():
    repo = Repo()
    ctx = _ctx(repo, minimum=5000.0)

    await hedge.scan_account_hedge(
        ctx, parents=PARENTS, opts=OPTIONS, summary=_summary(),
        index_price=68000.0)

    assert ctx.queue.items == []
    assert repo.actions[-1]["skipped_reason"] == "MIN_REBALANCE_USD"


@pytest.mark.asyncio
async def test_side_books_apply_minimums_independently_then_net_one_order():
    repo = Repo(states={1: "HEDGE_ACTIVE", 2: "HEDGE_ACTIVE"})
    ctx = _ctx(repo, scope="SIDE_BOOK", put_min=10.0, call_min=5000.0)

    await hedge.scan_account_hedge(
        ctx, parents=PARENTS, opts=OPTIONS, summary=_summary(),
        index_price=68000.0)

    assert len(ctx.queue.items) == 1
    assert ctx.queue.items[0]["spread_types"] == ["PUT_CREDIT"]
    assert ctx.queue.items[0]["order_btc"] == pytest.approx(-0.04)
    assert repo.actions[-1]["book_key"] == "CALL_CREDIT"
    assert repo.actions[-1]["skipped_reason"] == "MIN_REBALANCE_USD"
