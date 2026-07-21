"""状态查询与手动控制端点（人工强平入口，规范第九节 source=MANUAL）。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from .context import Ctx
from .monitor import events as ev

router = APIRouter()


@router.get("/status")
async def status(request: Request):
    ctx: Ctx = request.app.state.ctx
    repo = ctx.repo
    parents = await repo.active_parents()
    detail = []
    for p in parents:
        detail.append({
            "parent": p,
            "groups": await repo.groups_of(p["id"]),
            "hedge": await repo.get_hedge(p["id"]),
        })
    return {
        "env": "testnet" if ctx.cfg.env.is_testnet else "mainnet",
        "trading_enabled": await ctx.trading_enabled(),
        "force_close_active": await ctx.force_close_active(),
        "hedge_frozen": ctx.hedge_frozen,
        "last_scan_ts": await repo.get_state("last_scan_ts"),
        "active_trades": detail,
    }


@router.get("/events")
async def recent_events(request: Request, limit: int = 50):
    ctx: Ctx = request.app.state.ctx
    cur = await ctx.repo.conn.execute(
        "SELECT * FROM events ORDER BY id DESC LIMIT ?", (min(limit, 500),))
    return [dict(r) for r in await cur.fetchall()]


@router.get("/hedge_stats")
async def hedge_stats(request: Request, days: int = 7):
    """对冲统计（规范十八.1）：按日聚合次数/手续费/平均量/跳过数。"""
    ctx: Ctx = request.app.state.ctx
    cur = await ctx.repo.conn.execute(
        """
        SELECT date(ts/1000,'unixepoch') AS day,
               SUM(action IN ('REBALANCE','ACTIVATE','CLOSE')) AS hedge_trades,
               ROUND(SUM(COALESCE(fee,0)), 8) AS total_fee,
               ROUND(AVG(CASE WHEN order_usd IS NOT NULL
                              THEN ABS(order_usd) END), 1) AS avg_order_usd,
               SUM(skipped_reason='MIN_AMOUNT') AS skipped_min_amount,
               SUM(skipped_reason='BELOW_TRIGGER') AS skipped_below_trigger
        FROM hedge_actions
        WHERE ts >= (strftime('%s','now') - ?*86400) * 1000
        GROUP BY day ORDER BY day DESC
        """, (days,))
    return [dict(r) for r in await cur.fetchall()]


@router.post("/control/trading")
async def set_trading(request: Request, enabled: bool):
    ctx: Ctx = request.app.state.ctx
    await ctx.repo.set_flag("trading_enabled", enabled)
    return {"trading_enabled": enabled}


@router.post("/control/force_close")
async def manual_force_close(request: Request, reason: str = "manual"):
    """人工强平（规范第九节）：入队最高优先级 FORCE_CLOSE。"""
    ctx: Ctx = request.app.state.ctx
    ctx.queue.put(ctx.queue.force_close(source="MANUAL", reason=reason))
    await ev.emit(ctx.repo.conn, ev.FORCE_CLOSE_MANUAL, level="WARN", reason=reason)
    return {"status": "queued"}


@router.post("/control/clear_force_close")
async def clear_force_close(request: Request):
    """人工确认清仓完成后解除 force_close_active（解除后才能重新开仓）。"""
    ctx: Ctx = request.app.state.ctx
    await ctx.repo.set_flag("force_close_active", False)
    return {"force_close_active": False}
