"""3 分钟独立扫描（规范第二、十五节）。

run_scheduler: 对齐 3 分钟 bar 边界（+offset 秒）直接执行一次扫描，不占用
TradingView 交易命令队列。扫描产生的平仓/对冲/强平动作仍进入原优先级队列：
强平 > 正常平仓 > 对冲撤销 > 对冲调整 > 新开仓。

这样长时间 Maker/IOC 尝试不会再让健康扫描饿死，同时也不会合并、丢弃或阻止
任何 TradingView 反向信号。
"""

from __future__ import annotations

import asyncio
import logging
import time

from ..context import Ctx
from .. import runtime_settings
from ..exchange import account, market_data
from ..monitor import events as ev
from . import entry_conditions, hedge, risk

log = logging.getLogger("v2.scan")
RISK_ALERT_REPEAT_MS = 15 * 60 * 1000
RESIDUAL_AUTO_RECONCILE_REASON = "RESIDUAL_AUTO_RECONCILE_FLAT"


def flat_residual_decision_parents(
        parents: list[dict], option_positions: list,
        min_lot: float) -> list[dict]:
    """Return decision-waiting parents whose managed option legs are flat.

    A residual decision deliberately suppresses automatic close retries while
    the protective long still exists.  Once Deribit reports both managed
    instruments below the tradable lot, keeping the parent in CLOSING serves
    no safety purpose and only leaves a stale account-wide entry lock.
    """
    sizes = {
        position.instrument: float(position.size)
        for position in option_positions
    }
    flat: list[dict] = []
    threshold = max(0.0, float(min_lot) - 1e-9)
    for parent in parents:
        if (parent.get("state") != "CLOSING"
                or parent.get("close_reason") != risk.RESIDUAL_DECISION_REASON):
            continue
        instruments = {
            parent.get("sell_instrument"), parent.get("buy_instrument")}
        instruments.discard(None)
        if instruments and all(
                abs(sizes.get(instrument, 0.0)) < threshold
                for instrument in instruments):
            flat.append(parent)
    return flat


async def sync_risk_alert_event(ctx: Ctx, breach,
                                *, now_ms: int | None = None) -> bool:
    """Emit a risk reminder on change, then at most once every 15 minutes."""
    repo = ctx.repo
    active_key = "risk_alert_active"
    reason_key = "risk_alert_last_reason"
    ts_key = "risk_alert_last_ts"
    if breach is None:
        if await repo.get_state(active_key) == "1":
            await repo.set_state(active_key, "0")
        return False

    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    reason = str(breach.reason)
    active = await repo.get_state(active_key) == "1"
    previous_reason = await repo.get_state(reason_key)
    previous_ts = int(await repo.get_state(ts_key) or 0)
    should_emit = (
        not active
        or previous_reason != reason
        or now_ms - previous_ts >= RISK_ALERT_REPEAT_MS
    )
    if should_emit:
        auto_pause = bool(ctx.cfg.risk.auto_pause_enabled)
        await ev.emit(
            repo.conn, ev.RISK_ALERT, level="WARN",
            reason=reason,
            auto_pause_enabled=auto_pause,
            response="AUTO_FORCE_CLOSE" if auto_pause else "MONITOR_ONLY",
        )
        await repo.set_state(reason_key, reason)
        await repo.set_state(ts_key, str(now_ms))
    if not active:
        await repo.set_state(active_key, "1")
    return should_emit

async def run_scheduler(ctx: Ctx) -> None:
    offset = ctx.cfg.scan.bar_align_offset_sec
    log.info("scanner scheduler started (review bar runtime-configured, offset=%ss)", offset)
    while True:
        interval = runtime_settings.effective_review_bar_minutes(ctx) * 60
        now = time.time()
        next_fire = (int(now // interval) + 1) * interval + offset
        await asyncio.sleep(max(1.0, next_fire - now))
        await run_scan_cycle(ctx)


async def run_scan_cycle(ctx: Ctx) -> None:
    """运行一次隔离扫描并记录可供 API/页面展示的真实状态。"""
    started_ms = int(time.time() * 1000)
    ctx.scan_running = True
    ctx.scan_last_started_ts = started_ms
    try:
        healthy = await handle_scan(ctx, {})
        if healthy:
            completed_ms = int(time.time() * 1000)
            ctx.scan_last_completed_ts = completed_ms
            ctx.scan_last_error = None
            ctx.scan_last_error_kind = None
            ctx.scan_last_error_ts = None
            if not getattr(ctx, "scan_last_warning", None):
                ctx.scan_last_healthy_ts = completed_ms
                setter = getattr(getattr(ctx, "repo", None), "set_state", None)
                if setter:
                    await setter("last_healthy_scan_ts", str(completed_ms))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 单轮失败不能终止后续定时扫描
        ctx.scan_last_error = str(exc)[:240]
        ctx.scan_last_error_kind = "internal"
        ctx.scan_last_error_ts = int(time.time() * 1000)
        log.exception("independent scan failed")
        try:
            await ev.emit(ctx.repo.conn, ev.SCAN_ERROR, level="ERROR",
                          command="SCAN", error=ctx.scan_last_error)
        except Exception:  # noqa: BLE001
            pass
    finally:
        ctx.scan_running = False


async def _last_completed_review_bar(ctx: Ctx) -> tuple[float, int]:
    """返回最近已收盘复核 Bar 的 (close, bar_start_ts_ms)。"""
    perp = ctx.cfg.hedge.perp_instrument
    minutes = runtime_settings.effective_review_bar_minutes(ctx)
    candles = await market_data.fetch_candles(
        ctx.ws, perp, str(minutes), 5)
    now = market_data.now_ms()
    done = [c for c in candles if c.ts + minutes * 60_000 <= now]
    if not done:
        raise RuntimeError(f"没有已收盘的 {minutes}min 复核 bar")
    return done[-1].close, done[-1].ts


async def _last_completed_3m_bar(ctx: Ctx) -> tuple[float, int]:
    """旧测试/调用兼容别名；实际周期读取运行时参数。"""
    return await _last_completed_review_bar(ctx)


async def _quote_issues(ctx: Ctx, parents: list[dict]) -> list[dict]:
    """Return per-instrument quote defects without blocking account risk scans."""
    instruments = {
        instrument
        for p in parents
        for instrument in (p.get("sell_instrument"), p.get("buy_instrument"))
        if instrument
    }
    issues = []
    for instrument in sorted(instruments):
        try:
            ticker = await ctx.ws.request(
                "public/ticker", {"instrument_name": instrument})
            bid = float(ticker.get("best_bid_price") or 0.0)
            ask = float(ticker.get("best_ask_price") or 0.0)
            defects = []
            if bid <= 0:
                defects.append("missing_bid")
            if ask <= 0:
                defects.append("missing_ask")
            if bid > 0 and ask > 0 and bid > ask:
                defects.append("crossed_book")
            if defects:
                issues.append({
                    "instrument": instrument,
                    "bid": bid or None,
                    "ask": ask or None,
                    "reason": ",".join(defects),
                })
        except Exception as exc:  # noqa: BLE001 - record exact degraded leg
            issues.append({
                "instrument": instrument,
                "bid": None,
                "ask": None,
                "reason": "ticker_error",
                "error": str(exc)[:120],
            })
    return issues


def _quote_warning(issues: list[dict]) -> str:
    instruments = ", ".join(item["instrument"] for item in issues)
    return f"Deribit 部分活跃期权缺少双边盘口：{instruments}"


async def _update_group_pnl(ctx: Ctx, parents: list[dict], opts: list) -> None:
    """按 Deribit mark price 更新所有未关闭子组的估算未实现盈亏与盈利比例。"""
    by_instr = {p.instrument: p for p in opts}
    for parent in parents:
        sell = by_instr.get(parent.get("sell_instrument"))
        buy = by_instr.get(parent.get("buy_instrument"))
        credit = float(parent.get("net_credit_btc") or 0.0)
        if not sell or not buy or credit <= 0:
            continue
        pnl_per_contract = credit - (sell.mark_price - buy.mark_price)
        ratio = pnl_per_contract / credit
        for group in await ctx.repo.groups_of(parent["id"]):
            remaining = max(0.0, float(group["qty"] or 0.0) -
                            float(group["closed_qty"] or 0.0))
            await ctx.repo.update_group_mark(
                group["id"], pnl_per_contract * remaining, ratio)


def settlement_pnl_per_contract(parent: dict, delivery_price: float) -> float:
    """按 Deribit BTC 期权交割口径计算信用价差每张到期损益。"""
    if delivery_price <= 0:
        raise ValueError("delivery_price must be positive")
    sell_strike = float(parent["sell_strike"])
    buy_strike = float(parent["buy_strike"])
    credit = float(parent.get("net_credit_btc") or 0.0)
    if parent["spread_type"] == "PUT_CREDIT":
        sell_value = max(sell_strike - delivery_price, 0.0) / delivery_price
        buy_value = max(buy_strike - delivery_price, 0.0) / delivery_price
    elif parent["spread_type"] == "CALL_CREDIT":
        sell_value = max(delivery_price - sell_strike, 0.0) / delivery_price
        buy_value = max(delivery_price - buy_strike, 0.0) / delivery_price
    else:
        raise ValueError(f"unsupported spread type: {parent['spread_type']}")
    return credit - (sell_value - buy_value)


def _has_expiry_residue(parent: dict, opts: list, orders: list[dict]) -> bool:
    instruments = {
        parent.get("sell_instrument"), parent.get("buy_instrument")
    } - {None, ""}
    if any(p.instrument in instruments and abs(float(p.size)) > 1e-9
           for p in opts):
        return True
    return any(
        o.get("instrument_name") in instruments
        and float(o.get("amount") or 0.0) > 1e-9
        for o in orders
    )


async def _settlement_hedge_is_flat(ctx: Ctx, parent_id: int) -> bool:
    """将到期仓位名下的永续对冲送入既有归零队列。"""
    row = await ctx.repo.get_hedge(parent_id)
    if not row:
        return True
    state = row["state"]
    managed_usd = abs(float(row.get("futures_usd") or 0.0))
    minimum = float(ctx.cfg.hedge.perp_contract_usd)
    if state == "HEDGE_INACTIVE" and managed_usd < minimum - 1e-9:
        return True
    if state == "HEDGE_INACTIVE":
        await ctx.repo.transition_hedge(parent_id, "HEDGE_ACTIVE")
        state = "HEDGE_ACTIVE"
    if state == "HEDGE_ACTIVE":
        await ctx.repo.transition_hedge(
            parent_id, "HEDGE_RELEASE_PENDING", ev.HEDGE_RELEASE_PENDING,
            reason="EXPIRY_SETTLEMENT")
        state = "HEDGE_RELEASE_PENDING"
    if state in ("HEDGE_RELEASE_PENDING", "HEDGE_CLOSING"):
        ctx.queue.put(ctx.queue.hedge_release(parent_id))
    return False


async def _finalize_expiry_settlement(ctx: Ctx, parent: dict,
                                      delivery_price: float) -> None:
    repo = ctx.repo
    pnl_per_contract = settlement_pnl_per_contract(parent, delivery_price)
    for group in await repo.groups_of(parent["id"]):
        if group["state"] != "OPEN":
            continue
        remaining = max(
            0.0, float(group["qty"] or 0.0) -
            float(group["closed_qty"] or 0.0))
        realized = float(group.get("realized_pnl_btc") or 0.0)
        await repo.transition_group(
            group["id"], "SETTLED", closed_qty=float(group["qty"] or 0.0),
            realized_pnl=realized + pnl_per_contract * remaining,
            reason="EXPIRY_SETTLEMENT")
    await repo.mark_leg_closed(parent["id"], "SELL_LEG")
    await repo.mark_leg_closed(parent["id"], "BUY_LEG")
    await repo.set_settlement_price(parent["id"], delivery_price)
    await repo.set_close_reason(parent["id"], "EXPIRY_SETTLEMENT")
    await repo.transition_parent(
        parent["id"], "SETTLED", ev.EXPIRY_SETTLED,
        settlement_price=delivery_price,
        per_contract_pnl=pnl_per_contract)


async def _handle_expiry_settlement(ctx: Ctx, parents: list[dict],
                                    opts: list, orders: list[dict],
                                    now: int) -> bool:
    """推进到期结算；返回 True 表示本轮存在到期/待结算仓位。"""
    handled = False
    delivery_prices: dict[int, float | None] = {}
    for parent in parents:
        expiry_ts = int(parent.get("expiry_ts") or 0)
        if parent["state"] not in ("SETTLEMENT_PENDING", "RESIDUAL_LONG") and (
                not expiry_ts or now < expiry_ts):
            continue
        handled = True
        if parent["state"] in ("OPEN", "PARTIALLY_CLOSED"):
            await ctx.repo.set_close_reason(parent["id"], "EXPIRY_SETTLEMENT")
            await ctx.repo.transition_parent(
                parent["id"], "SETTLEMENT_PENDING",
                ev.EXPIRY_SETTLEMENT_PENDING, expiry_ts=expiry_ts)
            for group in await ctx.repo.groups_of(parent["id"]):
                if group["state"] == "OPEN":
                    await ctx.repo.update_group_mark(group["id"], 0.0, None)
            parent = await ctx.repo.get_parent(parent["id"])
        if _has_expiry_residue(parent, opts, orders):
            continue
        if not await _settlement_hedge_is_flat(ctx, parent["id"]):
            continue
        if expiry_ts not in delivery_prices:
            try:
                delivery_prices[expiry_ts] = await market_data.get_delivery_price(
                    ctx.ws, expiry_ts, ctx.cfg.strategy.currency)
            except Exception as exc:  # noqa: BLE001 - 下一轮扫描继续等待正式交割价
                log.warning("parent %s 交割价暂不可用: %s", parent["id"], exc)
                delivery_prices[expiry_ts] = None
        delivery_price = delivery_prices[expiry_ts]
        if delivery_price is None:
            continue
        await _finalize_expiry_settlement(ctx, parent, delivery_price)
    return handled


async def _check_push_watchdog(ctx: Ctx) -> None:
    """推送看门狗：Pine 自报某根 2h bar 满足全部条件却没收到推送 → 记录 WARN（每 bar 一次）。"""
    repo = ctx.repo
    try:
        wd = await entry_conditions.push_watchdog(ctx)
    except Exception as exc:  # noqa: BLE001 - 看门狗不得影响扫描主流程
        log.debug("push watchdog 计算失败: %s", exc)
        return
    if not wd.get("alarm"):
        return
    last = await repo.get_state("last_push_alarm_bar_ts")
    if last is not None and int(last) >= wd["bar_ts"]:
        return   # 该 bar 已告警过，避免每 3 分钟重复刷
    await ev.emit(repo.conn, ev.PUSH_MISSING, level="WARN",
                  bar_ts=wd["bar_ts"], side=wd["side"], bar_close=wd["bar_close"])
    await repo.set_state("last_push_alarm_bar_ts", str(wd["bar_ts"]))


async def _resume_closing_hedges(ctx: Ctx, parents: list[dict]) -> None:
    """盘口异常时也恢复永续归零；队列会按 parent_id 自动去重。"""
    for parent in parents:
        hedge_row = await ctx.repo.get_hedge(parent["id"])
        if hedge_row and hedge_row.get("state") == "HEDGE_CLOSING":
            ctx.queue.put(ctx.queue.hedge_release(parent["id"]))


async def handle_scan(ctx: Ctx, payload: dict) -> bool:
    repo, cfg = ctx.repo, ctx.cfg
    t0 = time.time()

    # 独立扫描可能与开平仓并行，不能把平仓处理器设置的冻结标志清掉。
    # 只有账户当前没有正在平仓的父交易时，才按新 bar 重置。
    active_at_start = await repo.active_parents()
    if not any(p["state"] == "CLOSING" for p in active_at_start):
        ctx.hedge_frozen = False

    # 推送看门狗（规范第五节链路可观测）：与持仓无关，每根扫描都跑，不影响主流程
    await _check_push_watchdog(ctx)

    # 3. FORCE_CLOSE 已激活 → 只做核对，不做其他动作
    if await ctx.force_close_active():
        await repo.set_state("last_scan_ts", str(int(t0 * 1000)))
        return True

    # 1. 获取真实持仓与订单
    try:
        # This timestamp belongs to the option-position snapshot, not to the
        # later risk decision.  The independent scanner can overlap a long
        # OPEN/REVERSE command; in that case a parent may become OPEN after
        # this snapshot was requested and must not be compared with stale
        # exchange positions from the middle of its execution.
        positions_snapshot_started_ts = int(time.time() * 1000)
        opts = await account.get_option_positions(ctx.ws, cfg.strategy.currency)
        futs = await account.get_future_positions(ctx.ws, cfg.strategy.currency)
        summary = await account.get_account_summary(ctx.ws, cfg.strategy.currency)
        orders = await account.get_open_orders(ctx.ws, cfg.strategy.currency)
    except Exception as exc:  # noqa: BLE001 - 行情/账户不可用计入风控
        ctx.quote_missing_count += 1
        ctx.scan_last_error = f"Deribit 持仓/订单读取失败：{str(exc)[:160]}"
        ctx.scan_last_error_kind = "deribit"
        ctx.scan_last_error_ts = int(time.time() * 1000)
        log.warning("scan 持仓获取失败(%s)，连续 %d 次", exc, ctx.quote_missing_count)
        if ctx.quote_missing_count >= cfg.risk.quote_missing_scans and cfg.risk.auto_pause_enabled:
            await risk.request_risk_force_close(
                ctx,
                f"持仓/盘口连续 {ctx.quote_missing_count} 轮不可用",
                auto_resume=True)
        return False

    active_after_snapshot = await repo.active_parents()
    # 永续撤销不依赖期权盘口。即使临近到期时期权报价缺失，也必须优先
    # 恢复之前失败的 HEDGE_CLOSING，避免方向性永续仓位永久遗留。
    await _resume_closing_hedges(ctx, active_after_snapshot)
    expiry_policy = str(getattr(
        cfg.exit, "expiry_policy", "HOLD_TO_EXPIRY")).upper()
    settlement_handled = False
    if expiry_policy == "HOLD_TO_EXPIRY":
        settlement_reader = getattr(repo, "settlement_parents", None)
        settlement_parents = (
            await settlement_reader()
            if callable(settlement_reader) else active_after_snapshot)
        settlement_handled = await _handle_expiry_settlement(
            ctx, settlement_parents, opts, orders, market_data.now_ms())

    parents = await repo.open_parents()
    # PUT/CALL can coexist. A pending settlement on one side must not starve
    # the other side's quote, risk, mark-to-market, or hedge scan.
    if settlement_handled and not parents:
        await repo.set_state("last_scan_ts", str(int(t0 * 1000)))
        return True
    quote_issues = await _quote_issues(ctx, parents) if parents else []
    if quote_issues:
        ctx.quote_missing_count += 1
        ctx.scan_quote_issues = quote_issues
        ctx.scan_last_warning = _quote_warning(quote_issues)
        ctx.scan_last_warning_kind = "option_quotes"
        ctx.scan_last_warning_ts = int(time.time() * 1000)
        log.warning("活跃期权盘口连续缺失 %d/%d 轮: %s",
                    ctx.quote_missing_count, cfg.risk.quote_missing_scans,
                    ctx.scan_last_warning)
        if ctx.quote_missing_count >= cfg.risk.quote_missing_scans and cfg.risk.auto_pause_enabled:
            await risk.request_risk_force_close(
                ctx,
                f"期权盘口连续 {ctx.quote_missing_count} 轮缺失",
                auto_resume=True)
            await repo.set_state("last_scan_ts", str(int(t0 * 1000)))
            return True
    else:
        ctx.quote_missing_count = 0
        ctx.scan_quote_issues = []
        ctx.scan_last_warning = None
        ctx.scan_last_warning_kind = None
        ctx.scan_last_warning_ts = None

    await risk.maybe_resume_testnet_daily_loss_pause(
        ctx, opts=opts, futs=futs, orders=orders, summary=summary)

    await _update_group_pnl(ctx, parents, opts)

    # 2. 与数据库核对 + 附带风控检查（保证金/日亏/Delta 异常）
    breach = await risk.run_risk_checks(
        ctx, opts, futs, summary=summary,
        positions_snapshot_started_ts=positions_snapshot_started_ts)
    await sync_risk_alert_event(ctx, breach)
    if breach and cfg.risk.auto_pause_enabled:
        await risk.request_risk_force_close(
            ctx, breach.reason, auto_resume=False,
            resume_after_date=breach.resume_after_date)
        await repo.set_state("last_scan_ts", str(int(t0 * 1000)))
        return True
    active = await repo.active_parents()
    await risk.maybe_resume_transient_risk_pause(ctx, active)
    opening = [p for p in active if p["state"] == "OPENING"]
    flat_residuals = flat_residual_decision_parents(
        active, opts, cfg.strategy.min_lot)
    closing = [
        p for p in active
        if p["state"] == "CLOSING"
        and p.get("close_reason") != risk.RESIDUAL_DECISION_REASON
    ]

    # 4. OPENING/CLOSING 中间态都交回串行业务队列恢复。即使扫描与
    # 当前开仓重叠，恢复命令真正执行时也会先重读状态，正常完成后自动 no-op。
    if opening:
        for parent in opening:
            ctx.queue.put(ctx.queue.recover_opening(parent["id"]))
        await repo.set_state("last_scan_ts", str(int(t0 * 1000)))
        return True

    # 人工处理中的保护腿一旦已在 Deribit 归零，就恢复一次幂等平仓核对，
    # 让数据库父流程、子组和对冲状态进入终态并释放全账户开仓锁。
    # 保护腿仍存在时不会进入这里，继续维持人工决定边界。
    if flat_residuals:
        for parent in flat_residuals:
            ctx.queue.put(ctx.queue.normal_close(
                parent["id"], RESIDUAL_AUTO_RECONCILE_REASON))
        await repo.set_state("last_scan_ts", str(int(t0 * 1000)))
        return True

    # 已在平仓中的父交易 → 每根扫描周期按交易所实际残余继续恢复，
    # 不做对冲，也不让 CLOSING 永久卡住。
    if closing:
        for parent in closing:
            ctx.queue.put(ctx.queue.normal_close(
                parent["id"], parent.get("close_reason") or "CLOSE_RETRY"))
        await repo.set_state("last_scan_ts", str(int(t0 * 1000)))
        return True

    # 5. 兼容旧的提前平仓策略；默认 HOLD_TO_EXPIRY 不进入此分支。
    if expiry_policy == "CLOSE_BEFORE_EXPIRY":
        now = market_data.now_ms()
        for p in parents:
            if p["expiry_ts"]:
                dte = (p["expiry_ts"] - now) / 86400000
                if dte <= cfg.exit.expiry_close_dte:
                    ctx.queue.put(ctx.queue.normal_close(p["id"], "EXPIRY_DTE"))
                    await repo.set_state("last_scan_ts", str(int(t0 * 1000)))
                    return True

    # 6~11. BEP / 撤销线 / Delta 管线
    if parents:
        try:
            bar_close, bar_ts = await _last_completed_review_bar(ctx)
            index_price = await market_data.get_index_price(
                ctx.ws, cfg.strategy.currency)
        except Exception as exc:  # noqa: BLE001
            log.warning("复核 bar 获取失败: %s", exc)
            await repo.set_state("last_scan_ts", str(int(t0 * 1000)))
            return True
        view = hedge.delta_view_from_positions(
            ctx, summary, opts, futs, index_price)
        observed_ts = int(time.time() * 1000)
        price_source = (
            f"{runtime_settings.effective_review_bar_minutes(ctx)}m_close")
        ctx.hedge_snapshot = hedge.snapshot_payload(
            view, bar_close, bar_ts, observed_ts, price_source)
        ctx.hedge_snapshots = {}
        fresh_sides: set[str] = set()
        review_cache = getattr(ctx, "hedge_review_bar_ts", None)
        if not isinstance(review_cache, dict):
            review_cache = {}
            ctx.hedge_review_bar_ts = review_cache
        state_reader = getattr(repo, "get_state", None)
        for side in runtime_settings.HEDGE_SIDES:
            last = review_cache.get(side)
            if last is None and callable(state_reader):
                raw = await state_reader(f"hedge.review.last_bar.{side}")
                last = int(raw) if raw not in (None, "") else 0
            if bar_ts > int(last or 0):
                fresh_sides.add(side)
        for p in parents:
            hedge_row = await repo.get_hedge(p["id"])
            managed_usd = float((hedge_row or {}).get("futures_usd") or 0.0)
            option_delta = hedge.parent_option_delta(p, opts)
            parent_view = hedge.DeltaView(
                option_delta=option_delta,
                futures_delta=(managed_usd / index_price
                               if index_price > 0 else 0.0),
                net_delta=(option_delta + managed_usd / index_price
                           if index_price > 0 else option_delta),
                equity_btc=summary.equity_btc,
                index_price=index_price,
            )
            ctx.hedge_snapshots[p["id"]] = hedge.snapshot_payload(
                parent_view, bar_close, bar_ts, observed_ts, price_source)
            if p.get("spread_type") in fresh_sides:
                await hedge.scan_hedge(
                    ctx, p, bar_close, bar_ts, view=parent_view,
                    price_source=price_source, allow_rebalance=False)
        if fresh_sides:
            await hedge.scan_account_hedge(
                ctx, parents=parents, opts=opts, summary=summary,
                index_price=index_price, review_sides=fresh_sides)
            state_writer = getattr(repo, "set_state", None)
            for side in fresh_sides:
                review_cache[side] = bar_ts
                if callable(state_writer):
                    await state_writer(
                        f"hedge.review.last_bar.{side}", str(bar_ts))
    else:
        ctx.hedge_snapshot = None
        ctx.hedge_snapshots = {}

    # 12. 记录扫描时间（子组状态/PnL 由各处理器在动作发生时更新）
    await repo.set_state("last_scan_ts", str(int(t0 * 1000)))
    dt = time.time() - t0
    if dt > 30:
        log.warning("independent scan 耗时 %.1fs", dt)
    return True
