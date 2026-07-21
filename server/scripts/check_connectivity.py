"""M1 连通性验证：testnet 认证 + 拉权益/期权持仓/期货持仓 + 指数价 + 12h DVOL。

用法（在 server/ 目录下）: python scripts/check_connectivity.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")  # Windows GBK 控制台下打印中文/符号
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config
from src.exchange.deribit_ws import DeribitWS, TESTNET_URL, MAINNET_URL
from src.exchange import account, market_data


async def main() -> None:
    cfg = load_config()
    env = "TESTNET" if cfg.env.is_testnet else "MAINNET"
    url = TESTNET_URL if cfg.env.is_testnet else MAINNET_URL
    print(f"[1/5] 环境: {env}  ({url})")
    assert cfg.env.client_id and cfg.env.client_secret, ".env 缺少 client id/secret"

    ws = DeribitWS(url=url, client_id=cfg.env.client_id, client_secret=cfg.env.client_secret)
    await ws.connect()
    print(f"[2/5] 认证: {'成功' if ws.authenticated else '失败'}")

    summ = await account.get_account_summary(ws, cfg.strategy.currency)
    print(f"[3/5] 账户权益: {summ.equity_btc:.6f} BTC  "
          f"维持保证金率: {summ.margin_ratio:.2%}  可用: {summ.available_funds:.6f}")

    opts = await account.get_option_positions(ws, cfg.strategy.currency)
    futs = await account.get_future_positions(ws, cfg.strategy.currency)
    shorts = [p for p in opts if p.is_short]
    print(f"[4/5] 期权持仓: {len(opts)} 笔（卖方 {len(shorts)} 笔）  期货持仓: {len(futs)} 笔")
    for p in opts:
        print(f"       option {p.instrument}  size={p.size:+.1f}  delta={p.delta:+.4f}")
    for f in futs:
        print(f"       future {f.instrument}  usd={f.size_usd:+.0f}  delta={f.delta_btc:+.4f}")

    idx = await market_data.get_index_price(ws, cfg.strategy.currency)
    dvol = await market_data.fetch_dvol_12h(ws, cfg.strategy.currency, points=130)
    done = [b for b in dvol if b.completed()]
    print(f"[5/5] BTC 指数价: {idx:,.1f}  12h DVOL bar: 共 {len(dvol)} 根 "
          f"(已完成 {len(done)} 根, 最新完成值 {done[-1].close if done else float('nan'):.2f})")

    await ws.close()
    print("连通性验证通过 ✓")


if __name__ == "__main__":
    asyncio.run(main())
