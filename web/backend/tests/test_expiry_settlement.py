from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.engine import scanner
from src.engine.commands import CommandQueue
from src.store.db import Database
from src.store.repo import Repo


EXPIRY = int(datetime(2026, 8, 8, 8, tzinfo=timezone.utc).timestamp() * 1000)


class DeliveryWS:
    def __init__(self, price=None):
        self.price = price

    async def request(self, method, params):
        assert method == "public/get_delivery_prices"
        assert params["index_name"] == "btc_usd"
        data = [] if self.price is None else [
            {"date": "2026-08-08", "delivery_price": self.price}
        ]
        return {"data": data}


def context(repo, price=None):
    return SimpleNamespace(
        repo=repo,
        ws=DeliveryWS(price),
        queue=CommandQueue(),
        cfg=SimpleNamespace(
            strategy=SimpleNamespace(currency="BTC"),
            hedge=SimpleNamespace(perp_contract_usd=10.0),
        ),
    )


async def insert_open_trade(repo, *, spread_type="PUT_CREDIT",
                            sell_strike=64000.0, buy_strike=63500.0,
                            credit=0.001, hedge_state="HEDGE_INACTIVE",
                            hedge_usd=0.0):
    await repo.conn.execute(
        "INSERT INTO parent_trades("
        "id, spread_type, state, sell_instrument, buy_instrument, expiry_ts, "
        "sell_strike, buy_strike, qty_total, net_credit_btc, created_at) "
        "VALUES(1,?,?,?,?,?,?,?,?,?,1)",
        (spread_type, "OPEN", f"BTC-8AUG26-{int(sell_strike)}-P",
         f"BTC-8AUG26-{int(buy_strike)}-P", EXPIRY, sell_strike,
         buy_strike, 2.0, credit),
    )
    await repo.conn.execute(
        "INSERT INTO trade_groups(parent_id, group_index, qty, state, "
        "unrealized_pnl_btc) VALUES(1,1,2,'OPEN',0.25)")
    await repo.conn.executemany(
        "INSERT INTO legs(parent_id, role, instrument, qty) VALUES(1,?,?,2)",
        [("SELL_LEG", f"BTC-8AUG26-{int(sell_strike)}-P"),
         ("BUY_LEG", f"BTC-8AUG26-{int(buy_strike)}-P")],
    )
    await repo.conn.execute(
        "INSERT INTO hedge_state(parent_id, state, futures_usd) VALUES(1,?,?)",
        (hedge_state, hedge_usd),
    )
    await repo.conn.commit()


def test_settlement_pnl_uses_btc_intrinsic_value():
    put = {
        "spread_type": "PUT_CREDIT", "sell_strike": 64000,
        "buy_strike": 63500, "net_credit_btc": 0.001,
    }
    call = {
        "spread_type": "CALL_CREDIT", "sell_strike": 65000,
        "buy_strike": 65500, "net_credit_btc": 0.001,
    }
    assert scanner.settlement_pnl_per_contract(put, 65000) == pytest.approx(0.001)
    assert scanner.settlement_pnl_per_contract(put, 63000) == pytest.approx(
        0.001 - 500 / 63000)
    assert scanner.settlement_pnl_per_contract(call, 66000) == pytest.approx(
        0.001 - 500 / 66000)


@pytest.mark.asyncio
async def test_expiry_waits_for_official_delivery_price(tmp_path):
    db = Database(tmp_path / "pending.db")
    conn = await db.open()
    try:
        repo = Repo(conn)
        await insert_open_trade(repo)
        ctx = context(repo, price=None)

        handled = await scanner._handle_expiry_settlement(
            ctx, await repo.active_parents(), [], [], EXPIRY + 1)

        assert handled is True
        parent = await repo.get_parent(1)
        group = (await repo.groups_of(1))[0]
        assert parent["state"] == "SETTLEMENT_PENDING"
        assert parent["closed_at"] is None
        assert group["state"] == "OPEN"
        assert group["unrealized_pnl_btc"] == 0
        assert await repo.active_parents()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_expiry_finalizes_without_close_fill(tmp_path):
    db = Database(tmp_path / "settled.db")
    conn = await db.open()
    try:
        repo = Repo(conn)
        await insert_open_trade(repo)
        ctx = context(repo, price=65000.0)

        handled = await scanner._handle_expiry_settlement(
            ctx, await repo.active_parents(), [], [], EXPIRY + 1)

        assert handled is True
        parent = await repo.get_parent(1)
        group = (await repo.groups_of(1))[0]
        assert parent["state"] == "SETTLED"
        assert parent["close_reason"] == "EXPIRY_SETTLEMENT"
        assert parent["settlement_price"] == 65000.0
        assert parent["close_liquidity_role"] is None
        assert group["state"] == "SETTLED"
        assert group["closed_qty"] == 2.0
        assert group["realized_pnl_btc"] == pytest.approx(0.002)
        assert not await repo.active_parents()
        fills = await (await conn.execute(
            "SELECT * FROM fills WHERE phase='close'")).fetchall()
        assert fills == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_expiry_adds_settlement_pnl_to_partial_close_pnl(tmp_path):
    db = Database(tmp_path / "partially-settled.db")
    conn = await db.open()
    try:
        repo = Repo(conn)
        await insert_open_trade(repo)
        await conn.execute(
            "UPDATE parent_trades SET state='PARTIALLY_CLOSED' WHERE id=1")
        await conn.execute(
            "UPDATE trade_groups SET closed_qty=0.5, realized_pnl_btc=0.0002 "
            "WHERE parent_id=1")
        await conn.commit()
        ctx = context(repo, price=65000.0)

        await scanner._handle_expiry_settlement(
            ctx, await repo.active_parents(), [], [], EXPIRY + 1)

        group = (await repo.groups_of(1))[0]
        assert group["state"] == "SETTLED"
        assert group["closed_qty"] == 2.0
        assert group["realized_pnl_btc"] == pytest.approx(0.0002 + 0.0015)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_expiry_releases_managed_hedge_before_settling(tmp_path):
    db = Database(tmp_path / "hedged.db")
    conn = await db.open()
    try:
        repo = Repo(conn)
        await insert_open_trade(
            repo, hedge_state="HEDGE_ACTIVE", hedge_usd=100.0)
        ctx = context(repo, price=65000.0)

        await scanner._handle_expiry_settlement(
            ctx, await repo.active_parents(), [], [], EXPIRY + 1)

        assert (await repo.get_parent(1))["state"] == "SETTLEMENT_PENDING"
        assert (await repo.get_hedge(1))["state"] == "HEDGE_RELEASE_PENDING"
        command = await ctx.queue.get()
        assert command.kind == "HEDGE_RELEASE"
        assert command.payload["parent_id"] == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_before_expiry_does_not_start_settlement(tmp_path):
    db = Database(tmp_path / "before.db")
    conn = await db.open()
    try:
        repo = Repo(conn)
        await insert_open_trade(repo)
        ctx = context(repo, price=65000.0)

        handled = await scanner._handle_expiry_settlement(
            ctx, await repo.active_parents(), [], [], EXPIRY - 1)

        assert handled is False
        assert (await repo.get_parent(1))["state"] == "OPEN"
        assert ctx.queue._q.empty()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_quarantined_long_does_not_block_entries_and_still_settles(
        tmp_path):
    db = Database(tmp_path / "residual-long.db")
    conn = await db.open()
    try:
        repo = Repo(conn)
        await insert_open_trade(repo, spread_type="CALL_CREDIT",
                                sell_strike=65000.0, buy_strike=65500.0)
        await conn.execute(
            "UPDATE parent_trades SET state='RESIDUAL_LONG', "
            "close_reason='RESIDUAL_LONG_AWAITING_FILL_OR_EXPIRY' WHERE id=1")
        await conn.commit()

        assert await repo.active_parents() == []
        assert [row["id"] for row in await repo.residual_parents()] == [1]
        assert [row["id"] for row in await repo.settlement_parents()] == [1]

        handled = await scanner._handle_expiry_settlement(
            context(repo, price=65000.0),
            await repo.settlement_parents(), [], [], EXPIRY + 1)

        assert handled is True
        assert (await repo.get_parent(1))["state"] == "SETTLED"
        assert not await repo.residual_parents()
    finally:
        await db.close()
