"""前端可调的运行时参数。

支持以下运行时覆盖：
  - 对冲激活系数 bep_activation_progress（主策略文档 α，开仓指数价走向 BEP
    的比例，[0,2]；0=开仓价，1=BEP，2=越过 BEP 同等距离）；
  - 强制开仓到期日 forced_expiry_ts（Deribit 到期时间戳，毫秒）。
  - 完整复核 Bar、参考文档撤销 β、撤销确认 Bar 数；
  - PUT/CALL 各自的 Delta 门槛、最小调仓额、冷却和紧急倍数。

持久化在 system_state 表，进程内缓存到 Ctx 以避免热路径（3 分钟扫描 / 状态页）
反复查库。后端单进程，只有 runtime_settings API 会写这两项，写时同步更新 Ctx，
因此缓存始终与库一致；启动时从库回填。

None 语义：未覆盖。系数回退 cfg 默认；到期日回退原 tier/DTE 选腿逻辑。
"""

from __future__ import annotations

import json
import logging
from typing import Optional

log = logging.getLogger("v2.runtime_settings")

PROGRESS_KEY = "runtime.bep_activation_progress"
EXPIRY_KEY = "runtime.forced_expiry_ts"
REVIEW_BAR_KEY = "runtime.hedge.review_bar_minutes"
RELEASE_RECOVERY_KEY = "runtime.hedge.release_recovery_pct"
RELEASE_CONFIRMATION_KEY = "runtime.hedge.release_confirmation_bars"
HEDGE_PROFILES_KEY = "runtime.hedge.side_profiles"
REVIEW_BAR_CHOICES = {1, 3, 5, 10, 15, 30, 60}
HEDGE_SIDES = ("PUT_CREDIT", "CALL_CREDIT")
PROFILE_FIELDS = (
    "rebalance_outer_equity_pct",
    "min_rebalance_usd",
    "rebalance_cooldown_sec",
    "emergency_trigger_multiplier",
)
PROFILE_DEFAULTS = {
    "rebalance_outer_equity_pct": 0.02,
    "min_rebalance_usd": 5000.0,
    "rebalance_cooldown_sec": 360.0,
    "emergency_trigger_multiplier": 2.0,
}

# 主策略文档允许 0%～200%：0 表示开仓价即激活，100% 表示到达 BEP，
# 大于 100% 表示越过 BEP 后再激活。
PROGRESS_MIN = 0.0
PROGRESS_MAX = 2.0


def progress_in_range(value: float) -> bool:
    return PROGRESS_MIN <= value <= PROGRESS_MAX


def hysteresis_gap(alpha: float, beta: float) -> float:
    """Return the normalized release-to-activation separation.

    Both coefficients use the open-to-BEP distance.  A strictly positive gap
    means the release line is back toward the entry side of the activation
    line; zero/negative values let activation and release overlap.
    """
    return float(alpha) + float(beta) - 1.0


def hysteresis_is_valid(alpha: float, beta: float) -> bool:
    return hysteresis_gap(alpha, beta) > 1e-9


async def load_into_ctx(ctx) -> None:
    """启动时把持久化的运行时参数读进 Ctx 缓存。"""
    raw_progress = await ctx.repo.get_state(PROGRESS_KEY)
    ctx.runtime_bep_activation_progress = (
        float(raw_progress) if raw_progress not in (None, "") else None)
    raw_expiry = await ctx.repo.get_state(EXPIRY_KEY)
    ctx.runtime_forced_expiry_ts = (
        int(raw_expiry) if raw_expiry not in (None, "") else None)
    raw_review = await ctx.repo.get_state(REVIEW_BAR_KEY)
    ctx.runtime_review_bar_minutes = (
        int(raw_review) if raw_review not in (None, "") else None)
    raw_release = await ctx.repo.get_state(RELEASE_RECOVERY_KEY)
    ctx.runtime_release_recovery_pct = (
        float(raw_release) if raw_release not in (None, "") else None)
    raw_confirmation = await ctx.repo.get_state(RELEASE_CONFIRMATION_KEY)
    ctx.runtime_release_confirmation_bars = (
        int(raw_confirmation) if raw_confirmation not in (None, "") else None)
    raw_profiles = await ctx.repo.get_state(HEDGE_PROFILES_KEY)
    try:
        parsed = json.loads(raw_profiles) if raw_profiles else {}
    except (TypeError, ValueError):
        parsed = {}
    ctx.runtime_hedge_profiles = parsed if isinstance(parsed, dict) else {}

    # Older builds allowed α/β combinations whose activation and release
    # zones overlap.  Never start the engine with such persisted overrides:
    # atomically fall back to the reviewed config defaults instead.
    alpha = effective_progress(ctx)
    beta = effective_release_recovery_pct(ctx)
    if not hysteresis_is_valid(alpha, beta):
        log.warning(
            "invalid persisted hedge hysteresis alpha=%.6f beta=%.6f; "
            "clearing both overrides", alpha, beta)
        await ctx.repo.set_state(PROGRESS_KEY, "")
        await ctx.repo.set_state(RELEASE_RECOVERY_KEY, "")
        ctx.runtime_bep_activation_progress = None
        ctx.runtime_release_recovery_pct = None


def effective_progress(ctx) -> float:
    """有运行时覆盖用覆盖值，否则回退 cfg 默认。对测试替身用 getattr 容错。"""
    override = getattr(ctx, "runtime_bep_activation_progress", None)
    if override is not None:
        return float(override)
    return float(ctx.cfg.hedge.bep_activation_progress)


def forced_expiry_ts(ctx) -> Optional[int]:
    """当前强制开仓到期日（毫秒时间戳）；未设置返回 None。"""
    value = getattr(ctx, "runtime_forced_expiry_ts", None)
    return int(value) if value is not None else None


def effective_review_bar_minutes(ctx) -> int:
    value = getattr(ctx, "runtime_review_bar_minutes", None)
    return int(value if value is not None else
               getattr(ctx.cfg.hedge, "review_bar_minutes", 3))


def effective_release_recovery_pct(ctx) -> float:
    value = getattr(ctx, "runtime_release_recovery_pct", None)
    if value is not None:
        return float(value)
    configured = getattr(ctx.cfg.hedge, "release_recovery_pct", None)
    if configured is not None:
        return float(configured)
    return 1.0 - float(getattr(ctx.cfg.hedge, "hedge_release_progress", 0.05))


def effective_release_confirmation_bars(ctx) -> int:
    value = getattr(ctx, "runtime_release_confirmation_bars", None)
    return int(value if value is not None else
               getattr(ctx.cfg.hedge, "release_confirmation_bars", 1))


def effective_side_profile(ctx, spread_type: str) -> dict[str, float]:
    if spread_type not in HEDGE_SIDES:
        raise ValueError(spread_type)
    side_name = "put" if spread_type == "PUT_CREDIT" else "call"
    side_cfg = getattr(ctx.cfg.hedge, side_name, ctx.cfg.hedge)
    overrides = getattr(ctx, "runtime_hedge_profiles", {}) or {}
    side_overrides = overrides.get(spread_type, {})
    return {
        field: float(side_overrides.get(
            field, getattr(side_cfg, field, getattr(
                ctx.cfg.hedge, field, PROFILE_DEFAULTS[field]))))
        for field in PROFILE_FIELDS
    }


async def set_progress(ctx, value: Optional[float]) -> None:
    """设置（value 为数字）或清除（value 为 None）激活系数；写库 + 更新缓存。"""
    if value is None:
        await ctx.repo.set_state(PROGRESS_KEY, "")
        ctx.runtime_bep_activation_progress = None
    else:
        await ctx.repo.set_state(PROGRESS_KEY, repr(float(value)))
        ctx.runtime_bep_activation_progress = float(value)


async def set_forced_expiry(ctx, value: Optional[int]) -> None:
    """设置（value 为时间戳）或清除（value 为 None）强制到期日；写库 + 更新缓存。"""
    if value is None:
        await ctx.repo.set_state(EXPIRY_KEY, "")
        ctx.runtime_forced_expiry_ts = None
    else:
        await ctx.repo.set_state(EXPIRY_KEY, str(int(value)))
        ctx.runtime_forced_expiry_ts = int(value)


async def _set_optional(ctx, key: str, attr: str, value, cast) -> None:
    if value is None:
        await ctx.repo.set_state(key, "")
        setattr(ctx, attr, None)
    else:
        parsed = cast(value)
        await ctx.repo.set_state(key, str(parsed))
        setattr(ctx, attr, parsed)


async def set_review_bar_minutes(ctx, value: Optional[int]) -> None:
    await _set_optional(
        ctx, REVIEW_BAR_KEY, "runtime_review_bar_minutes", value, int)


async def set_release_recovery_pct(ctx, value: Optional[float]) -> None:
    await _set_optional(
        ctx, RELEASE_RECOVERY_KEY, "runtime_release_recovery_pct", value, float)


async def set_release_confirmation_bars(ctx, value: Optional[int]) -> None:
    await _set_optional(
        ctx, RELEASE_CONFIRMATION_KEY,
        "runtime_release_confirmation_bars", value, int)


async def set_side_profiles(ctx, value: Optional[dict]) -> None:
    profiles = {} if value is None else value
    await ctx.repo.set_state(
        HEDGE_PROFILES_KEY,
        "" if not profiles else json.dumps(profiles, ensure_ascii=False,
                                             sort_keys=True))
    ctx.runtime_hedge_profiles = profiles
