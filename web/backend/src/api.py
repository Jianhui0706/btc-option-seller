"""状态查询与手动控制端点（人工强平入口，规范第九节 source=MANUAL）。"""

from __future__ import annotations

from datetime import datetime
import json
import time

from fastapi import APIRouter, HTTPException, Request

from . import webhook
from .context import Ctx
from .engine import entry_conditions as entry_conditions_mod
from .monitor import events as ev

router = APIRouter()


def _auth(request: Request) -> Ctx:
    return request.app.state.ctx


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


@router.get("/api/signals")
async def signal_feed(request: Request, limit: int = 100):
    ctx = _auth(request)
    safe_limit = max(1, min(limit, 500))
    cur = await ctx.repo.conn.execute(
        "SELECT signal_id, event, strategy, spread_type, payload_json, "
        "received_at, status, reject_reason FROM signals "
        "WHERE json_extract(payload_json, '$.source') = 'tradingview' "
        "OR (json_extract(payload_json, '$.source') IS NULL "
        "AND signal_id NOT LIKE 'UI\\_%' ESCAPE '\\' "
        "AND signal_id NOT LIKE 'TEST\\_%' ESCAPE '\\' "
        "AND signal_id NOT LIKE 'TEST-%') "
        "ORDER BY received_at DESC LIMIT ?",
        (safe_limit,),
    )
    items = []
    for row in await cur.fetchall():
        item = dict(row)
        try:
            payload = json.loads(item.pop("payload_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        item["source"] = "tradingview"
        item["payload"] = payload
        items.append(item)

    summary = {
        "total": len(items),
        "accepted": sum(i["status"] == "ACCEPTED" for i in items),
        "rejected": sum(i["status"] == "REJECTED" for i in items),
        "pending": sum(i["status"] not in ("ACCEPTED", "REJECTED") for i in items),
    }
    return {
        "configured": bool(ctx.cfg.env.webhook_auth_token),
        "webhook_path": "/webhook/tradingview",
        "last_received_at": items[0]["received_at"] if items else None,
        "summary": summary,
        "items": items,
    }


@router.get("/api/entry_conditions")
async def entry_conditions(request: Request, refresh: bool = False):
    """开仓条件看板：后端独立复现的门槛 + Pine 心跳上报的真值。

    绝不抛异常 —— 前端是 Promise.all 并发刷新，这里 500 会连带打掉整个面板。
    行情不可用时返回 backend=None + error，看板单独降级。
    """
    ctx = _auth(request)
    ttl = ctx.cfg.entry_signal.snapshot_ttl_sec
    backend, error = ctx.entry_snapshot, None
    if refresh or backend is None or time.time() - ctx.entry_snapshot_ts > ttl:
        try:
            backend = await entry_conditions_mod.snapshot(ctx)
            ctx.entry_snapshot, ctx.entry_snapshot_ts = backend, time.time()
        except Exception as exc:  # noqa: BLE001 - 看板降级，不影响交易链路
            error = str(exc)[:200]
            backend = ctx.entry_snapshot      # 保留上一份，标注为陈旧

    raw = await ctx.repo.get_state(webhook.PINE_STATUS_KEY)
    try:
        pine = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        pine = None
    return {
        "backend": backend,
        "backend_error": error,
        "pine": pine,
        "server_ts": int(time.time() * 1000),
        "params": ctx.cfg.entry_signal.model_dump(),
    }


@router.get("/api/performance")
async def performance(request: Request):
    ctx = _auth(request)
    conn = ctx.repo.conn
    now_ms = int(time.time() * 1000)
    local_now = datetime.now().astimezone()
    today_start = int(local_now.replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    seven_day_start = now_ms - 7 * 86400 * 1000
    thirty_day_start = now_ms - 30 * 86400 * 1000

    pnl_row = await (await conn.execute(
        """
        SELECT COALESCE(SUM(realized_pnl_btc), 0) AS realized,
               COALESCE(SUM(CASE WHEN state='OPEN' THEN unrealized_pnl_btc ELSE 0 END), 0) AS unrealized,
               COALESCE(SUM(CASE WHEN closed_at >= ? THEN realized_pnl_btc ELSE 0 END), 0) AS today_realized,
               COALESCE(SUM(CASE WHEN closed_at >= ? THEN realized_pnl_btc ELSE 0 END), 0) AS last_7d_realized,
               COALESCE(SUM(CASE WHEN closed_at >= ? THEN realized_pnl_btc ELSE 0 END), 0) AS last_30d_realized
        FROM trade_groups
        """,
        (today_start, seven_day_start, thirty_day_start),
    )).fetchone()
    hedge_fee_row = await (await conn.execute(
        "SELECT COALESCE(SUM(ABS(COALESCE(fee, 0))), 0) FROM hedge_actions"
    )).fetchone()
    trade_cur = await conn.execute(
        """
        SELECT p.id, p.signal_id, p.spread_type, p.state, p.qty_total,
               p.opened_at, p.closed_at, p.close_reason,
               COALESCE(SUM(g.realized_pnl_btc), 0) AS realized_pnl_btc,
               COALESCE(SUM(CASE WHEN g.state='OPEN' THEN g.unrealized_pnl_btc ELSE 0 END), 0) AS unrealized_pnl_btc
        FROM parent_trades p
        LEFT JOIN trade_groups g ON g.parent_id=p.id
        GROUP BY p.id
        ORDER BY p.id DESC
        LIMIT 200
        """
    )
    trades = [dict(row) for row in await trade_cur.fetchall()]
    completed = [t for t in trades if t["state"] in ("CLOSED", "FORCE_CLOSED")]
    wins = sum(float(t["realized_pnl_btc"]) > 0 for t in completed)
    losses = sum(float(t["realized_pnl_btc"]) < 0 for t in completed)
    decided = wins + losses

    daily_cur = await conn.execute(
        """
        SELECT date(closed_at/1000, 'unixepoch', 'localtime') AS day,
               COALESCE(SUM(realized_pnl_btc), 0) AS realized_pnl_btc
        FROM trade_groups
        WHERE closed_at IS NOT NULL AND closed_at >= ?
        GROUP BY day ORDER BY day
        """,
        (thirty_day_start,),
    )
    daily = [dict(row) for row in await daily_cur.fetchall()]

    realized = float(pnl_row["realized"])
    unrealized = float(pnl_row["unrealized"])
    hedge_fees = float(hedge_fee_row[0])
    return {
        "summary": {
            "realized_pnl_btc": realized,
            "unrealized_pnl_btc": unrealized,
            "hedge_fees_btc": hedge_fees,
            "tracked_net_pnl_btc": realized + unrealized - hedge_fees,
            "today_realized_btc": float(pnl_row["today_realized"]),
            "last_7d_realized_btc": float(pnl_row["last_7d_realized"]),
            "last_30d_realized_btc": float(pnl_row["last_30d_realized"]),
            "wins": wins,
            "losses": losses,
            "win_rate": wins / decided if decided else 0.0,
            "closed_trades": len(completed),
        },
        "daily": daily,
        "trades": trades,
    }


@router.get("/hedge_stats")
async def hedge_stats(request: Request, days: int = 7):
    """对冲统计（规范十八.1）：按日聚合次数/手续费/平均量/跳过数。"""
    ctx = _auth(request)
    cur = await ctx.repo.conn.execute(
        """
        SELECT date(ts/1000,'unixepoch') AS day,
               SUM(action IN ('REBALANCE','RELEASE','CLOSE')) AS hedge_trades,
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


@router.post("/control/close_group")
async def close_group(request: Request, group_id: int):
    """人工关闭一个管理子组；后端仍会复核父交易和子组状态。"""
    ctx = _auth(request)
    group = await ctx.repo.get_group(group_id)
    if not group:
        raise HTTPException(status_code=404, detail="group not found")
    if group["state"] != "OPEN":
        raise HTTPException(status_code=409, detail="group is not open")
    ctx.queue.put(ctx.queue.group_close(group_id, "MANUAL_GROUP"))
    return {"status": "queued", "group_id": group_id}


@router.post("/control/clear_force_close")
async def clear_force_close(request: Request):
    """人工确认清仓完成后解除 force_close_active（解除后才能重新开仓）。"""
    ctx = _auth(request)
    await ctx.repo.set_flag("force_close_active", False)
    return {"force_close_active": False}

