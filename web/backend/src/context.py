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
    # worker 当前处理的业务命令。扫描器不再占用业务命令队列，这两个字段只用于
    # 可观测性，不参与信号取舍或交易顺序。
    current_command_kind: Optional[str] = None
    current_command_started_ts: Optional[int] = None
    # 独立扫描协程的运行状态。时间戳均为 Unix 毫秒。
    scan_running: bool = False
    scan_last_started_ts: Optional[int] = None
    scan_last_completed_ts: Optional[int] = None
    scan_last_error: Optional[str] = None
    scan_last_error_kind: Optional[str] = None
    scan_last_error_ts: Optional[int] = None
    # 同一根 bar 内"平仓优先于对冲"：平仓处理时置位，对冲命令检查后丢弃
    hedge_frozen: bool = False
    # 最近一轮完整 3 分钟扫描得到的账户级对冲快照，仅用于状态页解释
    # “为什么没有下对冲单”。交易决策仍在 hedge.scan_hedge 内完成。
    hedge_snapshot: Optional[dict] = None
    # 盘口连续缺失计数（风控用）
    quote_missing_count: int = 0
    # 开仓条件看板快照缓存：2h bar 两小时才变一次，不必每次刷新面板都重拉全历史
    entry_snapshot: Optional[dict] = None
    entry_snapshot_ts: float = 0.0
    # DVOL 开仓分位单独长缓存：2 年历史需分页 ~13 次调用、每 12h 才更新一根，
    # 与 2h 门槛快照分开，看板降到 15s 高频刷新时不必反复重拉全历史（防 Deribit 限频）。
    dvol_entry_cache: Optional[tuple] = None      # (pct, tier, samples)
    dvol_entry_cache_ts: float = 0.0

    async def trading_enabled(self) -> bool:
        return await self.repo.get_flag("trading_enabled", default=False)

    async def force_close_active(self) -> bool:
        return await self.repo.get_flag("force_close_active", default=False)

    def request_force_close(self, source: str, reason: str = "",
                            auto_resume: bool = False) -> bool:
        """立即打断当前执行任务，并把强平命令放到最高优先级队列。

        只把 FORCE_CLOSE 放进队列并不能抢占 worker 正在等待的开仓成交，
        所以取消动作必须由发起请求的协程立即执行。
        返回值表示是否确实打断了一个仍在运行的执行任务。
        """
        task = self.current_exec_task
        interrupted = bool(task and not task.done())
        if interrupted:
            task.cancel()
        self.queue.put(self.queue.force_close(
            source=source, reason=reason, auto_resume=auto_resume))
        return interrupted
