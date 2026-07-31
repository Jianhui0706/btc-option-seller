import time
from types import SimpleNamespace

import pytest

from src.engine import closer, risk
from src.exchange.deribit_ws import DeribitError
from src.execution.spread_executor import (
    ExecParams,
    OrderCancellationUnconfirmed,
    SpreadExecutor,
)


class CaptureWs:
    def __init__(self):
        self.calls = []

    async def request(self, method, params):
        self.calls.append((method, params))
        return {"order": {"order_id": "o-1", "price": params.get("price")}}


@pytest.mark.asyncio
async def test_close_orders_are_reduce_only_and_open_orders_are_not():
    close_ws = CaptureWs()
    close_executor = SpreadExecutor(
        close_ws, "BTC-SELL", "BTC-BUY",
        ExecParams(amount=0.8, operation_kind="close"))
    await close_executor._place(
        "BTC-SELL", "buy", 0.8, 0.01,
        post_only=True, label="spread-batch-maker")

    assert close_ws.calls[-1][1]["reduce_only"] is True

    open_ws = CaptureWs()
    open_executor = SpreadExecutor(
        open_ws, "BTC-BUY", "BTC-SELL",
        ExecParams(amount=0.8, operation_kind="open"))
    await open_executor._place(
        "BTC-BUY", "buy", 0.8, 0.01,
        post_only=True, label="spread-batch-maker")

    assert "reduce_only" not in open_ws.calls[-1][1]


class NeverCancelsWs:
    async def request(self, method, params):
        if method == "private/get_order_state":
            return {
                "order_id": params["order_id"],
                "order_state": "open",
                "filled_amount": 0.0,
                "average_price": 0.0,
            }
        if method == "private/cancel":
            raise DeribitError(10028, "too_many_requests")
        raise AssertionError(method)


@pytest.mark.asyncio
async def test_unconfirmed_cancel_keeps_order_owned_and_aborts(monkeypatch):
    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(
        "src.execution.spread_executor.asyncio.sleep", no_wait)
    executor = SpreadExecutor(
        NeverCancelsWs(), "BTC-SELL", "BTC-BUY",
        ExecParams(amount=0.8, operation_kind="close"))
    leg = executor.legs["BTC-SELL"]
    leg.order_id = "live-order"
    leg.order_qty = 0.8

    with pytest.raises(OrderCancellationUnconfirmed):
        await executor._reconcile_and_cancel(leg, 1, 1)

    assert leg.order_id == "live-order"
    assert any("撤单未确认" in item for item in executor.report.events)


@pytest.mark.asyncio
async def test_close_leg_imbalance_never_reopens_the_leading_leg():
    executor = SpreadExecutor(
        CaptureWs(), "BTC-SELL", "BTC-BUY",
        ExecParams(amount=0.8, operation_kind="close"))
    executor.legs["BTC-SELL"].filled = 0.8
    executor.legs["BTC-BUY"].filled = 0.0

    balanced = await executor._rebalance_legs()

    assert balanced is False
    assert executor.ws.calls == []
    assert any("不反向重开" in item for item in executor.report.events)


@pytest.mark.asyncio
async def test_managed_order_cleanup_does_not_cancel_manual_orders():
    class ManualOrderWs:
        def __init__(self):
            self.cancelled = []

        async def request(self, method, params):
            if method == "private/get_open_orders_by_currency":
                return [{
                    "order_id": "manual-1",
                    "instrument_name": "BTC-SELL",
                    "direction": "buy",
                    "amount": 0.8,
                    "label": "manual-order",
                }]
            if method == "private/cancel":
                self.cancelled.append(params["order_id"])
                return {}
            raise AssertionError(method)

    ws = ManualOrderWs()
    context = SimpleNamespace(
        ws=ws,
        repo=SimpleNamespace(conn=object()),
        cfg=SimpleNamespace(
            strategy=SimpleNamespace(currency="BTC")),
    )
    managed_parent = {
        "id": 7,
        "sell_instrument": "BTC-SELL",
        "buy_instrument": "BTC-BUY",
    }

    safe = await closer.cancel_managed_option_orders(
        context, managed_parent)

    assert safe is True
    assert ws.cancelled == []


class RecoveryRepo:
    conn = object()

    def __init__(self):
        self.parent = {
            "id": 112,
            "state": "CLOSED",
            "closed_at": int(time.time() * 1000) - 60_000,
            "sell_instrument": "BTC-58000-P",
            "buy_instrument": "BTC-56000-P",
            "qty_total": 4.2,
        }

    async def active_parents(self):
        return []

    async def recent_terminal_parents(self, limit=50):
        return [self.parent]


class LateFillWs:
    async def request(self, method, params):
        if method == "private/get_user_trades_by_currency_and_time":
            return {
                "trades": [
                    {
                        "trade_id": "t-1",
                        "order_id": "o-1",
                        "timestamp": params["start_timestamp"] + 1000,
                        "instrument_name": "BTC-58000-P",
                        "direction": "buy",
                        "amount": 0.8,
                        "label": "spread-batch-maker",
                    },
                    {
                        "trade_id": "t-2",
                        "order_id": "o-2",
                        "timestamp": params["start_timestamp"] + 2000,
                        "instrument_name": "BTC-56000-P",
                        "direction": "sell",
                        "amount": 0.8,
                        "label": "spread-batch-maker",
                    },
                ]
            }
        raise AssertionError(method)


def recovery_context():
    return SimpleNamespace(
        repo=RecoveryRepo(),
        ws=LateFillWs(),
        cfg=SimpleNamespace(
            strategy=SimpleNamespace(currency="BTC", min_lot=0.1),
        ),
    )


@pytest.mark.asyncio
async def test_terminal_late_fills_are_recovered_only_after_label_attribution(
    monkeypatch,
):
    context = recovery_context()
    positions = [
        SimpleNamespace(instrument="BTC-58000-P", size=0.8),
        SimpleNamespace(instrument="BTC-56000-P", size=-0.8),
    ]
    actions = [{"instrument": "BTC-58000-P", "filled": 0.8}]
    emitted = []

    async def cancel_orders(*args, **kwargs):
        return True

    async def flatten(*args, **kwargs):
        positions.clear()
        return actions

    async def get_positions(*args, **kwargs):
        return positions

    async def emit(*args, **kwargs):
        emitted.append((args, kwargs))

    monkeypatch.setattr(closer, "cancel_managed_option_orders", cancel_orders)
    monkeypatch.setattr(
        closer, "_flatten_managed_option_residuals", flatten)
    monkeypatch.setattr(risk.account, "get_option_positions", get_positions)
    monkeypatch.setattr(risk.ev, "emit", emit)

    recovered = await risk.recover_terminal_late_fills(
        context,
        [
            SimpleNamespace(instrument="BTC-58000-P", size=0.8),
            SimpleNamespace(instrument="BTC-56000-P", size=-0.8),
        ],
    )

    assert recovered is True
    assert any(
        args[1] == risk.ev.CLOSE_RESIDUAL_RECOVERY
        and kwargs.get("source") == "TERMINAL_LATE_FILL"
        for args, kwargs in emitted
    )


@pytest.mark.asyncio
async def test_unlabelled_terminal_position_is_never_auto_closed(monkeypatch):
    context = recovery_context()

    async def unlabelled(method, params):
        assert method == "private/get_user_trades_by_currency_and_time"
        return {
            "trades": [{
                "instrument_name": "BTC-56000-P",
                "direction": "sell",
                "amount": 0.8,
                "label": "manual-order",
            }]
        }

    flattened = False

    async def flatten(*args, **kwargs):
        nonlocal flattened
        flattened = True
        return []

    context.ws.request = unlabelled
    monkeypatch.setattr(
        closer, "_flatten_managed_option_residuals", flatten)

    recovered = await risk.recover_terminal_late_fills(
        context,
        [SimpleNamespace(instrument="BTC-56000-P", size=-0.8)],
    )

    assert recovered is None
    assert flattened is False
