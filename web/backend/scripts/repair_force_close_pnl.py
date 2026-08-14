"""Conservatively backfill legacy FORCE_CLOSED option PnL.

Old account-level force closes stored option fills with parent_id=NULL and then
marked groups FORCE_CLOSED without realized PnL.  This tool only repairs a
trade when its terminal time window contains attributable fills for both of
its option instruments and their quantities cover the recorded trade size.

Dry-run is the default.  Use --apply to create a timestamped database backup
and commit the repair.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


EPS = 1e-9


def candidate(conn: sqlite3.Connection, parent: sqlite3.Row) -> dict | None:
    start = int(parent["opened_at"] or parent["created_at"] or 0)
    end = int(parent["closed_at"] or 0) + 5000
    if not start or not end or not parent["sell_instrument"] or not parent["buy_instrument"]:
        return None
    fills = conn.execute(
        "SELECT * FROM fills WHERE parent_id IS NULL AND phase='close' "
        "AND ts BETWEEN ? AND ? AND instrument IN (?, ?) ORDER BY ts, id",
        (start, end, parent["sell_instrument"], parent["buy_instrument"]),
    ).fetchall()
    sell_fills = [row for row in fills if row["instrument"] == parent["sell_instrument"]
                  and row["direction"] == "buy"]
    buy_fills = [row for row in fills if row["instrument"] == parent["buy_instrument"]
                 and row["direction"] == "sell"]
    qty = float(parent["qty_total"] or 0.0)
    sell_qty = sum(float(row["qty"] or 0.0) for row in sell_fills)
    buy_qty = sum(float(row["qty"] or 0.0) for row in buy_fills)
    if (qty <= EPS or abs(sell_qty - qty) > EPS
            or abs(buy_qty - qty) > EPS):
        return None
    selected = sell_fills + buy_fills
    buy_cost = sum(float(row["qty"] or 0.0) * float(row["price"] or 0.0)
                   for row in sell_fills)
    sell_revenue = sum(float(row["qty"] or 0.0) * float(row["price"] or 0.0)
                       for row in buy_fills)
    fees = sum(abs(float(row["fee"] or 0.0)) for row in selected)
    pnl = float(parent["net_credit_btc"] or 0.0) * qty - buy_cost + sell_revenue - fees
    return {"fill_ids": [row["id"] for row in selected], "pnl": pnl,
            "sell_qty": sell_qty, "buy_qty": buy_qty}


def repair(db_path: Path, apply: bool) -> tuple[int, int]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    repaired = skipped = 0
    try:
        parents = conn.execute(
            "SELECT p.* FROM parent_trades p WHERE p.state='FORCE_CLOSED' "
            "AND COALESCE((SELECT SUM(ABS(g.realized_pnl_btc)) "
            "FROM trade_groups g WHERE g.parent_id=p.id), 0)=0 "
            "ORDER BY p.closed_at, p.id"
        ).fetchall()
        matches = {parent["id"]: candidate(conn, parent) for parent in parents}
        fill_owners: dict[int, list[int]] = {}
        for parent_id, match in matches.items():
            if match is None:
                continue
            for fill_id in match["fill_ids"]:
                fill_owners.setdefault(fill_id, []).append(parent_id)
        for parent in parents:
            match = matches[parent["id"]]
            if match is None:
                skipped += 1
                print(f"SKIP parent #{parent['id']}: insufficient unique close fills")
                continue
            conflicts = {
                fill_id: fill_owners[fill_id] for fill_id in match["fill_ids"]
                if len(fill_owners[fill_id]) > 1
            }
            if conflicts:
                skipped += 1
                print(f"SKIP parent #{parent['id']}: shared fill candidates {conflicts}")
                continue
            groups = conn.execute(
                "SELECT * FROM trade_groups WHERE parent_id=? ORDER BY group_index",
                (parent["id"],),
            ).fetchall()
            total_group_qty = sum(float(row["qty"] or 0.0) for row in groups)
            if total_group_qty <= EPS:
                skipped += 1
                print(f"SKIP parent #{parent['id']}: no group quantity")
                continue
            print(
                f"REPAIR parent #{parent['id']}: pnl={match['pnl']:.8f} BTC, "
                f"fills={match['fill_ids']}"
            )
            if apply:
                placeholders = ",".join("?" for _ in match["fill_ids"])
                conn.execute(
                    f"UPDATE fills SET parent_id=? WHERE id IN ({placeholders})",
                    (parent["id"], *match["fill_ids"]),
                )
                for group in groups:
                    allocated = match["pnl"] * float(group["qty"] or 0.0) / total_group_qty
                    conn.execute(
                        "UPDATE trade_groups SET realized_pnl_btc=? WHERE id=?",
                        (allocated, group["id"]),
                    )
            repaired += 1
        if apply:
            conn.commit()
        else:
            conn.rollback()
        return repaired, skipped
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("db", type=Path, help="account-scoped seller_v2_*.db path")
    parser.add_argument("--apply", action="store_true", help="backup and commit repairs")
    args = parser.parse_args()
    if not args.db.is_file():
        raise SystemExit(f"database not found: {args.db}")
    if args.apply:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = args.db.with_name(f"{args.db.name}.pre-force-pnl-{stamp}.bak")
        shutil.copy2(args.db, backup)
        print(f"BACKUP {backup}")
    repaired, skipped = repair(args.db, args.apply)
    mode = "applied" if args.apply else "dry-run"
    print(f"DONE {mode}: repairable={repaired}, skipped={skipped}")


if __name__ == "__main__":
    main()
