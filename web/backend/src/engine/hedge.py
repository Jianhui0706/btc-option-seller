"""对冲引擎（规范第十~十三节）。

- 提前门控：实时指数价未走到开仓指数价至 BEP 的配置比例时，即使 |Delta| 超阈值也不对冲；
- delta_trigger = account_btc_equity × 2% × 10%；effective = max(trigger, 最小下单量)；
- 对冲量：target = -期权持仓Delta（中性）；差值 ≥ effective 才下单；
- 撤销两段式：触及撤销线 → RELEASE_PENDING（记 bar）→ 下一根完整 bar 复核仍在线内侧
  才归零平仓；若期间重新突破提前激活价则取消 pending 回到 ACTIVE。

scan_hedge() 由实时指数监控和 3 分钟兜底扫描调用（只做判断与入队）；
handle_rebalance / handle_release 是命令处理器（真正下单）。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass

from ..context import Ctx
from ..exchange import account, futures, market_data
from ..execution import adapter as execution_adapter
from ..monitor import events as ev
from .. import runtime_settings
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
class PortfolioHedgeView:
    """所有已激活策略的虚拟 Delta 目标；实际仍由一个账户永续净仓执行。"""
    option_delta: float
    futures_delta: float
    net_delta: float
    equity_btc: float
    index_price: float
    controller_parent_id: int
    target_usd_by_parent: dict[int, float]
    managed_usd_by_parent: dict[int, float]
    last_rebalance_at: int | None


@dataclass
class TradeHedgeParams:
    account_risk_pct: float
    trigger_pct: float
    call_release_pct: float
    put_release_pct: float
    release_recovery_pct: float = 0.50


@dataclass(frozen=True)
class HedgePriceLevels:
    open_index_price: float | None
    bep: float
    activation_progress: float
    activation_offset: float
    activation_price: float
    release_line: float


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
        release_recovery_pct=runtime_settings.effective_release_recovery_pct(ctx),
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
                      trigger_pct: float | None = None,
                      outer_pct: float | None = None) -> float:
    cfg = ctx.cfg.hedge
    legacy_trigger = equity_btc * (account_risk_pct or cfg.delta_account_risk_pct) * \
        (trigger_pct or cfg.delta_trigger_pct)
    configured_outer = (outer_pct if outer_pct is not None else
                        getattr(cfg, "rebalance_outer_equity_pct", None))
    trigger = max(legacy_trigger, equity_btc * float(configured_outer)) \
        if configured_outer is not None else legacy_trigger
    min_trade_delta = cfg.perp_contract_usd / index_price
    eff = max(trigger, min_trade_delta)
    if ctx.cfg.debug.trigger_override is not None:
        eff = ctx.cfg.debug.trigger_override
    return eff


def inner_trigger(ctx: Ctx, outer: float) -> float:
    return outer * float(getattr(ctx.cfg.hedge, "rebalance_inner_ratio", 0.0))


def rebalance_order_btc(net_delta: float, outer: float, inner: float) -> float:
    """外层阈值只负责触发；触发后目标为账户 Delta 中性。"""
    if abs(net_delta) < outer:
        return 0.0
    return -net_delta


def contract_usd(amount: float, lot: float) -> float:
    """把永续美元数量归整为交易所合约面值的整数倍。"""
    if lot <= 0:
        raise ValueError("perp contract size must be positive")
    rounded = round(float(amount) / lot) * lot
    return 0.0 if abs(rounded) < lot / 2 else float(rounded)


def _rebalance_command(ctx: Ctx, parent_id: int, order_btc: float,
                       net_delta: float, *, degraded_retry: bool = False,
                       spread_types: list[str] | None = None,
                       grouped: bool = False):
    """兼容轻量测试替身和旧队列适配器。"""
    try:
        return ctx.queue.hedge_rebalance(
            parent_id, order_btc, net_delta,
            degraded_retry=degraded_retry, spread_types=spread_types,
            grouped=grouped)
    except TypeError:
        try:
            return ctx.queue.hedge_rebalance(
                parent_id, order_btc, net_delta,
                degraded_retry=degraded_retry, spread_types=spread_types)
        except TypeError:
            return ctx.queue.hedge_rebalance(
                parent_id, order_btc, net_delta,
                degraded_retry=degraded_retry)


def _release_command(ctx: Ctx, parent_id: int, *, manual: bool = False,
                     grouped: bool = False):
    """兼容轻量测试替身和旧队列适配器。"""
    try:
        return ctx.queue.hedge_release(
            parent_id, manual=manual, grouped=grouped)
    except TypeError:
        try:
            return ctx.queue.hedge_release(parent_id, manual=manual)
        except TypeError:
            return ctx.queue.hedge_release(parent_id)


def neutral_tolerance(ctx: Ctx, index_price: float) -> float:
    """一个永续合约以内视为可执行精度下的 Delta 中性。"""
    if index_price <= 0:
        return 0.0
    return float(ctx.cfg.hedge.perp_contract_usd) / index_price


def parent_option_delta(parent: dict, opts: list) -> float:
    instruments = {parent.get("sell_instrument"), parent.get("buy_instrument")}
    return sum(float(p.delta) for p in opts
               if getattr(p, "instrument", None) in instruments)


ACTIVE_TARGET_STATES = {
    "HEDGE_ACTIVE", "HEDGE_REBALANCING", "HEDGE_RETRYING",
    "HEDGE_DEGRADED",
}

REBALANCE_BLOCKING_STATES = {"HEDGE_RELEASE_PENDING", "HEDGE_CLOSING"}


def manual_hedge_attempt_waits(cfg) -> tuple[float, float, float]:
    """一键开/关对冲的两档 Maker 等待；A3 为即时 IOC。"""
    execution = getattr(cfg, "execution", None)
    wait = float(getattr(execution, "manual_hedge_maker_wait_sec", 10.0))
    return wait, wait, 0.0


def _rearm_state_key(parent_id: int) -> str:
    return f"hedge.manual_rearm_after.{int(parent_id)}"


async def set_manual_rearm_cooldown(ctx: Ctx, parent_id: int) -> int:
    """人工归零完成后设置自动重开冷却，并持久化以跨进程重启。"""
    seconds = float(getattr(ctx.cfg.hedge, "manual_rearm_sec", 10.0))
    rearm_after = market_data.now_ms() + max(0, int(seconds * 1000))
    cache = getattr(ctx, "hedge_rearm_after", None)
    if not isinstance(cache, dict):
        cache = {}
        ctx.hedge_rearm_after = cache
    cache[int(parent_id)] = rearm_after
    setter = getattr(ctx.repo, "set_state", None)
    if callable(setter):
        await setter(_rearm_state_key(parent_id), str(rearm_after))
    return rearm_after


async def manual_rearm_blocked(ctx: Ctx, parent_id: int,
                               now_ms: int | None = None) -> bool:
    """返回人工取消后的 10 秒自动重开冷却是否仍然生效。"""
    pid = int(parent_id)
    cache = getattr(ctx, "hedge_rearm_after", None)
    if not isinstance(cache, dict):
        cache = {}
        ctx.hedge_rearm_after = cache
    rearm_after = int(cache.get(pid) or 0)
    if not rearm_after:
        getter = getattr(ctx.repo, "get_state", None)
        raw = await getter(_rearm_state_key(pid)) if callable(getter) else None
        rearm_after = int(raw) if raw not in (None, "") else 0
        if rearm_after:
            cache[pid] = rearm_after
    now = market_data.now_ms() if now_ms is None else int(now_ms)
    if rearm_after > now:
        return True
    if rearm_after:
        cache.pop(pid, None)
        setter = getattr(ctx.repo, "set_state", None)
        if callable(setter):
            await setter(_rearm_state_key(pid), "")
    return False


async def portfolio_hedge_view(ctx: Ctx, *, parents: list[dict] | None = None,
                               opts: list | None = None,
                               summary=None, index_price: float | None = None,
                               spread_types: set[str] | None = None
                               ) -> PortfolioHedgeView | None:
    """逐策略计算虚拟目标，再合成账户唯一的 BTC-PERPETUAL 调仓目标。"""
    cfg = ctx.cfg
    parents = parents if parents is not None else await ctx.repo.open_parents()
    opts = opts if opts is not None else await account.get_option_positions(
        ctx.ws, cfg.strategy.currency)
    summary = summary or await account.get_account_summary(
        ctx.ws, cfg.strategy.currency)
    index_price = index_price or await market_data.get_index_price(
        ctx.ws, cfg.strategy.currency)
    lot = float(cfg.hedge.perp_contract_usd)
    target_by_parent: dict[int, float] = {}
    managed_by_parent: dict[int, float] = {}
    option_delta = 0.0
    last_rebalance_at = None
    active_ids: list[int] = []
    for parent in parents:
        if spread_types is not None and parent.get("spread_type") not in spread_types:
            continue
        row = await ctx.repo.get_hedge(parent["id"])
        if not row:
            continue
        managed = float(row.get("futures_usd") or 0.0)
        managed_by_parent[parent["id"]] = managed
        if row.get("last_rebalance_at"):
            last_rebalance_at = max(last_rebalance_at or 0,
                                    int(row["last_rebalance_at"]))
        if row.get("state") not in ACTIVE_TARGET_STATES:
            continue
        delta = parent_option_delta(parent, opts)
        option_delta += delta
        target_by_parent[parent["id"]] = round(
            (-delta * index_price) / lot) * lot
        active_ids.append(parent["id"])
    if not active_ids:
        return None
    managed_total = sum(managed_by_parent.values())
    futures_delta = managed_total / index_price if index_price > 0 else 0.0
    return PortfolioHedgeView(
        option_delta=option_delta,
        futures_delta=futures_delta,
        net_delta=option_delta + futures_delta,
        equity_btc=float(summary.equity_btc),
        index_price=float(index_price),
        controller_parent_id=min(active_ids),
        target_usd_by_parent=target_by_parent,
        managed_usd_by_parent=managed_by_parent,
        last_rebalance_at=last_rebalance_at,
    )


def delta_view_from_positions(ctx: Ctx, summary, opts: list, futs: list,
                              index_price: float) -> DeltaView:
    """用扫描器已取得的账户数据构造 DeltaView，避免同一轮重复请求 Deribit。"""
    option_delta = sum(p.delta for p in opts)
    futures_delta = next(
        (p.delta_btc for p in futs
         if p.instrument == ctx.cfg.hedge.perp_instrument),
        0.0,
    )
    return DeltaView(
        option_delta=option_delta,
        futures_delta=futures_delta,
        net_delta=option_delta + futures_delta,
        equity_btc=summary.equity_btc,
        index_price=index_price,
    )


def hedge_price_levels(ctx: Ctx, parent: dict,
                       params: TradeHedgeParams) -> HedgePriceLevels | None:
    """统一计算真实 BEP、提前激活价和撤销线；旧持仓缺开仓价时安全回退到 BEP。"""
    raw_bep = ctx.cfg.debug.bep_override or parent.get("bep_price")
    if not raw_bep:
        return None
    bep = float(raw_bep)
    raw_open = parent.get("open_index_price")
    open_index_price = float(raw_open) if raw_open else None
    progress = runtime_settings.effective_progress(ctx)
    trigger = (
        bep_mod.activation_price(open_index_price, bep, progress)
        if open_index_price is not None else bep
    )
    return HedgePriceLevels(
        open_index_price=open_index_price,
        bep=bep,
        activation_progress=progress,
        activation_offset=(abs(bep - open_index_price) * progress
                           if open_index_price is not None else 0.0),
        activation_price=trigger,
        release_line=(
            bep_mod.recovery_release_line(
                open_index_price, bep, params.release_recovery_pct)
            if open_index_price is not None
            else bep_mod.release_line(
                parent["spread_type"], trigger,
                params.call_release_pct, params.put_release_pct)
        ),
    )


def snapshot_payload(view: DeltaView, observed_price: float, price_ts: int,
                     observed_at: int, price_source: str = "3m_close") -> dict:
    """转成可序列化的只读诊断快照。"""
    return {
        "option_delta": view.option_delta,
        "futures_delta": view.futures_delta,
        "net_delta": view.net_delta,
        "equity_btc": view.equity_btc,
        "index_price": view.index_price,
        # 保留旧字段，兼容现有 API/前端；新增字段明确价格来源。
        "bar_close": observed_price,
        "bar_ts": price_ts,
        "observed_price": observed_price,
        "price_source": price_source,
        "observed_at": observed_at,
    }


def activation_check_values(parent: dict, levels: HedgePriceLevels,
                            price: float) -> dict:
    """把一次价格与提前激活线的比较转成可记录/展示的字段。"""
    spread_type = parent["spread_type"]
    touched = bep_mod.activation_touched(
        spread_type, price, levels.activation_price)
    # 正数表示仍需朝风险方向再走多少 USD，负数表示已经越过激活线。
    remaining_signed = (
        levels.activation_price - price
        if spread_type == "CALL_CREDIT"
        else price - levels.activation_price
    )
    operator = ">=" if spread_type == "CALL_CREDIT" else "<="
    return {
        "touched": touched,
        "remaining_signed": remaining_signed,
        "remaining_to_trigger": max(0.0, remaining_signed),
        "condition": f"BTC 指数价 {operator} {levels.activation_price:,.1f}",
    }


async def record_hedge_check(
        ctx: Ctx, parent: dict, levels: HedgePriceLevels, price: float,
        checked_at: int, source: str, *, count: int = 1,
        emit_checkpoint: bool = False) -> int | None:
    """记录一次或一批门槛比较，并可选地写入可读的审计事件。"""
    recorder = getattr(ctx.repo, "record_hedge_check", None)
    if not callable(recorder):  # 保持轻量测试替身兼容
        return None
    values = activation_check_values(parent, levels, price)
    total = await recorder(
        parent["id"], count=count, checked_at=checked_at, price=price,
        source=source, trigger=levels.activation_price,
        progress=levels.activation_progress, offset=levels.activation_offset,
        touched=values["touched"])
    live_checks = getattr(ctx, "hedge_live_checks", None)
    if isinstance(live_checks, dict):
        current = live_checks.get(parent["id"], {})
        if int(current.get("last_check_at") or 0) <= checked_at:
            current.update({
                "check_count": max(int(current.get("check_count") or 0), total),
                "last_check_at": checked_at,
                "last_check_price": price,
                "last_check_source": source,
                "last_check_trigger": levels.activation_price,
                "last_check_progress": levels.activation_progress,
                "last_check_offset": levels.activation_offset,
                "last_check_touched": values["touched"],
            })
            live_checks[parent["id"]] = current
    if emit_checkpoint and getattr(ctx.repo, "conn", None) is not None:
        await ev.emit(
            ctx.repo.conn, ev.HEDGE_CHECKPOINT, parent_id=parent["id"],
            check_count=total, checks_in_batch=max(1, int(count)),
            price=price, price_source=source,
            activation_progress=levels.activation_progress,
            activation_offset=levels.activation_offset,
            activation_price=levels.activation_price,
            trigger_condition=values["condition"],
            remaining_to_trigger=values["remaining_to_trigger"],
            touched=values["touched"],
        )
    return total


def hedge_diagnostic(ctx: Ctx, parent: dict, hedge: dict | None,
                     snapshot: dict | None, params: TradeHedgeParams) -> dict:
    """解释当前是否会下对冲单；只读，不改变交易状态。"""
    state = (hedge or {}).get("state") or "HEDGE_INACTIVE"
    levels = hedge_price_levels(ctx, parent, params)
    bep = levels.bep if levels else None
    live_checks = getattr(ctx, "hedge_live_checks", {})
    parent_id = parent.get("id")
    live_check = (live_checks.get(parent_id, {})
                  if parent_id is not None and isinstance(live_checks, dict)
                  else {})
    stored_check_count = int((hedge or {}).get("check_count") or 0)
    check_count = max(stored_check_count, int(live_check.get("check_count") or 0))
    last_check_at = (live_check.get("last_check_at") or
                     (hedge or {}).get("last_check_at"))
    last_check_price = (live_check.get("last_check_price") if
                        live_check.get("last_check_price") is not None else
                        (hedge or {}).get("last_check_price"))
    last_check_source = (live_check.get("last_check_source") or
                         (hedge or {}).get("last_check_source"))
    base = {
        "state": state,
        "manual_hold": bool((hedge or {}).get("manual_hold")),
        "review_bar_minutes": runtime_settings.effective_review_bar_minutes(ctx),
        "reason_code": "WAITING_FOR_SCAN",
        "reason": "等待实时指数行情或下一轮完整扫描，暂时没有足够数据判断。",
        "next_action": "保持观察；收到行情并取得 Delta 后自动更新。",
        "bep": bep,
        "open_index_price": levels.open_index_price if levels else None,
        "activation_progress": levels.activation_progress if levels else None,
        "activation_offset": levels.activation_offset if levels else None,
        "activation_price": levels.activation_price if levels else None,
        "trigger_condition": None,
        "remaining_to_trigger": None,
        "check_count": check_count,
        "last_check_at": last_check_at,
        "last_check_price": last_check_price,
        "last_check_source": last_check_source,
        "last_check_touched": live_check.get("last_check_touched") if live_check else
                              (hedge or {}).get("last_check_touched"),
        "release_line": None,
        "bar_close": None,
        "bar_ts": None,
        "observed_at": None,
        "index_price": None,
        "option_delta": None,
        "futures_delta": None,
        "net_delta": None,
        "effective_trigger": None,
        "inner_trigger": None,
        "target_futures_delta": None,
        "adjustment_usd": None,
        "managed_futures_usd": float((hedge or {}).get("futures_usd") or 0.0),
    }
    if not bep:
        base.update(
            reason_code="MISSING_BEP",
            reason="开仓尚未形成有效 BEP，不能启用条件式 Delta 对冲。",
            next_action="等待期权双腿成交并计算 BEP。",
        )
        return base
    base["release_line"] = levels.release_line
    if last_check_price is not None:
        check = activation_check_values(parent, levels, float(last_check_price))
        base.update(
            observed_price=float(last_check_price),
            bar_close=float(last_check_price),
            price_source=last_check_source or "realtime_index",
            observed_at=last_check_at,
            trigger_condition=check["condition"],
            remaining_to_trigger=check["remaining_to_trigger"],
        )
    else:
        base["trigger_condition"] = activation_check_values(
            parent, levels, levels.activation_price)["condition"]
    if not snapshot:
        return base

    observed_price = float(last_check_price) if last_check_price is not None else float(
        snapshot.get("observed_price", snapshot["bar_close"]))
    price_source = last_check_source or snapshot.get("price_source") or "3m_close"
    option_delta = float(snapshot["option_delta"])
    futures_delta = float(snapshot["futures_delta"])
    net_delta = float(snapshot["net_delta"])
    index_price = float(snapshot["index_price"])
    eff = effective_trigger(
        ctx, float(snapshot["equity_btc"]), index_price,
        params.account_risk_pct, params.trigger_pct)
    inner = inner_trigger(ctx, eff)
    order_btc = rebalance_order_btc(net_delta, eff, inner)
    order_usd = contract_usd(
        order_btc * index_price, ctx.cfg.hedge.perp_contract_usd)
    base.update(
        bar_close=observed_price,
        observed_price=observed_price,
        price_source=price_source,
        bar_ts=snapshot.get("bar_ts"),
        observed_at=last_check_at or snapshot.get("observed_at"),
        index_price=index_price,
        option_delta=option_delta,
        futures_delta=futures_delta,
        net_delta=net_delta,
        effective_trigger=eff,
        inner_trigger=inner,
        target_futures_delta=futures_delta + order_btc,
        adjustment_usd=order_usd,
    )

    if ctx.hedge_frozen:
        base.update(
            reason_code="HEDGE_FROZEN",
            reason="平仓或强平流程正在占用执行通道，对冲调整已冻结。",
            next_action="等待平仓流程完成，不会另外增加永续仓位。",
        )
        return base
    if state == "HEDGE_REBALANCING":
        base.update(
            reason_code="REBALANCING",
            reason="对冲调整命令正在执行。",
            next_action=f"目标调整 {order_usd:+.0f} USD，成交后重新核对净 Delta。",
        )
        return base
    if state == "HEDGE_RELEASE_PENDING":
        review_minutes = runtime_settings.effective_review_bar_minutes(ctx)
        base.update(
            reason_code="RELEASE_PENDING",
            reason=f"价格已回到对冲撤销区，正在等待下一根完整 {review_minutes} 分钟 K 线确认。",
            next_action="确认仍在撤销区后，归零本策略建立的永续对冲。",
        )
        return base
    if state == "HEDGE_CLOSING":
        base.update(
            reason_code="HEDGE_CLOSING",
            reason="本策略建立的永续对冲正在归零。",
            next_action="核对剩余归属仓位，未完成时自动重试。",
        )
        return base
    if state == "HEDGE_RETRYING":
        base.update(
            reason_code="HEDGE_RETRYING",
            reason="上一笔对冲仅部分成交，正在同一调仓周期内重新读取仓位并补单。",
            next_action="按最新 Delta 重新计算剩余量，不沿用旧订单缺口。",
        )
        return base
    if state == "HEDGE_DEGRADED":
        base.update(
            reason_code="HEDGE_DEGRADED",
            reason="连续多轮 IOC 后仍未进入目标 Delta 区间。",
            next_action="每隔短周期高优先级重试，并持续显示未完成风险。",
        )
        return base

    trigger = levels.activation_price
    check = activation_check_values(parent, levels, observed_price)
    touched = check["touched"]
    base.update(
        trigger_condition=check["condition"],
        remaining_to_trigger=check["remaining_to_trigger"],
    )
    if state == "HEDGE_INACTIVE" and not touched:
        direction = "跌至或低于" if parent["spread_type"] == "PUT_CREDIT" else "涨至或高于"
        distance = check["remaining_to_trigger"]
        base.update(
            reason_code="WAITING_FOR_HEDGE_TRIGGER",
            reason=(
                f"尚未触及提前对冲价：当前指数价 {observed_price:,.1f}，"
                f"需{direction} {trigger:,.1f}。"
            ),
        next_action=f"距离激活线约 {distance:,.1f} USD；当前不下对冲单。",
        )
        return base
    if state == "HEDGE_INACTIVE":
        base.update(
            reason_code="ACTIVATION_PENDING",
            reason="当前指数价已触及提前对冲价，正在等待状态激活。",
            next_action="进入对冲状态后立即按最新 Delta 复算目标仓位。",
        )
        return base
    if abs(net_delta) < eff:
        base.update(
            reason_code="BELOW_DELTA_TRIGGER",
            reason=(
                f"对冲已激活，但净 Delta {net_delta:+.5f} BTC "
                f"小于调仓阈值 {eff:.5f} BTC。"
            ),
            next_action="保持现有永续归属，避免小额频繁交易；等待超过外层调仓阈值。",
        )
        return base
    if abs(order_btc) < eff:
        base.update(
            reason_code="BELOW_ADJUSTMENT_TRIGGER",
            reason=(
                f"净 Delta 已检查，但目标与当前永续的差额 "
                f"{order_btc:+.5f} BTC 小于阈值 {eff:.5f} BTC。"
            ),
            next_action="保持现有永续仓位。",
        )
        return base
    if abs(order_usd) < ctx.cfg.hedge.perp_contract_usd - 1e-9:
        base.update(
            reason_code="BELOW_MIN_ORDER",
            reason="所需调整不足一个 BTC-PERPETUAL 最小合约单位。",
            next_action="等待 Delta 扩大到至少 10 USD 等值后再调整。",
        )
        return base
    base.update(
        reason_code="REBALANCE_REQUIRED",
        reason=(
            f"已触及提前对冲价且净 Delta {net_delta:+.5f} BTC 超过阈值 "
            f"{eff:.5f} BTC。"
        ),
        next_action=(f"应调整 BTC-PERPETUAL {order_usd:+.0f} USD，"
                     "使账户衍生品净 Delta 回到合约精度内的中性。"),
    )
    return base


async def scan_hedge(ctx: Ctx, parent: dict, bar_close: float, bar_ts: int,
                     view: DeltaView | None = None, *,
                     price_source: str = "3m_close",
                     allow_release: bool = True,
                     record_check: bool = True,
                     allow_rebalance: bool = True) -> None:
    """扫描第 6~11 步：提前激活 / 撤销线 / Delta 管线 → 入队对冲命令。"""
    repo, cfg = ctx.repo, ctx.cfg
    pid = parent["id"]
    hedge = await repo.get_hedge(pid)
    if hedge is None:
        await repo.ensure_hedge_row(pid)
        hedge = await repo.get_hedge(pid)
    state = hedge["state"]
    manual_hold = bool(hedge.get("manual_hold"))
    spread_type = parent["spread_type"]
    params = await trade_hedge_params(ctx, parent)
    levels = hedge_price_levels(ctx, parent, params)
    if levels is None:
        return
    bep = levels.bep
    trigger = levels.activation_price
    if record_check:
        await record_hedge_check(
            ctx, parent, levels, bar_close, market_data.now_ms(), price_source,
            emit_checkpoint=True)

    # 6. 开仓价到 BEP 的进度线触及（实时指数价；3min 扫描为兜底）→ 激活
    if state == "HEDGE_INACTIVE":
        if await manual_rearm_blocked(ctx, pid):
            return
        if bep_mod.activation_touched(spread_type, bar_close, trigger):
            await repo.transition_hedge(pid, "HEDGE_ACTIVE", ev.BEP_HEDGE_ACTIVATED,
                                        bar_close=bar_close,
                                        observed_price=bar_close,
                                        price_source=price_source, bep=bep,
                                        open_index_price=levels.open_index_price,
                                        activation_price=trigger,
                                        review_bar_minutes=(
                                            runtime_settings.effective_review_bar_minutes(ctx)
                                        ))
            pending = getattr(ctx, "hedge_grouped_open_pending", None)
            if not isinstance(pending, set):
                pending = set()
                ctx.hedge_grouped_open_pending = pending
            pending.add(pid)
            state = "HEDGE_ACTIVE"
        else:
            return   # 未触及提前激活价：target=0，不对冲

    # 实时订阅只负责提前激活；撤销仍由配置的完整复核 Bar 两段确认，避免瞬时抖动。
    released_zone = allow_release and not manual_hold and (
        bar_close <= levels.release_line
        if spread_type == "CALL_CREDIT"
        else bar_close >= levels.release_line)
    if state in ("HEDGE_ACTIVE", "HEDGE_DEGRADED") and released_zone:
        await repo.transition_hedge(pid, "HEDGE_RELEASE_PENDING",
                                    ev.HEDGE_RELEASE_PENDING,
                                    bar_close=bar_close, bar_ts=bar_ts,
                                    review_bar_minutes=(
                                        runtime_settings.effective_review_bar_minutes(ctx)
                                    ))
        await repo.set_release_pending_bar(pid, bar_ts)
        return   # 本 bar 不再做其他对冲动作（下一根完整 bar 确认）
    if state == "HEDGE_RELEASE_PENDING":
        pending_ts = hedge["release_pending_bar_ts"] or 0
        if bep_mod.activation_touched(spread_type, bar_close, trigger):
            # 价格重新突破提前激活价 → 取消撤销，回到 ACTIVE
            await repo.transition_hedge(pid, "HEDGE_ACTIVE",
                                        ev.HEDGE_RELEASE_CANCELLED,
                                        bar_close=bar_close,
                                        activation_price=trigger)
            state = "HEDGE_ACTIVE"
        elif bar_ts > pending_ts and released_zone:
            required = runtime_settings.effective_release_confirmation_bars(ctx)
            increment = getattr(repo, "increment_release_confirmation", None)
            if callable(increment):
                confirmed = await increment(pid)
            else:
                bar_ms = runtime_settings.effective_review_bar_minutes(ctx) * 60_000
                confirmed = max(1, int((bar_ts - pending_ts) // bar_ms))
            if confirmed >= required:
                ctx.queue.put(_release_command(ctx, pid, grouped=True))
            return
        elif bar_ts > pending_ts:
            # 确认必须连续；中途离开撤销区则回到 ACTIVE 重新等待。
            await repo.transition_hedge(
                pid, "HEDGE_ACTIVE", ev.HEDGE_RELEASE_CANCELLED,
                bar_close=bar_close, release_line=levels.release_line,
                reason="LEFT_RELEASE_ZONE")
            return
        else:
            return
    if state in ("HEDGE_REBALANCING", "HEDGE_RETRYING", "HEDGE_CLOSING"):
        return   # 有动作在途，等待其完成

    # manual_hold 只兼容旧版已存在的人工固定持仓。新版一键对冲不会设置
    # 该标志，成交后会继续走上面的 β 撤销判断和正常 Delta 管理。
    if manual_hold or not allow_rebalance:
        return

    # 轻量测试替身没有组合读取器时，保留旧的单快照入队路径。
    if callable(getattr(repo, "open_parents", None)):
        await scan_account_hedge(ctx)
        return

    # 兼容路径：外层阈值触发，随后调至 Delta 中性。
    view = view or await snapshot_delta(ctx)
    eff = effective_trigger(ctx, view.equity_btc, view.index_price,
                            params.account_risk_pct, params.trigger_pct)
    if abs(view.net_delta) < eff:
        return
    inner = inner_trigger(ctx, eff)
    order_btc = rebalance_order_btc(view.net_delta, eff, inner)
    if not order_btc:
        await repo.record_hedge_action(parent_id=pid, action="SKIP",
                                       net_delta_before=view.net_delta,
                                       skipped_reason="BELOW_TRIGGER")
        return
    ctx.queue.put(ctx.queue.hedge_rebalance(pid, order_btc, view.net_delta))


async def scan_account_hedge(ctx: Ctx, *, parents: list[dict] | None = None,
                             opts: list | None = None, summary=None,
                             index_price: float | None = None,
                             review_sides: set[str] | None = None
                             ) -> PortfolioHedgeView | None:
    """PUT/CALL 独立过门槛与冷却，最终只生成一个永续净额命令。"""
    account_view = await portfolio_hedge_view(
        ctx, parents=parents, opts=opts, summary=summary,
        index_price=index_price)
    if (account_view is None or ctx.hedge_frozen
            or await ctx.force_close_active()):
        return account_view

    # A release owns the account hedge until its managed perpetual amount has
    # reached zero.  Do not enqueue a stale rebalance in the same scan (or for
    # another side-book) while release is pending/closing.
    scoped_parents = parents if parents is not None else await ctx.repo.open_parents()
    for parent in scoped_parents:
        row = await ctx.repo.get_hedge(parent["id"])
        if row and row.get("state") in REBALANCE_BLOCKING_STATES:
            return account_view

    side_book = getattr(ctx.cfg.hedge, "hedge_scope", "ACCOUNT_NET") == "SIDE_BOOK"
    specs: list[tuple[str | None, PortfolioHedgeView]] = []
    if side_book:
        for spread_type in runtime_settings.HEDGE_SIDES:
            if review_sides is not None and spread_type not in review_sides:
                continue
            side_view = await portfolio_hedge_view(
                ctx, parents=parents, opts=opts, summary=summary,
                index_price=index_price, spread_types={spread_type})
            if side_view is not None:
                specs.append((spread_type, side_view))
    else:
        specs.append((None, account_view))

    now = market_data.now_ms()
    lot = float(ctx.cfg.hedge.perp_contract_usd)
    order_btc_total = 0.0
    eligible_sides: list[str] = []
    controllers: list[int] = []
    for spread_type, view in specs:
        parent = await ctx.repo.get_parent(view.controller_parent_id)
        if not parent:
            continue
        params = await trade_hedge_params(ctx, parent)
        profile = (runtime_settings.effective_side_profile(ctx, spread_type)
                   if spread_type else {
                       field: float(getattr(ctx.cfg.hedge, field))
                       for field in runtime_settings.PROFILE_FIELDS
                   })
        outer = effective_trigger(
            ctx, view.equity_btc, view.index_price,
            params.account_risk_pct, params.trigger_pct,
            profile["rebalance_outer_equity_pct"])
        if abs(view.net_delta) < outer:
            continue
        cooldown_ms = int(profile["rebalance_cooldown_sec"] * 1000)
        emergency = outer * profile["emergency_trigger_multiplier"]
        if (view.last_rebalance_at and cooldown_ms > 0
                and now - view.last_rebalance_at < cooldown_ms
                and abs(view.net_delta) < emergency):
            await ctx.repo.record_hedge_action(
                parent_id=view.controller_parent_id, book_key=spread_type,
                action="SKIP", net_delta_before=view.net_delta,
                skipped_reason="COOLDOWN")
            continue
        order_btc = rebalance_order_btc(
            view.net_delta, outer, inner_trigger(ctx, outer))
        usd = contract_usd(order_btc * view.index_price, lot)
        minimum = max(lot, profile["min_rebalance_usd"])
        if abs(usd) < minimum - 1e-9:
            await ctx.repo.record_hedge_action(
                parent_id=view.controller_parent_id, book_key=spread_type,
                action="SKIP", net_delta_before=view.net_delta,
                requested_usd=usd, skipped_reason="MIN_REBALANCE_USD")
            continue
        order_btc_total += order_btc
        controllers.append(view.controller_parent_id)
        if spread_type:
            eligible_sides.append(spread_type)

    order_usd = contract_usd(order_btc_total * account_view.index_price, lot)
    if not controllers or not order_usd:
        if controllers and not order_usd:
            for parent_id, spread_type in zip(controllers, eligible_sides):
                await ctx.repo.record_hedge_action(
                    parent_id=parent_id, book_key=spread_type, action="SKIP",
                    requested_usd=0.0, skipped_reason="NETTED_BETWEEN_BOOKS")
        return account_view
    controller = min(controllers)
    pending_grouped = getattr(ctx, "hedge_grouped_open_pending", None)
    grouped_open = (isinstance(pending_grouped, set)
                    and any(pid in pending_grouped for pid in controllers))
    queued = ctx.queue.put(_rebalance_command(
        ctx, controller, order_usd / account_view.index_price,
        account_view.net_delta, spread_types=eligible_sides or None,
        grouped=grouped_open))
    if queued and grouped_open:
        pending_grouped.difference_update(controllers)
    return account_view


def _order_to_inner(view: PortfolioHedgeView, outer: float, inner: float,
                    *, continuation: bool = False) -> float:
    if continuation:
        if abs(view.net_delta) <= 0:
            return 0.0
        return -view.net_delta
    return rebalance_order_btc(view.net_delta, outer, inner)


async def _allocate_managed_fill(ctx: Ctx, view: PortfolioHedgeView,
                                 requested_usd: float,
                                 filled_usd: float) -> float:
    """按各策略虚拟目标的移动比例分摊共享永续真实成交。"""
    current = dict(view.managed_usd_by_parent)
    parent_ids = set(current) | set(view.target_usd_by_parent)
    targets = {pid: view.target_usd_by_parent.get(pid, 0.0)
               for pid in parent_ids}
    full_move = sum(targets.values()) - sum(current.values())
    desired_ratio = requested_usd / full_move if abs(full_move) > 1e-9 else 0.0
    desired_ratio = max(-1.0, min(1.0, desired_ratio))
    fill_ratio = filled_usd / requested_usd if abs(requested_usd) > 1e-9 else 0.0
    fill_ratio = max(0.0, min(1.0, fill_ratio))
    lot = float(ctx.cfg.hedge.perp_contract_usd)
    updated: dict[int, float] = {}
    for pid in targets:
        start = current.get(pid, 0.0)
        desired = start + (targets[pid] - start) * desired_ratio
        updated[pid] = contract_usd(
            start + (desired - start) * fill_ratio, lot)
    expected_total = contract_usd(sum(current.values()) + filled_usd, lot)
    residual = contract_usd(expected_total - sum(updated.values()), lot)
    updated[view.controller_parent_id] = (
        updated.get(view.controller_parent_id, 0.0) + residual)
    for pid, usd in updated.items():
        await ctx.repo.set_hedge_futures(
            pid, usd, usd / view.index_price if view.index_price > 0 else 0.0)
    return updated.get(view.controller_parent_id, 0.0)


async def _schedule_degraded_retry(ctx: Ctx, parent_id: int,
                                   spread_types: set[str] | None = None) -> None:
    tasks = getattr(ctx, "hedge_retry_tasks", None)
    if not isinstance(tasks, dict):
        tasks = {}
        ctx.hedge_retry_tasks = tasks
    existing = tasks.get(parent_id)
    if existing and not existing.done():
        return

    async def later() -> None:
        try:
            await asyncio.sleep(float(getattr(
                ctx.cfg.hedge, "degraded_retry_sec", 15.0)))
            row = await ctx.repo.get_hedge(parent_id)
            if row and row.get("state") == "HEDGE_DEGRADED":
                ctx.queue.put(_rebalance_command(
                    ctx, parent_id, 0.0, 0.0, degraded_retry=True,
                    spread_types=sorted(spread_types) if spread_types else None))
        finally:
            tasks.pop(parent_id, None)

    tasks[parent_id] = asyncio.create_task(later())


async def handle_rebalance(ctx: Ctx, payload: dict) -> None:
    """账户级调仓周期：每轮重读 Delta，部分/零成交即时提高价格重试。"""
    pid = payload["parent_id"]
    repo, cfg = ctx.repo, ctx.cfg
    if ctx.hedge_frozen or await ctx.force_close_active():
        return
    parent = await repo.get_parent(pid)
    hedge = await repo.get_hedge(pid)
    if not parent or parent["state"] not in ("OPEN", "PARTIALLY_CLOSED"):
        return
    if not hedge or hedge["state"] not in ("HEDGE_ACTIVE", "HEDGE_DEGRADED"):
        return
    cycle_id = uuid.uuid4().hex
    manual = bool(payload.get("manual"))
    grouped = manual or bool(payload.get("grouped"))
    # 五组执行器内部已经包含五批、每批三级报价，不再外套三轮完整重试。
    max_attempts = 1 if grouped else int(getattr(cfg.hedge, "retry_rounds", 3))
    delay = float(getattr(cfg.hedge, "retry_delay_sec", 1.5))
    base_ticks = int(cfg.hedge.ioc_ticks)
    tick_step = int(getattr(cfg.hedge, "retry_tick_step", 5))
    continuation = bool(payload.get("degraded_retry"))
    spread_types = set(payload.get("spread_types") or []) or None
    book_key = ",".join(sorted(spread_types)) if spread_types else None
    success = False
    transitioned = False
    try:
        for attempt in range(1, max_attempts + 1):
            view = await portfolio_hedge_view(ctx, spread_types=spread_types)
            if view is None:
                success = True
                break
            params = await trade_hedge_params(ctx, parent)
            outer = effective_trigger(
                ctx, view.equity_btc, view.index_price,
                params.account_risk_pct, params.trigger_pct)
            inner = inner_trigger(ctx, outer)
            order_btc = _order_to_inner(
                view, outer, inner,
                continuation=(bool(spread_types) or continuation or attempt > 1))
            lot = float(cfg.hedge.perp_contract_usd)
            usd = contract_usd(order_btc * view.index_price, lot)
            minimum = (lot if spread_types else max(lot, float(getattr(
                cfg.hedge, "min_rebalance_usd", lot))))
            if not order_btc or abs(usd) < minimum - 1e-9:
                success = abs(view.net_delta) <= outer
                await repo.record_hedge_action(
                    parent_id=pid, book_key=book_key, action="SKIP",
                    net_delta_before=view.net_delta, requested_usd=usd,
                    cycle_id=cycle_id, attempt_no=attempt,
                    max_attempts=max_attempts, residual_delta=view.net_delta,
                    skipped_reason=("BELOW_TRIGGER" if not order_btc
                                    else "MIN_REBALANCE_USD"))
                break
            if not transitioned:
                await repo.transition_hedge(
                    pid, "HEDGE_REBALANCING", ev.DELTA_HEDGE_REBALANCE,
                    order_usd=usd, net_delta=view.net_delta,
                    cycle_id=cycle_id, max_attempts=max_attempts,
                    manual=manual)
                transitioned = True
            if grouped:
                async def latest_manual_target() -> float:
                    latest = await portfolio_hedge_view(
                        ctx, spread_types=spread_types)
                    if latest is None:
                        return usd
                    return contract_usd(
                        -latest.net_delta * latest.index_price, lot)

                attempt_recorder = getattr(
                    repo, "record_execution_attempts", None)

                async def record_group_attempt(item: dict) -> None:
                    if callable(attempt_recorder):
                        await attempt_recorder(pid, [{
                            **item,
                            "hedge_action": "REBALANCE",
                            "hedge_cycle_id": cycle_id,
                            "cycle_attempt_no": attempt,
                            "cycle_max_attempts": max_attempts,
                        }])

                fill = await execution_adapter.execute_perp_maker_first(
                    ctx.ws,
                    instrument=cfg.hedge.perp_instrument,
                    usd_signed=usd,
                    min_lot=lot,
                    n_exec_groups=5,
                    attempt_waits=manual_hedge_attempt_waits(cfg),
                    reduce_only=False,
                    target_provider=latest_manual_target,
                    on_attempt=(record_group_attempt
                                if callable(attempt_recorder) else None))
            else:
                fill = await futures.perp_ioc(
                    ctx.ws, cfg.hedge.perp_instrument, usd,
                    ioc_ticks=base_ticks + (attempt - 1) * tick_step)
            effective_requested = fill.requested_usd if grouped else usd
            managed_after = await _allocate_managed_fill(
                ctx, view, effective_requested, fill.filled_usd)
            after_view = await portfolio_hedge_view(
                ctx, spread_types=spread_types)
            after = after_view.net_delta if after_view else 0.0
            await repo.record_hedge_action(
                parent_id=pid, book_key=book_key, action="REBALANCE",
                net_delta_before=view.net_delta, net_delta_after=after,
                requested_usd=effective_requested, order_usd=fill.filled_usd,
                managed_usd_after=managed_after, cycle_id=cycle_id,
                attempt_no=attempt, max_attempts=max_attempts,
                residual_delta=after,
                fill_price=fill.avg_price, fee=fill.fee_btc,
                details=({
                    "manual": manual,
                    "grouped": True,
                    "liquidity_role": fill.liquidity_role,
                    "groups": fill.groups,
                } if grouped else {
                    "manual": False,
                    "liquidity_role": "TAKER",
                    "exchange_order_id": getattr(fill, "order_id", None),
                    "exchange_trade_ids": getattr(fill, "trade_ids", None) or [],
                }))
            if abs(fill.filled_usd) >= lot - 1e-9:
                await repo.record_fill(
                    parent_id=pid, group_id=None,
                    instrument=cfg.hedge.perp_instrument,
                    direction="buy" if fill.filled_usd > 0 else "sell",
                    qty=abs(fill.filled_usd), price=fill.avg_price,
                    fee=fill.fee_btc, phase="hedge")
            if abs(after) <= neutral_tolerance(ctx, view.index_price):
                success = True
                break
            if (grouped and abs(
                    fill.filled_usd - fill.requested_usd) < lot / 2):
                # 五个固定批次已经完整成交。人工持有期间不追逐执行过程中
                # 变化的 Delta，否则页面会在五批之后继续出现自动补单。
                success = True
                break
            if attempt < max_attempts:
                current = await repo.get_hedge(pid)
                if current and current["state"] == "HEDGE_REBALANCING":
                    await repo.transition_hedge(pid, "HEDGE_RETRYING")
                continuation = True
                if delay:
                    await asyncio.sleep(delay)
    finally:
        h = await repo.get_hedge(pid)
        if h and h["state"] in ("HEDGE_REBALANCING", "HEDGE_RETRYING"):
            if success:
                await repo.transition_hedge(pid, "HEDGE_ACTIVE")
            else:
                await repo.transition_hedge(
                    pid, "HEDGE_DEGRADED", ev.HEDGE_SKIPPED,
                    reason="IOC_RETRIES_EXHAUSTED", cycle_id=cycle_id)
                await _schedule_degraded_retry(ctx, pid, spread_types)


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
    manual = bool(payload.get("manual"))
    grouped = manual or bool(payload.get("grouped"))
    remaining = await close_managed_hedge(
        ctx, pid, action="RELEASE", manual=manual, grouped=grouped)
    if abs(remaining) < cfg.hedge.perp_contract_usd - 1e-9:
        setter = getattr(repo, "set_hedge_manual_hold", None)
        if callable(setter):
            await setter(pid, False)
        if manual:
            await set_manual_rearm_cooldown(ctx, pid)
        await repo.transition_hedge(pid, "HEDGE_INACTIVE")
    else:
        log.error("策略对冲归零未完成，剩余 %.0f USD，保持 HEDGE_CLOSING 下轮重试", remaining)
        ctx.queue.put(_release_command(
            ctx, pid, manual=manual, grouped=grouped))


async def close_managed_hedge(ctx: Ctx, parent_id: int, *,
                              action: str = "CLOSE", max_rounds: int = 5,
                              manual: bool = False,
                              grouped: bool = False) -> float:
    """只反向冲销本策略实际建立的期货数量，保留账户原有/人工期货仓位。"""
    repo, cfg = ctx.repo, ctx.cfg
    use_grouped = manual or grouped
    rounds = 1 if use_grouped else max_rounds
    cycle_id = uuid.uuid4().hex
    for cycle_attempt in range(1, rounds + 1):
        row = await repo.get_hedge(parent_id)
        managed_usd = float((row or {}).get("futures_usd") or 0.0)
        if abs(managed_usd) < cfg.hedge.perp_contract_usd - 1e-9:
            await repo.set_hedge_futures(parent_id, 0.0, 0.0)
            setter = getattr(repo, "set_hedge_manual_hold", None)
            if callable(setter):
                await setter(parent_id, False)
            return 0.0
        close_usd = contract_usd(-managed_usd, cfg.hedge.perp_contract_usd)
        if not close_usd:
            await repo.set_hedge_futures(parent_id, 0.0, 0.0)
            setter = getattr(repo, "set_hedge_manual_hold", None)
            if callable(setter):
                await setter(parent_id, False)
            return 0.0
        if use_grouped:
            attempt_recorder = getattr(repo, "record_execution_attempts", None)

            async def record_group_attempt(item: dict) -> None:
                if callable(attempt_recorder):
                    await attempt_recorder(parent_id, [{
                        **item,
                        "hedge_action": action,
                        "hedge_cycle_id": cycle_id,
                        "cycle_attempt_no": cycle_attempt,
                        "cycle_max_attempts": rounds,
                    }])

            fill = await execution_adapter.execute_perp_maker_first(
                ctx.ws,
                instrument=cfg.hedge.perp_instrument,
                usd_signed=close_usd,
                min_lot=float(cfg.hedge.perp_contract_usd),
                n_exec_groups=5,
                attempt_waits=manual_hedge_attempt_waits(cfg),
                reduce_only=True,
                on_attempt=(record_group_attempt
                            if callable(attempt_recorder) else None))
        else:
            fill = await futures.perp_ioc(
                ctx.ws, cfg.hedge.perp_instrument, close_usd,
                ioc_ticks=cfg.hedge.ioc_ticks, reduce_only=False)
        if abs(fill.filled_usd) < cfg.hedge.perp_contract_usd - 1e-9:
            break
        index_price = fill.avg_price or await market_data.get_index_price(
            ctx.ws, cfg.strategy.currency)
        remaining, _ = await repo.adjust_hedge_futures(
            parent_id, fill.filled_usd, index_price)
        await repo.record_hedge_action(
            parent_id=parent_id, action=action, requested_usd=close_usd,
            order_usd=fill.filled_usd, managed_usd_after=remaining,
            cycle_id=cycle_id, attempt_no=cycle_attempt,
            max_attempts=rounds,
            fill_price=fill.avg_price, fee=fill.fee_btc,
            details=({
                "manual": manual,
                "grouped": True,
                "liquidity_role": fill.liquidity_role,
                "groups": fill.groups,
            } if use_grouped else {
                "manual": False,
                "liquidity_role": "TAKER",
                "exchange_order_id": getattr(fill, "order_id", None),
                "exchange_trade_ids": getattr(fill, "trade_ids", None) or [],
            }))
        await repo.record_fill(
            parent_id=parent_id, group_id=None,
            instrument=cfg.hedge.perp_instrument,
            direction="buy" if fill.filled_usd > 0 else "sell",
            qty=abs(fill.filled_usd), price=fill.avg_price,
            fee=fill.fee_btc, phase="hedge")
        if abs(remaining) < cfg.hedge.perp_contract_usd - 1e-9:
            await repo.set_hedge_futures(parent_id, 0.0, 0.0)
            setter = getattr(repo, "set_hedge_manual_hold", None)
            if callable(setter):
                await setter(parent_id, False)
            return 0.0
    row = await repo.get_hedge(parent_id)
    return float((row or {}).get("futures_usd") or 0.0)
