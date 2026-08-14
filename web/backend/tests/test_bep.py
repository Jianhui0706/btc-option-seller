"""BEP 计算与激活/撤销判定测试（规范第十一、十三节）。"""

import pytest

from src.engine.bep import (activation_price, activation_touched, bep_price,
                            progress_release_line, progress_release_touched,
                            recovery_release_line,
                            release_line, release_touched)


def test_call_bep():
    # Call BEP = 卖腿行权价 + 净权利金(USD)
    assert bep_price("CALL_CREDIT", 70000, 0.005, 66000) == 70000 + 0.005 * 66000


def test_put_bep():
    assert bep_price("PUT_CREDIT", 62000, 0.0064, 65747) == pytest.approx(61579.2, abs=0.1)


def test_bad_type():
    with pytest.raises(ValueError):
        bep_price("STRADDLE", 1, 1, 1)


def test_call_activation_defaults_to_bep_per_main_strategy_doc():
    trigger = activation_price(66000.0, 70330.0)
    assert trigger == pytest.approx(70330.0)
    assert not activation_touched("CALL_CREDIT", 70329.9, trigger)
    assert activation_touched("CALL_CREDIT", 70330.0, trigger)
    assert activation_touched("CALL_CREDIT", 71000.0, trigger)


def test_put_activation_defaults_to_bep_per_main_strategy_doc():
    trigger = activation_price(65747.0, 61579.2)
    assert trigger == pytest.approx(61579.2)
    assert not activation_touched("PUT_CREDIT", 61579.3, trigger)
    assert activation_touched("PUT_CREDIT", 61579.2, trigger)
    assert activation_touched("PUT_CREDIT", 61000.0, trigger)


def test_activation_progress_allows_documented_zero_to_two_range():
    assert activation_price(96000.0, 100000.0, 0.0) == pytest.approx(96000.0)
    assert activation_price(96000.0, 100000.0, 1.5) == pytest.approx(102000.0)
    assert activation_price(96000.0, 100000.0, 2.0) == pytest.approx(104000.0)
    with pytest.raises(ValueError):
        activation_price(65000.0, 62000.0, 2.01)


def test_call_release():
    trigger = 68165.0
    line = trigger * 0.98
    assert release_line("CALL_CREDIT", trigger) == pytest.approx(line)
    assert release_touched("CALL_CREDIT", line, trigger)      # 回落到线内
    assert not release_touched("CALL_CREDIT", line + 0.1, trigger)


def test_put_release():
    trigger = 63663.1
    line = trigger * 1.02
    assert release_line("PUT_CREDIT", trigger) == pytest.approx(line)
    assert release_touched("PUT_CREDIT", line, trigger)
    assert not release_touched("PUT_CREDIT", line - 0.1, trigger)


def test_release_uses_five_percent_of_open_to_bep_progress():
    assert progress_release_line(66000.0, 70000.0, 0.05) == 66200.0
    assert progress_release_touched(
        "CALL_CREDIT", 66200.0, 66000.0, 70000.0, 0.05)
    assert not progress_release_touched(
        "CALL_CREDIT", 66200.1, 66000.0, 70000.0, 0.05)

    assert progress_release_line(66000.0, 62000.0, 0.05) == 65800.0
    assert progress_release_touched(
        "PUT_CREDIT", 65800.0, 66000.0, 62000.0, 0.05)
    assert not progress_release_touched(
        "PUT_CREDIT", 65799.9, 66000.0, 62000.0, 0.05)


def test_recovery_release_line_matches_reference_beta_for_call_and_put():
    assert recovery_release_line(96000, 100000, 0.50) == pytest.approx(98000)
    assert recovery_release_line(96000, 92000, 0.50) == pytest.approx(94000)
