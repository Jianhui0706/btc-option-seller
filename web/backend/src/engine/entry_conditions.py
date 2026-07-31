"""开仓条件实时快照（信号中心诊断面板，规范第五节的只读复现）。

回答"现在 TradingView 到底能不能发开仓信号"：把 Pine 的每一道门槛
（DVOL 档位 / HV 分位 / CCI / 布林触发形态）拆成逐条通过与否。

⚠ 数据源差异：Pine 挂在 TradingView 的 BTCUSDT 上，后端只能取 Deribit
BTC-PERPETUAL，两条价格序列高度相关但不相同，CCI/布林/HV 会有小幅偏差。
本模块的结论是诊断参考；Pine 心跳（webhook STATUS）才是信号判定的真值。
"""

from __future__ import annotations

import math
import time
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


def typical_price(bars: Sequence[market_data.Candle]) -> list[float]:
    """典型价 HLC3 —— 标准 CCI 的输入源（规范第五节的 CCI20）。"""
    return [(b.high + b.low + b.close) / 3 for b in bars]


def cci(source: Sequence[float], length: int) -> Optional[float]:
    """CCI = (src - sma) / (0.015 × 平均绝对偏差)。

    source 传典型价 HLC3（Lambert 原始定义，也是 TradingView 内置 CCI
    指标的默认源）。⚠ 传收盘价会得到不同的值 —— 2026-07-22 实测同一批
    数据 close 口径 135.2、HLC3 口径 163.4，在阈值 101 附近会一个放行
    一个拦截。
    """
    if len(source) < length:
        return None
    window = source[-length:]
    mean = _sma(window)
    mad = sum(abs(x - mean) for x in window) / length
    if mad == 0:
        return None
    return (source[-1] - mean) / (0.015 * mad)


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


def evaluate(bars: Sequence[market_data.Candle], dvol_pct: Optional[float],
             dvol_tier: int, dvol_samples: int, cfg) -> dict:
    """把最后一根已收盘 2h bar 与各窗口数据折算成逐条门槛结论。

    bars 需按时间升序且只含已收盘 bar，最后一个即待判定的那根。
    三个指标的输入源不同：布林带与 HV 用收盘价，CCI 用典型价 HLC3。
    """
    es = cfg.entry_signal
    levels = cfg.strategy.dvol_entry_levels
    bar = bars[-1]
    closes = [b.close for b in bars]
    band = bollinger(closes, es.boll_len, es.boll_mult)
    cci_value = cci(typical_price(bars), es.cci_len)
    hvs = hv_series(closes, es.hv_len, es.hv_pctl_window)
    hv_pct = indicators.percentile_rank(hvs) if len(hvs) >= es.hv_pctl_window else None

    # Pine 的 cfgOk：档位阈值必须严格递增，否则整段开仓逻辑停发
    config_ok = all(levels[i] < levels[i + 1] for i in range(len(levels) - 1))
    # 这是只读复现值，不会进入订单执行链路。样本不足统一显示未就绪，
    # 不能被解释为后端替 TradingView 放行。
    tier_ok = dvol_tier > 0
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


async def _dvol_entry_cached(ctx, ttl: float = 600.0) -> tuple:
    """只读诊断用 DVOL 分位（2 年历史，分页 ~13 次调用）单独长缓存。

    DVOL 每 12h 才收一根，两年窗口的分位不会在几秒内变化；与 2h 门槛快照分开
    缓存，看板降到 15s 高频刷新时不必反复重拉全历史（防 Deribit 限频）。
    返回 (pct, tier, samples)，不得用于 V2 订单审批。
    """
    now = time.time()
    cached = ctx.dvol_entry_cache
    if cached is not None and now - ctx.dvol_entry_cache_ts < ttl:
        return cached
    cfg = ctx.cfg
    dvol_bars = await market_data.fetch_dvol_12h_days(
        ctx.ws, cfg.strategy.currency, cfg.strategy.dvol_entry_window_days)
    completed = [b.close for b in dvol_bars if b.completed()]
    dvol_pct, tier = indicators.dvol_entry_tier(
        completed, cfg.strategy.dvol_entry_window_days * 2,
        cfg.strategy.dvol_entry_levels)
    if dvol_pct is None:
        tier = -1          # 样本不足：与 Pine 一致标记为“未就绪”
    result = (dvol_pct, tier, len(completed))
    ctx.dvol_entry_cache = result
    ctx.dvol_entry_cache_ts = now
    return result


async def snapshot(ctx) -> dict:
    """拉取 2h K 线（每次）与 DVOL 分位（长缓存），算出当前已收盘 bar 的门槛结论。

    2h 门槛（HV/CCI/布林）随 snapshot_ttl_sec（默认 15s）刷新；DVOL 2 年分位走
    _dvol_entry_cached 长缓存，避免高频刷新重拉全历史。
    """
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

    dvol_pct, tier, samples = await _dvol_entry_cached(ctx)

    result = evaluate(done, dvol_pct, tier, samples, cfg)
    result.update({
        "source": "backend",
        "instrument": cfg.hedge.perp_instrument,
        "computed_at": now,
    })
    return result


BAR_2H_MS = BAR_2H_SEC * 1000
PUSH_GRACE_MS = 120_000       # 收盘后 2 分钟宽限：给 alert→webhook 的网络延迟留时间
PUSH_MATCH_LEAD_MS = 300_000  # 收盘前 5 分钟起算（时钟/时间戳微小偏差容错）
PUSH_MATCH_TAIL_MS = 3_600_000  # 收盘后 1 小时内到达都算这根 bar 的推送


async def push_watchdog(ctx) -> dict:
    """推送看门狗（规范第五节链路可观测）：核对「Pine 自报满足 → 是否真的收到推送」。

    Pine 心跳带着它对每道门槛的判定（put_shape/call_shape/cfg/tier/hv/cci）。当 Pine
    自己判定某根 2h bar 满足全部条件时，它就会 alert() 发 OPEN_REQUEST——我们理应收到。
    这里用「收盘时间窗 + spread 方向」匹配 signals 表（避开 signal_id 文本格式差异），
    没匹配到就是「满足了但没收到 TradingView 推送」。纯读，不产生副作用（emit 由扫描器
    按 bar 去重触发）。
    """
    from ..webhook import PINE_STATUS_KEY
    import json as _json
    repo = ctx.repo
    raw = await repo.get_state(PINE_STATUS_KEY)
    if not raw:
        return {"armed": False, "reason": "no_heartbeat"}
    try:
        p = _json.loads(raw)
    except (TypeError, ValueError):
        return {"armed": False, "reason": "bad_heartbeat"}

    side = ("PUT_CREDIT" if p.get("put_shape") else
            "CALL_CREDIT" if p.get("call_shape") else None)
    pine_can_fire = bool(p.get("cfg_ok") and p.get("tier_ok") and p.get("hv_ok")
                         and p.get("cci_ok") and side)
    bar_ts = p.get("bar_ts")
    if not pine_can_fire or not bar_ts:
        return {"armed": False, "side": side, "bar_ts": bar_ts,
                "pine_can_fire": pine_can_fire, "heartbeat_at": p.get("received_at")}

    bar_close = int(bar_ts) + BAR_2H_MS
    now = int(time.time() * 1000)
    cur = await repo.conn.execute(
        "SELECT signal_id, status, received_at FROM signals "
        "WHERE event='OPEN_REQUEST' AND spread_type=? "
        "AND received_at BETWEEN ? AND ? ORDER BY received_at DESC LIMIT 1",
        (side, bar_close - PUSH_MATCH_LEAD_MS, bar_close + PUSH_MATCH_TAIL_MS))
    row = await cur.fetchone()
    received = row is not None
    overdue = now >= bar_close + PUSH_GRACE_MS
    return {
        "armed": True,
        "side": side,
        "bar_ts": int(bar_ts),
        "bar_close": bar_close,
        "pine_can_fire": True,
        "signal_received": received,
        "signal_id": row["signal_id"] if row else None,
        "signal_status": row["status"] if row else None,
        "overdue": overdue,
        "alarm": bool(overdue and not received),
        "heartbeat_at": p.get("received_at"),
    }
