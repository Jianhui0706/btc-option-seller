"""Maker/taker 成交角色汇总与持久化测试。"""

import pytest

from src.execution.adapter import _spread_report_liquidity_role
from src.execution.exec_report import AttemptRecord, GroupRecord
from src.execution.single_leg import SingleLegReport
from src.execution.spread_executor import ExecReport
from src.store.db import Database
from src.store.repo import Repo


def test_single_leg_report_distinguishes_maker_taker_and_mixed():
    report = SingleLegReport("BTC-TEST", "buy", 1.0)
    assert report.liquidity_role == "UNKNOWN"
    report.maker_filled = 0.4
    assert report.liquidity_role == "MAKER"
    report.taker_filled = 0.6
    assert report.liquidity_role == "MIXED"


def test_spread_report_uses_only_attempts_with_real_fills():
    report = ExecReport()
    report.groups = [
        GroupRecord(
            index=1,
            cum_target=1.0,
            attempts=[
                AttemptRecord(attempt=1),  # 未成交 maker 不应计入
                AttemptRecord(attempt=3, buy_filled=0.5, sell_filled=0.5),
            ],
        )
    ]
    assert _spread_report_liquidity_role(report) == "TAKER"

    report.combo_filled = 0.5
    assert _spread_report_liquidity_role(report) == "MIXED"


@pytest.mark.asyncio
async def test_parent_liquidity_role_merges_multiple_close_batches(tmp_path):
    db = Database(tmp_path / "liquidity.db")
    conn = await db.open()
    try:
        await conn.execute(
            "INSERT INTO parent_trades(id, spread_type, state, created_at) "
            "VALUES(1, 'PUT_CREDIT', 'OPEN', 1)"
        )
        await conn.commit()
        repo = Repo(conn)

        await repo.set_liquidity_role(1, "open", "MAKER")
        await repo.set_liquidity_role(1, "close", "MAKER")
        await repo.set_liquidity_role(1, "close", "TAKER")

        row = await (await conn.execute(
            "SELECT open_liquidity_role, close_liquidity_role "
            "FROM parent_trades WHERE id=1"
        )).fetchone()
        assert row["open_liquidity_role"] == "MAKER"
        assert row["close_liquidity_role"] == "MIXED"
    finally:
        await db.close()
