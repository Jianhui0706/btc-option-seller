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
                       "OPENING", "OPEN", "PARTIALLY_CLOSED",
                       "SETTLEMENT_PENDING", "CLOSING")


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
                                reason: Optional[str] = None,
                                detail: Optional[str] = None) -> None:
        await self.conn.execute(
            "UPDATE signals SET status=?, reject_reason=?, reject_detail=? "
            "WHERE signal_id=?",
            (status, reason, detail, signal_id))
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

    async def recent_pine_heartbeats(
        self,
        limit: int = 100,
        *,
        strategy: Optional[str] = None,
    ) -> list[dict]:
        safe_limit = max(1, min(int(limit), 500))
        where = ""
        params: tuple = (safe_limit,)
        if strategy:
            where = "WHERE strategy=? "
            params = (strategy, safe_limit)
        cur = await self.conn.execute(
            "SELECT id, received_at, bar_ts, strategy, payload_json "
            f"FROM pine_heartbeats {where}"
            "ORDER BY received_at DESC, id DESC LIMIT ?",
            params)
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
                            dvol_tier: Optional[int],
                            trade_id: Optional[str] = None) -> int:
        cur = await self.conn.execute(
            "INSERT INTO parent_trades(signal_id, trade_id, spread_type, state, "
            "dvol_tier, created_at) VALUES(?,?,?,?,?,?)",
            (signal_id, trade_id, spread_type, "SIGNAL_RECEIVED", dvol_tier,
             now_ms()))
        await self.conn.commit()
        return cur.lastrowid

    async def get_parent(self, parent_id: int) -> Optional[dict]:
        cur = await self.conn.execute(
            "SELECT * FROM parent_trades WHERE id=?", (parent_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def parent_by_signal_id(self, signal_id: str) -> Optional[dict]:
        cur = await self.conn.execute(
            "SELECT * FROM parent_trades WHERE signal_id=? ORDER BY id DESC LIMIT 1",
            (signal_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def active_parents(self) -> list[dict]:
        q = ",".join("?" * len(ACTIVE_TRADE_STATES))
        cur = await self.conn.execute(
            f"SELECT p.*, s.strategy FROM parent_trades p "
            "LEFT JOIN signals s ON s.signal_id=p.signal_id "
            f"WHERE p.state IN ({q}) ORDER BY p.id",
            ACTIVE_TRADE_STATES)
        return [dict(r) for r in await cur.fetchall()]

    async def open_parents(self) -> list[dict]:
        cur = await self.conn.execute(
            "SELECT * FROM parent_trades WHERE state IN ('OPEN','PARTIALLY_CLOSED') "
            "ORDER BY id")
        return [dict(r) for r in await cur.fetchall()]

    async def residual_parents(self) -> list[dict]:
        """Long-only leftovers kept visible but excluded from new-trade locks."""
        cur = await self.conn.execute(
            "SELECT p.*, s.strategy FROM parent_trades p "
            "LEFT JOIN signals s ON s.signal_id=p.signal_id "
            "WHERE p.state='RESIDUAL_LONG' ORDER BY p.id")
        return [dict(r) for r in await cur.fetchall()]

    async def settlement_parents(self) -> list[dict]:
        """All parents that may need the expiry settlement state machine."""
        cur = await self.conn.execute(
            "SELECT * FROM parent_trades WHERE state IN "
            "('OPEN','PARTIALLY_CLOSED','SETTLEMENT_PENDING','RESIDUAL_LONG') "
            "ORDER BY id")
        return [dict(r) for r in await cur.fetchall()]

    async def active_risk_positions(self) -> list[dict]:
        """Return stable spreads with their not-yet-closed contract quantity."""
        cur = await self.conn.execute(
            "SELECT p.id, p.spread_type, p.sell_strike, p.buy_strike, "
            "p.net_credit_btc, CASE WHEN EXISTS ("
            "SELECT 1 FROM trade_groups gx WHERE gx.parent_id=p.id"
            ") THEN COALESCE((SELECT SUM(CASE WHEN g.qty > g.closed_qty "
            "THEN g.qty - g.closed_qty ELSE 0 END) FROM trade_groups g "
            "WHERE g.parent_id=p.id), 0) ELSE COALESCE(p.qty_total, 0) END "
            "AS remaining_qty FROM parent_trades p "
            "WHERE p.state IN ('OPEN','PARTIALLY_CLOSED') ORDER BY p.id")
        return [dict(row) for row in await cur.fetchall()]

    async def recent_terminal_parents(self, limit: int = 50) -> list[dict]:
        """Recent completed trades used to attribute exchange-side late fills."""
        safe_limit = max(1, min(int(limit), 200))
        cur = await self.conn.execute(
            "SELECT * FROM parent_trades "
            "WHERE state IN ('CLOSED','FORCE_CLOSED') "
            "AND closed_at IS NOT NULL "
            "AND sell_instrument IS NOT NULL AND buy_instrument IS NOT NULL "
            "ORDER BY closed_at DESC, id DESC LIMIT ?",
            (safe_limit,))
        return [dict(r) for r in await cur.fetchall()]

    async def active_parents_by_trade_id(
            self, trade_id: str, strategy: str = "",
            spread_type: str = "") -> list[dict]:
        """按信号源定位活跃交易，可进一步限定 PUT/CALL 方向。"""
        q = ",".join("?" * len(ACTIVE_TRADE_STATES))
        spread_sql = " AND p.spread_type=?" if spread_type else ""
        spread_args = (spread_type,) if spread_type else ()
        if strategy:
            cur = await self.conn.execute(
                f"SELECT p.* FROM parent_trades p "
                "JOIN signals s ON s.signal_id=p.signal_id "
                f"WHERE p.trade_id=? AND s.strategy=? AND p.state IN ({q})"
                f"{spread_sql} ORDER BY p.id DESC",
                (trade_id, strategy, *ACTIVE_TRADE_STATES, *spread_args))
        else:
            cur = await self.conn.execute(
                f"SELECT p.* FROM parent_trades p "
                f"WHERE p.trade_id=? AND p.state IN ({q})"
                f"{spread_sql} ORDER BY p.id DESC",
                (trade_id, *ACTIVE_TRADE_STATES, *spread_args))
        return [dict(row) for row in await cur.fetchall()]

    async def active_parent_by_trade_id(
            self, trade_id: str, strategy: str = "",
            spread_type: str = "") -> Optional[dict]:
        """兼容单仓调用；多仓调用应使用 active_parents_by_trade_id。"""
        rows = await self.active_parents_by_trade_id(
            trade_id, strategy, spread_type)
        return rows[0] if rows else None

    async def record_execution_attempts(self, parent_id: int, attempts: list[dict]) -> None:
        for a in attempts or []:
            await self.conn.execute(
                "INSERT INTO execution_attempts(parent_id, phase, operation, "
                "instrument, direction, liquidity_role, attempt_no, round_no, "
                "requested_qty, filled_qty, limit_price, avg_fill_price, status, "
                "placed_at, finished_at, duration_ms, group_index, group_count, "
                "cycle_id, cycle_attempt_no, cycle_max_attempts, recorded_at, "
                "exchange_order_id, details_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (parent_id, a.get("phase", "open"),
                 a.get("operation", "single_leg"), a.get("instrument"),
                 a.get("direction"), a.get("liquidity_role"),
                 a.get("attempt_no"), a.get("round"), a.get("requested_qty"),
                 a.get("filled_qty", 0), a.get("limit_price"),
                 a.get("avg_fill_price"), a.get("status"), a.get("placed_at"),
                 a.get("finished_at"), a.get("duration_ms"),
                 a.get("group_index"), a.get("group_count"),
                 a.get("hedge_cycle_id") or a.get("cycle_id"),
                 a.get("cycle_attempt_no"), a.get("cycle_max_attempts"),
                 a.get("recorded_at") or a.get("finished_at") or now_ms(),
                 a.get("exchange_order_id"),
                 json.dumps(a, ensure_ascii=False)))
        await self.conn.commit()

    async def execution_attempts(self, parent_id: int) -> list[dict]:
        cur = await self.conn.execute("SELECT * FROM execution_attempts WHERE parent_id=? ORDER BY id", (parent_id,))
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
        if to_state in ("CLOSED", "FORCE_CLOSED", "SETTLED"):
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

    async def set_settlement_price(self, parent_id: int, price: float) -> None:
        await self.conn.execute(
            "UPDATE parent_trades SET settlement_price=? WHERE id=?",
            (price, parent_id))
        await self.conn.commit()

    async def set_liquidity_role(
            self, parent_id: int, phase: str, role: str) -> None:
        """记录真实成交角色；分批成交出现不同角色时汇总为 MIXED。"""
        if phase not in ("open", "close"):
            raise ValueError(f"unsupported liquidity phase: {phase}")
        if role not in ("MAKER", "TAKER", "MIXED"):
            return
        column = f"{phase}_liquidity_role"
        row = await (await self.conn.execute(
            f"SELECT {column} FROM parent_trades WHERE id=?", (parent_id,)
        )).fetchone()
        if row is None:
            return
        current = row[column]
        merged = (
            role if current not in ("MAKER", "TAKER", "MIXED")
            else current if current == role
            else "MIXED"
        )
        await self.conn.execute(
            f"UPDATE parent_trades SET {column}=? WHERE id=?",
            (merged, parent_id))
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
        if to_state in ("CLOSED", "FORCE_CLOSED", "SETTLED"):
            sets.append("closed_at=?")
            args.append(now_ms())
            sets.append("unrealized_pnl_btc=0")
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
            "UPDATE legs SET closed_qty = MIN(qty, closed_qty + ?) "
            "WHERE parent_id=? AND role=?",
            (qty, parent_id, role))
        await self.conn.commit()

    async def mark_leg_closed(self, parent_id: int, role: str) -> None:
        await self.conn.execute(
            "UPDATE legs SET closed_qty=qty WHERE parent_id=? AND role=?",
            (parent_id, role))
        await self.conn.commit()

    async def close_cashflows(self, parent_id: int) -> tuple[float, float]:
        cur = await self.conn.execute(
            "SELECT direction, SUM(qty * COALESCE(price, 0)) AS total "
            "FROM fills WHERE parent_id=? AND phase='close' "
            "GROUP BY direction",
            (parent_id,))
        totals = {
            row["direction"]: float(row["total"] or 0.0)
            for row in await cur.fetchall()
        }
        return totals.get("buy", 0.0), totals.get("sell", 0.0)

    async def close_fees(self, parent_id: int) -> float:
        row = await (await self.conn.execute(
            "SELECT COALESCE(SUM(ABS(COALESCE(fee, 0))), 0) "
            "FROM fills WHERE parent_id=? AND phase='close'",
            (parent_id,)
        )).fetchone()
        return float(row[0] or 0.0)

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

    async def set_hedge_manual_hold(self, parent_id: int, enabled: bool) -> None:
        await self.conn.execute(
            "UPDATE hedge_state SET manual_hold=? WHERE parent_id=?",
            (1 if enabled else 0, parent_id))
        await self.conn.commit()

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
            sets.append("release_confirm_count=0")
        args.append(parent_id)
        await self.conn.execute(
            f"UPDATE hedge_state SET {', '.join(sets)} WHERE parent_id=?", args)
        await ev.emit(self.conn, event_code, parent_id=parent_id,
                      from_state=row["state"], to_state=to_state, **extra)

    async def set_release_pending_bar(self, parent_id: int, bar_ts: int) -> None:
        await self.conn.execute(
            "UPDATE hedge_state SET release_pending_bar_ts=?, "
            "release_confirm_count=0 WHERE parent_id=?",
            (bar_ts, parent_id))
        await self.conn.commit()

    async def increment_release_confirmation(self, parent_id: int) -> int:
        await self.conn.execute(
            "UPDATE hedge_state SET release_confirm_count="
            "COALESCE(release_confirm_count, 0)+1 WHERE parent_id=?",
            (parent_id,))
        cur = await self.conn.execute(
            "SELECT release_confirm_count FROM hedge_state WHERE parent_id=?",
            (parent_id,))
        row = await cur.fetchone()
        await self.conn.commit()
        return int(row["release_confirm_count"] or 0) if row else 0

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

    async def record_hedge_check(
            self, parent_id: int, *, count: int, checked_at: int,
            price: float, source: str, trigger: float, progress: float,
            offset: float, touched: bool) -> int:
        """持久化对冲门槛比较的汇总状态，返回累计检查次数。

        实时订阅会先在内存中合并多个 tick，再批量调用此方法，避免每个行情
        更新都写一次 SQLite；3 分钟兜底扫描则直接记一条。
        """
        increment = max(1, int(count))
        await self.conn.execute(
            "UPDATE hedge_state SET check_count=COALESCE(check_count, 0)+?, "
            "last_check_at=?, last_check_price=?, last_check_source=?, "
            "last_check_trigger=?, last_check_progress=?, last_check_offset=?, "
            "last_check_touched=? WHERE parent_id=?",
            (increment, checked_at, price, source, trigger, progress, offset,
             1 if touched else 0, parent_id))
        cur = await self.conn.execute(
            "SELECT check_count FROM hedge_state WHERE parent_id=?", (parent_id,))
        row = await cur.fetchone()
        await self.conn.commit()
        return int(row["check_count"] or 0) if row else 0

    async def recent_hedge_events(self, parent_id: int,
                                  limit: int = 8) -> list[dict]:
        """返回当前父交易最近的对冲检查、激活、调仓和撤销事件。"""
        codes = (
            ev.HEDGE_CHECKPOINT,
            ev.BEP_HEDGE_ACTIVATED,
            ev.DELTA_HEDGE_REBALANCE,
            ev.HEDGE_RELEASE_PENDING,
            ev.HEDGE_RELEASE_CANCELLED,
            ev.HEDGE_TARGET_ZERO,
            ev.HEDGE_SKIPPED,
        )
        marks = ",".join("?" * len(codes))
        cur = await self.conn.execute(
            f"SELECT * FROM events WHERE parent_id=? AND code IN ({marks}) "
            "ORDER BY id DESC LIMIT ?",
            (parent_id, *codes, max(1, min(int(limit), 30))))
        return [dict(row) for row in await cur.fetchall()]

    async def record_hedge_action(self, *, parent_id: Optional[int], action: str,
                                  book_key: Optional[str] = None,
                                  net_delta_before: Optional[float] = None,
                                  net_delta_after: Optional[float] = None,
                                  order_usd: Optional[float] = None,
                                  requested_usd: Optional[float] = None,
                                  managed_usd_after: Optional[float] = None,
                                  cycle_id: Optional[str] = None,
                                  attempt_no: Optional[int] = None,
                                  max_attempts: Optional[int] = None,
                                  residual_delta: Optional[float] = None,
                                  fill_price: Optional[float] = None,
                                  fee: Optional[float] = None,
                                  skipped_reason: Optional[str] = None,
                                  details: Optional[dict] = None) -> None:
        await self.conn.execute(
            "INSERT INTO hedge_actions(parent_id, book_key, ts, action, net_delta_before, "
            "net_delta_after, order_usd, requested_usd, managed_usd_after, "
            "cycle_id, attempt_no, max_attempts, residual_delta, fill_price, fee, "
            "skipped_reason, details_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (parent_id, book_key, now_ms(), action, net_delta_before, net_delta_after,
             order_usd, requested_usd, managed_usd_after, cycle_id, attempt_no,
             max_attempts, residual_delta, fill_price, fee, skipped_reason,
             json.dumps(details, ensure_ascii=False) if details else None))
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
