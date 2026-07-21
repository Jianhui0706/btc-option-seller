"""数据库增量迁移烟雾测试。"""

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
    finally:
        await db.close()
