from types import SimpleNamespace

import pytest

from src.engine import closer
from src.execution.adapter import CloseResult


def position(instrument: str, size: float):
    return SimpleNamespace(instrument=instrument, size=size)


def parent():
    return {
        "id": 7,
        "state": "OPEN",
        "qty_total": 5.1,
        "net_credit_btc": 0.004,
        "sell_instrument": "BTC-SELL",
        "buy_instrument": "BTC-BUY",
    }


def test_residual_plan_repairs_inverted_spread_directions():
    plan = closer.residual_close_plan(
        parent(),
        [position("BTC-SELL", 4.0), position("BTC-BUY", -4.0)],
        keep_buy=False,
        min_lot=0.1,
    )

    assert [(item["instrument"], item["direction"], item["qty"])
            for item in plan] == [
        ("BTC-SELL", "sell", 4.0),
        ("BTC-BUY", "buy", 4.0),
    ]


def test_sell_leg_only_exit_preserves_positive_protective_leg():
    plan = closer.residual_close_plan(
        parent(),
        [position("BTC-SELL", -1.2), position("BTC-BUY", 5.1)],
        keep_buy=True,
        min_lot=0.1,
    )

    assert [(item["instrument"], item["direction"], item["qty"])
            for item in plan] == [
        ("BTC-SELL", "buy", 1.2),
    ]


def test_residual_plan_preserves_other_batches_on_same_instruments():
    plan = closer.residual_close_plan(
        parent(),
        [position("BTC-SELL", -3.0), position("BTC-BUY", 3.0)],
        keep_buy=False,
        min_lot=0.1,
        reserved_sizes={"BTC-SELL": -2.0, "BTC-BUY": 2.0},
    )

    assert [(item["instrument"], item["direction"], item["qty"])
            for item in plan] == [
        ("BTC-SELL", "buy", 1.0),
        ("BTC-BUY", "sell", 1.0),
    ]


class FakeWs:
    async def request(self, method, params):
        return {}


class FakeRepo:
    def __init__(self):
        self.parent = parent()
        self.conn = object()
        self.fills = []
        self.transitions = []
        self.marked_legs = []
        self.groups = [
            {
                "id": index,
                "state": "OPEN",
                "qty": qty,
            }
            for index, qty in enumerate((1.3, 1.3, 1.3, 1.2), start=1)
        ]

    async def get_parent(self, parent_id):
        return self.parent

    async def transition_parent(self, parent_id, state, *args, **kwargs):
        self.parent["state"] = state
        self.transitions.append(state)

    async def set_close_reason(self, parent_id, reason):
        self.parent["close_reason"] = reason

    async def add_leg_closed(self, parent_id, role, qty):
        return None

    async def record_fill(self, **fill):
        self.fills.append(fill)

    async def set_liquidity_role(self, parent_id, phase, role):
        return None

    async def mark_leg_closed(self, parent_id, role):
        self.marked_legs.append(role)

    async def get_hedge(self, parent_id):
        return None

    async def close_cashflows(self, parent_id):
        buy = sum(
            fill["qty"] * (fill["price"] or 0)
            for fill in self.fills if fill["direction"] == "buy")
        sell = sum(
            fill["qty"] * (fill["price"] or 0)
            for fill in self.fills if fill["direction"] == "sell")
        return buy, sell

    async def groups_of(self, parent_id):
        return self.groups

    async def transition_group(self, group_id, state, **kwargs):
        group = next(item for item in self.groups if item["id"] == group_id)
        group["state"] = state


def ctx(repo):
    return SimpleNamespace(
        repo=repo,
        ws=FakeWs(),
        hedge_frozen=False,
        cfg=SimpleNamespace(
            strategy=SimpleNamespace(currency="BTC", min_lot=0.1),
            exit=SimpleNamespace(keep_buy_leg_on_normal_exit=False),
            execution=SimpleNamespace(n_exec_groups=4),
            hedge=SimpleNamespace(
                perp_instrument="BTC-PERPETUAL",
                perp_contract_usd=10.0,
            ),
        ),
    )


@pytest.mark.asyncio
async def test_partial_close_uses_actual_residue_and_only_then_marks_closed(
    monkeypatch,
):
    repo = FakeRepo()
    context = ctx(repo)
    position_reads = iter([
        [position("BTC-SELL", -5.1), position("BTC-BUY", 5.1)],
        [position("BTC-SELL", -1.1), position("BTC-BUY", 1.1)],
        [],
    ])
    spread_calls = []
    single_calls = []
    emitted = []

    async def get_positions(*args, **kwargs):
        return next(position_reads)

    async def close_spread(*args, **kwargs):
        spread_calls.append(kwargs["qty"])
        return CloseResult(
            closed_qty=4.0,
            buy_back_avg=0.010,
            sell_out_avg=0.003,
            complete=False,
            liquidity_role="TAKER",
        )

    async def close_short(*args, **kwargs):
        single_calls.append(("buy", kwargs["sell_instrument"], kwargs["qty"]))
        return CloseResult(
            closed_qty=kwargs["qty"],
            buy_back_avg=0.011,
            sell_out_avg=0.0,
            complete=True,
            liquidity_role="TAKER",
        )

    async def close_long(*args, **kwargs):
        single_calls.append(("sell", kwargs["instrument"], kwargs["qty"]))
        return CloseResult(
            closed_qty=kwargs["qty"],
            buy_back_avg=0.0,
            sell_out_avg=0.002,
            complete=True,
            liquidity_role="TAKER",
        )

    async def emit(*args, **kwargs):
        emitted.append((args, kwargs))

    async def close_hedge(*args, **kwargs):
        return 0.0

    monkeypatch.setattr(closer.account, "get_option_positions", get_positions)
    monkeypatch.setattr(
        closer.adapter, "close_spread_both_legs", close_spread)
    monkeypatch.setattr(closer.adapter, "close_sell_leg_only", close_short)
    monkeypatch.setattr(closer.adapter, "close_long_leg", close_long)
    monkeypatch.setattr(closer.ev, "emit", emit)
    monkeypatch.setattr(
        closer.hedge_engine, "close_managed_hedge", close_hedge)

    await closer.handle_normal_close(
        context, {"parent_id": 7, "reason": "TEST"})

    assert spread_calls == [5.1]
    assert single_calls == [
        ("buy", "BTC-SELL", 1.1),
        ("sell", "BTC-BUY", 1.1),
    ]
    assert repo.parent["state"] == "CLOSED"
    assert repo.marked_legs == ["SELL_LEG", "BUY_LEG"]
    assert "CLOSED" in repo.transitions
    assert any(
        args[1] == closer.ev.CLOSE_RESIDUAL_RECOVERY
        for args, _kwargs in emitted
    )


@pytest.mark.asyncio
async def test_close_one_batch_preserves_other_batch_on_shared_instruments(
    monkeypatch,
):
    repo = FakeRepo()
    repo.parent["qty_total"] = 1.0
    repo.groups = [{"id": 1, "state": "OPEN", "qty": 1.0,
                    "closed_qty": 0.0}]
    other = {
        "id": 8, "state": "OPEN", "qty_total": 2.0,
        "sell_instrument": "BTC-SELL", "buy_instrument": "BTC-BUY",
    }

    async def active_parents():
        rows = [other]
        if repo.parent["state"] != "CLOSED":
            rows.insert(0, repo.parent)
        return rows

    async def groups_of(parent_id):
        if parent_id == 8:
            return [{"qty": 2.0, "closed_qty": 0.0}]
        return repo.groups

    repo.active_parents = active_parents
    repo.groups_of = groups_of
    context = ctx(repo)
    position_reads = iter([
        [position("BTC-SELL", -3.0), position("BTC-BUY", 3.0)],
        [position("BTC-SELL", -2.0), position("BTC-BUY", 2.0)],
        [position("BTC-SELL", -2.0), position("BTC-BUY", 2.0)],
    ])
    spread_calls = []

    async def get_positions(*args, **kwargs):
        return next(position_reads)

    async def close_spread(*args, **kwargs):
        spread_calls.append(kwargs["qty"])
        return CloseResult(
            closed_qty=kwargs["qty"], buy_back_avg=0.010,
            sell_out_avg=0.003, complete=True, liquidity_role="TAKER")

    async def close_hedge(*args, **kwargs):
        return 0.0

    monkeypatch.setattr(closer.account, "get_option_positions", get_positions)
    monkeypatch.setattr(
        closer.adapter, "close_spread_both_legs", close_spread)
    monkeypatch.setattr(
        closer.hedge_engine, "close_managed_hedge", close_hedge)

    await closer.handle_normal_close(
        context, {"parent_id": 7, "reason": "CLOSE_PUT"})

    assert spread_calls == [1.0]
    assert repo.parent["state"] == "CLOSED"


@pytest.mark.asyncio
async def test_unfilled_inverted_residue_stays_closing(monkeypatch):
    repo = FakeRepo()
    context = ctx(repo)
    inverted = [
        position("BTC-SELL", 4.0),
        position("BTC-BUY", -4.0),
    ]
    position_reads = iter([inverted, inverted, inverted])
    emitted = []

    async def get_positions(*args, **kwargs):
        return next(position_reads)

    async def incomplete_result(*args, **kwargs):
        return CloseResult(
            closed_qty=0.0,
            buy_back_avg=0.0,
            sell_out_avg=0.0,
            complete=False,
            liquidity_role="UNKNOWN",
        )

    async def emit(*args, **kwargs):
        emitted.append((args, kwargs))

    monkeypatch.setattr(closer.account, "get_option_positions", get_positions)
    monkeypatch.setattr(closer.adapter, "close_sell_leg_only", incomplete_result)
    monkeypatch.setattr(closer.adapter, "close_long_leg", incomplete_result)
    monkeypatch.setattr(closer.ev, "emit", emit)

    await closer.handle_normal_close(
        context, {"parent_id": 7, "reason": "TEST"})

    assert repo.parent["state"] == "CLOSING"
    assert "CLOSED" not in repo.transitions
    assert repo.marked_legs == []
    assert any(
        args[1] == closer.ev.CLOSE_RESIDUAL_RETRY
        for args, _kwargs in emitted
    )


@pytest.mark.asyncio
async def test_decision_waiting_parent_finalizes_after_exchange_becomes_flat(
    monkeypatch,
):
    repo = FakeRepo()
    repo.parent["state"] = "CLOSING"
    repo.parent["close_reason"] = "RESIDUAL_LONG_DECISION_REQUIRED"
    context = ctx(repo)

    async def no_positions(*args, **kwargs):
        return []

    async def close_hedge(*args, **kwargs):
        return 0.0

    async def unexpected_order(*args, **kwargs):
        raise AssertionError("flat reconciliation must not place an option order")

    monkeypatch.setattr(closer.account, "get_option_positions", no_positions)
    monkeypatch.setattr(
        closer.adapter, "close_spread_both_legs", unexpected_order)
    monkeypatch.setattr(closer.adapter, "close_sell_leg_only", unexpected_order)
    monkeypatch.setattr(closer.adapter, "close_long_leg", unexpected_order)
    monkeypatch.setattr(
        closer.hedge_engine, "close_managed_hedge", close_hedge)

    await closer.handle_normal_close(context, {
        "parent_id": 7,
        "reason": "RESIDUAL_AUTO_RECONCILE_FLAT",
    })

    assert repo.parent["state"] == "CLOSED"
    assert repo.marked_legs == ["SELL_LEG", "BUY_LEG"]
    assert all(group["state"] == "CLOSED" for group in repo.groups)
    assert context.hedge_frozen is False
