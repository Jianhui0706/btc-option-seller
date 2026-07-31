"""指标纯函数：分位数（中位秩，与 V1/Pine 对齐）、DVOL 滚动退出判定。"""

from __future__ import annotations

from typing import Optional, Sequence


def percentile_rank(series: Sequence[float]) -> Optional[float]:
    """最后一个值在整个序列中的分位（0-100），中位秩处理平局（移植 V1 strategy.py）。"""
    if not series:
        return None
    cur = series[-1]
    less = sum(1 for x in series if x < cur)
    less_eq = sum(1 for x in series if x <= cur)
    return 100.0 * (less + less_eq) / (2 * len(series))


def quantile(sorted_values: Sequence[float], q: float) -> float:
    """线性插值分位数，sorted_values 需已升序。"""
    n = len(sorted_values)
    if n == 0:
        raise ValueError("empty series")
    if n == 1:
        return sorted_values[0]
    pos = q * (n - 1)
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def dvol_entry_tier(completed_closes: Sequence[float], window_bars: int,
                    levels: Sequence[float]) -> tuple[Optional[float], int]:
    """DVOL 开仓分位与档位（后端只读诊断口径，规范第四.1节）。

    completed_closes: 已完成 12h DVOL 收盘序列（升序）；取最近 window_bars 个
    算当前值的中位秩分位（0~1）。档位：< L1 → 0（不开仓）；L1~L2 → 1；
    L2~L3 → 2；≥ L3 → 3。样本为空返回 (None, 0)。
    本函数不得用于 V2 信号的二次审批。
    """
    if window_bars < 1 or len(completed_closes) < window_bars:
        return None, 0
    tail = completed_closes[-window_bars:]
    pct = percentile_rank(tail)
    if pct is None:
        return None, 0
    pct /= 100.0
    tier = 0
    for lv in levels:
        if pct >= lv:
            tier += 1
    return pct, tier


def dvol_exit_triggered(completed_closes: Sequence[float], window: int = 120,
                        pct: float = 30.0) -> tuple[bool, Optional[float]]:
    """DVOL 滚动退出（规范第八节，防前视）。

    completed_closes: 已完成 12h DVOL bar 收盘值（升序，不含当前未完成 bar）。
    最后一个值为"当前完成值"，与其之前 window 个值的 pct 分位比较。
    返回 (是否触发, 分位阈值)；样本不足返回 (False, None)。
    """
    if len(completed_closes) < window + 1:
        return False, None
    current = completed_closes[-1]
    ref = sorted(completed_closes[-window - 1:-1])
    level = quantile(ref, pct / 100.0)
    return current < level, level
