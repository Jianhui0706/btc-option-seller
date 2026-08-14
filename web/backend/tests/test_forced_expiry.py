"""强制开仓到期日选腿 + 运行时激活系数覆盖。"""

from types import SimpleNamespace

import pytest

from src.engine.hedge import DeltaView, scan_hedge
from src.exchange import market_data
from src.exchange.market_data import option_chain_tickers
from src.engine.leg_selector import select_spread
from src.monitor import events as ev

E1 = 2_000_000_000_000
E2 = 2_100_000_000_000
MISSING = 9_999_999_999_999


class ChainWS:
    """按到期日/行权价返回 call 报价的假 ws。

    quotes: {expiry: {strike: (bid, ask, delta)}}；bid/ask 为 0 表示单边。
    """

    def __init__(self, quotes):
        self.quotes = quotes

    async def request(self, method, params):
        if method == "public/get_instruments":
            out = []
            for expiry, strikes in self.quotes.items():
                for strike in strikes:
                    out.append({
                        "instrument_name": f"BTC-{expiry}-{strike}-C",
                        "option_type": "call",
                        "expiration_timestamp": expiry,
                    })
            return out
        if method == "public/ticker":
            name = params["instrument_name"]
            _, expiry, strike, _ = name.split("-")
            bid, ask, delta = self.quotes[int(expiry)][int(strike)]
            return {
                "instrument_name": name,
                "best_bid_price": bid,
                "best_ask_price": ask,
                "greeks": {"delta": delta},
            }
        raise AssertionError(method)


def _two_sided_chain():
    # E1 有两条双边 call；E2 也有，验证不会误选 E2
    return ChainWS({
        E1: {
            70000: (0.010, 0.011, 0.20),
            72000: (0.004, 0.005, 0.12),
        },
        E2: {
            70000: (0.020, 0.021, 0.25),
            72000: (0.010, 0.011, 0.18),
        },
    })


@pytest.mark.asyncio
async def test_forced_expiry_locks_to_that_expiry():
    ws = _two_sided_chain()
    chain = await option_chain_tickers(
        ws, "BTC", "call", 1, 730, forced_expiry_ts=E1)
    assert chain.expiration_timestamp == E1
    assert len(chain.tickers) == 2


@pytest.mark.asyncio
async def test_forced_expiry_not_listed_returns_empty_chain():
    ws = _two_sided_chain()
    chain = await option_chain_tickers(
        ws, "BTC", "call", 1, 730, forced_expiry_ts=MISSING)
    assert chain.tickers == []
    assert chain.expiration_timestamp == MISSING
    assert chain.checked_expiries == 0
    # 空链 → select_spread 选不出 → 上层 REJECT_NO_VALID_LEGS
    assert select_spread(chain.tickers, "CALL_CREDIT", 0.20) is None


@pytest.mark.asyncio
async def test_forced_expiry_without_two_quotes_does_not_fall_back():
    # E1 只有一条双边，另一条是单边（ask=0）；不得改选 E2
    ws = ChainWS({
        E1: {
            70000: (0.010, 0.011, 0.20),
            72000: (0.004, 0.0, 0.12),     # 单边
        },
        E2: {
            70000: (0.020, 0.021, 0.25),
            72000: (0.010, 0.011, 0.18),
        },
    })
    chain = await option_chain_tickers(
        ws, "BTC", "call", 1, 730, forced_expiry_ts=E1)
    assert chain.expiration_timestamp == E1        # 仍是 E1，绝不回退 E2
    two_sided = [t for t in chain.tickers if market_data.has_two_sided_quote(t)]
    assert len(two_sided) < 2
    assert select_spread(chain.tickers, "CALL_CREDIT", 0.20) is None


@pytest.mark.asyncio
async def test_no_forced_expiry_uses_dte_range_logic():
    ws = _two_sided_chain()
    chain = await option_chain_tickers(ws, "BTC", "call", 1, 730)
    # 未指定 → 走原逻辑，仍能选到有效链
    assert chain.expiration_timestamp in (E1, E2)
    assert len(chain.tickers) == 2


# ---- 运行时激活系数覆盖（第 1/3 点核心） --------------------------------

class HedgeRepo:
    def __init__(self, state="HEDGE_INACTIVE"):
        self.hedge = {"state": state, "release_pending_bar_ts": 100}
        self.transitions = []

    async def get_hedge(self, parent_id):
        return self.hedge.copy()

    async def ensure_hedge_row(self, parent_id):
        return None

    async def get_signal_payload(self, signal_id):
        return {}

    async def transition_hedge(self, parent_id, state, event_code, **payload):
        self.hedge["state"] = state
        self.transitions.append((state, event_code, payload))

    async def set_release_pending_bar(self, parent_id, bar_ts):
        self.hedge["release_pending_bar_ts"] = bar_ts


class HedgeQueue:
    def __init__(self):
        self.items = []

    def hedge_release(self, parent_id):
        return ("release", parent_id)

    def put(self, item):
        self.items.append(item)


def _hedge_ctx(runtime_progress):
    """cfg 默认故意设 0.50，用 runtime 覆盖为 0.20 证明覆盖生效。"""
    return SimpleNamespace(
        repo=HedgeRepo(),
        queue=HedgeQueue(),
        runtime_bep_activation_progress=runtime_progress,
        cfg=SimpleNamespace(
            hedge=SimpleNamespace(
                delta_account_risk_pct=0.02,
                delta_trigger_pct=0.10,
                bep_activation_progress=0.50,
                call_release_pct=0.98,
                put_release_pct=1.02,
                perp_contract_usd=10.0,
            ),
            debug=SimpleNamespace(bep_override=None, trigger_override=None),
        ),
    )


def _parent():
    return {
        "id": 1,
        "signal_id": "CALL-1",
        "spread_type": "CALL_CREDIT",
        "open_index_price": 66000.0,
        "bep_price": 70330.0,
    }


def _view():
    return DeltaView(option_delta=0.001, futures_delta=0.0, net_delta=0.001,
                     equity_btc=1.0, index_price=67000.0)


@pytest.mark.asyncio
async def test_runtime_progress_activates_earlier_than_default():
    # progress=0.2 → 激活价 = 66000 + (70330-66000)*0.2 = 66866
    ctx = _hedge_ctx(0.20)
    await scan_hedge(ctx, _parent(), 67000.0, 200, view=_view())
    assert [item[:2] for item in ctx.repo.transitions] == [
        ("HEDGE_ACTIVE", ev.BEP_HEDGE_ACTIVATED),
    ]
    assert ctx.repo.transitions[0][2]["activation_price"] == pytest.approx(66866.0)


@pytest.mark.asyncio
async def test_default_progress_not_yet_activated_at_same_price():
    # 无 runtime 覆盖 → 回退 cfg 0.50 → 激活价 68165 > 67000 → 不激活
    ctx = _hedge_ctx(None)
    await scan_hedge(ctx, _parent(), 67000.0, 200, view=_view())
    assert ctx.repo.transitions == []
