import json

import pytest

from src.store.db import Database
from src.store.repo import Repo


@pytest.mark.asyncio
async def test_heartbeat_history_is_sanitized_ordered_and_bounded(tmp_path):
    db = Database(tmp_path / "heartbeats.db")
    conn = await db.open()
    try:
        repo = Repo(conn)
        for bar_ts in range(503):
            await repo.record_pine_heartbeat({
                "event": "STATUS",
                "strategy": "BTC_OPTION_SELLER_V2",
                "bar_ts": bar_ts,
                "received_at": bar_ts,
                "passphrase": "must-not-be-stored",
            })

        count = (await (await conn.execute(
            "SELECT COUNT(*) FROM pine_heartbeats")).fetchone())[0]
        oldest = (await (await conn.execute(
            "SELECT bar_ts FROM pine_heartbeats ORDER BY received_at, id LIMIT 1"
        )).fetchone())[0]
        latest_state = json.loads(await repo.get_state("pine_status", "{}"))
        recent = await repo.recent_pine_heartbeats(2)

        assert count == 500
        assert oldest == 3
        assert latest_state["bar_ts"] == 502
        assert "passphrase" not in latest_state
        assert [row["bar_ts"] for row in recent] == [502, 501]
        assert all("passphrase" not in row["payload"] for row in recent)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_heartbeat_history_deduplicates_only_complete_strategy_bar_identity(tmp_path):
    db = Database(tmp_path / "heartbeat-idempotency.db")
    conn = await db.open()
    try:
        repo = Repo(conn)
        base = {
            "event": "STATUS",
            "strategy": "BTC_OPTION_SELLER_V2",
            "bar_ts": 100,
            "received_at": 1000,
        }

        first = await repo.record_pine_heartbeat(base)
        duplicate = await repo.record_pine_heartbeat({**base, "received_at": 1001})
        next_bar = await repo.record_pine_heartbeat({**base, "bar_ts": 200})
        other_strategy = await repo.record_pine_heartbeat({**base, "strategy": "OTHER"})
        missing_strategy_1 = await repo.record_pine_heartbeat({**base, "strategy": ""})
        missing_strategy_2 = await repo.record_pine_heartbeat({**base, "strategy": ""})
        missing_bar_1 = await repo.record_pine_heartbeat({**base, "bar_ts": None})
        missing_bar_2 = await repo.record_pine_heartbeat({**base, "bar_ts": None})

        count = (await (await conn.execute(
            "SELECT COUNT(*) FROM pine_heartbeats")).fetchone())[0]

        assert first is True
        assert duplicate is False
        assert all((next_bar, other_strategy, missing_strategy_1,
                    missing_strategy_2, missing_bar_1, missing_bar_2))
        assert count == 7
    finally:
        await db.close()
