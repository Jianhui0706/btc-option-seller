"""TradingView 参数透传与安全范围测试。"""

from src.webhook import TVSignal, _payload_sane


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


def test_strategy_name_is_preserved_for_backend_validation():
    assert _signal(strategy="OTHER").strategy == "OTHER"
