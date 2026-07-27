"""风控检查器 + FORCE_CLOSE（规范第九节）+ 启动崩溃恢复。

FORCE_CLOSE：RISK_ENGINE 与 MANUAL 共用同一执行函数，只是日志来源不同。
执行内容：停开仓 → 撤全部挂单 → 平全部卖腿+买腿（不留跛腿）→ 平期货对冲 →
核对归零 → 子组标记 FORCE_CLOSED。

持仓一致性口径：只核对"本系统父交易"的腿；账户中与本系统无关的期权仓位
（其他策略/人工，规范第三节允许其存在）只告警并阻止新开仓，不强平它们。
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

from ..context import Ctx
from ..exchange import account, futures
from ..execution import adapter
from ..monitor import events as ev

log = logging.getLogger("v2.risk")

EPS = 1e-9


async def run_risk_checks(ctx: Ctx, opts: list, futs: list) -> Optional[str]:
    """每次扫描附带的风控检查。返回触发原因（None=正常）。"""
    repo, cfg = ctx.repo, ctx.cfg
    summ = await account.get_account_summary(ctx.ws, cfg.strategy.currency)

    # 日初权益快照（北京时间自然日）
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d")
    if await repo.get_state("day_start_date") != today:
        await repo.set_state("day_start_date", today)
        await repo.set_state("day_start_equity", str(summ.equity_btc))
    day_start = float(await repo.get_state("day_start_equity") or summ.equity_btc)

    # 1) 保证金比例
    if summ.margin_ratio > cfg.risk.margin_ratio_limit:
        return (f"维持保证金率 {summ.margin_ratio:.1%} > "
                f"{cfg.risk.margin_ratio_limit:.0%}")
    # 2) 日亏损
    if day_start > 0:
        loss = (day_start - summ.equity_btc) / day_start
        if loss > cfg.risk.daily_loss_limit_pct:
            return f"日亏损 {loss:.1%} 超过限制 {cfg.risk.daily_loss_limit_pct:.0%}"
    # 3) Delta 异常扩大
    net_delta = sum(p.delta for p in opts) + sum(f.delta_btc for f in futs)
    if abs(net_delta) > cfg.risk.delta_anomaly_btc:
        return f"净 Delta {net_delta:+.3f} BTC 异常扩大(> {cfg.risk.delta_anomaly_btc})"
    # 4) 本系统持仓与 DB 一致性
    mismatch = await _check_consistency(ctx, opts)
    if mismatch:
        return mismatch
    return None


async def _check_consistency(ctx: Ctx, opts: list) -> Optional[str]:
    repo = ctx.repo
    cfg = ctx.cfg
    by_instr = {p.instrument: p.size for p in opts}
    ours: set[str] = set()
    for parent in await repo.open_parents():
        groups = await repo.groups_of(parent["id"])
        closed = sum(g["closed_qty"] or 0.0 for g in groups)
        remaining = round((parent["qty_total"] or 0.0) - closed, 10)
        if remaining < cfg.strategy.min_lot - EPS:
            continue
        ours.add(parent["sell_instrument"])
        ours.add(parent["buy_instrument"])
        actual_short = -by_instr.get(parent["sell_instrument"], 0.0)
        if abs(actual_short - remaining) > cfg.strategy.min_lot / 2:
            return (f"parent {parent['id']} 卖腿不一致: DB={remaining:.1f} "
                    f"交易所={actual_short:.1f} ({parent['sell_instrument']})")
        if not cfg.exit.keep_buy_leg_on_normal_exit:
            actual_long = by_instr.get(parent["buy_instrument"], 0.0)
            if abs(actual_long - remaining) > cfg.strategy.min_lot / 2:
                return (f"parent {parent['id']} 买腿不一致: DB={remaining:.1f} "
                        f"交易所={actual_long:.1f} ({parent['buy_instrument']})")
    # 与本系统无关的卖方仓位：只告警（阻止新开仓已由开仓前检查覆盖）
    foreign_shorts = [i for i, s in by_instr.items() if s < 0 and i not in ours]
    if foreign_shorts:
        log.debug("账户存在非本系统卖方仓位: %s（不强平，仅阻止新开仓）", foreign_shorts)
    return None


async def handle_force_close(ctx: Ctx, payload: dict) -> None:
    """FORCE_CLOSE 统一执行函数（source=RISK_ENGINE / MANUAL）。幂等可重入。"""
    source = payload.get("source", "MANUAL")
    reason = payload.get("reason", "")
    repo, cfg = ctx.repo, ctx.cfg
    code = ev.FORCE_CLOSE_RISK if source == "RISK_ENGINE" else ev.FORCE_CLOSE_MANUAL
    log.warning("FORCE_CLOSE 开始 source=%s reason=%s", source, reason)

    # 1. 停止新的开仓与 Delta 加仓
    await repo.set_flag("force_close_active", True)
    ctx.hedge_frozen = True
    await ev.emit(repo.conn, code, level="ERROR", reason=reason, stage="start")

    # 2. 打断执行中的开/平仓任务（执行器 finally 会撤残留挂单）
    task = ctx.current_exec_task
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 - 强平继续以交易所仓位为事实源
            log.warning("等待被打断的执行任务结束时出错: %s", exc)
        finally:
            if ctx.current_exec_task is task:
                ctx.current_exec_task = None

    # 3. 撤销全部未成交订单
    try:
        await account.cancel_all(ctx.ws, cfg.strategy.currency)
    except Exception as exc:  # noqa: BLE001
        log.error("cancel_all 失败: %s", exc)

    # 4. 平掉全部期权腿（以交易所为事实源；卖方买回、买方卖出，不留跛腿）
    incomplete = []
    opts = await account.get_option_positions(ctx.ws, cfg.strategy.currency)
    for p in opts:
        qty = round(abs(p.size), 10)
        if qty < cfg.strategy.min_lot - EPS:
            continue
        try:
            if p.is_short:
                res = await adapter.close_sell_leg_only(
                    ctx.ws, sell_instrument=p.instrument, qty=qty,
                    min_lot=cfg.strategy.min_lot)
            else:
                res = await adapter.close_long_leg(
                    ctx.ws, instrument=p.instrument, qty=qty,
                    min_lot=cfg.strategy.min_lot)
            await repo.record_fill(parent_id=None, group_id=None,
                                   instrument=p.instrument,
                                   direction="buy" if p.is_short else "sell",
                                   qty=res.closed_qty,
                                   price=res.buy_back_avg or res.sell_out_avg,
                                   fee=0.0, phase="close")
            if not res.complete:
                incomplete.append(p.instrument)
        except Exception as exc:  # noqa: BLE001
            log.error("强平 %s 失败: %s", p.instrument, exc)
            incomplete.append(p.instrument)

    # 5. 平掉全部 BTC 期货对冲
    try:
        await futures.flatten_perp(ctx.ws, cfg.hedge.perp_instrument,
                                   ioc_ticks=cfg.hedge.ioc_ticks, max_rounds=8)
    except Exception as exc:  # noqa: BLE001
        log.error("强平期货失败: %s", exc)

    # 6. 核对
    opts_after = await account.get_option_positions(ctx.ws, cfg.strategy.currency)
    perp_usd, _ = await futures.current_perp_position(ctx.ws, cfg.hedge.perp_instrument)
    clean = not opts_after and abs(perp_usd) < cfg.hedge.perp_contract_usd

    # 7. 标记所有活跃父交易/子组为 FORCE_CLOSED
    for parent in await repo.active_parents():
        for g in await repo.groups_of(parent["id"]):
            if g["state"] not in ("CLOSED", "FORCE_CLOSED"):
                await repo.transition_group(g["id"], "FORCE_CLOSED", reason=source)
        try:
            await repo.transition_parent(parent["id"], "FORCE_CLOSED", code,
                                         source=source, reason=reason)
        except Exception:  # noqa: BLE001 - 前置态(如 SIGNAL_RECEIVED)直接置 OPEN_FAILED
            try:
                await repo.transition_parent(parent["id"], "OPEN_FAILED", code)
            except Exception:  # noqa: BLE001
                pass
        hedge_row = await repo.get_hedge(parent["id"])
        if hedge_row:
            await repo.set_hedge_futures(parent["id"], 0.0, 0.0)

    await ev.emit(repo.conn, code, level="ERROR", reason=reason, stage="done",
                  clean=clean, incomplete=incomplete)
    if not clean:
        log.error("FORCE_CLOSE 未完全清仓（%s / perp %.0f USD），"
                  "force_close_active 保持，等待人工处理", incomplete, perp_usd)
    else:
        log.warning("FORCE_CLOSE 完成，全部清仓。force_close_active 保持置位，"
                    "确认后用 POST /control/clear_force_close 解除")


async def reconcile_on_startup(ctx: Ctx) -> None:
    """崩溃恢复：处理停在中间态（OPENING/CLOSING/HEDGE_REBALANCING）的记录。"""
    repo, cfg = ctx.repo, ctx.cfg
    transients = [p for p in await repo.active_parents()
                  if p["state"] in ("SIGNAL_RECEIVED", "POSITION_CHECKING",
                                    "SELECTING_LEGS", "OPENING", "CLOSING")]
    if not transients:
        return
    log.warning("启动 reconcile: %d 笔中间态交易", len(transients))
    try:
        await account.cancel_all(ctx.ws, cfg.strategy.currency)   # 清孤儿挂单
    except Exception:  # noqa: BLE001
        pass
    opts = await account.get_option_positions(ctx.ws, cfg.strategy.currency)
    by_instr = {p.instrument: p.size for p in opts}
    for p in transients:
        pid = p["id"]
        if p["state"] in ("SIGNAL_RECEIVED", "POSITION_CHECKING", "SELECTING_LEGS"):
            # 未开始下单，直接作废
            await repo.transition_parent(pid, "OPEN_FAILED", ev.RECONCILE_MISMATCH,
                                         note="startup reconcile: 未完成即中断")
            continue
        if p["state"] == "OPENING":
            await _reconcile_opening(ctx, p, by_instr)
        elif p["state"] == "CLOSING":
            # 继续未完成的平仓（幂等）
            ctx.queue.put(ctx.queue.normal_close(pid, p["close_reason"] or "RESUME"))


async def _reconcile_opening(ctx: Ctx, p: dict, by_instr: dict) -> None:
    """OPENING 中断恢复：回滚多余腿 → 从 Deribit 成交记录重建净权利金/BEP → 认领为 OPEN。"""
    repo, cfg = ctx.repo, ctx.cfg
    pid = p["id"]
    min_lot = cfg.strategy.min_lot
    sell_i, buy_i = p["sell_instrument"], p["buy_instrument"]
    if not sell_i or not buy_i:
        await ev.emit(repo.conn, ev.RECONCILE_MISMATCH, level="ERROR", parent_id=pid,
                      note="OPENING 中断且无合约信息，无法认领持仓，请人工核对账户")
        await repo.transition_parent(pid, "OPEN_FAILED", ev.RECONCILE_MISMATCH)
        return

    short = -by_instr.get(sell_i, 0.0)
    long_ = by_instr.get(buy_i, 0.0)
    # 多余腿回滚（买先设计下通常是买腿多）
    excess = round(long_ - short, 10)
    if excess >= min_lot - EPS:
        res = await adapter.close_long_leg(ctx.ws, instrument=buy_i, qty=excess,
                                           min_lot=min_lot)
        long_ = round(long_ - res.closed_qty, 10)
        await ev.emit(repo.conn, ev.OPEN_FAILED_UNWOUND, level="WARN", parent_id=pid,
                      unwound=res.closed_qty, note="reconcile 回滚多余买腿")
    elif excess <= -(min_lot - EPS):
        res = await adapter.close_sell_leg_only(ctx.ws, sell_instrument=sell_i,
                                               qty=-excess, min_lot=min_lot)
        short = round(short - res.closed_qty, 10)
        await ev.emit(repo.conn, ev.OPEN_FAILED_UNWOUND, level="WARN", parent_id=pid,
                      unwound=res.closed_qty, note="reconcile 回滚多余卖腿")

    matched = round(min(short, long_), 10)
    if matched < min_lot - EPS:
        await repo.transition_parent(pid, "OPEN_FAILED", ev.RECONCILE_MISMATCH,
                                     note="reconcile: 交易所无等量持仓")
        return

    rebuilt = await _rebuild_open_from_trades(ctx, p, matched)
    if rebuilt:
        await repo.transition_parent(pid, "OPEN", ev.RECONCILE_MISMATCH,
                                     note=f"reconcile: 认领 {matched} 张，"
                                          f"净权利金按成交记录重建")
    else:
        # 重建失败：无法计算 BEP 就无法管理，安全起见把这笔认不回来的腿平掉
        await adapter.close_spread_both_legs(
            ctx.ws, sell_instrument=sell_i, buy_instrument=buy_i, qty=matched,
            min_lot=min_lot, n_exec_groups=cfg.execution.n_exec_groups)
        await repo.transition_parent(pid, "OPEN_FAILED", ev.RECONCILE_MISMATCH,
                                     note="reconcile: 成交记录重建失败，已平掉认领腿")


async def _rebuild_open_from_trades(ctx: Ctx, p: dict, matched: float) -> bool:
    """用 private/get_user_trades_by_instrument 重建两腿均价 → BEP/分组/OPEN 落库。"""
    from ..exchange import market_data
    from ..engine import bep as bep_mod, grouping
    repo, cfg = ctx.repo, ctx.cfg
    pid = p["id"]

    async def leg_avg(instrument: str, direction: str) -> Optional[float]:
        try:
            r = await ctx.ws.request("private/get_user_trades_by_instrument", {
                "instrument_name": instrument,
                "start_timestamp": p["created_at"],
                "count": 1000, "sorting": "asc"})
        except Exception:  # noqa: BLE001
            return None
        qty = cost = 0.0
        for t in r.get("trades", []):
            if t.get("direction") == direction:
                a = float(t.get("amount") or 0.0)
                qty += a
                cost += a * float(t.get("price") or 0.0)
        return (cost / qty) if qty > EPS else None

    sell_avg = await leg_avg(p["sell_instrument"], "sell")
    buy_avg = await leg_avg(p["buy_instrument"], "buy")
    if sell_avg is None or buy_avg is None:
        return False
    net_credit = sell_avg - buy_avg
    idx = await market_data.get_index_price(ctx.ws, cfg.strategy.currency)
    bep = cfg.debug.bep_override or bep_mod.bep_price(
        p["spread_type"], p["sell_strike"], net_credit, idx)
    await repo.fill_open_details(
        pid, sell_instrument=p["sell_instrument"], buy_instrument=p["buy_instrument"],
        expiry_ts=p["expiry_ts"] or 0, sell_strike=p["sell_strike"],
        buy_strike=p["buy_strike"], qty_total=matched, net_credit_btc=net_credit,
        open_index_price=idx, bep_price=bep)
    await repo.create_legs(pid, p["sell_instrument"], matched, sell_avg,
                           p["buy_instrument"], matched, buy_avg)
    payload = await repo.get_signal_payload(p["signal_id"]) or {}
    n = int(payload.get("group_split_count") or cfg.group_split_count_default)
    await repo.create_groups(pid, grouping.split_groups(matched, n, cfg.strategy.min_lot))
    await repo.ensure_hedge_row(pid)
    await ev.emit(repo.conn, ev.OPENED, parent_id=pid, qty=matched,
                  net_credit=net_credit, bep=bep, note="reconciled from trades")
    return True
