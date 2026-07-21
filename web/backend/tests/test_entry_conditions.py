"""开仓条件看板：指标口径与门槛判定测试。

指标必须与 Pine 同口径（总体标准差、ta.cci 的平均绝对偏差），
否则看板会告诉用户"能发信号"而 TradingView 实际不发。
"""

import asyncio
import json
import math
from types import SimpleNamespace

from src.engine import entry_conditions as ec
from src.exchange.market_data import Candle
from src.webhook import PineStatus, _handle_status


def _cfg(**overrides):
    entry = {"boll_len": 20, "boll_mult": 2.0, "cci_len": 20, "cci_max": 101.0,
             "hv_len": 20, "hv_pctl_window": 252, "hv_pctl_min": 30.0,
             "snapshot_ttl_sec": 300}
    entry.update(overrides)
    return SimpleNamespace(
        entry_signal=SimpleNamespace(**entry),
        strategy=SimpleNamespace(dvol_entry_levels=[0.40, 0.60, 0.85],
                                 dvol_entry_window_days=730))


def test_cci_matches_pine_mean_absolute_deviation():
    # closes 1..20: sma=10.5, 平均绝对偏差=5.0 → (20-10.5)/(0.015*5)
    closes = [float(i) for i in range(1, 21)]
    assert ec.cci(closes, 20) == 9.5 / (0.015 * 5.0)


def test_bollinger_uses_population_stdev_like_pine():
    closes = [float(i) for i in range(1, 21)]
    mid, upper, lower = ec.bollinger(closes, 20, 2.0)
    expected_sd = math.sqrt((20 ** 2 - 1) / 12)      # 1..n 总体方差
    assert mid == 10.5
    assert upper == 10.5 + 2 * expected_sd
    assert lower == 10.5 - 2 * expected_sd


def test_indicators_return_none_when_sample_short():
    assert ec.cci([1.0, 2.0], 20) is None
    assert ec.bollinger([1.0, 2.0], 20, 2.0) is None
    assert ec.hv_annualized([1.0] * 10, 20) is None


def test_flat_series_gives_zero_hv():
    assert ec.hv_annualized([100.0] * 25, 20) == 0.0


def _evaluate(bar, closes, cfg=None, dvol_pct=0.7, tier=2):
    return ec.evaluate(bar, closes, dvol_pct, tier, 1460, cfg or _cfg())


def test_put_shape_needs_break_below_band_and_bullish_close():
    closes = [100.0] * 19 + [100.0]
    band = ec.bollinger(closes, 20, 2.0)
    assert band is not None and band[2] == 100.0          # 无波动时上下轨重合
    # 最低价跌破下轨 + 收阳
    hit = _evaluate(Candle(0, 99.0, 101.0, 98.0, 100.0), closes)
    assert hit["checks"]["shape"]["put"] and not hit["checks"]["shape"]["call"]
    # 同样破轨但收阴 → 不成立
    miss = _evaluate(Candle(0, 100.0, 101.0, 98.0, 99.0), closes)
    assert not miss["checks"]["shape"]["put"]


def test_high_cci_blocks_signal_even_with_valid_shape():
    """截图里的实际情形：CCI 141 > 101，形态成立也不该发信号。"""
    closes = [float(i) for i in range(1, 21)]           # CCI ≈ 126.7
    result = _evaluate(Candle(0, 1.0, 5.0, -50.0, 21.0), closes)
    assert result["checks"]["cci"]["ok"] is False
    assert result["checks"]["shape"]["put"] is True
    assert result["can_fire"] is False
    assert "cci" in result["blocking"]


def test_tier_minus_one_does_not_block_but_tier_zero_does():
    closes = [100.0] * 20
    bar = Candle(0, 100.0, 100.0, 100.0, 100.0)
    assert ec.evaluate(bar, closes, None, -1, 88, _cfg())["checks"]["dvol_tier"]["ok"]
    assert not ec.evaluate(bar, closes, 0.1, 0, 1460, _cfg())["checks"]["dvol_tier"]["ok"]


def test_config_gate_fails_when_levels_not_ascending():
    cfg = _cfg()
    cfg.strategy.dvol_entry_levels = [0.60, 0.40, 0.85]
    result = _evaluate(Candle(0, 100.0, 100.0, 100.0, 100.0), [100.0] * 20, cfg)
    assert result["checks"]["config"]["ok"] is False
    assert result["can_fire"] is False


def test_hv_gate_needs_full_percentile_window():
    """窗口没填满就不该给出 HV 分位 —— 短窗口分位没有可比性。"""
    cfg = _cfg(hv_pctl_window=50)
    closes = [100.0 + i % 7 for i in range(40)]
    result = _evaluate(Candle(0, 100.0, 100.0, 100.0, 100.0), closes, cfg)
    assert result["checks"]["hv"]["value"] is None
    assert result["checks"]["hv"]["ok"] is False


# ---------------------------------------------------------------- Pine 心跳

class _FakeRepo:
    def __init__(self):
        self.state = {}

    async def set_state(self, key, value):
        self.state[key] = value


def _status_body(**overrides):
    body = {"passphrase": "secret", "event": "STATUS", "bar_ts": 1753000000000,
            "cci": 141.1, "hv_percentile": 0.827, "dvol_tier": -1,
            "cfg_ok": True, "tier_ok": True, "hv_ok": True, "cci_ok": False,
            "put_shape": False, "call_shape": False}
    body.update(overrides)
    return body


def test_heartbeat_stored_without_passphrase():
    repo = _FakeRepo()
    ctx = SimpleNamespace(repo=repo)
    result = asyncio.run(_handle_status(ctx, _status_body(), "secret"))
    assert result["status"] == "ok"
    saved = json.loads(repo.state["pine_status"])
    assert "passphrase" not in saved
    assert saved["cci"] == 141.1 and saved["cci_ok"] is False
    assert saved["received_at"] > 0


def test_heartbeat_rejects_wrong_passphrase_and_stores_nothing():
    repo = _FakeRepo()
    ctx = SimpleNamespace(repo=repo)
    result = asyncio.run(_handle_status(ctx, _status_body(), "other"))
    assert result == {"status": "rejected", "reason": "auth"}
    assert repo.state == {}


def test_heartbeat_accepts_json_null_from_pine():
    """Pine 样本不足时字段发 null，模型必须容忍而不是整条丢弃。"""
    status = PineStatus(**_status_body(cci=None, hv_percentile=None, boll_upper=None))
    assert status.cci is None and status.boll_upper is None
