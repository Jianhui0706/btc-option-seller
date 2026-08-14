"""单腿期权执行器：mid maker → 2/3 touch maker → 对手价 IOC 三级阶梯。

用途（V2）：
  1. 开仓"严格买腿先行"序列中逐腿执行（先买保护腿，成交后再卖收权腿）；
  2. 正常退出保留买腿时，仅买回卖腿；
  3. 开仓失败回滚（IOC 平掉已成交买腿）。

报价阶梯与等待节奏沿用 V1 spread_executor 的组内三次尝试设计。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from ..exchange.deribit_ws import DeribitWS, DeribitError
from .spread_executor import (EPS, OrderCancellationUnconfirmed, TickRule,
                              round_lot)

log = logging.getLogger("deribit.single")


@dataclass
class SingleLegParams:
    amount: float                          # 目标张数
    min_lot: float = 0.1
    attempt_waits: tuple = (3.0, 8.0, 15.0)   # A1/A2 挂单等待、A3 后复查间隔
    pay_extra_ticks: int = 2               # 兜底 IOC 越过对手价的 tick 数
    limit_price: Optional[float] = None    # 价格保护:买不高于/卖不低于该价(None 不限)
    rounds: int = 2                        # 三级阶梯最多重复几轮
    dry_run: bool = False
    reduce_only: bool = False


@dataclass
class SingleLegReport:
    instrument: str
    direction: str
    target: float
    filled: float = 0.0
    avg_price: float = 0.0
    maker_filled: float = 0.0
    taker_filled: float = 0.0
    fee_btc: float = 0.0
    events: list[str] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.filled + EPS >= self.target

    @property
    def liquidity_role(self) -> str:
        if self.maker_filled > EPS and self.taker_filled > EPS:
            return "MIXED"
        if self.maker_filled > EPS:
            return "MAKER"
        if self.taker_filled > EPS:
            return "TAKER"
        return "UNKNOWN"


class SingleLegExecutor:
    def __init__(self, ws: DeribitWS, instrument: str, direction: str,
                 params: SingleLegParams,
                 on_event: Optional[Callable[[str], None]] = None,
                 on_attempt: Optional[
                     Callable[[dict], Awaitable[None]]] = None):
        assert direction in ("buy", "sell")
        self.ws = ws
        self.instrument = instrument
        self.direction = direction
        self.p = params
        self.rule = TickRule()
        self.ticker: dict = {}
        self.report = SingleLegReport(instrument=instrument, direction=direction,
                                      target=params.amount)
        self._on_event = on_event
        self._on_attempt = on_attempt
        self._order_id: Optional[str] = None
        self._order_qty = 0.0
        self._order_counted = 0.0
        self._order_counted_cost = 0.0

    # ------------------------------------------------------------ infra

    def _log(self, msg: str) -> None:
        self.report.events.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        log.info("%s %s: %s", self.direction, self.instrument, msg)
        if self._on_event:
            try:
                self._on_event(msg)
            except Exception:  # noqa: BLE001
                log.exception("single-leg event callback failed")

    async def _record_attempt(self, started: float, **values) -> None:
        finished = time.time()
        item = {
            **values,
            "fill_qty_is_incremental": True,
            "placed_at": int(started * 1000),
            "finished_at": int(finished * 1000),
            "duration_ms": int((finished - started) * 1000),
        }
        self.report.attempts.append(item)
        if self._on_attempt:
            try:
                await self._on_attempt(dict(item))
            except Exception:  # noqa: BLE001 - 统计失败不能打断真实交易
                log.exception("single-leg attempt callback failed")

    async def _on_ticker(self, channel: str, data: dict) -> None:
        self.ticker = data

    def _top(self) -> tuple[Optional[float], Optional[float]]:
        return (self.ticker.get("best_bid_price") or None,
                self.ticker.get("best_ask_price") or None)

    def _add_fill(self, qty: float, avg: float, liquidity_role: str) -> None:
        if qty <= EPS:
            return
        cost = self.report.avg_price * self.report.filled + avg * qty
        self.report.filled += qty
        self.report.avg_price = cost / max(self.report.filled, EPS)
        if liquidity_role == "MAKER":
            self.report.maker_filled += qty
        elif liquidity_role == "TAKER":
            self.report.taker_filled += qty

    async def _setup(self) -> None:
        info = await self.ws.request("public/get_instrument",
                                     {"instrument_name": self.instrument})
        self.rule = TickRule.from_instrument(info)
        await self.ws.subscribe([f"ticker.{self.instrument}.100ms"], self._on_ticker)
        for _ in range(50):
            if self.ticker:
                return
            await asyncio.sleep(0.1)
        # A websocket subscription acknowledgement does not guarantee that the
        # first ticker frame arrives promptly.  Falling back to the public
        # snapshot avoids aborting after a protective buy leg has already
        # filled merely because one streaming update was lost.
        try:
            snapshot = await self.ws.request(
                "public/ticker", {"instrument_name": self.instrument})
        except Exception:  # noqa: BLE001 - preserve the explicit error below
            snapshot = {}
        if snapshot:
            self.ticker = snapshot
            self._log("行情订阅首帧超时，已使用 public/ticker 快照兜底")
            return
        raise RuntimeError(f"no ticker data for {self.instrument}")

    # ------------------------------------------------------------ pricing

    def _clamp_limits(self, price: float) -> Optional[float]:
        """交易所 min/max 限价 + 调用方 limit_price 保护;违反保护返回 None(放弃本次)。"""
        is_buy = self.direction == "buy"
        if is_buy and self.ticker.get("max_price") is not None:
            price = min(price, self.ticker["max_price"])
        if (not is_buy) and self.ticker.get("min_price") is not None:
            price = max(price, self.ticker["min_price"])
        lp = self.p.limit_price
        if lp is not None:
            if is_buy and price > lp + EPS:
                return None
            if (not is_buy) and price < lp - EPS:
                return None
        return round(max(price, self.rule.base), 10)

    def _quote(self, attempt: int) -> Optional[float]:
        bid, ask = self._top()
        if bid is None or ask is None:
            return None
        mid = (bid + ask) / 2.0
        is_buy = self.direction == "buy"
        if attempt <= 1:
            raw = mid
        elif attempt == 2:
            touch = ask if is_buy else bid
            raw = (2.0 / 3.0) * touch + (1.0 / 3.0) * mid
        else:
            touch = ask if is_buy else bid
            raw = touch + (self.p.pay_extra_ticks * self.rule.tick_at(touch)
                           * (1 if is_buy else -1))
        if attempt >= 3:
            price = self.rule.round(raw, up=is_buy)
            return self._clamp_limits(price)
        price = self.rule.round(raw, up=(not is_buy))
        price = min(max(price, bid), ask)
        return self._clamp_limits(price)

    # ------------------------------------------------------------ orders

    async def _place(self, qty: float, price: float, ioc: bool) -> Optional[dict]:
        params = {
            "instrument_name": self.instrument,
            "amount": qty,
            "type": "limit",
            "price": price,
            "label": "v2-single-leg",
        }
        if ioc:
            params["time_in_force"] = "immediate_or_cancel"
        else:
            params["post_only"] = True
        if self.p.reduce_only:
            params["reduce_only"] = True
        method = "private/buy" if self.direction == "buy" else "private/sell"
        if self.p.dry_run:
            self._log(f"DRY-RUN {method} {qty}@{price} {'IOC' if ioc else 'post_only'}")
            return None
        return await self.ws.request(method, params)

    async def _reconcile_and_cancel(self) -> None:
        """读取当前挂单成交并撤单，累计到 report。"""
        if self._order_id is None:
            return
        order_id = self._order_id
        terminal = {"filled", "cancelled", "rejected"}
        last_error = ""
        for attempt in range(1, 4):
            try:
                order = await self.ws.request(
                    "private/get_order_state", {"order_id": order_id})
                self._sync(order)
                if order.get("order_state") in terminal:
                    break
            except DeribitError as exc:
                last_error = str(exc)
            try:
                await self.ws.request("private/cancel", {"order_id": order_id})
            except DeribitError as exc:
                last_error = str(exc)
            try:
                order = await self.ws.request(
                    "private/get_order_state", {"order_id": order_id})
                self._sync(order)
                if order.get("order_state") in terminal:
                    break
            except DeribitError as exc:
                last_error = str(exc)
            if attempt < 3:
                await asyncio.sleep(0.25 * attempt)
        else:
            self._log(
                f"ERROR 撤单未确认 order_id={order_id}; 保留执行中状态，禁止继续下单")
            raise OrderCancellationUnconfirmed(
                order_id, self.instrument, last_error)
        self._order_id = None
        self._order_qty = 0.0
        self._order_counted = 0.0
        self._order_counted_cost = 0.0

    def _sync(self, order: dict) -> None:
        filled = float(order.get("filled_amount") or 0.0)
        avg = float(order.get("average_price") or 0.0)
        cost = filled * avg
        dq = max(0.0, filled - self._order_counted)
        dc = max(0.0, cost - self._order_counted_cost)
        if dq > EPS:
            # _sync 只处理 post_only 委托，因此其真实成交角色必为 maker。
            self._add_fill(dq, dc / max(dq, EPS), "MAKER")
            self._order_counted = filled
            self._order_counted_cost = cost

    # ------------------------------------------------------------ main

    async def execute(self) -> SingleLegReport:
        p = self.p
        await self._setup()
        try:
            for rnd in range(1, p.rounds + 1):
                for attempt in (1, 2, 3):
                    started = time.time()
                    todo = round_lot(p.amount - self.report.filled, p.min_lot)
                    if todo < p.min_lot - EPS:
                        return self.report
                    price = self._quote(attempt)
                    if price is None:
                        await self._record_attempt(
                            started, attempt_no=attempt, round=rnd,
                            liquidity_role="MAKER" if attempt < 3 else "TAKER",
                            requested_qty=todo, limit_price=None,
                            filled_qty=0.0, status="SKIPPED",
                            details="no_quote_or_price_protection")
                        self._log(f"[R{rnd}/A{attempt}] 无有效盘口或触发价格保护,跳过")
                        await asyncio.sleep(1.0)
                        continue
                    if attempt < 3:
                        filled_before = self.report.filled
                        cost_before = self.report.avg_price * self.report.filled
                        result = await self._place(todo, price, ioc=False)
                        order_id = None
                        if result:
                            order_id = result["order"]["order_id"]
                            self._order_id = order_id
                            self._order_qty = todo
                            self._order_counted = 0.0
                            self._order_counted_cost = 0.0
                            self._log(f"[R{rnd}/A{attempt}] 挂 {todo:.1f}@"
                                      f"{result['order'].get('price', price)} post_only")
                        await asyncio.sleep(p.attempt_waits[attempt - 1])
                        await self._reconcile_and_cancel()
                        filled_now = max(0.0, self.report.filled - filled_before)
                        cost_now = max(
                            0.0,
                            self.report.avg_price * self.report.filled - cost_before)
                        avg_now = cost_now / filled_now if filled_now > EPS else None
                        await self._record_attempt(
                            started, attempt_no=attempt, round=rnd,
                            liquidity_role="MAKER", requested_qty=todo,
                            limit_price=price, filled_qty=filled_now,
                            avg_fill_price=avg_now,
                            status=("FILLED" if filled_now + EPS >= todo
                                    else "PARTIAL" if filled_now > EPS
                                    else "CANCELED"),
                            exchange_order_id=order_id)
                    else:
                        self._log(f"[R{rnd}/A{attempt}] IOC {todo:.1f}@{price}")
                        result = await self._place(todo, price, ioc=True)
                        if result:
                            order = result["order"]
                            trades = result.get("trades") or []
                            filled = float(order.get("filled_amount") or 0.0)
                            avg = float(order.get("average_price") or 0.0)
                            self.report.fee_btc += sum(
                                float(trade.get("fee") or 0.0)
                                for trade in trades)
                            self._add_fill(filled, avg, "TAKER")
                            await self._record_attempt(
                                started, attempt_no=attempt, round=rnd,
                                liquidity_role="TAKER", requested_qty=todo,
                                limit_price=price, filled_qty=filled,
                                avg_fill_price=avg or None,
                                status=("FILLED" if filled + EPS >= todo
                                        else "PARTIAL" if filled > EPS
                                        else "CANCELED"),
                                exchange_order_id=order.get("order_id"),
                                exchange_trade_ids=[
                                    trade.get("trade_id") for trade in trades
                                    if trade.get("trade_id")])
                        else:
                            await self._record_attempt(
                                started, attempt_no=attempt, round=rnd,
                                liquidity_role="TAKER", requested_qty=todo,
                                limit_price=price, filled_qty=0.0,
                                status="DRY_RUN")
                    self._log(f"[R{rnd}/A{attempt}] 累计 {self.report.filled:.1f}/"
                              f"{p.amount:.1f} @ {self.report.avg_price:.4f}")
            return self.report
        finally:
            await self._reconcile_and_cancel()
            self._log(f"结束: {self.report.filled:.1f}/{p.amount:.1f}"
                      f" @ {self.report.avg_price:.4f}")
