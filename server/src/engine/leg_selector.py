"""选腿与算量（纯函数）。

规范第六节：按 DVOL 档位选卖腿 Delta 与 DTE；买卖腿同到期日；净收权利金按实际
bid/ask；"反向盈亏比"选买腿；按账户风险预算算总量；禁止裸卖。

⚠ 待确认口径（占位实现，见 config/base.yaml 注释）：
  - 档位→卖腿|Delta|/DTE 映射表（tier_map）
  - 买腿规则：在 max_loss/net_credit ≤ risk_reward_max 的候选中选权利金最便宜（最虚值）者
  - 风险预算：每笔最大亏损 ≤ 权益 × account_risk_pct
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

# put 深度下跌时反向(BTC)结算亏损会超过 width/buy_strike，留安全系数（占位）
PUT_STRESS_FACTOR = 1.3


def _strike(t: dict) -> float:
    return float(t.get("strike") or t["instrument_name"].split("-")[2])


def _delta(t: dict) -> float:
    return float(t["greeks"]["delta"])


def _two_sided(t: dict) -> bool:
    bid = float(t.get("best_bid_price") or 0.0)
    ask = float(t.get("best_ask_price") or 0.0)
    return bid > 0.0 and ask > 0.0 and bid <= ask


@dataclass
class LegPick:
    instrument: str
    strike: float
    delta: float
    bid: float
    ask: float


@dataclass
class SpreadSelection:
    sell: LegPick
    buy: LegPick
    net_credit_est: float      # 估算净权利金 = 卖腿bid − 买腿ask（BTC/张）
    max_loss_btc: float        # 每张最大亏损估算（BTC）
    risk_reward: float         # max_loss / net_credit
    diag: dict


def _mk(t: dict) -> LegPick:
    return LegPick(
        instrument=t["instrument_name"],
        strike=_strike(t),
        delta=_delta(t),
        bid=float(t.get("best_bid_price") or 0.0),
        ask=float(t.get("best_ask_price") or 0.0),
    )


def max_loss_per_contract(spread_type: str, sell_strike: float, buy_strike: float,
                          net_credit: float, *, stressed: bool = True) -> float:
    """每张最大亏损（BTC）。反向期权：价差最大亏损 ≈ 行权价宽度/买腿行权价。
    CALL 在 S=买腿行权价处为精确上界；PUT 深度下跌会继续放大——
    stressed=True 时乘安全系数（用于风险预算算量），盈亏比筛选用 stressed=False。"""
    width = abs(sell_strike - buy_strike)
    base = width / buy_strike
    if stressed and spread_type == "PUT_CREDIT":
        base *= PUT_STRESS_FACTOR
    return max(0.0, base - net_credit)


def select_spread(
    tickers: list[dict],
    spread_type: str,              # "PUT_CREDIT" / "CALL_CREDIT"
    target_sell_delta: float,      # 档位映射的卖腿目标 |Delta|
    *,
    sell_delta_min: float = 0.05,
    sell_delta_max: float = 0.70,
    min_credit_btc: float = 0.0005,
    risk_reward_max: float = 6.0,
) -> Optional[SpreadSelection]:
    """从同一到期日的单边（call 或 put）ticker 列表中选卖腿+买腿。选不出返回 None。"""
    assert spread_type in ("PUT_CREDIT", "CALL_CREDIT")
    quoted = [t for t in tickers if _two_sided(t)]
    if len(quoted) < 2:
        return None

    # 卖腿：|delta| 在约束区间内，最接近档位目标
    sell_cands = [t for t in quoted
                  if sell_delta_min <= abs(_delta(t)) <= sell_delta_max]
    if not sell_cands:
        return None
    sell_t = min(sell_cands, key=lambda t: abs(abs(_delta(t)) - target_sell_delta))
    sell = _mk(sell_t)

    # 买腿候选：PUT 买腿行权价更低 / CALL 买腿行权价更高（规范第六节）
    if spread_type == "PUT_CREDIT":
        buy_cands = [t for t in quoted if _strike(t) < sell.strike]
    else:
        buy_cands = [t for t in quoted if _strike(t) > sell.strike]

    best: Optional[SpreadSelection] = None
    for t in buy_cands:
        buy = _mk(t)
        credit = sell.bid - buy.ask
        if credit < min_credit_btc:
            continue
        # 盈亏比筛选用未加压口径；算量用加压口径（PUT 深跌保护）
        plain_loss = max_loss_per_contract(spread_type, sell.strike, buy.strike,
                                           credit, stressed=False)
        loss = max_loss_per_contract(spread_type, sell.strike, buy.strike, credit)
        if plain_loss <= 0 or loss <= 0:
            continue
        rr = plain_loss / credit
        if rr > risk_reward_max:
            continue
        cand = SpreadSelection(
            sell=sell, buy=buy, net_credit_est=credit, max_loss_btc=loss,
            risk_reward=rr,
            diag={"target_sell_delta": target_sell_delta,
                  "sell_delta_actual": sell.delta,
                  "buy_candidates": len(buy_cands)},
        )
        # 占位"反向盈亏比"规则：约束内选买腿权利金最便宜（最虚值）者
        if best is None or buy.ask < best.buy.ask:
            best = cand
    return best


def contracts_for_budget(equity_btc: float, account_risk_pct: float,
                         max_loss_per_contract_btc: float,
                         min_lot: float = 0.1) -> float:
    """总开仓张数 = 风险预算 / 每张最大亏损，向下取整到 min_lot。"""
    if max_loss_per_contract_btc <= 0:
        return 0.0
    budget = equity_btc * account_risk_pct
    n = math.floor(budget / max_loss_per_contract_btc / min_lot) * min_lot
    return round(max(n, 0.0), 10)
