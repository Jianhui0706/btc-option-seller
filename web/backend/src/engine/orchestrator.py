"""开仓编排（规范第三、六、七节）：
持仓检查 → 选腿 → 算量 → 执行（Combo 优先→买腿先行）→ BEP → 分组落库。
"""

from __future__ import annotations

import asyncio
import logging
import math

from ..config import DATA_DIR
from ..context import Ctx
from ..exchange import account, market_data
from ..execution import adapter
from ..monitor import events as ev
from . import bep as bep_mod
from . import grouping, leg_selector, risk

log = logging.getLogger("v2.open")


async def handle_open(ctx: Ctx, payload: dict) -> None:
    signal: dict = payload["signal"]
    signal_id: str = signal["signal_id"]
    spread_type: str = signal["spread_type"]
    repo, cfg, ws = ctx.repo, ctx.cfg, ctx.ws

    async def reject(code: str, **extra) -> None:
        await repo.set_signal_status(signal_id, "REJECTED", code)
        await ev.emit(repo.conn, code, level="WARN", signal_id=signal_id, **extra)

    # 0. 系统状态
    if await ctx.force_close_active() or not await ctx.trading_enabled():
        await reject(ev.REJECT_TRADING_DISABLED,
                     force_close=await ctx.force_close_active())
        return

    parent_id = await repo.create_parent(
        signal_id, spread_type, signal.get("dvol_tier"),
        trade_id=signal.get("trade_id"))

    # 1. 开仓前持仓检查（规范第三节：任何期权卖方持仓都阻止，无论来源）
    await repo.transition_parent(parent_id, "POSITION_CHECKING")
    opts = await account.get_option_positions(ws, cfg.strategy.currency)
    if opts:
        recovered = await risk.recover_terminal_late_fills(
            ctx, opts, ignore_parent_id=parent_id)
        if recovered:
            opts = await account.get_option_positions(
                ws, cfg.strategy.currency)
    shorts = [p for p in opts if p.is_short]
    if shorts:
        await repo.transition_parent(parent_id, "OPEN_FAILED",
                                     ev.REJECT_EXISTING_OPTION_SHORT,
                                     shorts=[p.instrument for p in shorts])
        await repo.set_signal_status(signal_id, "REJECTED",
                                     ev.REJECT_EXISTING_OPTION_SHORT)
        return
    # DB 侧防重：已有活跃父交易同样视为占用（同一时间仅一笔卖方交易）
    actives = [p for p in await repo.active_parents() if p["id"] != parent_id]
    if actives:
        await repo.transition_parent(parent_id, "OPEN_FAILED",
                                     ev.REJECT_EXISTING_OPTION_SHORT,
                                     active_parent_ids=[p["id"] for p in actives])
        await repo.set_signal_status(signal_id, "REJECTED",
                                     ev.REJECT_EXISTING_OPTION_SHORT)
        return

    # 2. 选腿。TradingView 已经决定是否交易，并把本次执行所需的
    # sell_delta / DTE 区间随信号传入；后端只做参数校验和真实合约选择。
    # 旧 Alert 暂时可用 dvol_tier 映射执行参数，但后端不再拉行情重算档位。
    await repo.transition_parent(parent_id, "SELECTING_LEGS")
    sell_delta = signal.get("sell_delta")
    dte_lo = signal.get("dte_min")
    dte_hi = signal.get("dte_max")
    tier_no = signal.get("dvol_tier")
    if sell_delta is None or dte_lo is None or dte_hi is None:
        try:
            tier = cfg.leg_selection.tier_map.get(int(tier_no))
        except (TypeError, ValueError):
            tier = None
        # TradingView built-in strategies cannot supply this project's option
        # leg parameters.  On Testnet only, use the most conservative configured
        # tier so the adapter can be exercised without weakening Mainnet rules.
        if (tier is None
                and signal.get("execution_profile") == "TESTNET_DEFAULT"
                and cfg.env.is_testnet
                and cfg.leg_selection.tier_map):
            tier = cfg.leg_selection.tier_map[
                min(cfg.leg_selection.tier_map)]
        if tier is not None:
            sell_delta = tier.sell_delta
            dte_lo = tier.dte_lo
            dte_hi = tier.dte_hi
    if sell_delta is None or dte_lo is None or dte_hi is None:
        await repo.transition_parent(parent_id, "OPEN_FAILED", ev.REJECT_BAD_PAYLOAD,
                                     reason="TradingView 未提供 sell_delta/DTE")
        await repo.set_signal_status(signal_id, "REJECTED", ev.REJECT_BAD_PAYLOAD)
        return
    sell_delta = abs(float(sell_delta))
    dte_lo, dte_hi = float(dte_lo), float(dte_hi)
    if (not cfg.leg_selection.sell_delta_min <= sell_delta
            <= cfg.leg_selection.sell_delta_max
            or not 1 <= dte_lo <= dte_hi <= 730):
        await repo.transition_parent(parent_id, "OPEN_FAILED", ev.REJECT_BAD_PAYLOAD,
                                     reason="TradingView 选腿参数超出安全范围")
        await repo.set_signal_status(signal_id, "REJECTED", ev.REJECT_BAD_PAYLOAD)
        return
    opt_type = "put" if spread_type == "PUT_CREDIT" else "call"
    chain = await market_data.option_chain_tickers(
        ws, cfg.strategy.currency, opt_type, dte_lo, dte_hi)
    sel = leg_selector.select_spread(
        chain.tickers, spread_type, sell_delta,
        sell_delta_min=cfg.leg_selection.sell_delta_min,
        sell_delta_max=cfg.leg_selection.sell_delta_max,
        sell_delta_search_radius=cfg.leg_selection.sell_delta_search_radius,
        min_credit_btc=cfg.leg_selection.min_credit_btc,
        risk_reward_max=cfg.leg_selection.risk_reward_max)
    if sel is None:
        await repo.transition_parent(parent_id, "OPEN_FAILED", ev.REJECT_NO_VALID_LEGS,
                                     dte=round(chain.dte, 1),
                                     checked=chain.checked_expiries)
        await repo.set_signal_status(signal_id, "REJECTED", ev.REJECT_NO_VALID_LEGS)
        return

    # 3. 算量（风险预算）
    summ = await account.get_account_summary(ws, cfg.strategy.currency)
    risk_qty = leg_selector.contracts_for_budget(
        summ.equity_btc, cfg.risk.account_risk_pct, sel.max_loss_btc,
        cfg.strategy.min_lot)
    requested = signal.get("quantity")
    if requested is not None and signal.get("qty_type", "fixed") == "fixed":
        requested_qty = (
            math.floor(float(requested) / cfg.strategy.min_lot + 1e-9)
            * cfg.strategy.min_lot)
        qty = min(risk_qty, requested_qty)
    else:
        qty = risk_qty
    if qty < cfg.strategy.min_lot:
        await repo.transition_parent(parent_id, "OPEN_FAILED",
                                     ev.REJECT_INSUFFICIENT_RISK_BUDGET,
                                     equity=summ.equity_btc,
                                     max_loss=sel.max_loss_btc)
        await repo.set_signal_status(signal_id, "REJECTED",
                                     ev.REJECT_INSUFFICIENT_RISK_BUDGET)
        return

    # 4. 执行（作为可被 FORCE_CLOSE 打断的任务）
    await repo.transition_parent(parent_id, "OPENING",
                                 sell=sel.sell.instrument, buy=sel.buy.instrument,
                                 qty=qty, credit_est=sel.net_credit_est)
    # 执行前先落库合约信息：进程中断后 reconcile 才能按合约认领交易所持仓
    await repo.set_instruments(parent_id,
                               sell_instrument=sel.sell.instrument,
                               buy_instrument=sel.buy.instrument,
                               sell_strike=sel.sell.strike,
                               buy_strike=sel.buy.strike,
                               expiry_ts=chain.expiration_timestamp)
    await repo.set_signal_status(signal_id, "ACCEPTED")
    idx_price = await market_data.get_index_price(ws, cfg.strategy.currency)

    task = asyncio.create_task(adapter.open_credit_spread(
        ws,
        buy_instrument=sel.buy.instrument,
        sell_instrument=sel.sell.instrument,
        qty=qty,
        min_credit_btc=cfg.leg_selection.min_credit_btc,
        min_lot=cfg.strategy.min_lot,
        n_exec_groups=cfg.execution.n_exec_groups,
        attempt_waits=tuple(cfg.execution.attempt_waits),
        report_dir=str(DATA_DIR / "exec_reports"),
        dry_run=cfg.debug.dry_run,
    ))
    ctx.current_exec_task = task
    try:
        res = await task
    except asyncio.CancelledError:
        # FORCE_CLOSE 打断：执行器 finally 已撤残单，状态交由强平流程处理
        await ev.emit(repo.conn, ev.STATE_TRANSITION, level="WARN",
                      parent_id=parent_id, note="opening cancelled by force close")
        raise
    except Exception as exc:  # noqa: BLE001 - OPENING must never leak
        await ev.emit(
            repo.conn, ev.OPEN_EXECUTION_ERROR, level="ERROR",
            parent_id=parent_id, signal_id=signal_id, error=str(exc))
        recovered = await risk.recover_opening_parent(ctx, parent_id)
        if recovered and recovered["state"] != "OPENING":
            if recovered["state"] == "OPEN_FAILED":
                await repo.set_signal_status(
                    signal_id, "REJECTED", ev.OPEN_EXECUTION_ERROR)
            return
        raise
    finally:
        ctx.current_exec_task = None

    # 5. 结果登记
    if res.unwound_qty > 0:
        await ev.emit(repo.conn, ev.OPEN_FAILED_UNWOUND, level="WARN",
                      parent_id=parent_id, unwound=res.unwound_qty)
    if not res.opened:
        await repo.transition_parent(parent_id, "OPEN_FAILED",
                                     reason="执行后无等量持仓")
        return

    bep = cfg.debug.bep_override or bep_mod.bep_price(
        spread_type, sel.sell.strike, res.net_credit_btc, idx_price)
    await repo.fill_open_details(
        parent_id,
        sell_instrument=sel.sell.instrument, buy_instrument=sel.buy.instrument,
        expiry_ts=chain.expiration_timestamp,
        sell_strike=sel.sell.strike, buy_strike=sel.buy.strike,
        qty_total=res.qty, net_credit_btc=res.net_credit_btc,
        open_index_price=idx_price, bep_price=bep)
    await repo.create_legs(parent_id,
                           sel.sell.instrument, res.qty, res.sell_avg,
                           sel.buy.instrument, res.qty, res.buy_avg)
    await repo.record_fill(parent_id=parent_id, group_id=None,
                           instrument=sel.buy.instrument, direction="buy",
                           qty=res.qty, price=res.buy_avg, fee=0.0, phase="open")
    await repo.record_fill(parent_id=parent_id, group_id=None,
                           instrument=sel.sell.instrument, direction="sell",
                           qty=res.qty, price=res.sell_avg, fee=0.0, phase="open")
    await repo.set_liquidity_role(parent_id, "open", res.liquidity_role)

    # 6. 分组（规范第七节）
    n = int(signal.get("group_split_count") or cfg.group_split_count_default)
    quantities = grouping.split_groups(res.qty, n, cfg.strategy.min_lot)
    await repo.create_groups(parent_id, quantities)
    await repo.ensure_hedge_row(parent_id)
    await repo.transition_parent(parent_id, "OPEN", ev.OPENED,
                                 qty=res.qty, net_credit=res.net_credit_btc,
                                 bep=bep, groups=quantities,
                                 combo_qty=res.combo_qty)
    log.info("parent %s OPEN: qty=%.1f credit=%.4f bep=%.1f groups=%s",
             parent_id, res.qty, res.net_credit_btc, bep, quantities)
