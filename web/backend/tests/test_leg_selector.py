"""选腿与算量测试（用仿 testnet 期权链 fixture）。"""

import pytest

from src.engine.leg_selector import (
    available_entry_risk_btc,
    contracts_for_budget,
    contracts_for_risk_budget,
    max_loss_per_contract,
    portfolio_risk_btc,
    select_spread,
)


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
    sel = select_spread(PUT_CHAIN, "PUT_CREDIT", 0.20)
    assert sel is not None
    assert sel.sell.instrument == "BTC-7AUG26-62000-P"   # |Δ| 最接近 0.20
    assert sel.buy.strike < sel.sell.strike              # PUT 买腿行权价更低
    assert sel.net_credit_est == pytest.approx(sel.sell.bid - sel.buy.ask)
    assert sel.net_credit_est > 0                        # 净收权利金


def test_call_credit_buy_leg_above_sell():
    chain = [
        _ticker(68000, 0.343, 0.0155, 0.0165, "C"),
        _ticker(70000, 0.212, 0.0080, 0.0090, "C"),
        _ticker(72000, 0.125, 0.0041, 0.0046, "C"),
    ]
    sel = select_spread(chain, "CALL_CREDIT", 0.20)
    assert sel is not None
    assert sel.sell.instrument == "BTC-7AUG26-70000-C"
    assert sel.buy.strike > sel.sell.strike              # CALL 买腿行权价更高


def test_high_risk_reward_is_diagnostic_not_an_entry_filter():
    chain = [
        _ticker(62500, -0.035, 0.0002, 0.0004),
        _ticker(63000, -0.059, 0.0004, 0.0006),
        _ticker(63500, -0.110, 0.0008, 0.0011),
        _ticker(64000, -0.200, 0.0016, 0.0021),
    ]

    sel = select_spread(chain, "PUT_CREDIT", 0.15)

    assert sel is not None
    assert sel.sell.strike == 64000
    assert sel.buy.strike == 62500
    assert sel.risk_reward > 12.0


def test_existing_residual_instrument_is_never_reused_as_a_new_leg():
    chain = [
        _ticker(68000, 0.343, 0.0155, 0.0165, "C"),
        _ticker(70000, 0.212, 0.0080, 0.0090, "C"),
        _ticker(72000, 0.125, 0.0041, 0.0046, "C"),
        _ticker(74000, 0.080, 0.0020, 0.0024, "C"),
    ]

    sel = select_spread(
        chain, "CALL_CREDIT", 0.20,
        excluded_instruments={"BTC-7AUG26-72000-C"})

    assert sel is not None
    assert sel.sell.instrument != "BTC-7AUG26-72000-C"
    assert sel.buy.instrument != "BTC-7AUG26-72000-C"


def test_tries_next_nearby_sell_leg_when_closest_has_no_valid_spread():
    chain = [
        _ticker(50000, -0.05, 0.0030, 0.0040),
        _ticker(54000, -0.10, 0.0040, 0.0070),
        _ticker(56000, -0.14, 0.0040, 0.0100),
        _ticker(58000, -0.20, 0.0140, 0.0150),
    ]

    sel = select_spread(chain, "PUT_CREDIT", 0.15)

    assert sel is not None
    assert sel.sell.instrument == "BTC-7AUG26-58000-P"
    assert sel.diag["sell_candidates_checked"] >= 2


def test_nearby_sell_search_never_exceeds_configured_delta_radius():
    chain = [
        _ticker(50000, -0.05, 0.0030, 0.0040),
        _ticker(56000, -0.14, 0.0040, 0.0100),
        _ticker(62000, -0.40, 0.0300, 0.0310),
    ]

    assert select_spread(
        chain,
        "PUT_CREDIT",
        0.15,
        sell_delta_search_radius=0.10,
    ) is None


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


def test_portfolio_budget_reduces_later_batch_size():
    equity = 10.0
    active_risk = 0.55
    available = available_entry_risk_btc(
        equity, account_risk_pct=0.02, portfolio_risk_pct=0.06,
        active_risk_btc=active_risk)

    assert available == pytest.approx(0.05)
    assert contracts_for_risk_budget(available, 0.01) == 5.0


def test_portfolio_risk_uses_remaining_quantity_and_conservative_fallback():
    positions = [
        {
            "spread_type": "CALL_CREDIT",
            "sell_strike": 70000,
            "buy_strike": 72000,
            "net_credit_btc": 0.003,
            "remaining_qty": 2.5,
        },
        {"remaining_qty": 1.0},
    ]

    expected = max_loss_per_contract(
        "CALL_CREDIT", 70000, 72000, 0.003) * 2.5 + 0.2
    assert portfolio_risk_btc(
        positions, fallback_per_position_btc=0.2) == pytest.approx(expected)


def test_portfolio_budget_is_zero_after_limit_is_consumed():
    assert available_entry_risk_btc(
        10.0, account_risk_pct=0.02, portfolio_risk_pct=0.06,
        active_risk_btc=0.61) == 0.0
