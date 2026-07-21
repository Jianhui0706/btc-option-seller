"""对冲引擎（规范第十~十三节）。

- BEP 门控：未触及 BEP 时 target_hedge=0，即使 |Delta| 超阈值也不对冲；
- delta_trigger = account_btc_equity × 2% × 10%；effective = max(trigger, 最小下单量)；
- 对冲量：target = -期权持仓Delta（中性）；差值 ≥ effective 才下单；
- 撤销两段式：触及撤销线 → RELEASE_PENDING（记 bar）→ 下一根完整 bar 复核仍在线内侧
  才归零平仓；若期间重新突破 BEP 则取消 pending 回到 ACTIVE。

scan_hedge() 由 3 分钟扫描调用（只做判断与入队）；handle_rebalance / handle_release
是命令处理器（真正下单）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..context import Ctx
from ..exchange import account, futures, market_data
from ..monitor import events as ev
from . import bep as bep_mod

log = logging.getLogger("v2.hedge")


@dataclass
class DeltaView:
    option_delta: float        # 账户全部期权 Delta（含跛腿买方，规范第十.3）
    futures_delta: float       # 对冲永续 Delta
    net_delta: float
    equity_btc: float
    index_price: float


@dataclass
class TradeHedgeParams:
    account_risk_pct: float
    trigger_pct: float
    call_release_pct: float
    put_release_pct: float


async def trade_hedge_params(ctx: Ctx, parent: dict) -> TradeHedgeParams:
    """读取开仓信号中的逐笔参数；未传时使用后端安全默认值。"""
    raw = await ctx.repo.get_signal_payload(parent["signal_id"]) or {}
    cfg = ctx.cfg.hedge
    return TradeHedgeParams(
        account_risk_pct=float(raw.get("delta_account_risk_pct") or
                               cfg.delta_account_risk_pct),
        trigger_pct=float(raw.get("delta_trigger_pct") or cfg.delta_trigger_pct),
        call_release_pct=float(raw.get("call_hedge_release_pct") or
                               cfg.call_release_pct),
        put_release_pct=float(raw.get("put_hedge_release_pct") or
                              cfg.put_release_pct),
    )


async def snapshot_delta(ctx: Ctx) -> DeltaView:
    cfg = ctx.cfg
    summ = await account.get_account_summary(ctx.ws, cfg.strategy.currency)
    opts = await account.get_option_positions(ctx.ws, cfg.strategy.currency)
    option_delta = sum(p.delta for p in opts)
    _, fut_delta = await futures.current_perp_position(ctx.ws, cfg.hedge.perp_instrument)
    idx = await market_data.get_index_price(ctx.ws, cfg.strategy.currency)
    return DeltaView(option_delta=option_delta, futures_delta=fut_delta,
                     net_delta=option_delta + fut_delta,
                     equity_btc=summ.equity_btc, index_price=idx)


def effective_trigger(ctx: Ctx, equity_btc: float, index_price: float,
                      account_risk_pct: float | None = None,
                      trigger_pct: float | None = None) -> float:
    cfg = ctx.cfg.hedge
    trigger = equity_btc * (account_risk_pct or cfg.delta_account_risk_pct) * \
        (trigger_pct or cfg.delta_trigger_pct)
    min_trade_delta = cfg.perp_contract_usd / index_price
    eff = max(trigger, min_trade_delta)
    if ctx.cfg.debug.trigger_override is not None:
        eff = ctx.cfg.debug.trigger_override
    return eff


async def scan_hedge(ctx: Ctx, parent: dict, bar_close: float, bar_ts: int) -> None:
    """扫描第 6~11 步：BEP 激活 / 撤销线 / Delta 管线 → 入队对冲命令。"""
    repo, cfg = ctx.repo, ctx.cfg
    pid = parent["id"]
    hedge = await repo.get_hedge(pid)
    if hedge is None:
        await repo.ensure_hedge_row(pid)
        hedge = await repo.get_hedge(pid)
    state = hedge["state"]
    spread_type = parent["spread_type"]
    params = await trade_hedge_params(ctx, parent)
    bep = ctx.cfg.debug.bep_override or parent["bep_price"]
    if not bep:
        return

    # 6. BEP 触及（已收盘 3min bar 收盘价）→ 激活
    if state == "HEDGE_INACTIVE":
        if bep_mod.bep_touched(spread_type, bar_close, bep):
            await repo.transition_hedge(pid, "HEDGE_ACTIVE", ev.BEP_HEDGE_ACTIVATED,
                                        bar_close=bar_close, bep=bep)
            state = "HEDGE_ACTIVE"
        else:
            return   # 未触及 BEP：target=0，不对冲（规范十一.3）

    # 7. 撤销线两段式
    released_zone = bep_mod.release_touched(
        spread_type, bar_close, bep,
        params.call_release_pct, params.put_release_pct)
    if state == "HEDGE_ACTIVE" and released_zone:
        await repo.transition_hedge(pid, "HEDGE_RELEASE_PENDING",
                                    ev.HEDGE_RELEASE_PENDING,
                                    bar_close=bar_close, bar_ts=bar_ts)
        await repo.set_release_pending_bar(pid, bar_ts)
        return   # 本 bar 不再做其他对冲动作（下一根完整 bar 确认）
    if state == "HEDGE_RELEASE_PENDING":
        pending_ts = hedge["release_pending_bar_ts"] or 0
        if bep_mod.bep_touched(spread_type, bar_close, bep):
            # 价格重新突破 BEP → 取消撤销，回到 ACTIVE
            await repo.transition_hedge(pid, "HEDGE_ACTIVE",
                                        ev.HEDGE_RELEASE_CANCELLED,
                                        bar_close=bar_close)
            state = "HEDGE_ACTIVE"
        elif bar_ts > pending_ts and released_zone:
            # 下一根完整 bar 确认仍在撤销线内侧 → 执行归零
            ctx.queue.put(ctx.queue.hedge_release(pid))
            return
        else:
            return
    if state in ("HEDGE_REBALANCING", "HEDGE_CLOSING"):
        return   # 有动作在途，等待其完成

    # 8~11. Delta 管线（仅 HEDGE_ACTIVE）
    view = await snapshot_delta(ctx)
    eff = effective_trigger(ctx, view.equity_btc, view.index_price,
                            params.account_risk_pct, params.trigger_pct)
    if abs(view.net_delta) < eff:
        return
    target_btc = -view.option_delta          # Delta 中性目标
    order_btc = target_btc - view.futures_delta
    if abs(order_btc) < eff:
        await repo.record_hedge_action(parent_id=pid, action="SKIP",
                                       net_delta_before=view.net_delta,
                                       skipped_reason="BELOW_TRIGGER")
        return
    ctx.queue.put(ctx.queue.hedge_rebalance(pid, order_btc, view.net_delta))


async def handle_rebalance(ctx: Ctx, payload: dict) -> None:
    """命令处理器：执行一次 BTC 永续对冲调整。"""
    pid = payload["parent_id"]
    repo, cfg = ctx.repo, ctx.cfg
    if ctx.hedge_frozen or await ctx.force_close_active():
        return
    parent = await repo.get_parent(pid)
    hedge = await repo.get_hedge(pid)
    if not parent or parent["state"] not in ("OPEN", "PARTIALLY_CLOSED"):
        return
    if not hedge or hedge["state"] != "HEDGE_ACTIVE":
        return

    # 重算（入队到执行之间行情可能已变）
    view = await snapshot_delta(ctx)
    params = await trade_hedge_params(ctx, parent)
    eff = effective_trigger(ctx, view.equity_btc, view.index_price,
                            params.account_risk_pct, params.trigger_pct)
    order_btc = -view.option_delta - view.futures_delta
    if abs(order_btc) < eff:
        await repo.record_hedge_action(parent_id=pid, action="SKIP",
                                       net_delta_before=view.net_delta,
                                       skipped_reason="BELOW_TRIGGER")
        return
    usd = round(order_btc * view.index_price / cfg.hedge.perp_contract_usd) \
        * cfg.hedge.perp_contract_usd
    if abs(usd) < cfg.hedge.perp_contract_usd - 1e-9:
        await repo.record_hedge_action(parent_id=pid, action="SKIP",
                                       net_delta_before=view.net_delta,
                                       skipped_reason="MIN_AMOUNT")
        return

    await repo.transition_hedge(pid, "HEDGE_REBALANCING", ev.DELTA_HEDGE_REBALANCE,
                                order_usd=usd, net_delta=view.net_delta)
    try:
        fill = await futures.perp_ioc(ctx.ws, cfg.hedge.perp_instrument, usd,
                                      ioc_ticks=cfg.hedge.ioc_ticks)
        _, fut_delta = await futures.current_perp_position(
            ctx.ws, cfg.hedge.perp_instrument)
        after = view.option_delta + fut_delta
        await repo.adjust_hedge_futures(pid, fill.filled_usd, view.index_price)
        await repo.record_hedge_action(
            parent_id=pid, action="REBALANCE",
            net_delta_before=view.net_delta, net_delta_after=after,
            order_usd=fill.filled_usd, fill_price=fill.avg_price, fee=fill.fee_btc)
        await repo.record_fill(parent_id=pid, group_id=None,
                               instrument=cfg.hedge.perp_instrument,
                               direction="buy" if fill.filled_usd > 0 else "sell",
                               qty=abs(fill.filled_usd), price=fill.avg_price,
                               fee=fill.fee_btc, phase="hedge")
    finally:
        h = await repo.get_hedge(pid)
        if h and h["state"] == "HEDGE_REBALANCING":
            await repo.transition_hedge(pid, "HEDGE_ACTIVE")


async def handle_release(ctx: Ctx, payload: dict) -> None:
    """命令处理器：对冲撤销——BTC 期货目标归零并回到 HEDGE_INACTIVE。"""
    pid = payload["parent_id"]
    repo, cfg = ctx.repo, ctx.cfg
    hedge = await repo.get_hedge(pid)
    # 可重入：首次从 RELEASE_PENDING 进入；归零未完成的重试从 HEDGE_CLOSING 进入
    if not hedge or hedge["state"] not in ("HEDGE_RELEASE_PENDING", "HEDGE_CLOSING"):
        return
    if hedge["state"] == "HEDGE_RELEASE_PENDING":
        await repo.transition_hedge(pid, "HEDGE_CLOSING", ev.HEDGE_TARGET_ZERO)
    remaining = await close_managed_hedge(ctx, pid, action="RELEASE")
    if abs(remaining) < cfg.hedge.perp_contract_usd - 1e-9:
        await repo.transition_hedge(pid, "HEDGE_INACTIVE")
    else:
        log.error("策略对冲归零未完成，剩余 %.0f USD，保持 HEDGE_CLOSING 下轮重试", remaining)
        ctx.queue.put(ctx.queue.hedge_release(pid))


async def close_managed_hedge(ctx: Ctx, parent_id: int, *,
                              action: str = "CLOSE", max_rounds: int = 5) -> float:
    """只反向冲销本策略实际建立的期货数量，保留账户原有/人工期货仓位。"""
    repo, cfg = ctx.repo, ctx.cfg
    for _ in range(max_rounds):
        row = await repo.get_hedge(parent_id)
        managed_usd = float((row or {}).get("futures_usd") or 0.0)
        if abs(managed_usd) < cfg.hedge.perp_contract_usd - 1e-9:
            await repo.set_hedge_futures(parent_id, 0.0, 0.0)
            return 0.0
        fill = await futures.perp_ioc(
            ctx.ws, cfg.hedge.perp_instrument, -managed_usd,
            ioc_ticks=cfg.hedge.ioc_ticks, reduce_only=False)
        if abs(fill.filled_usd) < cfg.hedge.perp_contract_usd - 1e-9:
            break
        index_price = fill.avg_price or await market_data.get_index_price(
            ctx.ws, cfg.strategy.currency)
        remaining, _ = await repo.adjust_hedge_futures(
            parent_id, fill.filled_usd, index_price)
        await repo.record_hedge_action(
            parent_id=parent_id, action=action, order_usd=fill.filled_usd,
            fill_price=fill.avg_price, fee=fill.fee_btc)
        await repo.record_fill(
            parent_id=parent_id, group_id=None,
            instrument=cfg.hedge.perp_instrument,
            direction="buy" if fill.filled_usd > 0 else "sell",
            qty=abs(fill.filled_usd), price=fill.avg_price,
            fee=fill.fee_btc, phase="hedge")
        if abs(remaining) < cfg.hedge.perp_contract_usd - 1e-9:
            await repo.set_hedge_futures(parent_id, 0.0, 0.0)
            return 0.0
    row = await repo.get_hedge(parent_id)
    return float((row or {}).get("futures_usd") or 0.0)
