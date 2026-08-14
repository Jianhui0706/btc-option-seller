"""前端可调运行时参数：存储层、生效语义与 runtime_settings API。"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src import runtime_settings
from src.api import get_runtime_settings, set_runtime_settings

E1 = 2_000_000_000_000
E2 = 2_100_000_000_000


class FakeRepo:
    def __init__(self, state=None):
        self.state = dict(state or {})

    async def get_state(self, key, default=None):
        return self.state.get(key, default)

    async def set_state(self, key, value):
        self.state[key] = value


class FakeWS:
    """只回应 public/get_instruments，供到期日列表使用。"""

    def __init__(self, expiries):
        self.expiries = expiries

    async def request(self, method, params):
        assert method == "public/get_instruments"
        out = []
        for e in self.expiries:
            for opt in ("call", "put"):
                out.append({
                    "instrument_name": f"BTC-{e}-{opt}",
                    "option_type": opt,
                    "expiration_timestamp": e,
                })
        return out


class FakeRequest:
    def __init__(self, ctx, body=None):
        self.app = SimpleNamespace(state=SimpleNamespace(ctx=ctx))
        self._body = body

    async def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _ctx(state=None, progress=None, expiry=None):
    return SimpleNamespace(
        repo=FakeRepo(state),
        ws=FakeWS([E1, E2]),
        cfg=SimpleNamespace(
            hedge=SimpleNamespace(
                bep_activation_progress=1.00, review_bar_minutes=1,
                release_recovery_pct=0.50, release_confirmation_bars=1),
            strategy=SimpleNamespace(currency="BTC"),
        ),
        runtime_bep_activation_progress=progress,
        runtime_forced_expiry_ts=expiry,
        runtime_review_bar_minutes=None,
        runtime_release_recovery_pct=None,
        runtime_release_confirmation_bars=None,
        runtime_hedge_profiles={},
    )


# ---- 存储层 / 生效语义 ---------------------------------------------------

def test_effective_progress_falls_back_to_cfg_default():
    assert runtime_settings.effective_progress(_ctx()) == pytest.approx(1.00)


def test_effective_progress_uses_override_when_set():
    assert runtime_settings.effective_progress(
        _ctx(progress=0.35)) == pytest.approx(0.35)


def test_forced_expiry_getter():
    assert runtime_settings.forced_expiry_ts(_ctx()) is None
    assert runtime_settings.forced_expiry_ts(_ctx(expiry=E1)) == E1


def test_progress_in_range():
    assert runtime_settings.progress_in_range(0.0)
    assert runtime_settings.progress_in_range(0.2)
    assert runtime_settings.progress_in_range(1.0)
    assert runtime_settings.progress_in_range(1.5)
    assert runtime_settings.progress_in_range(2.0)
    assert not runtime_settings.progress_in_range(2.01)
    assert not runtime_settings.progress_in_range(-0.1)


def test_hysteresis_requires_release_line_beyond_activation_line():
    assert runtime_settings.hysteresis_is_valid(0.10, 0.95)
    assert runtime_settings.hysteresis_gap(0.10, 0.95) == pytest.approx(0.05)
    assert not runtime_settings.hysteresis_is_valid(0.01, 0.95)
    assert not runtime_settings.hysteresis_is_valid(0.50, 0.50)


@pytest.mark.asyncio
async def test_load_into_ctx_reads_persisted_values():
    ctx = _ctx(state={
        runtime_settings.PROGRESS_KEY: "0.6",
        runtime_settings.EXPIRY_KEY: str(E1),
    })
    await runtime_settings.load_into_ctx(ctx)
    assert ctx.runtime_bep_activation_progress == pytest.approx(0.6)
    assert ctx.runtime_forced_expiry_ts == E1


@pytest.mark.asyncio
async def test_load_clears_legacy_overlapping_alpha_beta_overrides():
    ctx = _ctx(state={
        runtime_settings.PROGRESS_KEY: "0.01",
        runtime_settings.RELEASE_RECOVERY_KEY: "0.95",
    })
    await runtime_settings.load_into_ctx(ctx)
    assert ctx.runtime_bep_activation_progress is None
    assert ctx.runtime_release_recovery_pct is None
    assert ctx.repo.state[runtime_settings.PROGRESS_KEY] == ""
    assert ctx.repo.state[runtime_settings.RELEASE_RECOVERY_KEY] == ""


@pytest.mark.asyncio
async def test_load_into_ctx_treats_blank_as_unset():
    ctx = _ctx(state={
        runtime_settings.PROGRESS_KEY: "",
        runtime_settings.EXPIRY_KEY: "",
    })
    await runtime_settings.load_into_ctx(ctx)
    assert ctx.runtime_bep_activation_progress is None
    assert ctx.runtime_forced_expiry_ts is None


@pytest.mark.asyncio
async def test_set_and_clear_progress_updates_state_and_cache():
    ctx = _ctx()
    await runtime_settings.set_progress(ctx, 0.25)
    assert ctx.runtime_bep_activation_progress == pytest.approx(0.25)
    assert float(ctx.repo.state[runtime_settings.PROGRESS_KEY]) == pytest.approx(0.25)
    await runtime_settings.set_progress(ctx, None)
    assert ctx.runtime_bep_activation_progress is None
    assert ctx.repo.state[runtime_settings.PROGRESS_KEY] == ""


@pytest.mark.asyncio
async def test_set_and_clear_forced_expiry_updates_state_and_cache():
    ctx = _ctx()
    await runtime_settings.set_forced_expiry(ctx, E1)
    assert ctx.runtime_forced_expiry_ts == E1
    assert ctx.repo.state[runtime_settings.EXPIRY_KEY] == str(E1)
    await runtime_settings.set_forced_expiry(ctx, None)
    assert ctx.runtime_forced_expiry_ts is None
    assert ctx.repo.state[runtime_settings.EXPIRY_KEY] == ""


# ---- API 端点 ------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_runtime_settings_reports_effective_and_available():
    ctx = _ctx()
    out = await get_runtime_settings(FakeRequest(ctx))
    assert out["bep_activation_progress"] == pytest.approx(1.00)
    assert out["bep_activation_progress_override"] is None
    assert out["bep_activation_progress_default"] == pytest.approx(1.00)
    assert out["forced_expiry_ts"] is None
    assert {e["ts"] for e in out["available_expiries"]} == {E1, E2}
    assert all("date" in e and "dte" in e for e in out["available_expiries"])


@pytest.mark.asyncio
async def test_post_sets_progress_and_expiry():
    ctx = _ctx()
    out = await set_runtime_settings(
        FakeRequest(ctx, {"bep_activation_progress": 0.6, "forced_expiry_ts": E1}))
    assert ctx.runtime_bep_activation_progress == pytest.approx(0.6)
    assert ctx.runtime_forced_expiry_ts == E1
    assert out["bep_activation_progress_override"] == pytest.approx(0.6)
    assert out["forced_expiry_ts"] == E1


@pytest.mark.asyncio
async def test_post_sets_review_release_and_side_profiles():
    ctx = _ctx()
    out = await set_runtime_settings(FakeRequest(ctx, {
        "review_bar_minutes": 3,
        "release_recovery_pct": 0.60,
        "release_confirmation_bars": 2,
        "hedge_profiles": {
            "PUT_CREDIT": {"min_rebalance_usd": 1200},
            "CALL_CREDIT": {"rebalance_cooldown_sec": 90},
        },
    }))
    assert out["review_bar_minutes"] == 3
    assert out["release_recovery_pct"] == pytest.approx(0.60)
    assert out["release_confirmation_bars"] == 2
    assert out["hedge_profiles"]["PUT_CREDIT"]["min_rebalance_usd"] == 1200
    assert out["hedge_profiles"]["CALL_CREDIT"]["rebalance_cooldown_sec"] == 90


@pytest.mark.asyncio
async def test_post_rejects_unsupported_review_bar():
    with pytest.raises(HTTPException) as exc:
        await set_runtime_settings(FakeRequest(
            _ctx(), {"review_bar_minutes": 2}))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_post_absent_key_leaves_value_unchanged():
    ctx = _ctx(progress=0.4, expiry=E2)
    await set_runtime_settings(FakeRequest(ctx, {"bep_activation_progress": 0.6}))
    assert ctx.runtime_bep_activation_progress == pytest.approx(0.6)
    assert ctx.runtime_forced_expiry_ts == E2   # 未出现在 body → 不变


@pytest.mark.asyncio
async def test_post_null_clears_values():
    ctx = _ctx(progress=0.4, expiry=E2)
    await set_runtime_settings(
        FakeRequest(ctx, {"bep_activation_progress": None, "forced_expiry_ts": None}))
    assert ctx.runtime_bep_activation_progress is None
    assert ctx.runtime_forced_expiry_ts is None


@pytest.mark.asyncio
async def test_post_rejects_out_of_range_progress():
    ctx = _ctx()
    with pytest.raises(HTTPException) as exc:
        await set_runtime_settings(FakeRequest(ctx, {"bep_activation_progress": 2.01}))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_post_rejects_overlapping_alpha_beta_without_partial_write():
    ctx = _ctx()
    with pytest.raises(HTTPException) as exc:
        await set_runtime_settings(FakeRequest(ctx, {
            "bep_activation_progress": 0.01,
            "release_recovery_pct": 0.95,
        }))
    assert exc.value.status_code == 400
    assert "α + β" in exc.value.detail
    assert ctx.runtime_bep_activation_progress is None
    assert ctx.runtime_release_recovery_pct is None


@pytest.mark.asyncio
async def test_post_accepts_positive_hysteresis_and_above_bep_progress():
    ctx = _ctx()
    await set_runtime_settings(FakeRequest(ctx, {
        "bep_activation_progress": 0.10,
        "release_recovery_pct": 0.95,
    }))
    assert ctx.runtime_bep_activation_progress == pytest.approx(0.10)
    assert ctx.runtime_release_recovery_pct == pytest.approx(0.95)
    await set_runtime_settings(FakeRequest(ctx, {"bep_activation_progress": 1.5}))
    assert ctx.runtime_bep_activation_progress == pytest.approx(1.5)


@pytest.mark.asyncio
async def test_post_rejects_unknown_expiry():
    ctx = _ctx()
    with pytest.raises(HTTPException) as exc:
        await set_runtime_settings(FakeRequest(ctx, {"forced_expiry_ts": 123}))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_post_rejects_non_object_body():
    ctx = _ctx()
    with pytest.raises(HTTPException) as exc:
        await set_runtime_settings(FakeRequest(ctx, ["not", "a", "dict"]))
    assert exc.value.status_code == 400
