"""配置加载：.env（密钥/环境选择） + config/base.yaml + config/{env}.yaml 深合并。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

SERVER_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = SERVER_DIR / "config"
DATA_DIR = SERVER_DIR / "data"


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=SERVER_DIR / ".env", extra="ignore")

    deribit_env: str = "testnet"
    deribit_testnet_client_id: str = ""
    deribit_testnet_client_secret: str = ""
    deribit_mainnet_client_id: str = ""
    deribit_mainnet_client_secret: str = ""
    webhook_secret: str = ""

    @property
    def is_testnet(self) -> bool:
        return self.deribit_env.lower() != "mainnet"

    @property
    def client_id(self) -> str:
        return (self.deribit_testnet_client_id if self.is_testnet
                else self.deribit_mainnet_client_id)

    @property
    def client_secret(self) -> str:
        return (self.deribit_testnet_client_secret if self.is_testnet
                else self.deribit_mainnet_client_secret)


class TierRule(BaseModel):
    sell_delta: float
    dte_lo: float
    dte_hi: float


class LegSelectionCfg(BaseModel):
    tier_map: dict[int, TierRule]
    sell_delta_min: float = 0.05
    sell_delta_max: float = 0.70
    risk_reward_max: float = 6.0
    min_credit_btc: float = 0.0005


class RiskCfg(BaseModel):
    account_risk_pct: float = 0.02
    daily_loss_limit_pct: float = 0.05
    margin_ratio_limit: float = 0.60
    quote_missing_scans: int = 5
    delta_anomaly_btc: float = 1.0


class HedgeCfg(BaseModel):
    delta_account_risk_pct: float = 0.02
    delta_trigger_pct: float = 0.10
    call_release_pct: float = 0.98
    put_release_pct: float = 1.02
    perp_instrument: str = "BTC-PERPETUAL"
    perp_contract_usd: float = 10.0
    ioc_ticks: int = 5


class ExitCfg(BaseModel):
    dvol_window_bars: int = 120
    dvol_percentile: float = 30.0
    keep_buy_leg_on_normal_exit: bool = False
    expiry_close_dte: float = 1.0


class ScanCfg(BaseModel):
    interval_sec: int = 180
    bar_align_offset_sec: int = 5


class ExecutionCfg(BaseModel):
    n_exec_groups: int = 5
    attempt_waits: list[float] = [3.0, 8.0, 15.0]


class WebhookCfg(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8300


class StrategyCfg(BaseModel):
    name: str = "BTC_OPTION_SELLER_V2"
    currency: str = "BTC"
    min_lot: float = 0.1
    # 与 Pine 的 dvol_entry_level_1/2/3 对应（0~1）。后端为权威判定方：
    # 收到开仓信号时自行拉全历史计算分位与档位（Pine 3分钟图历史不足）。
    dvol_entry_levels: list[float] = [0.40, 0.60, 0.85]
    dvol_entry_window_days: int = 730


class DebugCfg(BaseModel):
    bep_override: Optional[float] = None
    trigger_override: Optional[float] = None
    dry_run: bool = False


class AppConfig(BaseModel):
    strategy: StrategyCfg
    leg_selection: LegSelectionCfg
    risk: RiskCfg
    hedge: HedgeCfg
    exit: ExitCfg
    scan: ScanCfg
    execution: ExecutionCfg
    group_split_count_default: int = 4
    webhook: WebhookCfg
    debug: DebugCfg

    env: EnvSettings = None  # type: ignore[assignment]

    model_config = {"arbitrary_types_allowed": True}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(env_name: Optional[str] = None) -> AppConfig:
    env = EnvSettings()
    name = env_name or ("testnet" if env.is_testnet else "mainnet")
    with open(CONFIG_DIR / "base.yaml", encoding="utf-8") as f:
        merged: dict[str, Any] = yaml.safe_load(f)
    overlay_path = CONFIG_DIR / f"{name}.yaml"
    if overlay_path.exists():
        with open(overlay_path, encoding="utf-8") as f:
            overlay = yaml.safe_load(f) or {}
        merged = _deep_merge(merged, overlay)
    cfg = AppConfig(**merged)
    cfg.env = env
    if not env.is_testnet:
        # mainnet 禁止 debug 旁路，防止把测试口径带上实盘
        assert cfg.debug.bep_override is None and cfg.debug.trigger_override is None, \
            "mainnet 配置禁止 debug override"
    return cfg


def db_path() -> Path:
    os.makedirs(DATA_DIR, exist_ok=True)
    return DATA_DIR / "seller_v2.db"
