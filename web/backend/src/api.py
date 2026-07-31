"""状态查询与手动控制端点（人工强平入口，规范第九节 source=MANUAL）。"""

from __future__ import annotations

from datetime import datetime
import json
import time

import aiosqlite
from fastapi import APIRouter, HTTPException, Request, Response

from . import webhook
from .context import Ctx
from .engine import entry_conditions as entry_conditions_mod
from .engine import hedge as hedge_mod
from .engine import risk as risk_mod
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
        hedge_row = await repo.get_hedge(p["id"])
        params = await hedge_mod.trade_hedge_params(ctx, p)
        detail.append({
            "parent": p,
            "groups": await repo.groups_of(p["id"]),
            "hedge": hedge_row,
            "hedge_diagnostic": hedge_mod.hedge_diagnostic(
                ctx, p, hedge_row, getattr(ctx, "hedge_snapshot", None), params),
        })
    stored_last_scan = await repo.get_state("last_scan_ts")
    last_scan_ts = (
        ctx.scan_last_completed_ts
        or (int(stored_last_scan) if stored_last_scan else None)
    )
    error_is_latest = bool(
        ctx.scan_last_error
        and (ctx.scan_last_error_ts or 0) > (last_scan_ts or 0)
    )
    scan_state = (
        "error" if error_is_latest
        else "running" if ctx.scan_running
        else "ok" if last_scan_ts
        else "starting"
    )
    return {
        "env": "testnet" if ctx.cfg.env.is_testnet else "mainnet",
        "trading_enabled": await ctx.trading_enabled(),
        "force_close_active": await ctx.force_close_active(),
        "trading_pause_reason": (
            await repo.get_state(risk_mod.PAUSE_REASON_KEY) or None),
        "risk_auto_resume_state": (
            await repo.get_state(risk_mod.AUTO_RESUME_STATE_KEY) or None),
        "hedge_frozen": ctx.hedge_frozen,
        "last_scan_ts": last_scan_ts,
        "scan": {
            "state": scan_state,
            "running": ctx.scan_running,
            "last_started_ts": ctx.scan_last_started_ts,
            "last_completed_ts": last_scan_ts,
            "last_error": ctx.scan_last_error if error_is_latest else None,
            "last_error_kind": (
                ctx.scan_last_error_kind if error_is_latest else None
            ),
            "last_error_ts": ctx.scan_last_error_ts if error_is_latest else None,
        },
        "execution": {
            "busy": ctx.current_command_kind is not None,
            "command": ctx.current_command_kind,
            "started_ts": ctx.current_command_started_ts,
        },
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
        "SELECT p.*, s.strategy, s.payload_json AS signal_payload_json "
        "FROM parent_trades p LEFT JOIN signals s ON s.signal_id=p.signal_id "
        "ORDER BY p.id DESC LIMIT ?", (min(limit, 200),))
    out = []
    for r in await cur.fetchall():
        p = dict(r)
        try:
            signal_payload = json.loads(
                p.pop("signal_payload_json", None) or "{}")
        except (TypeError, json.JSONDecodeError):
            signal_payload = {}
        if not isinstance(signal_payload, dict):
            signal_payload = {}
        p["tv_id"] = signal_payload.get("tv_id") or p.get("trade_id")
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
async def signal_feed(
    request: Request,
    limit: int = 100,
    strategy: str | None = None,
):
    ctx = _auth(request)
    safe_limit = max(1, min(limit, 500))
    selected_strategy = (strategy or "").strip()
    tradingview_filter = (
        "(json_extract(payload_json, '$.source') = 'tradingview' "
        "OR (json_extract(payload_json, '$.source') IS NULL "
        "AND signal_id NOT LIKE 'UI\\_%' ESCAPE '\\' "
        "AND signal_id NOT LIKE 'TEST\\_%' ESCAPE '\\' "
        "AND signal_id NOT LIKE 'TEST-%'))"
    )
    item_filter = tradingview_filter
    item_params: tuple = (safe_limit,)
    if selected_strategy:
        item_filter = f"({tradingview_filter}) AND strategy=?"
        item_params = (selected_strategy, safe_limit)
    cur = await ctx.repo.conn.execute(
        "SELECT signal_id, event, strategy, spread_type, payload_json, "
        "received_at, status, reject_reason FROM signals "
        f"WHERE {item_filter} "
        "ORDER BY received_at DESC LIMIT ?",
        item_params,
    )
    items = []
    for row in await cur.fetchall():
        item = dict(row)
        try:
            payload = json.loads(item.pop("payload_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        payload.pop("passphrase", None)
        item["key"] = f"signal:{item['signal_id']}"
        item["kind"] = (
            "OPEN_SIGNAL" if item["event"] == "OPEN_REQUEST" else "EXIT_SIGNAL")
        item["source"] = "tradingview"
        item["payload"] = payload
        items.append(item)

    for row in await ctx.repo.recent_pine_heartbeats(
        safe_limit,
        strategy=selected_strategy or None,
    ):
        payload = dict(row.get("payload") or {})
        payload.pop("passphrase", None)
        items.append({
            "key": f"heartbeat:{row['id']}",
            "kind": "HEARTBEAT",
            "event": "STATUS",
            "received_at": row["received_at"],
            "status": "CONNECTED",
            "reject_reason": None,
            "source": "tradingview",
            "strategy": row.get("strategy"),
            "signal_id": None,
            "spread_type": None,
            "payload": payload,
        })

    items.sort(key=lambda item: (item["received_at"], item["key"]), reverse=True)
    items = items[:safe_limit]
    signal_items = [item for item in items if item["kind"] != "HEARTBEAT"]
    summary = {
        "total": len(items),
        "heartbeats": sum(i["kind"] == "HEARTBEAT" for i in items),
        "signal_total": len(signal_items),
        "open_signals": sum(i["kind"] == "OPEN_SIGNAL" for i in items),
        "exit_signals": sum(i["kind"] == "EXIT_SIGNAL" for i in items),
        "accepted": sum(i["status"] == "ACCEPTED" for i in signal_items),
        "rejected": sum(i["status"] == "REJECTED" for i in signal_items),
        "pending": sum(
            i["status"] not in ("ACCEPTED", "REJECTED") for i in signal_items),
    }

    # 多策略概览按完整历史聚合，而不是只统计当前分页的最近记录。
    # strategy 只是经过 Webhook 口令认证后的归属标签，不是鉴权凭证。
    strategies: dict[str, dict] = {}

    def strategy_slot(name: str | None) -> dict:
        key = (name or "").strip()
        if key not in strategies:
            strategies[key] = {
                "strategy": key,
                "account_name": "",
                "signal_total": 0,
                "open_signals": 0,
                "exit_signals": 0,
                "accepted": 0,
                "rejected": 0,
                "pending": 0,
                "heartbeats": 0,
                "active_trades": 0,
                "last_signal_at": None,
                "last_heartbeat_at": None,
                "last_activity_at": None,
            }
        return strategies[key]

    cur = await ctx.repo.conn.execute(
        "SELECT strategy, "
        "MAX(COALESCE(json_extract(payload_json, '$.account_name'), "
        "json_extract(payload_json, '$.accountName'), '')) AS account_name, "
        "COUNT(*) AS signal_total, "
        "SUM(CASE WHEN event = 'OPEN_REQUEST' THEN 1 ELSE 0 END) AS open_signals, "
        "SUM(CASE WHEN event <> 'OPEN_REQUEST' THEN 1 ELSE 0 END) AS exit_signals, "
        "SUM(CASE WHEN status = 'ACCEPTED' THEN 1 ELSE 0 END) AS accepted, "
        "SUM(CASE WHEN status = 'REJECTED' THEN 1 ELSE 0 END) AS rejected, "
        "SUM(CASE WHEN status NOT IN ('ACCEPTED', 'REJECTED') THEN 1 ELSE 0 END) "
        "AS pending, MAX(received_at) AS last_signal_at "
        f"FROM signals WHERE {tradingview_filter} GROUP BY strategy"
    )
    for row in await cur.fetchall():
        values = dict(row)
        slot = strategy_slot(values.pop("strategy"))
        slot.update(values)

    cur = await ctx.repo.conn.execute(
        "SELECT strategy, COUNT(*) AS heartbeats, "
        "MAX(received_at) AS last_heartbeat_at "
        "FROM pine_heartbeats GROUP BY strategy"
    )
    for row in await cur.fetchall():
        values = dict(row)
        slot = strategy_slot(values.pop("strategy"))
        slot["heartbeats"] = values["heartbeats"]
        slot["last_heartbeat_at"] = values["last_heartbeat_at"]

    cur = await ctx.repo.conn.execute(
        "SELECT s.strategy, COUNT(*) AS active_trades "
        "FROM parent_trades p JOIN signals s ON s.signal_id = p.signal_id "
        "WHERE p.state NOT IN ('CLOSED', 'FORCE_CLOSED', 'OPEN_FAILED') "
        "GROUP BY s.strategy"
    )
    for row in await cur.fetchall():
        slot = strategy_slot(row["strategy"])
        slot["active_trades"] = row["active_trades"]

    for slot in strategies.values():
        slot["last_activity_at"] = max(
            slot["last_signal_at"] or 0,
            slot["last_heartbeat_at"] or 0,
        ) or None

    strategy_items = sorted(
        strategies.values(),
        key=lambda item: (item["last_activity_at"] or 0, item["strategy"]),
        reverse=True,
    )
    return {
        "configured": bool(ctx.cfg.env.webhook_auth_token),
        "webhook_path": "/webhook/signal",
        "last_received_at": items[0]["received_at"] if items else None,
        "server_ts": int(time.time() * 1000),
        "summary": summary,
        "strategies": strategy_items,
        "items": items,
    }


@router.delete("/api/strategies")
async def delete_strategy_records(request: Request, strategy: str):
    """删除一个已停用的 TradingView 策略及其全部后台历史记录。

    策略由 Webhook 自动发现，并不存在独立的“接口实例”。共享 Webhook
    保留不动；这里只清理该 strategy/source_name 归属的数据。
    """
    ctx = _auth(request)
    strategy = strategy.strip()
    if not strategy or len(strategy) > 200:
        raise HTTPException(status_code=400, detail="策略名称不合法")

    async with aiosqlite.connect(ctx.db.path, timeout=15) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("BEGIN IMMEDIATE")
        try:
            active = await (await conn.execute(
                """
                SELECT p.id, p.state
                FROM parent_trades p
                JOIN signals s ON s.signal_id=p.signal_id
                WHERE s.strategy=?
                  AND p.state NOT IN ('CLOSED', 'FORCE_CLOSED', 'OPEN_FAILED')
                ORDER BY p.id
                """,
                (strategy,),
            )).fetchall()
            if active:
                await conn.rollback()
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "该策略仍有关联仓位，必须先完成平仓",
                        "active_trades": [
                            {"id": row["id"], "state": row["state"]}
                            for row in active
                        ],
                    },
                )

            signal_rows = await (await conn.execute(
                "SELECT signal_id FROM signals WHERE strategy=?",
                (strategy,),
            )).fetchall()
            signal_ids = [row["signal_id"] for row in signal_rows]
            parent_rows = []
            if signal_ids:
                marks = ",".join("?" for _ in signal_ids)
                parent_rows = await (await conn.execute(
                    f"SELECT id FROM parent_trades "
                    f"WHERE signal_id IN ({marks})",
                    signal_ids,
                )).fetchall()
            parent_ids = [row["id"] for row in parent_rows]
            group_ids: list[int] = []
            if parent_ids:
                marks = ",".join("?" for _ in parent_ids)
                group_rows = await (await conn.execute(
                    f"SELECT id FROM trade_groups "
                    f"WHERE parent_id IN ({marks})",
                    parent_ids,
                )).fetchall()
                group_ids = [row["id"] for row in group_rows]

                await conn.execute(
                    f"DELETE FROM hedge_actions WHERE parent_id IN ({marks})",
                    parent_ids,
                )
                await conn.execute(
                    f"DELETE FROM hedge_state WHERE parent_id IN ({marks})",
                    parent_ids,
                )
                await conn.execute(
                    f"DELETE FROM fills WHERE parent_id IN ({marks})",
                    parent_ids,
                )
                await conn.execute(
                    f"DELETE FROM legs WHERE parent_id IN ({marks})",
                    parent_ids,
                )
                await conn.execute(
                    f"DELETE FROM events WHERE parent_id IN ({marks})",
                    parent_ids,
                )
                if group_ids:
                    group_marks = ",".join("?" for _ in group_ids)
                    await conn.execute(
                        f"DELETE FROM events WHERE group_id IN ({group_marks})",
                        group_ids,
                    )
                await conn.execute(
                    f"DELETE FROM trade_groups WHERE parent_id IN ({marks})",
                    parent_ids,
                )
                await conn.execute(
                    f"DELETE FROM parent_trades WHERE id IN ({marks})",
                    parent_ids,
                )

            if signal_ids:
                marks = ",".join("?" for _ in signal_ids)
                await conn.execute(
                    f"DELETE FROM events "
                    f"WHERE json_valid(payload_json) "
                    f"AND json_extract(payload_json, '$.signal_id') IN ({marks})",
                    signal_ids,
                )
                await conn.execute(
                    f"DELETE FROM signals WHERE signal_id IN ({marks})",
                    signal_ids,
                )
            heartbeat_cursor = await conn.execute(
                "DELETE FROM pine_heartbeats WHERE strategy=?",
                (strategy,),
            )
            await conn.execute(
                "DELETE FROM system_state WHERE key='pine_status' "
                "AND json_valid(value) "
                "AND json_extract(value, '$.strategy')=?",
                (strategy,),
            )
            await conn.commit()
        except HTTPException:
            raise
        except Exception:
            await conn.rollback()
            raise

    return {
        "deleted": True,
        "strategy": strategy,
        "signals": len(signal_ids),
        "trades": len(parent_ids),
        "groups": len(group_ids),
        "heartbeats": heartbeat_cursor.rowcount,
    }


@router.get("/api/tradingview_setup")
async def tradingview_setup(request: Request, response: Response):
    """实验阶段的 TradingView 配置向导，包含可直接复制的 Webhook 口令。"""
    ctx = _auth(request)
    response.headers["Cache-Control"] = "no-store"
    return {
        "schema_version": "2",
        "webhook_path": "/webhook/signal",
        "legacy_webhook_path": "/webhook/tradingview",
        "account_name": ctx.cfg.env.trading_account_name,
        "strategy": ctx.cfg.strategy.name,
        "passphrase": ctx.cfg.env.webhook_auth_token,
        "passphrase_configured": bool(ctx.cfg.env.webhook_auth_token),
        "condition": "策略 → 订单成交",
        "message_mode": "generic_position_transition",
    }


@router.get("/api/entry_conditions")
async def entry_conditions(request: Request, refresh: bool = False):
    """开仓条件看板：后端诊断值 + Pine/TradingView 唯一策略真值。

    绝不抛异常 —— 前端是 Promise.all 并发刷新，这里 500 会连带打掉整个面板。
    后端诊断值不参与开平仓；行情不可用时返回 backend=None + error，看板单独降级。
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

    # 实时价格：每次请求单独取一次（轻量单调用），不受上面 2h 门槛快照 TTL 节流，
    # 让看板价格与「距轨道」随行情实时跳动——这是真正的实时数据源（后端自有 Deribit 流）。
    live = None
    try:
        from .exchange import market_data
        price = await market_data.get_index_price(ctx.ws, ctx.cfg.strategy.currency)
        shape = ((backend or {}).get("checks") or {}).get("shape") or {}
        upper, lower = shape.get("upper"), shape.get("lower")
        live = {
            "price": price,
            "upper": upper,
            "lower": lower,
            # 正 = 还需向上/向下移动多少 % 才触及轨道；负 = 已在轨道外侧
            "to_upper_pct": (upper - price) / price * 100 if upper else None,
            "to_lower_pct": (price - lower) / price * 100 if lower else None,
            "ts": int(time.time() * 1000),
        }
    except Exception as exc:  # noqa: BLE001 - 看板降级，绝不影响交易链路
        live = {"error": str(exc)[:120]}

    # 推送看门狗：Pine 自报满足全部条件时，核对是否真的收到了对应开仓推送（读，不落事件）
    try:
        watchdog = await entry_conditions_mod.push_watchdog(ctx)
    except Exception as exc:  # noqa: BLE001 - 看板降级，绝不影响交易链路
        watchdog = {"armed": False, "reason": f"error: {str(exc)[:80]}"}

    raw = await ctx.repo.get_state(webhook.PINE_STATUS_KEY)
    try:
        pine = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        pine = None
    return {
        "backend": backend,
        "backend_error": error,
        "live": live,
        "watchdog": watchdog,
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
        SELECT p.id, p.signal_id, p.trade_id, s.strategy,
               p.spread_type, p.state, p.qty_total,
               p.opened_at, p.closed_at, p.close_reason,
               p.open_liquidity_role, p.close_liquidity_role,
               COALESCE(SUM(g.realized_pnl_btc), 0) AS realized_pnl_btc,
               COALESCE(SUM(CASE WHEN g.state='OPEN' THEN g.unrealized_pnl_btc ELSE 0 END), 0) AS unrealized_pnl_btc
        FROM parent_trades p
        LEFT JOIN signals s ON s.signal_id=p.signal_id
        LEFT JOIN trade_groups g ON g.parent_id=p.id
        GROUP BY p.id
        ORDER BY p.id DESC
        LIMIT 200
        """
    )
    trades = [dict(row) for row in await trade_cur.fetchall()]
    for trade in trades:
        # parent_trades.trade_id stores the TradingView Alert identity.
        # Expose the domain name explicitly while keeping trade_id compatible.
        trade["tv_id"] = trade.get("trade_id")
    completed = [t for t in trades if t["state"] in ("CLOSED", "FORCE_CLOSED")]
    wins = sum(float(t["realized_pnl_btc"]) > 0 for t in completed)
    losses = sum(float(t["realized_pnl_btc"]) < 0 for t in completed)
    decided = wins + losses
    cumulative_realized = 0.0
    curve = []
    for trade in sorted(
        (t for t in completed if t.get("closed_at") is not None),
        key=lambda item: (item["closed_at"], item["id"]),
    ):
        trade_pnl = float(trade["realized_pnl_btc"])
        cumulative_realized += trade_pnl
        curve.append({
            "trade_id": trade["id"],
            "closed_at": trade["closed_at"],
            "strategy": trade.get("strategy"),
            "spread_type": trade["spread_type"],
            "realized_pnl_btc": trade_pnl,
            "cumulative_realized_pnl_btc": cumulative_realized,
        })

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
        "curve": curve,
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
    # An explicit operator choice always supersedes an older automatic
    # recovery marker; otherwise a later scan could undo a manual pause.
    await ctx.repo.set_state(risk_mod.AUTO_RESUME_STATE_KEY, "")
    await ctx.repo.set_state(risk_mod.PAUSE_REASON_KEY, "")
    return {"trading_enabled": enabled}


@router.post("/control/force_close")
async def manual_force_close(request: Request, reason: str = "manual"):
    """人工强平（规范第九节）：立即打断执行，再入队最高优先级命令。"""
    ctx = _auth(request)
    await ctx.repo.set_flag("trading_enabled", False)
    await ctx.repo.set_flag("force_close_active", False)
    await ctx.repo.set_state(risk_mod.AUTO_RESUME_STATE_KEY, "")
    await ctx.repo.set_state(risk_mod.PAUSE_REASON_KEY, reason)
    interrupted = ctx.request_force_close(source="MANUAL", reason=reason)
    await ev.emit(ctx.repo.conn, ev.FORCE_CLOSE_MANUAL, level="WARN", reason=reason)
    return {"status": "queued", "interrupted_execution": interrupted}


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

