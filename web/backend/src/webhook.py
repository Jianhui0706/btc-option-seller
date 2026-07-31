"""TradingView Webhook 接收与命令标准化。

TradingView 是开仓/平仓策略的唯一信号源。该模块只负责鉴权、严格校验、
幂等去重、把 TradingView 消息标准化为执行命令并立即入队；真实交易不在
HTTP 请求协程内执行，避免 TradingView 超时重试造成重复订单。
"""

from __future__ import annotations

import hmac
import logging
import time
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .context import Ctx
from .monitor import events as ev

log = logging.getLogger("v2.webhook")

router = APIRouter()

OPEN_EVENTS = {"OPEN_REQUEST"}
EXIT_EVENTS = {"EXIT_DVOL", "EXIT_DVOL_ROLLING_30", "CLOSE_REQUEST"}
ACTION_ALIASES = {
    "OPEN": "OPEN",
    "OPEN_REQUEST": "OPEN",
    "OPEN_LONG": "OPEN_LONG",
    "OPEN_SHORT": "OPEN_SHORT",
    "CLOSE": "CLOSE",
    "CLOSE_REQUEST": "CLOSE",
    "CLOSE_LONG": "CLOSE_LONG",
    "CLOSE_SHORT": "CLOSE_SHORT",
    "EXIT": "CLOSE",
    "REDUCE": "CLOSE",
    "STOP": "CLOSE",
    "REVERSE_LONG": "REVERSE_LONG",
    "REVERSE_SHORT": "REVERSE_SHORT",
}
OPEN_ACTIONS = {"OPEN", "OPEN_LONG", "OPEN_SHORT"}
CLOSE_ACTIONS = {"CLOSE", "CLOSE_LONG", "CLOSE_SHORT"}
REVERSE_ACTIONS = {"REVERSE_LONG", "REVERSE_SHORT"}
STATUS_EVENT = "STATUS"
PINE_STATUS_KEY = "pine_status"


class TVSignal(BaseModel):
    """兼容本项目旧消息与老师项目 TradingView strategy 占位符消息。"""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    passphrase: str
    schema_version: Optional[str | int] = None
    event: Optional[str] = None
    action: Optional[str] = None
    explicit_action: Optional[str] = None
    strategy: str = ""
    source_id: Optional[str | int] = None
    source_name: Optional[str] = None
    signal_code: Optional[str] = None
    signal_id: Optional[str] = None
    trade_id: Optional[str | int] = None
    account_name: Optional[str] = Field(default=None, alias="accountName")

    # TradingView strategy 占位符字段
    side: Optional[str] = None
    exchange: Optional[str] = None
    period: Optional[str] = None
    timeframe: Optional[str] = None
    market_position: Optional[str] = Field(default=None, alias="marketPosition")
    prev_market_position: Optional[str] = Field(
        default=None, alias="prevMarketPosition")
    previous_position: Optional[str] = None
    symbol: Optional[str] = None
    price: Optional[float | str] = None
    timestamp: Optional[int | str] = None
    event_time: Optional[int | str] = None
    bar_time: Optional[int | str] = None
    size: Optional[float | str] = None
    position_size: Optional[float | str] = Field(
        default=None, alias="positionSize")
    order_id: Optional[str] = Field(default=None, alias="id")
    tv_id: Optional[str | int] = None
    alert_message: Optional[str] = Field(default=None, alias="alertMessage")
    comment: Optional[str] = None
    qty_type: Optional[str] = Field(default=None, alias="qtyType")

    # 信用价差执行意图；旧字段继续兼容
    spread_type: Optional[str] = None
    sell_delta: Optional[float] = None
    dte_min: Optional[float] = None
    dte_max: Optional[float] = None
    close_ratio: Optional[float] = Field(default=None, alias="closeRatio")
    reason: Optional[str] = None
    delta1: Optional[float] = None
    delta2: Optional[float] = None
    n: Optional[float] = None
    dvol_percentile: Optional[float] = None
    dvol_tier: Optional[int] = None
    btc_price: Optional[float] = None
    hv_percentile: Optional[float] = None
    group_split_count: Optional[int] = None
    delta_account_risk_pct: Optional[float] = None
    delta_trigger_pct: Optional[float] = None
    call_hedge_release_pct: Optional[float] = None
    put_hedge_release_pct: Optional[float] = None


class PineStatus(BaseModel):
    """Pine 每根 2h bar 收盘发的连接回报，只用于链路诊断。"""

    passphrase: str
    event: str
    strategy: str = ""
    bar_ts: Optional[int] = None
    btc_price: Optional[float] = None
    bar_open: Optional[float] = None
    bar_high: Optional[float] = None
    bar_low: Optional[float] = None
    cci: Optional[float] = None
    hv_percentile: Optional[float] = None
    boll_upper: Optional[float] = None
    boll_lower: Optional[float] = None
    dvol_percentile: Optional[float] = None
    dvol_tier: Optional[int] = None
    dvol_samples: Optional[int] = None
    cfg_ok: Optional[bool] = None
    tier_ok: Optional[bool] = None
    hv_ok: Optional[bool] = None
    cci_ok: Optional[bool] = None
    put_shape: Optional[bool] = None
    call_shape: Optional[bool] = None


def _number(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None  # 排除 NaN


def _derive_action(sig: TVSignal) -> str:
    explicit = (sig.explicit_action or sig.action or "").strip().upper()
    current = (sig.market_position or "").strip().lower()
    previous = (
        sig.previous_position or sig.prev_market_position or ""
    ).strip().lower()
    if explicit == "REVERSE":
        if current == "long":
            return "REVERSE_LONG"
        if current == "short":
            return "REVERSE_SHORT"
    if explicit in ACTION_ALIASES:
        action = ACTION_ALIASES[explicit]
        if action == "OPEN" and current in {"long", "short"}:
            return f"OPEN_{current.upper()}"
        if action == "CLOSE" and previous in {"long", "short"}:
            return f"CLOSE_{previous.upper()}"
        return action

    event = (sig.event or "").strip().upper()
    if event in OPEN_EVENTS:
        return f"OPEN_{current.upper()}" if current in {"long", "short"} else "OPEN"
    if event in EXIT_EVENTS:
        return (
            f"CLOSE_{previous.upper()}"
            if previous in {"long", "short"}
            else "CLOSE"
        )

    if previous == "flat" and current == "long":
        return "OPEN_LONG"
    if previous == "flat" and current == "short":
        return "OPEN_SHORT"
    if current == "flat" and previous == "long":
        return "CLOSE_LONG"
    if current == "flat" and previous == "short":
        return "CLOSE_SHORT"
    if previous == "long" and current == "short":
        return "REVERSE_SHORT"
    if previous == "short" and current == "long":
        return "REVERSE_LONG"
    raise ValueError("cannot determine OPEN/CLOSE action")


def _derive_spread_type(sig: TVSignal, action: str) -> Optional[str]:
    explicit = (sig.spread_type or "").strip().upper()
    if explicit:
        return explicit
    if action not in OPEN_ACTIONS | REVERSE_ACTIONS:
        return None
    if action in {"OPEN_LONG", "REVERSE_LONG"}:
        return "PUT_CREDIT"
    if action in {"OPEN_SHORT", "REVERSE_SHORT"}:
        return "CALL_CREDIT"
    current = (sig.market_position or "").strip().lower()
    if current == "long":
        return "PUT_CREDIT"
    if current == "short":
        return "CALL_CREDIT"
    side = (sig.side or "").strip().lower()
    if side == "buy":
        return "PUT_CREDIT"
    if side == "sell":
        return "CALL_CREDIT"
    if sig.delta1 is not None:
        return "CALL_CREDIT" if sig.delta1 > 0 else "PUT_CREDIT"
    comment = (sig.comment or "").upper()
    if "PUT_CREDIT" in comment:
        return "PUT_CREDIT"
    if "CALL_CREDIT" in comment:
        return "CALL_CREDIT"
    return None


def _normalise_signal(sig: TVSignal) -> dict:
    """把两种输入格式变成执行层唯一认识的命令格式。"""
    action = _derive_action(sig)
    is_v2 = str(sig.schema_version or "") == "2" or sig.source_id is not None
    source_id = str(
        sig.source_id if sig.source_id is not None else ""
    ).strip()
    source_name = (sig.source_name or sig.strategy or "").strip()
    signal_code = sig.signal_code or sig.order_id or sig.alert_message or action
    if is_v2 and (not source_id or not source_name or not signal_code):
        raise ValueError("missing generic signal identity")
    timestamp = str(
        sig.event_time or sig.timestamp or int(time.time() * 1000)
    )
    source_order_id = str(signal_code)
    # New Alerts carry the TradingView bar time and order id.  Canonicalising
    # those fields on the server prevents two copies of the same Pine Alert
    # from bypassing idempotency merely by changing tv_id or signal_id.
    if sig.bar_time is not None and source_order_id:
        signal_id = (
            f"{source_id if is_v2 else source_name or 'TRADINGVIEW'}:"
            f"{source_order_id}:"
            f"{action}:{sig.bar_time}")
    else:
        signal_id = sig.signal_id or (
            f"{source_name or 'TRADINGVIEW'}:{source_order_id}:{timestamp}")
    # tv_id is the stable identity of one TradingView Alert. Prefer it when
    # present so every OPEN/CLOSE emitted by that Alert targets the same trade.
    # trade_id remains a compatibility fallback for Alerts created previously.
    trade_value = (
        source_id
        or (sig.tv_id if sig.tv_id is not None else sig.trade_id)
    )
    if not trade_value and action in OPEN_ACTIONS:
        trade_value = signal_id
    trade_id = str(trade_value) if trade_value else None

    sell_delta = sig.sell_delta
    if sell_delta is None and sig.delta1 is not None:
        sell_delta = abs(sig.delta1)
    dte_min = sig.dte_min if sig.dte_min is not None else sig.n
    dte_max = sig.dte_max
    if dte_min is not None and dte_max is None:
        dte_max = max(float(dte_min) + 30.0, 60.0)

    # TradingView built-in strategies report the strategy position change
    # (for example +2/-2 on a reversal), not a safe Deribit option quantity.
    # Generic position-derived and reversal signals therefore use the
    # backend's configured risk budget instead of trusting that number.
    explicit_action = (
        sig.explicit_action or sig.action or ""
    ).strip().upper()
    explicit_event = (sig.event or "").strip().upper()
    uses_strategy_position = (
        explicit_action not in ACTION_ALIASES
        and explicit_action != "REVERSE"
        and explicit_event not in OPEN_EVENTS
        and explicit_event not in EXIT_EVENTS
    )
    has_option_execution_intent = any(value is not None for value in (
        sig.spread_type, sig.sell_delta, sig.dte_min, sig.dte_max,
        sig.delta1, sig.n, sig.dvol_tier,
    ))
    uses_backend_profile = (
        action in OPEN_ACTIONS | REVERSE_ACTIONS
        and (is_v2 or not has_option_execution_intent)
    )
    quantity = (
        None if uses_backend_profile or uses_strategy_position
        or action in REVERSE_ACTIONS
        else _number(sig.size)
    )
    close_ratio = sig.close_ratio if sig.close_ratio is not None else 1.0
    reason = sig.reason or (
        "STOP_LOSS" if "止损" in (sig.comment or "") else
        (sig.event or "TRADINGVIEW_CLOSE"))

    return {
        "event": (
            "OPEN_REQUEST"
            if action in OPEN_ACTIONS | REVERSE_ACTIONS
            else "CLOSE_REQUEST"
        ),
        "action": action,
        "signal_id": signal_id,
        "trade_id": trade_id,
        "tv_id": str(sig.tv_id) if sig.tv_id is not None else None,
        "source_id": source_id or None,
        "signal_code": source_order_id,
        "account_name": sig.account_name,
        "strategy": source_name,
        "spread_type": _derive_spread_type(sig, action),
        "quantity": quantity,
        "qty_type": (
            "risk" if uses_backend_profile else (sig.qty_type or "fixed").lower()
        ),
        "sell_delta": None if is_v2 else sell_delta,
        "dte_min": None if is_v2 else dte_min,
        "dte_max": None if is_v2 else dte_max,
        "close_ratio": close_ratio,
        "reason": reason,
        "timestamp": sig.event_time or sig.timestamp,
        "bar_time": sig.bar_time,
        "symbol": sig.symbol,
        "side": sig.side,
        "market_position": sig.market_position,
        "prev_market_position": (
            sig.previous_position or sig.prev_market_position
        ),
        "price": _number(sig.price),
        "position_size": _number(sig.position_size),
        "source_order_id": source_order_id,
        "source": "tradingview",
        "execution_profile": (
            "TESTNET_DEFAULT"
            if uses_backend_profile
            else None
        ),
        # 执行和对冲参数
        "dvol_tier": None if is_v2 else sig.dvol_tier,
        "dvol_percentile": None if is_v2 else sig.dvol_percentile,
        "btc_price": sig.btc_price,
        "hv_percentile": sig.hv_percentile,
        "group_split_count": None if is_v2 else sig.group_split_count,
        "delta_account_risk_pct": (
            None if is_v2 else sig.delta_account_risk_pct
        ),
        "delta_trigger_pct": None if is_v2 else sig.delta_trigger_pct,
        "call_hedge_release_pct": (
            None if is_v2 else sig.call_hedge_release_pct
        ),
        "put_hedge_release_pct": (
            None if is_v2 else sig.put_hedge_release_pct
        ),
        "delta2": sig.delta2,
    }


def _payload_sane(sig: TVSignal) -> bool:
    """仅验证消息与执行参数，不重新判断市场是否应该交易。"""
    if sig.dvol_tier is not None and not (-1 <= int(sig.dvol_tier) <= 3):
        return False
    if (sig.dvol_percentile is not None and sig.dvol_percentile != -1
            and not (0.0 <= sig.dvol_percentile <= 1.0)):
        return False
    if sig.group_split_count is not None and not (1 <= sig.group_split_count <= 20):
        return False
    if sig.sell_delta is not None and not (0.05 <= abs(sig.sell_delta) <= 0.70):
        return False
    if sig.delta1 is not None and not (0.05 <= abs(sig.delta1) <= 0.70):
        return False
    if sig.dte_min is not None and not (1 <= sig.dte_min <= 365):
        return False
    if sig.dte_max is not None and not (1 <= sig.dte_max <= 730):
        return False
    if sig.close_ratio is not None and not (0 < sig.close_ratio <= 1):
        return False
    quantity = _number(sig.size)
    explicit_action = (
        sig.explicit_action or sig.action or ""
    ).strip().upper()
    explicit_event = (sig.event or "").strip().upper()
    uses_strategy_position = (
        explicit_action not in ACTION_ALIASES
        and explicit_action != "REVERSE"
        and explicit_event not in OPEN_EVENTS
        and explicit_event not in EXIT_EVENTS
    )
    is_v2 = str(sig.schema_version or "") == "2" or sig.source_id is not None
    if (not is_v2 and not uses_strategy_position and sig.size is not None
            and (quantity is None or quantity <= 0)):
        return False
    ranges = (
        (sig.delta_account_risk_pct, 0.0001, 0.20),
        (sig.delta_trigger_pct, 0.0001, 1.0),
        (sig.call_hedge_release_pct, 0.50, 1.0),
        (sig.put_hedge_release_pct, 1.0, 1.50),
    )
    return not any(
        value is not None and not (low <= value <= high)
        for value, low, high in ranges)


async def _handle_status(ctx: Ctx, body: dict, expected: str) -> dict:
    try:
        status = PineStatus(**body)
    except ValidationError:
        return {"status": "rejected", "reason": "bad_payload"}
    if not expected or not hmac.compare_digest(status.passphrase, expected):
        log.warning("pine status passphrase mismatch (bar_ts=%s)", status.bar_ts)
        return {"status": "rejected", "reason": "auth"}
    payload = status.model_dump(exclude={"passphrase"})
    payload["received_at"] = int(time.time() * 1000)
    fresh = await ctx.repo.record_pine_heartbeat(payload)
    return {
        "status": "ok",
        "bar_ts": status.bar_ts,
        "duplicate": not fresh,
    }


async def _process_tradingview(request: Request) -> JSONResponse:
    ctx: Ctx = request.app.state.ctx
    repo = ctx.repo
    expected = ctx.cfg.env.webhook_auth_token
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("request body must be an object")
    except (ValueError, TypeError):
        await ev.emit(repo.conn, ev.REJECT_BAD_PAYLOAD, level="WARN",
                      error="invalid JSON object")
        return JSONResponse(
            {"status": "rejected", "reason": "bad_payload"}, status_code=400)

    if body.get("event") == STATUS_EVENT:
        return await _handle_status(ctx, body, expected)

    try:
        sig = TVSignal(**body)
    except ValidationError:
        await ev.emit(repo.conn, ev.REJECT_BAD_PAYLOAD, level="WARN",
                      error="payload schema validation failed")
        return JSONResponse(
            {"status": "rejected", "reason": "bad_payload"}, status_code=400)

    if not expected or not hmac.compare_digest(sig.passphrase, expected):
        log.warning("webhook passphrase mismatch")
        return JSONResponse(
            {"status": "rejected", "reason": "auth"}, status_code=401)
    if not _payload_sane(sig):
        return JSONResponse(
            {"status": "rejected", "reason": "bad_execution_parameters"},
            status_code=400)

    try:
        payload = _normalise_signal(sig)
    except ValueError as exc:
        await ev.emit(repo.conn, ev.REJECT_BAD_PAYLOAD, level="WARN",
                      error=str(exc))
        reason = (
            "missing_signal_identity"
            if str(exc) == "missing generic signal identity"
            else "unknown_action"
        )
        return JSONResponse(
            {"status": "rejected", "reason": reason},
            status_code=400)

    signal_id = payload["signal_id"]
    configured_account = getattr(ctx.cfg.env, "trading_account_name", "")
    if (payload["account_name"] and configured_account
            and payload["account_name"] != configured_account):
        return JSONResponse(
            {"status": "rejected", "reason": "bad_account"}, status_code=400)

    fresh = await repo.insert_signal(
        signal_id, payload["event"], payload["strategy"] or ctx.cfg.strategy.name,
        payload["spread_type"] or "", payload)
    if not fresh:
        await ev.emit(repo.conn, ev.REJECT_DUPLICATE_SIGNAL, level="WARN",
                      signal_id=signal_id)
        return JSONResponse({
            "status": "duplicate",
            "signal_id": signal_id,
            "trade_id": payload["trade_id"],
        })

    if payload["action"] in OPEN_ACTIONS | REVERSE_ACTIONS:
        if payload["spread_type"] not in ("CALL_CREDIT", "PUT_CREDIT"):
            await repo.set_signal_status(
                signal_id, "REJECTED", "bad spread_type")
            return JSONResponse(
                {"status": "rejected", "reason": "bad_spread_type"},
                status_code=400)
        if payload["action"] in REVERSE_ACTIONS:
            ctx.queue.put(ctx.queue.reverse(payload))
            return JSONResponse({
                "status": "queued",
                "action": "REVERSE",
                "normalized_action": payload["action"],
                "signal_id": signal_id,
                "trade_id": payload["trade_id"],
            }, status_code=202)
        ctx.queue.put(ctx.queue.open(payload))
        return JSONResponse({
            "status": "queued",
            "action": "OPEN",
            "normalized_action": payload["action"],
            "signal_id": signal_id,
            "trade_id": payload["trade_id"],
        }, status_code=202)

    # CLOSE 必须优先按 source_id/tv_id 精确定位。仅当旧消息没有身份且当前
    # 确实只有一笔开放交易时才兼容回退，绝不再“一条退出信号关闭全部”。
    parent = None
    if payload["trade_id"]:
        parent = await repo.active_parent_by_trade_id(
            payload["trade_id"],
            "" if payload["source_id"] else payload["strategy"])
    else:
        parents = await repo.open_parents()
        if len(parents) == 1:
            parent = parents[0]
    if parent is None:
        await repo.set_signal_status(
            signal_id, "ACCEPTED", "no matching active trade; no-op")
        return JSONResponse({
            "status": "noop",
            "reason": "no_matching_trade",
            "signal_id": signal_id,
            "trade_id": payload["trade_id"],
        }, status_code=200)

    await repo.set_signal_status(signal_id, "ACCEPTED")
    ctx.queue.put(ctx.queue.normal_close(parent["id"], payload["reason"]))
    await ev.emit(repo.conn, ev.NORMAL_CLOSE, signal_id=signal_id,
                  parent_id=parent["id"], source="tradingview",
                  reason=payload["reason"])
    return JSONResponse({
        "status": "queued",
        "action": "CLOSE",
        "normalized_action": payload["action"],
        "signal_id": signal_id,
        "trade_id": payload["trade_id"] or parent.get("trade_id"),
        "parent_id": parent["id"],
    }, status_code=202)


@router.post("/webhook/tradingview")
async def tradingview_webhook(request: Request):
    """旧 URL 保持兼容。"""
    return await _process_tradingview(request)


@router.post("/webhook/signal")
async def tradingview_signal_webhook(request: Request):
    """与老师项目一致的新标准入口。"""
    return await _process_tradingview(request)
