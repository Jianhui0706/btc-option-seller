"""3 分钟扫描（规范第二、十五节）。

run_scheduler: 对齐 3 分钟 bar 边界（+offset 秒）入队 SCAN 命令；
handle_scan: 严格按规范 12 步执行。优先级由命令队列保证：
强平 > 正常平仓 > 对冲撤销 > 对冲调整 > 新开仓。
"""

from __future__ import annotations

import asyncio
import logging
import time

from ..context import Ctx
from ..exchange import account, market_data
from ..monitor import events as ev
from . import hedge, indicators, risk

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
        ctx.queue.put(ctx.queue.scan())


async def _last_completed_3m_bar(ctx: Ctx) -> tuple[float, int]:
    """返回最近已收盘 3min bar 的 (close, bar_start_ts_ms)。"""
    perp = ctx.cfg.hedge.perp_instrument
    candles = await market_data.fetch_candles(ctx.ws, perp, "3", 5)
    now = market_data.now_ms()
    done = [c for c in candles if c.ts + BAR_SEC * 1000 <= now]
    if not done:
        raise RuntimeError("没有已收盘的 3min bar")
    return done[-1].close, done[-1].ts


async def _check_dvol_exit(ctx: Ctx) -> bool:
    """第 5 步：后端自算 DVOL 滚动退出（每根 12h bar 只判一次）。"""
    repo, cfg = ctx.repo, ctx.cfg
    bars = await market_data.fetch_dvol_12h(ctx.ws, cfg.strategy.currency,
                                            points=cfg.exit.dvol_window_bars + 8)
    completed = [b for b in bars if b.completed()]
    if not completed:
        return False
    last_ts = completed[-1].ts
    checked = await repo.get_state("last_dvol_bar_ts")
    if checked is not None and int(checked) >= last_ts:
        return False   # 该 12h bar 已判过
    triggered, threshold = indicators.dvol_exit_triggered(
        [b.close for b in completed],
        window=cfg.exit.dvol_window_bars, pct=cfg.exit.dvol_percentile)
    if triggered:
        await ev.emit(repo.conn, ev.EXIT_DVOL_ROLLING_30, source="backend",
                      current=completed[-1].close, threshold=threshold)
    # 标记放最后：中途异常时该 12h bar 下轮扫描重判，退出信号不会被吞
    await repo.set_state("last_dvol_bar_ts", str(last_ts))
    return triggered


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


async def handle_scan(ctx: Ctx, payload: dict) -> None:
    repo, cfg = ctx.repo, ctx.cfg
    ctx.hedge_frozen = False   # 每根 bar 重置"平仓冻结对冲"标志
    t0 = time.time()

    # 3. FORCE_CLOSE 已激活 → 只做核对，不做其他动作
    if await ctx.force_close_active():
        await repo.set_state("last_scan_ts", str(int(t0 * 1000)))
        return

    # 1. 获取真实持仓与订单
    try:
        opts = await account.get_option_positions(ctx.ws, cfg.strategy.currency)
        futs = await account.get_future_positions(ctx.ws, cfg.strategy.currency)
        await account.get_open_orders(ctx.ws, cfg.strategy.currency)
    except Exception as exc:  # noqa: BLE001 - 行情/账户不可用计入风控
        ctx.quote_missing_count += 1
        log.warning("scan 持仓获取失败(%s)，连续 %d 次", exc, ctx.quote_missing_count)
        if ctx.quote_missing_count >= cfg.risk.quote_missing_scans:
            ctx.queue.put(ctx.queue.force_close(
                "RISK_ENGINE", f"持仓/盘口连续 {ctx.quote_missing_count} 轮不可用"))
        return


    parents = await repo.open_parents()
    if parents and not await _quotes_available(ctx, parents):
        ctx.quote_missing_count += 1
        log.warning("活跃期权盘口连续缺失 %d/%d 轮",
                    ctx.quote_missing_count, cfg.risk.quote_missing_scans)
        if ctx.quote_missing_count >= cfg.risk.quote_missing_scans:
            ctx.queue.put(ctx.queue.force_close(
                "RISK_ENGINE", f"期权盘口连续 {ctx.quote_missing_count} 轮缺失"))
        return
    ctx.quote_missing_count = 0

    await _update_group_pnl(ctx, parents, opts)

    # 2. 与数据库核对 + 附带风控检查（保证金/日亏/Delta 异常）
    reason = await risk.run_risk_checks(ctx, opts, futs)
    if reason:
        ctx.queue.put(ctx.queue.force_close("RISK_ENGINE", reason))
        return

    closing = [p for p in await repo.active_parents() if p["state"] == "CLOSING"]

    # 4. 已在平仓中的父交易 → 本 bar 不做对冲，仅等待
    if closing:
        await repo.set_state("last_scan_ts", str(int(t0 * 1000)))
        return

    # 5. DVOL 退出（后端自算，与 TV webhook 互为双保险）
    if parents and await _check_dvol_exit(ctx):
        for p in parents:
            ctx.queue.put(ctx.queue.normal_close(p["id"], ev.EXIT_DVOL_ROLLING_30))
        await repo.set_state("last_scan_ts", str(int(t0 * 1000)))
        return   # 同一 bar 只执行平仓，不再新增对冲（规范第十五节）

    # 5b. DTE 到期保护（沿用 V1：距到期 ≤ N 天平仓）
    now = market_data.now_ms()
    for p in parents:
        if p["expiry_ts"]:
            dte = (p["expiry_ts"] - now) / 86400000
            if dte <= cfg.exit.expiry_close_dte:
                ctx.queue.put(ctx.queue.normal_close(p["id"], "EXPIRY_DTE"))
                await repo.set_state("last_scan_ts", str(int(t0 * 1000)))
                return

    # 6~11. BEP / 撤销线 / Delta 管线
    if parents:
        try:
            bar_close, bar_ts = await _last_completed_3m_bar(ctx)
        except Exception as exc:  # noqa: BLE001
            log.warning("3min bar 获取失败: %s", exc)
            await repo.set_state("last_scan_ts", str(int(t0 * 1000)))
            return
        for p in parents:
            await hedge.scan_hedge(ctx, p, bar_close, bar_ts)

    # 12. 记录扫描时间（子组状态/PnL 由各处理器在动作发生时更新）
    await repo.set_state("last_scan_ts", str(int(t0 * 1000)))
    dt = time.time() - t0
    if dt > 30:
        log.warning("scan 耗时 %.1fs（执行占用了扫描窗口）", dt)
