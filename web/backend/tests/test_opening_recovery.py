from types import SimpleNamespace

import pytest

from src.engine import risk
from src.execution.adapter import CloseResult
from src.execution.single_leg import SingleLegExecutor, SingleLegParams


def option(instrument: str, size: float):
    return SimpleNamespace(instrument=instrument, size=size)


class RecoveryRepo:
    conn = object()

    def __init__(self):
        self.parent = {
            "id": 84,
            "signal_id": "sig-84",
            "state": "OPENING",
            "spread_type": "CALL_CREDIT",
            "sell_instrument": "BTC-SELL-C",
            "buy_instrument": "BTC-BUY-C",
            "sell_strike": 71000.0,
            "buy_strike": 75000.0,
            "expiry_ts": 123456789,
            "created_at": 1000,
        }
        self.transitions = []
        self.open_details = None
        self.legs = None
        self.groups = None

    async def get_parent(self, parent_id):
        return self.parent

    async def transition_parent(self, parent_id, state, *args, **kwargs):
        self.parent["state"] = state
        self.transitions.append((state, kwargs))

    async def fill_open_details(self, parent_id, **kwargs):
        self.open_details = kwargs

    async def create_legs(self, parent_id, *args):
        self.legs = args

    async def get_signal_payload(self, signal_id):
        return {"group_split_count": 4}

    async def create_groups(self, parent_id, quantities):
        self.groups = quantities

    async def ensure_hedge_row(self, parent_id):
        return None


class RecoveryWs:
    def __init__(self):
        self.cancelled = []

    async def request(self, method, params):
        if method == "private/cancel_all_by_instrument":
            self.cancelled.append(params["instrument_name"])
            return {}
        if method == "private/get_user_trades_by_instrument":
            if params["instrument_name"] == "BTC-SELL-C":
                return {
                    "trades": [{
                        "direction": "sell", "amount": 1.6, "price": 0.007,
                    }]
                }
            return {
                "trades": [{
                    "direction": "buy", "amount": 2.4, "price": 0.0028,
                }]
            }
        raise AssertionError((method, params))


def recovery_ctx(repo, ws):
    return SimpleNamespace(
        repo=repo,
        ws=ws,
        cfg=SimpleNamespace(
            strategy=SimpleNamespace(currency="BTC", min_lot=0.1),
            execution=SimpleNamespace(n_exec_groups=5),
            group_split_count_default=4,
            debug=SimpleNamespace(bep_override=None),
        ),
    )


@pytest.mark.asyncio
async def test_opening_recovery_unwinds_extra_protection_and_adopts_matched_spread(
    monkeypatch,
):
    repo = RecoveryRepo()
    ws = RecoveryWs()
    ctx = recovery_ctx(repo, ws)
    reads = iter([
        [option("BTC-SELL-C", -1.6), option("BTC-BUY-C", 2.4)],
        [option("BTC-SELL-C", -1.6), option("BTC-BUY-C", 2.4)],
        [option("BTC-SELL-C", -1.6), option("BTC-BUY-C", 1.6)],
    ])
    closed = []

    async def positions(*args, **kwargs):
        return next(reads)

    async def close_long(*args, **kwargs):
        closed.append((kwargs["instrument"], kwargs["qty"]))
        return CloseResult(
            closed_qty=0.8,
            buy_back_avg=0.0,
            sell_out_avg=0.0027,
            complete=True,
            liquidity_role="TAKER",
        )

    async def index_price(*args, **kwargs):
        return 65000.0

    async def emit(*args, **kwargs):
        return None

    monkeypatch.setattr(risk.account, "get_option_positions", positions)
    monkeypatch.setattr(risk.adapter, "close_long_leg", close_long)
    monkeypatch.setattr(
        "src.exchange.market_data.get_index_price", index_price)
    monkeypatch.setattr(risk.ev, "emit", emit)

    result = await risk.recover_opening_parent(ctx, 84)

    assert closed == [("BTC-BUY-C", 0.8)]
    assert result["state"] == "OPEN"
    assert repo.open_details["qty_total"] == 1.6
    assert repo.legs[1] == 1.6
    assert repo.legs[4] == 1.6
    assert sum(repo.groups) == pytest.approx(1.6)
    assert set(ws.cancelled) == {"BTC-SELL-C", "BTC-BUY-C"}


@pytest.mark.asyncio
async def test_opening_recovery_keeps_opening_when_extra_leg_cannot_be_closed(
    monkeypatch,
):
    repo = RecoveryRepo()
    ws = RecoveryWs()
    ctx = recovery_ctx(repo, ws)
    positions_now = [
        option("BTC-SELL-C", -1.6),
        option("BTC-BUY-C", 2.4),
    ]
    emitted = []

    async def positions(*args, **kwargs):
        return positions_now

    async def close_long(*args, **kwargs):
        return CloseResult(
            closed_qty=0.0,
            buy_back_avg=0.0,
            sell_out_avg=0.0,
            complete=False,
            liquidity_role="UNKNOWN",
        )

    async def emit(*args, **kwargs):
        emitted.append((args, kwargs))

    monkeypatch.setattr(risk.account, "get_option_positions", positions)
    monkeypatch.setattr(risk.adapter, "close_long_leg", close_long)
    monkeypatch.setattr(risk.ev, "emit", emit)

    result = await risk.recover_opening_parent(ctx, 84)

    assert result["state"] == "OPENING"
    assert repo.open_details is None
    assert any(
        args[1] == risk.ev.RECONCILE_MISMATCH
        and kwargs.get("note") == "OPENING recovery still has unmatched legs"
        for args, kwargs in emitted
    )


@pytest.mark.asyncio
async def test_single_leg_uses_public_snapshot_when_stream_first_frame_is_lost(
    monkeypatch,
):
    class SnapshotWs:
        async def subscribe(self, channels, callback):
            return None

        async def request(self, method, params):
            if method == "public/get_instrument":
                return {"tick_size": 0.0001, "tick_size_steps": []}
            if method == "public/ticker":
                return {
                    "instrument_name": "BTC-SELL-C",
                    "best_bid_price": 0.007,
                    "best_ask_price": 0.0075,
                }
            raise AssertionError(method)

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr("src.execution.single_leg.asyncio.sleep", no_wait)
    executor = SingleLegExecutor(
        SnapshotWs(), "BTC-SELL-C", "sell", SingleLegParams(amount=0.8))

    await executor._setup()

    assert executor._top() == (0.007, 0.0075)
    assert any("public/ticker" in item for item in executor.report.events)
