"""DVOL 滚动 30 分位退出测试（规范第八节，防前视）。"""

from src.engine.indicators import (dvol_entry_tier, dvol_exit_triggered,
                                   percentile_rank, quantile)


def test_insufficient_samples():
    triggered, level = dvol_exit_triggered([50.0] * 100, window=120)
    assert not triggered and level is None


def test_triggered_when_below_30pct():
    # 前 120 个值均匀 40..99.x，30 分位 ≈ 57.9；当前值 41 → 触发
    window = [40.0 + i * 0.5 for i in range(120)]
    triggered, level = dvol_exit_triggered(window + [41.0], window=120)
    assert triggered
    assert 55.0 < level < 60.0


def test_not_triggered_at_high_value():
    window = [40.0 + i * 0.5 for i in range(120)]
    triggered, _ = dvol_exit_triggered(window + [90.0], window=120)
    assert not triggered


def test_current_bar_not_in_reference_window():
    # 当前值不参与阈值计算：120 个 50 + 当前 30 → 阈值应为 50（而非被 30 拉低）
    triggered, level = dvol_exit_triggered([50.0] * 120 + [30.0], window=120)
    assert triggered
    assert level == 50.0


def test_quantile_interpolation():
    assert quantile([1, 2, 3, 4], 0.5) == 2.5
    assert quantile([10], 0.3) == 10


def test_percentile_rank_midrank():
    # 平坦序列中位秩 = 50，不虚高到 100（V1 口径）
    assert percentile_rank([5.0] * 10) == 50.0
    assert percentile_rank(list(range(10))) == 95.0


def test_entry_requires_full_window():
    assert dvol_entry_tier([40.0] * 119, 120, [0.4, 0.6, 0.85]) == (None, 0)


def test_entry_tier_with_full_window():
    values = list(range(119)) + [1000.0]
    pct, tier = dvol_entry_tier(values, 120, [0.4, 0.6, 0.85])
    assert pct > 0.99
    assert tier == 3
