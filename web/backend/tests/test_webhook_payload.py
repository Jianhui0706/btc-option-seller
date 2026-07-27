"""TradingView 参数透传与安全范围测试。"""

from src.webhook import TVSignal, _normalise_signal, _payload_sane


def _signal(**overrides):
    payload = {
        "passphrase": "x",
        "event": "OPEN_REQUEST",
        "strategy": "BTC_OPTION_SELLER_V2",
        "signal_id": "s-1",
        "spread_type": "PUT_CREDIT",
        "dvol_percentile": 0.67,
        "dvol_tier": 2,
        "group_split_count": 4,
        "delta_account_risk_pct": 0.02,
        "delta_trigger_pct": 0.10,
        "call_hedge_release_pct": 0.98,
        "put_hedge_release_pct": 1.02,
    }
    payload.update(overrides)
    return TVSignal(**payload)


def test_hedge_parameters_survive_model_dump():
    signal = _signal()
    assert _payload_sane(signal)
    dumped = signal.model_dump(exclude={"passphrase"})
    assert dumped["delta_account_risk_pct"] == 0.02
    assert dumped["delta_trigger_pct"] == 0.10
    assert dumped["call_hedge_release_pct"] == 0.98
    assert dumped["put_hedge_release_pct"] == 1.02


def test_invalid_runtime_parameters_rejected():
    assert not _payload_sane(_signal(group_split_count=0))
    assert not _payload_sane(_signal(delta_trigger_pct=1.1))
    assert not _payload_sane(_signal(call_hedge_release_pct=1.01))
    assert not _payload_sane(_signal(put_hedge_release_pct=0.99))


def test_strategy_name_is_preserved_for_multi_strategy_attribution():
    assert _signal(strategy="OTHER").strategy == "OTHER"


def test_chinese_strategy_name_is_preserved_in_signal_identity():
    normalized = _normalise_signal(_signal(
        strategy="BTC期权卖方_项目规范策略A",
        order_id="PUT_ENTRY",
        bar_time=1784851200000,
    ))

    assert normalized["strategy"] == "BTC期权卖方_项目规范策略A"
    assert normalized["signal_id"].startswith(
        "BTC期权卖方_项目规范策略A:PUT_ENTRY:OPEN:")


def test_same_strategy_order_action_and_bar_share_canonical_signal_id():
    first = _normalise_signal(_signal(
        signal_id="caller-generated-1",
        tv_id=1001,
        order_id="PUT_ENTRY",
        bar_time=1784851200000,
        timestamp=1784851201000,
    ))
    second = _normalise_signal(_signal(
        signal_id="caller-generated-2",
        tv_id=1002,
        order_id="PUT_ENTRY",
        bar_time=1784851200000,
        timestamp=1784851209000,
    ))

    expected = "BTC_OPTION_SELLER_V2:PUT_ENTRY:OPEN:1784851200000"
    assert first["signal_id"] == expected
    assert second["signal_id"] == expected
    assert first["trade_id"] == "1001"
    assert second["trade_id"] == "1002"


def test_numeric_strings_from_valid_json_alert_are_coerced():
    signal = _signal(
        size="0.1",
        sell_delta="0.15",
        dte_min="30",
        dte_max="45",
        dvol_tier="1",
        group_split_count="4",
        delta_account_risk_pct="0.02",
    )
    normalized = _normalise_signal(signal)

    assert normalized["quantity"] == 0.1
    assert normalized["sell_delta"] == 0.15
    assert normalized["dte_min"] == 30
    assert normalized["dte_max"] == 45
    assert normalized["dvol_tier"] == 1
    assert normalized["group_split_count"] == 4
    assert normalized["delta_account_risk_pct"] == 0.02
