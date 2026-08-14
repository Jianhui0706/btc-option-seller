from types import SimpleNamespace

import pytest

from src.engine.orchestrator import open_conflict
from src.monitor import events as ev
from src.store.db import Database
from src.store.repo import Repo


def position(instrument: str, *, short: bool = True):
    return SimpleNamespace(instrument=instrument, is_short=short)


def parent(parent_id: int, source: str, spread_type: str,
           sell_instrument: str):
    return {
        "id": parent_id,
        "trade_id": source,
        "spread_type": spread_type,
        "sell_instrument": sell_instrument,
        "state": "OPEN",
    }


def signal(source: str, spread_type: str):
    return {"trade_id": source, "spread_type": spread_type}


def test_same_source_cannot_open_opposite_spread_while_any_position_active():
    active = [parent(1, "source-a", "PUT_CREDIT", "BTC-PUT-SELL")]
    conflict, detail = open_conflict(
        signal("source-a", "CALL_CREDIT"), active,
        [position("BTC-PUT-SELL")])

    assert conflict == ev.REJECT_ACTIVE_POSITION_EXISTS
    assert detail["active_parent_ids"] == [1]
    assert detail["requested_spread_type"] == "CALL_CREDIT"


def test_same_source_rejects_another_spread_on_same_side():
    active = [parent(1, "source-a", "PUT_CREDIT", "BTC-PUT-SELL")]
    conflict, detail = open_conflict(
        signal("source-a", "PUT_CREDIT"), active,
        [position("BTC-PUT-SELL")])

    assert conflict == ev.REJECT_ACTIVE_POSITION_EXISTS
    assert detail["active_parent_ids"] == [1]


def test_other_source_is_rejected_while_source_owns_active_trade():
    active = [parent(1, "source-a", "PUT_CREDIT", "BTC-PUT-SELL")]
    conflict, detail = open_conflict(
        signal("source-b", "PUT_CREDIT"), active,
        [position("BTC-PUT-SELL")])

    assert conflict == ev.REJECT_ACTIVE_POSITION_EXISTS
    assert detail["active_parent_ids"] == [1]


def test_other_source_cannot_open_opposite_spread_while_any_position_active():
    active = [parent(1, "source-a", "PUT_CREDIT", "BTC-PUT-SELL")]
    conflict, detail = open_conflict(
        signal("source-b", "CALL_CREDIT"), active,
        [position("BTC-PUT-SELL")])

    assert conflict == ev.REJECT_ACTIVE_POSITION_EXISTS
    assert detail["active_parent_ids"] == [1]


def test_same_source_cannot_add_while_previous_action_is_incomplete():
    active = [parent(1, "source-a", "PUT_CREDIT", "BTC-PUT-SELL")]
    active[0]["state"] = "CLOSING"

    conflict, detail = open_conflict(
        signal("source-a", "PUT_CREDIT"), active,
        [position("BTC-PUT-SELL")])

    assert conflict == ev.REJECT_ACTIVE_POSITION_EXISTS
    assert detail["active_states"] == ["CLOSING"]


def test_unmanaged_account_short_still_blocks_first_open():
    conflict, detail = open_conflict(
        signal("source-a", "CALL_CREDIT"), [],
        [position("BTC-MANUAL-SHORT")])

    assert conflict == ev.REJECT_EXISTING_OPTION_SHORT
    assert detail["shorts"] == ["BTC-MANUAL-SHORT"]


def test_first_position_is_allowed_when_account_has_no_active_parent_or_short():
    conflict, detail = open_conflict(
        signal("source-a", "PUT_CREDIT"), [], [])

    assert conflict is None
    assert detail == {}


@pytest.mark.asyncio
async def test_repository_routes_same_source_close_by_spread_type(tmp_path):
    db = Database(tmp_path / "source-routing.db")
    conn = await db.open()
    repo = Repo(conn)
    try:
        for signal_id, spread_type in (
                ("put-open", "PUT_CREDIT"),
                ("call-open", "CALL_CREDIT")):
            await repo.insert_signal(
                signal_id, "OPEN_REQUEST", "MACD", spread_type, {})
            await repo.create_parent(
                signal_id, spread_type, None, trade_id="source-a")

        puts = await repo.active_parents_by_trade_id(
            "source-a", "MACD", "PUT_CREDIT")
        calls = await repo.active_parents_by_trade_id(
            "source-a", "MACD", "CALL_CREDIT")

        assert [row["spread_type"] for row in puts] == ["PUT_CREDIT"]
        assert [row["spread_type"] for row in calls] == ["CALL_CREDIT"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_active_risk_positions_release_closed_group_quantity(tmp_path):
    db = Database(tmp_path / "portfolio-risk.db")
    conn = await db.open()
    repo = Repo(conn)
    try:
        await repo.insert_signal(
            "put-open", "OPEN_REQUEST", "MACD", "PUT_CREDIT", {})
        parent_id = await repo.create_parent(
            "put-open", "PUT_CREDIT", None, trade_id="source-a")
        await conn.execute(
            "UPDATE parent_trades SET state='PARTIALLY_CLOSED', "
            "sell_strike=62000, buy_strike=60000, net_credit_btc=0.004, "
            "qty_total=4 WHERE id=?", (parent_id,))
        await conn.execute(
            "INSERT INTO trade_groups(parent_id, group_index, qty, state, "
            "closed_qty) VALUES(?,?,?,?,?),(?,?,?,?,?)",
            (parent_id, 1, 2.0, "CLOSED", 2.0,
             parent_id, 2, 2.0, "OPEN", 0.5))
        await conn.commit()

        rows = await repo.active_risk_positions()

        assert len(rows) == 1
        assert rows[0]["remaining_qty"] == pytest.approx(1.5)
    finally:
        await db.close()
