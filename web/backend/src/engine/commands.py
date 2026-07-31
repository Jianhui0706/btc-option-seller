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
    """asyncio.PriorityQueue 包装：同优先级按入队次序；仅 SCAN 去重合并。

    OPEN/REVERSE 等 TradingView 交易命令永不在这里合并，便于高频测试逐条
    验证后端确实遵循信号。
    """

    def __init__(self) -> None:
        self._q: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._seq = itertools.count()
        self._scan_pending = False

    def put(self, cmd: Command) -> bool:
        """入队。SCAN 若已有未处理的则丢弃（合并），返回是否实际入队。"""
        if cmd.kind == "SCAN":
            if self._scan_pending:
                return False
            self._scan_pending = True
        self._q.put_nowait((cmd.priority, next(self._seq), cmd))
        return True

    async def get(self) -> Command:
        _, _, cmd = await self._q.get()
        if cmd.kind == "SCAN":
            self._scan_pending = False
        return cmd

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

    def hedge_release(self, parent_id: int) -> Command:
        return Command("HEDGE_RELEASE", PRIO_HEDGE_RELEASE, {"parent_id": parent_id})

    def hedge_rebalance(self, parent_id: int, order_btc: float,
                        net_delta: float) -> Command:
        return Command("HEDGE_REBALANCE", PRIO_HEDGE_REBALANCE,
                       {"parent_id": parent_id, "order_btc": order_btc,
                        "net_delta": net_delta})

    def scan(self) -> Command:
        return Command("SCAN", PRIO_SCAN)

    def open(self, signal: dict) -> Command:
        return Command("OPEN", PRIO_OPEN, {"signal": signal})
