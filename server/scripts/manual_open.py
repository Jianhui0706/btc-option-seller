"""M2 验证 CLI：testnet 手动开/平一笔信用价差。

用法（server/ 目录下）:
  python scripts/manual_open.py --type PUT_CREDIT --tier 2            # dry-run 报价阶梯
  python scripts/manual_open.py --type PUT_CREDIT --tier 2 --qty 0.1 --real   # 真实开仓
  python scripts/manual_open.py --close SELL_INSTR BUY_INSTR --qty 0.1        # 双腿全平
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config, DATA_DIR
from src.engine import leg_selector
from src.exchange import account, market_data
from src.exchange.deribit_ws import DeribitWS, TESTNET_URL, MAINNET_URL
from src.execution import adapter


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--type", choices=["PUT_CREDIT", "CALL_CREDIT"], default="PUT_CREDIT")
    ap.add_argument("--tier", type=int, default=2)
    ap.add_argument("--qty", type=float, default=0.1)
    ap.add_argument("--real", action="store_true", help="真实下单（默认 dry-run）")
    ap.add_argument("--close", nargs=2, metavar=("SELL_INSTR", "BUY_INSTR"),
                    help="双腿全平指定合约")
    args = ap.parse_args()

    cfg = load_config()
    assert cfg.env.is_testnet, "manual_open 仅限 testnet 使用"
    ws = DeribitWS(url=TESTNET_URL, client_id=cfg.env.client_id,
                   client_secret=cfg.env.client_secret)
    await ws.connect()
    report_dir = str(DATA_DIR / "exec_reports")

    try:
        if args.close:
            sell_instr, buy_instr = args.close
            print(f"双腿全平: 买回 {sell_instr} / 卖出 {buy_instr} × {args.qty}")
            res = await adapter.close_spread_both_legs(
                ws, sell_instrument=sell_instr, buy_instrument=buy_instr,
                qty=args.qty, n_exec_groups=cfg.execution.n_exec_groups,
                report_dir=report_dir, on_event=lambda m: print("  |", m))
            print(f"结果: closed={res.closed_qty} buy_back_avg={res.buy_back_avg:.4f} "
                  f"sell_out_avg={res.sell_out_avg:.4f} complete={res.complete}")
            return

        tier = cfg.leg_selection.tier_map[args.tier]
        opt_type = "put" if args.type == "PUT_CREDIT" else "call"
        print(f"选腿: {args.type} tier={args.tier} → 卖腿|Δ|≈{tier.sell_delta} "
              f"DTE {tier.dte_lo}-{tier.dte_hi}")
        chain = await market_data.option_chain_tickers(
            ws, cfg.strategy.currency, opt_type, tier.dte_lo, tier.dte_hi)
        print(f"到期日: DTE={chain.dte:.1f} (检查了 {chain.checked_expiries} 个, "
              f"区间匹配={chain.target_range_match}), 合约数={len(chain.tickers)}")

        sel = leg_selector.select_spread(
            chain.tickers, args.type, tier.sell_delta,
            sell_delta_min=cfg.leg_selection.sell_delta_min,
            sell_delta_max=cfg.leg_selection.sell_delta_max,
            min_credit_btc=cfg.leg_selection.min_credit_btc,
            risk_reward_max=cfg.leg_selection.risk_reward_max)
        if sel is None:
            print("REJECT_NO_VALID_LEGS: 没有满足约束的腿组合")
            return
        print(f"卖腿: {sel.sell.instrument}  Δ={sel.sell.delta:+.3f}  "
              f"bid/ask {sel.sell.bid}/{sel.sell.ask}")
        print(f"买腿: {sel.buy.instrument}  Δ={sel.buy.delta:+.3f}  "
              f"bid/ask {sel.buy.bid}/{sel.buy.ask}")
        print(f"估算净权利金 {sel.net_credit_est:.4f} BTC/张  "
              f"每张最大亏损 {sel.max_loss_btc:.4f} BTC  盈亏比 {sel.risk_reward:.2f}")

        summ = await account.get_account_summary(ws, cfg.strategy.currency)
        q_budget = leg_selector.contracts_for_budget(
            summ.equity_btc, cfg.risk.account_risk_pct, sel.max_loss_btc,
            cfg.strategy.min_lot)
        print(f"权益 {summ.equity_btc:.4f} BTC → 预算允许 {q_budget:.1f} 张; "
              f"本次使用 --qty {args.qty}")

        idx = await market_data.get_index_price(ws, cfg.strategy.currency)
        res = await adapter.open_credit_spread(
            ws, buy_instrument=sel.buy.instrument, sell_instrument=sel.sell.instrument,
            qty=args.qty, min_credit_btc=cfg.leg_selection.min_credit_btc,
            min_lot=cfg.strategy.min_lot, n_exec_groups=cfg.execution.n_exec_groups,
            attempt_waits=tuple(cfg.execution.attempt_waits),
            report_dir=report_dir, dry_run=not args.real,
            on_event=lambda m: print("  |", m))
        print(f"\n开仓结果: qty={res.qty} net_credit={res.net_credit_btc:.4f} "
              f"combo={res.combo_qty} unwound={res.unwound_qty}")
        if res.opened:
            from src.engine.bep import bep_price
            bep = bep_price(args.type, sel.sell.strike, res.net_credit_btc, idx)
            print(f"BEP = {bep:,.1f} (指数价 {idx:,.1f})")
            print(f"如需平仓: python scripts/manual_open.py --close "
                  f"{sel.sell.instrument} {sel.buy.instrument} --qty {res.qty}")
    finally:
        await ws.close()


if __name__ == "__main__":
    asyncio.run(main())
