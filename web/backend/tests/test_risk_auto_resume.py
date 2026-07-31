from types import SimpleNamespace

import pytest

from src.engine import risk


class FakeRepo:
    conn = None

    def __init__(self, *, trading_enabled=True):
        self.flags = {"trading_enabled": trading_enabled}
        self.states = {}

    async def get_flag(self, key, default=False):
        return self.flags.get(key, default)

    async def set_flag(self, key, value):
        self.flags[key] = value

    async def get_state(self, key, default=None):
        return self.states.get(key, default)

    async def set_state(self, key, value):
        self.states[key] = value


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
