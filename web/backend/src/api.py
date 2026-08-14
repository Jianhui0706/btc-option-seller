"""状态查询与手动控制端点（人工强平入口，规范第九节 source=MANUAL）。"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import time

import aiosqlite
from fastapi import APIRouter, HTTPException, Request, Response

from . import runtime_settings, webhook
from .config import account_scope_key
from .context import Ctx
from .engine import entry_conditions as entry_conditions_mod
from .engine import hedge as hedge_mod
from .engine import risk as risk_mod
from .exchange import futures, market_data
from .monitor import events as ev

router = APIRouter()


def _auth(request: Request) -> Ctx:
    return request.app.state.ctx


@router.get("/status")
async def status(request: Request):
    ctx = _auth(request)
    repo = ctx.repo
    parents = await repo.active_parents()
    residual_reader = getattr(repo, "residual_parents", None)
    if callable(residual_reader):
        known = {parent["id"] for parent in parents}
        parents.extend(
            parent for parent in await residual_reader()
            if parent["id"] not in known)
    detail = []
    for p in parents:
        hedge_row = await repo.get_hedge(p["id"])
        params = await hedge_mod.trade_hedge_params(ctx, p)
        event_reader = getattr(repo, "recent_hedge_events", None)
        hedge_events = (await event_reader(p["id"]) if callable(event_reader)
                        else [])
        parent_snapshots = getattr(ctx, "hedge_snapshots", {})
        diagnostic = hedge_mod.hedge_diagnostic(
            ctx, p, hedge_row,
            parent_snapshots.get(p["id"], getattr(ctx, "hedge_snapshot", None)),
            params)
        diagnostic["audit_events"] = hedge_events
        detail.append({
            "parent": p,
            "groups": await repo.groups_of(p["id"]),
            "hedge": hedge_row,
            "hedge_diagnostic": diagnostic,
            "hedge_events": hedge_events,
        })
    stored_last_scan = await repo.get_state("last_scan_ts")
    last_scan_ts = (
        ctx.scan_last_completed_ts
        or (int(stored_last_scan) if stored_last_scan else None)
    )
    stored_healthy_scan = await repo.get_state("last_healthy_scan_ts")
    last_healthy_ts = (
        getattr(ctx, "scan_last_healthy_ts", None)
        or (int(stored_healthy_scan) if stored_healthy_scan else None)
        or last_scan_ts
    )
    error_is_latest = bool(
        ctx.scan_last_error
        and (ctx.scan_last_error_ts or 0) > (last_scan_ts or 0)
    )
    scan_state = (
        "error" if error_is_latest
        else "running" if ctx.scan_running
        else "degraded" if getattr(ctx, "scan_last_warning", None)
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
            "last_healthy_ts": last_healthy_ts,
            "last_error": ctx.scan_last_error if error_is_latest else None,
            "last_error_kind": (
                ctx.scan_last_error_kind if error_is_latest else None
            ),
            "last_error_ts": ctx.scan_last_error_ts if error_is_latest else None,
            "last_warning": getattr(ctx, "scan_last_warning", None),
            "last_warning_kind": getattr(ctx, "scan_last_warning_kind", None),
            "last_warning_ts": getattr(ctx, "scan_last_warning_ts", None),
            "quote_issues": getattr(ctx, "scan_quote_issues", []),
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
    residual_reader = getattr(ctx.repo, "residual_parents", None)
    residuals = await residual_reader() if callable(residual_reader) else []
    quarantined_instruments = {
        parent.get("buy_instrument") for parent in residuals
        if parent.get("buy_instrument")
    }
    active = await ctx.repo.active_parents()
    decision_by_instrument = {
        parent.get("buy_instrument"): parent["id"] for parent in active
        if parent.get("close_reason") == risk_mod.RESIDUAL_DECISION_REASON
        and parent.get("buy_instrument")
    }
    residual_parent_by_instrument = {
        parent.get("buy_instrument"): parent["id"] for parent in residuals
        if parent.get("buy_instrument")
    }
    return {
        "equity_btc": summ.equity_btc,
        "available_funds": summ.available_funds,
        "margin_ratio": summ.margin_ratio,
        "index_price": idx,
        "option_positions": [{
            "instrument": p.instrument, "size": p.size, "delta": p.delta,
            "mark_price": p.mark_price, "avg_price": p.average_price,
            "pnl": p.floating_pnl,
            "quarantined": p.instrument in quarantined_instruments,
            "residual_parent_id": (
                decision_by_instrument.get(p.instrument)
                or residual_parent_by_instrument.get(p.instrument)),
            "residual_decision_required": p.instrument in decision_by_instrument}
            for p in opts],
        "net_option_delta": sum(p.delta for p in opts),
        "perp_usd": perp_usd,
        "perp_delta": perp_delta,
        "open_orders": [{
            "instrument": o["instrument_name"], "direction": o["direction"],
            "amount": o["amount"], "price": o.get("price"),
            "label": o.get("label", ""),
            "quarantined": o["instrument_name"] in quarantined_instruments}
            for o in orders],
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

@router.get("/trades/{parent_id}/execution_attempts")
async def trade_execution_attempts(request: Request, parent_id: int):
    ctx = _auth(request)
    parent = await ctx.repo.get_parent(parent_id)
    if not parent:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="trade not found")
    return await ctx.repo.execution_attempts(parent_id)


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
        "received_at, status, reject_reason, reject_detail FROM signals "
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
        "WHERE p.state NOT IN ('CLOSED', 'FORCE_CLOSED', 'SETTLED', 'OPEN_FAILED') "
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
                  AND p.state NOT IN ('CLOSED', 'FORCE_CLOSED', 'SETTLED', 'OPEN_FAILED')
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
               p.opened_at, p.closed_at, p.close_reason, p.settlement_price,
               p.open_liquidity_role, p.close_liquidity_role,
               COALESCE(SUM(g.realized_pnl_btc), 0) AS realized_pnl_btc,
               COALESCE(SUM(CASE WHEN g.state='OPEN' THEN g.unrealized_pnl_btc ELSE 0 END), 0) AS unrealized_pnl_btc
        FROM parent_trades p
        LEFT JOIN signals s ON s.signal_id=p.signal_id
        LEFT JOIN trade_groups g ON g.parent_id=p.id
        WHERE p.state <> 'OPEN_FAILED'
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
    completed_cur = await conn.execute(
        """
        SELECT p.id, s.strategy, p.spread_type, p.closed_at,
               COALESCE(SUM(g.realized_pnl_btc), 0) AS realized_pnl_btc
        FROM parent_trades p
        LEFT JOIN signals s ON s.signal_id=p.signal_id
        LEFT JOIN trade_groups g ON g.parent_id=p.id
        WHERE p.state IN ('CLOSED', 'FORCE_CLOSED', 'SETTLED')
        GROUP BY p.id
        ORDER BY p.closed_at, p.id
        """
    )
    # Performance statistics and the cumulative curve must use the complete
    # terminal-trade history.  The 200-row limit above belongs only to the
    # history table; filtering that limited page can hide every completed trade
    # when many newer OPEN_FAILED attempts exist.
    completed = [dict(row) for row in await completed_cur.fetchall()]
    wins = sum(float(t["realized_pnl_btc"]) > 0 for t in completed)
    losses = sum(float(t["realized_pnl_btc"]) < 0 for t in completed)
    decided = wins + losses
    cumulative_realized = 0.0
    curve = []
    for trade in (t for t in completed if t.get("closed_at") is not None):
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
    env = getattr(getattr(ctx, "cfg", None), "env", None)
    account_view = {
        "name": getattr(env, "trading_account_name", "") if env else "",
        "environment": (
            "TESTNET" if env and env.is_testnet else
            "MAINNET" if env else "UNKNOWN"
        ),
        "scope": account_scope_key(env) if env else "legacy-unscoped",
    }
    return {
        "account": account_view,
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


@router.get("/api/hedge_ledger")
async def hedge_ledger(request: Request, days: int = 14, limit: int = 200):
    """Account-level hedge execution ledger with trigger-trade attribution."""
    ctx = _auth(request)
    days = max(1, min(int(days), 365))
    limit = max(1, min(int(limit), 500))
    cutoff = int((time.time() - days * 86400) * 1000)
    cur = await ctx.repo.conn.execute(
        """
        SELECT h.*, p.spread_type, p.signal_id, s.strategy
        FROM hedge_actions h
        LEFT JOIN parent_trades p ON p.id=h.parent_id
        LEFT JOIN signals s ON s.signal_id=p.signal_id
        ORDER BY h.ts, h.id
        """
    )
    rows = [dict(row) for row in await cur.fetchall()]
    managed_by_parent: dict[int | None, float] = {}
    actions = []
    for row in rows:
        parent_id = row.get("parent_id")
        actual = float(row.get("order_usd") or 0.0)
        managed_by_parent[parent_id] = managed_by_parent.get(parent_id, 0.0) + actual
        managed_after = row.get("managed_usd_after")
        if managed_after is None:
            managed_after = managed_by_parent[parent_id]
        else:
            managed_by_parent[parent_id] = float(managed_after)
        if int(row["ts"]) < cutoff:
            continue
        requested = row.get("requested_usd")
        ratio = None
        if requested is not None and abs(float(requested)) > 1e-9:
            ratio = min(1.0, abs(actual) / abs(float(requested)))
        if row["action"] == "SKIP":
            result = "SKIPPED"
        elif requested is None:
            result = "RECORDED"
        elif abs(actual) < 1e-9:
            result = "NO_FILL"
        elif ratio is not None and ratio < 0.999:
            result = "PARTIAL"
        else:
            result = "FILLED"
        try:
            details = json.loads(row.get("details_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            details = {}
        actions.append({
            **row,
            "filled_usd": actual,
            "managed_usd_after": float(managed_after),
            "fill_ratio": ratio,
            "result": result,
            "manual": bool(details.get("manual")),
            "liquidity_role": details.get("liquidity_role"),
            "exchange_order_id": details.get("exchange_order_id"),
            "exchange_trade_ids": details.get("exchange_trade_ids") or [],
            "execution_groups": details.get("groups") or [],
        })

    visible = actions[-limit:]
    executions = [row for row in actions
                  if row["action"] in ("REBALANCE", "RELEASE", "CLOSE")]
    matched = [row for row in executions if row.get("requested_usd") is not None]
    requested_total = sum(abs(float(row["requested_usd"])) for row in matched)
    matched_filled = sum(abs(float(row["filled_usd"])) for row in matched)

    # Deribit-like order/attempt ledger.  Manual Maker-first cycles have one
    # row per group attempt; automatic IOC actions are represented by a
    # synthetic one-row attempt because older code only persisted the action.
    attempts_cur = await ctx.repo.conn.execute(
        """
        SELECT e.*, p.spread_type, s.strategy
        FROM execution_attempts e
        LEFT JOIN parent_trades p ON p.id=e.parent_id
        LEFT JOIN signals s ON s.signal_id=p.signal_id
        WHERE e.phase='hedge'
          AND (e.recorded_at IS NULL OR e.recorded_at>=?)
        ORDER BY e.id
        """,
        (cutoff,),
    )
    attempt_rows = [dict(row) for row in await attempts_cur.fetchall()]
    detailed_records = []
    for row in attempt_rows:
        try:
            attempt_details = json.loads(row.get("details_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            attempt_details = {}
        direction = str(row.get("direction") or "").lower()
        sign = 1.0 if direction == "buy" else -1.0
        detailed_records.append({
            "record_type": "ORDER_ATTEMPT",
            "record_id": f"attempt-{row['id']}",
            "sequence_id": int(row["id"]),
            "ts": row.get("placed_at") or row.get("recorded_at"),
            "parent_id": row.get("parent_id"),
            "strategy": row.get("strategy"),
            "spread_type": row.get("spread_type"),
            "action": attempt_details.get("hedge_action"),
            "cycle_id": row.get("cycle_id") or
                        attempt_details.get("hedge_cycle_id"),
            "cycle_attempt_no": row.get("cycle_attempt_no") or
                                attempt_details.get("cycle_attempt_no"),
            "cycle_max_attempts": row.get("cycle_max_attempts") or
                                  attempt_details.get("cycle_max_attempts"),
            "instrument": row.get("instrument") or "BTC-PERPETUAL",
            "operation": row.get("operation"),
            "direction": direction,
            "order_type": "LIMIT",
            "time_in_force": (
                "POST_ONLY" if row.get("liquidity_role") == "MAKER" else "IOC"),
            "liquidity_role": row.get("liquidity_role") or "UNKNOWN",
            "group_index": row.get("group_index") or
                           attempt_details.get("group_index"),
            "group_count": row.get("group_count") or
                           attempt_details.get("group_count") or 5,
            "group_attempt_no": row.get("attempt_no"),
            "group_round_no": row.get("round_no"),
            "requested_usd": sign * float(row.get("requested_qty") or 0.0),
            "filled_usd": sign * float(row.get("filled_qty") or 0.0),
            "limit_price": row.get("limit_price"),
            "fill_price": row.get("avg_fill_price"),
            "status": row.get("status") or "UNKNOWN",
            "duration_ms": row.get("duration_ms"),
            "exchange_order_id": row.get("exchange_order_id") or
                                 attempt_details.get("exchange_order_id"),
            "exchange_trade_ids": attempt_details.get("exchange_trade_ids") or [],
            "initial_target_usd": attempt_details.get("initial_target_usd"),
            "initial_group_usd": attempt_details.get("initial_group_usd"),
            "live_target_usd": attempt_details.get("live_target_usd"),
            "live_group_usd": attempt_details.get("live_group_usd"),
            "incremental_fill": bool(
                attempt_details.get("fill_qty_is_incremental")),
        })

    # Attribute legacy manual attempts (which predate cycle/timestamp columns)
    # to their matching manual action, preserving their database order.
    legacy_chunks: list[list[dict]] = []
    current_chunk: list[dict] = []
    for record in (item for item in detailed_records if not item["cycle_id"]):
        previous = current_chunk[-1] if current_chunk else None
        starts_new = bool(previous and (
            record["parent_id"] != previous["parent_id"] or
            record["direction"] != previous["direction"] or
            int(record.get("group_index") or 0) <
            int(previous.get("group_index") or 0)))
        if starts_new:
            legacy_chunks.append(current_chunk)
            current_chunk = []
        current_chunk.append(record)
    if current_chunk:
        legacy_chunks.append(current_chunk)
    available_manual = [row for row in actions if row["manual"]]
    used_manual_ids: set[int] = set()
    for chunk in legacy_chunks:
        first = chunk[0]
        # Before this fix, Maker attempt rows stored the group's cumulative
        # filled amount.  Convert those legacy values back to per-attempt fills
        # so 29,610 + 2,080 is not displayed as 29,610 + 31,690.
        cumulative_by_group: dict[int, float] = {}
        for record in chunk:
            group_index = int(record.get("group_index") or 0)
            if (record.get("liquidity_role") == "MAKER"
                    and record.get("operation") == "manual_perp"
                    and not record.get("incremental_fill")):
                cumulative = abs(float(record.get("filled_usd") or 0.0))
                previous = cumulative_by_group.get(group_index, 0.0)
                incremental = max(0.0, cumulative - previous)
                cumulative_by_group[group_index] = max(previous, cumulative)
                record["filled_usd"] = (
                    incremental if record.get("direction") == "buy"
                    else -incremental)
        expected_positive = first["direction"] == "buy"
        match = next((row for row in available_manual
                      if row["id"] not in used_manual_ids
                      and row.get("parent_id") == first.get("parent_id")
                      and (float(row.get("filled_usd") or 0.0) > 0)
                      == expected_positive), None)
        if not match:
            continue
        used_manual_ids.add(match["id"])
        for record in chunk:
            record.update(
                ts=match["ts"], action=match["action"],
                cycle_id=match.get("cycle_id") or f"legacy-action-{match['id']}",
                cycle_attempt_no=match.get("attempt_no") or 1,
                cycle_max_attempts=match.get("max_attempts") or 1)

    # Normalize legacy status values after converting cumulative Maker fills.
    for record in detailed_records:
        requested_abs = abs(float(record.get("requested_usd") or 0.0))
        filled_abs = abs(float(record.get("filled_usd") or 0.0))
        if filled_abs > 1e-9:
            record["status"] = (
                "FILLED" if filled_abs + 1e-9 >= requested_abs else "PARTIAL")
        elif record.get("status") in ("FILLED", "PARTIAL"):
            record["status"] = "CANCELED"

    synthetic_records = []
    for row in executions:
        if row["manual"]:
            continue
        filled = float(row.get("filled_usd") or 0.0)
        requested = float(row.get("requested_usd") or filled)
        synthetic_records.append({
            "record_type": "AUTO_IOC",
            "record_id": f"action-{row['id']}",
            "sequence_id": 1_000_000_000 + int(row["id"]),
            "ts": row["ts"],
            "parent_id": row.get("parent_id"),
            "strategy": row.get("strategy"),
            "spread_type": row.get("spread_type"),
            "action": row["action"],
            "cycle_id": row.get("cycle_id"),
            "cycle_attempt_no": row.get("attempt_no"),
            "cycle_max_attempts": row.get("max_attempts"),
            "instrument": "BTC-PERPETUAL",
            "operation": "automatic_perp_ioc",
            "direction": "buy" if (filled or requested) > 0 else "sell",
            "order_type": "LIMIT",
            "time_in_force": "IOC",
            "liquidity_role": "TAKER" if abs(filled) > 1e-9 else "UNKNOWN",
            "group_index": None,
            "group_count": None,
            "group_attempt_no": None,
            "group_round_no": None,
            "requested_usd": requested,
            "filled_usd": filled,
            "limit_price": None,
            "fill_price": row.get("fill_price"),
            "status": row["result"],
            "duration_ms": None,
            "exchange_order_id": row.get("exchange_order_id"),
            "exchange_trade_ids": row.get("exchange_trade_ids") or [],
            "initial_target_usd": None,
            "initial_group_usd": None,
            "live_target_usd": None,
            "live_group_usd": None,
            "incremental_fill": True,
        })
    execution_records = detailed_records + synthetic_records
    execution_records.sort(
        key=lambda row: (int(row.get("ts") or 0), int(row["sequence_id"])),
        reverse=True)
    execution_records = execution_records[:max(100, min(limit * 5, 1000))]
    return {
        "range_days": days,
        "summary": {
            "executions": len(executions),
            "requested_usd": requested_total,
            "filled_usd": matched_filled,
            "fill_rate": (matched_filled / requested_total
                          if requested_total > 1e-9 else None),
            "partial_fills": sum(row["result"] == "PARTIAL" for row in executions),
            "skips": sum(row["action"] == "SKIP" for row in actions),
            "fees_btc": sum(float(row.get("fee") or 0.0) for row in executions),
        },
        "actions": list(reversed(visible)),
        "execution_records": execution_records,
    }


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


@router.post("/control/residual/retry")
async def retry_residual_close(request: Request, parent_id: int):
    """Operator explicitly asks the engine to retry closing a long residue."""
    ctx = _auth(request)
    parent = await ctx.repo.get_parent(parent_id)
    if not parent or parent["state"] not in ("CLOSING", "RESIDUAL_LONG"):
        raise HTTPException(status_code=409, detail="residual position is not actionable")
    if parent["state"] == "RESIDUAL_LONG":
        await ctx.repo.transition_parent(
            parent_id, "CLOSING", ev.RESIDUAL_LONG_RETRY_REQUESTED,
            source="MANUAL")
    else:
        await ev.emit(ctx.repo.conn, ev.RESIDUAL_LONG_RETRY_REQUESTED,
                      parent_id=parent_id, source="MANUAL")
    await ctx.repo.set_close_reason(parent_id, "MANUAL_RESIDUAL_RETRY")
    ctx.queue.put(ctx.queue.normal_close(parent_id, "MANUAL_RESIDUAL_RETRY"))
    return {"status": "queued", "parent_id": parent_id}


@router.post("/control/residual/quarantine")
async def quarantine_residual(request: Request, parent_id: int):
    """Operator explicitly accepts leaving a protective long until fill/expiry."""
    ctx = _auth(request)
    positions = await account.get_option_positions(
        ctx.ws, ctx.cfg.strategy.currency)
    ids = await risk_mod.quarantine_residual_long_parents(
        ctx, positions, source="MANUAL", reason="OPERATOR_CONFIRMED",
        parent_id=parent_id)
    if parent_id not in ids:
        raise HTTPException(status_code=409, detail="no long-only residue found")
    return {"status": "quarantined", "parent_id": parent_id}


@router.post("/control/manual_hedge")
async def manual_hedge(request: Request):
    """Testnet-only hedge test: neutralize now, then allow automatic release.

    The command bypasses only the price-activation gate. It still uses the
    existing serialized queue, position reconciliation, and strategy ownership
    accounting; its operator-triggered order uses the grouped Maker-first path.
    Once filled, the normal beta recovery line and full-bar confirmation own the
    release, so the operator can exercise the complete open-to-auto-close path.
    """
    ctx = _auth(request)
    if not ctx.cfg.env.is_testnet:
        raise HTTPException(
            status_code=403, detail="一键对冲仅允许在 Testnet 使用")
    if ctx.hedge_frozen or await ctx.force_close_active():
        raise HTTPException(
            status_code=409, detail="平仓流程正在占用执行通道，暂不能对冲")
    task = getattr(ctx, "current_exec_task", None)
    if getattr(ctx, "current_command_kind", None) or (
            task is not None and not task.done()):
        raise HTTPException(
            status_code=409, detail="策略引擎正在执行其他交易，请稍后再试")
    if bool(getattr(ctx.queue, "hedge_release_in_flight", False)):
        raise HTTPException(
            status_code=409,
            detail="关对冲已在队列中；必须完成归零后才能重新开对冲")

    parents = await ctx.repo.open_parents()
    if not parents:
        raise HTTPException(
            status_code=409, detail="当前没有可对冲的活跃价差仓位")

    active_parents = await ctx.repo.active_parents()
    hedge_rows = {}
    managed_usd = 0.0
    for parent in active_parents:
        row = await ctx.repo.get_hedge(parent["id"])
        if row:
            hedge_rows[parent["id"]] = row
            managed_usd += float(row.get("futures_usd") or 0.0)
    actual_usd, _ = await futures.current_perp_position(
        ctx.ws, ctx.cfg.hedge.perp_instrument)
    lot = float(ctx.cfg.hedge.perp_contract_usd)
    if abs(actual_usd - managed_usd) >= lot / 2:
        raise HTTPException(
            status_code=409,
            detail=(f"Deribit 实际永续 {actual_usd:+.0f} USD 与系统归属 "
                    f"{managed_usd:+.0f} USD 不一致，请先核对仓位"))

    allowed = {"HEDGE_INACTIVE", "HEDGE_ACTIVE", "HEDGE_DEGRADED"}
    for parent in parents:
        row = hedge_rows.get(parent["id"])
        if not row:
            await ctx.repo.ensure_hedge_row(parent["id"])
            row = await ctx.repo.get_hedge(parent["id"])
            hedge_rows[parent["id"]] = row
        state = (row or {}).get("state")
        if state in {"HEDGE_RELEASE_PENDING", "HEDGE_CLOSING"}:
            raise HTTPException(
                status_code=409,
                detail=(f"仓位 #{parent['id']} 正在关对冲；必须完成归零后"
                        "才能重新开对冲"))
        if state not in allowed:
            raise HTTPException(
                status_code=409,
                detail=f"仓位 #{parent['id']} 的对冲状态 {state} 正在处理中")

    activated = []
    for parent in parents:
        state = hedge_rows[parent["id"]]["state"]
        if state == "HEDGE_INACTIVE":
            await ctx.repo.transition_hedge(
                parent["id"], "HEDGE_ACTIVE", ev.STATE_TRANSITION,
                reason="MANUAL_HEDGE")
            activated.append(parent["id"])

    # 旧版一键对冲会留下 manual_hold=1 并永久阻止自动撤销。新版测试模式
    # 明确清掉该标志：建仓仍走人工五批 Maker-first，但成交后立刻交回自动
    # β 撤销状态机管理；“取消对冲”按钮继续作为随时人工归零的兜底。
    hold_setter = getattr(ctx.repo, "set_hedge_manual_hold", None)
    if callable(hold_setter):
        for parent in parents:
            await hold_setter(parent["id"], False)

    spread_types = sorted({parent["spread_type"] for parent in parents})
    view = await hedge_mod.portfolio_hedge_view(
        ctx, parents=parents, spread_types=set(spread_types))
    if view is None:
        raise HTTPException(
            status_code=409, detail="当前策略没有可计算的期权 Delta")
    order_usd = hedge_mod.contract_usd(
        -view.net_delta * view.index_price, lot)
    if abs(order_usd) < lot - 1e-9:
        await ev.emit(
            ctx.repo.conn, ev.MANUAL_HEDGE_REQUESTED,
            parent_id=view.controller_parent_id, status="already_neutral",
            net_delta=view.net_delta, estimated_order_usd=0.0)
        return {
            "status": "already_neutral",
            "net_delta": view.net_delta,
            "estimated_order_usd": 0.0,
            "activated_parent_ids": activated,
            "auto_release": True,
        }

    cmd = ctx.queue.hedge_rebalance(
        view.controller_parent_id,
        order_usd / view.index_price,
        view.net_delta,
        spread_types=spread_types)
    cmd.payload["manual"] = True
    queued = ctx.queue.put(cmd)
    await ev.emit(
        ctx.repo.conn, ev.MANUAL_HEDGE_REQUESTED,
        parent_id=view.controller_parent_id,
        status="queued" if queued else "already_queued",
        net_delta=view.net_delta, estimated_order_usd=order_usd,
        spread_types=spread_types, activated_parent_ids=activated,
        auto_release=True)
    return {
        "status": "queued" if queued else "already_queued",
        "net_delta": view.net_delta,
        "estimated_order_usd": order_usd,
        "activated_parent_ids": activated,
        "auto_release": True,
    }


@router.post("/control/manual_hedge_release")
async def manual_hedge_release(request: Request):
    """Testnet-only operator action: close only system-owned perpetual hedge."""
    ctx = _auth(request)
    if not ctx.cfg.env.is_testnet:
        raise HTTPException(
            status_code=403, detail="一键取消对冲仅允许在 Testnet 使用")
    if ctx.hedge_frozen or await ctx.force_close_active():
        raise HTTPException(
            status_code=409, detail="平仓流程正在占用执行通道，暂不能取消对冲")
    task = getattr(ctx, "current_exec_task", None)
    if getattr(ctx, "current_command_kind", None) or (
            task is not None and not task.done()):
        raise HTTPException(
            status_code=409, detail="策略引擎正在执行其他交易，请稍后再试")

    parents = await ctx.repo.active_parents()
    managed_rows: list[tuple[dict, dict]] = []
    managed_usd = 0.0
    for parent in parents:
        row = await ctx.repo.get_hedge(parent["id"])
        amount = float((row or {}).get("futures_usd") or 0.0)
        managed_usd += amount
        if abs(amount) >= float(ctx.cfg.hedge.perp_contract_usd) / 2:
            managed_rows.append((parent, row))

    actual_usd, _ = await futures.current_perp_position(
        ctx.ws, ctx.cfg.hedge.perp_instrument)
    lot = float(ctx.cfg.hedge.perp_contract_usd)
    if not managed_rows:
        detail = "当前没有系统管理的永续对冲仓位"
        if abs(actual_usd) >= lot / 2:
            detail += "；现有永续不属于策略，一键取消不会处理"
        raise HTTPException(status_code=409, detail=detail)
    if abs(actual_usd - managed_usd) >= lot / 2:
        raise HTTPException(
            status_code=409,
            detail=(f"Deribit 实际永续 {actual_usd:+.0f} USD 与系统归属 "
                    f"{managed_usd:+.0f} USD 不一致，请先核对仓位"))

    allowed = {"HEDGE_ACTIVE", "HEDGE_DEGRADED", "HEDGE_RELEASE_PENDING"}
    for parent, row in managed_rows:
        state = row.get("state")
        if state not in allowed:
            raise HTTPException(
                status_code=409,
                detail=f"仓位 #{parent['id']} 的对冲状态 {state} 不能人工取消")

    queued_ids = []
    already_queued_ids = []
    for parent, row in managed_rows:
        hold_setter = getattr(ctx.repo, "set_hedge_manual_hold", None)
        if callable(hold_setter):
            await hold_setter(parent["id"], False)
        if row["state"] != "HEDGE_RELEASE_PENDING":
            await ctx.repo.transition_hedge(
                parent["id"], "HEDGE_RELEASE_PENDING",
                ev.HEDGE_RELEASE_PENDING, reason="MANUAL_HEDGE_RELEASE")
        command = ctx.queue.hedge_release(parent["id"], manual=True)
        if ctx.queue.put(command):
            queued_ids.append(parent["id"])
        else:
            already_queued_ids.append(parent["id"])

    status = "queued" if queued_ids else "already_queued"
    await ev.emit(
        ctx.repo.conn, ev.MANUAL_HEDGE_RELEASE_REQUESTED,
        status=status, managed_usd=managed_usd,
        parent_ids=[parent["id"] for parent, _ in managed_rows],
        queued_parent_ids=queued_ids)
    return {
        "status": status,
        "managed_usd": managed_usd,
        "queued_parent_ids": queued_ids,
        "already_queued_parent_ids": already_queued_ids,
    }


@router.post("/control/clear_force_close")
async def clear_force_close(request: Request):
    """人工确认清仓完成后解除 force_close_active（解除后才能重新开仓）。"""
    ctx = _auth(request)
    await ctx.repo.set_flag("force_close_active", False)
    return {"force_close_active": False}


def _expiry_date(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


async def _runtime_settings_view(ctx: Ctx) -> dict:
    override = getattr(ctx, "runtime_bep_activation_progress", None)
    forced = getattr(ctx, "runtime_forced_expiry_ts", None)
    alpha = runtime_settings.effective_progress(ctx)
    beta = runtime_settings.effective_release_recovery_pct(ctx)
    try:
        expiries = await market_data.list_option_expiries(
            ctx.ws, ctx.cfg.strategy.currency)
    except Exception:  # noqa: BLE001 - 到期日列表拉不到不阻塞读取当前设置
        expiries = []
    return {
        "bep_activation_progress": alpha,
        "bep_activation_progress_override": override,
        "bep_activation_progress_default": float(
            ctx.cfg.hedge.bep_activation_progress),
        "forced_expiry_ts": forced,
        "forced_expiry_date": _expiry_date(forced) if forced else None,
        "review_bar_minutes": runtime_settings.effective_review_bar_minutes(ctx),
        "review_bar_minutes_override": getattr(
            ctx, "runtime_review_bar_minutes", None),
        "review_bar_minutes_default": int(getattr(
            ctx.cfg.hedge, "review_bar_minutes", 3)),
        "release_recovery_pct": beta,
        "release_recovery_pct_override": getattr(
            ctx, "runtime_release_recovery_pct", None),
        "release_recovery_pct_default": float(getattr(
            ctx.cfg.hedge, "release_recovery_pct", 0.50)),
        "hysteresis_gap": runtime_settings.hysteresis_gap(alpha, beta),
        "hysteresis_valid": runtime_settings.hysteresis_is_valid(alpha, beta),
        "release_confirmation_bars": (
            runtime_settings.effective_release_confirmation_bars(ctx)),
        "release_confirmation_bars_override": getattr(
            ctx, "runtime_release_confirmation_bars", None),
        "release_confirmation_bars_default": int(getattr(
            ctx.cfg.hedge, "release_confirmation_bars", 1)),
        "hedge_profiles": {
            side: runtime_settings.effective_side_profile(ctx, side)
            for side in runtime_settings.HEDGE_SIDES
        },
        "hedge_profile_overrides": getattr(ctx, "runtime_hedge_profiles", {}) or {},
        "available_expiries": [
            {"ts": e["ts"], "date": _expiry_date(e["ts"]),
             "dte": round(e["dte"], 1)}
            for e in expiries
        ],
    }


@router.get("/api/runtime_settings")
async def get_runtime_settings(request: Request):
    """当前运行时参数 + 可用到期日下拉数据。"""
    return await _runtime_settings_view(_auth(request))


@router.post("/api/runtime_settings")
async def set_runtime_settings(request: Request):
    """设置 / 清空对冲激活系数与强制开仓到期日。

    body 里出现某键才改动它，值为 null/"" 表示清空回退默认；键缺省保持不变。
    """
    ctx = _auth(request)
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")

    # Validate the two price lines as one atomic setting before mutating any
    # persisted value.  α + β must be strictly greater than 1, otherwise the
    # activation price is already inside the release zone.
    if {"bep_activation_progress", "release_recovery_pct"} & set(body):
        raw_alpha = body.get("bep_activation_progress", "__unchanged__")
        raw_beta = body.get("release_recovery_pct", "__unchanged__")
        try:
            alpha = (
                float(ctx.cfg.hedge.bep_activation_progress)
                if raw_alpha in (None, "")
                else runtime_settings.effective_progress(ctx)
                if raw_alpha == "__unchanged__"
                else float(raw_alpha)
            )
            beta = (
                float(getattr(ctx.cfg.hedge, "release_recovery_pct", 0.50))
                if raw_beta in (None, "")
                else runtime_settings.effective_release_recovery_pct(ctx)
                if raw_beta == "__unchanged__"
                else float(raw_beta)
            )
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="α 和 β 必须是数字")
        if (runtime_settings.progress_in_range(alpha)
                and 0.0 <= beta <= 1.0
                and not runtime_settings.hysteresis_is_valid(alpha, beta)):
            raise HTTPException(
                status_code=400,
                detail=("α + β 必须大于 100%，否则启动线与撤销线会重叠；"
                        f"当前为 {(alpha + beta) * 100:.1f}%"))

    if "bep_activation_progress" in body:
        raw = body["bep_activation_progress"]
        if raw is None or raw == "":
            await runtime_settings.set_progress(ctx, None)
        else:
            try:
                value = float(raw)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="激活系数必须是数字")
            if not runtime_settings.progress_in_range(value):
                raise HTTPException(
                    status_code=400, detail="激活系数 α 必须在 [0, 2] 之间")
            await runtime_settings.set_progress(ctx, value)

    if "forced_expiry_ts" in body:
        raw = body["forced_expiry_ts"]
        if raw is None or raw == "":
            await runtime_settings.set_forced_expiry(ctx, None)
        else:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="到期日时间戳必须是整数")
            try:
                expiries = await market_data.list_option_expiries(
                    ctx.ws, ctx.cfg.strategy.currency)
            except Exception:  # noqa: BLE001
                raise HTTPException(status_code=503, detail="暂时无法读取可用到期日")
            if value not in {e["ts"] for e in expiries}:
                raise HTTPException(
                    status_code=400, detail="到期日不在当前挂牌列表中")
            await runtime_settings.set_forced_expiry(ctx, value)

    if "review_bar_minutes" in body:
        raw = body["review_bar_minutes"]
        if raw is None or raw == "":
            await runtime_settings.set_review_bar_minutes(ctx, None)
        else:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="复核 Bar 必须是整数分钟")
            if value not in runtime_settings.REVIEW_BAR_CHOICES:
                raise HTTPException(status_code=400, detail="不支持该复核 Bar 周期")
            await runtime_settings.set_review_bar_minutes(ctx, value)

    if "release_recovery_pct" in body:
        raw = body["release_recovery_pct"]
        if raw is None or raw == "":
            await runtime_settings.set_release_recovery_pct(ctx, None)
        else:
            try:
                value = float(raw)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="撤销恢复比例必须是数字")
            if not 0.0 <= value <= 1.0:
                raise HTTPException(status_code=400, detail="撤销恢复比例必须在 [0,1]")
            await runtime_settings.set_release_recovery_pct(ctx, value)

    if "release_confirmation_bars" in body:
        raw = body["release_confirmation_bars"]
        if raw is None or raw == "":
            await runtime_settings.set_release_confirmation_bars(ctx, None)
        else:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="撤销确认 Bar 数必须是整数")
            if not 1 <= value <= 10:
                raise HTTPException(status_code=400, detail="撤销确认 Bar 数必须在 [1,10]")
            await runtime_settings.set_release_confirmation_bars(ctx, value)

    if "hedge_profiles" in body:
        raw = body["hedge_profiles"]
        if raw is None or raw == "":
            await runtime_settings.set_side_profiles(ctx, None)
        elif not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail="两侧对冲参数必须是对象")
        else:
            clean: dict[str, dict[str, float]] = {}
            for side, values in raw.items():
                if side not in runtime_settings.HEDGE_SIDES or not isinstance(values, dict):
                    raise HTTPException(status_code=400, detail="对冲方向或参数格式无效")
                unknown = set(values) - set(runtime_settings.PROFILE_FIELDS)
                if unknown:
                    raise HTTPException(status_code=400, detail="包含未知对冲参数")
                try:
                    parsed = {key: float(value) for key, value in values.items()}
                except (TypeError, ValueError):
                    raise HTTPException(
                        status_code=400, detail="对冲参数必须全部是数字")
                if ("rebalance_outer_equity_pct" in parsed and
                        not 0 < parsed["rebalance_outer_equity_pct"] <= 0.20):
                    raise HTTPException(status_code=400, detail="Delta 外层比例必须在 (0,0.20]")
                if "min_rebalance_usd" in parsed and parsed["min_rebalance_usd"] < 10:
                    raise HTTPException(status_code=400, detail="最小调仓额不能低于 10 USD")
                if "rebalance_cooldown_sec" in parsed and parsed["rebalance_cooldown_sec"] < 0:
                    raise HTTPException(status_code=400, detail="冷却时间不能为负数")
                if ("emergency_trigger_multiplier" in parsed and
                        parsed["emergency_trigger_multiplier"] < 1):
                    raise HTTPException(status_code=400, detail="紧急倍数不能小于 1")
                clean[side] = parsed
            await runtime_settings.set_side_profiles(ctx, clean)

    return await _runtime_settings_view(ctx)
