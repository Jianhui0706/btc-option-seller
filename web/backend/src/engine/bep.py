"""盈亏平衡点（BEP）与提前对冲价格判定（纯函数）。

BEP 由后端按实际成交净权利金计算（规范十八.3）；触及判定使用配置的完整复核 Bar
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


def activation_price(open_index_price: float, bep: float,
                     progress: float = 1.0) -> float:
    """主策略文档 α：返回开仓价走向 BEP 的进度价；默认 100% 即 BEP。"""
    if not 0.0 <= progress <= 2.0:
        raise ValueError("activation progress must be between 0 and 2")
    return open_index_price + (bep - open_index_price) * progress


def activation_touched(spread_type: str, bar_close: float,
                       trigger_price: float) -> bool:
    """完整复核 Bar 收盘价触及提前对冲价。"""
    if spread_type == "CALL_CREDIT":
        return bar_close >= trigger_price
    if spread_type == "PUT_CREDIT":
        return bar_close <= trigger_price
    raise ValueError(spread_type)


def bep_touched(spread_type: str, bar_close: float, bep: float) -> bool:
    """兼容旧调用：判断是否触及传入的风险线。"""
    return activation_touched(spread_type, bar_close, bep)


def release_line(spread_type: str, trigger_price: float, call_pct: float = 0.98,
                 put_pct: float = 1.02) -> float:
    if spread_type == "CALL_CREDIT":
        return trigger_price * call_pct
    if spread_type == "PUT_CREDIT":
        return trigger_price * put_pct
    raise ValueError(spread_type)


def release_touched(spread_type: str, bar_close: float, trigger_price: float,
                    call_pct: float = 0.98, put_pct: float = 1.02) -> bool:
    """价格回到撤销线内侧 → 标记 HEDGE_RELEASE_PENDING（下一根完整 bar 确认执行）。"""
    line = release_line(spread_type, trigger_price, call_pct, put_pct)
    if spread_type == "CALL_CREDIT":
        return bar_close <= line
    if spread_type == "PUT_CREDIT":
        return bar_close >= line
    raise ValueError(spread_type)


def recovery_release_line(open_index_price: float, bep: float,
                          recovery_pct: float = 0.50) -> float:
    """参考文档 β 口径：价格从 BEP 向开仓价恢复指定比例后的撤销线。"""
    if not 0.0 <= recovery_pct <= 1.0:
        raise ValueError("release recovery pct must be between 0 and 1")
    return bep - recovery_pct * (bep - open_index_price)


def progress_release_line(open_index_price: float, bep: float,
                          progress: float = 0.05) -> float:
    """旧口径兼容：progress 表示撤销时仍剩的风险距离比例。"""
    return recovery_release_line(open_index_price, bep, 1.0 - progress)


def progress_release_touched(spread_type: str, bar_close: float,
                             open_index_price: float, bep: float,
                             progress: float = 0.05) -> bool:
    line = progress_release_line(open_index_price, bep, progress)
    if spread_type == "CALL_CREDIT":
        return bar_close <= line
    if spread_type == "PUT_CREDIT":
        return bar_close >= line
    raise ValueError(spread_type)
