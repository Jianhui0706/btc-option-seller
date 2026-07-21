"""状态查询与手动控制端点（人工强平入口，规范第九节 source=MANUAL）。

访问控制：本机（127.0.0.1）免鉴权；远程访问需请求头 X-Auth = WEBHOOK_SECRET。
/webhook 不走本模块鉴权（它有自己的 passphrase）。
"""

from __future__ import annotations

import hmac
import time

from fastapi import APIRouter, HTTPException, Request

from .context import Ctx
from .monitor import events as ev

router = APIRouter()


def _auth(request: Request) -> Ctx:
    ctx: Ctx = request.app.state.ctx
    client = request.client.host if request.client else ""
    if client in ("127.0.0.1", "::1", "localhost"):
        return ctx
    token = request.headers.get("x-auth", "")
    if not hmac.compare_digest(token, ctx.cfg.env.webhook_secret):
        raise HTTPException(status_code=401, detail="X-Auth required")
    return ctx


@router.get("/status")
async def status(request: Request):
    ctx = _auth(request)
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
        "server_ts": int(time.time() * 1000),
        "active_trades": detail,
    }


@router.get("/account")
async def account_view(request: Request):
    """账户面板：权益/保证金/指数价/期权与期货持仓/挂单。"""
    ctx = _auth(request)
    from .exchange import account, futures, market_data
    cur = ctx.cfg.strategy.currency
    summ = await account.get_account_summary(ctx.ws, cur)
    opts = await account.get_option_positions(ctx.ws, cur)
    perp_usd, perp_delta = await futures.current_perp_position(
        ctx.ws, ctx.cfg.hedge.perp_instrument)
    orders = await account.get_open_orders(ctx.ws, cur)
    idx = await market_data.get_index_price(ctx.ws, cur)
    return {
        "equity_btc": summ.equity_btc,
        "available_funds": summ.available_funds,
        "margin_ratio": summ.margin_ratio,
        "index_price": idx,
        "option_positions": [{
            "instrument": p.instrument, "size": p.size, "delta": p.delta,
            "mark_price": p.mark_price, "avg_price": p.average_price,
            "pnl": p.floating_pnl} for p in opts],
        "net_option_delta": sum(p.delta for p in opts),
        "perp_usd": perp_usd,
        "perp_delta": perp_delta,
        "open_orders": [{
            "instrument": o["instrument_name"], "direction": o["direction"],
            "amount": o["amount"], "price": o.get("price"),
            "label": o.get("label", "")} for o in orders],
    }


@router.get("/trades")
async def trades_history(request: Request, limit: int = 50):
    """全部父交易（含已平仓历史）与子组。"""
    ctx = _auth(request)
    cur = await ctx.repo.conn.execute(
        "SELECT * FROM parent_trades ORDER BY id DESC LIMIT ?", (min(limit, 200),))
    out = []
    for r in await cur.fetchall():
        p = dict(r)
        p["groups"] = await ctx.repo.groups_of(p["id"])
        out.append(p)
    return out


@router.get("/events")
async def recent_events(request: Request, limit: int = 50):
    ctx = _auth(request)
    cur = await ctx.repo.conn.execute(
        "SELECT * FROM events ORDER BY id DESC LIMIT ?", (min(limit, 500),))
    return [dict(r) for r in await cur.fetchall()]


@router.get("/hedge_stats")
async def hedge_stats(request: Request, days: int = 7):
    """对冲统计（规范十八.1）：按日聚合次数/手续费/平均量/跳过数。"""
    ctx = _auth(request)
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
    ctx = _auth(request)
    await ctx.repo.set_flag("trading_enabled", enabled)
    return {"trading_enabled": enabled}


@router.post("/control/force_close")
async def manual_force_close(request: Request, reason: str = "manual"):
    """人工强平（规范第九节）：入队最高优先级 FORCE_CLOSE。"""
    ctx = _auth(request)
    ctx.queue.put(ctx.queue.force_close(source="MANUAL", reason=reason))
    await ev.emit(ctx.repo.conn, ev.FORCE_CLOSE_MANUAL, level="WARN", reason=reason)
    return {"status": "queued"}


@router.post("/control/clear_force_close")
async def clear_force_close(request: Request):
    """人工确认清仓完成后解除 force_close_active（解除后才能重新开仓）。"""
    ctx = _auth(request)
    await ctx.repo.set_flag("force_close_active", False)
    return {"force_close_active": False}


@router.post("/control/test_signal")
async def test_signal(request: Request, kind: str = "PUT_CREDIT", tier: int = 2):
    """调试用模拟信号（仅 testnet）：kind = PUT_CREDIT / CALL_CREDIT / EXIT_DVOL。"""
    ctx = _auth(request)
    if not ctx.cfg.env.is_testnet:
        raise HTTPException(status_code=403, detail="mainnet 禁用模拟信号")
    now = int(time.time())
    if kind == "EXIT_DVOL":
        sid = f"UI_EXIT_{now}"
        await ctx.repo.insert_signal(sid, "EXIT_DVOL", ctx.cfg.strategy.name, "", {})
        parents = await ctx.repo.open_parents()
        for p in parents:
            ctx.queue.put(ctx.queue.normal_close(p["id"], ev.EXIT_DVOL_ROLLING_30))
        await ev.emit(ctx.repo.conn, ev.EXIT_DVOL_ROLLING_30, signal_id=sid,
                      source="ui-test", parents=[p["id"] for p in parents])
        return {"status": "queued", "parents": [p["id"] for p in parents]}
    if kind not in ("PUT_CREDIT", "CALL_CREDIT"):
        raise HTTPException(status_code=400, detail="bad kind")
    levels = ctx.cfg.strategy.dvol_entry_levels
    pct = (levels[tier - 1] + (levels[tier] if tier < len(levels) else 1.0)) / 2 \
        if 1 <= tier <= len(levels) else 0.5
    payload = {
        "event": "OPEN_REQUEST", "strategy": ctx.cfg.strategy.name,
        "spread_type": kind, "signal_id": f"UI_{kind}_{now}",
        "hv_percentile": 0.5, "dvol_percentile": round(pct, 4), "dvol_tier": tier,
        "group_split_count": ctx.cfg.group_split_count_default,
    }
    fresh = await ctx.repo.insert_signal(payload["signal_id"], "OPEN_REQUEST",
                                         ctx.cfg.strategy.name, kind, payload)
    if not fresh:
        raise HTTPException(status_code=409, detail="duplicate signal_id")
    ctx.queue.put(ctx.queue.open(payload))
    return {"status": "queued", "signal_id": payload["signal_id"]}
