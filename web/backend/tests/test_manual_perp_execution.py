from types import SimpleNamespace

import pytest

from src.execution import adapter
from src.execution.single_leg import SingleLegExecutor, SingleLegParams
from src.exchange.futures import PerpFill


@pytest.mark.asyncio
async def test_maker_first_perp_uses_five_fixed_groups_and_completes_each(
        monkeypatch):
    requested = []
    completions = []

    class FakeExecutor:
        def __init__(self, ws, instrument, direction, params, on_event=None):
            requested.append((instrument, direction, params))
            self.params = params

        async def execute(self):
            return SimpleNamespace(
                filled=40.0,
                avg_price=68000.0,
                liquidity_role="MAKER",
                attempts=[],
                fee_btc=0.0,
                maker_filled=40.0,
                taker_filled=0.0,
            )

    async def fake_ioc(ws, instrument, usd_signed, **kwargs):
        completions.append((usd_signed, kwargs))
        return PerpFill(usd_signed, usd_signed, 67999.0, 0.00001)

    monkeypatch.setattr(adapter, "SingleLegExecutor", FakeExecutor)
    monkeypatch.setattr(adapter.futures, "perp_ioc", fake_ioc)

    result = await adapter.execute_perp_maker_first(
        object(), instrument="BTC-PERPETUAL", usd_signed=500.0,
        min_lot=10.0, n_exec_groups=5, reduce_only=True)

    assert [item[2].amount for item in requested] == [100.0] * 5
    assert [item[2].pay_extra_ticks for item in requested] == [10] * 5
    assert [item[0] for item in completions] == [60.0] * 5
    assert all(item[1]["ioc_ticks"] == 20 for item in completions)
    assert all(item[1]["reduce_only"] is True for item in completions)
    assert all(item[2].rounds == 1 for item in requested)
    assert all(item[2].reduce_only is True for item in requested)
    assert result.filled_usd == 500.0
    assert result.avg_price == pytest.approx(67999.4)
    assert result.liquidity_role == "MIXED"
    assert result.fee_btc == pytest.approx(0.00005)
    assert len(result.groups) == 5
    assert all(group["requested_usd"] == 100.0 for group in result.groups)
    assert all(group["filled_usd"] == 100.0 for group in result.groups)
    assert all(group["complete"] is True for group in result.groups)
    assert all(group["maker_filled_usd"] == 40.0 for group in result.groups)
    assert all(group["taker_filled_usd"] == 60.0 for group in result.groups)


@pytest.mark.asyncio
async def test_maker_first_perp_preserves_sell_direction(monkeypatch):
    class FakeExecutor:
        def __init__(self, ws, instrument, direction, params, on_event=None):
            assert direction == "sell"
            self.params = params

        async def execute(self):
            return SimpleNamespace(
                filled=self.params.amount,
                avg_price=68000.0,
                liquidity_role="TAKER",
                attempts=[],
                fee_btc=0.00001,
                maker_filled=0.0,
                taker_filled=self.params.amount,
            )

    monkeypatch.setattr(adapter, "SingleLegExecutor", FakeExecutor)

    result = await adapter.execute_perp_maker_first(
        object(), instrument="BTC-PERPETUAL", usd_signed=-20.0,
        min_lot=10.0, n_exec_groups=5)

    assert result.requested_usd == -20.0
    assert result.filled_usd == -20.0
    assert result.liquidity_role == "TAKER"
    assert [group["filled_usd"] for group in result.groups] == [-10.0, -10.0]
    assert [group["taker_filled_usd"] for group in result.groups] == [10.0, 10.0]


@pytest.mark.asyncio
async def test_dynamic_target_is_redistributed_across_remaining_five_groups(
        monkeypatch):
    requested = []
    targets = iter([500.0, 600.0, 700.0, 800.0, 900.0])

    class FakeExecutor:
        def __init__(self, ws, instrument, direction, params, on_event=None):
            requested.append(params.amount)
            self.params = params

        async def execute(self):
            return SimpleNamespace(
                filled=self.params.amount,
                avg_price=68000.0,
                liquidity_role="TAKER",
                attempts=[],
                fee_btc=0.0,
            )

    async def latest_target():
        return next(targets)

    monkeypatch.setattr(adapter, "SingleLegExecutor", FakeExecutor)

    result = await adapter.execute_perp_maker_first(
        object(), instrument="BTC-PERPETUAL", usd_signed=500.0,
        min_lot=10.0, n_exec_groups=5, target_provider=latest_target)

    assert requested == [100.0, 130.0, 160.0, 210.0, 300.0]
    assert result.requested_usd == 900.0
    assert result.filled_usd == 900.0
    assert len(result.groups) == 5
    assert [group["initial_group_usd"] for group in result.groups] == [100.0] * 5
    assert [group["live_target_usd"] for group in result.groups] == [
        500.0, 600.0, 700.0, 800.0, 900.0]
    assert [group["dynamically_recalculated"] for group in result.groups] == [
        False, True, True, True, True]


@pytest.mark.asyncio
async def test_dynamic_target_never_flips_direction_inside_one_cycle(monkeypatch):
    directions = []
    targets = iter([500.0, -600.0, -700.0, -800.0, -900.0])

    class FakeExecutor:
        def __init__(self, ws, instrument, direction, params, on_event=None):
            directions.append(direction)
            self.params = params

        async def execute(self):
            return SimpleNamespace(
                filled=self.params.amount, avg_price=68000.0,
                liquidity_role="TAKER", attempts=[], fee_btc=0.0)

    async def latest_target():
        return next(targets)

    monkeypatch.setattr(adapter, "SingleLegExecutor", FakeExecutor)

    result = await adapter.execute_perp_maker_first(
        object(), instrument="BTC-PERPETUAL", usd_signed=500.0,
        min_lot=10.0, n_exec_groups=5, target_provider=latest_target)

    assert directions == ["buy"] * 5
    assert result.requested_usd == 500.0
    assert result.filled_usd == 500.0


@pytest.mark.asyncio
async def test_perp_attempt_callback_publishes_each_group_immediately(
        monkeypatch):
    published = []

    class FakeExecutor:
        def __init__(self, ws, instrument, direction, params, on_event=None,
                     on_attempt=None):
            self.params = params
            self.on_attempt = on_attempt

        async def execute(self):
            item = {
                "attempt_no": 1, "round": 1,
                "liquidity_role": "MAKER",
                "requested_qty": self.params.amount,
                "filled_qty": self.params.amount,
                "status": "FILLED", "placed_at": 100,
                "finished_at": 200, "duration_ms": 100,
            }
            await self.on_attempt(item)
            return SimpleNamespace(
                filled=self.params.amount, avg_price=68000.0,
                liquidity_role="MAKER", attempts=[item], fee_btc=0.0,
                maker_filled=self.params.amount, taker_filled=0.0)

    async def publish(item):
        published.append(item)

    monkeypatch.setattr(adapter, "SingleLegExecutor", FakeExecutor)

    await adapter.execute_perp_maker_first(
        object(), instrument="BTC-PERPETUAL", usd_signed=20.0,
        min_lot=10.0, n_exec_groups=2, on_attempt=publish)

    assert [item["group_index"] for item in published] == [1, 2]
    assert all(item["group_count"] == 2 for item in published)
    assert all(item["operation"] == "manual_perp" for item in published)


@pytest.mark.asyncio
async def test_single_leg_maker_attempts_record_incremental_not_cumulative_fill(
        monkeypatch):
    executor = SingleLegExecutor(
        object(), "BTC-PERPETUAL", "sell",
        SingleLegParams(amount=10.0, min_lot=1.0,
                        attempt_waits=(0.0, 0.0, 0.0), rounds=1))
    reconcile_calls = 0

    async def fake_setup():
        return None

    async def fake_place(qty, price, ioc):
        return {"order": {"order_id": f"order-{reconcile_calls + 1}"}}

    async def fake_reconcile():
        nonlocal reconcile_calls
        reconcile_calls += 1
        if reconcile_calls == 1:
            executor._add_fill(7.0, 64000.0, "MAKER")
        elif reconcile_calls == 2:
            executor._add_fill(3.0, 64010.0, "MAKER")
        executor._order_id = None

    monkeypatch.setattr(executor, "_setup", fake_setup)
    monkeypatch.setattr(executor, "_quote", lambda attempt: 64000.0 + attempt)
    monkeypatch.setattr(executor, "_place", fake_place)
    monkeypatch.setattr(executor, "_reconcile_and_cancel", fake_reconcile)

    report = await executor.execute()

    assert [item["requested_qty"] for item in report.attempts] == [10.0, 3.0]
    assert [item["filled_qty"] for item in report.attempts] == [7.0, 3.0]
    assert [item["status"] for item in report.attempts] == ["PARTIAL", "FILLED"]
    assert all(item["fill_qty_is_incremental"] for item in report.attempts)
    assert all(item["placed_at"] <= item["finished_at"]
               for item in report.attempts)
