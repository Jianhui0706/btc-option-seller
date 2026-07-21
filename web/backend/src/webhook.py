"""TradingView Webhook 接收（规范第五、十七节）。

TradingView 不支持自定义 header → passphrase 内嵌 payload；
handler 只做校验/去重/入队，立即返回（TV 超时约 3 秒，禁止在此下单）。
"""

from __future__ import annotations

import hmac
import json
import logging
import time
from typing import Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel, ValidationError

from .context import Ctx
from .monitor import events as ev

log = logging.getLogger("v2.webhook")

router = APIRouter()

OPEN_EVENTS = {"OPEN_REQUEST"}
EXIT_EVENTS = {"EXIT_DVOL", "EXIT_DVOL_ROLLING_30"}
STATUS_EVENT = "STATUS"
PINE_STATUS_KEY = "pine_status"


class TVSignal(BaseModel):
    passphrase: str
    event: str
    strategy: str = ""
    signal_id: str
    spread_type: Optional[str] = None          # CALL_CREDIT / PUT_CREDIT（开仓必填）
    btc_price: Optional[float] = None
    hv_percentile: Optional[float] = None
    dvol_percentile: Optional[float] = None    # 0~1
    dvol_tier: Optional[int] = None
    group_split_count: Optional[int] = None
    delta_account_risk_pct: Optional[float] = None
    delta_trigger_pct: Optional[float] = None
    call_hedge_release_pct: Optional[float] = None
    put_hedge_release_pct: Optional[float] = None


class PineStatus(BaseModel):
    """Pine 每根 2h bar 收盘发的心跳：它自己算出的门槛数值与逐条结论。

    心跳不进 signals 表 —— 它不是交易信号，混进去会污染信号中心的计数。
    """
    passphrase: str
    event: str
    strategy: str = ""
    bar_ts: Optional[int] = None
    btc_price: Optional[float] = None
    bar_open: Optional[float] = None
    bar_high: Optional[float] = None
    bar_low: Optional[float] = None
    cci: Optional[float] = None
    hv_percentile: Optional[float] = None      # 0~1
    boll_upper: Optional[float] = None
    boll_lower: Optional[float] = None
    dvol_percentile: Optional[float] = None    # 0~1，-1 表示样本不足
    dvol_tier: Optional[int] = None
    dvol_samples: Optional[int] = None
    cfg_ok: Optional[bool] = None
    tier_ok: Optional[bool] = None
    hv_ok: Optional[bool] = None
    cci_ok: Optional[bool] = None
    put_shape: Optional[bool] = None
    call_shape: Optional[bool] = None


def _payload_sane(sig: TVSignal) -> bool:
    """基本合法性检查。DVOL 档位由后端在开仓时权威计算（Pine 3分钟图历史不足，
    其 tier 仅作参考，-1/缺失表示样本不足交由后端判定）。"""
    if sig.dvol_tier is not None and not (-1 <= int(sig.dvol_tier) <= 3):
        return False
    if (sig.dvol_percentile is not None and sig.dvol_percentile != -1
            and not (0.0 <= sig.dvol_percentile <= 1.0)):
        return False
    if sig.group_split_count is not None and not (1 <= sig.group_split_count <= 20):
        return False
    ranges = (
        (sig.delta_account_risk_pct, 0.0001, 0.20),
        (sig.delta_trigger_pct, 0.0001, 1.0),
        (sig.call_hedge_release_pct, 0.50, 1.0),
        (sig.put_hedge_release_pct, 1.0, 1.50),
    )
    if any(v is not None and not (lo <= v <= hi) for v, lo, hi in ranges):
        return False
    return True


async def _handle_status(ctx: Ctx, body: dict, expected: str) -> dict:
    """Pine 心跳：存最新一份到 system_state，供信号中心看板对照。

    心跳同时是链路存活证明 —— 口令错/网络断时它会一起停，
    比"等一个几天才出现一次的开仓信号"能早得多地暴露问题。
    """
    status = PineStatus(**body)
    if not expected or not hmac.compare_digest(status.passphrase, expected):
        log.warning("pine status passphrase mismatch (bar_ts=%s)", status.bar_ts)
        return {"status": "rejected", "reason": "auth"}
    payload = status.model_dump(exclude={"passphrase"})
    payload["received_at"] = int(time.time() * 1000)
    await ctx.repo.set_state(PINE_STATUS_KEY,
                             json.dumps(payload, ensure_ascii=False))
    return {"status": "ok", "bar_ts": status.bar_ts}


@router.post("/webhook/tradingview")
async def tradingview_webhook(request: Request):
    ctx: Ctx = request.app.state.ctx
    repo = ctx.repo
    expected = ctx.cfg.env.webhook_auth_token
    try:
        body = await request.json()
        if (body or {}).get("event") == STATUS_EVENT:
            return await _handle_status(ctx, body, expected)
        sig = TVSignal(**body)
    except (ValidationError, ValueError) as exc:
        log.warning("bad webhook payload: %s", exc)
        await ev.emit(repo.conn, ev.REJECT_BAD_PAYLOAD, level="WARN",
                      error=str(exc)[:500])
        return {"status": "rejected", "reason": "bad_payload"}

    if not expected or not hmac.compare_digest(sig.passphrase, expected):
        log.warning("webhook passphrase mismatch (signal_id=%s)", sig.signal_id)
        return {"status": "rejected", "reason": "auth"}

    payload = sig.model_dump(exclude={"passphrase"})
    payload["source"] = "tradingview"

    # signal_id 去重（规范第五节）
    fresh = await repo.insert_signal(sig.signal_id, sig.event, sig.strategy,
                                     sig.spread_type or "", payload)
    if not fresh:
        await ev.emit(repo.conn, ev.REJECT_DUPLICATE_SIGNAL, level="WARN",
                      signal_id=sig.signal_id)
        return {"status": "rejected", "reason": "duplicate"}

    if sig.strategy and sig.strategy != ctx.cfg.strategy.name:
        await repo.set_signal_status(sig.signal_id, "REJECTED", "bad strategy")
        await ev.emit(repo.conn, ev.REJECT_BAD_PAYLOAD, level="WARN",
                      signal_id=sig.signal_id, strategy=sig.strategy)
        return {"status": "rejected", "reason": "bad_strategy"}

    if sig.event in OPEN_EVENTS:
        if sig.spread_type not in ("CALL_CREDIT", "PUT_CREDIT"):
            await repo.set_signal_status(sig.signal_id, "REJECTED", "bad spread_type")
            await ev.emit(repo.conn, ev.REJECT_BAD_PAYLOAD, level="WARN",
                          signal_id=sig.signal_id, spread_type=sig.spread_type)
            return {"status": "rejected", "reason": "bad_spread_type"}
        if not _payload_sane(sig):
            await repo.set_signal_status(sig.signal_id, "REJECTED", "bad tier/pct")
            await ev.emit(repo.conn, ev.REJECT_BAD_PAYLOAD, level="WARN",
                          signal_id=sig.signal_id, dvol_tier=sig.dvol_tier,
                          dvol_percentile=sig.dvol_percentile)
            return {"status": "rejected", "reason": "bad_tier_or_pct"}
        ctx.queue.put(ctx.queue.open(payload))
        return {"status": "queued", "signal_id": sig.signal_id}

    if sig.event in EXIT_EVENTS:
        await repo.set_signal_status(sig.signal_id, "ACCEPTED")
        parents = await repo.open_parents()
        for p in parents:
            ctx.queue.put(ctx.queue.normal_close(p["id"], ev.EXIT_DVOL_ROLLING_30))
        await ev.emit(repo.conn, ev.EXIT_DVOL_ROLLING_30, signal_id=sig.signal_id,
                      source="tradingview", parents=[p["id"] for p in parents])
        return {"status": "queued", "parents": [p["id"] for p in parents]}

    await repo.set_signal_status(sig.signal_id, "REJECTED", "unknown event")
    return {"status": "rejected", "reason": "unknown_event"}
