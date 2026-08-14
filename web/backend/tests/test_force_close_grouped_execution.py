from types import SimpleNamespace

import pytest

from src.execution import adapter


@pytest.mark.asyncio
async def test_grouped_single_leg_close_uses_five_maker_first_batches(monkeypatch):
    calls = []

    class FakeExecutor:
        def __init__(self, ws, instrument, direction, params, on_event=None):
            calls.append((instrument, direction, params))
            self.params = params

        async def execute(self):
            return SimpleNamespace(
                filled=self.params.amount,
                avg_price=0.01,
                fee_btc=0.00001,
                complete=True,
                liquidity_role="MAKER",
                attempts=[{
                    "attempt_no": 1,
                    "liquidity_role": "MAKER",
                    "filled_qty": self.params.amount,
                }],
            )

    monkeypatch.setattr(adapter, "SingleLegExecutor", FakeExecutor)

    result = await adapter.close_sell_leg_only(
        object(), sell_instrument="BTC-SHORT", qty=2.3,
        min_lot=0.1, n_exec_groups=5,
        attempt_waits=(3.0, 8.0, 15.0))

    assert [call[2].amount for call in calls] == [0.5, 0.5, 0.5, 0.4, 0.4]
    assert all(call[2].rounds == 1 for call in calls)
    assert all(call[2].reduce_only is True for call in calls)
    assert all(call[2].attempt_waits == (3.0, 8.0, 15.0) for call in calls)
    assert result.closed_qty == 2.3
    assert result.complete is True
    assert result.liquidity_role == "MAKER"
    assert result.fee_btc == pytest.approx(0.00005)
    assert [attempt["group_index"] for attempt in result.attempts] == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_unfilled_quantity_carries_into_next_force_close_batch(monkeypatch):
    requested = []
    fills = iter([0.3, 0.7, 0.5, 0.5, 0.5])

    class FakeExecutor:
        def __init__(self, ws, instrument, direction, params, on_event=None):
            requested.append(params.amount)
            self.params = params

        async def execute(self):
            filled = next(fills)
            return SimpleNamespace(
                filled=filled,
                avg_price=0.02,
                fee_btc=0.00001,
                complete=filled >= self.params.amount,
                liquidity_role="TAKER",
                attempts=[],
            )

    monkeypatch.setattr(adapter, "SingleLegExecutor", FakeExecutor)

    result = await adapter.close_long_leg(
        object(), instrument="BTC-LONG", qty=2.5,
        min_lot=0.1, n_exec_groups=5)

    assert requested == [0.5, 0.7, 0.5, 0.5, 0.5]
    assert result.closed_qty == 2.5
    assert result.complete is True
    assert result.sell_out_avg == pytest.approx(0.02)
    assert result.fee_btc == pytest.approx(0.00005)
