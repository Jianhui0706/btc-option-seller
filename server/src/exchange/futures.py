"""BTC 永续对冲下单：IOC 限价（对手价 ± N tick），平仓单 reduce_only。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .deribit_ws import DeribitWS

log = logging.getLogger("v2.futures")


@dataclass
class PerpFill:
    requested_usd: float        # 带方向：正=买入
    filled_usd: float
    avg_price: float
    fee_btc: float


async def perp_ioc(ws: DeribitWS, instrument: str, usd_signed: float,
                   *, ioc_ticks: int = 5, reduce_only: bool = False) -> PerpFill:
    """按带符号 USD 数量下 IOC 限价单（正=buy 负=sell），返回成交结果。

    usd_signed 需已取整到合约面值（10 USD）的倍数。
    """
    assert abs(usd_signed) >= 10 - 1e-9, f"数量过小: {usd_signed}"
    direction = "buy" if usd_signed > 0 else "sell"
    info = await ws.request("public/get_instrument", {"instrument_name": instrument})
    tick = float(info["tick_size"])
    t = await ws.request("public/ticker", {"instrument_name": instrument})
    bid = float(t.get("best_bid_price") or 0.0)
    ask = float(t.get("best_ask_price") or 0.0)
    if bid <= 0 or ask <= 0:
        raise RuntimeError(f"{instrument} 盘口缺失")
    price = ask + ioc_ticks * tick if direction == "buy" else bid - ioc_ticks * tick
    price = round(round(price / tick) * tick, 10)

    params = {
        "instrument_name": instrument,
        "amount": abs(usd_signed),
        "type": "limit",
        "price": price,
        "time_in_force": "immediate_or_cancel",
        "label": "v2-hedge",
    }
    if reduce_only:
        params["reduce_only"] = True
    method = "private/buy" if direction == "buy" else "private/sell"
    result = await ws.request(method, params)
    order = result.get("order") or {}
    trades = result.get("trades") or []
    filled = float(order.get("filled_amount") or 0.0)
    avg = float(order.get("average_price") or 0.0)
    fee = sum(float(tr.get("fee") or 0.0) for tr in trades)
    log.info("perp %s %s %.0f USD @ %.1f → filled %.0f avg %.1f fee %.8f",
             direction, instrument, abs(usd_signed), price, filled, avg, fee)
    return PerpFill(requested_usd=usd_signed,
                    filled_usd=filled if direction == "buy" else -filled,
                    avg_price=avg, fee_btc=fee)


async def current_perp_position(ws: DeribitWS, instrument: str) -> tuple[float, float]:
    """返回 (size_usd 带方向, delta_btc)。无持仓返回 (0, 0)。"""
    rows = await ws.request("private/get_positions",
                            {"currency": instrument.split("-")[0], "kind": "future"})
    for p in rows:
        if p["instrument_name"] == instrument and abs(float(p.get("size") or 0)) > 1e-9:
            return float(p["size"]), float(p.get("delta") or 0.0)
    return 0.0, 0.0


async def flatten_perp(ws: DeribitWS, instrument: str, *, ioc_ticks: int = 5,
                       max_rounds: int = 5) -> Optional[PerpFill]:
    """把永续仓位平到 0（reduce_only IOC，多轮重试）。返回最后一次成交。"""
    last: Optional[PerpFill] = None
    for _ in range(max_rounds):
        size_usd, _ = await current_perp_position(ws, instrument)
        if abs(size_usd) < 10 - 1e-9:
            return last
        last = await perp_ioc(ws, instrument, -size_usd,
                              ioc_ticks=ioc_ticks, reduce_only=True)
    return last
