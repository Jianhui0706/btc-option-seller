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
