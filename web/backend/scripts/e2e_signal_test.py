"""M3 端到端测试：模拟 TradingView 依次发送各类信号，验证 webhook 链路。

用法（server/ 目录，先启动后端）:
  python scripts/e2e_signal_test.py            # 拒绝路径测试
  python scripts/e2e_signal_test.py --open     # 真实开仓信号（要求账户无卖方持仓）
  python scripts/e2e_signal_test.py --exit     # DVOL 退出信号（平掉当前持仓）
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

sys.stdout.reconfigure(encoding="utf-8")
BASE = "http://127.0.0.1:8300"
SERVER_DIR = Path(__file__).resolve().parent.parent


def secret(*names: str) -> str:
    values = {}
    for line in (SERVER_DIR / ".env").read_text(encoding="utf-8-sig").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    for name in names:
        if values.get(name):
            return values[name]
    raise RuntimeError(f"none of {names!r} found in .env")


def webhook_secret() -> str:
    return secret("WEBHOOK_PASSPHRASE", "WEBHOOK_SECRET")


def sig(event="OPEN_REQUEST", spread="PUT_CREDIT", tier=2, pct=0.67, sid=None,
        passphrase=None) -> dict:
    return {
        "passphrase": passphrase if passphrase is not None else webhook_secret(),
        "event": event,
        "strategy": "BTC_OPTION_SELLER_V2",
        "spread_type": spread,
        "signal_id": sid or f"TEST_{event}_{spread}_{int(time.time())}",
        "btc_price": 65650.0,
        "hv_percentile": 0.48,
        "dvol_percentile": pct,
        "dvol_tier": tier,
        "group_split_count": 4,
    }


def post(payload: dict) -> dict:
    r = httpx.post(f"{BASE}/webhook/tradingview", json=payload, timeout=10)
    return r.json()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--exit", action="store_true")
    args = ap.parse_args()

    httpx.post(f"{BASE}/control/trading", params={"enabled": "true"}, timeout=10)

    if args.exit:
        print("EXIT_DVOL →", post(sig(event="EXIT_DVOL", spread=None)))
        return

    if args.open:
        s = sig()
        print(f"OPEN {s['signal_id']} →", post(s))
        print("（观察后端日志，等待执行完成后查询 /status）")
        return

    print("== 1) 错误 passphrase →", post(sig(passphrase="wrong")))
    print("== 2) 档位与分位不自洽 (tier=3, pct=0.50) →", post(sig(tier=3, pct=0.50)))
    print("== 3) 非法 spread_type →", post(sig(spread="IRON_CONDOR")))
    s = sig(sid=f"TEST_DUP_{int(time.time())}")
    print("== 4) 正常开仓信号（预期后端拒绝：已有卖方持仓）→", post(s))
    time.sleep(4)   # 等 worker 处理
    print("== 5) 重复 signal_id →", post(s))
    print("\n最近事件:")
    for e in httpx.get(f"{BASE}/events", params={"limit": 8}, timeout=10).json():
        print(f"  [{e['level']}] {e['code']}  {e.get('payload_json') or ''}"[:150])


if __name__ == "__main__":
    main()
