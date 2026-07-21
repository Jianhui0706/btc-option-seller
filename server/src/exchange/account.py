"""Deribit 账户查询封装：权益、持仓、挂单、保证金。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .deribit_ws import DeribitWS


@dataclass
class AccountSummary:
    equity_btc: float           # 账户权益（BTC）= 规范十的 account_btc_equity
    available_funds: float
    maintenance_margin: float
    initial_margin: float
    margin_ratio: float         # maintenance_margin / equity（权益为 0 时取 0）
    session_rpl: float
    session_upl: float
    raw: dict = field(repr=False, default_factory=dict)


async def get_account_summary(ws: DeribitWS, currency: str = "BTC") -> AccountSummary:
    r = await ws.request("private/get_account_summary",
                         {"currency": currency, "extended": True})
    equity = float(r.get("equity") or 0.0)
    mm = float(r.get("maintenance_margin") or 0.0)
    return AccountSummary(
        equity_btc=equity,
        available_funds=float(r.get("available_funds") or 0.0),
        maintenance_margin=mm,
        initial_margin=float(r.get("initial_margin") or 0.0),
        margin_ratio=(mm / equity) if equity > 0 else 0.0,
        session_rpl=float(r.get("session_rpl") or 0.0),
        session_upl=float(r.get("session_upl") or 0.0),
        raw=r,
    )


@dataclass
class OptionPosition:
    instrument: str
    size: float                 # 张数，负=卖方
    delta: float                # Deribit 返回的该仓位总 Delta（BTC）
    mark_price: float
    average_price: float
    floating_pnl: float

    @property
    def is_short(self) -> bool:
        return self.size < 0


async def get_option_positions(ws: DeribitWS, currency: str = "BTC") -> list[OptionPosition]:
    rows: list[dict[str, Any]] = await ws.request(
        "private/get_positions", {"currency": currency, "kind": "option"})
    out = []
    for p in rows:
        if abs(float(p.get("size") or 0.0)) < 1e-9:
            continue
        out.append(OptionPosition(
            instrument=p["instrument_name"],
            size=float(p["size"]),
            delta=float(p.get("delta") or 0.0),
            mark_price=float(p.get("mark_price") or 0.0),
            average_price=float(p.get("average_price") or 0.0),
            floating_pnl=float(p.get("floating_profit_loss") or 0.0),
        ))
    return out


@dataclass
class FuturePosition:
    instrument: str
    size_usd: float             # 合约张数×面值（USD），正=多
    delta_btc: float            # Deribit 返回的 Delta（BTC）
    average_price: float
    floating_pnl: float


async def get_future_positions(ws: DeribitWS, currency: str = "BTC") -> list[FuturePosition]:
    rows: list[dict[str, Any]] = await ws.request(
        "private/get_positions", {"currency": currency, "kind": "future"})
    out = []
    for p in rows:
        if abs(float(p.get("size") or 0.0)) < 1e-9:
            continue
        out.append(FuturePosition(
            instrument=p["instrument_name"],
            size_usd=float(p["size"]),
            delta_btc=float(p.get("delta") or 0.0),
            average_price=float(p.get("average_price") or 0.0),
            floating_pnl=float(p.get("floating_profit_loss") or 0.0),
        ))
    return out


async def get_open_orders(ws: DeribitWS, currency: str = "BTC") -> list[dict]:
    return await ws.request("private/get_open_orders_by_currency", {"currency": currency})


async def cancel_all(ws: DeribitWS, currency: str = "BTC") -> Any:
    return await ws.request("private/cancel_all_by_currency", {"currency": currency})
