from types import SimpleNamespace

import pytest

from src.engine import reverser


class FakeRepo:
    conn = None

    def __init__(self, parent=None):
        self.parent = parent
        self.status_updates = []
        self.states = {}

    async def active_parent_by_trade_id(self, trade_id, strategy=""):
        self.looked_up_trade_id = trade_id
        self.looked_up_strategy = strategy
        return self.parent

    async def open_parents(self):
        return [self.parent] if self.parent else []

    async def get_parent(self, parent_id):
        return self.parent

    async def get_state(self, key, default=None):
        return self.states.get(key, default)

    async def set_signal_status(
            self, signal_id, status, reason=None, detail=None):
        self.status_updates.append((signal_id, status, reason, detail))


def make_ctx(repo, *, enabled=True, force_close=False):
    async def trading_enabled():
        return enabled

    async def force_close_active():
        return force_close

    return SimpleNamespace(
        repo=repo,
        cfg=SimpleNamespace(
            env=SimpleNamespace(is_testnet=False),
            strategy=SimpleNamespace(currency="BTC"),
        ),
        ws=object(),
        trading_enabled=trading_enabled,
        force_close_active=force_close_active,
        hedge_frozen=True,
    )


def signal():
    return {
        "signal_id": "macd-reverse-1",
        "trade_id": "43904791",
        "strategy": "MACD Strategy",
        "action": "REVERSE_SHORT",
        "reason": "MACD reverse",
    }


@pytest.mark.asyncio
async def test_reverse_closes_and_confirms_before_open(monkeypatch):
    repo = FakeRepo({"id": 41, "state": "OPEN"})
    ctx = make_ctx(repo)
    calls = []

    async def close(_ctx, payload):
        calls.append(("close", payload["parent_id"]))
        repo.parent["state"] = "CLOSED"

    async def open_(_ctx, payload):
        calls.append(("open", payload["signal"]["action"]))

    async def emit(*args, **kwargs):
        return None

    monkeypatch.setattr(reverser.closer, "handle_normal_close", close)
    monkeypatch.setattr(reverser.orchestrator, "handle_open", open_)
    monkeypatch.setattr(reverser.ev, "emit", emit)

    await reverser.handle_reverse(ctx, {"signal": signal()})

    assert calls == [("close", 41), ("open", "REVERSE_SHORT")]
    assert ctx.hedge_frozen is False


@pytest.mark.asyncio
async def test_reverse_never_opens_when_close_is_incomplete(monkeypatch):
    repo = FakeRepo({"id": 41, "state": "OPEN"})
    ctx = make_ctx(repo)
    opened = False

    async def close(_ctx, payload):
        repo.parent["state"] = "CLOSING"

    async def open_(_ctx, payload):
        nonlocal opened
        opened = True

    async def emit(*args, **kwargs):
        return None

    monkeypatch.setattr(reverser.closer, "handle_normal_close", close)
    monkeypatch.setattr(reverser.orchestrator, "handle_open", open_)
    monkeypatch.setattr(reverser.ev, "emit", emit)

    await reverser.handle_reverse(ctx, {"signal": signal()})

    assert opened is False
    assert repo.status_updates[-1][2] == reverser.ev.REVERSE_CLOSE_INCOMPLETE


@pytest.mark.asyncio
async def test_pause_allows_old_side_to_close_but_skips_new_side(monkeypatch):
    repo = FakeRepo({"id": 41, "state": "OPEN"})
    ctx = make_ctx(repo, enabled=False)
    calls = []

    async def close(_ctx, payload):
        calls.append("close")
        repo.parent["state"] = "CLOSED"

    async def open_(_ctx, payload):
        calls.append("open")

    async def emit(*args, **kwargs):
        return None

    monkeypatch.setattr(reverser.closer, "handle_normal_close", close)
    monkeypatch.setattr(reverser.orchestrator, "handle_open", open_)
    monkeypatch.setattr(reverser.ev, "emit", emit)

    await reverser.handle_reverse(ctx, {"signal": signal()})

    assert calls == ["close"]
    assert repo.status_updates[-1][2] == reverser.ev.REVERSE_OPEN_SKIPPED


@pytest.mark.asyncio
async def test_pause_reports_trading_gate_when_reverse_has_no_old_side(
        monkeypatch):
    repo = FakeRepo()
    repo.states[reverser.risk.PAUSE_REASON_KEY] = "日亏损 5.1% 超过限制 5%"
    ctx = make_ctx(repo, enabled=False)
    opened = False

    async def open_(_ctx, payload):
        nonlocal opened
        opened = True

    async def emit(*args, **kwargs):
        return None

    monkeypatch.setattr(reverser.orchestrator, "handle_open", open_)
    monkeypatch.setattr(reverser.ev, "emit", emit)

    await reverser.handle_reverse(ctx, {"signal": signal()})

    assert opened is False
    assert repo.status_updates[-1][2] == reverser.ev.REJECT_TRADING_DISABLED
    assert repo.status_updates[-1][3] == "日亏损 5.1% 超过限制 5%"


@pytest.mark.asyncio
async def test_flat_testnet_daily_pause_resumes_before_reverse_open(
        monkeypatch):
    repo = FakeRepo()
    ctx = make_ctx(repo, enabled=False)
    opened = False

    async def resume(_ctx):
        return True

    async def open_(_ctx, payload):
        nonlocal opened
        opened = True

    monkeypatch.setattr(
        reverser.risk, "maybe_resume_testnet_daily_loss_pause", resume)
    monkeypatch.setattr(reverser.orchestrator, "handle_open", open_)

    await reverser.handle_reverse(ctx, {"signal": signal()})

    assert opened is True
    assert repo.status_updates == []


@pytest.mark.asyncio
async def test_v2_reverse_uses_stable_source_id_not_display_name(monkeypatch):
    repo = FakeRepo({"id": 51, "state": "OPEN"})
    ctx = make_ctx(repo)

    async def close(_ctx, payload):
        repo.parent["state"] = "CLOSED"

    async def open_(_ctx, payload):
        return None

    monkeypatch.setattr(reverser.closer, "handle_normal_close", close)
    monkeypatch.setattr(reverser.orchestrator, "handle_open", open_)

    v2_signal = signal()
    v2_signal.update({
        "source_id": "stable-source-51",
        "trade_id": "stable-source-51",
        "strategy": "Renamed Strategy",
    })
    await reverser.handle_reverse(ctx, {"signal": v2_signal})

    assert repo.looked_up_trade_id == "stable-source-51"
    assert repo.looked_up_strategy == ""


@pytest.mark.asyncio
async def test_reverse_recovers_stranded_opening_before_close(monkeypatch):
    repo = FakeRepo({"id": 84, "state": "OPENING"})
    ctx = make_ctx(repo)
    calls = []

    async def recover(_ctx, parent_id):
        calls.append(("recover", parent_id))
        repo.parent["state"] = "OPEN"
        return repo.parent

    async def close(_ctx, payload):
        calls.append(("close", payload["parent_id"]))
        repo.parent["state"] = "CLOSED"

    async def open_(_ctx, payload):
        calls.append(("open", payload["signal"]["action"]))

    async def emit(*args, **kwargs):
        return None

    monkeypatch.setattr(
        reverser.risk, "recover_opening_parent", recover)
    monkeypatch.setattr(reverser.closer, "handle_normal_close", close)
    monkeypatch.setattr(reverser.orchestrator, "handle_open", open_)
    monkeypatch.setattr(reverser.ev, "emit", emit)

    await reverser.handle_reverse(ctx, {"signal": signal()})

    assert calls == [
        ("recover", 84),
        ("close", 84),
        ("open", "REVERSE_SHORT"),
    ]
