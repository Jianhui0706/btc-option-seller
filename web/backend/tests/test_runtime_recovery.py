from types import SimpleNamespace

import pytest

from src.engine import scanner, worker
from src.engine.commands import Command, CommandQueue, PRIO_OPEN
from src.monitor import events as ev


class OpenRepo:
    def __init__(self, state="POSITION_CHECKING"):
        self.parent = {"id": 653, "state": state}
        self.transitions = []
        self.signal_status = None

    async def parent_by_signal_id(self, signal_id):
        assert signal_id == "OPEN-653"
        return dict(self.parent)

    async def transition_parent(self, parent_id, state, code, **payload):
        self.parent["state"] = state
        self.transitions.append((parent_id, state, code, payload))

    async def set_signal_status(self, signal_id, status, reason=None, detail=None):
        self.signal_status = (signal_id, status, reason, detail)


@pytest.mark.asyncio
async def test_open_exception_terminalizes_pre_execution_parent():
    repo = OpenRepo()
    ctx = SimpleNamespace(repo=repo, queue=CommandQueue())
    cmd = Command("OPEN", PRIO_OPEN, {"signal": {"signal_id": "OPEN-653"}})

    await worker.recover_failed_open_command(ctx, cmd, RuntimeError())

    assert repo.parent["state"] == "OPEN_FAILED"
    assert repo.transitions[-1][2] == ev.OPEN_EXECUTION_ERROR
    assert repo.signal_status == (
        "OPEN-653", "REJECTED", ev.OPEN_EXECUTION_ERROR, "RuntimeError")


@pytest.mark.asyncio
async def test_opening_exception_queues_reconciliation_instead_of_false_failure():
    repo = OpenRepo(state="OPENING")
    ctx = SimpleNamespace(repo=repo, queue=CommandQueue())
    cmd = Command("OPEN", PRIO_OPEN, {"signal": {"signal_id": "OPEN-653"}})

    await worker.recover_failed_open_command(ctx, cmd, RuntimeError("network"))

    queued = await ctx.queue.get()
    assert queued.kind == "RECOVER_OPENING"
    assert queued.payload["parent_id"] == 653
    assert repo.transitions == []


class HedgeRepo:
    async def get_hedge(self, parent_id):
        return {"state": "HEDGE_CLOSING" if parent_id == 485 else "HEDGE_ACTIVE"}


@pytest.mark.asyncio
async def test_closing_hedge_recovery_is_queued_once_per_parent():
    queue = CommandQueue()
    ctx = SimpleNamespace(repo=HedgeRepo(), queue=queue)
    parents = [{"id": 485}, {"id": 490}]

    await scanner._resume_closing_hedges(ctx, parents)
    await scanner._resume_closing_hedges(ctx, parents)

    queued = await queue.get()
    assert queued.kind == "HEDGE_RELEASE"
    assert queued.payload["parent_id"] == 485
    assert queue._q.empty()
