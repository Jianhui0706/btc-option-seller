"""正常平仓（规范第八、十四节）。

执行顺序：冻结 Delta 调整 → 撤对冲挂单 → 平期权腿（默认双腿全平；config 可切
只平卖腿保留买腿）→ 卖腿确认后 BTC 目标归零 → 平期货对冲 → 核对 → CLOSED。
"""

from __future__ import annotations

import logging

from ..config import DATA_DIR
from ..context import Ctx
from ..exchange import account
from ..execution import adapter
from ..monitor import events as ev
from . import hedge as hedge_engine

log = logging.getLogger("v2.close")


async def handle_normal_close(ctx: Ctx, payload: dict) -> None:
    pid: int = payload["parent_id"]
    reason: str = payload.get("reason", "NORMAL")
    repo, cfg = ctx.repo, ctx.cfg
    parent = await repo.get_parent(pid)
    if not parent or parent["state"] not in ("OPEN", "PARTIALLY_CLOSED", "CLOSING"):
        return

    # 1. 冻结新的 Delta 调整（同 bar 内后续对冲命令直接丢弃）
    ctx.hedge_frozen = True
    if parent["state"] != "CLOSING":
        await repo.transition_parent(pid, "CLOSING", ev.NORMAL_CLOSE, reason=reason)
        await repo.set_close_reason(pid, reason)

    # 2. 撤销尚未成交的对冲挂单（对冲用 IOC 不留挂单，此处为防御性撤单）
    try:
        await ctx.ws.request("private/cancel_all_by_instrument",
                             {"instrument_name": cfg.hedge.perp_instrument})
    except Exception:  # noqa: BLE001
        pass

    # 3. 平期权腿
    remaining = _remaining_qty(parent, await repo.groups_of(pid))
    keep_buy = cfg.exit.keep_buy_leg_on_normal_exit
    close_net = 0.0    # 平仓净支出（BTC/张，正=付出）
    ok = True
    if remaining >= cfg.strategy.min_lot:
        if keep_buy:
            res = await adapter.close_sell_leg_only(
                ctx.ws, sell_instrument=parent["sell_instrument"], qty=remaining,
                min_lot=cfg.strategy.min_lot)
            close_net = res.buy_back_avg
            await repo.add_leg_closed(pid, "SELL_LEG", res.closed_qty)
            await repo.record_fill(parent_id=pid, group_id=None,
                                   instrument=parent["sell_instrument"],
                                   direction="buy", qty=res.closed_qty,
                                   price=res.buy_back_avg, fee=0.0, phase="close")
        else:
            res = await adapter.close_spread_both_legs(
                ctx.ws, sell_instrument=parent["sell_instrument"],
                buy_instrument=parent["buy_instrument"], qty=remaining,
                min_lot=cfg.strategy.min_lot,
                n_exec_groups=cfg.execution.n_exec_groups,
                report_dir=str(DATA_DIR / "exec_reports"))
            close_net = res.buy_back_avg - res.sell_out_avg
            await repo.add_leg_closed(pid, "SELL_LEG", res.closed_qty)
            await repo.add_leg_closed(pid, "BUY_LEG", res.closed_qty)
            await repo.record_fill(parent_id=pid, group_id=None,
                                   instrument=parent["sell_instrument"],
                                   direction="buy", qty=res.closed_qty,
                                   price=res.buy_back_avg, fee=0.0, phase="close")
            await repo.record_fill(parent_id=pid, group_id=None,
                                   instrument=parent["buy_instrument"],
                                   direction="sell", qty=res.closed_qty,
                                   price=res.sell_out_avg, fee=0.0, phase="close")
        ok = res.complete
        closed_qty = res.closed_qty
    else:
        closed_qty = 0.0

    # 4. 卖腿确认后：BTC 期货目标归零（reduce_only IOC）
    sell_pos = await _short_size(ctx, parent["sell_instrument"])
    if sell_pos > 1e-9:
        log.error("卖腿仍有 %.1f 张未平，先不平期货对冲，下轮扫描重试", sell_pos)
        ctx.queue.put(ctx.queue.normal_close(pid, reason))
        return
    size_usd = await hedge_engine.close_managed_hedge(ctx, pid, action="CLOSE")
    hedge_row = await repo.get_hedge(pid)
    if hedge_row:
        if hedge_row["state"] != "HEDGE_INACTIVE":
            # 任何在途对冲状态在平仓时收敛到 INACTIVE
            for step in ("HEDGE_CLOSING", "HEDGE_INACTIVE"):
                try:
                    await repo.transition_hedge(pid, step, ev.HEDGE_TARGET_ZERO)
                except Exception:  # noqa: BLE001 - 已在目标态则跳过
                    pass

    # 5. 核对 + 子组结算
    if abs(size_usd) >= cfg.hedge.perp_contract_usd - 1e-9:
        log.error("期货对冲未归零(%.0f USD)，保持 CLOSING 下轮重试", size_usd)
        ctx.queue.put(ctx.queue.normal_close(pid, reason))
        return
    per_contract_pnl = (parent["net_credit_btc"] or 0.0) - close_net
    for g in await repo.groups_of(pid):
        if g["state"] in ("CLOSED", "FORCE_CLOSED"):
            continue
        await repo.transition_group(g["id"], "CLOSED",
                                    closed_qty=g["qty"],
                                    realized_pnl=per_contract_pnl * g["qty"],
                                    reason=reason)
    await repo.transition_parent(pid, "CLOSED", ev.NORMAL_CLOSE,
                                 reason=reason, closed_qty=closed_qty,
                                 per_contract_pnl=round(per_contract_pnl, 6),
                                 execution_complete=ok)
    log.info("parent %s CLOSED (%s): closed=%.1f pnl/张=%.4f",
             pid, reason, closed_qty, per_contract_pnl)


async def handle_group_close(ctx: Ctx, payload: dict) -> None:
    """独立平掉一个管理子组；没有文档化自动阶梯时由人工明确触发。"""
    group_id = int(payload["group_id"])
    reason = payload.get("reason", "MANUAL_GROUP")
    repo, cfg = ctx.repo, ctx.cfg
    group = await repo.get_group(group_id)
    if not group or group["state"] != "OPEN":
        return
    parent = await repo.get_parent(group["parent_id"])
    if not parent or parent["state"] not in ("OPEN", "PARTIALLY_CLOSED"):
        return

    qty = round((group["qty"] or 0.0) - (group["closed_qty"] or 0.0), 10)
    if qty < cfg.strategy.min_lot:
        return
    ctx.hedge_frozen = True
    await repo.transition_group(group_id, "CLOSING", reason=reason)
    try:
        res = await adapter.close_spread_both_legs(
            ctx.ws, sell_instrument=parent["sell_instrument"],
            buy_instrument=parent["buy_instrument"], qty=qty,
            min_lot=cfg.strategy.min_lot,
            n_exec_groups=cfg.execution.n_exec_groups,
            report_dir=str(DATA_DIR / "exec_reports"))
    except Exception:
        await repo.transition_group(group_id, "OPEN", reason="EXECUTION_ERROR")
        raise

    closed_total = round((group["closed_qty"] or 0.0) + res.closed_qty, 10)
    close_net = res.buy_back_avg - res.sell_out_avg
    pnl = ((parent["net_credit_btc"] or 0.0) - close_net) * res.closed_qty
    if res.closed_qty > 0:
        await repo.add_leg_closed(parent["id"], "SELL_LEG", res.closed_qty)
        await repo.add_leg_closed(parent["id"], "BUY_LEG", res.closed_qty)
        await repo.record_fill(
            parent_id=parent["id"], group_id=group_id,
            instrument=parent["sell_instrument"], direction="buy",
            qty=res.closed_qty, price=res.buy_back_avg, fee=0.0, phase="close")
        await repo.record_fill(
            parent_id=parent["id"], group_id=group_id,
            instrument=parent["buy_instrument"], direction="sell",
            qty=res.closed_qty, price=res.sell_out_avg, fee=0.0, phase="close")

    if not res.complete or closed_total < group["qty"] - cfg.strategy.min_lot / 2:
        await repo.transition_group(
            group_id, "OPEN", closed_qty=closed_total,
            realized_pnl=(group["realized_pnl_btc"] or 0.0) + pnl,
            reason="PARTIAL_EXECUTION")
        return

    await repo.transition_group(
        group_id, "CLOSED", closed_qty=group["qty"],
        realized_pnl=(group["realized_pnl_btc"] or 0.0) + pnl, reason=reason)
    groups = await repo.groups_of(parent["id"])
    if any(g["state"] not in ("CLOSED", "FORCE_CLOSED") for g in groups):
        if parent["state"] == "OPEN":
            await repo.transition_parent(parent["id"], "PARTIALLY_CLOSED",
                                         ev.NORMAL_CLOSE, reason=reason,
                                         group_id=group_id)
        return

    await repo.transition_parent(parent["id"], "CLOSING", ev.NORMAL_CLOSE,
                                 reason=reason)
    remaining_hedge = await hedge_engine.close_managed_hedge(
        ctx, parent["id"], action="CLOSE")
    if abs(remaining_hedge) >= cfg.hedge.perp_contract_usd - 1e-9:
        ctx.queue.put(ctx.queue.normal_close(parent["id"], reason))
        return
    await repo.transition_parent(parent["id"], "CLOSED", ev.NORMAL_CLOSE,
                                 reason=reason)


def _remaining_qty(parent: dict, groups: list[dict]) -> float:
    total = parent["qty_total"] or 0.0
    closed = sum(g["closed_qty"] or 0.0 for g in groups)
    return round(max(0.0, total - closed), 10)


async def _short_size(ctx: Ctx, instrument: str) -> float:
    opts = await account.get_option_positions(ctx.ws, ctx.cfg.strategy.currency)
    for p in opts:
        if p.instrument == instrument and p.size < 0:
            return -p.size
    return 0.0
