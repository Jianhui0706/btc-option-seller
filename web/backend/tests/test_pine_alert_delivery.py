"""Pine 信号与心跳的发送契约。"""

from pathlib import Path


PINE = Path(__file__).parents[3] / "pine" / "btc_credit_spread_v2.pine"
PINE_TEST_A = Path(__file__).parents[3] / "pine" / "tv_strategy_a_standard.pine"
PINE_TEST_B = (
    Path(__file__).parents[3] / "pine" / "tv_strategy_b_relaxed.pine"
)


def test_orders_are_strategy_fills_and_status_remains_an_alert_call():
    """开平仓由 strategy 订单成交事件触发，STATUS 继续由 alert() 回报。"""
    source = PINE.read_text(encoding="utf-8")

    assert 'strategy.entry("PUT_CREDIT", strategy.long' in source
    assert 'strategy.entry("CALL_CREDIT", strategy.short' in source
    assert 'alert_message = "OPEN"' in source
    assert 'strategy.close_all(comment = "TV_DVOL_EXIT", alert_message = "CLOSE")' in source
    assert "activeTradeId := t2" in source
    assert "activeSellDelta := selectedSellDelta" in source
    assert 'plot(activeTradeId, "trade_id", display = display.none)' in source
    assert 'plot(activeSellDelta, "sell_delta", display = display.none)' in source
    assert "f_open_json" not in source
    assert "f_exit_json" not in source
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


def test_dvol_history_is_computed_inside_the_12h_request():
    """730 天窗口不能依赖 3 分钟主图逐根积累。"""
    source = PINE.read_text(encoding="utf-8")

    assert 'request.security(\n     "DERIBIT:DVOL", "720"' in source
    assert "ta.percentrank(close, entryWinBars)[1]" in source
    assert "ta.percentile_linear_interpolation(close[2], exitWinBars" in source
    assert "calc_bars_count = entryWinBars + 10" in source
    assert "array.new_float()" not in source


def test_alert_plot_indexes_keep_the_json_contract_order():
    """控制台使用 plot_2..plot_10；这些隐藏 plot 的顺序不可漂移。"""
    source = PINE.read_text(encoding="utf-8")
    expected = [
        'plot(activeTradeId, "trade_id", display = display.none)',
        'plot(order_size, "size", display = display.none)',
        'plot(activeSellDelta, "sell_delta", display = display.none)',
        'plot(activeDteMin, "dte_min", display = display.none)',
        'plot(activeDteMax, "dte_max", display = display.none)',
        'plot(activeTier, "dvol_tier", display = display.none)',
        'plot(group_split_count, "group_split_count", display = display.none)',
        'plot(delta_account_risk_pct / 100.0, "delta_account_risk_pct", display = display.none)',
        'plot(delta_trigger_pct / 100.0, "delta_trigger_pct", display = display.none)',
        'plot(call_hedge_release_pct / 100.0, "call_hedge_release_pct", display = display.none)',
        'plot(put_hedge_release_pct / 100.0, "put_hedge_release_pct", display = display.none)',
    ]
    offsets = [source.index(line) for line in expected]
    assert offsets == sorted(offsets)


def test_two_multi_strategy_pines_are_standard_and_relaxed_variants():
    standard = PINE_TEST_A.read_text(encoding="utf-8")
    relaxed = PINE_TEST_B.read_text(encoding="utf-8")

    assert 'strategy_name = input.string("BTC期权卖方_项目规范策略A"' in standard
    assert 'strategy_name = input.string("BTC期权卖方_宽松对照策略B"' in relaxed
    assert 'input.float(40.0, "dvol_entry_level_1 (%)"' in standard
    assert 'input.float(60.0, "dvol_entry_level_2 (%)"' in standard
    assert 'input.float(85.0, "dvol_entry_level_3 (%)"' in standard
    assert 'input.float(101.0, "abs(CCI) 上限"' in standard
    assert 'input.float(30.0, "HV 分位下限 (%)"' in standard
    assert 'input.float(35.0, "dvol_entry_level_1 (%) · 宽松"' in relaxed
    assert 'input.float(55.0, "dvol_entry_level_2 (%) · 宽松"' in relaxed
    assert 'input.float(80.0, "dvol_entry_level_3 (%) · 宽松"' in relaxed
    assert 'input.float(120.0, "abs(CCI) 上限 · 宽松"' in relaxed
    assert 'input.float(25.0, "HV 分位下限 (%) · 宽松"' in relaxed
    for source in (standard, relaxed):
        assert 'enable_order_signals = input.bool(false' in source
        assert "putSignal = newBar2h and cfgOk and tierOk and hvOk and cciOk and putShape" in source
        assert "callSignal = newBar2h and cfgOk and tierOk and hvOk and cciOk and callShape" in source
        assert "barstate.isconfirmed and enable_order_signals and putSignal" in source
        assert "barstate.isconfirmed and enable_order_signals and callSignal" in source
        assert "barstate.isconfirmed and enable_order_signals and dvolExitHit" in source
        assert 'alert_message = "OPEN"' in source
        assert 'alert_message = "CLOSE"' in source
        assert "realtimeStatusSent = false" in source
        assert "alert(f_status_json(), alert.freq_all)" in source


def test_two_multi_strategy_pines_keep_the_numbered_plot_contract():
    expected_order = [
        '"trade_id"',
        '"size"',
        '"sell_delta"',
        '"dte_min"',
        '"dte_max"',
        '"dvol_tier"',
        '"group_split_count"',
        '"delta_account_risk_pct"',
        '"delta_trigger_pct"',
        '"call_hedge_release_pct"',
        '"put_hedge_release_pct"',
    ]
    for path in (PINE_TEST_A, PINE_TEST_B):
        source = path.read_text(encoding="utf-8")
        plot_block = source[source.index('plot(activeTradeId, "trade_id"'):]
        offsets = [plot_block.index(name) for name in expected_order]
        assert offsets == sorted(offsets)
