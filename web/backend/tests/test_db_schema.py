"""数据库增量迁移烟雾测试。"""

import json
import sqlite3

import pytest

from src.store.db import Database


@pytest.mark.asyncio
async def test_group_pnl_and_hedge_attribution_columns(tmp_path):
    db = Database(tmp_path / "schema.db")
    conn = await db.open()
    try:
        hedge_rows = await (await conn.execute(
            "PRAGMA table_info(hedge_state)")).fetchall()
        group_rows = await (await conn.execute(
            "PRAGMA table_info(trade_groups)")).fetchall()
        hedge_cols = {r[1] for r in hedge_rows}
        group_cols = {r[1] for r in group_rows}
        assert {"futures_usd", "futures_delta_btc"} <= hedge_cols
        assert {"unrealized_pnl_btc", "profit_ratio"} <= group_cols

        heartbeat_rows = await (await conn.execute(
            "PRAGMA table_info(pine_heartbeats)")).fetchall()
        heartbeat_cols = {r[1] for r in heartbeat_rows}
        assert {"id", "received_at", "bar_ts", "strategy", "payload_json"} <= heartbeat_cols

        parent_rows = await (await conn.execute(
            "PRAGMA table_info(parent_trades)")).fetchall()
        parent_cols = {r[1] for r in parent_rows}
        assert "trade_id" in parent_cols
        indexes = await (await conn.execute(
            "PRAGMA index_list(parent_trades)")).fetchall()
        assert "idx_parent_trades_trade_id" in {r[1] for r in indexes}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_legacy_latest_pine_status_is_backfilled_into_history(tmp_path):
    db_path = tmp_path / "legacy-status.db"
    legacy = sqlite3.connect(db_path)
    legacy.execute("CREATE TABLE system_state (key TEXT PRIMARY KEY, value TEXT)")
    legacy.execute(
        "INSERT INTO system_state(key, value) VALUES('pine_status', ?)",
        (json.dumps({
            "event": "STATUS",
            "strategy": "BTC_OPTION_SELLER_V2",
            "bar_ts": 123,
            "received_at": 456,
            "passphrase": "must-not-survive-upgrade",
        }),),
    )
    legacy.commit()
    legacy.close()

    db = Database(db_path)
    conn = await db.open()
    try:
        rows = await (await conn.execute(
            "SELECT received_at, bar_ts, strategy, payload_json "
            "FROM pine_heartbeats")).fetchall()

        assert len(rows) == 1
        assert rows[0]["received_at"] == 456
        assert rows[0]["bar_ts"] == 123
        assert rows[0]["strategy"] == "BTC_OPTION_SELLER_V2"
        assert "passphrase" not in json.loads(rows[0]["payload_json"])
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_heartbeat_migration_collapses_complete_duplicate_identities(tmp_path):
    db_path = tmp_path / "legacy-duplicate-heartbeats.db"
    legacy = sqlite3.connect(db_path)
    legacy.execute(
        "CREATE TABLE pine_heartbeats ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, received_at INTEGER NOT NULL, "
        "bar_ts INTEGER, strategy TEXT, payload_json TEXT NOT NULL)"
    )
    legacy.executemany(
        "INSERT INTO pine_heartbeats(received_at, bar_ts, strategy, payload_json) "
        "VALUES(?,?,?,?)",
        [
            (100, 123, "BTC_OPTION_SELLER_V2", '{"copy":1}'),
            (101, 123, "BTC_OPTION_SELLER_V2", '{"copy":2}'),
            (102, 123, "", '{"diagnostic":1}'),
            (103, 123, "", '{"diagnostic":2}'),
        ],
    )
    legacy.commit()
    legacy.close()

    db = Database(db_path)
    conn = await db.open()
    try:
        rows = await (await conn.execute(
            "SELECT received_at, strategy FROM pine_heartbeats ORDER BY id"
        )).fetchall()
        indexes = await (await conn.execute(
            "PRAGMA index_list(pine_heartbeats)"
        )).fetchall()

        assert [(row["received_at"], row["strategy"]) for row in rows] == [
            (100, "BTC_OPTION_SELLER_V2"),
            (102, ""),
            (103, ""),
        ]
        assert "uq_pine_heartbeats_strategy_bar" in {row[1] for row in indexes}
    finally:
        await db.close()
