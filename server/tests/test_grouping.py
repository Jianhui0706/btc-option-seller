"""分组拆分性质测试（规范第七节）。"""

import pytest

from src.engine.grouping import split_groups


def test_spec_example():
    # 文档例：总量 10 张拆 4 组 → 3/3/2/2
    assert split_groups(1.0, 4, 0.1) == [0.3, 0.3, 0.2, 0.2]


def test_no_split():
    assert split_groups(1.0, 1) == [1.0]


def test_effective_group_count_capped_by_lots():
    # 0.3 张拆 5 组 → 只能 3 组
    assert split_groups(0.3, 5) == [0.1, 0.1, 0.1]


@pytest.mark.parametrize("total,n", [(0.1, 1), (0.5, 2), (2.3, 4), (10.0, 5),
                                     (0.7, 7), (23.3, 4), (1.1, 3)])
def test_properties(total, n):
    groups = split_groups(total, n)
    assert round(sum(groups), 10) == round(total, 10)          # 总量守恒
    assert all(g >= 0.1 - 1e-9 for g in groups)                # 每组 ≥ 最小手
    assert max(groups) - min(groups) <= 0.1 + 1e-9             # 组差 ≤ 1 手
    assert groups == sorted(groups, reverse=True)              # 前面组优先 +1


def test_empty_and_invalid():
    assert split_groups(0.0, 4) == []
    with pytest.raises(ValueError):
        split_groups(1.0, 0)
