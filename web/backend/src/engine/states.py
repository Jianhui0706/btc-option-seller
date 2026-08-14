"""策略/对冲状态机枚举与合法迁移表（规范第十六节）。"""

from __future__ import annotations

from enum import Enum


class TradeState(str, Enum):
    SIGNAL_RECEIVED = "SIGNAL_RECEIVED"
    POSITION_CHECKING = "POSITION_CHECKING"
    SELECTING_LEGS = "SELECTING_LEGS"
    OPENING = "OPENING"
    OPEN = "OPEN"
    PARTIALLY_CLOSED = "PARTIALLY_CLOSED"
    SETTLEMENT_PENDING = "SETTLEMENT_PENDING"
    RESIDUAL_LONG = "RESIDUAL_LONG"
    SETTLED = "SETTLED"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    FORCE_CLOSED = "FORCE_CLOSED"
    OPEN_FAILED = "OPEN_FAILED"


class HedgeState(str, Enum):
    HEDGE_INACTIVE = "HEDGE_INACTIVE"
    HEDGE_ACTIVE = "HEDGE_ACTIVE"
    HEDGE_REBALANCING = "HEDGE_REBALANCING"
    HEDGE_RETRYING = "HEDGE_RETRYING"
    HEDGE_DEGRADED = "HEDGE_DEGRADED"
    HEDGE_RELEASE_PENDING = "HEDGE_RELEASE_PENDING"
    HEDGE_CLOSING = "HEDGE_CLOSING"


class GroupState(str, Enum):
    OPEN = "OPEN"
    CLOSING = "CLOSING"
    SETTLED = "SETTLED"
    CLOSED = "CLOSED"
    FORCE_CLOSED = "FORCE_CLOSED"


TRADE_TRANSITIONS: dict[TradeState, set[TradeState]] = {
    TradeState.SIGNAL_RECEIVED: {TradeState.POSITION_CHECKING, TradeState.OPEN_FAILED},
    TradeState.POSITION_CHECKING: {TradeState.SELECTING_LEGS, TradeState.OPEN_FAILED},
    TradeState.SELECTING_LEGS: {TradeState.OPENING, TradeState.OPEN_FAILED},
    TradeState.OPENING: {TradeState.OPEN, TradeState.OPEN_FAILED, TradeState.FORCE_CLOSED},
    TradeState.OPEN: {TradeState.PARTIALLY_CLOSED, TradeState.SETTLEMENT_PENDING,
                      TradeState.RESIDUAL_LONG, TradeState.CLOSING,
                      TradeState.FORCE_CLOSED},
    TradeState.PARTIALLY_CLOSED: {TradeState.SETTLEMENT_PENDING, TradeState.CLOSING,
                                  TradeState.RESIDUAL_LONG, TradeState.CLOSED,
                                  TradeState.FORCE_CLOSED},
    TradeState.SETTLEMENT_PENDING: {TradeState.RESIDUAL_LONG, TradeState.SETTLED,
                                    TradeState.FORCE_CLOSED},
    TradeState.RESIDUAL_LONG: {TradeState.CLOSING, TradeState.SETTLED,
                               TradeState.FORCE_CLOSED},
    TradeState.SETTLED: set(),
    TradeState.CLOSING: {TradeState.CLOSED, TradeState.PARTIALLY_CLOSED,
                         TradeState.RESIDUAL_LONG, TradeState.FORCE_CLOSED},
    TradeState.CLOSED: set(),
    TradeState.FORCE_CLOSED: set(),
    TradeState.OPEN_FAILED: set(),
}

HEDGE_TRANSITIONS: dict[HedgeState, set[HedgeState]] = {
    HedgeState.HEDGE_INACTIVE: {HedgeState.HEDGE_ACTIVE},
    HedgeState.HEDGE_ACTIVE: {HedgeState.HEDGE_REBALANCING, HedgeState.HEDGE_RELEASE_PENDING,
                              HedgeState.HEDGE_CLOSING},
    HedgeState.HEDGE_REBALANCING: {HedgeState.HEDGE_ACTIVE, HedgeState.HEDGE_RETRYING,
                                   HedgeState.HEDGE_DEGRADED, HedgeState.HEDGE_CLOSING},
    HedgeState.HEDGE_RETRYING: {HedgeState.HEDGE_ACTIVE, HedgeState.HEDGE_DEGRADED,
                                HedgeState.HEDGE_CLOSING},
    HedgeState.HEDGE_DEGRADED: {HedgeState.HEDGE_REBALANCING,
                                HedgeState.HEDGE_RELEASE_PENDING,
                                HedgeState.HEDGE_CLOSING},
    # 撤销待确认期间价格重新突破 BEP → 取消 pending 回到 ACTIVE
    HedgeState.HEDGE_RELEASE_PENDING: {HedgeState.HEDGE_CLOSING, HedgeState.HEDGE_ACTIVE},
    HedgeState.HEDGE_CLOSING: {HedgeState.HEDGE_INACTIVE},
}

GROUP_TRANSITIONS: dict[GroupState, set[GroupState]] = {
    GroupState.OPEN: {GroupState.CLOSING, GroupState.SETTLED,
                      GroupState.CLOSED, GroupState.FORCE_CLOSED},
    GroupState.CLOSING: {GroupState.CLOSED, GroupState.OPEN, GroupState.FORCE_CLOSED},
    GroupState.CLOSED: set(),
    GroupState.SETTLED: set(),
    GroupState.FORCE_CLOSED: set(),
}


class IllegalTransition(Exception):
    pass


def check_transition(kind: str, frm: str, to: str) -> None:
    table = {"trade": TRADE_TRANSITIONS, "hedge": HEDGE_TRANSITIONS,
             "group": GROUP_TRANSITIONS}[kind]
    enum_cls = {"trade": TradeState, "hedge": HedgeState, "group": GroupState}[kind]
    if enum_cls(to) not in table[enum_cls(frm)]:
        raise IllegalTransition(f"{kind}: {frm} -> {to} 不是合法迁移")
