"""FastAPI 入口：lifespan 启动 WS / DB / 引擎 worker / 3 分钟扫描器。

运行（server/ 目录）: python -m uvicorn src.app:app --host 0.0.0.0 --port 8300
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from . import api, webhook
from .config import db_path, load_config
from .context import Ctx
from .engine import closer, hedge, orchestrator, risk, scanner, worker
from .engine.commands import CommandQueue
from .exchange.deribit_ws import DeribitWS, MAINNET_URL, TESTNET_URL
from .store.db import Database
from .store.repo import Repo

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("v2.app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_config()
    env_name = "TESTNET" if cfg.env.is_testnet else "MAINNET"
    banner = "=" * 60
    log.warning("%s\n  BTC OPTION SELLER V2  —  %s\n%s", banner, env_name, banner)
    if not cfg.env.is_testnet:
        log.warning("!!! 实盘环境 MAINNET，请确认所有待确认口径已由用户确认 !!!")

    db = Database(db_path())
    conn = await db.open()
    repo = Repo(conn)

    ws = DeribitWS(url=TESTNET_URL if cfg.env.is_testnet else MAINNET_URL,
                   client_id=cfg.env.client_id, client_secret=cfg.env.client_secret)
    delay = 3.0
    for attempt in range(1, 9):
        try:
            await ws.connect()
            break
        except Exception as exc:  # noqa: BLE001 - 启动期网络抖动重试
            log.warning("deribit ws 连接失败(第%d次): %s, %.0fs 后重试", attempt, exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)
    else:
        raise RuntimeError("deribit ws 连接失败，放弃启动")
    log.info("deribit ws connected, authenticated=%s", ws.authenticated)

    ctx = Ctx(cfg=cfg, db=db, repo=repo, ws=ws, queue=CommandQueue())
    ctx.handlers = {
        "OPEN": orchestrator.handle_open,
        "SCAN": scanner.handle_scan,
        "NORMAL_CLOSE": closer.handle_normal_close,
        "GROUP_CLOSE": closer.handle_group_close,
        "HEDGE_RELEASE": hedge.handle_release,
        "HEDGE_REBALANCE": hedge.handle_rebalance,
        "FORCE_CLOSE": risk.handle_force_close,
    }
    app.state.ctx = ctx

    # 崩溃恢复：启动时核对 DB 与交易所（M6 reconcile）
    await risk.reconcile_on_startup(ctx)

    worker_task = asyncio.create_task(worker.run_worker(ctx))
    scanner_task = asyncio.create_task(scanner.run_scheduler(ctx))
    try:
        yield
    finally:
        scanner_task.cancel()
        worker_task.cancel()
        await ws.close()
        await db.close()


app = FastAPI(title="BTC Option Seller V2", lifespan=lifespan)
app.include_router(webhook.router)
app.include_router(api.router)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# 单文件前端没有内容哈希文件名，缺了 Cache-Control 浏览器会按启发式缓存
# 自行决定过期时间，部署后继续显示旧页面。no-cache 不是"不缓存"，
# 而是每次都带 ETag 回源校验；页面没变仍然是 304，几乎不增加流量。
DASHBOARD_HEADERS = {"Cache-Control": "no-cache"}


def _dashboard_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers=DASHBOARD_HEADERS)


@app.get("/")
async def dashboard():
    return _dashboard_page()


@app.get("/signals")
async def signals_dashboard():
    return _dashboard_page()


@app.get("/returns")
async def returns_dashboard():
    return _dashboard_page()
