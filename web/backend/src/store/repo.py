"""DAO：信号去重、父交易/子组/腿/成交、对冲状态、系统状态。

状态迁移统一走 transition_*()，同事务内校验合法性 + 更新 + 写事件。
单 worker 串行写，SQLite WAL。
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

import aiosqlite

from ..engine.states import check_transition
from ..monitor import events as ev

ACTIVE_TRADE_STATES = ("SIGNAL_RECEIVED", "POSITION_CHECKING", "SELECTING_LEGS",
                       "OPENING", "OPEN", "PARTIALLY_CLOSED", "CLOSING")


def now_ms() -> int:
    return int(time.time() * 1000)


class Repo:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    # ------------------------------------------------------------ signals

    async def insert_signal(self, signal_id: str, event: str, strategy: str,
                            spread_type: str, payload: dict) -> bool:
        """INSERT OR IGNORE 去重。返回 True=新信号，False=重复。"""
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO signals(signal_id, event, strategy, spread_type, "
            "payload_json, received_at) VALUES(?,?,?,?,?,?)",
            (signal_id, event, strategy, spread_type,
             json.dumps(payload, ensure_ascii=False), now_ms()))
        await self.conn.commit()
        return cur.rowcount > 0

    async def set_signal_status(self, signal_id: str, status: str,
                                reason: Optional[str] = None) -> None:
        await self.conn.execute(
            "UPDATE signals SET status=?, reject_reason=? WHERE signal_id=?",
            (status, reason, signal_id))
        await self.conn.commit()

    # ------------------------------------------------------------ Pine heartbeats

    async def record_pine_heartbeat(self, payload: dict,
                                    retention: int = 500) -> bool:
        safe_payload = dict(payload)
        safe_payload.pop("passphrase", None)
        received_value = safe_payload.get("received_at")
        received_at = now_ms() if received_value is None else int(received_value)
        safe_payload["received_at"] = received_at
        payload_json = json.dumps(safe_payload, ensure_ascii=False)
        keep = max(1, int(retention))

        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO pine_heartbeats("
            "received_at, bar_ts, strategy, payload_json) "
            "VALUES(?,?,?,?)",
            (received_at, safe_payload.get("bar_ts"),
             safe_payload.get("strategy"), payload_json))
        fresh = cur.rowcount > 0
        if fresh:
            await self.conn.execute(
                "INSERT INTO system_state(key, value) VALUES('pine_status', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (payload_json,))
            await self.conn.execute(
                "DELETE FROM pine_heartbeats WHERE id NOT IN ("
                "SELECT id FROM pine_heartbeats "
                "ORDER BY received_at DESC, id DESC LIMIT ?)",
                (keep,))
        await self.conn.commit()
        return fresh

    async def recent_pine_heartbeats(self, limit: int = 100) -> list[dict]:
        safe_limit = max(1, min(int(limit), 500))
        cur = await self.conn.execute(
            "SELECT id, received_at, bar_ts, strategy, payload_json "
            "FROM pine_heartbeats ORDER BY received_at DESC, id DESC LIMIT ?",
            (safe_limit,))
        result = []
        for row in await cur.fetchall():
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            payload.pop("passphrase", None)
            result.append({
                "id": row["id"],
                "received_at": row["received_at"],
                "bar_ts": row["bar_ts"],
                "strategy": row["strategy"],
                "payload": payload,
            })
        return result

    # ------------------------------------------------------------ parent trades

    async def create_parent(self, signal_id: str, spread_type: str,
                            dvol_tier: Optional[int]) -> int:
        cur = await self.conn.execute(
            "INSERT INTO parent_trades(signal_id, spread_type, state, dvol_tier, "
            "created_at) VALUES(?,?,?,?,?)",
            (signal_id, spread_type, "SIGNAL_RECEIVED", dvol_tier, now_ms()))
        await self.conn.commit()
        return cur.lastrowid

    async def get_parent(self, parent_id: int) -> Optional[dict]:
        cur = await self.conn.execute(
            "SELECT * FROM parent_trades WHERE id=?", (parent_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def active_parents(self) -> list[dict]:
        q = ",".join("?" * len(ACTIVE_TRADE_STATES))
        cur = await self.conn.execute(
            f"SELECT * FROM parent_trades WHERE state IN ({q}) ORDER BY id",
            ACTIVE_TRADE_STATES)
        return [dict(r) for r in await cur.fetchall()]

    async def open_parents(self) -> list[dict]:
        cur = await self.conn.execute(
            "SELECT * FROM parent_trades WHERE state IN ('OPEN','PARTIALLY_CLOSED') "
            "ORDER BY id")
        return [dict(r) for r in await cur.fetchall()]

    async def transition_parent(self, parent_id: int, to_state: str,
                                event_code: str = ev.STATE_TRANSITION,
                                **extra: Any) -> None:
        cur = await self.conn.execute(
            "SELECT state FROM parent_trades WHERE id=?", (parent_id,))
        row = await cur.fetchone()
        assert row is not None, f"parent {parent_id} 不存在"
        check_transition("trade", row["state"], to_state)
        sets = "state=?"
        args: list[Any] = [to_state]
        if to_state in ("CLOSED", "FORCE_CLOSED"):
            sets += ", closed_at=?"
            args.append(now_ms())
        args.append(parent_id)
        await self.conn.execute(
            f"UPDATE parent_trades SET {sets} WHERE id=? AND state=?",
            args + [row["state"]])
        await ev.emit(self.conn, event_code, parent_id=parent_id,
                      from_state=row["state"], to_state=to_state, **extra)

    async def set_instruments(self, parent_id: int, *, sell_instrument: str,
                              buy_instrument: str, sell_strike: float,
                              buy_strike: float, expiry_ts: int) -> None:
        """开仓执行前先落库合约信息——进程中断后 reconcile 才能认领交易所持仓。"""
        await self.conn.execute(
            "UPDATE parent_trades SET sell_instrument=?, buy_instrument=?, "
            "sell_strike=?, buy_strike=?, expiry_ts=? WHERE id=?",
            (sell_instrument, buy_instrument, sell_strike, buy_strike, expiry_ts,
             parent_id))
        await self.conn.commit()

    async def get_signal_payload(self, signal_id: str) -> Optional[dict]:
        cur = await self.conn.execute(
            "SELECT payload_json FROM signals WHERE signal_id=?", (signal_id,))
        row = await cur.fetchone()
        return json.loads(row["payload_json"]) if row else None

    async def fill_open_details(self, parent_id: int, *, sell_instrument: str,
                                buy_instrument: str, expiry_ts: int,
                                sell_strike: float, buy_strike: float,
                                qty_total: float, net_credit_btc: float,
                                open_index_price: float, bep_price: float) -> None:
        await self.conn.execute(
            "UPDATE parent_trades SET sell_instrument=?, buy_instrument=?, expiry_ts=?, "
            "sell_strike=?, buy_strike=?, qty_total=?, net_credit_btc=?, "
            "open_index_price=?, bep_price=?, opened_at=? WHERE id=?",
            (sell_instrument, buy_instrument, expiry_ts, sell_strike, buy_strike,
             qty_total, net_credit_btc, open_index_price, bep_price, now_ms(),
             parent_id))
        await self.conn.commit()

    async def set_close_reason(self, parent_id: int, reason: str) -> None:
        await self.conn.execute(
            "UPDATE parent_trades SET close_reason=? WHERE id=?", (reason, parent_id))
        await self.conn.commit()

    # ------------------------------------------------------------ groups / legs / fills

    async def create_groups(self, parent_id: int, quantities: list[float]) -> list[int]:
        ids = []
        for i, q in enumerate(quantities, start=1):
            cur = await self.conn.execute(
                "INSERT INTO trade_groups(parent_id, group_index, qty, state) "
                "VALUES(?,?,?,'OPEN')", (parent_id, i, q))
            ids.append(cur.lastrowid)
        await self.conn.commit()
        return ids

    async def groups_of(self, parent_id: int) -> list[dict]:
        cur = await self.conn.execute(
            "SELECT * FROM trade_groups WHERE parent_id=? ORDER BY group_index",
            (parent_id,))
        return [dict(r) for r in await cur.fetchall()]

    async def get_group(self, group_id: int) -> Optional[dict]:
        cur = await self.conn.execute(
            "SELECT * FROM trade_groups WHERE id=?", (group_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def transition_group(self, group_id: int, to_state: str,
                               *, closed_qty: Optional[float] = None,
                               realized_pnl: Optional[float] = None,
                               reason: Optional[str] = None) -> None:
        cur = await self.conn.execute(
            "SELECT state, parent_id FROM trade_groups WHERE id=?", (group_id,))
        row = await cur.fetchone()
        assert row is not None
        check_transition("group", row["state"], to_state)
        sets, args = ["state=?"], [to_state]
        if closed_qty is not None:
            sets.append("closed_qty=?")
            args.append(closed_qty)
        if realized_pnl is not None:
            sets.append("realized_pnl_btc=?")
            args.append(realized_pnl)
        if reason is not None:
            sets.append("close_reason=?")
            args.append(reason)
        if to_state in ("CLOSED", "FORCE_CLOSED"):
            sets.append("closed_at=?")
            args.append(now_ms())
        args.append(group_id)
        await self.conn.execute(
            f"UPDATE trade_groups SET {', '.join(sets)} WHERE id=?", args)
        await ev.emit(self.conn, ev.STATE_TRANSITION, parent_id=row["parent_id"],
                      group_id=group_id, from_state=row["state"], to_state=to_state)

    async def update_group_mark(self, group_id: int, unrealized_pnl: float,
                                profit_ratio: Optional[float]) -> None:
        await self.conn.execute(
            "UPDATE trade_groups SET unrealized_pnl_btc=?, profit_ratio=? WHERE id=?",
            (unrealized_pnl, profit_ratio, group_id))
        await self.conn.commit()

    async def create_legs(self, parent_id: int, sell_instrument: str, sell_qty: float,
                          sell_avg: float, buy_instrument: str, buy_qty: float,
                          buy_avg: float) -> None:
        await self.conn.execute(
            "INSERT INTO legs(parent_id, role, instrument, qty, avg_price) "
            "VALUES(?,?,?,?,?)",
            (parent_id, "SELL_LEG", sell_instrument, sell_qty, sell_avg))
        await self.conn.execute(
            "INSERT INTO legs(parent_id, role, instrument, qty, avg_price) "
            "VALUES(?,?,?,?,?)",
            (parent_id, "BUY_LEG", buy_instrument, buy_qty, buy_avg))
        await self.conn.commit()

    async def add_leg_closed(self, parent_id: int, role: str, qty: float) -> None:
        await self.conn.execute(
            "UPDATE legs SET closed_qty = closed_qty + ? WHERE parent_id=? AND role=?",
            (qty, parent_id, role))
        await self.conn.commit()

    async def record_fill(self, *, parent_id: Optional[int], group_id: Optional[int],
                          instrument: str, direction: str, qty: float,
                          price: Optional[float], fee: float, phase: str) -> None:
        await self.conn.execute(
            "INSERT INTO fills(parent_id, group_id, instrument, direction, qty, price, "
            "fee, phase, ts) VALUES(?,?,?,?,?,?,?,?,?)",
            (parent_id, group_id, instrument, direction, qty, price, fee, phase,
             now_ms()))
        await self.conn.commit()

    # ------------------------------------------------------------ hedge

    async def ensure_hedge_row(self, parent_id: int) -> None:
        await self.conn.execute(
            "INSERT OR IGNORE INTO hedge_state(parent_id) VALUES(?)", (parent_id,))
        await self.conn.commit()

    async def get_hedge(self, parent_id: int) -> Optional[dict]:
        cur = await self.conn.execute(
            "SELECT * FROM hedge_state WHERE parent_id=?", (parent_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def transition_hedge(self, parent_id: int, to_state: str,
                               event_code: str = ev.STATE_TRANSITION,
                               **extra: Any) -> None:
        cur = await self.conn.execute(
            "SELECT state FROM hedge_state WHERE parent_id=?", (parent_id,))
        row = await cur.fetchone()
        assert row is not None
        check_transition("hedge", row["state"], to_state)
        sets, args = ["state=?"], [to_state]
        if to_state == "HEDGE_ACTIVE" and row["state"] == "HEDGE_INACTIVE":
            sets.append("activated_at=?")
            args.append(now_ms())
        if to_state != "HEDGE_RELEASE_PENDING":
            sets.append("release_pending_bar_ts=NULL")
        args.append(parent_id)
        await self.conn.execute(
            f"UPDATE hedge_state SET {', '.join(sets)} WHERE parent_id=?", args)
        await ev.emit(self.conn, event_code, parent_id=parent_id,
                      from_state=row["state"], to_state=to_state, **extra)

    async def set_release_pending_bar(self, parent_id: int, bar_ts: int) -> None:
        await self.conn.execute(
            "UPDATE hedge_state SET release_pending_bar_ts=? WHERE parent_id=?",
            (bar_ts, parent_id))
        await self.conn.commit()

    async def set_hedge_futures(self, parent_id: int, futures_usd: float,
                                futures_delta_btc: float) -> None:
        await self.conn.execute(
            "UPDATE hedge_state SET futures_usd=?, futures_delta_btc=?, "
            "last_rebalance_at=? WHERE parent_id=?",
            (futures_usd, futures_delta_btc, now_ms(), parent_id))
        await self.conn.commit()

    async def adjust_hedge_futures(self, parent_id: int, delta_usd: float,
                                   index_price: float) -> tuple[float, float]:
        """累计本策略实际下出的期货成交，不把账户已有期货误认成本策略对冲。"""
        row = await self.get_hedge(parent_id)
        current = float((row or {}).get("futures_usd") or 0.0)
        updated = round(current + delta_usd, 10)
        delta_btc = updated / index_price if index_price > 0 else 0.0
        await self.set_hedge_futures(parent_id, updated, delta_btc)
        return updated, delta_btc

    async def record_hedge_action(self, *, parent_id: Optional[int], action: str,
                                  net_delta_before: Optional[float] = None,
                                  net_delta_after: Optional[float] = None,
                                  order_usd: Optional[float] = None,
                                  fill_price: Optional[float] = None,
                                  fee: Optional[float] = None,
                                  skipped_reason: Optional[str] = None) -> None:
        await self.conn.execute(
            "INSERT INTO hedge_actions(parent_id, ts, action, net_delta_before, "
            "net_delta_after, order_usd, fill_price, fee, skipped_reason) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (parent_id, now_ms(), action, net_delta_before, net_delta_after,
             order_usd, fill_price, fee, skipped_reason))
        await self.conn.commit()

    # ------------------------------------------------------------ system state

    async def get_state(self, key: str, default: Optional[str] = None) -> Optional[str]:
        cur = await self.conn.execute(
            "SELECT value FROM system_state WHERE key=?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else default

    async def set_state(self, key: str, value: str) -> None:
        await self.conn.execute(
            "INSERT INTO system_state(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        await self.conn.commit()

    async def get_flag(self, key: str, default: bool = False) -> bool:
        v = await self.get_state(key)
        return default if v is None else v == "1"

    async def set_flag(self, key: str, value: bool) -> None:
        await self.set_state(key, "1" if value else "0")
