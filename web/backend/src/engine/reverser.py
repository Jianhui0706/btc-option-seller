"""Safe TradingView strategy reversal: close first, confirm, then open."""

from __future__ import annotations

from ..context import Ctx
from ..monitor import events as ev
from . import closer, orchestrator, risk


async def handle_reverse(ctx: Ctx, payload: dict) -> None:
    signal: dict = payload["signal"]
    signal_id: str = signal["signal_id"]
    repo = ctx.repo

    async def entry_enabled() -> bool:
        if await ctx.trading_enabled():
            return True
        return await risk.maybe_resume_testnet_daily_loss_pause(ctx)

    async def reject(code: str, **extra) -> None:
        detail = None
        if code in (ev.REJECT_TRADING_DISABLED, ev.REVERSE_OPEN_SKIPPED):
            detail = await repo.get_state(risk.PAUSE_REASON_KEY, "") or None
            if detail:
                extra.setdefault("pause_reason", detail)
        await repo.set_signal_status(
            signal_id, "REJECTED", code, detail=detail)
        await ev.emit(repo.conn, code, level="WARN",
                      signal_id=signal_id, **extra)

    # An active force-close owns the account; never race it with a reversal.
    if await ctx.force_close_active():
        await reject(ev.REJECT_TRADING_DISABLED, force_close=True)
        return

    parent = None
    trade_id = signal.get("trade_id")
    if trade_id:
        parent = await repo.active_parent_by_trade_id(
            trade_id,
            "" if signal.get("source_id") else signal.get("strategy", ""))
    if parent is None:
        # Compatibility for an Alert created before stable tv_id routing.
        parents = await repo.open_parents()
        if len(parents) == 1:
            parent = parents[0]
        elif len(parents) > 1:
            await reject("REJECT_AMBIGUOUS_ACTIVE_TRADE",
                         parent_ids=[p["id"] for p in parents])
            return

    if parent is not None and parent["state"] == "OPENING":
        parent = await risk.recover_opening_parent(ctx, parent["id"])
        if parent is not None and parent["state"] == "OPEN_FAILED":
            parent = None

    # A position-derived TradingView reversal can arrive while the backend is
    # already flat.  In that case no close happened, so report the real gate
    # instead of claiming that the old side was closed before the open was
    # skipped.
    if parent is None and not await entry_enabled():
        await reject(ev.REJECT_TRADING_DISABLED, force_close=False)
        return

    if parent is not None:
        await closer.handle_normal_close(ctx, {
            "parent_id": parent["id"],
            "reason": signal.get("reason") or signal["action"],
        })
        closed = await repo.get_parent(parent["id"])
        if not closed or closed["state"] != "CLOSED":
            await reject(ev.REVERSE_CLOSE_INCOMPLETE,
                         parent_id=parent["id"],
                         state=closed["state"] if closed else None)
            return

    # The operator's pause blocks only the new side; a managed old side above
    # is still allowed to close.
    if not await entry_enabled():
        await reject(ev.REVERSE_OPEN_SKIPPED,
                     closed_parent_id=parent["id"] if parent else None)
        return

    ctx.hedge_frozen = False
    await orchestrator.handle_open(ctx, {"signal": signal})
