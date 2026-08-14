"""Delta trigger 与对冲量口径测试（规范第十、十二节）。"""

from types import SimpleNamespace

import pytest

from src.engine.hedge import (TradeHedgeParams, delta_view_from_positions,
                              hedge_diagnostic)


def trigger(equity: float) -> float:
    return equity * 0.02 * 0.10          # = equity × 0.002


def effective(equity: float, index_price: float) -> float:
    return max(trigger(equity), 10.0 / index_price)


def perp_usd(order_btc: float, index_price: float, lot: float = 10.0) -> float:
    return round(order_btc * index_price / lot) * lot


def test_spec_examples():
    # 规范第十节示例
    assert trigger(1.0) == pytest.approx(0.002)
    assert trigger(5.0) == pytest.approx(0.01)
    assert trigger(10.0) == pytest.approx(0.02)


def test_min_trade_delta_floor():
    # 小账户：0.01 BTC 权益 → trigger 0.00002，被最小下单量兜底
    idx = 65000.0
    assert effective(0.01, idx) == pytest.approx(10.0 / idx)
    # 大账户：不受兜底影响
    assert effective(10.0, idx) == pytest.approx(0.02)


def test_usd_rounding():
    idx = 65000.0
    assert perp_usd(0.002, idx) == 130.0            # 0.002×65000=130
    assert perp_usd(0.0001, idx) == 10.0            # 6.5 → round → 10
    assert perp_usd(0.00007, idx) == 0.0            # 4.55 → round → 0 → MIN_AMOUNT
    assert perp_usd(-0.0031, idx) == -200.0         # 负向（卖出）


def test_hedge_direction_neutral():
    # 卖 Call 组合 Delta 为负 → target = -option_delta 为正 → 买入期货
    option_delta = -0.85
    futures_delta = 0.0
    target = -option_delta
    order = target - futures_delta
    assert order == pytest.approx(0.85)
    # 已有 0.5 对冲时只补差额
    assert (target - 0.5) == pytest.approx(0.35)


def test_scanner_position_snapshot_builds_delta_view():
    ctx = SimpleNamespace(cfg=SimpleNamespace(
        hedge=SimpleNamespace(perp_instrument="BTC-PERPETUAL")))
    view = delta_view_from_positions(
        ctx,
        SimpleNamespace(equity_btc=2.0),
        [SimpleNamespace(delta=-0.30), SimpleNamespace(delta=0.05)],
        [SimpleNamespace(instrument="BTC-PERPETUAL", delta_btc=0.10)],
        65000.0,
    )

    assert view.option_delta == pytest.approx(-0.25)
    assert view.futures_delta == pytest.approx(0.10)
    assert view.net_delta == pytest.approx(-0.15)


def _diagnostic_ctx():
    return SimpleNamespace(
        hedge_frozen=False,
        cfg=SimpleNamespace(
            hedge=SimpleNamespace(
                delta_account_risk_pct=0.02,
                delta_trigger_pct=0.10,
                bep_activation_progress=0.50,
                call_release_pct=0.98,
                put_release_pct=1.02,
                perp_contract_usd=10.0,
            ),
            debug=SimpleNamespace(
                bep_override=None,
                trigger_override=None,
            ),
        ),
    )


def _params():
    return TradeHedgeParams(
        account_risk_pct=0.02,
        trigger_pct=0.10,
        call_release_pct=0.98,
        put_release_pct=1.02,
    )


def _snapshot(*, bar_close=65000.0, option_delta=0.01,
              futures_delta=0.0):
    return {
        "bar_close": bar_close,
        "bar_ts": 123,
        "observed_at": 456,
        "option_delta": option_delta,
        "futures_delta": futures_delta,
        "net_delta": option_delta + futures_delta,
        "equity_btc": 1.0,
        "index_price": 65000.0,
    }


def test_diagnostic_explains_waiting_for_put_bep():
    result = hedge_diagnostic(
        _diagnostic_ctx(),
        {"spread_type": "PUT_CREDIT", "bep_price": 62000.0,
         "open_index_price": 65000.0},
        {"state": "HEDGE_INACTIVE", "futures_usd": 0.0},
        _snapshot(bar_close=65000.0),
        _params(),
    )

    assert result["reason_code"] == "WAITING_FOR_HEDGE_TRIGGER"
    assert "65,000.0" in result["reason"]
    assert "63,500.0" in result["reason"]
    assert "1,500.0 USD" in result["next_action"]
    assert result["activation_price"] == pytest.approx(63500.0)
    assert result["review_bar_minutes"] == 3


def test_release_pending_copy_uses_configured_review_bar_minutes():
    ctx = _diagnostic_ctx()
    ctx.runtime_review_bar_minutes = 1
    result = hedge_diagnostic(
        ctx,
        {"spread_type": "PUT_CREDIT", "bep_price": 62000.0,
         "open_index_price": 65000.0},
        {"state": "HEDGE_RELEASE_PENDING", "futures_usd": 100.0},
        _snapshot(bar_close=63500.0),
        _params(),
    )

    assert result["review_bar_minutes"] == 1
    assert "完整 1 分钟 K 线" in result["reason"]


def test_diagnostic_explains_active_delta_below_threshold():
    result = hedge_diagnostic(
        _diagnostic_ctx(),
        {"spread_type": "PUT_CREDIT", "bep_price": 62000.0},
        {"state": "HEDGE_ACTIVE", "futures_usd": 0.0},
        _snapshot(bar_close=61000.0, option_delta=0.001),
        _params(),
    )

    assert result["reason_code"] == "BELOW_DELTA_TRIGGER"
    assert result["effective_trigger"] == pytest.approx(0.002)
    assert "避免小额频繁交易" in result["next_action"]


def test_diagnostic_shows_required_perpetual_adjustment():
    result = hedge_diagnostic(
        _diagnostic_ctx(),
        {"spread_type": "PUT_CREDIT", "bep_price": 62000.0},
        {"state": "HEDGE_ACTIVE", "futures_usd": 0.0},
        _snapshot(bar_close=61000.0, option_delta=0.06648),
        _params(),
    )

    assert result["reason_code"] == "REBALANCE_REQUIRED"
    assert result["adjustment_usd"] == -4320.0
    assert "BTC-PERPETUAL -4320 USD" in result["next_action"]
