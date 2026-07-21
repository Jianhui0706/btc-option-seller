"""Delta trigger 与对冲量口径测试（规范第十、十二节）。"""

import pytest


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
