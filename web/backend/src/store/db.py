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
        await conn.execute("CREATE TABLE IF NOT EXISTS execution_attempts (id INTEGER PRIMARY KEY AUTOINCREMENT, parent_id INTEGER, phase TEXT NOT NULL, operation TEXT, instrument TEXT, direction TEXT, liquidity_role TEXT, attempt_no INTEGER, round_no INTEGER, requested_qty REAL, filled_qty REAL DEFAULT 0, limit_price REAL, avg_fill_price REAL, status TEXT, placed_at INTEGER, finished_at INTEGER, duration_ms INTEGER, group_index INTEGER, group_count INTEGER, cycle_id TEXT, cycle_attempt_no INTEGER, cycle_max_attempts INTEGER, recorded_at INTEGER, exchange_order_id TEXT, details_json TEXT)")
        # 增量迁移：老库补列（CREATE TABLE IF NOT EXISTS 不会更新已有表）
        for ddl in (
            "ALTER TABLE hedge_state ADD COLUMN futures_delta_btc REAL DEFAULT 0",
            "ALTER TABLE hedge_state ADD COLUMN check_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE hedge_state ADD COLUMN last_check_at INTEGER",
            "ALTER TABLE hedge_state ADD COLUMN last_check_price REAL",
            "ALTER TABLE hedge_state ADD COLUMN last_check_source TEXT",
            "ALTER TABLE hedge_state ADD COLUMN last_check_trigger REAL",
            "ALTER TABLE hedge_state ADD COLUMN last_check_progress REAL",
            "ALTER TABLE hedge_state ADD COLUMN last_check_offset REAL",
            "ALTER TABLE hedge_state ADD COLUMN last_check_touched INTEGER DEFAULT 0",
            "ALTER TABLE hedge_state ADD COLUMN release_confirm_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE hedge_state ADD COLUMN manual_hold INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE hedge_actions ADD COLUMN book_key TEXT",
            "ALTER TABLE hedge_actions ADD COLUMN requested_usd REAL",
            "ALTER TABLE hedge_actions ADD COLUMN managed_usd_after REAL",
            "ALTER TABLE hedge_actions ADD COLUMN cycle_id TEXT",
            "ALTER TABLE hedge_actions ADD COLUMN attempt_no INTEGER",
            "ALTER TABLE hedge_actions ADD COLUMN max_attempts INTEGER",
            "ALTER TABLE hedge_actions ADD COLUMN residual_delta REAL",
            "ALTER TABLE hedge_actions ADD COLUMN details_json TEXT",
            "ALTER TABLE trade_groups ADD COLUMN unrealized_pnl_btc REAL DEFAULT 0",
            "ALTER TABLE trade_groups ADD COLUMN profit_ratio REAL",
            "ALTER TABLE parent_trades ADD COLUMN trade_id TEXT",
            "ALTER TABLE parent_trades ADD COLUMN open_liquidity_role TEXT",
            "ALTER TABLE parent_trades ADD COLUMN close_liquidity_role TEXT",
            "ALTER TABLE parent_trades ADD COLUMN settlement_price REAL",
            "ALTER TABLE signals ADD COLUMN reject_detail TEXT",
            "ALTER TABLE execution_attempts ADD COLUMN group_index INTEGER",
            "ALTER TABLE execution_attempts ADD COLUMN group_count INTEGER",
            "ALTER TABLE execution_attempts ADD COLUMN cycle_id TEXT",
            "ALTER TABLE execution_attempts ADD COLUMN cycle_attempt_no INTEGER",
            "ALTER TABLE execution_attempts ADD COLUMN cycle_max_attempts INTEGER",
            "ALTER TABLE execution_attempts ADD COLUMN recorded_at INTEGER",
            "ALTER TABLE execution_attempts ADD COLUMN exchange_order_id TEXT",
        ):
            try:
                await conn.execute(ddl)
            except Exception:  # noqa: BLE001 - 列已存在
                pass
        # Older hedge rows stored the actual fill only. Recover the requested
        # amount from the immediately preceding rebalance audit event.
        await conn.execute(
            "UPDATE hedge_actions SET requested_usd=("
            "SELECT json_extract(events.payload_json, '$.order_usd') "
            "FROM events WHERE events.parent_id=hedge_actions.parent_id "
            "AND events.code='DELTA_HEDGE_REBALANCE' "
            "AND events.ts<=hedge_actions.ts "
            "AND hedge_actions.ts-events.ts<=30000 "
            "AND json_valid(events.payload_json) "
            "ORDER BY events.ts DESC LIMIT 1) "
            "WHERE action='REBALANCE' AND requested_usd IS NULL")
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_parent_trades_trade_id "
            "ON parent_trades(trade_id)")
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_execution_attempts_cycle "
            "ON execution_attempts(cycle_id, id)")
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
