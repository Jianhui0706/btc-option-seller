"""选腿与算量（纯函数）。

规范第六节：按 DVOL 档位选卖腿 Delta 与 DTE；买卖腿同到期日；净收权利金按实际
bid/ask；"反向盈亏比"选买腿；按账户风险预算算总量；禁止裸卖。

⚠ 待确认口径（见 config/base.yaml 注释）：
  - 档位→卖腿|Delta|/DTE 映射表（tier_map）
  - 买腿规则：在满足净收权利金的候选中选权利金最便宜（最虚值）者
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
    stressed=True 时乘安全系数（用于风险预算算量）。"""
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
    sell_delta_search_radius: float = 0.10,
    min_credit_btc: float = 0.0005,
    excluded_instruments: set[str] | None = None,
) -> Optional[SpreadSelection]:
    """从同一到期日的单边（call 或 put）ticker 列表中选卖腿+买腿。选不出返回 None。"""
    assert spread_type in ("PUT_CREDIT", "CALL_CREDIT")
    excluded = excluded_instruments or set()
    quoted = [t for t in tickers
              if _two_sided(t) and t["instrument_name"] not in excluded]
    if len(quoted) < 2:
        return None

    # 卖腿：从目标 |delta| 附近开始逐个尝试。以前这里只检查最接近
    # 目标的一条腿；那条腿没有合格买腿时，即使相邻行权价可用也会拒绝。
    sell_cands = [t for t in quoted
                  if sell_delta_min <= abs(_delta(t)) <= sell_delta_max
                  and abs(abs(_delta(t)) - target_sell_delta)
                  <= sell_delta_search_radius]
    if not sell_cands:
        return None
    sell_cands.sort(key=lambda t: abs(abs(_delta(t)) - target_sell_delta))

    for sell_rank, sell_t in enumerate(sell_cands, start=1):
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
            # 盈亏比只记录诊断；算量仍使用加压口径（PUT 深跌保护）。
            plain_loss = max_loss_per_contract(
                spread_type, sell.strike, buy.strike, credit, stressed=False)
            loss = max_loss_per_contract(
                spread_type, sell.strike, buy.strike, credit)
            if plain_loss <= 0 or loss <= 0:
                continue
            rr = plain_loss / credit
            cand = SpreadSelection(
                sell=sell, buy=buy, net_credit_est=credit, max_loss_btc=loss,
                risk_reward=rr,
                diag={
                    "target_sell_delta": target_sell_delta,
                    "sell_delta_actual": sell.delta,
                    "sell_candidates_checked": sell_rank,
                    "sell_candidates_in_radius": len(sell_cands),
                    "buy_candidates": len(buy_cands),
                },
            )
            # 同一卖腿内沿用原规则：选权利金最便宜（最虚值）的合格买腿。
            if best is None or buy.ask < best.buy.ask:
                best = cand
        if best is not None:
            return best
    return None


def contracts_for_risk_budget(risk_budget_btc: float,
                              max_loss_per_contract_btc: float,
                              min_lot: float = 0.1) -> float:
    """Convert an absolute BTC risk budget into contracts."""
    if max_loss_per_contract_btc <= 0:
        return 0.0
    lots = (max(risk_budget_btc, 0.0) / max_loss_per_contract_btc
            / min_lot)
    n = math.floor(lots + 1e-9) * min_lot
    return round(max(n, 0.0), 10)


def contracts_for_budget(equity_btc: float, account_risk_pct: float,
                         max_loss_per_contract_btc: float,
                         min_lot: float = 0.1) -> float:
    """总开仓张数 = 风险预算 / 每张最大亏损，向下取整到 min_lot。"""
    return contracts_for_risk_budget(
        equity_btc * account_risk_pct,
        max_loss_per_contract_btc,
        min_lot)


def portfolio_risk_btc(positions: list[dict],
                       fallback_per_position_btc: float = 0.0) -> float:
    """Estimate remaining maximum loss for active spread batches."""
    total = 0.0
    for position in positions:
        qty = max(float(position.get("remaining_qty") or 0.0), 0.0)
        if qty <= 0:
            continue
        try:
            per_contract = max_loss_per_contract(
                str(position["spread_type"]),
                float(position["sell_strike"]),
                float(position["buy_strike"]),
                float(position["net_credit_btc"]),
            )
        except (KeyError, TypeError, ValueError):
            total += max(fallback_per_position_btc, 0.0)
            continue
        total += per_contract * qty
    return max(total, 0.0)


def available_entry_risk_btc(equity_btc: float, account_risk_pct: float,
                             portfolio_risk_pct: float,
                             active_risk_btc: float) -> float:
    """Return the smaller of the per-trade and remaining portfolio budgets."""
    per_trade = max(equity_btc, 0.0) * max(account_risk_pct, 0.0)
    portfolio_remaining = max(
        max(equity_btc, 0.0) * max(portfolio_risk_pct, 0.0)
        - max(active_risk_btc, 0.0),
        0.0)
    return min(per_trade, portfolio_remaining)
