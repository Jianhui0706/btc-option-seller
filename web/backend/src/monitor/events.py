"""内部事件码（规范第十七节）与事件落库。"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

log = logging.getLogger("v2.events")

# --- 拒绝类 ---
REJECT_EXISTING_OPTION_SHORT = "REJECT_EXISTING_OPTION_SHORT"
REJECT_DUPLICATE_SIGNAL = "REJECT_DUPLICATE_SIGNAL"
REJECT_NO_VALID_LEGS = "REJECT_NO_VALID_LEGS"
REJECT_INSUFFICIENT_RISK_BUDGET = "REJECT_INSUFFICIENT_RISK_BUDGET"
REJECT_BAD_PAYLOAD = "REJECT_BAD_PAYLOAD"
REJECT_TRADING_DISABLED = "REJECT_TRADING_DISABLED"
REJECT_DVOL_BELOW_ENTRY = "REJECT_DVOL_BELOW_ENTRY"
DVOL_TIER_MISMATCH = "DVOL_TIER_MISMATCH"
# --- 开仓/对冲/平仓 ---
OPEN_FAILED_UNWOUND = "OPEN_FAILED_UNWOUND"
OPENED = "OPENED"
BEP_HEDGE_ACTIVATED = "BEP_HEDGE_ACTIVATED"
DELTA_HEDGE_REBALANCE = "DELTA_HEDGE_REBALANCE"
HEDGE_RELEASE_PENDING = "HEDGE_RELEASE_PENDING"
HEDGE_RELEASE_CANCELLED = "HEDGE_RELEASE_CANCELLED"
HEDGE_TARGET_ZERO = "HEDGE_TARGET_ZERO"
HEDGE_SKIPPED = "HEDGE_SKIPPED"
EXIT_DVOL_ROLLING_30 = "EXIT_DVOL_ROLLING_30"
# 推送看门狗：Pine 自报某根 2h bar 满足全部开仓条件，却没收到对应 OPEN_REQUEST 推送
PUSH_MISSING = "PUSH_MISSING"
NORMAL_CLOSE = "NORMAL_CLOSE"
FORCE_CLOSE_RISK = "FORCE_CLOSE_RISK"
FORCE_CLOSE_MANUAL = "FORCE_CLOSE_MANUAL"
# --- 系统 ---
RECONCILE_MISMATCH = "RECONCILE_MISMATCH"
SCAN_ERROR = "SCAN_ERROR"
STATE_TRANSITION = "STATE_TRANSITION"


async def emit(conn, code: str, *, level: str = "INFO",
               parent_id: Optional[int] = None, group_id: Optional[int] = None,
               **payload: Any) -> None:
    """写入 events 表并打日志。conn 为 aiosqlite 连接。"""
    getattr(log, level.lower(), log.info)(
        "%s parent=%s %s", code, parent_id,
        json.dumps(payload, ensure_ascii=False, default=str) if payload else "")
    await conn.execute(
        "INSERT INTO events(ts, level, code, parent_id, group_id, payload_json) "
        "VALUES(?,?,?,?,?,?)",
        (int(time.time() * 1000), level, code, parent_id, group_id,
         json.dumps(payload, ensure_ascii=False, default=str) if payload else None))
    await conn.commit()
