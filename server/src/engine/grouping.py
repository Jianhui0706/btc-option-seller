"""子组拆分（规范第七节，纯函数）。"""

from __future__ import annotations


def split_groups(total_qty: float, n_groups: int, min_lot: float = 0.1) -> list[float]:
    """把总量拆成 effective_group_count = min(手数, N) 组。

    以 min_lot 为单位整数化避免浮点误差；base=floor(Q/n)，前 remainder 组 +1 手。
    性质：sum == total_qty；任意两组差 ≤ min_lot；每组 ≥ min_lot。
    """
    if n_groups < 1:
        raise ValueError(f"group_split_count 必须 ≥ 1（当前 {n_groups}）")
    lots = int(round(total_qty / min_lot))
    if lots <= 0:
        return []
    n = min(lots, n_groups)
    base, rem = divmod(lots, n)
    return [round((base + (1 if i < rem else 0)) * min_lot, 10) for i in range(n)]
