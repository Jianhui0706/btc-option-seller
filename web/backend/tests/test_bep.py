"""BEP 计算与激活/撤销判定测试（规范第十一、十三节）。"""

import pytest

from src.engine.bep import bep_price, bep_touched, release_line, release_touched


def test_call_bep():
    # Call BEP = 卖腿行权价 + 净权利金(USD)
    assert bep_price("CALL_CREDIT", 70000, 0.005, 66000) == 70000 + 0.005 * 66000


def test_put_bep():
    assert bep_price("PUT_CREDIT", 62000, 0.0064, 65747) == pytest.approx(61579.2, abs=0.1)


def test_bad_type():
    with pytest.raises(ValueError):
        bep_price("STRADDLE", 1, 1, 1)


def test_call_activation():
    bep = 70330.0
    assert not bep_touched("CALL_CREDIT", 70329.9, bep)
    assert bep_touched("CALL_CREDIT", 70330.0, bep)
    assert bep_touched("CALL_CREDIT", 71000.0, bep)


def test_put_activation():
    bep = 61579.2
    assert not bep_touched("PUT_CREDIT", 61580.0, bep)
    assert bep_touched("PUT_CREDIT", 61579.2, bep)
    assert bep_touched("PUT_CREDIT", 61000.0, bep)


def test_call_release():
    bep = 70000.0
    assert release_line("CALL_CREDIT", bep) == pytest.approx(68600.0)
    assert release_touched("CALL_CREDIT", 68600.0, bep)      # 回落到线内
    assert not release_touched("CALL_CREDIT", 68601.0, bep)  # 仍在线外


def test_put_release():
    bep = 60000.0
    assert release_line("PUT_CREDIT", bep) == pytest.approx(61200.0)
    assert release_touched("PUT_CREDIT", 61200.0, bep)
    assert not release_touched("PUT_CREDIT", 61199.0, bep)
