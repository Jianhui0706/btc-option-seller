"""状态机合法迁移测试（规范第十六节）。"""

import pytest

from src.engine.states import IllegalTransition, check_transition


def test_legal_trade_path():
    for frm, to in [("SIGNAL_RECEIVED", "POSITION_CHECKING"),
                    ("POSITION_CHECKING", "SELECTING_LEGS"),
                    ("SELECTING_LEGS", "OPENING"),
                    ("OPENING", "OPEN"),
                    ("OPEN", "CLOSING"),
                    ("CLOSING", "CLOSED")]:
        check_transition("trade", frm, to)


def test_illegal_trade_transitions():
    for frm, to in [("SIGNAL_RECEIVED", "OPEN"),
                    ("CLOSED", "OPEN"),
                    ("OPEN_FAILED", "OPEN"),
                    ("OPEN", "SIGNAL_RECEIVED")]:
        with pytest.raises(IllegalTransition):
            check_transition("trade", frm, to)


def test_force_close_from_open_states():
    for frm in ("OPENING", "OPEN", "PARTIALLY_CLOSED", "CLOSING"):
        check_transition("trade", frm, "FORCE_CLOSED")


def test_hedge_cycle():
    for frm, to in [("HEDGE_INACTIVE", "HEDGE_ACTIVE"),
                    ("HEDGE_ACTIVE", "HEDGE_REBALANCING"),
                    ("HEDGE_REBALANCING", "HEDGE_ACTIVE"),
                    ("HEDGE_ACTIVE", "HEDGE_RELEASE_PENDING"),
                    ("HEDGE_RELEASE_PENDING", "HEDGE_ACTIVE"),   # 重新突破 BEP 取消撤销
                    ("HEDGE_RELEASE_PENDING", "HEDGE_CLOSING"),
                    ("HEDGE_CLOSING", "HEDGE_INACTIVE")]:
        check_transition("hedge", frm, to)


def test_hedge_illegal():
    with pytest.raises(IllegalTransition):
        check_transition("hedge", "HEDGE_INACTIVE", "HEDGE_REBALANCING")
    with pytest.raises(IllegalTransition):
        check_transition("hedge", "HEDGE_CLOSING", "HEDGE_ACTIVE")
