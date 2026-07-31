"""两腿期权价差的 maker(挂单)执行引擎 — Deribit.

执行分三个阶段,目标:尽量以 maker 成交(手续费低 + 不付买卖价差),
同时保证两腿尽量同时建仓(先用原子成交的组合簿,再用双腿对冲式挂单)。

阶段 1  Combo 组合簿挂单(原子成交,零腿差风险,手续费有组合折扣):
        用 private/create_combo 建组合工具,在组合簿上 post_only 挂单。
        挂单价从"合成被动价"(syn_bid)随时间线性推进到激进上限
        (mid 与 far-touch 之间,由 aggressiveness 控制)。

阶段 2  双腿并行 post_only 挂单(经典 legger 算法):
        两腿同时在各自盘口最优价挂 post_only 单,随盘口移动改价(pegging),
        并随时间向对侧推进。一旦出现单腿先成(imbalance),启动对冲计时,
        超过 hedge_timeout 后立刻用 IOC 吃单补齐落后腿(touch ± pay_extra),
        把裸露敞口时间压到最短。

阶段 3  taker 兜底(可选):总超时后用 IOC 吃单完成剩余数量。
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from ..exchange.deribit_ws import DeribitWS, DeribitError
from .exec_report import AttemptRecord, GroupRecord, render_report_md

log = logging.getLogger("deribit.exec")

EPS = 1e-9
CHINA_TZ = ZoneInfo("Asia/Shanghai")


def china_now(fmt: str) -> str:
    return datetime.now(CHINA_TZ).strftime(fmt)


# ------------------------------------------------------------------ dataclasses


@dataclass
class TickRule:
    """价格网格规则:基础 tick + 阶梯(如 BTC 期权 >0.005 时 tick=0.0005)。"""
    base: float = 0.0001
    steps: list = field(default_factory=list)   # [(above_price, tick)],按 above_price 升序

    def tick_at(self, price: float) -> float:
        t = self.base
        for above, tick in self.steps:
            if abs(price) >= above:
                t = tick
        return t

    def round(self, price: float, up: bool) -> float:
        """取整到该价位适用的网格;若取整后跨过阶梯边界,用新档位再取整一次。"""
        t = self.tick_at(price)
        p = round_tick(price, t, up)
        t2 = self.tick_at(p)
        if t2 != t:
            p = round_tick(p, t2, up)
        return p

    @classmethod
    def from_instrument(cls, info: dict) -> "TickRule":
        steps = sorted(
            (s["above_price"], s["tick_size"]) for s in info.get("tick_size_steps") or []
        )
        return cls(base=info["tick_size"], steps=steps)


@dataclass
class Leg:
    instrument: str
    direction: str  # "buy" | "sell"


@dataclass
class ExecParams:
    amount: float                     # 每腿数量(BTC 期权:单位为 BTC,最小 0.1)
    use_combo: bool = True
    combo_timeout: float = 45.0       # 阶段 1 时长(秒)
    passive_seconds: float = 30.0     # 挂单价从被动推进到激进上限的时间
    requote_interval: float = 1.0     # 改价节流(秒)
    requote_min_ticks: int = 1        # 目标价偏离 ≥ N tick 才改价
    aggressiveness: float = 0.6       # 激进上限 = mid + a*(far_touch - mid), 0~1
    max_net_debit: Optional[float] = None  # 组合净权利金上限(BTC/张),None 不限
    leg_phase_timeout: float = 120.0  # 阶段 2 时长
    hedge_timeout: float = 2.0        # 单腿先成后,落后腿允许继续被动的秒数
    hedge_pay_extra_ticks: int = 2    # 对冲吃单时越过对手价的额外 tick 数
    taker_fallback: bool = True       # 遗留池是否最终 IOC 兜底扫单
    # --- 分组分批 maker(新策略)---
    n_groups: int = 5                 # 把每腿总量拆成几组,串行执行
    strict_five_groups: bool = True   # 策略建仓严格按设计固定5组;平仓可显式关闭
    attempt_wait: float = 5.0         # 每次 maker 尝试挂单后等待成交的秒数
    regroup_wait_fast: float = 3.0    # 上组第 1 次即成交 → 组间等待
    regroup_wait_mid: float = 8.0     # 上组第 2/3 次成交 → 组间等待
    regroup_wait_slow: float = 15.0   # 上组 3 次仍未达标 → 组间等待
    min_lot: float = 0.1
    dry_run: bool = False             # 只打印计划,不下单
    write_report: bool = True         # 执行结束后生成 Markdown 核验报告
    report_dir: Optional[str] = None  # 报告目录,None=默认 <maker_exec>/state/exec_reports
    operation_kind: str = "open"     # open | top_up | close，供仪表盘说明当前任务
    combo_only: bool = False          # V2:只跑阶段1组合簿(原子成交),不进入双腿并行阶段;
                                      # 未成交部分由调用方按"买腿先行"顺序自行执行


@dataclass
class LegState:
    leg: Leg
    rule: TickRule = field(default_factory=TickRule)
    filled: float = 0.0
    avg_price: float = 0.0
    order_id: Optional[str] = None
    order_price: Optional[float] = None
    order_qty: float = 0.0            # 当前挂单的委托数量(用于计算撤单量)
    order_counted: float = 0.0
    order_counted_cost: float = 0.0
    hedge_deadline: Optional[float] = None


@dataclass
class ExecReport:
    target: float = 0.0
    combo_target: float = 0.0
    combo_filled: float = 0.0
    combo_avg_net: float = 0.0
    combo_seconds: float = 0.0
    leg_filled: dict[str, float] = field(default_factory=dict)
    leg_avg_price: dict[str, float] = field(default_factory=dict)
    groups: list = field(default_factory=list)          # list[GroupRecord],分组明细
    leftover: dict = field(default_factory=dict)         # 腿名 -> {short, swept}
    rebalance: list[dict] = field(default_factory=list)
    completion_error: Optional[str] = None
    events: list[str] = field(default_factory=list)
    log_path: Optional[str] = None
    event_callback: Optional[Callable[[str], None]] = field(default=None, repr=False)

    def add(self, msg: str) -> None:
        stamp = china_now("%H:%M:%S")
        line = f"[{stamp}] {msg}"
        self.events.append(line)
        log.info(msg)
        if self.event_callback is not None:
            try:
                self.event_callback(msg)
            except Exception:  # noqa: BLE001 - 日志回调不得影响交易执行
                log.exception("execution event callback failed")
        # 实时逐行落盘:执行中途崩溃也能留下完整日志
        fh = getattr(self, "_fh", None)
        if fh is not None:
            try:
                fh.write(line + "\n")
                fh.flush()
            except OSError:
                pass

    def attach_file(self, path: str, header: str) -> None:
        """开始实时落盘:打开报告文件并写入表头。失败则静默降级(不影响交易)。"""
        self.log_path = path
        try:
            self._fh = open(path, "w", encoding="utf-8")
            self._fh.write(header)
            self._fh.flush()
        except OSError as exc:
            log.warning("执行报告无法写入 %s: %s", path, exc)
            self._fh = None

    def finalize(self, md_text: str) -> None:
        """执行结束:关闭实时流,用结构化 Markdown 重写整份报告。"""
        fh = getattr(self, "_fh", None)
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass
            self._fh = None
        if self.log_path:
            try:
                with open(self.log_path, "w", encoding="utf-8") as f:
                    f.write(md_text)
            except OSError as exc:
                log.warning("执行报告重写失败 %s: %s", self.log_path, exc)


class ExecutionIncompleteError(RuntimeError):
    """最终兜底后仍未达到两腿目标;携带报告交由策略层登记部分成交。"""

    def __init__(self, report: ExecReport, message: str):
        super().__init__(message)
        self.report = report


class OrderCancellationUnconfirmed(RuntimeError):
    """A live GTC order could not be proven terminal.

    Losing local ownership of such an order is unsafe: it may fill after the
    strategy has already moved on and create an untracked or inverted leg.
    """

    def __init__(self, order_id: str, instrument: str, detail: str = ""):
        message = f"order cancellation unconfirmed: {order_id} {instrument}"
        if detail:
            message += f" ({detail})"
        super().__init__(message)
        self.order_id = order_id
        self.instrument = instrument


# ------------------------------------------------------------------ helpers


def round_tick(price: float, tick: float, up: bool) -> float:
    q = price / tick
    q = math.ceil(q - EPS) if up else math.floor(q + EPS)
    return round(q * tick, 10)


def round_lot(amount: float, lot: float) -> float:
    return round(math.floor(amount / lot + EPS) * lot, 10)


def escalate(passive: float, cap: float, elapsed: float, horizon: float) -> float:
    frac = min(1.0, max(0.0, elapsed / max(horizon, EPS)))
    return passive + frac * (cap - passive)


# ------------------------------------------------------------------ executor


class SpreadExecutor:
    """执行 买 buy_leg / 卖 sell_leg 各 params.amount 张。"""

    def __init__(self, ws: DeribitWS, buy_leg: str, sell_leg: str, params: ExecParams,
                 on_event: Optional[Callable[[str], None]] = None):
        self.ws = ws
        self.params = params
        self.legs = {
            buy_leg: LegState(Leg(buy_leg, "buy")),
            sell_leg: LegState(Leg(sell_leg, "sell")),
        }
        self.buy_leg = buy_leg
        self.sell_leg = sell_leg
        self.tickers: dict[str, dict] = {}
        self.report = ExecReport(target=params.amount, event_callback=on_event)
        self.combo_name: Optional[str] = None
        self.combo_side: str = "buy"     # 我们对组合工具应下的方向
        self.combo_rule: TickRule = TickRule()
        # --- 仪表盘用的实时状态 ---
        self.phase = "idle"              # idle | combo | legging | taker | done
        self.started_at = time.monotonic()
        self.combo_order_price: Optional[float] = None
        self.combo_target_price: Optional[float] = None
        self.combo_filled_live = 0.0
        # --- 分组分批执行的实时进度 ---
        self.cur_group = 0               # 当前第几组(1..n_groups)
        self.cur_attempt = 0             # 当前第几次尝试(1..3),0 表示非分组阶段
        self.n_groups = params.n_groups

    # ------------------------------------------------------------ market data

    async def _on_ticker(self, channel: str, data: dict) -> None:
        self.tickers[data["instrument_name"]] = data

    def _top(self, instrument: str) -> tuple[Optional[float], Optional[float]]:
        t = self.tickers.get(instrument)
        if not t:
            return None, None
        return t.get("best_bid_price") or None, t.get("best_ask_price") or None

    def _syn_quotes(self) -> Optional[tuple[float, float]]:
        """组合(+买腿 -卖腿)的合成 bid/ask,基于两腿盘口。"""
        bid_a, ask_a = self._top(self.buy_leg)
        bid_b, ask_b = self._top(self.sell_leg)
        if None in (bid_a, ask_a, bid_b, ask_b):
            return None
        return bid_a - ask_b, ask_a - bid_b  # (syn_bid, syn_ask)

    async def _subscribe_tickers(self, instruments: list[str]) -> None:
        channels = [f"ticker.{i}.100ms" for i in instruments]
        await self.ws.subscribe(channels, self._on_ticker)
        # 等待首帧行情
        for _ in range(50):
            if all(i in self.tickers for i in instruments):
                return
            await asyncio.sleep(0.1)
        # Streaming can occasionally acknowledge a subscription without
        # delivering every first frame.  Seed missing legs from public
        # snapshots so a transient websocket gap does not abort execution.
        for instrument in instruments:
            if instrument in self.tickers:
                continue
            try:
                snapshot = await self.ws.request(
                    "public/ticker", {"instrument_name": instrument})
            except Exception:  # noqa: BLE001 - report all unresolved legs below
                continue
            if snapshot:
                self.tickers[instrument] = snapshot
                self.report.add(
                    f"{instrument} 行情订阅首帧超时，已使用 public/ticker 快照兜底")
        missing = [i for i in instruments if i not in self.tickers]
        if missing:
            raise RuntimeError(f"no ticker data for {missing}")

    async def _load_ticks(self) -> None:
        for name, state in self.legs.items():
            info = await self.ws.request("public/get_instrument", {"instrument_name": name})
            state.rule = TickRule.from_instrument(info)

    # ------------------------------------------------------------ orders

    async def _place(self, instrument: str, direction: str, amount: float, price: float,
                     post_only: bool = True, ioc: bool = False, label: str = "") -> Optional[dict]:
        params: dict[str, Any] = {
            "instrument_name": instrument,
            "amount": amount,
            "type": "limit",
            "price": price,
            "label": label[:64],
        }
        if post_only:
            params["post_only"] = True  # 若会立即成交,交易所自动改价到盘口内侧
        if ioc:
            params["time_in_force"] = "immediate_or_cancel"
        if self.params.operation_kind == "close":
            # A delayed close order must never survive long enough to invert
            # the position.  Deribit will cap/cancel reduce-only quantity once
            # the current leg has reached zero.
            params["reduce_only"] = True
        method = "private/buy" if direction == "buy" else "private/sell"
        if self.params.dry_run:
            self.report.add(f"DRY-RUN {method} {instrument} {amount}@{price} "
                            f"{'post_only' if post_only else ''}{'IOC' if ioc else ''}")
            return None
        result = await self.ws.request(method, params)
        return result

    async def _cancel(self, order_id: str) -> dict:
        return await self.ws.request("private/cancel", {"order_id": order_id})

    async def _order_state(self, order_id: str) -> dict:
        return await self.ws.request("private/get_order_state", {"order_id": order_id})

    async def _cancel_order_and_confirm(
        self,
        order_id: str,
        instrument: str,
        on_state: Optional[Callable[[dict], None]] = None,
        attempts: int = 3,
    ) -> dict:
        """Cancel a GTC order and require a terminal exchange state.

        A successful cancel request is not sufficient, and a failed request
        must not be treated as a cancellation.  Re-read the order after every
        attempt and retain ownership by raising if its terminal state cannot
        be proven.
        """
        terminal = {"filled", "cancelled", "rejected"}
        last_error = ""
        last_order: dict = {}
        for attempt in range(1, max(1, attempts) + 1):
            try:
                last_order = await self._order_state(order_id)
                if on_state:
                    on_state(last_order)
                if last_order.get("order_state") in terminal:
                    return last_order
            except DeribitError as exc:
                last_error = str(exc)

            try:
                await self._cancel(order_id)
            except DeribitError as exc:
                last_error = str(exc)

            try:
                last_order = await self._order_state(order_id)
                if on_state:
                    on_state(last_order)
                if last_order.get("order_state") in terminal:
                    return last_order
            except DeribitError as exc:
                last_error = str(exc)

            if attempt < attempts:
                await asyncio.sleep(0.25 * attempt)

        self.report.add(
            f"ERROR 撤单未确认: {instrument} order_id={order_id}; "
            "保留执行中状态，禁止继续下单"
        )
        raise OrderCancellationUnconfirmed(order_id, instrument, last_error)

    async def _edit(self, order_id: str, amount: float, price: float) -> Optional[dict]:
        try:
            return await self.ws.request(
                "private/edit",
                {"order_id": order_id, "amount": amount, "price": price, "post_only": True},
            )
        except DeribitError as exc:
            log.warning("edit failed (%s), will re-place", exc.message)
            return None

    # ------------------------------------------------------------ dashboard

    def snapshot(self) -> dict:
        """返回当前执行状态的 JSON 可序列化快照,供仪表盘轮询。"""
        now = time.monotonic()
        legs = []
        for name, st in self.legs.items():
            bid, ask = self._top(name)
            hedge_remaining = None
            if st.hedge_deadline is not None:
                hedge_remaining = max(0.0, st.hedge_deadline - now)
            legs.append({
                "instrument": name,
                "direction": st.leg.direction,
                "tick": st.rule.base,
                "bid": bid,
                "ask": ask,
                "order_price": st.order_price if st.order_id else None,
                "has_order": st.order_id is not None,
                "filled": st.filled,
                "target": self.params.amount,
                "avg_price": st.avg_price,
                "hedge_remaining": hedge_remaining,
            })
        syn = self._syn_quotes()
        combo = None
        if self.combo_name:
            cbid, cask = self._top(self.combo_name)
            combo = {
                "name": self.combo_name,
                "side": self.combo_side,
                "bid": cbid,
                "ask": cask,
                "target_price": self.combo_target_price,
                "order_price": self.combo_order_price,
                "filled": self.combo_filled_live,
            }
        return {
            "phase": self.phase,
            "elapsed": now - self.started_at,
            "target": self.params.amount,
            "syn_bid": syn[0] if syn else None,
            "syn_ask": syn[1] if syn else None,
            "legs": legs,
            "combo": combo,
            "group": self.cur_group,
            "attempt": self.cur_attempt,
            "n_groups": self.n_groups,
            "events": self.report.events[-500:],
            "dry_run": self.params.dry_run,
            "operation_kind": self.params.operation_kind,
        }

    @staticmethod
    def _order_fields(order: dict) -> tuple[float, float, str]:
        return (
            order.get("filled_amount", 0.0) or 0.0,
            order.get("average_price", 0.0) or 0.0,
            order.get("order_state", "?"),
        )

    @staticmethod
    def _add_fill(state: LegState, qty: float, avg_price: float) -> None:
        if qty <= EPS:
            return
        total_cost = state.avg_price * state.filled + avg_price * qty
        state.filled += qty
        state.avg_price = total_cost / max(state.filled, EPS)

    def _sync_leg_order(self, state: LegState, order: dict) -> str:
        """Accumulate only newly reported fills from the currently tracked order."""
        order_filled, order_avg, order_state = self._order_fields(order)
        order_cost = order_filled * order_avg
        delta_qty = max(0.0, order_filled - state.order_counted)
        delta_cost = max(0.0, order_cost - state.order_counted_cost)
        if delta_qty > EPS:
            incr_avg = delta_cost / max(delta_qty, EPS)
            self._add_fill(state, delta_qty, incr_avg)
            state.order_counted = order_filled
            state.order_counted_cost = order_cost
        return order_state

    # ------------------------------------------------------------ phase 1: combo

    async def _setup_combo(self) -> bool:
        p = self.params
        # legs 用 1:1 比例定义组合工具;实际张数在组合簿下单时用 amount 指定
        trades = [
            {"instrument_name": self.buy_leg, "amount": 1, "direction": "buy"},
            {"instrument_name": self.sell_leg, "amount": 1, "direction": "sell"},
        ]
        if p.dry_run:
            self.report.add("DRY-RUN private/create_combo " + str(trades))
            return False
        try:
            combo = await self.ws.request("private/create_combo", {"trades": trades})
        except DeribitError as exc:
            self.report.add(f"combo 创建失败({exc.message}),跳过阶段1")
            return False
        self.combo_name = combo["id"]
        # 交易所可能把组合规范化成相反方向(我们的买腿在 legs 里是负数),
        # 那时要在组合簿上下 sell 单才等价于我们想要的头寸。
        legs = {l["instrument_name"]: l["amount"] for l in combo.get("legs", [])}
        if legs.get(self.buy_leg, 1) < 0:
            self.combo_side = "sell"
        try:
            info = await self.ws.request("public/get_instrument", {"instrument_name": self.combo_name})
            self.combo_rule = TickRule.from_instrument(info)
        except DeribitError:
            self.combo_rule = self.legs[self.buy_leg].rule
        await self._subscribe_tickers([self.combo_name])
        self.report.add(f"combo 已创建: {self.combo_name},下单方向 {self.combo_side}")
        return True

    def _combo_targets(self, elapsed: float) -> Optional[tuple[float, float]]:
        """返回 (target_price, cap)。组合净价按 +买腿-卖腿 定义;combo_side==sell 时取反。"""
        syn = self._syn_quotes()
        if syn is None:
            return None
        syn_bid, syn_ask = syn
        mid = (syn_bid + syn_ask) / 2
        p = self.params
        if self.combo_side == "buy":
            cap = mid + p.aggressiveness * (syn_ask - mid)
            if p.max_net_debit is not None:
                cap = min(cap, p.max_net_debit)
            passive = syn_bid
            cbid, _ = self._top(self.combo_name)
            if cbid is not None:
                # 排队优先:压过组合簿现有买一
                passive = max(passive, cbid + self.combo_rule.tick_at(cbid))
            passive = min(passive, cap)
            target = self.combo_rule.round(escalate(passive, cap, elapsed, p.passive_seconds), up=False)
            return target, cap
        # combo 规范化为反方向:我们卖出组合,净价 = -(买腿-卖腿),镜像处理
        cap = -(mid - p.aggressiveness * (mid - syn_bid))
        if p.max_net_debit is not None:
            cap = max(cap, -p.max_net_debit)
        passive = -syn_ask
        _, cask = self._top(self.combo_name)
        if cask is not None:
            passive = min(passive, cask - self.combo_rule.tick_at(cask))
        passive = max(passive, cap)
        target = self.combo_rule.round(escalate(passive, cap, elapsed, p.passive_seconds), up=True)
        return target, cap

    async def _run_combo_phase(self, combo_target: float) -> float:
        """仅用第1组数量在组合簿做 maker,返回成交张数。"""
        p = self.params
        start = time.monotonic()
        order_id: Optional[str] = None
        order_price: Optional[float] = None
        filled = 0.0
        avg = 0.0
        order_counted = 0.0
        order_counted_cost = 0.0
        last_quote = 0.0

        def sync_combo_order(order: dict) -> None:
            nonlocal filled, avg, order_counted, order_counted_cost
            order_filled, order_avg, _ = self._order_fields(order)
            order_cost = order_filled * order_avg
            delta_qty = max(0.0, order_filled - order_counted)
            delta_cost = max(0.0, order_cost - order_counted_cost)
            if delta_qty > EPS:
                total_cost = avg * filled + delta_cost
                filled += delta_qty
                avg = total_cost / max(filled, EPS)
                order_counted = order_filled
                order_counted_cost = order_cost

        while time.monotonic() - start < p.combo_timeout:
            elapsed = time.monotonic() - start
            tp = self._combo_targets(elapsed)
            if tp is None:
                await asyncio.sleep(0.2)
                continue
            target, _cap = tp
            self.combo_target_price = target

            if order_id is not None:
                state = await self._order_state(order_id)
                order_filled, order_avg, ostate = self._order_fields(state)
                order_cost = order_filled * order_avg
                delta_qty = max(0.0, order_filled - order_counted)
                delta_cost = max(0.0, order_cost - order_counted_cost)
                if delta_qty > EPS:
                    total_cost = avg * filled + delta_cost
                    filled += delta_qty
                    avg = total_cost / max(filled, EPS)
                    order_counted = order_filled
                    order_counted_cost = order_cost
                self.combo_filled_live = filled
                if ostate == "filled":
                    break
                if ostate in ("cancelled", "rejected"):
                    order_id = None
                    order_counted = 0.0
                    order_counted_cost = 0.0

            need_requote = (
                order_id is None
                or abs(target - (order_price or 0.0)) >= p.requote_min_ticks * self.combo_rule.base - EPS
            )
            if need_requote and time.monotonic() - last_quote >= p.requote_interval:
                remaining = round_lot(combo_target - filled, p.min_lot)
                if remaining < p.min_lot - EPS:
                    break
                if order_id is not None:
                    edited = await self._edit(order_id, remaining, target)
                    if edited is None:
                        # 改价失败(可能刚好已成交):撤单后等下一轮重新读状态,避免重复下单
                        await self._cancel_order_and_confirm(
                            order_id, self.combo_name or "combo", sync_combo_order)
                        order_id = None
                        await asyncio.sleep(0.25)
                        continue
                    order_price = target
                    last_quote = time.monotonic()
                if order_id is None:
                    result = await self._place(
                        self.combo_name, self.combo_side, remaining, target,
                        post_only=True, label="spread-combo-maker",
                    )
                    if result:
                        order_id = result["order"]["order_id"]
                        order_price = result["order"].get("price", target)
                        order_counted = 0.0
                        order_counted_cost = 0.0
                        self.report.add(f"combo 挂单 {self.combo_side} {remaining}@{order_price}")
                    last_quote = time.monotonic()
                self.combo_order_price = order_price if order_id else None
            await asyncio.sleep(0.25)

        if order_id is not None:
            await self._cancel_order_and_confirm(
                order_id, self.combo_name or "combo", sync_combo_order)
        self.combo_filled_live = filled
        self.combo_order_price = None
        self.report.combo_avg_net = avg
        if filled > 0:
            self.report.add(f"阶段1 combo 成交 {filled} 张,均价 {avg}")
        else:
            self.report.add("阶段1 combo 未成交,转入双腿挂单")
        return filled

    # ------------------------------------------------------------ phase 2: legging

    @staticmethod
    def _price_kind(attempt: int) -> str:
        return "mid" if attempt <= 1 else ("2/3touch" if attempt == 2 else "taker")

    def _clamp_exchange_limits(self, state: LegState, price: float, is_buy: bool) -> float:
        t = self.tickers.get(state.leg.instrument) or {}
        if is_buy and t.get("max_price") is not None:
            price = min(price, t["max_price"])
        if (not is_buy) and t.get("min_price") is not None:
            price = max(price, t["min_price"])
        return price

    def _quote_price(self, state: LegState, attempt: int) -> Optional[float]:
        """按尝试序号在 mid → 2/3对手价 → 对手价 之间报价,并满足 tick 精度。

        尝试 1/2 为 maker(买向下取整、卖向上取整,夹在 [bid, ask] 内保持 post_only);
        尝试 3 为 taker(买向上取整、卖向下取整,穿越对手价成交)。
        """
        bid, ask = self._top(state.leg.instrument)
        if bid is None or ask is None:
            return None
        mid = (bid + ask) / 2.0
        is_buy = state.leg.direction == "buy"
        rule = state.rule
        if attempt <= 1:
            raw = mid
        elif attempt == 2:
            touch = ask if is_buy else bid
            raw = (2.0 / 3.0) * touch + (1.0 / 3.0) * mid
        else:
            raw = ask if is_buy else bid
        if attempt >= 3:
            price = rule.round(raw, up=is_buy)
            price = self._clamp_exchange_limits(state, price, is_buy)
            return round(max(price, rule.base), 10)
        price = rule.round(raw, up=(not is_buy))
        return min(max(price, bid), ask)

    async def _place_leg_maker(self, state: LegState, qty: float, attempt: int, g: int) -> None:
        """在指定腿上按尝试序号挂 post_only maker 单。返回实际挂单价(失败 None)。"""
        price = self._quote_price(state, attempt)
        if price is None:
            self.report.add(f"[G{g}/A{attempt}] {state.leg.instrument} 无有效盘口,跳过挂单")
            return None
        result = await self._place(state.leg.instrument, state.leg.direction, qty, price,
                                   post_only=True, label="spread-batch-maker")
        if result:
            state.order_id = result["order"]["order_id"]
            state.order_price = result["order"].get("price", price)
            state.order_qty = qty
            state.order_counted = 0.0
            state.order_counted_cost = 0.0
            self.report.add(f"[G{g}/A{attempt}] 挂 {state.leg.direction} {state.leg.instrument} "
                            f"{qty:.1f}@{state.order_price} (maker {self._price_kind(attempt)})")
            return state.order_price
        return None

    async def _reconcile_and_cancel(self, state: LegState, g: int, attempt: int) -> tuple[float, float]:
        """读取挂单最终成交;未全成则撤单,以撤单后 dealt_qty 为唯一真相累计。

        返回 (本单成交量, 撤单量)。
        """
        if state.order_id is None:
            return 0.0, 0.0
        order_id = state.order_id
        await self._cancel_order_and_confirm(
            order_id, state.leg.instrument,
            lambda order: self._sync_leg_order(state, order))
        this_filled = state.order_counted
        cancelled = max(0.0, round_lot(state.order_qty - this_filled, self.params.min_lot))
        if cancelled >= self.params.min_lot - EPS:
            self.report.add(f"[G{g}/A{attempt}] 撤 {state.leg.instrument}: "
                            f"挂 {state.order_qty:.1f} · 成 {this_filled:.1f} · 撤 {cancelled:.1f}")
        state.order_id = None
        state.order_price = None
        state.order_qty = 0.0
        state.order_counted = 0.0
        state.order_counted_cost = 0.0
        return this_filled, cancelled

    async def _taker_at_touch(self, state: LegState, qty: float, g: int,
                              attempt: int) -> tuple[Optional[float], float]:
        """尝试 3:直接报对手价的 IOC 吃单。返回 (报价, 本次成交量)。"""
        price = self._quote_price(state, 3)
        if price is None:
            return None, 0.0
        self.report.add(f"[G{g}/A{attempt}] IOC {state.leg.direction} {state.leg.instrument} "
                        f"{qty:.1f}@{price} (taker 对手价)")
        result = await self._place(state.leg.instrument, state.leg.direction, qty, price,
                                   post_only=False, ioc=True, label="spread-batch-taker")
        newly = 0.0
        if result:
            filled, avg, _ = self._order_fields(result["order"])
            if filled > 0:
                self._add_fill(state, filled, avg)
                newly = filled
        return price, newly

    async def _sweep_ioc(self, state: LegState, qty: float) -> float:
        """遗留池最终扫单:越过对手价 pay_extra 个 tick 的 IOC 兜底。返回成交量。"""
        price = self._ioc_price(state, state.leg.direction)
        if price is None:
            return 0.0
        result = await self._place(state.leg.instrument, state.leg.direction, qty, price,
                                   post_only=False, ioc=True, label="spread-leg-hedge")
        newly = 0.0
        if result:
            filled, avg, _ = self._order_fields(result["order"])
            if filled > 0:
                self._add_fill(state, filled, avg)
                newly = filled
        return newly

    def _ioc_price(self, state: LegState, direction: str) -> Optional[float]:
        """IOC 吃单价:越过对手价 pay_extra 个 tick,并夹在交易所 min/max 限价内。"""
        bid, ask = self._top(state.leg.instrument)
        p = self.params
        if direction == "buy":
            if ask is None:
                return None
            price = ask + p.hedge_pay_extra_ticks * state.rule.tick_at(ask)
        else:
            if bid is None:
                return None
            price = bid - p.hedge_pay_extra_ticks * state.rule.tick_at(bid)
        price = state.rule.round(price, up=(direction == "buy"))
        ticker = self.tickers.get(state.leg.instrument) or {}
        max_price = ticker.get("max_price")
        min_price = ticker.get("min_price")
        if direction == "buy" and max_price is not None:
            price = min(price, max_price)
        elif direction == "sell" and min_price is not None:
            price = max(price, min_price)
        return state.rule.round(max(price, state.rule.base), up=(direction == "buy"))

    def _group_targets(self, amount: float) -> list[float]:
        """严格拆5组,返回每组结束时的累计目标;最后一组吸收最小单位余数。"""
        p = self.params
        if not p.strict_five_groups:
            lots = int(math.floor(amount / p.min_lot + EPS))
            n = max(1, min(p.n_groups, lots))
            targets = [round_lot(amount * g / n, p.min_lot) for g in range(1, n)]
            targets.append(round_lot(amount, p.min_lot))
            return targets
        if p.n_groups != 5:
            raise ValueError(f"设计要求固定5组,n_groups必须为5(当前 {p.n_groups})")
        if amount + EPS < p.n_groups * p.min_lot:
            raise ValueError(
                f"总量 {amount:.1f} 不足拆成5组(每组至少 {p.min_lot:.1f}),拒绝下单"
            )
        targets = [round_lot(amount * g / p.n_groups, p.min_lot)
                   for g in range(1, p.n_groups)]
        targets.append(round_lot(amount, p.min_lot))
        if any(targets[i] - (targets[i - 1] if i else 0.0) < p.min_lot - EPS
               for i in range(len(targets))):
            raise ValueError(f"总量 {amount:.1f} 无法按最小单位 {p.min_lot:.1f} 拆成5个有效组")
        return targets

    def _leg_diff(self) -> float:
        """腿差 = 买腿累计 − 卖腿累计(正=买腿多)。"""
        return round_lot(self.legs[self.buy_leg].filled - self.legs[self.sell_leg].filled,
                         self.params.min_lot)

    def _log_attempt(self, g: int, attempt: int, cum: float) -> None:
        """每次尝试结束后,实时打印两腿成交量与剩余量。"""
        p = self.params
        lines = []
        for tag, st in (("买腿", self.legs[self.buy_leg]), ("卖腿", self.legs[self.sell_leg])):
            rem = max(0.0, round_lot(cum - st.filled, p.min_lot))
            lines.append(f"  {tag} {st.leg.instrument}: 累计 {st.filled:.1f} 张 @ {st.avg_price:.4f}"
                         f" | 组内目标 {cum:.1f} | 剩 {rem:.1f}")
        diff = self._leg_diff()
        flag = "" if abs(diff) < p.min_lot - EPS else "  ⚠腿差非0"
        self.report.add(f"[G{g}/A{attempt}] 成交结果\n" + "\n".join(lines)
                        + f"\n  腿差: {diff:+.1f} 张{flag}")

    def _log_group_summary(self, g: int, cum: float) -> None:
        b, s = self.legs[self.buy_leg], self.legs[self.sell_leg]
        self.report.add(
            f"[G{g}] 本组完成: 买腿 {b.filled:.1f}@{b.avg_price:.4f} · "
            f"卖腿 {s.filled:.1f}@{s.avg_price:.4f} · 腿差 {self._leg_diff():+.1f}"
        )

    def _log_final(self, target: float) -> None:
        b, s = self.legs[self.buy_leg], self.legs[self.sell_leg]
        complete = (abs(b.filled - target) < self.params.min_lot - EPS
                    and abs(s.filled - target) < self.params.min_lot - EPS
                    and abs(self._leg_diff()) < self.params.min_lot - EPS)
        label = "执行成功" if complete else "执行未完成"
        self.report.add(
            f"[{label}] 买腿 {b.filled:.1f}/{target:.1f}@{b.avg_price:.4f} · "
            f"卖腿 {s.filled:.1f}/{target:.1f}@{s.avg_price:.4f} · "
            f"最终腿差 {self._leg_diff():+.1f} 张"
        )

    async def _rebalance_legs(self, attempts: int = 4) -> bool:
        """终局两腿再平衡:若不等量,反向 IOC 砍掉多出的部分,不留裸腿。

        2026-07-10 实盘复现:兜底各自朝目标吃单,买腿吃到、卖腿吃不到,
        留下 +20/-10 的 10 张账外裸多。此函数保证执行结束时两腿等量。
        """
        p = self.params
        a, b = list(self.legs.values())
        for i in range(attempts):
            imbalance = round_lot(a.filled - b.filled, p.min_lot)
            if abs(imbalance) < p.min_lot - EPS:
                return True
            if p.operation_kind == "close":
                # Reversing the leading close leg would reopen exposure.  Let
                # the caller re-read exchange positions and close only the
                # lagging residue with direction-aware reduce-only orders.
                self.report.add(
                    f"平仓腿差 {imbalance:+.1f} 张：不反向重开领先腿，"
                    "交由交易所持仓残余恢复")
                return False
            over = a if imbalance > 0 else b
            excess = round_lot(abs(imbalance), p.min_lot)
            reverse = "sell" if over.leg.direction == "buy" else "buy"
            price = self._ioc_price(over, reverse)
            if price is not None:
                self.report.add(f"腿差再平衡: {reverse} {over.leg.instrument} {excess}@{price} IOC(第{i+1}次)")
                result = await self._place(over.leg.instrument, reverse, excess, price,
                                           post_only=False, ioc=True, label="spread-leg-rebalance")
                filled = 0.0
                if result:
                    filled, _, _ = self._order_fields(result["order"])
                    if filled > 0:
                        over.filled = round_lot(over.filled - filled, p.min_lot)
                remaining_diff = round_lot(a.filled - b.filled, p.min_lot)
                self.report.rebalance.append({
                    "attempt": i + 1,
                    "instrument": over.leg.instrument,
                    "direction": reverse,
                    "qty": excess,
                    "filled": filled,
                    "remaining_diff": remaining_diff,
                })
            await asyncio.sleep(1.5)
        imbalance = round_lot(a.filled - b.filled, p.min_lot)
        if abs(imbalance) >= p.min_lot - EPS:
            over = a if imbalance > 0 else b
            self.report.add(f"⚠ 再平衡未完成: {over.leg.instrument} 仍多 {abs(imbalance):.1f} 张裸腿,请手动处理")
            log.error("腿差再平衡失败: %s 多 %.1f 张", over.leg.instrument, abs(imbalance))
            return False
        return True

    async def _run_group(self, g: int, cum: float, previous_cum: float) -> int:
        """执行第 g 组:最多 3 次尝试,把两腿各自补到累计目标 cum。

        - 尝试 1/2:两腿并行挂 post_only maker,共享一个 attempt_wait 等待窗口,
          未全成则撤单,以「剩余量」进入下一次尝试(而非重新从组目标开始)。
        - 尝试 3:对未补齐的量直接 IOC 吃对手价。
        返回两腿都达标时的尝试序号(1/2/3);3 次仍未达标返回 0。
        """
        p = self.params
        buy, sell = self.legs[self.buy_leg], self.legs[self.sell_leg]
        grec = GroupRecord(index=g, cum_target=cum,
                           group_target=round_lot(cum - previous_cum, p.min_lot))
        self.report.groups.append(grec)
        reached_at = 0
        for attempt in (1, 2, 3):
            self.cur_attempt = attempt
            buy_todo = max(0.0, round_lot(cum - buy.filled, p.min_lot))
            sell_todo = max(0.0, round_lot(cum - sell.filled, p.min_lot))
            if buy_todo < p.min_lot - EPS and sell_todo < p.min_lot - EPS:
                reached_at = attempt
                break
            rec = AttemptRecord(attempt=attempt)
            kind = self._price_kind(attempt)
            if attempt < 3:
                if buy_todo >= p.min_lot - EPS:
                    rec.buy_kind, rec.buy_qty = kind, buy_todo
                    rec.buy_price = await self._place_leg_maker(buy, buy_todo, attempt, g)
                if sell_todo >= p.min_lot - EPS:
                    rec.sell_kind, rec.sell_qty = kind, sell_todo
                    rec.sell_price = await self._place_leg_maker(sell, sell_todo, attempt, g)
                await asyncio.sleep(p.attempt_wait)
                rec.buy_filled, rec.buy_cancelled = await self._reconcile_and_cancel(buy, g, attempt)
                rec.sell_filled, rec.sell_cancelled = await self._reconcile_and_cancel(sell, g, attempt)
            else:
                if buy_todo >= p.min_lot - EPS:
                    rec.buy_kind, rec.buy_qty = "taker", buy_todo
                    rec.buy_price, rec.buy_filled = await self._taker_at_touch(buy, buy_todo, g, attempt)
                if sell_todo >= p.min_lot - EPS:
                    rec.sell_kind, rec.sell_qty = "taker", sell_todo
                    rec.sell_price, rec.sell_filled = await self._taker_at_touch(sell, sell_todo, g, attempt)
            grec.attempts.append(rec)
            self._log_attempt(g, attempt, cum)
            if (round_lot(cum - buy.filled, p.min_lot) < p.min_lot - EPS
                    and round_lot(cum - sell.filled, p.min_lot) < p.min_lot - EPS):
                reached_at = attempt
                break
        grec.reached_at = reached_at
        grec.buy_after = buy.filled
        grec.sell_after = sell.filled
        grec.buy_carry = max(0.0, round_lot(cum - buy.filled, p.min_lot))
        grec.sell_carry = max(0.0, round_lot(cum - sell.filled, p.min_lot))
        grec.imbalance = self._leg_diff()
        self._log_group_summary(g, cum)
        if grec.buy_carry >= p.min_lot - EPS or grec.sell_carry >= p.min_lot - EPS:
            self.report.add(
                f"[G{g}] 遗留池累计:买腿 {grec.buy_carry:.1f} 张 · "
                f"卖腿 {grec.sell_carry:.1f} 张,自动带入下一组"
            )
        if abs(grec.imbalance) >= p.min_lot - EPS:
            self.report.add(f"[G{g}] 腿差待处理 {grec.imbalance:+.1f} 张,后续组优先补落后腿")
        return reached_at

    async def _run_batched_phase(self, target_amount: float) -> None:
        """分组分批建仓主流程:n 组串行,每组累计目标 = 组序号 × 组量。

        combo 已成交的部分作为两腿的前置成交,自动抵消前若干组的目标;
        每组用自适应间隔衔接;所有组结束后对剩余量做 IOC 兜底并再平衡。
        """
        p = self.params
        buy, sell = self.legs[self.buy_leg], self.legs[self.sell_leg]
        targets = self._group_targets(target_amount)
        n = len(targets)
        self.n_groups = n
        group_size = targets[0]
        pre = f"(combo 已成 {buy.filled:.1f} 张,自动计入前置组)" if buy.filled > EPS else ""
        self.report.add(f"分组建仓: 目标 {target_amount:.1f} 张/腿 → 固定 {n} 组,"
                        f"第1组 {group_size:.1f} 张,最小单位余数由后续组吸收 {pre}")
        try:
            previous_cum = 0.0
            for g, cum in enumerate(targets, start=1):
                self.cur_group = g
                # combo 预成交已覆盖本组累计目标 → 无单可下,直接跳过(不占用组间等待)
                if all(round_lot(cum - st.filled, p.min_lot) < p.min_lot - EPS for st in (buy, sell)):
                    grec = GroupRecord(
                        index=g,
                        cum_target=cum,
                        group_target=round_lot(cum - previous_cum, p.min_lot),
                        combo_covered=(g == 1),
                        reached_at=1,
                        buy_after=buy.filled,
                        sell_after=sell.filled,
                        imbalance=self._leg_diff(),
                    )
                    self.report.groups.append(grec)
                    self.report.add(f"[G{g}] Combo成交已覆盖本组累计目标 {cum:.1f} 张/腿")
                    if g < n:
                        self.report.add(f"[G{g}] → 等待 {p.regroup_wait_fast:.0f}s 后开下一组")
                        await asyncio.sleep(p.regroup_wait_fast)
                    previous_cum = cum
                    continue
                reached_at = await self._run_group(g, cum, previous_cum)
                if g < n:
                    if reached_at == 1:
                        wait = p.regroup_wait_fast
                    elif reached_at in (2, 3):
                        wait = p.regroup_wait_mid
                    else:
                        wait = p.regroup_wait_slow
                    self.report.add(f"[G{g}] → 等待 {wait:.0f}s 后开下一组")
                    await asyncio.sleep(wait)
                previous_cum = cum
        finally:
            # 收尾(含异常路径):撤掉任何残留挂单,不留孤儿订单
            cleanup_errors: list[OrderCancellationUnconfirmed] = []
            for st in (buy, sell):
                if st.order_id is not None:
                    try:
                        await self._cancel_order_and_confirm(
                            st.order_id, st.leg.instrument,
                            lambda order, current=st:
                                self._sync_leg_order(current, order))
                    except OrderCancellationUnconfirmed as exc:
                        cleanup_errors.append(exc)
                        continue
                    st.order_id = None
                    st.order_price = None
                    st.order_qty = 0.0
                    st.order_counted = 0.0
                    st.order_counted_cost = 0.0
            if cleanup_errors:
                raise cleanup_errors[0]
        # 遗留池:所有组结束后的剩余量,一次性 IOC 兜底扫单
        self.cur_attempt = 0
        if p.taker_fallback:
            self.phase = "taker"
            for st in (buy, sell):
                short = round_lot(target_amount - st.filled, p.min_lot)
                if short >= p.min_lot - EPS:
                    self.report.add(f"[遗留扫单] {st.leg.direction} {st.leg.instrument} "
                                    f"剩 {short:.1f} 张 → IOC 兜底")
                    swept = await self._sweep_ioc(st, short)
                    self.report.leftover[st.leg.instrument] = {
                        "short": short,
                        "swept": swept,
                        "remaining": max(0.0, round_lot(short - swept, p.min_lot)),
                    }
            await self._rebalance_legs()
        self._log_final(target_amount)
        buy_short = max(0.0, round_lot(target_amount - buy.filled, p.min_lot))
        sell_short = max(0.0, round_lot(target_amount - sell.filled, p.min_lot))
        imbalance = self._leg_diff()
        if (buy_short >= p.min_lot - EPS or sell_short >= p.min_lot - EPS
                or abs(imbalance) >= p.min_lot - EPS):
            message = (f"执行未完整完成:买腿缺 {buy_short:.1f} 张,卖腿缺 {sell_short:.1f} 张,"
                       f"腿差 {imbalance:+.1f} 张")
            self.report.completion_error = message
            self.report.add(f"ERROR {message};已上报策略层处理")
            raise ExecutionIncompleteError(self.report, message)

    # ------------------------------------------------------------ main

    # ------------------------------------------------------------ report

    def _report_path(self) -> Optional[str]:
        """报告文件路径;dry-run 或关闭报告时返回 None。"""
        if self.params.dry_run or not self.params.write_report:
            return None
        d = self.params.report_dir or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), os.pardir, "state", "exec_reports")
        d = os.path.abspath(d)
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as exc:
            log.warning("执行报告目录创建失败 %s: %s", d, exc)
            return None
        stamp = china_now("%Y%m%d_%H%M%S")
        safe = self.buy_leg.replace("/", "_").replace(":", "_")
        return os.path.join(d, f"exec_report_{stamp}_{safe}.md")

    def _open_report(self) -> None:
        path = self._report_path()
        if not path:
            return
        header = (
            "# 价差建仓执行报告(执行中…)\n\n"
            f"- 开始: {china_now('%Y-%m-%d %H:%M:%S')} CST\n"
            f"- 买腿: `{self.buy_leg}`\n"
            f"- 卖腿: `{self.sell_leg}`\n"
            f"- 目标: {self.params.amount:.1f} 张/腿\n\n"
            "> 以下为实时日志;执行结束后本文件会被重写为结构化报告。\n\n"
            "## 实时日志\n\n```\n"
        )
        self.report.attach_file(path, header)

    def _finalize_report(self) -> None:
        if not self.report.log_path:
            return
        buy, sell = self.legs[self.buy_leg], self.legs[self.sell_leg]
        md = render_report_md(
            buy_leg=self.buy_leg, sell_leg=self.sell_leg,
            target=self.params.amount, n_groups=self.n_groups, dry_run=self.params.dry_run,
            combo_target=self.report.combo_target,
            combo_filled=self.report.combo_filled,
            combo_avg_net=self.report.combo_avg_net,
            combo_seconds=self.report.combo_seconds,
            groups=self.report.groups, leftover=self.report.leftover,
            rebalance=self.report.rebalance,
            completion_error=self.report.completion_error,
            buy_filled=buy.filled, sell_filled=sell.filled,
            buy_avg=buy.avg_price, sell_avg=sell.avg_price,
            events=self.report.events,
        )
        self.report.finalize(md)
        log.info("执行报告已生成: %s", self.report.log_path)

    # ------------------------------------------------------------ main

    async def execute(self) -> ExecReport:
        p = self.params
        self.started_at = time.monotonic()
        self._open_report()
        completed = False
        try:
            group_targets = self._group_targets(p.amount)
            combo_target = group_targets[0]
            self.report.combo_target = combo_target
            await self._load_ticks()
            await self._subscribe_tickers(list(self.legs.keys()))
            syn = self._syn_quotes()
            if syn:
                self.report.add(f"合成价差盘口 syn_bid={syn[0]:.4f} syn_ask={syn[1]:.4f} "
                                f"(mid={(syn[0]+syn[1])/2:.4f})")

            combo_start = time.monotonic()
            combo_filled = 0.0
            if p.use_combo and not p.dry_run:
                self.phase = "combo"
                if await self._setup_combo():
                    # combo_only 时组合簿目标为全量,而非仅第1组
                    ct = p.amount if p.combo_only else combo_target
                    self.report.combo_target = ct
                    combo_filled = await self._run_combo_phase(ct)
                    self.report.combo_filled = combo_filled
                    for st in self.legs.values():
                        st.filled = combo_filled  # 组合成交 = 两腿等量成交
            self.report.combo_seconds = time.monotonic() - combo_start

            if p.combo_only and not p.dry_run:
                # 只跑组合簿阶段:成交多少算多少,剩余量由调用方买先卖后补齐
                completed = True
                return self.report

            if p.dry_run:
                n = len(group_targets)
                self.report.add("dry-run 模式:跳过实际下单。以下为分组建仓将采用的报价阶梯:")
                self.report.add(f"  拆分:固定 {n} 组,第1组 {combo_target:.1f} 张/腿,"
                                f"每组最多 3 次尝试(mid → 2/3对手价 → IOC 对手价)")
                for st in self.legs.values():
                    bid, ask = self._top(st.leg.instrument)
                    a1 = self._quote_price(st, 1)
                    a2 = self._quote_price(st, 2)
                    a3 = self._quote_price(st, 3)
                    self.report.add(
                        f"  {st.leg.direction} {st.leg.instrument}: 盘口 {bid}/{ask} → "
                        f"A1(mid) {a1} · A2(2/3) {a2} · A3(taker) {a3}"
                    )
                return self.report

            self.phase = "legging"
            await self._run_batched_phase(p.amount)
            completed = True
        finally:
            # 收尾(含异常路径):登记两腿成交、生成结构化报告
            self.phase = "done"
            for name, st in self.legs.items():
                self.report.leg_filled[name] = st.filled
                self.report.leg_avg_price[name] = st.avg_price
            if not p.dry_run:
                outcome = ("执行成功" if completed and not self.report.completion_error
                           else ("执行失败" if self.report.completion_error else "执行异常结束"))
                self.report.add(
                    f"{outcome}: "
                    + ", ".join(f"{st.leg.direction} {n}: {st.filled}@{st.avg_price}"
                                for n, st in self.legs.items())
                )
                self._finalize_report()
        return self.report
