"""运行时上下文：配置、DB、WS、命令队列、运行标志、当前执行任务。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from .config import AppConfig
from .engine.commands import CommandQueue
from .exchange.deribit_ws import DeribitWS
from .store.db import Database
from .store.repo import Repo

Handler = Callable[["Ctx", dict], Awaitable[None]]


@dataclass
class Ctx:
    cfg: AppConfig
    db: Database
    repo: Repo
    ws: DeribitWS
    queue: CommandQueue
    handlers: dict[str, Handler] = field(default_factory=dict)
    # 当前正在运行的执行任务（开/平仓），FORCE_CLOSE 可打断
    current_exec_task: Optional[asyncio.Task] = None
    # 同一根 bar 内"平仓优先于对冲"：平仓处理时置位，对冲命令检查后丢弃
    hedge_frozen: bool = False
    # 盘口连续缺失计数（风控用）
    quote_missing_count: int = 0

    async def trading_enabled(self) -> bool:
        return await self.repo.get_flag("trading_enabled", default=False)

    async def force_close_active(self) -> bool:
        return await self.repo.get_flag("force_close_active", default=False)
