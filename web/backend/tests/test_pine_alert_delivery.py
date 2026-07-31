"""Pine 订单成交信号的发送契约。"""

from pathlib import Path


PINE_CHART = (
    Path(__file__).parents[3] / "pine" / "btc_credit_spread_v2_chart.pine"
)
PINE_2H = Path(__file__).parents[3] / "pine" / "btc_credit_spread_v2_2h.pine"
V2_PINES = (PINE_CHART, PINE_2H)
PINE_TEST_A = Path(__file__).parents[3] / "pine" / "tv_strategy_a_standard.pine"
PINE_TEST_B = (
    Path(__file__).parents[3] / "pine" / "tv_strategy_b_relaxed.pine"
)


def test_v2_orders_are_strategy_fills_without_legacy_status_alerts():
    """V2 开平仓只由 strategy 订单成交事件触发。"""
    for path in V2_PINES:
        source = path.read_text(encoding="utf-8")

        assert 'strategy.entry("PUT_CREDIT", strategy.long' in source
        assert 'strategy.entry("CALL_CREDIT", strategy.short' in source
        assert 'alert_message = "OPEN"' in source
        assert 'strategy.close_all(comment = "TV_DVOL_EXIT", alert_message = "CLOSE")' in source
        assert "f_open_json" not in source
        assert "f_exit_json" not in source
        assert "f_status_json" not in source
        assert "heartbeat_on" not in source
        assert "alert(" not in source
        assert "putEntrySignal = barstate.isconfirmed and putSignal" in source
        assert "callEntrySignal = barstate.isconfirmed and callSignal" in source
        assert "closeSignal = barstate.isconfirmed and dvolExitHit" in source
        assert "if putEntrySignal" in source
        assert "if callEntrySignal" in source
        assert "if closeSignal" in source


def test_v2_signal_period_follows_chart_and_marks_orders_on_price_chart():
    """切到 3 分钟图后应按 3 分钟收盘判断，并在主图显示开平仓标记。"""
    source = PINE_CHART.read_text(encoding="utf-8")

    assert "overlay = true" in source
    assert "timeframe.in_seconds()" in source
    assert "timeframe.period" in source
    assert 'request.security(syminfo.tickerid, "120"' not in source
    assert 'plotshape(putEntrySignal, title = "PUT开仓信号"' in source
    assert 'plotshape(callEntrySignal, title = "CALL开仓信号"' in source
    assert 'plotshape(closeSignal, title = "平仓信号"' in source
    assert 'plot(dvolPct, "DVOL分位%", color = color.orange, display = display.data_window)' in source
    assert 'plot(signalHvPct, "HV分位%", color = color.teal, display = display.data_window)' in source


def test_fixed_2h_version_keeps_higher_timeframe_contract_and_unique_name():
    source = PINE_2H.read_text(encoding="utf-8")

    assert 'strategy("BTC期权卖方V2·固定2小时版"' in source
    assert 'syminfo.tickerid, "120"' in source
    assert "newSignalBar =" in source
    assert "putSignal = newSignalBar and cfgOk" in source
    assert 'table.cell(tb, 1, 0, "固定 2H"' in source
    assert "timeframe.in_seconds()" not in source


def test_v2_variants_have_distinct_file_and_strategy_names():
    chart = PINE_CHART.read_text(encoding="utf-8")
    fixed = PINE_2H.read_text(encoding="utf-8")

    assert PINE_CHART.name != PINE_2H.name
    assert 'strategy("BTC期权卖方V2·图表周期版"' in chart
    assert 'strategy("BTC期权卖方V2·固定2小时版"' in fixed


def test_v2_variants_fail_closed_and_explain_dvol_readiness_on_chart():
    for path in V2_PINES:
        source = path.read_text(encoding="utf-8")

        assert "not dvolReady ? \"DVOL历史不足·不开仓\"" in source
        assert 'tier == 0 ? "DVOL低于L1·不开仓"' in source
        assert 'table.cell(tb, 0, 2, "DVOL数据"' in source
        assert '"就绪 " + str.tostring(dvolSamples)' in source
        assert '"不足 " + str.tostring(dvolSamples)' in source
        assert 'table.cell(tb, 0, 7, "当前状态"' in source


def test_dvol_history_is_computed_inside_the_12h_request():
    """730 天窗口不能依赖 3 分钟主图逐根积累。"""
    for path in V2_PINES:
        source = path.read_text(encoding="utf-8")

        assert 'request.security(\n     "DERIBIT:DVOL", "720"' in source
        assert "ta.percentrank(close, entryWinBars)[1]" in source
        assert "ta.percentile_linear_interpolation(close[2], exitWinBars" in source
        assert "calc_bars_count = entryWinBars + 10" in source
        assert "array.new_float()" not in source


def test_v2_pine_has_no_duplicate_identity_or_execution_payload_contract():
    """来源身份与执行参数由 V2 Alert/后端负责，Pine 不再重复维护。"""
    for path in V2_PINES:
        source = path.read_text(encoding="utf-8")

        assert "Alert JSON V2" in source
        assert 'Alert 条件只选择“订单成交”' in source
        assert "passphrase = input." not in source
        assert "strategy_name = input." not in source
        assert "display = display.none" not in source
        for legacy_name in (
            "activeTradeId",
            "activeSellDelta",
            "activeDteMin",
            "activeDteMax",
            "activeTier",
            "selectedSellDelta",
            "selectedDteMin",
            "selectedDteMax",
            "sell_delta_tier_",
            "dte_min_tier_",
            "dte_max_tier_",
            "group_split_count",
            "delta_account_risk_pct",
            "delta_trigger_pct",
            "call_hedge_release_pct",
            "put_hedge_release_pct",
        ):
            assert legacy_name not in source
        assert 'order_size = input.float(' in source
        assert 'qty = order_size' in source
        assert "后端忽略" in source


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
