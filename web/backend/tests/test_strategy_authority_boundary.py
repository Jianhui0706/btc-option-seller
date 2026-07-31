"""TradingView 负责策略判断，后端只做安全执行。"""

from pathlib import Path


ROOT = Path(__file__).parents[3]
WEBHOOK = ROOT / "web" / "backend" / "src" / "webhook.py"
ORCHESTRATOR = ROOT / "web" / "backend" / "src" / "engine" / "orchestrator.py"
ENTRY_CONDITIONS = (
    ROOT / "web" / "backend" / "src" / "engine" / "entry_conditions.py"
)
MARKET_DATA = ROOT / "web" / "backend" / "src" / "exchange" / "market_data.py"


def test_generic_v2_payload_drops_strategy_indicator_values():
    """V2 信号不能把旧版 DVOL/HV 参数变成后端策略输入。"""
    source = WEBHOOK.read_text(encoding="utf-8")

    assert '"dvol_tier": None if is_v2 else sig.dvol_tier' in source
    assert '"dvol_percentile": None if is_v2 else sig.dvol_percentile' in source
    assert '"group_split_count": None if is_v2 else sig.group_split_count' in source


def test_order_orchestrator_does_not_import_or_recompute_entry_conditions():
    """订单执行器不得调用只读条件诊断模块进行二次审批。"""
    source = ORCHESTRATOR.read_text(encoding="utf-8")

    assert "entry_conditions" not in source
    assert "fetch_dvol" not in source
    assert "REJECT_DVOL_BELOW_ENTRY" not in source
    assert "后端不再拉行情重算档位" in source


def test_backend_dvol_modules_are_explicitly_diagnostic_only():
    entry = ENTRY_CONDITIONS.read_text(encoding="utf-8")
    market = MARKET_DATA.read_text(encoding="utf-8")

    assert "只读诊断用 DVOL 分位" in entry
    assert "不得用于 V2 订单审批" in entry
    assert "用于面板核对 TradingView 数据完整性" in market
    assert "不得接入 V2 开仓批准或拒绝链路" in market
