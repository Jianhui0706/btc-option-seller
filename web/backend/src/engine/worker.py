"""单 worker 串行消费交易命令队列——确保每条 TradingView 信号依次执行。

处理器在 app 启动时注册到 ctx.handlers（避免模块间循环依赖）：
  OPEN → orchestrator / NORMAL_CLOSE、GROUP_CLOSE → closer /
  HEDGE_RELEASE, HEDGE_REBALANCE → hedge / FORCE_CLOSE → risk

定时风险扫描由独立协程运行，不占用此队列；反向信号仍逐条排队，不合并、不跳过。
"""

from __future__ import annotations

import asyncio
import logging
import time

from ..context import Ctx
from ..monitor import events as ev

log = logging.getLogger("v2.worker")


async def run_worker(ctx: Ctx) -> None:
    log.info("engine worker started")
    while True:
        cmd = await ctx.queue.get()
        handler = ctx.handlers.get(cmd.kind)
        if handler is None:
            log.error("no handler for command %s", cmd.kind)
            continue
        ctx.current_command_kind = cmd.kind
        ctx.current_command_started_ts = int(time.time() * 1000)
        try:
            await handler(ctx, cmd.payload)
        except asyncio.CancelledError:
            # FORCE_CLOSE 会取消当前开/平仓的子任务。等待已取消子任务时抛出的
            # CancelledError 不代表 worker 自身应退出；应用停机直接取消 worker
            # 时，cancelling() > 0，仍按正常停机路径向上传播。
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            log.warning("command %s interrupted; worker continues", cmd.kind)
        except Exception as exc:  # noqa: BLE001 - 单条命令失败不拖垮 worker
            log.exception("command %s failed", cmd.kind)
            try:
                await ev.emit(ctx.repo.conn, ev.SCAN_ERROR, level="ERROR",
                              command=cmd.kind, error=str(exc))
            except Exception:  # noqa: BLE001
                pass
        finally:
            ctx.current_command_kind = None
            ctx.current_command_started_ts = None
