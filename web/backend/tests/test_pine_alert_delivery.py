"""Pine 信号与心跳的发送契约。"""

from pathlib import Path


PINE = Path(__file__).parents[3] / "pine" / "btc_credit_spread_v2.pine"


def test_open_signal_and_status_can_both_fire_on_the_same_chart_bar():
    """条件成立时 OPEN_REQUEST 和 STATUS 必须能在同一根 3m bar 发出。

    alert.freq_once_per_bar 会在首次 alert() 后抑制同 bar 的第二次调用，
    使依赖 STATUS 的推送看门狗恰好看不到真正的信号 bar。
    """
    source = PINE.read_text(encoding="utf-8")

    assert 'alert(f_open_json("PUT_CREDIT"), alert.freq_all)' in source
    assert 'alert(f_open_json("CALL_CREDIT"), alert.freq_all)' in source
    assert "alert(f_exit_json(), alert.freq_all)" in source
    assert "alert(f_status_json(), alert.freq_all)" in source
    assert "if barstate.isconfirmed and putSignal and canFire" in source
    assert "if barstate.isconfirmed and callSignal and canFire" in source
    assert "if barstate.isconfirmed and dvolExitHit" in source
    assert "varip int lastStatusT = na" in source
    assert "statusCanFire = na(lastStatusT) or t2 != lastStatusT" in source
    assert (
        "if barstate.isconfirmed and newBar2h and heartbeat_on and statusCanFire"
        in source
    )
    assert "lastStatusT := t2" in source
