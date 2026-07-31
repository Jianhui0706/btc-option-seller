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
from ..exchange import account, market_data
from ..monitor import events as ev
from . import entry_conditions, hedge, risk

log = logging.getLogger("v2.scan")

BAR_SEC = 180


async def run_scheduler(ctx: Ctx) -> None:
    offset = ctx.cfg.scan.bar_align_offset_sec
    interval = ctx.cfg.scan.interval_sec
    log.info("scanner scheduler started (interval=%ss offset=%ss)", interval, offset)
    while True:
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
            ctx.scan_last_completed_ts = int(time.time() * 1000)
            ctx.scan_last_error = None
            ctx.scan_last_error_kind = None
            ctx.scan_last_error_ts = None
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


async def _last_completed_3m_bar(ctx: Ctx) -> tuple[float, int]:
    """返回最近已收盘 3min bar 的 (close, bar_start_ts_ms)。"""
    perp = ctx.cfg.hedge.perp_instrument
    candles = await market_data.fetch_candles(ctx.ws, perp, "3", 5)
    now = market_data.now_ms()
    done = [c for c in candles if c.ts + BAR_SEC * 1000 <= now]
    if not done:
        raise RuntimeError("没有已收盘的 3min bar")
    return done[-1].close, done[-1].ts


async def _quotes_available(ctx: Ctx, parents: list[dict]) -> bool:
    """检查活跃价差两腿是否仍有可执行双边盘口。"""
    instruments = {
        instrument
        for p in parents
        for instrument in (p.get("sell_instrument"), p.get("buy_instrument"))
        if instrument
    }
    for instrument in instruments:
        try:
            ticker = await ctx.ws.request(
                "public/ticker", {"instrument_name": instrument})
            bid = float(ticker.get("best_bid_price") or 0.0)
            ask = float(ticker.get("best_ask_price") or 0.0)
            if bid <= 0 or ask <= 0 or bid > ask:
                return False
        except Exception:  # noqa: BLE001 - 连续计数后进入统一风控
            return False
    return True


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
        opts = await account.get_option_positions(ctx.ws, cfg.strategy.currency)
        futs = await account.get_future_positions(ctx.ws, cfg.strategy.currency)
        summary = await account.get_account_summary(ctx.ws, cfg.strategy.currency)
        await account.get_open_orders(ctx.ws, cfg.strategy.currency)
    except Exception as exc:  # noqa: BLE001 - 行情/账户不可用计入风控
        ctx.quote_missing_count += 1
        ctx.scan_last_error = f"Deribit 持仓/订单读取失败：{str(exc)[:160]}"
        ctx.scan_last_error_kind = "deribit"
        ctx.scan_last_error_ts = int(time.time() * 1000)
        log.warning("scan 持仓获取失败(%s)，连续 %d 次", exc, ctx.quote_missing_count)
        if ctx.quote_missing_count >= cfg.risk.quote_missing_scans:
            await risk.request_risk_force_close(
                ctx,
                f"持仓/盘口连续 {ctx.quote_missing_count} 轮不可用",
                auto_resume=True)
        return False


    parents = await repo.open_parents()
    if parents and not await _quotes_available(ctx, parents):
        ctx.quote_missing_count += 1
        ctx.scan_last_error = "Deribit 活跃期权盘口不可用"
        ctx.scan_last_error_kind = "deribit"
        ctx.scan_last_error_ts = int(time.time() * 1000)
        log.warning("活跃期权盘口连续缺失 %d/%d 轮",
                    ctx.quote_missing_count, cfg.risk.quote_missing_scans)
        if ctx.quote_missing_count >= cfg.risk.quote_missing_scans:
            await risk.request_risk_force_close(
                ctx,
                f"期权盘口连续 {ctx.quote_missing_count} 轮缺失",
                auto_resume=True)
        return False
    ctx.quote_missing_count = 0

    await _update_group_pnl(ctx, parents, opts)

    # 2. 与数据库核对 + 附带风控检查（保证金/日亏/Delta 异常）
    reason = await risk.run_risk_checks(ctx, opts, futs, summary=summary)
    if reason:
        await risk.request_risk_force_close(ctx, reason, auto_resume=False)
        await repo.set_state("last_scan_ts", str(int(t0 * 1000)))
        return True

    active = await repo.active_parents()
    await risk.maybe_resume_transient_risk_pause(ctx, active)
    opening = [p for p in active if p["state"] == "OPENING"]
    closing = [p for p in active if p["state"] == "CLOSING"]

    # 4. OPENING/CLOSING 中间态都交回串行业务队列恢复。即使扫描与
    # 当前开仓重叠，恢复命令真正执行时也会先重读状态，正常完成后自动 no-op。
    if opening:
        for parent in opening:
            ctx.queue.put(ctx.queue.recover_opening(parent["id"]))
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

    # 5. DTE 到期保护属于执行安全兜底，不是策略退出信号。
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
            bar_close, bar_ts = await _last_completed_3m_bar(ctx)
            index_price = await market_data.get_index_price(
                ctx.ws, cfg.strategy.currency)
        except Exception as exc:  # noqa: BLE001
            log.warning("3min bar 获取失败: %s", exc)
            await repo.set_state("last_scan_ts", str(int(t0 * 1000)))
            return True
        view = hedge.delta_view_from_positions(
            ctx, summary, opts, futs, index_price)
        ctx.hedge_snapshot = hedge.snapshot_payload(
            view, bar_close, bar_ts, int(time.time() * 1000))
        for p in parents:
            await hedge.scan_hedge(ctx, p, bar_close, bar_ts, view=view)
    else:
        ctx.hedge_snapshot = None

    # 12. 记录扫描时间（子组状态/PnL 由各处理器在动作发生时更新）
    await repo.set_state("last_scan_ts", str(int(t0 * 1000)))
    dt = time.time() - t0
    if dt > 30:
        log.warning("independent scan 耗时 %.1fs", dt)
    return True
