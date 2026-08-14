"""用 Deribit 实时 BTC 指数价提前激活动态对冲。

行情回调只做内存比较；只有价格真正触及激活线时才读取实时 Delta 并进入
既有 hedge.scan_hedge 状态机，避免逐笔行情放大账户接口和数据库负载。
3 分钟扫描器仍负责完整风控、Delta 定期复核和对冲撤销确认。
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass

from .. import runtime_settings
from ..context import Ctx
from . import bep, hedge

log = logging.getLogger("v2.realtime_hedge")

TARGET_REFRESH_SEC = 1.0
CHECK_FLUSH_SEC = 15.0
CHECKPOINT_EVENT_SEC = 60.0


@dataclass(frozen=True)
class ActivationTarget:
    parent: dict
    trigger_price: float
    progress: float
    offset: float
    check_count: int


class RealtimeHedgeMonitor:
    """订阅逐笔指数价，并在内存中检测尚未激活的父交易。"""

    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.channel = f"deribit_price_index.{ctx.cfg.strategy.currency.lower()}_usd"
        self._targets: dict[int, ActivationTarget] = {}
        self._refresh_due_at = 0.0
        self._refresh_task: asyncio.Task | None = None
        self._activation_tasks: dict[int, asyncio.Task] = {}
        self._activation_lock = asyncio.Lock()
        self._check_flush_task: asyncio.Task | None = None
        self._pending_checks: dict[int, dict] = {}
        self._last_checkpoint_event_at: dict[int, float] = {}
        self._closed = False

    async def start(self) -> None:
        await self._refresh_targets()
        await self.ctx.ws.subscribe([self.channel], self.on_tick)
        self._check_flush_task = asyncio.create_task(self._flush_checks_loop())
        log.info("realtime hedge price monitor subscribed: %s", self.channel)

    async def close(self) -> None:
        self._closed = True
        tasks = [t for t in [self._refresh_task, self._check_flush_task,
                              *self._activation_tasks.values()]
                 if t is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._flush_checks(force_event=True)

    def on_tick(self, channel: str, data: dict) -> None:
        """同步回调，避免 DeribitWS 为每个 tick 再创建一个空协程任务。"""
        if self._closed or channel != self.channel or not isinstance(data, dict):
            return
        try:
            price = float(data["price"])
            timestamp = int(data.get("timestamp") or time.time() * 1000)
        except (KeyError, TypeError, ValueError):
            return
        if not math.isfinite(price) or price <= 0:
            return

        self.ctx.realtime_index_price = price
        self.ctx.realtime_index_ts = timestamp
        self._schedule_refresh_if_due()

        if getattr(self.ctx.cfg.hedge, "activation_price_source",
                   "REALTIME_INDEX") != "REALTIME_INDEX":
            return
        if self.ctx.hedge_frozen:
            return
        for parent_id, target in tuple(self._targets.items()):
            spread_type = target.parent["spread_type"]
            touched = bep.activation_touched(spread_type, price, target.trigger_price)
            self._observe_tick(target, price, timestamp, touched)
            if not touched:
                continue
            running = self._activation_tasks.get(parent_id)
            if running is None or running.done():
                task = asyncio.create_task(
                    self._activate(target, price, timestamp))
                self._activation_tasks[parent_id] = task

    def _observe_tick(self, target: ActivationTarget, price: float,
                      timestamp: int, touched: bool) -> None:
        """先在内存累计每个 tick，再由后台批量写库。"""
        parent_id = target.parent["id"]
        entry = self._pending_checks.get(parent_id)
        if entry is None:
            entry = {"target": target, "count": 0}
            self._pending_checks[parent_id] = entry
        entry.update(price=price, timestamp=timestamp, touched=touched)
        entry["count"] += 1

        live_checks = getattr(self.ctx, "hedge_live_checks", None)
        if not isinstance(live_checks, dict):
            return
        current = live_checks.get(parent_id, {})
        current.update({
            "check_count": int(current.get("check_count") or target.check_count) + 1,
            "last_check_at": timestamp,
            "last_check_price": price,
            "last_check_source": "realtime_index",
            "last_check_trigger": target.trigger_price,
            "last_check_progress": target.progress,
            "last_check_offset": target.offset,
            "last_check_touched": touched,
        })
        live_checks[parent_id] = current

    async def _flush_checks_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(CHECK_FLUSH_SEC)
            await self._flush_checks()

    async def _flush_checks(self, *, force_event: bool = False) -> None:
        """把 tick 汇总写入持久化状态，并每分钟产出一条可审计的检查事件。"""
        pending, self._pending_checks = self._pending_checks, {}
        for parent_id, entry in pending.items():
            target = entry["target"]
            try:
                total = await hedge.record_hedge_check(
                    self.ctx, target.parent,
                    hedge.HedgePriceLevels(
                        open_index_price=float(target.parent["open_index_price"])
                        if target.parent.get("open_index_price") else None,
                        bep=float(self.ctx.cfg.debug.bep_override or
                                  target.parent["bep_price"]),
                        activation_progress=target.progress,
                        activation_offset=target.offset,
                        activation_price=target.trigger_price,
                        release_line=0.0,
                    ),
                    float(entry["price"]), int(entry["timestamp"]),
                    "realtime_index", count=int(entry["count"]),
                    emit_checkpoint=(force_event or
                                     time.monotonic() - self._last_checkpoint_event_at.get(
                                         parent_id, 0.0) >= CHECKPOINT_EVENT_SEC),
                )
                if total is not None and (force_event or
                                          time.monotonic() - self._last_checkpoint_event_at.get(
                                              parent_id, 0.0) >= CHECKPOINT_EVENT_SEC):
                    self._last_checkpoint_event_at[parent_id] = time.monotonic()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 写库失败时保留未刷新的计数，下一轮继续合并后再试。
                previous = self._pending_checks.get(parent_id)
                if previous is None:
                    self._pending_checks[parent_id] = entry
                else:
                    previous["count"] += entry["count"]
                log.warning("flush realtime hedge checks failed for parent %s: %s",
                            parent_id, exc)

    def _schedule_refresh_if_due(self) -> None:
        now = time.monotonic()
        if now < self._refresh_due_at:
            return
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_due_at = now + TARGET_REFRESH_SEC
            self._refresh_task = asyncio.create_task(self._refresh_targets())

    async def _refresh_targets(self) -> None:
        """每秒刷新观察表，新开仓和运行时系数修改可快速生效。"""
        try:
            targets: dict[int, ActivationTarget] = {}
            progress = runtime_settings.effective_progress(self.ctx)
            for parent in await self.ctx.repo.open_parents():
                row = await self.ctx.repo.get_hedge(parent["id"])
                if not row or row.get("state") != "HEDGE_INACTIVE":
                    continue
                if await hedge.manual_rearm_blocked(
                        self.ctx, parent["id"]):
                    continue
                raw_bep = self.ctx.cfg.debug.bep_override or parent.get("bep_price")
                if not raw_bep:
                    continue
                raw_open = parent.get("open_index_price")
                trigger = (
                    bep.activation_price(float(raw_open), float(raw_bep), progress)
                    if raw_open else float(raw_bep)
                )
                live_checks = getattr(self.ctx, "hedge_live_checks", {})
                live = (live_checks.get(parent["id"], {})
                        if isinstance(live_checks, dict) else {})
                targets[parent["id"]] = ActivationTarget(
                    parent=parent,
                    trigger_price=trigger,
                    progress=progress,
                    offset=(abs(float(raw_bep) - float(raw_open)) * progress
                            if raw_open else 0.0),
                    check_count=max(int(row.get("check_count") or 0),
                                    int(live.get("check_count") or 0)),
                )
            self._targets = targets
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("refresh realtime hedge targets failed: %s", exc)

    async def _activate(self, target: ActivationTarget, price: float,
                        timestamp: int) -> None:
        parent_id = target.parent["id"]
        try:
            async with self._activation_lock:
                if self.ctx.hedge_frozen or await self.ctx.force_close_active():
                    return
                row = await self.ctx.repo.get_hedge(parent_id)
                if not row or row.get("state") != "HEDGE_INACTIVE":
                    return

                view = await hedge.snapshot_delta(self.ctx)
                await self._flush_checks(force_event=True)
                self.ctx.hedge_snapshot = hedge.snapshot_payload(
                    view, price, timestamp, int(time.time() * 1000),
                    price_source="realtime_index")
                await hedge.scan_hedge(
                    self.ctx, target.parent, price, timestamp, view=view,
                    price_source="realtime_index", allow_release=False,
                    record_check=False, allow_rebalance=False)
                account_view = await hedge.scan_account_hedge(self.ctx)
                if account_view and parent_id in account_view.target_usd_by_parent:
                    managed_usd = account_view.managed_usd_by_parent.get(
                        parent_id, 0.0)
                    option_delta = (
                        -account_view.target_usd_by_parent[parent_id]
                        / account_view.index_price)
                    parent_view = hedge.DeltaView(
                        option_delta=option_delta,
                        futures_delta=managed_usd / account_view.index_price,
                        net_delta=(option_delta
                                   + managed_usd / account_view.index_price),
                        equity_btc=account_view.equity_btc,
                        index_price=account_view.index_price,
                    )
                    snapshots = getattr(self.ctx, "hedge_snapshots", None)
                    if not isinstance(snapshots, dict):
                        snapshots = {}
                        self.ctx.hedge_snapshots = snapshots
                    snapshots[parent_id] = hedge.snapshot_payload(
                        parent_view, price, timestamp, int(time.time() * 1000),
                        price_source="realtime_index")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("realtime hedge activation failed for parent %s: %s",
                        parent_id, exc)
        finally:
            self._activation_tasks.pop(parent_id, None)
            await self._refresh_targets()
