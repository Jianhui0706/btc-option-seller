"""风控检查器 + FORCE_CLOSE（规范第九节）+ 启动崩溃恢复。

FORCE_CLOSE：RISK_ENGINE 与 MANUAL 共用同一执行函数，只是日志来源不同。
执行内容：停开仓 → 撤全部挂单 → 平全部卖腿+买腿（不留跛腿）→ 平期货对冲 →
核对归零 → 子组标记 FORCE_CLOSED。

持仓一致性口径：只核对"本系统父交易"的腿；账户中与本系统无关的期权仓位
（其他策略/人工，规范第三节允许其存在）只告警并阻止新开仓，不强平它们。
"""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass
import logging
import time
from typing import Optional

from ..context import Ctx
from ..exchange import account, futures
from ..execution import adapter
from ..monitor import events as ev

log = logging.getLogger("v2.risk")

EPS = 1e-9
AUTO_RESUME_STATE_KEY = "risk_auto_resume_state"
PAUSE_REASON_KEY = "trading_pause_reason"
AUTO_RESUME_ARMED = "ARMED"
AUTO_RESUME_READY = "READY"
AUTO_RESUME_LOCKED = "LOCKED"
DAILY_LOSS_STATE_PREFIX = "DAILY_LOSS:"
RESIDUAL_DECISION_REASON = "RESIDUAL_LONG_DECISION_REQUIRED"

LATE_FILL_RECOVERY_WINDOW_MS = 30 * 24 * 60 * 60 * 1000


@dataclass(frozen=True)
class RiskBreach:
    reason: str
    resume_after_date: Optional[str] = None


def beijing_date() -> str:
    return dt.datetime.now(
        dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d")


def daily_loss_resume_state(date: str) -> str:
    return f"{DAILY_LOSS_STATE_PREFIX}{date}"


def daily_loss_pause_date(state: str) -> Optional[str]:
    if not state.startswith(DAILY_LOSS_STATE_PREFIX):
        return None
    value = state[len(DAILY_LOSS_STATE_PREFIX):]
    return value or None


async def request_risk_force_close(
        ctx: Ctx, reason: str, *, auto_resume: bool = False,
        resume_after_date: Optional[str] = None) -> bool:
    """Pause immediately and enqueue one risk close.

    Transient Deribit availability failures may resume only after the close
    finishes cleanly and a later full scan succeeds.  ARMED/READY also acts as
    a latch so scan 6, 7, ... cannot enqueue duplicate force-close commands.
    """
    repo = ctx.repo
    state = await repo.get_state(AUTO_RESUME_STATE_KEY, "")
    requested_daily_state = (
        daily_loss_resume_state(resume_after_date)
        if resume_after_date else None)
    active_reader = getattr(repo, "active_parents", None)
    active_parents = await active_reader() if active_reader else []
    stranded_active = bool(active_parents)
    if state == AUTO_RESUME_LOCKED and not stranded_active:
        return False
    if daily_loss_pause_date(state):
        # The same daily limit remains latched until the next Beijing day.
        # A transient account-read failure must not replace that stronger
        # pause.  A different material breach may still upgrade it to LOCKED.
        if ((requested_daily_state == state and not stranded_active)
                or auto_resume):
            return False
    if auto_resume and state in (AUTO_RESUME_ARMED, AUTO_RESUME_READY):
        return False

    was_enabled = await ctx.trading_enabled()
    armed = bool(auto_resume and was_enabled)
    if requested_daily_state and (
            was_enabled or state == requested_daily_state):
        next_state = requested_daily_state
    elif armed:
        next_state = AUTO_RESUME_ARMED
    else:
        # Material limits stay locked until an explicit operator action.  A
        # daily-loss breach that starts while trading is already manually
        # paused must not auto-enable that manual pause on the next day.
        next_state = AUTO_RESUME_LOCKED
    await repo.set_flag("trading_enabled", False)
    await repo.set_state(PAUSE_REASON_KEY, reason)
    await repo.set_state(AUTO_RESUME_STATE_KEY, next_state)
    ctx.request_force_close(
        "RISK_ENGINE", reason, auto_resume=armed)
    if armed:
        await ev.emit(
            repo.conn, ev.RISK_AUTO_RESUME_ARMED, level="WARN",
            reason=reason)
    elif requested_daily_state and next_state == requested_daily_state:
        await ev.emit(
            repo.conn, ev.RISK_DAILY_LOSS_PAUSED, level="WARN",
            reason=reason, resume_after_date=resume_after_date)
    return True


async def maybe_resume_transient_risk_pause(
        ctx: Ctx, active_parents: list[dict], *,
        current_date: Optional[str] = None) -> bool:
    """Resume a recoverable pause after its safety condition has cleared.

    Transient Deribit failures resume after a clean close plus one healthy
    scan.  A daily-loss pause resumes only on the first healthy scan of a
    later Beijing calendar day.  Manual and other material locks never resume
    here.
    """
    repo = ctx.repo
    state = await repo.get_state(AUTO_RESUME_STATE_KEY, "")
    pause_date = daily_loss_pause_date(state)
    transient_ready = state == AUTO_RESUME_READY
    daily_ready = bool(
        pause_date and (current_date or beijing_date()) > pause_date)
    if not transient_ready and not daily_ready:
        return False
    if active_parents or await ctx.force_close_active():
        return False
    if getattr(ctx, "current_command_kind", None) == "FORCE_CLOSE":
        return False

    reason = await repo.get_state(PAUSE_REASON_KEY, "")
    await repo.set_flag("trading_enabled", True)
    await repo.set_state(AUTO_RESUME_STATE_KEY, "")
    await repo.set_state(PAUSE_REASON_KEY, "")
    await ev.emit(
        repo.conn, ev.TRADING_AUTO_RESUMED, level="INFO",
        recovered_from=reason,
        recovery_policy=(
            "next_beijing_day" if daily_ready else "healthy_scan"))
    log.warning("风控恢复条件已满足，自动重新启用新开仓")
    return True


async def maybe_resume_testnet_daily_loss_pause(
        ctx: Ctx, *, opts: Optional[list] = None,
        futs: Optional[list] = None, orders: Optional[list] = None,
        summary=None) -> bool:
    """Let Testnet continue after a daily-loss close is provably flat.

    Testnet is used to exercise continuous signal execution.  A daily-loss
    breach still interrupts and flattens the current trade, but the next scan
    or signal may resume only after Deribit confirms there are no option or
    future positions, no open orders, no active DB parent, and no force-close
    command.  Mainnet keeps the stricter cross-day pause.
    """
    repo, cfg = ctx.repo, ctx.cfg
    if not cfg.env.is_testnet:
        return False
    state = await repo.get_state(AUTO_RESUME_STATE_KEY, "")
    if not daily_loss_pause_date(state):
        return False
    if await ctx.force_close_active():
        return False
    if getattr(ctx, "current_command_kind", None) == "FORCE_CLOSE":
        return False
    if await repo.active_parents():
        return False

    try:
        if opts is None:
            opts = await account.get_option_positions(
                ctx.ws, cfg.strategy.currency)
        if futs is None:
            futs = await account.get_future_positions(
                ctx.ws, cfg.strategy.currency)
        if orders is None:
            orders = await account.get_open_orders(
                ctx.ws, cfg.strategy.currency)
        if opts or futs or orders:
            return False
        if summary is None:
            summary = await account.get_account_summary(
                ctx.ws, cfg.strategy.currency)
    except Exception as exc:  # noqa: BLE001 - failed proof must stay paused
        log.warning("Testnet 日亏损暂停复核失败，保持暂停: %s", exc)
        return False

    # Re-read after the network calls so two concurrent scan/signal checks
    # cannot both resume and emit duplicate recovery events.
    if not daily_loss_pause_date(
            await repo.get_state(AUTO_RESUME_STATE_KEY, "")):
        return await ctx.trading_enabled()
    recovered_from = await repo.get_state(PAUSE_REASON_KEY, "")
    today = beijing_date()
    await repo.set_state("day_start_date", today)
    await repo.set_state("day_start_equity", str(summary.equity_btc))
    await repo.set_flag("trading_enabled", True)
    await repo.set_state(AUTO_RESUME_STATE_KEY, "")
    await repo.set_state(PAUSE_REASON_KEY, "")
    await ev.emit(
        repo.conn, ev.TRADING_AUTO_RESUMED, level="INFO",
        recovered_from=recovered_from,
        recovery_policy="testnet_flat_confirmation")
    log.warning("Testnet 已确认账户清仓，重置测试基准并恢复新开仓")
    return True


async def run_risk_checks(
        ctx: Ctx, opts: list, futs: list, summary=None,
        positions_snapshot_started_ts: Optional[int] = None,
        current_date: Optional[str] = None) -> Optional[RiskBreach]:
    """每次扫描附带的风控检查。返回触发原因（None=正常）。"""
    repo, cfg = ctx.repo, ctx.cfg
    summ = summary or await account.get_account_summary(
        ctx.ws, cfg.strategy.currency)

    # 日初权益快照（北京时间自然日）
    today = current_date or beijing_date()
    if await repo.get_state("day_start_date") != today:
        await repo.set_state("day_start_date", today)
        await repo.set_state("day_start_equity", str(summ.equity_btc))
    day_start = float(await repo.get_state("day_start_equity") or summ.equity_btc)

    # 1) 保证金比例
    if summ.margin_ratio > cfg.risk.margin_ratio_limit:
        return RiskBreach(
            f"维持保证金率 {summ.margin_ratio:.1%} > "
            f"{cfg.risk.margin_ratio_limit:.0%}")
    # 2) 日亏损
    if day_start > 0:
        loss = (day_start - summ.equity_btc) / day_start
        if loss > cfg.risk.daily_loss_limit_pct:
            return RiskBreach(
                f"日亏损 {loss:.1%} 超过限制 "
                f"{cfg.risk.daily_loss_limit_pct:.0%}",
                resume_after_date=today)
    # 3) Delta 异常扩大
    net_delta = sum(p.delta for p in opts) + sum(f.delta_btc for f in futs)
    if abs(net_delta) > cfg.risk.delta_anomaly_btc:
        return RiskBreach(
            f"净 Delta {net_delta:+.3f} BTC 异常扩大"
            f"(> {cfg.risk.delta_anomaly_btc})")
    # 4) 本系统持仓与 DB 一致性
    mismatch = await _check_consistency(
        ctx, opts,
        positions_snapshot_started_ts=positions_snapshot_started_ts)
    if mismatch:
        return RiskBreach(mismatch)
    return None


async def _check_consistency(
        ctx: Ctx, opts: list, *,
        positions_snapshot_started_ts: Optional[int] = None) -> Optional[str]:
    repo = ctx.repo
    cfg = ctx.cfg
    by_instr = {p.instrument: p.size for p in opts}
    ours: set[str] = set()
    expected: dict[str, float] = {}
    owners: dict[str, list[int]] = {}
    for parent in await repo.open_parents():
        opened_at = int(parent.get("opened_at") or 0)
        if (positions_snapshot_started_ts is not None
                and opened_at >= positions_snapshot_started_ts):
            # The account snapshot began before this parent finished opening.
            # Comparing it with the newly committed DB quantity would create a
            # false mismatch on the final fill (for example 2.4 vs 2.9).  The
            # next independent scan will verify the complete position.
            continue
        groups = await repo.groups_of(parent["id"])
        closed = sum(g["closed_qty"] or 0.0 for g in groups)
        remaining = round((parent["qty_total"] or 0.0) - closed, 10)
        if remaining < cfg.strategy.min_lot - EPS:
            continue
        ours.add(parent["sell_instrument"])
        ours.add(parent["buy_instrument"])
        sell = parent["sell_instrument"]
        buy = parent["buy_instrument"]
        expected[sell] = expected.get(sell, 0.0) - remaining
        expected[buy] = expected.get(buy, 0.0) + remaining
        owners.setdefault(sell, []).append(parent["id"])
        owners.setdefault(buy, []).append(parent["id"])
    for instrument, expected_size in expected.items():
        actual_size = float(by_instr.get(instrument, 0.0))
        if abs(actual_size - expected_size) <= cfg.strategy.min_lot / 2:
            continue
        role = "卖腿" if expected_size < 0 else "买腿"
        parent_ids = owners[instrument]
        owner = (f"parent {parent_ids[0]}" if len(parent_ids) == 1
                 else f"parents {','.join(map(str, parent_ids))}")
        return (f"{owner} {role}不一致: DB={abs(expected_size):.1f} "
                f"交易所={abs(actual_size):.1f} ({instrument})")
    # 与本系统无关的卖方仓位：只告警（阻止新开仓已由开仓前检查覆盖）
    foreign_shorts = [i for i, s in by_instr.items() if s < 0 and i not in ours]
    if foreign_shorts:
        log.debug("账户存在非本系统卖方仓位: %s（不强平，仅阻止新开仓）", foreign_shorts)
    return None


async def _force_close_claims(repo, parents: list[dict]) -> dict[tuple[str, str], list[dict]]:
    """Freeze how much of each exchange position belongs to each parent."""
    claims: dict[tuple[str, str], list[dict]] = {}
    for parent in parents:
        groups = await repo.groups_of(parent["id"])
        remaining = (
            sum(max(0.0, float(group.get("qty") or 0.0)
                    - float(group.get("closed_qty") or 0.0))
                for group in groups)
            if groups else float(parent.get("qty_total") or 0.0)
        )
        if remaining <= EPS:
            continue
        for instrument, direction in (
                (parent.get("sell_instrument"), "buy"),
                (parent.get("buy_instrument"), "sell")):
            if instrument:
                claims.setdefault((instrument, direction), []).append({
                    "parent_id": parent["id"],
                    "remaining": remaining,
                })
    return claims


def _allocate_force_close_fill(
        claims: dict[tuple[str, str], list[dict]], instrument: str,
        direction: str, qty: float) -> list[tuple[int, float]]:
    """Allocate one account-level close fill to its parent trades."""
    remaining = max(0.0, float(qty or 0.0))
    allocations: list[tuple[int, float]] = []
    for claim in claims.get((instrument, direction), []):
        if remaining <= EPS:
            break
        allocated = min(remaining, float(claim["remaining"]))
        if allocated <= EPS:
            continue
        allocations.append((int(claim["parent_id"]), allocated))
        claim["remaining"] = max(0.0, float(claim["remaining"]) - allocated)
        remaining = max(0.0, remaining - allocated)
    return allocations


async def _force_close_realized_pnl(
        repo, parent: dict, groups: list[dict]) -> Optional[float]:
    """Return PnL for the still-open quantity when both close legs are known."""
    if not hasattr(repo, "close_cashflows"):
        return None
    remaining_qty = sum(max(
        0.0, float(group.get("qty") or 0.0)
        - float(group.get("closed_qty") or 0.0))
        for group in groups
        if group.get("state") not in ("CLOSED", "FORCE_CLOSED", "SETTLED"))
    if remaining_qty <= EPS:
        return 0.0
    buy_cost, sell_revenue = await repo.close_cashflows(parent["id"])
    if buy_cost <= EPS and sell_revenue <= EPS:
        return None
    close_fees = 0.0
    if hasattr(repo, "close_fees"):
        close_fees = await repo.close_fees(parent["id"])
    total_qty = (
        sum(float(group.get("qty") or 0.0) for group in groups)
        if groups else float(parent.get("qty_total") or 0.0)
    )
    total_realized = (
        float(parent.get("net_credit_btc") or 0.0) * total_qty
        - buy_cost + sell_revenue - close_fees
    )
    already_realized = sum(
        float(group.get("realized_pnl_btc") or 0.0) for group in groups)
    return total_realized - already_realized


async def handle_force_close(ctx: Ctx, payload: dict) -> None:
    """FORCE_CLOSE 统一执行函数（source=RISK_ENGINE / MANUAL）。幂等可重入。"""
    source = payload.get("source", "MANUAL")
    reason = payload.get("reason", "")
    auto_resume = bool(payload.get("auto_resume")) and source == "RISK_ENGINE"
    repo, cfg = ctx.repo, ctx.cfg
    code = ev.FORCE_CLOSE_RISK if source == "RISK_ENGINE" else ev.FORCE_CLOSE_MANUAL
    log.warning("FORCE_CLOSE 开始 source=%s reason=%s", source, reason)

    # 1. 停止新的开仓与 Delta 加仓。用户只需要一个开仓总开关；
    # force_close_active 不再作为清仓后的持久锁。
    await repo.set_flag("trading_enabled", False)
    await repo.set_flag("force_close_active", False)
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
    incomplete: list[str] = []
    failures: list[dict] = []
    opts = await account.get_option_positions(ctx.ws, cfg.strategy.currency)
    force_close_parents = await repo.active_parents()
    residual_reader = getattr(repo, "residual_parents", None)
    if callable(residual_reader):
        known_parent_ids = {parent["id"] for parent in force_close_parents}
        force_close_parents.extend(
            parent for parent in await residual_reader()
            if parent["id"] not in known_parent_ids)
    close_claims = await _force_close_claims(repo, force_close_parents)
    # Account close removes short risk first and keeps protective long legs
    # until every short option has had its five-batch Maker-first close.
    opts = sorted(opts, key=lambda position: not position.is_short)
    n_exec_groups = int(getattr(
        getattr(cfg, "execution", None), "n_exec_groups", 5))
    attempt_waits = tuple(getattr(
        getattr(cfg, "execution", None), "attempt_waits", (3.0, 8.0, 15.0)))
    for p in opts:
        qty = round(abs(p.size), 10)
        if qty < cfg.strategy.min_lot - EPS:
            continue
        try:
            if p.is_short:
                res = await adapter.close_sell_leg_only(
                    ctx.ws, sell_instrument=p.instrument, qty=qty,
                    min_lot=cfg.strategy.min_lot,
                    n_exec_groups=n_exec_groups,
                    attempt_waits=attempt_waits)
            else:
                res = await adapter.close_long_leg(
                    ctx.ws, instrument=p.instrument, qty=qty,
                    min_lot=cfg.strategy.min_lot,
                    n_exec_groups=n_exec_groups,
                    attempt_waits=attempt_waits)
            direction = "buy" if p.is_short else "sell"
            price = res.buy_back_avg or res.sell_out_avg
            allocations = _allocate_force_close_fill(
                close_claims, p.instrument, direction, res.closed_qty)
            allocated_qty = sum(qty for _, qty in allocations)
            fee_total = float(getattr(res, "fee_btc", 0.0) or 0.0)
            for parent_id, allocated in allocations:
                fee = fee_total * allocated / res.closed_qty if res.closed_qty else 0.0
                await repo.record_fill(
                    parent_id=parent_id, group_id=None,
                    instrument=p.instrument, direction=direction,
                    qty=allocated, price=price, fee=fee, phase="close")
            unattributed = max(0.0, res.closed_qty - allocated_qty)
            if unattributed > EPS:
                fee = fee_total * unattributed / res.closed_qty if res.closed_qty else 0.0
                await repo.record_fill(
                    parent_id=None, group_id=None,
                    instrument=p.instrument, direction=direction,
                    qty=unattributed, price=price, fee=fee, phase="close")
            for parent in force_close_parents:
                if p.instrument in (
                        parent.get("sell_instrument"),
                        parent.get("buy_instrument")):
                    await repo.set_liquidity_role(
                        parent["id"], "close", res.liquidity_role)
            if not res.complete:
                incomplete.append(p.instrument)
                failures.append({
                    "instrument": p.instrument,
                    "error": "execution_incomplete",
                    "closed_qty": res.closed_qty,
                    "requested_qty": qty,
                })
        except Exception as exc:  # noqa: BLE001
            log.error("强平 %s 失败: %s", p.instrument, exc)
            incomplete.append(p.instrument)
            failures.append({
                "instrument": p.instrument,
                "error": str(exc),
                "closed_qty": 0.0,
                "requested_qty": qty,
            })

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

    remaining = {p.instrument: abs(p.size) for p in opts_after}
    for instrument, qty in remaining.items():
        if instrument not in incomplete:
            incomplete.append(instrument)
            failures.append({
                "instrument": instrument,
                "error": "position_still_open",
                "remaining_qty": qty,
            })

    # 7. 按父策略核对实际期权腿。账户内另一策略仍有残余时，已经完全归零的
    # 策略也必须单独结案；否则页面会把已平掉的 CALL/PUT 继续显示为活跃仓位。
    # 永续是账户级净仓，只有实际永续也归零时才能安全结案任一父策略。
    closed_parent_ids: list[int] = []
    quarantined_parent_ids: list[int] = []
    pending_parent_ids: list[int] = []
    if abs(perp_usd) < cfg.hedge.perp_contract_usd:
        closed_parent_ids = await reconcile_force_closed_parents(
            ctx, opts_after, code=code, source=source, reason=reason,
            force_all=clean, parents=force_close_parents)
        pending_parent_ids = await mark_residual_long_decision_required(
            ctx, opts_after, source=source, reason=reason)

    pause_state = await repo.get_state(AUTO_RESUME_STATE_KEY, "")
    if clean and auto_resume:
        await repo.set_state(AUTO_RESUME_STATE_KEY, AUTO_RESUME_READY)
    elif clean and (
            pause_state == AUTO_RESUME_LOCKED
            or daily_loss_pause_date(pause_state)):
        # Preserve the material/daily latch after a clean close.  This stops
        # the independent scanner from enqueuing another identical close every
        # three minutes and lets a daily pause resume on the next Beijing day.
        pass
    else:
        # Manual closes, material risk limits, and incomplete closes must never
        # be re-enabled by a stale transient-recovery marker.
        await repo.set_state(AUTO_RESUME_STATE_KEY, "")

    await ev.emit(repo.conn, code, level="ERROR", reason=reason, stage="done",
                  clean=clean, incomplete=incomplete, failures=failures,
                  perp_usd=perp_usd,
                  closed_parent_ids=closed_parent_ids,
                  quarantined_parent_ids=quarantined_parent_ids,
                  residual_decision_parent_ids=pending_parent_ids,
                  auto_resume_ready=bool(clean and auto_resume))
    # 清仓结果无论成功失败都不保留额外的强平锁；失败通过 ERROR 事件显示，
    # trading_enabled 仍保持关闭，用户处理完错误后再主动启用新开仓。
    await repo.set_flag("force_close_active", False)
    ctx.hedge_frozen = False
    if not clean:
        log.error("FORCE_CLOSE 未完全清仓（%s / perp %.0f USD），"
                  "已记录错误；新开仓保持暂停", incomplete, perp_usd)
    else:
        log.warning("FORCE_CLOSE 完成，全部清仓；新开仓保持暂停")


async def reconcile_force_closed_parents(
        ctx: Ctx, option_positions: list, *, code: str,
        source: str, reason: str, force_all: bool = False,
        parents: Optional[list[dict]] = None) -> list[int]:
    """将交易所两腿均已归零的父策略单独结案。

    ``force_all`` 仅用于账户已确认完全清仓的路径，可兼容尚未写入完整腿信息的
    OPENING 记录。账户仍有其他残余时采用保守判断：只有父策略的买卖合约都不在
    实际持仓中才结案，无法证明归零的记录继续保持活跃。
    """
    repo, cfg = ctx.repo, ctx.cfg
    live_instruments = {
        p.instrument for p in option_positions
        if abs(float(p.size)) >= cfg.strategy.min_lot - EPS
    }
    closed_parent_ids: list[int] = []
    parents = list(parents) if parents is not None else await repo.active_parents()
    residual_reader = getattr(repo, "residual_parents", None)
    if callable(residual_reader):
        known = {parent["id"] for parent in parents}
        parents.extend(
            parent for parent in await residual_reader()
            if parent["id"] not in known)
    for parent in parents:
        sell = parent.get("sell_instrument")
        buy = parent.get("buy_instrument")
        if not force_all:
            if not sell or not buy:
                continue
            if sell in live_instruments or buy in live_instruments:
                continue
        groups = await repo.groups_of(parent["id"])
        unsettled = [
            group for group in groups
            if group["state"] not in ("CLOSED", "FORCE_CLOSED", "SETTLED")
        ]
        force_pnl = await _force_close_realized_pnl(repo, parent, groups)
        remaining_qty = sum(max(
            0.0, float(group.get("qty") or 0.0)
            - float(group.get("closed_qty") or 0.0)) for group in unsettled)
        for group in unsettled:
            group_remaining = max(
                0.0, float(group.get("qty") or 0.0)
                - float(group.get("closed_qty") or 0.0))
            realized = None
            if force_pnl is not None:
                existing = float(group.get("realized_pnl_btc") or 0.0)
                allocated = (
                    force_pnl * group_remaining / remaining_qty
                    if remaining_qty > EPS else 0.0)
                realized = existing + allocated
            if group["state"] not in ("CLOSED", "FORCE_CLOSED"):
                await repo.transition_group(
                    group["id"], "FORCE_CLOSED",
                    closed_qty=float(group.get("qty") or 0.0),
                    realized_pnl=realized, reason=source)
        try:
            await repo.transition_parent(
                parent["id"], "FORCE_CLOSED", code,
                source=source, reason=reason)
        except Exception:  # noqa: BLE001 - 前置态直接置 OPEN_FAILED
            try:
                await repo.transition_parent(
                    parent["id"], "OPEN_FAILED", code)
            except Exception:  # noqa: BLE001
                continue
        hedge_row = await repo.get_hedge(parent["id"])
        if hedge_row:
            await repo.set_hedge_futures(parent["id"], 0.0, 0.0)
        closed_parent_ids.append(parent["id"])
    return closed_parent_ids


async def quarantine_residual_long_parents(
        ctx: Ctx, option_positions: list, *, source: str,
        reason: str, parent_id: int | None = None) -> list[int]:
    """Release the new-entry lock when only a protective long leg remains.

    The real Deribit position and any resting reduce-only sell order remain
    untouched. The parent stays non-terminal so expiry settlement can still
    use the official delivery price, but it is excluded from strategy entry,
    close routing, risk-budget ownership, and Delta hedge targets.
    """
    repo, cfg = ctx.repo, ctx.cfg
    sizes = {
        item.instrument: float(item.size) for item in option_positions
        if abs(float(item.size)) >= cfg.strategy.min_lot - EPS
    }
    quarantined: list[int] = []
    for parent in await repo.active_parents():
        if parent_id is not None and parent["id"] != parent_id:
            continue
        if parent.get("state") not in (
                "OPEN", "PARTIALLY_CLOSED", "SETTLEMENT_PENDING", "CLOSING"):
            continue
        sell = parent.get("sell_instrument")
        buy = parent.get("buy_instrument")
        if not sell or not buy:
            continue
        sell_size = float(sizes.get(sell, 0.0))
        buy_size = float(sizes.get(buy, 0.0))
        if (abs(sell_size) >= cfg.strategy.min_lot - EPS
                or buy_size < cfg.strategy.min_lot - EPS):
            continue
        await repo.set_close_reason(
            parent["id"], "RESIDUAL_LONG_AWAITING_FILL_OR_EXPIRY")
        for group in await repo.groups_of(parent["id"]):
            if group["state"] == "OPEN":
                await repo.update_group_mark(group["id"], 0.0, None)
        await repo.transition_parent(
            parent["id"], "RESIDUAL_LONG", ev.RESIDUAL_LONG_QUARANTINED,
            source=source, reason=reason, instrument=buy,
            remaining_qty=buy_size,
            note="卖方风险腿已归零；保护买腿隔离等待成交或到期")
        hedge_row = await repo.get_hedge(parent["id"])
        if hedge_row:
            await repo.set_hedge_futures(parent["id"], 0.0, 0.0)
        quarantined.append(parent["id"])
    return quarantined


async def mark_residual_long_decision_required(
        ctx: Ctx, option_positions: list, *, source: str,
        reason: str) -> list[int]:
    """Keep a long-only residue visible until the operator chooses an action."""
    repo, cfg = ctx.repo, ctx.cfg
    sizes = {
        item.instrument: float(item.size) for item in option_positions
        if abs(float(item.size)) >= cfg.strategy.min_lot - EPS
    }
    pending: list[int] = []
    for parent in await repo.active_parents():
        sell = parent.get("sell_instrument")
        buy = parent.get("buy_instrument")
        if not sell or not buy:
            continue
        sell_size = float(sizes.get(sell, 0.0))
        buy_size = float(sizes.get(buy, 0.0))
        if (abs(sell_size) >= cfg.strategy.min_lot - EPS
                or buy_size < cfg.strategy.min_lot - EPS):
            continue
        if parent["state"] != "CLOSING":
            await repo.transition_parent(
                parent["id"], "CLOSING", ev.RESIDUAL_LONG_DECISION_REQUIRED,
                source=source, reason=reason, instrument=buy,
                remaining_qty=buy_size)
        else:
            await ev.emit(
                repo.conn, ev.RESIDUAL_LONG_DECISION_REQUIRED,
                parent_id=parent["id"], source=source, reason=reason,
                instrument=buy, remaining_qty=buy_size)
        await repo.set_close_reason(parent["id"], RESIDUAL_DECISION_REASON)
        pending.append(parent["id"])
    return pending


async def recover_terminal_late_fills(
    ctx: Ctx,
    positions: Optional[list] = None,
    *,
    ignore_parent_id: Optional[int] = None,
) -> Optional[bool]:
    """Flatten a terminal trade's provable, strategy-labelled late fills.

    Returns True after a matched residue is flattened, False if an attributed
    residue was found but could not be flattened, and None when positions
    cannot be attributed safely.  Manual or other-strategy positions therefore
    remain untouched and continue to block new short-option entries.
    """
    from . import closer

    repo, cfg = ctx.repo, ctx.cfg
    active = [
        parent for parent in await repo.active_parents()
        if parent["id"] != ignore_parent_id
    ]
    residual_reader = getattr(repo, "residual_parents", None)
    residuals = await residual_reader() if callable(residual_reader) else []
    if active or residuals:
        return None

    positions = positions if positions is not None else (
        await account.get_option_positions(ctx.ws, cfg.strategy.currency))
    if not positions:
        return None
    position_by_instrument = {
        item.instrument: float(item.size) for item in positions
    }
    now_ms = int(time.time() * 1000)
    min_lot = cfg.strategy.min_lot

    for parent in await repo.recent_terminal_parents(limit=50):
        closed_at = int(parent.get("closed_at") or 0)
        if closed_at <= 0 or now_ms - closed_at > LATE_FILL_RECOVERY_WINDOW_MS:
            continue
        instruments = {
            parent.get("sell_instrument"), parent.get("buy_instrument")}
        instruments.discard(None)
        if not instruments or not set(position_by_instrument).issubset(instruments):
            continue
        qty_total = float(parent.get("qty_total") or 0.0)
        if qty_total <= 0 or any(
                abs(position_by_instrument.get(name, 0.0))
                > qty_total + min_lot / 2 for name in instruments):
            continue

        try:
            response = await ctx.ws.request(
                "private/get_user_trades_by_currency_and_time",
                {
                    "currency": cfg.strategy.currency,
                    "kind": "option",
                    "start_timestamp": closed_at + 1,
                    "end_timestamp": now_ms,
                    "count": 1000,
                    "sorting": "asc",
                })
        except Exception as exc:  # noqa: BLE001 - no attribution, no trading
            log.warning(
                "late-fill attribution query failed for parent %s: %s",
                parent["id"], exc)
            continue

        late_net = {name: 0.0 for name in instruments}
        attributed_trades = []
        for trade in response.get("trades", []):
            instrument = trade.get("instrument_name")
            label = str(trade.get("label") or "")
            if (instrument not in instruments
                    or not label.startswith(
                        closer.MANAGED_OPTION_ORDER_LABEL_PREFIXES)):
                continue
            amount = float(trade.get("amount") or 0.0)
            signed = amount if trade.get("direction") == "buy" else -amount
            late_net[instrument] += signed
            attributed_trades.append({
                "trade_id": trade.get("trade_id"),
                "order_id": trade.get("order_id"),
                "timestamp": trade.get("timestamp"),
                "instrument": instrument,
                "direction": trade.get("direction"),
                "amount": amount,
                "label": label,
            })

        if not attributed_trades or any(
                abs(position_by_instrument.get(name, 0.0) - late_net[name])
                >= min_lot / 2 for name in instruments):
            continue

        try:
            if not await closer.cancel_managed_option_orders(ctx, parent):
                return False
            actions = await closer._flatten_managed_option_residuals(
                ctx, parent, keep_buy=False)
            if not await closer.cancel_managed_option_orders(ctx, parent):
                return False
            remaining = await account.get_option_positions(
                ctx.ws, cfg.strategy.currency)
            remaining_managed = {
                item.instrument: item.size for item in remaining
                if item.instrument in instruments
                and abs(float(item.size)) >= min_lot - EPS
            }
            if remaining_managed:
                await ev.emit(
                    repo.conn, ev.CLOSE_RESIDUAL_RETRY, level="ERROR",
                    parent_id=parent["id"],
                    note="attributed late fills remain after recovery",
                    remaining=remaining_managed,
                    attributed_trades=attributed_trades)
                return False
            await ev.emit(
                repo.conn, ev.CLOSE_RESIDUAL_RECOVERY, level="WARN",
                parent_id=parent["id"],
                note="recovered strategy-labelled fills after terminal close",
                source="TERMINAL_LATE_FILL",
                actions=actions,
                attributed_trades=attributed_trades)
            log.warning(
                "recovered terminal late fills for parent %s: %s",
                parent["id"], actions)
            return True
        except Exception as exc:  # noqa: BLE001 - keep entry safety gate closed
            log.exception(
                "terminal late-fill recovery failed for parent %s",
                parent["id"])
            await ev.emit(
                repo.conn, ev.CLOSE_RESIDUAL_RETRY, level="ERROR",
                parent_id=parent["id"],
                note="terminal late-fill recovery failed",
                error=str(exc), attributed_trades=attributed_trades)
            return False
    return None


async def reconcile_on_startup(ctx: Ctx) -> None:
    """崩溃恢复：处理停在中间态（OPENING/CLOSING/HEDGE_REBALANCING）的记录。"""
    repo, cfg = ctx.repo, ctx.cfg
    try:
        positions = await account.get_option_positions(
            ctx.ws, cfg.strategy.currency)
        if positions:
            await recover_terminal_late_fills(ctx, positions)
    except Exception:  # noqa: BLE001 - startup continues with entry blocked
        log.exception("startup terminal late-fill recovery failed")

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


async def handle_opening_recovery(ctx: Ctx, payload: dict) -> None:
    """Serialized command handler for a stranded OPENING trade."""
    await recover_opening_parent(ctx, int(payload["parent_id"]))


async def recover_opening_parent(ctx: Ctx, parent_id: int) -> Optional[dict]:
    """Reconcile one OPENING parent against exchange truth.

    The function is safe to call after an execution exception, from a reverse
    command, or from the queued scanner recovery path.  It always re-reads the
    parent first, so duplicate recovery commands become no-ops.
    """
    parent = await ctx.repo.get_parent(parent_id)
    if not parent or parent["state"] != "OPENING":
        return parent

    for instrument in {
            parent.get("sell_instrument"), parent.get("buy_instrument")}:
        if not instrument:
            continue
        try:
            await ctx.ws.request(
                "private/cancel_all_by_instrument",
                {"instrument_name": instrument})
        except Exception as exc:  # noqa: BLE001 - positions remain truth source
            log.warning(
                "OPENING recovery parent=%s cancel %s failed: %s",
                parent_id, instrument, exc)

    try:
        opts = await account.get_option_positions(
            ctx.ws, ctx.cfg.strategy.currency)
        await _reconcile_opening(
            ctx, parent, {p.instrument: p.size for p in opts})
    except Exception as exc:  # noqa: BLE001 - retain OPENING for queued retry
        log.exception("OPENING recovery parent=%s failed", parent_id)
        await ev.emit(
            ctx.repo.conn, ev.RECONCILE_MISMATCH, level="ERROR",
            parent_id=parent_id, note="online OPENING recovery failed",
            error=str(exc))
    return await ctx.repo.get_parent(parent_id)


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

    sell_size = float(by_instr.get(sell_i, 0.0))
    buy_size = float(by_instr.get(buy_i, 0.0))

    # Repair positions that point opposite to the intended credit spread
    # before calculating matched quantity.
    if sell_size >= min_lot - EPS:
        await adapter.close_long_leg(
            ctx.ws, instrument=sell_i, qty=sell_size, min_lot=min_lot)
    if buy_size <= -(min_lot - EPS):
        await adapter.close_sell_leg_only(
            ctx.ws, sell_instrument=buy_i, qty=abs(buy_size), min_lot=min_lot)

    positions = await account.get_option_positions(
        ctx.ws, cfg.strategy.currency)
    by_instr = {item.instrument: item.size for item in positions}
    short = max(0.0, -float(by_instr.get(sell_i, 0.0)))
    long_ = max(0.0, float(by_instr.get(buy_i, 0.0)))
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

    # Never adopt a spread from optimistic local arithmetic.  Re-read exchange
    # positions after every repair and require both intended legs to be equal.
    positions = await account.get_option_positions(
        ctx.ws, cfg.strategy.currency)
    by_instr = {item.instrument: item.size for item in positions}
    sell_size = float(by_instr.get(sell_i, 0.0))
    buy_size = float(by_instr.get(buy_i, 0.0))
    short = max(0.0, -sell_size)
    long_ = max(0.0, buy_size)
    remaining_excess = round(long_ - short, 10)
    inverted = (
        sell_size >= min_lot - EPS
        or buy_size <= -(min_lot - EPS)
    )
    if inverted or abs(remaining_excess) >= min_lot - EPS:
        await ev.emit(
            repo.conn, ev.RECONCILE_MISMATCH, level="ERROR",
            parent_id=pid, note="OPENING recovery still has unmatched legs",
            sell_size=sell_size, buy_size=buy_size)
        return

    matched = round(min(short, long_), 10)
    if matched < min_lot - EPS:
        if abs(sell_size) < min_lot - EPS and abs(buy_size) < min_lot - EPS:
            await repo.transition_parent(
                pid, "OPEN_FAILED", ev.RECONCILE_MISMATCH,
                note="reconcile: 交易所无等量持仓")
        else:
            await ev.emit(
                repo.conn, ev.RECONCILE_MISMATCH, level="ERROR",
                parent_id=pid, note="OPENING recovery has unmanaged residue",
                sell_size=sell_size, buy_size=buy_size)
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
        positions = await account.get_option_positions(
            ctx.ws, cfg.strategy.currency)
        remaining = {
            item.instrument: item.size for item in positions
            if item.instrument in (sell_i, buy_i)
        }
        if all(abs(size) < min_lot - EPS for size in remaining.values()):
            await repo.transition_parent(
                pid, "OPEN_FAILED", ev.RECONCILE_MISMATCH,
                note="reconcile: 成交记录重建失败，已平掉认领腿")
        else:
            await ev.emit(
                repo.conn, ev.RECONCILE_MISMATCH, level="ERROR",
                parent_id=pid,
                note="reconcile: 成交记录重建失败且平仓未完成",
                remaining=remaining)


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
