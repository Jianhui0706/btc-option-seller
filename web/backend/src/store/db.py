"""aiosqlite 连接管理：WAL 模式、schema 初始化、单连接串行写。"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import aiosqlite

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._conn: Optional[aiosqlite.Connection] = None

    async def open(self) -> aiosqlite.Connection:
        conn = await aiosqlite.connect(self.path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        # 增量迁移：老库补列（CREATE TABLE IF NOT EXISTS 不会更新已有表）
        for ddl in (
            "ALTER TABLE hedge_state ADD COLUMN futures_delta_btc REAL DEFAULT 0",
            "ALTER TABLE trade_groups ADD COLUMN unrealized_pnl_btc REAL DEFAULT 0",
            "ALTER TABLE trade_groups ADD COLUMN profit_ratio REAL",
            "ALTER TABLE parent_trades ADD COLUMN trade_id TEXT",
            "ALTER TABLE parent_trades ADD COLUMN open_liquidity_role TEXT",
            "ALTER TABLE parent_trades ADD COLUMN close_liquidity_role TEXT",
        ):
            try:
                await conn.execute(ddl)
            except Exception:  # noqa: BLE001 - 列已存在
                pass
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_parent_trades_trade_id "
            "ON parent_trades(trade_id)")
        await conn.commit()
        self._conn = conn
        return conn

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "database not opened"
        return self._conn

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None
