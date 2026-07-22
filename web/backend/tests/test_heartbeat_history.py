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
