"""行情数据：指数价、K 线、12h DVOL、期权链（Call/Put 通用）。

移植自 V1 paper_trader.py 的 fetch_candles_1m / fetch_dvol_1m / option_chain_tickers，
改为通用分辨率并支持 put。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from .deribit_ws import DeribitWS

DVOL_12H_SEC = 12 * 3600


def now_ms() -> int:
    return int(time.time() * 1000)


async def get_index_price(ws: DeribitWS, currency: str = "BTC") -> float:
    r = await ws.request("public/get_index_price", {"index_name": f"{currency.lower()}_usd"})
    return float(r["index_price"])


@dataclass
class Candle:
    ts: int          # bar 开始时间（ms）
    open: float
    high: float
    low: float
    close: float


async def fetch_candles(ws: DeribitWS, instrument: str, resolution: str,
                        bars: int) -> list[Candle]:
    """resolution: 分钟数字符串（"3"/"120"）或 "1D"。返回按时间升序、最多 bars 根。"""
    res_sec = 86400 if resolution == "1D" else int(resolution) * 60
    end = now_ms()
    start = end - bars * res_sec * 1000
    r = await ws.request("public/get_tradingview_chart_data", {
        "instrument_name": instrument,
        "start_timestamp": start,
        "end_timestamp": end,
        "resolution": resolution,
    })
    if r.get("status") != "ok" or not r.get("close"):
        raise RuntimeError(f"K线获取失败: {instrument} {resolution}")
    rows = [Candle(t, o, h, l, c) for t, o, h, l, c in
            zip(r["ticks"], r["open"], r["high"], r["low"], r["close"])]
    return rows[-bars:]


@dataclass
class DvolBar:
    ts: int          # bar 开始时间（ms）
    close: float

    def completed(self, bar_sec: int = DVOL_12H_SEC) -> bool:
        return self.ts + bar_sec * 1000 <= now_ms()


async def fetch_dvol_12h(ws: DeribitWS, currency: str = "BTC",
                         points: int = 130) -> list[DvolBar]:
    """12h DVOL K 线（Deribit 原生 resolution=43200 秒）。升序返回。"""
    end = now_ms()
    start = end - points * DVOL_12H_SEC * 1000
    r = await ws.request("public/get_volatility_index_data", {
        "currency": currency, "start_timestamp": start, "end_timestamp": end,
        "resolution": str(DVOL_12H_SEC),
    })
    data = r.get("data") or []
    return [DvolBar(ts=int(row[0]), close=float(row[4])) for row in data]


async def fetch_dvol_12h_days(ws: DeribitWS, currency: str = "BTC",
                              days: int = 730) -> list[DvolBar]:
    """分页拉取 N 天的 12h DVOL 全历史（开仓分位窗口用，约 730 天 = 1460 根）。

    TradingView 3 分钟图加载不了 2 年历史，DVOL 开仓分位由后端在此权威计算。
    """
    end = now_ms()
    # 多取两天，覆盖起止边界与当前未完成 12h bar；调用方再严格截取完整窗口。
    start = end - (days + 2) * 86400 * 1000
    step = 120 * DVOL_12H_SEC * 1000          # 每页约 120 根
    out: dict[int, float] = {}
    cur = start
    while cur < end:
        chunk_end = min(end, cur + step)
        r = await ws.request("public/get_volatility_index_data", {
            "currency": currency, "start_timestamp": cur,
            "end_timestamp": chunk_end, "resolution": str(DVOL_12H_SEC),
        })
        for row in r.get("data") or []:
            out[int(row[0])] = float(row[4])
        cur = chunk_end + 1
    return [DvolBar(ts=t, close=c) for t, c in sorted(out.items())]


@dataclass
class OptionChain:
    tickers: list[dict]          # 含 greeks.delta / best_bid_price / best_ask_price / strike
    option_type: str             # "call" / "put"
    dte: float
    expiration_timestamp: int
    checked_expiries: int
    target_range_match: bool


def has_two_sided_quote(ticker: dict) -> bool:
    bid = float(ticker.get("best_bid_price") or 0.0)
    ask = float(ticker.get("best_ask_price") or 0.0)
    return bid > 0.0 and ask > 0.0 and bid <= ask


def strike_of(ticker: dict) -> float:
    return float(ticker["instrument_name"].split("-")[2])


async def option_chain_tickers(ws: DeribitWS, currency: str, option_type: str,
                               dte_lo: float, dte_hi: float) -> OptionChain:
    """按目标 DTE 区间排序逐个检查到期日，返回首个至少两只双边报价的链（沿用 V1 降级规则）。"""
    instruments = await ws.request(
        "public/get_instruments", {"currency": currency, "kind": "option", "expired": False})
    side = [i for i in instruments if i["option_type"] == option_type]
    now = now_ms()
    expiries = sorted({i["expiration_timestamp"] for i in side})
    if not expiries:
        raise RuntimeError(f"{currency} 没有可用 {option_type} 到期日")
    mid = (dte_lo + dte_hi) / 2
    ranked = sorted(expiries, key=lambda e: (
        0 if dte_lo <= (e - now) / 86400000 <= dte_hi else 1,
        abs((e - now) / 86400000 - mid),
    ))

    fallback: Optional[OptionChain] = None
    for checked, expiry in enumerate(ranked, start=1):
        tickers = []
        for inst in side:
            if inst["expiration_timestamp"] != expiry:
                continue
            t = await ws.request("public/ticker", {"instrument_name": inst["instrument_name"]})
            if (t.get("greeks") or {}).get("delta") is None:
                continue
            t["expiration_timestamp"] = expiry
            t["strike"] = strike_of(t)
            tickers.append(t)
        dte = (expiry - now) / 86400000
        chain = OptionChain(
            tickers=tickers, option_type=option_type, dte=dte,
            expiration_timestamp=int(expiry), checked_expiries=checked,
            target_range_match=dte_lo <= dte <= dte_hi,
        )
        fallback = fallback or chain
        if sum(1 for t in tickers if has_two_sided_quote(t)) >= 2:
            return chain
    assert fallback is not None
    fallback.checked_expiries = len(ranked)
    return fallback
