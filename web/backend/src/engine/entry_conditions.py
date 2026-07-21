"""开仓条件实时快照（信号中心诊断面板，规范第五节的只读复现）。

回答"现在 TradingView 到底能不能发开仓信号"：把 Pine 的每一道门槛
（DVOL 档位 / HV 分位 / CCI / 布林触发形态）拆成逐条通过与否。

⚠ 数据源差异：Pine 挂在 TradingView 的 BTCUSDT 上，后端只能取 Deribit
BTC-PERPETUAL，两条价格序列高度相关但不相同，CCI/布林/HV 会有小幅偏差。
本模块的结论是诊断参考；Pine 心跳（webhook STATUS）才是信号判定的真值。
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

from ..exchange import market_data
from . import indicators

BAR_2H_SEC = 7200


def _sma(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs)


def _stdev_pop(xs: Sequence[float]) -> float:
    """总体标准差（除以 N），与 Pine ta.stdev 默认口径一致。"""
    mean = _sma(xs)
    return math.sqrt(sum((x - mean) ** 2 for x in xs) / len(xs))


def bollinger(closes: Sequence[float], length: int,
              mult: float) -> Optional[tuple[float, float, float]]:
    """返回 (中轨, 上轨, 下轨)；样本不足返回 None。"""
    if len(closes) < length:
        return None
    window = closes[-length:]
    mid = _sma(window)
    dev = mult * _stdev_pop(window)
    return mid, mid + dev, mid - dev


def cci(closes: Sequence[float], length: int) -> Optional[float]:
    """Pine ta.cci(close, len)：(close - sma) / (0.015 × 平均绝对偏差)。"""
    if len(closes) < length:
        return None
    window = closes[-length:]
    mean = _sma(window)
    mad = sum(abs(x - mean) for x in window) / length
    if mad == 0:
        return None
    return (closes[-1] - mean) / (0.015 * mad)


def hv_annualized(closes: Sequence[float], hv_len: int) -> Optional[float]:
    """最后一根 bar 的年化历史波动率(%)：hv_len 根 2h 对数收益率标准差 × √(365×12)。"""
    if len(closes) < hv_len + 1:
        return None
    rets = [math.log(closes[i] / closes[i - 1])
            for i in range(len(closes) - hv_len, len(closes))]
    return _stdev_pop(rets) * math.sqrt(365 * 12) * 100


def hv_series(closes: Sequence[float], hv_len: int, count: int) -> list[float]:
    """最近 count 根 bar 各自的 HV 值（升序，最后一个是当前值）。"""
    out: list[float] = []
    for end in range(max(hv_len + 1, len(closes) - count + 1), len(closes) + 1):
        value = hv_annualized(closes[:end], hv_len)
        if value is not None:
            out.append(value)
    return out


def evaluate(bar: market_data.Candle, closes: Sequence[float], dvol_pct: Optional[float],
             dvol_tier: int, dvol_samples: int, cfg) -> dict:
    """把一根已收盘 2h bar 与各窗口数据折算成逐条门槛结论。"""
    es = cfg.entry_signal
    levels = cfg.strategy.dvol_entry_levels
    band = bollinger(closes, es.boll_len, es.boll_mult)
    cci_value = cci(closes, es.cci_len)
    hvs = hv_series(closes, es.hv_len, es.hv_pctl_window)
    hv_pct = indicators.percentile_rank(hvs) if len(hvs) >= es.hv_pctl_window else None

    # Pine 的 cfgOk：档位阈值必须严格递增，否则整段开仓逻辑停发
    config_ok = all(levels[i] < levels[i + 1] for i in range(len(levels) - 1))
    # tier=0 才是"明确低于 L1"；样本不足（tier=-1）交由后端权威判定，不拦
    tier_ok = dvol_tier != 0
    hv_ok = hv_pct is not None and hv_pct >= es.hv_pctl_min
    cci_ok = cci_value is not None and abs(cci_value) < es.cci_max

    put_shape = band is not None and bar.low < band[2] and bar.close > bar.open
    call_shape = band is not None and bar.high > band[1] and bar.close < bar.open

    checks = {
        "config": {"ok": config_ok, "levels": levels},
        "dvol_tier": {"ok": tier_ok, "tier": dvol_tier, "percentile": dvol_pct,
                      "samples": dvol_samples,
                      "window_bars": cfg.strategy.dvol_entry_window_days * 2},
        "hv": {"ok": hv_ok, "value": hv_pct, "threshold": es.hv_pctl_min,
               "samples": len(hvs), "window": es.hv_pctl_window},
        "cci": {"ok": cci_ok, "value": cci_value, "threshold": es.cci_max},
        "shape": {"ok": put_shape or call_shape, "put": put_shape, "call": call_shape,
                  "upper": band[1] if band else None,
                  "lower": band[2] if band else None,
                  "mid": band[0] if band else None},
    }
    gates_ok = config_ok and tier_ok and hv_ok and cci_ok
    return {
        "bar_ts": bar.ts,
        "bar": {"open": bar.open, "high": bar.high, "low": bar.low, "close": bar.close},
        "checks": checks,
        # 前四道是持续性门槛，第五道是每根 2h bar 才有一次的触发形态
        "gates_ok": gates_ok,
        "can_fire": gates_ok and (put_shape or call_shape),
        "blocking": [k for k, v in checks.items() if not v["ok"]],
    }


async def snapshot(ctx) -> dict:
    """拉取 2h K 线与 DVOL 全历史，算出当前这根已收盘 bar 的门槛结论。"""
    cfg = ctx.cfg
    es = cfg.entry_signal
    bars_needed = max(es.hv_pctl_window + es.hv_len + 8, es.boll_len + 4, es.cci_len + 4)
    candles = await market_data.fetch_candles(
        ctx.ws, cfg.hedge.perp_instrument, "120", bars_needed)
    now = market_data.now_ms()
    # 与 Pine 的 [1] + lookahead_on 对齐：只用已收盘的 2h bar
    done = [c for c in candles if c.ts + BAR_2H_SEC * 1000 <= now]
    if not done:
        raise RuntimeError("没有已收盘的 2h bar")

    dvol_bars = await market_data.fetch_dvol_12h_days(
        ctx.ws, cfg.strategy.currency, cfg.strategy.dvol_entry_window_days)
    completed = [b.close for b in dvol_bars if b.completed()]
    dvol_pct, tier = indicators.dvol_entry_tier(
        completed, cfg.strategy.dvol_entry_window_days * 2,
        cfg.strategy.dvol_entry_levels)
    if dvol_pct is None:
        tier = -1          # 样本不足：与 Pine 一致标记为"待判定"，不算作 tier=0 拦截

    result = evaluate(done[-1], [c.close for c in done], dvol_pct, tier,
                      len(completed), cfg)
    result.update({
        "source": "backend",
        "instrument": cfg.hedge.perp_instrument,
        "computed_at": now,
    })
    return result
