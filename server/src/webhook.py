"""TradingView Webhook 接收（规范第五、十七节）。

TradingView 不支持自定义 header → passphrase 内嵌 payload；
handler 只做校验/去重/入队，立即返回（TV 超时约 3 秒，禁止在此下单）。
"""

from __future__ import annotations

import hmac
import logging
from typing import Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel, ValidationError

from .context import Ctx
from .monitor import events as ev

log = logging.getLogger("v2.webhook")

router = APIRouter()

OPEN_EVENTS = {"OPEN_REQUEST"}
EXIT_EVENTS = {"EXIT_DVOL", "EXIT_DVOL_ROLLING_30"}


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


def _payload_sane(tier: Optional[int], pct: Optional[float]) -> bool:
    """基本合法性检查。DVOL 档位由后端在开仓时权威计算（Pine 3分钟图历史不足，
    其 tier 仅作参考，-1/缺失表示样本不足交由后端判定）。"""
    if tier is not None and not (-1 <= int(tier) <= 3):
        return False
    if pct is not None and pct != -1 and not (0.0 <= pct <= 1.0):
        return False
    return True


@router.post("/webhook/tradingview")
async def tradingview_webhook(request: Request):
    ctx: Ctx = request.app.state.ctx
    repo = ctx.repo
    try:
        body = await request.json()
        sig = TVSignal(**body)
    except (ValidationError, ValueError) as exc:
        log.warning("bad webhook payload: %s", exc)
        await ev.emit(repo.conn, ev.REJECT_BAD_PAYLOAD, level="WARN",
                      error=str(exc)[:500])
        return {"status": "rejected", "reason": "bad_payload"}

    if not hmac.compare_digest(sig.passphrase, ctx.cfg.env.webhook_secret):
        log.warning("webhook passphrase mismatch (signal_id=%s)", sig.signal_id)
        return {"status": "rejected", "reason": "auth"}

    payload = sig.model_dump(exclude={"passphrase"})

    # signal_id 去重（规范第五节）
    fresh = await repo.insert_signal(sig.signal_id, sig.event, sig.strategy,
                                     sig.spread_type or "", payload)
    if not fresh:
        await ev.emit(repo.conn, ev.REJECT_DUPLICATE_SIGNAL, level="WARN",
                      signal_id=sig.signal_id)
        return {"status": "rejected", "reason": "duplicate"}

    if sig.event in OPEN_EVENTS:
        if sig.spread_type not in ("CALL_CREDIT", "PUT_CREDIT"):
            await repo.set_signal_status(sig.signal_id, "REJECTED", "bad spread_type")
            await ev.emit(repo.conn, ev.REJECT_BAD_PAYLOAD, level="WARN",
                          signal_id=sig.signal_id, spread_type=sig.spread_type)
            return {"status": "rejected", "reason": "bad_spread_type"}
        if not _payload_sane(sig.dvol_tier, sig.dvol_percentile):
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
