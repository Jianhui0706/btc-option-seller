"""配置加载：.env（密钥/环境选择） + config/base.yaml + config/{env}.yaml 深合并。"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, Field
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
    webhook_passphrase: str = ""
    trading_account_name: str = "account_1"
    # 旧版兼容：新变量未配置时临时回退，云端应使用上面两枚独立密钥。
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

    @property
    def webhook_auth_token(self) -> str:
        return self.webhook_passphrase or self.webhook_secret

class TierRule(BaseModel):
    sell_delta: float
    dte_lo: float
    dte_hi: float


class LegSelectionCfg(BaseModel):
    tier_map: dict[int, TierRule]
    sell_delta_min: float = 0.05
    sell_delta_max: float = 0.70
    sell_delta_search_radius: float = 0.10
    min_credit_btc: float = 0.0005


class RiskCfg(BaseModel):
    account_risk_pct: float = 0.02
    portfolio_risk_pct: float = Field(default=0.06, gt=0.0, le=1.0)
    daily_loss_limit_pct: float = 0.05
    margin_ratio_limit: float = 0.60
    quote_missing_scans: int = 5
    delta_anomaly_btc: float = 1.0
    auto_pause_enabled: bool = False


class HedgeSideCfg(BaseModel):
    """PUT/CALL 独立对冲决策参数；交易所永续仓位仍按净额执行。"""
    rebalance_outer_equity_pct: float = Field(default=0.02, gt=0.0, le=0.20)
    min_rebalance_usd: float = Field(default=5000.0, ge=10.0)
    rebalance_cooldown_sec: float = Field(default=360.0, ge=0.0)
    emergency_trigger_multiplier: float = Field(default=2.0, ge=1.0)


class HedgeCfg(BaseModel):
    delta_account_risk_pct: float = 0.02
    delta_trigger_pct: float = 0.10
    rebalance_outer_equity_pct: float = Field(default=0.02, gt=0.0, le=1.0)
    rebalance_inner_ratio: float = Field(default=0.0, ge=0.0, lt=1.0)
    min_rebalance_usd: float = Field(default=5000.0, ge=0.0)
    rebalance_cooldown_sec: float = Field(default=360.0, ge=0.0)
    emergency_trigger_multiplier: float = Field(default=2.0, ge=1.0)
    hedge_scope: Literal["ACCOUNT_NET", "SIDE_BOOK"] = "SIDE_BOOK"
    review_bar_minutes: Literal[1, 3, 5, 10, 15, 30, 60] = 1
    release_confirmation_bars: int = Field(default=1, ge=1, le=10)
    activation_price_source: Literal[
        "REALTIME_INDEX", "COMPLETED_REVIEW_BAR"
    ] = "REALTIME_INDEX"
    bep_activation_progress: float = Field(default=1.00, ge=0.0, le=2.0)
    release_recovery_pct: float = Field(default=0.50, ge=0.0, le=1.0)
    hedge_release_progress: float = Field(default=0.05, ge=0.0, le=1.0)
    call_release_pct: float = 0.98
    put_release_pct: float = 1.02
    perp_instrument: str = "BTC-PERPETUAL"
    perp_contract_usd: float = 10.0
    ioc_ticks: int = 5
    retry_rounds: int = Field(default=3, ge=1, le=8)
    retry_delay_sec: float = Field(default=1.5, ge=0.0, le=30.0)
    retry_tick_step: int = Field(default=5, ge=0, le=100)
    degraded_retry_sec: float = Field(default=15.0, ge=1.0, le=300.0)
    manual_rearm_sec: float = Field(default=10.0, ge=0.0, le=3600.0)
    put: HedgeSideCfg = HedgeSideCfg()
    call: HedgeSideCfg = HedgeSideCfg()


class ExitCfg(BaseModel):
    dvol_window_bars: int = 120
    dvol_percentile: float = 30.0
    keep_buy_leg_on_normal_exit: bool = False
    expiry_policy: Literal["HOLD_TO_EXPIRY", "CLOSE_BEFORE_EXPIRY"] = "HOLD_TO_EXPIRY"
    expiry_close_dte: float = 1.0


class ScanCfg(BaseModel):
    interval_sec: int = 180
    bar_align_offset_sec: int = 5


class EntrySignalCfg(BaseModel):
    """必须与 Pine grpSig 的同名 input 保持一致，否则诊断面板会和真实信号打架。"""
    boll_len: int = 20
    boll_mult: float = 2.0
    cci_len: int = 20
    cci_max: float = 101.0
    hv_len: int = 20
    hv_pctl_window: int = 252
    hv_pctl_min: float = 30.0
    snapshot_ttl_sec: int = 300


class ExecutionCfg(BaseModel):
    n_exec_groups: int = 5
    attempt_waits: list[float] = [3.0, 8.0, 15.0]
    manual_hedge_maker_wait_sec: float = Field(
        default=10.0, ge=1.0, le=60.0)


class WebhookCfg(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8300


class StrategyCfg(BaseModel):
    name: str = "BTC_OPTION_SELLER_V2"
    currency: str = "BTC"
    min_lot: float = 0.1
    # 与 Pine 的 dvol_entry_level_1/2/3 对应（0~1）。
    # 仅供后端诊断快照复现 TradingView 条件，不参与 V2 开仓批准或拒绝。
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
    entry_signal: EntrySignalCfg = EntrySignalCfg()
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


def account_scope_key(env: EnvSettings) -> str:
    """Return a stable, non-secret storage namespace for one Deribit account."""
    environment = "testnet" if env.is_testnet else "mainnet"
    client_id = env.client_id.strip()
    if not client_id:
        return f"{environment}-unconfigured"
    digest = hashlib.sha256(
        f"{environment}:{client_id}".encode("utf-8")
    ).hexdigest()[:12]
    return f"{environment}-{digest}"


def db_path(env: EnvSettings | None = None) -> Path:
    os.makedirs(DATA_DIR, exist_ok=True)
    # Keep the no-argument path for developer tools and legacy callers. The
    # running service always supplies EnvSettings and therefore gets a fully
    # account-scoped ledger.
    if env is None:
        return DATA_DIR / "seller_v2.db"
    return DATA_DIR / f"seller_v2_{account_scope_key(env)}.db"
