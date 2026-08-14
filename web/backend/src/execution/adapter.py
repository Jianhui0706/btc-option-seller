"""V2 执行编排：把执行引擎适配成开仓/平仓的高层操作。

开仓（信用价差，规范第六节 + 用户口径"严格买腿先行"）：
  阶段A  Combo 组合簿原子成交（两腿同时成交，无任何单腿窗口，安全性优于买先）；
  阶段B  剩余量逐组执行：先买保护腿 → 成交后再卖收权腿（卖腿挂单量 ≤ 买腿已成交量，
         任何时刻不存在裸卖）；卖腿带价格保护（≥ 本组买腿均价 + min_credit）。
  收尾    卖腿始终补不齐时，IOC 回滚多余买腿（OPEN_FAILED_UNWOUND 语义由调用方标记）。

平仓：
  双腿全平 → SpreadExecutor 反向（买回卖腿 + 卖出买腿，并行+再平衡，无裸卖风险）；
  只平卖腿（保留买腿）→ SingleLegExecutor 买回。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from ..exchange.deribit_ws import DeribitWS
from ..exchange import futures
from .spread_executor import (EPS, ExecParams, ExecutionIncompleteError,
                              SpreadExecutor, round_lot)
from .single_leg import SingleLegExecutor, SingleLegParams

log = logging.getLogger("v2.exec")

OnEvent = Optional[Callable[[str], None]]


def _merge_liquidity_roles(*roles: str) -> str:
    known = {role for role in roles if role in {"MAKER", "TAKER", "MIXED"}}
    if "MIXED" in known or {"MAKER", "TAKER"} <= known:
        return "MIXED"
    if "MAKER" in known:
        return "MAKER"
    if "TAKER" in known:
        return "TAKER"
    return "UNKNOWN"


def _spread_report_liquidity_role(report) -> str:
    """根据真实成交批次汇总 maker/taker；未成交委托不计入。"""
    roles: list[str] = []
    if report.combo_filled > EPS:
        roles.append("MAKER")  # combo 委托强制 post_only
    for group in report.groups:
        for attempt in group.attempts:
            if attempt.buy_filled + attempt.sell_filled > EPS:
                roles.append("TAKER" if attempt.attempt >= 3 else "MAKER")
    if any(float(item.get("swept") or 0.0) > EPS
           for item in report.leftover.values()):
        roles.append("TAKER")
    if any(float(item.get("filled") or 0.0) > EPS
           for item in report.rebalance):
        roles.append("TAKER")
    return _merge_liquidity_roles(*roles)


@dataclass
class OpenResult:
    qty: float                 # 两腿等量成交张数（=最终登记持仓量）
    net_credit_btc: float      # 每张净收权利金（BTC，加权）
    buy_avg: float             # 腿单独执行部分的买腿均价（combo 部分不可分拆）
    sell_avg: float
    combo_qty: float
    unwound_qty: float         # 被回滚的买腿张数
    liquidity_role: str = "UNKNOWN"
    events: list[str] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)

    @property
    def opened(self) -> bool:
        return self.qty >= 0.1 - EPS


@dataclass
class PerpOrderResult:
    """人工永续委托的分批 Maker-first 执行结果。"""

    requested_usd: float
    filled_usd: float
    avg_price: float
    fee_btc: float = 0.0
    liquidity_role: str = "UNKNOWN"
    events: list[str] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)
    groups: list[dict] = field(default_factory=list)


def _exec_groups(total: float, n: int, min_lot: float) -> list[float]:
    """把剩余量拆成至多 n 个执行批次（每批 ≥ min_lot，和 = total）。"""
    lots = int(round(total / min_lot))
    if lots <= 0:
        return []
    n = max(1, min(n, lots))
    base, rem = divmod(lots, n)
    return [round((base + (1 if i < rem else 0)) * min_lot, 10) for i in range(n)]


async def _spot_quotes(ws: DeribitWS, instrument: str) -> tuple[float, float]:
    t = await ws.request("public/ticker", {"instrument_name": instrument})
    return float(t.get("best_bid_price") or 0.0), float(t.get("best_ask_price") or 0.0)


async def execute_perp_maker_first(
    ws: DeribitWS,
    *,
    instrument: str,
    usd_signed: float,
    min_lot: float = 10.0,
    n_exec_groups: int = 5,
    attempt_waits: tuple = (3.0, 8.0, 15.0),
    reduce_only: bool = False,
    ioc_ticks: int = 10,
    completion_ioc_ticks: tuple[int, ...] = (20, 40),
    target_provider: Optional[Callable[[], Awaitable[float]]] = None,
    on_event: OnEvent = None,
    on_attempt: Optional[Callable[[dict], Awaitable[None]]] = None,
) -> PerpOrderResult:
    """按普通开仓节奏执行人工永续委托。

    最多拆成五个近似等量批次；每批先跑一轮中间价 Maker、靠近
    对手价 Maker、剩余 IOC。若 Testnet 薄盘口只部分成交，则在同一
    批内逐级扩大 IOC 价格范围，完成本批后才进入下一批。
    """
    events: list[str] = []

    def emit(msg: str) -> None:
        events.append(msg)
        if on_event:
            on_event(msg)

    target = round_lot(abs(usd_signed), min_lot)
    groups = _exec_groups(target, n_exec_groups, min_lot)
    if not groups:
        return PerpOrderResult(usd_signed, 0.0, 0.0, events=events)

    filled_signed_total = 0.0
    filled_abs_total = 0.0
    value_total = 0.0
    roles: list[str] = []
    attempts: list[dict] = []
    group_reports: list[dict] = []
    fee_total = 0.0
    latest_target = usd_signed
    initial_target = usd_signed
    cycle_sign = 1.0 if usd_signed > 0 else -1.0
    group_count = len(groups)
    for group_index in range(1, group_count + 1):
        initial_group_qty = groups[group_index - 1]
        if target_provider is not None:
            fresh_target = float(await target_provider())
            fresh_abs = round_lot(abs(fresh_target), min_lot)
            if fresh_abs >= min_lot - EPS and fresh_target * cycle_sign > 0:
                latest_target = cycle_sign * fresh_abs
            elif fresh_target * cycle_sign < 0:
                emit(
                    f"[人工对冲 {group_index}/{group_count}] 最新 Delta 已反向，"
                    "本周期保持原方向，避免来回成交")
        remaining_signed = round(
            latest_target - filled_signed_total, 10)
        remaining_groups = group_count - group_index + 1
        next_groups = _exec_groups(
            abs(remaining_signed), remaining_groups, min_lot)
        if not next_groups:
            break
        group_qty = next_groups[0]
        group_sign = 1.0 if remaining_signed > 0 else -1.0
        direction = "buy" if group_sign > 0 else "sell"
        emit(
            f"[人工对冲 {group_index}/{group_count}] {direction} "
            f"{instrument}，本批目标 {group_qty:.0f} USD")
        attempt_context = {
            "instrument": instrument,
            "direction": direction,
            "phase": "hedge",
            "operation": "manual_perp",
            "group_index": group_index,
            "group_count": group_count,
            "initial_target_usd": initial_target,
            "initial_group_usd": cycle_sign * initial_group_qty,
            "live_target_usd": latest_target,
            "live_group_usd": group_sign * group_qty,
        }

        async def publish_group_attempt(item: dict) -> None:
            if on_attempt:
                await on_attempt({**item, **attempt_context})

        executor_kwargs = {"on_event": emit}
        if on_attempt:
            executor_kwargs["on_attempt"] = publish_group_attempt
        executor = SingleLegExecutor(
            ws, instrument, direction,
            SingleLegParams(
                amount=group_qty,
                min_lot=min_lot,
                attempt_waits=attempt_waits,
                rounds=1,
                pay_extra_ticks=ioc_ticks,
                reduce_only=reduce_only),
            **executor_kwargs)
        report = await executor.execute()
        group_filled = report.filled
        group_value = report.filled * report.avg_price
        group_maker_filled = float(getattr(report, "maker_filled", 0.0))
        group_taker_filled = float(getattr(report, "taker_filled", 0.0))
        fee_total += report.fee_btc
        group_roles = [report.liquidity_role]
        group_attempts = [{**attempt, **attempt_context}
                          for attempt in report.attempts]

        for fallback_no, ticks in enumerate(completion_ioc_ticks, start=1):
            remaining = round_lot(group_qty - group_filled, min_lot)
            if remaining < min_lot - EPS:
                break
            signed_remaining = group_sign * remaining
            emit(
                f"[人工对冲 {group_index}/{group_count}] 本批剩余 "
                f"{remaining:.0f} USD，IOC {ticks} ticks 补齐")
            fallback_started = time.time()
            fallback = await futures.perp_ioc(
                ws, instrument, signed_remaining,
                ioc_ticks=ticks, reduce_only=reduce_only)
            fallback_finished = time.time()
            fallback_filled = abs(fallback.filled_usd)
            group_value += fallback_filled * fallback.avg_price
            group_filled = round_lot(
                group_filled + fallback_filled, min_lot)
            fee_total += fallback.fee_btc
            if fallback_filled > EPS:
                group_roles.append("TAKER")
                group_taker_filled += fallback_filled
            fallback_attempt = {
                "instrument": instrument,
                "direction": direction,
                "phase": "hedge",
                "operation": "manual_perp_completion",
                "group_index": group_index,
                "group_count": group_count,
                "initial_target_usd": initial_target,
                "initial_group_usd": cycle_sign * initial_group_qty,
                "live_target_usd": latest_target,
                "live_group_usd": group_sign * group_qty,
                "attempt_no": 3 + fallback_no,
                "round": 1,
                "liquidity_role": "TAKER",
                "requested_qty": remaining,
                "filled_qty": fallback_filled,
                "limit_price": None,
                "avg_fill_price": fallback.avg_price or None,
                "status": (
                    "FILLED" if fallback_filled + EPS >= remaining
                    else "PARTIAL" if fallback_filled > EPS else "CANCELED"),
                "ioc_ticks": ticks,
                "placed_at": int(fallback_started * 1000),
                "finished_at": int(fallback_finished * 1000),
                "duration_ms": int((fallback_finished - fallback_started) * 1000),
                "exchange_order_id": fallback.order_id,
                "exchange_trade_ids": fallback.trade_ids or [],
            }
            group_attempts.append(fallback_attempt)
            if on_attempt:
                await on_attempt(dict(fallback_attempt))

        filled_signed_total = round(
            filled_signed_total + group_sign * group_filled, 10)
        filled_abs_total = round_lot(
            filled_abs_total + group_filled, min_lot)
        value_total += group_value
        group_role = _merge_liquidity_roles(*group_roles)
        roles.append(group_role)
        attempts.extend(group_attempts)
        group_reports.append({
            "group_index": group_index,
            "group_count": group_count,
            "initial_target_usd": initial_target,
            "initial_group_usd": cycle_sign * initial_group_qty,
            "live_target_usd": latest_target,
            "dynamically_recalculated": abs(
                group_qty - initial_group_qty) >= min_lot - EPS,
            "requested_usd": group_sign * group_qty,
            "filled_usd": group_sign * group_filled,
            "avg_price": (
                group_value / group_filled if group_filled > EPS else 0.0),
            "liquidity_role": group_role,
            "maker_filled_usd": group_maker_filled,
            "taker_filled_usd": group_taker_filled,
            "complete": group_filled + EPS >= group_qty,
        })
        if group_filled + EPS < group_qty:
            emit(
                f"[人工对冲 {group_index}/{group_count}] 仍未完成，"
                "停止后续批次并交由安全重试")
            break

    average = value_total / filled_abs_total if filled_abs_total > EPS else 0.0
    return PerpOrderResult(
        requested_usd=latest_target,
        filled_usd=filled_signed_total,
        avg_price=average,
        fee_btc=fee_total,
        liquidity_role=_merge_liquidity_roles(*roles),
        events=events,
        attempts=attempts,
        groups=group_reports,
    )


async def open_credit_spread(
    ws: DeribitWS,
    *,
    buy_instrument: str,          # 保护买腿
    sell_instrument: str,         # 收权卖腿
    qty: float,
    min_credit_btc: float,
    min_lot: float = 0.1,
    n_exec_groups: int = 5,
    attempt_waits: tuple = (3.0, 8.0, 15.0),
    combo_timeout: float = 45.0,
    report_dir: Optional[str] = None,
    dry_run: bool = False,
    on_event: OnEvent = None,
) -> OpenResult:
    events: list[str] = []

    def emit(msg: str) -> None:
        events.append(msg)
        if on_event:
            on_event(msg)

    # ---------------- 阶段A：Combo 组合簿原子成交 ----------------
    combo_qty = 0.0
    combo_credit_total = 0.0
    params = ExecParams(
        amount=qty,
        use_combo=True,
        combo_only=True,
        combo_timeout=combo_timeout,
        strict_five_groups=False,
        max_net_debit=-min_credit_btc,   # 净支出 ≤ -min_credit ⇔ 净收权利金 ≥ min_credit
        min_lot=min_lot,
        dry_run=dry_run,
        write_report=report_dir is not None,
        report_dir=report_dir,
        operation_kind="open",
    )
    ex = SpreadExecutor(ws, buy_instrument, sell_instrument, params, on_event=emit)
    try:
        rep = await ex.execute()
        combo_qty = rep.combo_filled
        if combo_qty > EPS:
            # 组合净价按 +买腿−卖腿 定义；我方下买单成交价为负 ⇔ 收权利金
            credit_per = -rep.combo_avg_net if ex.combo_side == "buy" else rep.combo_avg_net
            combo_credit_total = credit_per * combo_qty
            emit(f"combo 阶段成交 {combo_qty:.1f} 张，净权利金 {credit_per:.4f}/张")
    except ExecutionIncompleteError as exc:   # combo_only 不应抛，防御处理
        combo_qty = exc.report.combo_filled
    except Exception as exc:  # noqa: BLE001 - combo 失败不阻塞买先路径
        emit(f"combo 阶段异常({exc})，转入买腿先行")

    if dry_run:
        emit("dry-run：跳过买先阶段真实下单")
        return OpenResult(qty=0.0, net_credit_btc=0.0, buy_avg=0.0, sell_avg=0.0,
                          combo_qty=0.0, unwound_qty=0.0,
                          liquidity_role="UNKNOWN", events=events)

    # ---------------- 阶段B：剩余量买腿先行逐组执行 ----------------
    remaining = round_lot(qty - combo_qty, min_lot)
    buy_filled = 0.0
    buy_cost = 0.0
    sell_filled = 0.0
    sell_revenue = 0.0
    liquidity_roles = ["MAKER"] if combo_qty > EPS else []
    attempts: list[dict] = []

    for gi, g_qty in enumerate(_exec_groups(remaining, n_exec_groups, min_lot), start=1):
        # 组前健康检查：当前盘口是否仍满足最低净权利金
        sell_bid, _ = await _spot_quotes(ws, sell_instrument)
        _, buy_ask = await _spot_quotes(ws, buy_instrument)
        if sell_bid <= 0 or buy_ask <= 0:
            emit(f"[组{gi}] 盘口缺失，停止后续开仓")
            break
        if sell_bid - buy_ask < min_credit_btc - EPS:
            emit(f"[组{gi}] 当前净权利金 {sell_bid - buy_ask:.4f} < {min_credit_btc}，停止后续开仓")
            break

        # 1) 买保护腿
        buy_ex = SingleLegExecutor(ws, buy_instrument, "buy", SingleLegParams(
            amount=g_qty, min_lot=min_lot, attempt_waits=attempt_waits), on_event=emit)
        brep = await buy_ex.execute()
        attempts.extend([{**a, "instrument": buy_instrument, "direction": "buy", "phase": "open"} for a in brep.attempts])
        liquidity_roles.append(brep.liquidity_role)
        if brep.filled < min_lot - EPS:
            emit(f"[组{gi}] 买腿未成交，停止后续开仓")
            break
        buy_filled = round_lot(buy_filled + brep.filled, min_lot)
        buy_cost += brep.filled * brep.avg_price

        # 2) 卖收权腿（数量 = 本组买腿实际成交；价格保护 ≥ 买腿均价 + min_credit）
        sell_ex = SingleLegExecutor(ws, sell_instrument, "sell", SingleLegParams(
            amount=brep.filled, min_lot=min_lot, attempt_waits=attempt_waits,
            limit_price=round(brep.avg_price + min_credit_btc, 10)), on_event=emit)
        srep = await sell_ex.execute()
        attempts.extend([{**a, "instrument": sell_instrument, "direction": "sell", "phase": "open"} for a in srep.attempts])
        liquidity_roles.append(srep.liquidity_role)
        sell_filled = round_lot(sell_filled + srep.filled, min_lot)
        sell_revenue += srep.filled * srep.avg_price
        if srep.filled + EPS < brep.filled:
            emit(f"[组{gi}] 卖腿仅成交 {srep.filled:.1f}/{brep.filled:.1f}，停止后续开仓")
            break

    # ---------------- 收尾：回滚未被卖腿覆盖的买腿 ----------------
    unwound = 0.0
    excess = round_lot(buy_filled - sell_filled, min_lot)
    if excess >= min_lot - EPS:
        emit(f"卖腿缺口 {excess:.1f} 张，回滚多余买腿（IOC 卖出平掉）")
        un_ex = SingleLegExecutor(ws, buy_instrument, "sell", SingleLegParams(
            amount=excess, min_lot=min_lot, attempt_waits=(2.0, 4.0, 6.0),
            rounds=3, reduce_only=True),
            on_event=emit)
        urep = await un_ex.execute()
        attempts.extend([{**a, "instrument": buy_instrument, "direction": "sell", "phase": "unwind"} for a in urep.attempts])
        liquidity_roles.append(urep.liquidity_role)
        unwound = urep.filled
        buy_filled = round_lot(buy_filled - urep.filled, min_lot)
        buy_cost -= urep.filled * urep.avg_price   # 回滚收回的权利金冲减买腿成本
        if urep.filled + EPS < excess:
            emit(f"⚠ 回滚未完成，仍有 {round_lot(excess - urep.filled, min_lot):.1f} 张多余买腿，需人工处理")

    legged_qty = min(buy_filled, sell_filled)
    total_qty = round_lot(combo_qty + legged_qty, min_lot)
    legged_credit = sell_revenue - buy_cost
    total_credit = combo_credit_total + legged_credit
    net_credit = (total_credit / total_qty) if total_qty > EPS else 0.0

    buy_avg = buy_cost / buy_filled if buy_filled > EPS else 0.0
    sell_avg = sell_revenue / sell_filled if sell_filled > EPS else 0.0
    emit(f"开仓执行结束：等量 {total_qty:.1f} 张（combo {combo_qty:.1f} + 腿 {legged_qty:.1f}），"
         f"净权利金 {net_credit:.4f}/张，回滚 {unwound:.1f} 张")
    return OpenResult(qty=total_qty, net_credit_btc=net_credit, buy_avg=buy_avg,
                      sell_avg=sell_avg, combo_qty=combo_qty, unwound_qty=unwound,
                      liquidity_role=_merge_liquidity_roles(*liquidity_roles),
                      events=events, attempts=attempts)


@dataclass
class CloseResult:
    closed_qty: float
    buy_back_avg: float        # 买回卖腿均价（BTC/张）
    sell_out_avg: float        # 卖出买腿均价（双腿全平时）
    complete: bool
    fee_btc: float = 0.0
    liquidity_role: str = "UNKNOWN"
    events: list[str] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)


async def close_spread_both_legs(
    ws: DeribitWS,
    *,
    sell_instrument: str,      # 原卖腿（现在买回）
    buy_instrument: str,       # 原买腿（现在卖出）
    qty: float,
    min_lot: float = 0.1,
    n_exec_groups: int = 5,
    report_dir: Optional[str] = None,
    on_event: OnEvent = None,
) -> CloseResult:
    """双腿全平：SpreadExecutor 反向执行（buy=原卖腿, sell=原买腿），
    并行+落后腿 IOC 补齐+终局再平衡，全程不会产生新的裸卖。"""
    events: list[str] = []

    def emit(msg: str) -> None:
        events.append(msg)
        if on_event:
            on_event(msg)

    params = ExecParams(
        amount=qty,
        # Combo instruments cannot constrain each component with reduce_only.
        # Close through the two real option instruments so every order is
        # position-reducing and can never create an inverted spread.
        use_combo=False,
        strict_five_groups=False,
        n_groups=n_exec_groups,
        taker_fallback=True,
        min_lot=min_lot,
        write_report=report_dir is not None,
        report_dir=report_dir,
        operation_kind="close",
    )
    ex = SpreadExecutor(ws, sell_instrument, buy_instrument, params, on_event=emit)
    complete = True
    try:
        rep = await ex.execute()
    except ExecutionIncompleteError as exc:
        rep = exc.report
        complete = False
        emit(f"平仓未完整完成：{exc}")
    closed = min(rep.leg_filled.get(sell_instrument, 0.0),
                 rep.leg_filled.get(buy_instrument, 0.0))
    attempts = []
    for group in rep.groups:
        for a in group.attempts:
            role = "TAKER" if a.attempt >= 3 else "MAKER"
            attempts.extend([
                {"phase": "close", "instrument": sell_instrument, "direction": "buy", "liquidity_role": role, "attempt_no": a.attempt, "requested_qty": a.buy_qty, "filled_qty": a.buy_filled, "limit_price": a.buy_price, "status": "FILLED" if a.buy_filled > EPS else "CANCELED"},
                {"phase": "close", "instrument": buy_instrument, "direction": "sell", "liquidity_role": role, "attempt_no": a.attempt, "requested_qty": a.sell_qty, "filled_qty": a.sell_filled, "limit_price": a.sell_price, "status": "FILLED" if a.sell_filled > EPS else "CANCELED"},
            ])
    return CloseResult(
        closed_qty=round_lot(closed, min_lot),
        buy_back_avg=rep.leg_avg_price.get(sell_instrument, 0.0),
        sell_out_avg=rep.leg_avg_price.get(buy_instrument, 0.0),
        complete=complete,
        liquidity_role=_spread_report_liquidity_role(rep),
        events=events,
        attempts=attempts,
    )


async def close_sell_leg_only(
    ws: DeribitWS,
    *,
    sell_instrument: str,
    qty: float,
    min_lot: float = 0.1,
    n_exec_groups: int = 1,
    attempt_waits: tuple = (3.0, 8.0, 15.0),
    on_event: OnEvent = None,
) -> CloseResult:
    """只买回卖腿；账户全平可指定五批 Maker 优先执行。"""
    return await _close_single_leg(
        ws, instrument=sell_instrument, direction="buy", qty=qty,
        min_lot=min_lot, n_exec_groups=n_exec_groups,
        attempt_waits=attempt_waits, on_event=on_event)


async def _close_single_leg(
    ws: DeribitWS,
    *,
    instrument: str,
    direction: str,
    qty: float,
    min_lot: float,
    n_exec_groups: int,
    attempt_waits: tuple,
    on_event: OnEvent,
) -> CloseResult:
    """Close one real position, optionally in cumulative Maker-first batches.

    A grouped close uses one three-step ladder per batch (mid Maker, 2/3-touch
    Maker, IOC). Any unfilled amount is carried into the next cumulative
    target, so the fifth batch becomes the final taker backstop.
    """
    events: list[str] = []

    def emit(msg: str) -> None:
        events.append(msg)
        if on_event:
            on_event(msg)

    groups = _exec_groups(qty, n_exec_groups, min_lot)
    grouped = len(groups) > 1
    if not groups:
        return CloseResult(0.0, 0.0, 0.0, True, events=events)

    filled_total = 0.0
    value_total = 0.0
    fee_total = 0.0
    cumulative_target = 0.0
    roles: list[str] = []
    attempts: list[dict] = []
    for group_index, group_qty in enumerate(groups, start=1):
        cumulative_target = round_lot(
            cumulative_target + group_qty, min_lot)
        todo = max(0.0, round_lot(
            cumulative_target - filled_total, min_lot))
        if todo < min_lot - EPS:
            continue
        if grouped:
            emit(
                f"[账户全平 {group_index}/{len(groups)}] {direction} "
                f"{instrument}，本轮目标 {todo:.1f} 张")
        ex = SingleLegExecutor(
            ws, instrument, direction,
            SingleLegParams(
                amount=todo, min_lot=min_lot,
                attempt_waits=attempt_waits,
                rounds=1 if grouped else 3,
                reduce_only=True),
            on_event=emit)
        rep = await ex.execute()
        filled_total = round_lot(filled_total + rep.filled, min_lot)
        value_total += rep.filled * rep.avg_price
        roles.append(rep.liquidity_role)
        fee_total += float(getattr(rep, "fee_btc", 0.0) or 0.0)
        attempts.extend([
            {**attempt, "instrument": instrument, "direction": direction,
             "phase": "close", "group_index": group_index}
            for attempt in rep.attempts
        ])

    average = value_total / filled_total if filled_total > EPS else 0.0
    complete = filled_total + EPS >= qty
    return CloseResult(
        closed_qty=round_lot(filled_total, min_lot),
        buy_back_avg=average if direction == "buy" else 0.0,
        sell_out_avg=average if direction == "sell" else 0.0,
        complete=complete,
        fee_btc=fee_total,
        liquidity_role=_merge_liquidity_roles(*roles),
        events=events,
        attempts=attempts)


async def close_long_leg(
    ws: DeribitWS,
    *,
    instrument: str,
    qty: float,
    min_lot: float = 0.1,
    n_exec_groups: int = 1,
    attempt_waits: tuple = (3.0, 8.0, 15.0),
    on_event: OnEvent = None,
) -> CloseResult:
    """卖出平掉买方长腿；账户全平可指定五批 Maker 优先执行。"""
    return await _close_single_leg(
        ws, instrument=instrument, direction="sell", qty=qty,
        min_lot=min_lot, n_exec_groups=n_exec_groups,
        attempt_waits=attempt_waits, on_event=on_event)
