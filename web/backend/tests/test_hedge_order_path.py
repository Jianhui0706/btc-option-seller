"""不连接交易所验证账户级对冲下单、即时重试与虚拟归属。"""

from types import SimpleNamespace

import pytest

from src.engine import hedge
from src.exchange.futures import PerpFill
from src.execution.adapter import PerpOrderResult


PARENT = {
    "id": 1, "signal_id": "ORDER-1", "state": "OPEN",
    "spread_type": "CALL_CREDIT", "open_index_price": 66000.0,
    "bep_price": 70000.0, "sell_instrument": "CALL-SHORT",
    "buy_instrument": "CALL-LONG",
}


class Repo:
    def __init__(self):
        self.state = {
            "state": "HEDGE_ACTIVE", "futures_usd": 0.0,
            "last_rebalance_at": None,
        }
        self.actions = []
        self.transitions = []
        self.fill = None

    async def open_parents(self):
        return [dict(PARENT)]

    async def get_hedge(self, parent_id):
        return dict(self.state)

    async def get_parent(self, parent_id):
        return dict(PARENT)

    async def get_signal_payload(self, signal_id):
        return {}

    async def transition_hedge(self, parent_id, state, event_code=None, **payload):
        self.state["state"] = state
        self.transitions.append((state, event_code, payload))

    async def record_hedge_action(self, **payload):
        self.actions.append(payload)

    async def set_hedge_futures(self, parent_id, usd, delta_btc):
        self.state["futures_usd"] = usd

    async def adjust_hedge_futures(self, parent_id, delta_usd, index_price):
        self.state["futures_usd"] += delta_usd
        return self.state["futures_usd"], self.state["futures_usd"] / index_price

    async def record_fill(self, **payload):
        self.fill = payload


class Queue:
    def __init__(self):
        self.items = []

    def hedge_rebalance(self, parent_id, order_btc, net_delta,
                        *, degraded_retry=False):
        return ("HEDGE_REBALANCE", parent_id, order_btc, net_delta,
                degraded_retry)

    def put(self, item):
        self.items.append(item)


async def _false():
    return False


def _ctx(repo, queue):
    return SimpleNamespace(
        repo=repo, queue=queue, ws=object(), hedge_frozen=False,
        hedge_retry_tasks={}, force_close_active=_false,
        cfg=SimpleNamespace(
            strategy=SimpleNamespace(currency="BTC"),
            execution=SimpleNamespace(manual_hedge_maker_wait_sec=10.0),
            debug=SimpleNamespace(bep_override=None, trigger_override=None),
            hedge=SimpleNamespace(
                delta_account_risk_pct=0.02, delta_trigger_pct=0.10,
                rebalance_outer_equity_pct=0.02,
                rebalance_inner_ratio=0.0, min_rebalance_usd=10.0,
                rebalance_cooldown_sec=360,
                emergency_trigger_multiplier=2.0,
                retry_rounds=3, retry_delay_sec=0.0,
                retry_tick_step=5, degraded_retry_sec=0.0,
                manual_rearm_sec=10.0,
                bep_activation_progress=0.20, hedge_release_progress=0.05,
                call_release_pct=0.98, put_release_pct=1.02,
                perp_contract_usd=10.0, ioc_ticks=5,
                perp_instrument="BTC-PERPETUAL",
            ),
        ),
    )


def _install_market(monkeypatch, repo, fills):
    options = [
        SimpleNamespace(instrument="CALL-SHORT", delta=-0.06),
        SimpleNamespace(instrument="CALL-LONG", delta=0.01),
    ]
    ticks = []

    async def fake_options(*args, **kwargs):
        return options

    async def fake_summary(*args, **kwargs):
        return SimpleNamespace(equity_btc=1.0)

    async def fake_index(*args, **kwargs):
        return 68000.0

    async def fake_ioc(*args, **kwargs):
        requested = args[2]
        ticks.append(kwargs["ioc_ticks"])
        amount = fills.pop(0)
        filled = requested if amount == "full" else float(amount)
        return PerpFill(requested_usd=requested, filled_usd=filled,
                        avg_price=68000.0 if filled else 0.0,
                        fee_btc=0.00001 if filled else 0.0,
                        order_id="auto-order-1", trade_ids=["auto-trade-1"])

    monkeypatch.setattr(hedge.account, "get_option_positions", fake_options)
    monkeypatch.setattr(hedge.account, "get_account_summary", fake_summary)
    monkeypatch.setattr(hedge.market_data, "get_index_price", fake_index)
    monkeypatch.setattr(hedge.futures, "perp_ioc", fake_ioc)
    return ticks


@pytest.mark.asyncio
async def test_triggered_scan_enqueues_rebalance():
    repo, queue = Repo(), Queue()
    ctx = _ctx(repo, queue)
    view = hedge.DeltaView(
        option_delta=-0.05, futures_delta=0.0, net_delta=-0.05,
        equity_btc=1.0, index_price=68000.0)

    # 兼容单仓调用路径仍可入队；生产扫描器使用两阶段账户汇总。
    repo.open_parents = None
    await hedge.scan_hedge(
        ctx, dict(PARENT), 68000.0, 123, view=view,
        record_check=False)

    assert queue.items and queue.items[0][0] == "HEDGE_REBALANCE"


@pytest.mark.asyncio
async def test_rebalance_execution_targets_delta_neutral(monkeypatch):
    repo, queue = Repo(), Queue()
    ctx = _ctx(repo, queue)
    ticks = _install_market(monkeypatch, repo, ["full"])

    await hedge.handle_rebalance(ctx, {
        "parent_id": 1, "order_btc": 0.0, "net_delta": -0.05,
    })

    action = next(a for a in repo.actions if a["action"] == "REBALANCE")
    assert action["requested_usd"] == pytest.approx(3400.0)
    assert action["order_usd"] == pytest.approx(3400.0)
    assert repo.state["futures_usd"] == pytest.approx(3400.0)
    assert abs(action["residual_delta"]) <= 10 / 68000
    assert ticks == [5]
    assert repo.state["state"] == "HEDGE_ACTIVE"
    assert repo.fill["phase"] == "hedge"
    assert action["details"]["liquidity_role"] == "TAKER"
    assert action["details"]["exchange_order_id"] == "auto-order-1"
    assert action["details"]["exchange_trade_ids"] == ["auto-trade-1"]


@pytest.mark.asyncio
async def test_side_book_execution_honors_queued_side_even_if_combined_threshold_differs(
        monkeypatch):
    repo, queue = Repo(), Queue()
    ctx = _ctx(repo, queue)
    _install_market(monkeypatch, repo, ["full"])

    await hedge.handle_rebalance(ctx, {
        "parent_id": 1, "order_btc": 0.05, "net_delta": -0.05,
        "spread_types": ["CALL_CREDIT"],
    })

    action = next(a for a in repo.actions if a["action"] == "REBALANCE")
    assert action["book_key"] == "CALL_CREDIT"
    assert action["requested_usd"] == pytest.approx(3400.0)


@pytest.mark.asyncio
async def test_zero_fill_retries_immediately_with_more_aggressive_ticks(monkeypatch):
    repo, queue = Repo(), Queue()
    ctx = _ctx(repo, queue)
    ticks = _install_market(monkeypatch, repo, [0.0, "full"])

    await hedge.handle_rebalance(ctx, {
        "parent_id": 1, "order_btc": 0.0, "net_delta": -0.05,
    })

    attempts = [a for a in repo.actions if a["action"] == "REBALANCE"]
    assert [a["attempt_no"] for a in attempts] == [1, 2]
    assert attempts[0]["order_usd"] == 0.0
    assert ticks == [5, 10]
    assert repo.state["state"] == "HEDGE_ACTIVE"


@pytest.mark.asyncio
async def test_partial_fill_recalculates_residual_before_retry(monkeypatch):
    repo, queue = Repo(), Queue()
    ctx = _ctx(repo, queue)
    _install_market(monkeypatch, repo, [1000.0, "full"])

    await hedge.handle_rebalance(ctx, {
        "parent_id": 1, "order_btc": 0.0, "net_delta": -0.05,
    })

    attempts = [a for a in repo.actions if a["action"] == "REBALANCE"]
    assert attempts[0]["requested_usd"] == pytest.approx(3400.0)
    assert attempts[1]["requested_usd"] == pytest.approx(2400.0)
    assert repo.state["futures_usd"] == pytest.approx(3400.0)


@pytest.mark.asyncio
async def test_exhausted_retries_enter_degraded_and_schedule_retry(monkeypatch):
    repo, queue = Repo(), Queue()
    ctx = _ctx(repo, queue)
    ticks = _install_market(monkeypatch, repo, [0.0, 0.0, 0.0])

    await hedge.handle_rebalance(ctx, {
        "parent_id": 1, "order_btc": 0.0, "net_delta": -0.05,
    })
    await ctx.hedge_retry_tasks[1]

    assert ticks == [5, 10, 15]
    assert repo.state["state"] == "HEDGE_DEGRADED"
    assert queue.items[-1][-1] is True


@pytest.mark.asyncio
async def test_release_rounds_legacy_fractional_attribution_to_contract_size(
        monkeypatch):
    repo, queue = Repo(), Queue()
    repo.state["futures_usd"] = 77778.03808212245
    ctx = _ctx(repo, queue)
    requested = []

    async def fake_ioc(*args, **kwargs):
        amount = float(args[2])
        requested.append(amount)
        assert amount % 10 == 0
        return PerpFill(
            requested_usd=amount, filled_usd=amount,
            avg_price=68000.0, fee_btc=0.00001)

    monkeypatch.setattr(hedge.futures, "perp_ioc", fake_ioc)

    remaining = await hedge.close_managed_hedge(ctx, 1, action="RELEASE")

    assert requested == [-77780.0]
    assert remaining == 0.0
    assert repo.state["futures_usd"] == 0.0


@pytest.mark.asyncio
async def test_manual_rebalance_uses_five_group_maker_first_not_ioc(monkeypatch):
    repo, queue = Repo(), Queue()
    ctx = _ctx(repo, queue)
    _install_market(monkeypatch, repo, ["full"])
    calls = []

    async def maker_first(*args, **kwargs):
        calls.append(kwargs)
        usd = kwargs["usd_signed"]
        return PerpOrderResult(usd, usd, 68000.0, liquidity_role="MIXED")

    async def forbidden_ioc(*args, **kwargs):
        raise AssertionError("manual hedge must not use the direct IOC path")

    monkeypatch.setattr(
        hedge.execution_adapter, "execute_perp_maker_first", maker_first)
    monkeypatch.setattr(hedge.futures, "perp_ioc", forbidden_ioc)

    await hedge.handle_rebalance(ctx, {
        "parent_id": 1, "order_btc": 0.0, "net_delta": -0.05,
        "manual": True,
    })

    assert len(calls) == 1
    assert calls[0]["usd_signed"] == pytest.approx(3400.0)
    assert calls[0]["n_exec_groups"] == 5
    assert calls[0]["attempt_waits"] == (10.0, 10.0, 0.0)
    assert calls[0]["reduce_only"] is False
    action = next(a for a in repo.actions if a["action"] == "REBALANCE")
    assert action["max_attempts"] == 1


@pytest.mark.asyncio
async def test_automatic_initial_hedge_uses_five_group_maker_first(monkeypatch):
    repo, queue = Repo(), Queue()
    ctx = _ctx(repo, queue)
    _install_market(monkeypatch, repo, ["full"])
    calls = []

    async def maker_first(*args, **kwargs):
        calls.append(kwargs)
        usd = kwargs["usd_signed"]
        return PerpOrderResult(usd, usd, 68000.0, liquidity_role="MAKER")

    async def forbidden_ioc(*args, **kwargs):
        raise AssertionError("initial automatic hedge must use grouped Maker-first")

    monkeypatch.setattr(
        hedge.execution_adapter, "execute_perp_maker_first", maker_first)
    monkeypatch.setattr(hedge.futures, "perp_ioc", forbidden_ioc)

    await hedge.handle_rebalance(ctx, {
        "parent_id": 1, "order_btc": 0.0, "net_delta": -0.05,
        "grouped": True,
    })

    assert len(calls) == 1
    assert calls[0]["n_exec_groups"] == 5
    assert calls[0]["attempt_waits"] == (10.0, 10.0, 0.0)
    action = next(a for a in repo.actions if a["action"] == "REBALANCE")
    assert action["max_attempts"] == 1
    assert action["details"]["grouped"] is True
    assert action["details"]["manual"] is False


@pytest.mark.asyncio
async def test_full_manual_five_group_fill_does_not_chase_live_delta_drift(
        monkeypatch):
    repo, queue = Repo(), Queue()
    ctx = _ctx(repo, queue)
    calls = 0

    async def portfolio(*args, **kwargs):
        nonlocal calls
        calls += 1
        net_delta = -0.05 if calls == 1 else 0.02
        return hedge.PortfolioHedgeView(
            option_delta=net_delta,
            futures_delta=0.0,
            net_delta=net_delta,
            equity_btc=1.0,
            index_price=68000.0,
            controller_parent_id=1,
            target_usd_by_parent={1: 3400.0},
            managed_usd_by_parent={1: 0.0},
            last_rebalance_at=None,
        )

    async def params(*args, **kwargs):
        return hedge.TradeHedgeParams(0.02, 0.10, 0.98, 1.02)

    async def maker_first(*args, **kwargs):
        usd = kwargs["usd_signed"]
        return PerpOrderResult(usd, usd, 68000.0, liquidity_role="MIXED")

    async def allocate(*args, **kwargs):
        repo.state["futures_usd"] = kwargs.get("filled_usd", args[-1])
        return repo.state["futures_usd"]

    monkeypatch.setattr(hedge, "portfolio_hedge_view", portfolio)
    monkeypatch.setattr(hedge, "trade_hedge_params", params)
    monkeypatch.setattr(
        hedge.execution_adapter, "execute_perp_maker_first", maker_first)
    monkeypatch.setattr(hedge, "_allocate_managed_fill", allocate)

    await hedge.handle_rebalance(ctx, {
        "parent_id": 1, "order_btc": 0.05, "net_delta": -0.05,
        "manual": True, "spread_types": ["CALL_CREDIT"],
    })

    assert calls == 2
    assert repo.state["state"] == "HEDGE_ACTIVE"
    assert repo.state["futures_usd"] == pytest.approx(3400.0)
    assert ctx.hedge_retry_tasks == {}


@pytest.mark.asyncio
async def test_manual_release_uses_maker_first_reduce_only(monkeypatch):
    repo, queue = Repo(), Queue()
    repo.state.update({
        "state": "HEDGE_RELEASE_PENDING",
        "futures_usd": 2500.0,
    })
    ctx = _ctx(repo, queue)
    calls = []

    async def maker_first(*args, **kwargs):
        calls.append(kwargs)
        usd = kwargs["usd_signed"]
        return PerpOrderResult(usd, usd, 68000.0, liquidity_role="MIXED")

    async def forbidden_ioc(*args, **kwargs):
        raise AssertionError("manual hedge release must not use direct IOC")

    monkeypatch.setattr(
        hedge.execution_adapter, "execute_perp_maker_first", maker_first)
    monkeypatch.setattr(hedge.futures, "perp_ioc", forbidden_ioc)

    await hedge.handle_release(ctx, {"parent_id": 1, "manual": True})

    assert len(calls) == 1
    assert calls[0]["usd_signed"] == -2500.0
    assert calls[0]["n_exec_groups"] == 5
    assert calls[0]["attempt_waits"] == (10.0, 10.0, 0.0)
    assert calls[0]["reduce_only"] is True
    assert repo.state["futures_usd"] == 0.0
    assert repo.state["state"] == "HEDGE_INACTIVE"
    assert ctx.hedge_rearm_after[1] > hedge.market_data.now_ms()


@pytest.mark.asyncio
async def test_beta_release_uses_five_group_maker_first(monkeypatch):
    repo, queue = Repo(), Queue()
    repo.state.update({
        "state": "HEDGE_RELEASE_PENDING",
        "futures_usd": 2500.0,
    })
    ctx = _ctx(repo, queue)
    calls = []

    async def maker_first(*args, **kwargs):
        calls.append(kwargs)
        usd = kwargs["usd_signed"]
        return PerpOrderResult(usd, usd, 68000.0, liquidity_role="MAKER")

    async def forbidden_ioc(*args, **kwargs):
        raise AssertionError("normal beta release must use grouped Maker-first")

    monkeypatch.setattr(
        hedge.execution_adapter, "execute_perp_maker_first", maker_first)
    monkeypatch.setattr(hedge.futures, "perp_ioc", forbidden_ioc)

    await hedge.handle_release(ctx, {
        "parent_id": 1, "manual": False, "grouped": True,
    })

    assert len(calls) == 1
    assert calls[0]["n_exec_groups"] == 5
    assert calls[0]["attempt_waits"] == (10.0, 10.0, 0.0)
    assert calls[0]["reduce_only"] is True
    assert repo.state["state"] == "HEDGE_INACTIVE"
    assert not hasattr(ctx, "hedge_rearm_after")
