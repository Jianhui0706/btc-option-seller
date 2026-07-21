"""盈亏平衡点（BEP）计算与触及/撤销判定（规范第十一、十三节，纯函数）。

BEP 由后端按实际成交净权利金计算（规范十八.3）；触及判定用已收盘 3 分钟 K 线
收盘价（用户确认口径）。
"""

from __future__ import annotations


def bep_price(spread_type: str, sell_strike: float, net_credit_btc: float,
              open_index_price: float) -> float:
    """Call BEP = 卖腿行权价 + 净权利金(USD)；Put BEP = 卖腿行权价 − 净权利金(USD)。
    净权利金 BTC→USD 用开仓时指数价折算。"""
    credit_usd = net_credit_btc * open_index_price
    if spread_type == "CALL_CREDIT":
        return sell_strike + credit_usd
    if spread_type == "PUT_CREDIT":
        return sell_strike - credit_usd
    raise ValueError(spread_type)


def bep_touched(spread_type: str, bar_close: float, bep: float) -> bool:
    """已收盘 3min bar 收盘价触及 BEP → 对冲激活。"""
    if spread_type == "CALL_CREDIT":
        return bar_close >= bep
    return bar_close <= bep


def release_line(spread_type: str, bep: float, call_pct: float = 0.98,
                 put_pct: float = 1.02) -> float:
    return bep * (call_pct if spread_type == "CALL_CREDIT" else put_pct)


def release_touched(spread_type: str, bar_close: float, bep: float,
                    call_pct: float = 0.98, put_pct: float = 1.02) -> bool:
    """价格回到撤销线内侧 → 标记 HEDGE_RELEASE_PENDING（下一根完整 bar 确认执行）。"""
    line = release_line(spread_type, bep, call_pct, put_pct)
    if spread_type == "CALL_CREDIT":
        return bar_close <= line
    return bar_close >= line
