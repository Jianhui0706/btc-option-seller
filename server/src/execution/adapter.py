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
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..exchange.deribit_ws import DeribitWS
from .spread_executor import (EPS, ExecParams, ExecutionIncompleteError,
                              SpreadExecutor, round_lot)
from .single_leg import SingleLegExecutor, SingleLegParams

log = logging.getLogger("v2.exec")

OnEvent = Optional[Callable[[str], None]]


@dataclass
class OpenResult:
    qty: float                 # 两腿等量成交张数（=最终登记持仓量）
    net_credit_btc: float      # 每张净收权利金（BTC，加权）
    buy_avg: float             # 腿单独执行部分的买腿均价（combo 部分不可分拆）
    sell_avg: float
    combo_qty: float
    unwound_qty: float         # 被回滚的买腿张数
    events: list[str] = field(default_factory=list)

    @property
    def opened(self) -> bool:
        return self.qty >= 0.1 - EPS


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
                          combo_qty=0.0, unwound_qty=0.0, events=events)

    # ---------------- 阶段B：剩余量买腿先行逐组执行 ----------------
    remaining = round_lot(qty - combo_qty, min_lot)
    buy_filled = 0.0
    buy_cost = 0.0
    sell_filled = 0.0
    sell_revenue = 0.0

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
            amount=excess, min_lot=min_lot, attempt_waits=(2.0, 4.0, 6.0), rounds=3),
            on_event=emit)
        urep = await un_ex.execute()
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
                      events=events)


@dataclass
class CloseResult:
    closed_qty: float
    buy_back_avg: float        # 买回卖腿均价（BTC/张）
    sell_out_avg: float        # 卖出买腿均价（双腿全平时）
    complete: bool
    events: list[str] = field(default_factory=list)


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
        use_combo=True,
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
    return CloseResult(
        closed_qty=round_lot(closed, min_lot),
        buy_back_avg=rep.leg_avg_price.get(sell_instrument, 0.0),
        sell_out_avg=rep.leg_avg_price.get(buy_instrument, 0.0),
        complete=complete,
        events=events,
    )


async def close_sell_leg_only(
    ws: DeribitWS,
    *,
    sell_instrument: str,
    qty: float,
    min_lot: float = 0.1,
    on_event: OnEvent = None,
) -> CloseResult:
    """只买回卖腿（保留买方保护腿），单腿三级阶梯。"""
    events: list[str] = []

    def emit(msg: str) -> None:
        events.append(msg)
        if on_event:
            on_event(msg)

    ex = SingleLegExecutor(ws, sell_instrument, "buy", SingleLegParams(
        amount=qty, min_lot=min_lot, rounds=3), on_event=emit)
    rep = await ex.execute()
    return CloseResult(closed_qty=round_lot(rep.filled, min_lot),
                       buy_back_avg=rep.avg_price, sell_out_avg=0.0,
                       complete=rep.complete, events=events)


async def close_long_leg(
    ws: DeribitWS,
    *,
    instrument: str,
    qty: float,
    min_lot: float = 0.1,
    on_event: OnEvent = None,
) -> CloseResult:
    """卖出平掉买方长腿（强平清跛腿等场景）。"""
    events: list[str] = []

    def emit(msg: str) -> None:
        events.append(msg)
        if on_event:
            on_event(msg)

    ex = SingleLegExecutor(ws, instrument, "sell", SingleLegParams(
        amount=qty, min_lot=min_lot, rounds=3), on_event=emit)
    rep = await ex.execute()
    return CloseResult(closed_qty=round_lot(rep.filled, min_lot),
                       buy_back_avg=0.0, sell_out_avg=rep.avg_price,
                       complete=rep.complete, events=events)
