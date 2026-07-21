"""选腿与算量测试（用仿 testnet 期权链 fixture）。"""

import pytest

from src.engine.leg_selector import (contracts_for_budget, max_loss_per_contract,
                                     select_spread)


def _ticker(strike: float, delta: float, bid: float, ask: float,
            typ: str = "P") -> dict:
    return {
        "instrument_name": f"BTC-7AUG26-{int(strike)}-{typ}",
        "strike": strike,
        "greeks": {"delta": delta},
        "best_bid_price": bid,
        "best_ask_price": ask,
    }


PUT_CHAIN = [
    _ticker(55000, -0.038, 0.0015, 0.0018),
    _ticker(58000, -0.080, 0.0033, 0.0037),
    _ticker(60000, -0.133, 0.0055, 0.0065),
    _ticker(62000, -0.218, 0.0100, 0.0110),
    _ticker(64000, -0.344, 0.0180, 0.0190),
    _ticker(66000, -0.500, 0.0305, 0.0320),
]


def test_put_credit_selection():
    sel = select_spread(PUT_CHAIN, "PUT_CREDIT", 0.20, risk_reward_max=12.0)
    assert sel is not None
    assert sel.sell.instrument == "BTC-7AUG26-62000-P"   # |Δ| 最接近 0.20
    assert sel.buy.strike < sel.sell.strike              # PUT 买腿行权价更低
    assert sel.net_credit_est == pytest.approx(sel.sell.bid - sel.buy.ask)
    assert sel.net_credit_est > 0                        # 净收权利金
    assert sel.risk_reward <= 12.0


def test_call_credit_buy_leg_above_sell():
    chain = [
        _ticker(68000, 0.343, 0.0155, 0.0165, "C"),
        _ticker(70000, 0.212, 0.0080, 0.0090, "C"),
        _ticker(72000, 0.125, 0.0041, 0.0046, "C"),
    ]
    sel = select_spread(chain, "CALL_CREDIT", 0.20, risk_reward_max=12.0)
    assert sel is not None
    assert sel.sell.instrument == "BTC-7AUG26-70000-C"
    assert sel.buy.strike > sel.sell.strike              # CALL 买腿行权价更高


def test_no_candidates_when_rr_too_strict():
    assert select_spread(PUT_CHAIN, "PUT_CREDIT", 0.20, risk_reward_max=0.5) is None


def test_one_sided_quotes_excluded():
    chain = [
        _ticker(62000, -0.218, 0.0, 0.0110),   # 无 bid
        _ticker(58000, -0.080, 0.0033, 0.0037),
    ]
    assert select_spread(chain, "PUT_CREDIT", 0.20) is None


def test_delta_bounds():
    # 卖腿 Delta 超出 [0.05, 0.7] 的不可选
    chain = [
        _ticker(75000, -0.03, 0.001, 0.0012),
        _ticker(50000, -0.9, 0.10, 0.12),
    ]
    assert select_spread(chain, "PUT_CREDIT", 0.20) is None


def test_max_loss_stress_only_for_put():
    plain_put = max_loss_per_contract("PUT_CREDIT", 62000, 58000, 0.006, stressed=False)
    stress_put = max_loss_per_contract("PUT_CREDIT", 62000, 58000, 0.006)
    plain_call = max_loss_per_contract("CALL_CREDIT", 70000, 72000, 0.003, stressed=False)
    stress_call = max_loss_per_contract("CALL_CREDIT", 70000, 72000, 0.003)
    assert stress_put > plain_put
    assert stress_call == plain_call
    assert plain_put == pytest.approx(4000 / 58000 - 0.006)


def test_contracts_for_budget():
    # 权益 97, 预算 2%, 每张最大亏损 0.0833 → floor(1.94/0.0833/0.1)*0.1 = 23.2/23.3
    q = contracts_for_budget(97.0, 0.02, 0.0833)
    assert q == pytest.approx(23.2, abs=0.11)
    assert contracts_for_budget(1.0, 0.02, 0.5) == 0.0     # 预算不足 → 0
    assert contracts_for_budget(1.0, 0.02, 0.0) == 0.0     # 防御
