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
