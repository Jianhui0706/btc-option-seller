from types import SimpleNamespace

import pytest

from src.engine import risk


class FakeRepo:
    conn = None

    def __init__(self, *, trading_enabled=True):
        self.flags = {"trading_enabled": trading_enabled}
        self.states = {}
        self.parents = []

    async def get_flag(self, key, default=False):
        return self.flags.get(key, default)

    async def set_flag(self, key, value):
        self.flags[key] = value

    async def get_state(self, key, default=None):
        return self.states.get(key, default)

    async def set_state(self, key, value):
        self.states[key] = value

    async def active_parents(self):
        return self.parents


def make_ctx(repo):
    commands = []

    async def trading_enabled():
        return await repo.get_flag("trading_enabled")

    async def force_close_active():
        return False

    def request_force_close(source, reason="", auto_resume=False):
        commands.append((source, reason, auto_resume))
        return False

    return SimpleNamespace(
        repo=repo,
        cfg=SimpleNamespace(
            env=SimpleNamespace(is_testnet=True),
            strategy=SimpleNamespace(currency="BTC"),
        ),
        ws=object(),
        trading_enabled=trading_enabled,
        force_close_active=force_close_active,
        request_force_close=request_force_close,
        current_command_kind=None,
        commands=commands,
    )


@pytest.mark.asyncio
async def test_transient_risk_pause_is_latched_and_not_queued_repeatedly(
        monkeypatch):
    repo = FakeRepo(trading_enabled=True)
    ctx = make_ctx(repo)
    events = []

    async def emit(*args, **kwargs):
        events.append(kwargs)

    monkeypatch.setattr(risk.ev, "emit", emit)

    first = await risk.request_risk_force_close(
        ctx, "positions unavailable", auto_resume=True)
    second = await risk.request_risk_force_close(
        ctx, "positions unavailable again", auto_resume=True)

    assert first is True
    assert second is False
    assert repo.flags["trading_enabled"] is False
    assert repo.states[risk.AUTO_RESUME_STATE_KEY] == risk.AUTO_RESUME_ARMED
    assert ctx.commands == [
        ("RISK_ENGINE", "positions unavailable", True),
    ]
    assert len(events) == 1


@pytest.mark.asyncio
async def test_material_risk_pause_is_latched_and_not_queued_repeatedly(
        monkeypatch):
    repo = FakeRepo(trading_enabled=True)
    ctx = make_ctx(repo)

    async def emit(*args, **kwargs):
        return None

    monkeypatch.setattr(risk.ev, "emit", emit)

    first = await risk.request_risk_force_close(
        ctx, "净 Delta 异常", auto_resume=False)
    second = await risk.request_risk_force_close(
        ctx, "净 Delta 仍然异常", auto_resume=False)

    assert first is True
    assert second is False
    assert repo.states[risk.AUTO_RESUME_STATE_KEY] == risk.AUTO_RESUME_LOCKED
    assert ctx.commands == [("RISK_ENGINE", "净 Delta 异常", False)]


@pytest.mark.asyncio
async def test_daily_loss_pause_latches_until_next_beijing_day(monkeypatch):
    repo = FakeRepo(trading_enabled=True)
    ctx = make_ctx(repo)
    events = []

    async def emit(*args, **kwargs):
        events.append((args, kwargs))

    monkeypatch.setattr(risk.ev, "emit", emit)

    first = await risk.request_risk_force_close(
        ctx, "日亏损 5.1% 超过限制 5%",
        resume_after_date="2026-08-01")
    second = await risk.request_risk_force_close(
        ctx, "日亏损 5.1% 超过限制 5%",
        resume_after_date="2026-08-01")

    assert first is True
    assert second is False
    assert repo.states[risk.AUTO_RESUME_STATE_KEY] == "DAILY_LOSS:2026-08-01"
    assert ctx.commands == [(
        "RISK_ENGINE", "日亏损 5.1% 超过限制 5%", False)]
    assert events[-1][0][1] == risk.ev.RISK_DAILY_LOSS_PAUSED
    assert events[-1][1]["resume_after_date"] == "2026-08-01"


@pytest.mark.asyncio
async def test_daily_loss_latch_requeues_after_restart_with_active_trade(
        monkeypatch):
    repo = FakeRepo(trading_enabled=False)
    repo.states[risk.AUTO_RESUME_STATE_KEY] = "DAILY_LOSS:2026-08-01"
    repo.parents = [{"id": 91, "state": "OPEN"}]
    ctx = make_ctx(repo)

    async def emit(*args, **kwargs):
        return None

    monkeypatch.setattr(risk.ev, "emit", emit)

    queued = await risk.request_risk_force_close(
        ctx, "日亏损 5.1% 超过限制 5%",
        resume_after_date="2026-08-01")

    assert queued is True
    assert repo.states[risk.AUTO_RESUME_STATE_KEY] == "DAILY_LOSS:2026-08-01"
    assert ctx.commands == [(
        "RISK_ENGINE", "日亏损 5.1% 超过限制 5%", False)]


@pytest.mark.asyncio
async def test_clean_transient_pause_resumes_after_healthy_scan(monkeypatch):
    repo = FakeRepo(trading_enabled=False)
    repo.states[risk.AUTO_RESUME_STATE_KEY] = risk.AUTO_RESUME_READY
    repo.states[risk.PAUSE_REASON_KEY] = "positions unavailable"
    ctx = make_ctx(repo)
    events = []

    async def emit(*args, **kwargs):
        events.append(kwargs)

    monkeypatch.setattr(risk.ev, "emit", emit)

    resumed = await risk.maybe_resume_transient_risk_pause(ctx, [])

    assert resumed is True
    assert repo.flags["trading_enabled"] is True
    assert repo.states[risk.AUTO_RESUME_STATE_KEY] == ""
    assert repo.states[risk.PAUSE_REASON_KEY] == ""
    assert events[-1]["recovered_from"] == "positions unavailable"


@pytest.mark.asyncio
async def test_daily_loss_pause_resumes_only_on_later_beijing_day(monkeypatch):
    repo = FakeRepo(trading_enabled=False)
    repo.states[risk.AUTO_RESUME_STATE_KEY] = "DAILY_LOSS:2026-08-01"
    repo.states[risk.PAUSE_REASON_KEY] = "日亏损 5.1% 超过限制 5%"
    ctx = make_ctx(repo)
    events = []

    async def emit(*args, **kwargs):
        events.append(kwargs)

    monkeypatch.setattr(risk.ev, "emit", emit)

    same_day = await risk.maybe_resume_transient_risk_pause(
        ctx, [], current_date="2026-08-01")
    next_day = await risk.maybe_resume_transient_risk_pause(
        ctx, [], current_date="2026-08-02")

    assert same_day is False
    assert next_day is True
    assert repo.flags["trading_enabled"] is True
    assert repo.states[risk.AUTO_RESUME_STATE_KEY] == ""
    assert repo.states[risk.PAUSE_REASON_KEY] == ""
    assert events[-1]["recovery_policy"] == "next_beijing_day"


@pytest.mark.asyncio
async def test_run_risk_checks_marks_daily_loss_as_cross_day_pause():
    repo = FakeRepo(trading_enabled=True)
    repo.states["day_start_date"] = "2026-08-01"
    repo.states["day_start_equity"] = "100"
    ctx = SimpleNamespace(
        repo=repo,
        cfg=SimpleNamespace(
            strategy=SimpleNamespace(currency="BTC"),
            risk=SimpleNamespace(
                margin_ratio_limit=0.6,
                daily_loss_limit_pct=0.05,
                delta_anomaly_btc=1.0,
            ),
        ),
        ws=object(),
    )
    summary = SimpleNamespace(equity_btc=94.9, margin_ratio=0.0)

    breach = await risk.run_risk_checks(
        ctx, [], [], summary=summary, current_date="2026-08-01")

    assert breach.reason == "日亏损 5.1% 超过限制 5%"
    assert breach.resume_after_date == "2026-08-01"


@pytest.mark.asyncio
async def test_testnet_daily_pause_resumes_immediately_after_flat_confirmation(
        monkeypatch):
    repo = FakeRepo(trading_enabled=False)
    repo.states[risk.AUTO_RESUME_STATE_KEY] = "DAILY_LOSS:2026-08-03"
    repo.states[risk.PAUSE_REASON_KEY] = "日亏损 5.0% 超过限制 5%"
    repo.states["day_start_equity"] = "8.58"
    ctx = make_ctx(repo)
    events = []

    async def empty(*args, **kwargs):
        return []

    async def summary(*args, **kwargs):
        return SimpleNamespace(equity_btc=8.15)

    async def emit(*args, **kwargs):
        events.append(kwargs)

    monkeypatch.setattr(risk.account, "get_option_positions", empty)
    monkeypatch.setattr(risk.account, "get_future_positions", empty)
    monkeypatch.setattr(risk.account, "get_open_orders", empty)
    monkeypatch.setattr(risk.account, "get_account_summary", summary)
    monkeypatch.setattr(risk.ev, "emit", emit)
    monkeypatch.setattr(risk, "beijing_date", lambda: "2026-08-03")

    resumed = await risk.maybe_resume_testnet_daily_loss_pause(ctx)

    assert resumed is True
    assert repo.flags["trading_enabled"] is True
    assert repo.states[risk.AUTO_RESUME_STATE_KEY] == ""
    assert repo.states[risk.PAUSE_REASON_KEY] == ""
    assert repo.states["day_start_date"] == "2026-08-03"
    assert repo.states["day_start_equity"] == "8.15"
    assert events[-1]["recovery_policy"] == "testnet_flat_confirmation"


@pytest.mark.asyncio
async def test_testnet_daily_pause_stays_closed_until_everything_is_flat(
        monkeypatch):
    repo = FakeRepo(trading_enabled=False)
    repo.states[risk.AUTO_RESUME_STATE_KEY] = "DAILY_LOSS:2026-08-03"
    ctx = make_ctx(repo)

    async def option_position(*args, **kwargs):
        return [SimpleNamespace(instrument="BTC-28AUG26-70000-C", size=-1)]

    async def empty(*args, **kwargs):
        return []

    monkeypatch.setattr(risk.account, "get_option_positions", option_position)
    monkeypatch.setattr(risk.account, "get_future_positions", empty)
    monkeypatch.setattr(risk.account, "get_open_orders", empty)

    resumed = await risk.maybe_resume_testnet_daily_loss_pause(ctx)

    assert resumed is False
    assert repo.flags["trading_enabled"] is False
    assert repo.states[risk.AUTO_RESUME_STATE_KEY] == "DAILY_LOSS:2026-08-03"


@pytest.mark.asyncio
async def test_mainnet_daily_pause_never_uses_testnet_flat_resume():
    repo = FakeRepo(trading_enabled=False)
    repo.states[risk.AUTO_RESUME_STATE_KEY] = "DAILY_LOSS:2026-08-03"
    ctx = make_ctx(repo)
    ctx.cfg.env.is_testnet = False

    resumed = await risk.maybe_resume_testnet_daily_loss_pause(ctx)

    assert resumed is False
    assert repo.flags["trading_enabled"] is False


@pytest.mark.asyncio
async def test_auto_resume_never_overrides_active_trade_or_manual_pause(
        monkeypatch):
    repo = FakeRepo(trading_enabled=False)
    repo.states[risk.AUTO_RESUME_STATE_KEY] = risk.AUTO_RESUME_READY
    ctx = make_ctx(repo)

    async def emit(*args, **kwargs):
        raise AssertionError("must not emit an auto-resume event")

    monkeypatch.setattr(risk.ev, "emit", emit)

    resumed = await risk.maybe_resume_transient_risk_pause(
        ctx, [{"id": 41, "state": "OPEN"}])

    assert resumed is False
    assert repo.flags["trading_enabled"] is False
    assert repo.states[risk.AUTO_RESUME_STATE_KEY] == risk.AUTO_RESUME_READY


class ConsistencyRepo:
    def __init__(self, parent):
        self.parent = parent

    async def open_parents(self):
        return [self.parent]

    async def groups_of(self, parent_id):
        return [{"qty": 2.9, "closed_qty": 0.0}]


def consistency_ctx(parent):
    return SimpleNamespace(
        repo=ConsistencyRepo(parent),
        cfg=SimpleNamespace(
            strategy=SimpleNamespace(min_lot=0.1),
            exit=SimpleNamespace(keep_buy_leg_on_normal_exit=False),
        ),
    )


@pytest.mark.asyncio
async def test_consistency_skips_position_snapshot_taken_during_open():
    parent = {
        "id": 144,
        "opened_at": 2_000,
        "qty_total": 2.9,
        "sell_instrument": "BTC-28AUG26-69000-C",
        "buy_instrument": "BTC-28AUG26-74000-C",
    }
    opts = [
        SimpleNamespace(instrument=parent["sell_instrument"], size=-2.4),
        SimpleNamespace(instrument=parent["buy_instrument"], size=2.9),
    ]

    mismatch = await risk._check_consistency(
        consistency_ctx(parent), opts,
        positions_snapshot_started_ts=1_999)

    assert mismatch is None


@pytest.mark.asyncio
async def test_consistency_still_rejects_mismatch_from_fresh_snapshot():
    parent = {
        "id": 144,
        "opened_at": 2_000,
        "qty_total": 2.9,
        "sell_instrument": "BTC-28AUG26-69000-C",
        "buy_instrument": "BTC-28AUG26-74000-C",
    }
    opts = [
        SimpleNamespace(instrument=parent["sell_instrument"], size=-2.4),
        SimpleNamespace(instrument=parent["buy_instrument"], size=2.9),
    ]

    mismatch = await risk._check_consistency(
        consistency_ctx(parent), opts,
        positions_snapshot_started_ts=2_001)

    assert "DB=2.9" in mismatch
    assert "交易所=2.4" in mismatch


@pytest.mark.asyncio
async def test_consistency_aggregates_batches_sharing_same_instruments():
    parents = [
        {
            "id": 201, "opened_at": 1_000, "qty_total": 1.2,
            "sell_instrument": "BTC-SHARED-SELL",
            "buy_instrument": "BTC-SHARED-BUY",
        },
        {
            "id": 202, "opened_at": 1_100, "qty_total": 0.8,
            "sell_instrument": "BTC-SHARED-SELL",
            "buy_instrument": "BTC-SHARED-BUY",
        },
    ]

    class SharedRepo:
        async def open_parents(self):
            return parents

        async def groups_of(self, parent_id):
            qty = 1.2 if parent_id == 201 else 0.8
            return [{"qty": qty, "closed_qty": 0.0}]

    context = SimpleNamespace(
        repo=SharedRepo(),
        cfg=SimpleNamespace(
            strategy=SimpleNamespace(min_lot=0.1),
            exit=SimpleNamespace(keep_buy_leg_on_normal_exit=False),
        ),
    )
    opts = [
        SimpleNamespace(instrument="BTC-SHARED-SELL", size=-2.0),
        SimpleNamespace(instrument="BTC-SHARED-BUY", size=2.0),
    ]

    mismatch = await risk._check_consistency(
        context, opts, positions_snapshot_started_ts=2_000)

    assert mismatch is None
