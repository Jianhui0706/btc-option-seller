"""命令定义与优先级队列（规范第十五节优先级：强平>正常平仓>对冲撤销>对冲调整>开仓）。"""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field
from typing import Any, Optional

# 数字越小优先级越高
PRIO_FORCE_CLOSE = 0
PRIO_NORMAL_CLOSE = 1
PRIO_HEDGE_RELEASE = 2
PRIO_HEDGE_REBALANCE = 3
PRIO_SCAN = 4
PRIO_OPEN = 5


@dataclass
class Command:
    kind: str                       # FORCE_CLOSE / NORMAL_CLOSE / REVERSE / GROUP_CLOSE / HEDGE_RELEASE /
                                    # HEDGE_REBALANCE / SCAN / OPEN
    priority: int
    payload: dict[str, Any] = field(default_factory=dict)


class CommandQueue:
    """优先级队列：同优先级按入队次序，并仲裁账户级对冲方向。

    SCAN 与重复对冲意图会合并；关对冲待执行/执行中时拒绝新调仓。
    OPEN/REVERSE 等 TradingView 命令不合并，便于逐条审计信号。
    """

    def __init__(self) -> None:
        self._q: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._seq = itertools.count()
        self._scan_pending = False
        self._hedge_rebalance_pending = False
        self._hedge_release_pending: set[int] = set()
        self._active_hedge_kind: str | None = None
        self._active_hedge_parent_id: int | None = None

    @property
    def active_hedge_kind(self) -> str | None:
        return self._active_hedge_kind

    @property
    def hedge_release_in_flight(self) -> bool:
        return (self._active_hedge_kind == "HEDGE_RELEASE"
                or bool(self._hedge_release_pending))

    def put(self, cmd: Command) -> bool:
        """入队。SCAN 若已有未处理的则丢弃（合并），返回是否实际入队。"""
        if cmd.kind == "SCAN":
            if self._scan_pending:
                return False
            self._scan_pending = True
        if cmd.kind == "HEDGE_REBALANCE":
            # BTC-PERPETUAL 是账户共享净仓。关对冲待执行/执行中时，禁止
            # 新的开对冲或调仓进入队列；同一时刻也只保留一个调仓意图。
            if (self._active_hedge_kind is not None
                    or self._hedge_release_pending
                    or self._hedge_rebalance_pending):
                return False
            self._hedge_rebalance_pending = True
        if cmd.kind == "HEDGE_RELEASE":
            parent_id = int(cmd.payload["parent_id"])
            if (parent_id in self._hedge_release_pending
                    or (self._active_hedge_kind == "HEDGE_RELEASE"
                        and self._active_hedge_parent_id == parent_id)):
                return False
            self._hedge_release_pending.add(parent_id)
        self._q.put_nowait((cmd.priority, next(self._seq), cmd))
        return True

    async def get(self) -> Command:
        _, _, cmd = await self._q.get()
        if cmd.kind == "SCAN":
            self._scan_pending = False
        if cmd.kind == "HEDGE_REBALANCE":
            self._hedge_rebalance_pending = False
        if cmd.kind == "HEDGE_RELEASE":
            self._hedge_release_pending.discard(int(cmd.payload["parent_id"]))
        if cmd.kind in ("HEDGE_REBALANCE", "HEDGE_RELEASE"):
            self._active_hedge_kind = cmd.kind
            self._active_hedge_parent_id = int(cmd.payload["parent_id"])
        return cmd

    def complete(self, cmd: Command) -> None:
        """释放账户级对冲执行权；worker 的 finally 必须调用。"""
        if (cmd.kind in ("HEDGE_REBALANCE", "HEDGE_RELEASE")
                and self._active_hedge_kind == cmd.kind
                and self._active_hedge_parent_id == int(cmd.payload["parent_id"])):
            self._active_hedge_kind = None
            self._active_hedge_parent_id = None

    def force_close(self, source: str, reason: str = "",
                    auto_resume: bool = False) -> Command:
        return Command("FORCE_CLOSE", PRIO_FORCE_CLOSE,
                       {"source": source, "reason": reason,
                        "auto_resume": auto_resume})

    def normal_close(self, parent_id: int, reason: str) -> Command:
        return Command("NORMAL_CLOSE", PRIO_NORMAL_CLOSE,
                       {"parent_id": parent_id, "reason": reason})

    def reverse(self, signal: dict) -> Command:
        """Close the managed trade first, then open the signal's new side."""
        return Command("REVERSE", PRIO_NORMAL_CLOSE, {"signal": signal})

    def recover_opening(self, parent_id: int) -> Command:
        """Reconcile a stranded OPENING trade from exchange positions."""
        return Command(
            "RECOVER_OPENING", PRIO_NORMAL_CLOSE, {"parent_id": parent_id})

    def group_close(self, group_id: int, reason: str = "MANUAL_GROUP") -> Command:
        return Command("GROUP_CLOSE", PRIO_NORMAL_CLOSE,
                       {"group_id": group_id, "reason": reason})

    def hedge_release(self, parent_id: int, *, manual: bool = False,
                      grouped: bool = False) -> Command:
        return Command(
            "HEDGE_RELEASE", PRIO_HEDGE_RELEASE,
            {"parent_id": parent_id, "manual": manual,
             "grouped": grouped})

    def hedge_rebalance(self, parent_id: int, order_btc: float,
                        net_delta: float, *, degraded_retry: bool = False,
                        spread_types: list[str] | None = None,
                        grouped: bool = False) -> Command:
        return Command("HEDGE_REBALANCE", PRIO_HEDGE_REBALANCE,
                       {"parent_id": parent_id, "order_btc": order_btc,
                        "net_delta": net_delta,
                        "degraded_retry": degraded_retry,
                        "spread_types": spread_types,
                        "grouped": grouped})

    def scan(self) -> Command:
        return Command("SCAN", PRIO_SCAN)

    def open(self, signal: dict) -> Command:
        return Command("OPEN", PRIO_OPEN, {"signal": signal})
