"""正常平仓（规范第八、十四节）。

执行顺序：冻结 Delta 调整 → 撤对冲挂单 → 平期权腿（默认双腿全平；config 可切
只平卖腿保留买腿）→ 卖腿确认后 BTC 目标归零 → 平期货对冲 → 核对 → CLOSED。
"""

from __future__ import annotations

import asyncio
import logging

from ..config import DATA_DIR
from ..context import Ctx
from ..exchange import account
from ..execution import adapter
from ..monitor import events as ev
from . import hedge as hedge_engine

log = logging.getLogger("v2.close")

MANAGED_OPTION_ORDER_LABEL_PREFIXES = (
    "spread-batch-",
    "spread-leg-",
    "spread-combo-",
    "v2-single-leg",
)


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

    # Do not place replacement close orders while an older GTC order for the
    # same legs may still be live.  A lost maker order can fill much later and
    # invert a spread that the database already considers closed.
    if not await cancel_managed_option_orders(ctx, parent):
        return

    # 3. 平期权腿。交易所仓位是事实源：先尽量按价差等量平仓，随后按实际
    # 残余方向逐腿归零。部分成交重试不再按数据库原始总量重复下单，已经
    # 反转方向的残余仓位也会按正确方向自动清理。
    keep_buy = cfg.exit.keep_buy_leg_on_normal_exit
    min_lot = cfg.strategy.min_lot
    reserved_sizes = await _other_managed_position_sizes(ctx, parent)
    parent_groups = await repo.groups_of(pid)
    parent_remaining = max(
        0.0,
        sum(float(group.get("qty") or 0.0) -
            float(group.get("closed_qty") or 0.0)
            for group in parent_groups)
        if parent_groups else float(parent.get("qty_total") or 0.0),
    )
    positions = await account.get_option_positions(
        ctx.ws, cfg.strategy.currency)
    by_instrument = {p.instrument: p.size for p in positions}
    sell_size = by_instrument.get(parent["sell_instrument"], 0.0)
    buy_size = by_instrument.get(parent["buy_instrument"], 0.0)

    if keep_buy:
        own_short = min(
            parent_remaining,
            max(0.0, -(sell_size - reserved_sizes.get(
                parent["sell_instrument"], 0.0))),
        )
        if own_short >= min_lot - 1e-9:
            res = await adapter.close_sell_leg_only(
                ctx.ws,
                sell_instrument=parent["sell_instrument"],
                qty=own_short,
                min_lot=min_lot,
            )
            await _record_close_result(
                ctx, parent, parent["sell_instrument"], "buy", res)
    else:
        matched = min(parent_remaining, max(0.0, -sell_size),
                      max(0.0, buy_size))
        if matched >= min_lot - 1e-9:
            res = await adapter.close_spread_both_legs(
                ctx.ws,
                sell_instrument=parent["sell_instrument"],
                buy_instrument=parent["buy_instrument"],
                qty=matched,
                min_lot=min_lot,
                n_exec_groups=cfg.execution.n_exec_groups,
                report_dir=str(DATA_DIR / "exec_reports"),
            )
            if res.closed_qty > 0:
                await repo.add_leg_closed(
                    pid, "SELL_LEG", res.closed_qty)
                await repo.add_leg_closed(
                    pid, "BUY_LEG", res.closed_qty)
                await repo.record_fill(
                    parent_id=pid, group_id=None,
                    instrument=parent["sell_instrument"],
                    direction="buy", qty=res.closed_qty,
                    price=res.buy_back_avg, fee=0.0, phase="close")
                await repo.record_fill(
                    parent_id=pid, group_id=None,
                    instrument=parent["buy_instrument"],
                    direction="sell", qty=res.closed_qty,
                    price=res.sell_out_avg, fee=0.0, phase="close")
            await repo.set_liquidity_role(
                pid, "close", res.liquidity_role)

    # Account-level verification also catches orders left by an older process
    # or retry that the current executor instance does not know about.
    if not await cancel_managed_option_orders(ctx, parent):
        return

    recovery = await _flatten_managed_option_residuals(
        ctx, parent, keep_buy=keep_buy, reserved_sizes=reserved_sizes)
    if recovery:
        await ev.emit(
            repo.conn, ev.CLOSE_RESIDUAL_RECOVERY, level="WARN",
            parent_id=pid, actions=recovery)

    remaining_positions = await account.get_option_positions(
        ctx.ws, cfg.strategy.currency)
    remaining_plan = residual_close_plan(
        parent, remaining_positions, keep_buy=keep_buy, min_lot=min_lot,
        reserved_sizes=reserved_sizes)
    if remaining_plan:
        long_only = all(
            item["role"] == "BUY_LEG" and item["direction"] == "sell"
            for item in remaining_plan)
        if long_only:
            await repo.set_close_reason(pid, "RESIDUAL_LONG_DECISION_REQUIRED")
            await ev.emit(
                repo.conn, ev.RESIDUAL_LONG_DECISION_REQUIRED, level="WARN",
                parent_id=pid, remaining=remaining_plan,
                note="保护买腿未能成交，等待人工选择再次平仓或确认隔离")
            log.warning("parent %s 只剩保护买腿，等待人工处理: %s", pid, remaining_plan)
            return
        await ev.emit(
            repo.conn, ev.CLOSE_RESIDUAL_RETRY, level="ERROR",
            parent_id=pid, remaining=remaining_plan)
        log.error(
            "parent %s 期权腿仍未归零，保持 CLOSING，下一轮扫描重试: %s",
            pid, remaining_plan)
        return

    await repo.mark_leg_closed(pid, "SELL_LEG")
    if not keep_buy:
        await repo.mark_leg_closed(pid, "BUY_LEG")

    # 4. 两条受管期权腿核对通过后，BTC 期货目标归零（reduce_only IOC）
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
        return
    buy_cost, sell_revenue = await repo.close_cashflows(pid)
    qty_total = float(parent["qty_total"] or 0.0)
    close_net = (
        (buy_cost - sell_revenue) / qty_total
        if qty_total > 0 else 0.0
    )
    per_contract_pnl = (parent["net_credit_btc"] or 0.0) - close_net
    for g in await repo.groups_of(pid):
        if g["state"] in ("CLOSED", "FORCE_CLOSED"):
            continue
        await repo.transition_group(g["id"], "CLOSED",
                                    closed_qty=g["qty"],
                                    realized_pnl=per_contract_pnl * g["qty"],
                                    reason=reason)
    await repo.transition_parent(pid, "CLOSED", ev.NORMAL_CLOSE,
                                 reason=reason, closed_qty=qty_total,
                                 per_contract_pnl=round(per_contract_pnl, 6),
                                 execution_complete=True)
    active = await repo.active_parents() if hasattr(repo, "active_parents") else []
    if not any(p["state"] == "CLOSING" for p in active):
        ctx.hedge_frozen = False
    log.info("parent %s CLOSED (%s): closed=%.1f pnl/张=%.4f",
             pid, reason, qty_total, per_contract_pnl)


async def cancel_managed_option_orders(ctx: Ctx, parent: dict) -> bool:
    """Cancel and verify all live orders for one parent's option instruments."""
    instruments = {
        item for item in (
            parent.get("sell_instrument"), parent.get("buy_instrument"))
        if item
    }
    if not instruments:
        return True

    try:
        open_orders = await account.get_open_orders(
            ctx.ws, ctx.cfg.strategy.currency)
    except Exception as exc:  # noqa: BLE001 - cannot prove safety
        await ev.emit(
            ctx.repo.conn, ev.CLOSE_RESIDUAL_RETRY, level="ERROR",
            parent_id=parent["id"],
            note="managed option order verification failed",
            error=str(exc))
        return False

    def is_managed(order: dict) -> bool:
        return (
            order.get("instrument_name") in instruments
            and str(order.get("label") or "").startswith(
                MANAGED_OPTION_ORDER_LABEL_PREFIXES)
        )

    pending = [order for order in (open_orders or []) if is_managed(order)]
    cancel_errors = []
    for attempt in range(1, 4):
        if not pending:
            return True
        for order in pending:
            order_id = order.get("order_id")
            if not order_id:
                continue
            try:
                await ctx.ws.request(
                    "private/cancel", {"order_id": order_id})
            except Exception as exc:  # noqa: BLE001 - verify below is truth
                cancel_errors.append({
                    "order_id": order_id,
                    "instrument": order.get("instrument_name"),
                    "error": str(exc),
                })
        try:
            open_orders = await account.get_open_orders(
                ctx.ws, ctx.cfg.strategy.currency)
        except Exception as exc:  # noqa: BLE001 - cannot prove safety
            cancel_errors.append({"verification_error": str(exc)})
            open_orders = pending
        pending = [order for order in (open_orders or []) if is_managed(order)]
        if not pending:
            return True
        if attempt < 3:
            await asyncio.sleep(0.25 * attempt)

    remaining = [
        {
            "order_id": order.get("order_id"),
            "instrument": order.get("instrument_name"),
            "direction": order.get("direction"),
            "amount": order.get("amount"),
            "filled_amount": order.get("filled_amount"),
            "label": order.get("label"),
        }
        for order in pending
    ]
    if remaining:
        await ev.emit(
            ctx.repo.conn, ev.CLOSE_RESIDUAL_RETRY, level="ERROR",
            parent_id=parent["id"],
            note="managed option orders still live after cancellation",
            remaining_orders=remaining, cancel_errors=cancel_errors)
        log.error(
            "parent %s still has live managed option orders: %s",
            parent["id"], remaining)
        return False
    return False


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
        await repo.set_liquidity_role(
            parent["id"], "close", res.liquidity_role)

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
    await repo.set_close_reason(parent["id"], reason)
    await handle_normal_close(ctx, {
        "parent_id": parent["id"],
        "reason": reason,
    })


def residual_close_plan(
    parent: dict,
    positions: list,
    *,
    keep_buy: bool,
    min_lot: float,
    reserved_sizes: dict[str, float] | None = None,
) -> list[dict]:
    """Build direction-aware orders for managed option residue.

    Full exits flatten both managed instruments regardless of whether a
    partial/retried close has inverted their direction.  Sell-leg-only exits
    preserve a positive protective buy leg, but still repair a negative one.
    """
    reserves = reserved_sizes or {}
    by_instrument = {
        p.instrument: float(p.size) - float(reserves.get(p.instrument, 0.0))
        for p in positions
    }
    plan: list[dict] = []
    legs = [
        ("SELL_LEG", parent["sell_instrument"]),
        ("BUY_LEG", parent["buy_instrument"]),
    ]
    seen: set[str] = set()
    for role, instrument in legs:
        if not instrument or instrument in seen:
            continue
        seen.add(instrument)
        size = by_instrument.get(instrument, 0.0)
        if keep_buy and role == "BUY_LEG" and size >= 0:
            continue
        qty = round(abs(size), 10)
        if qty < min_lot - 1e-9:
            continue
        plan.append({
            "role": role,
            "instrument": instrument,
            "position_size": size,
            "direction": "buy" if size < 0 else "sell",
            "qty": qty,
        })
    return plan


async def _record_close_result(
    ctx: Ctx,
    parent: dict,
    instrument: str,
    direction: str,
    result,
) -> None:
    if hasattr(ctx.repo, "record_execution_attempts"):
        await ctx.repo.record_execution_attempts(parent["id"], getattr(result, "attempts", []))
    if result.closed_qty <= 0:
        return
    role = (
        "SELL_LEG"
        if instrument == parent["sell_instrument"]
        else "BUY_LEG"
    )
    expected_direction = "buy" if role == "SELL_LEG" else "sell"
    if direction == expected_direction:
        await ctx.repo.add_leg_closed(
            parent["id"], role, result.closed_qty)
    await ctx.repo.record_fill(
        parent_id=parent["id"], group_id=None,
        instrument=instrument, direction=direction,
        qty=result.closed_qty,
        price=result.buy_back_avg or result.sell_out_avg,
        fee=0.0, phase="close")
    await ctx.repo.set_liquidity_role(
        parent["id"], "close", result.liquidity_role)


async def _flatten_managed_option_residuals(
    ctx: Ctx,
    parent: dict,
    *,
    keep_buy: bool,
    reserved_sizes: dict[str, float] | None = None,
) -> list[dict]:
    positions = await account.get_option_positions(
        ctx.ws, ctx.cfg.strategy.currency)
    plan = residual_close_plan(
        parent, positions, keep_buy=keep_buy,
        min_lot=ctx.cfg.strategy.min_lot,
        reserved_sizes=reserved_sizes)
    actions: list[dict] = []
    for item in plan:
        if item["direction"] == "buy":
            result = await adapter.close_sell_leg_only(
                ctx.ws,
                sell_instrument=item["instrument"],
                qty=item["qty"],
                min_lot=ctx.cfg.strategy.min_lot,
            )
        else:
            result = await adapter.close_long_leg(
                ctx.ws,
                instrument=item["instrument"],
                qty=item["qty"],
                min_lot=ctx.cfg.strategy.min_lot,
            )
        await _record_close_result(
            ctx, parent, item["instrument"], item["direction"], result)
        actions.append({
            **item,
            "filled": result.closed_qty,
            "complete": result.complete,
        })
    return actions


async def _other_managed_position_sizes(ctx: Ctx, parent: dict) -> dict[str, float]:
    """Exchange positions are netted; reserve quantities owned by other parents."""
    if hasattr(ctx.repo, "active_parents"):
        parents = await ctx.repo.active_parents()
    elif hasattr(ctx.repo, "open_parents"):
        parents = await ctx.repo.open_parents()
    else:
        return {}
    reserved: dict[str, float] = {}
    for other in parents:
        if other["id"] == parent["id"]:
            continue
        if not other.get("sell_instrument") or not other.get("buy_instrument"):
            continue
        groups = await ctx.repo.groups_of(other["id"])
        remaining = (
            sum(max(0.0, float(group.get("qty") or 0.0) -
                    float(group.get("closed_qty") or 0.0))
                for group in groups)
            if groups else float(other.get("qty_total") or 0.0)
        )
        if remaining <= 0:
            continue
        sell = other.get("sell_instrument")
        buy = other.get("buy_instrument")
        if sell:
            reserved[sell] = reserved.get(sell, 0.0) - remaining
        if buy:
            reserved[buy] = reserved.get(buy, 0.0) + remaining
    return reserved
