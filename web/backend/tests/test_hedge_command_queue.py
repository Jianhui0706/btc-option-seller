import pytest

from src.engine.commands import CommandQueue


def rebalance(queue: CommandQueue):
    return queue.hedge_rebalance(7, 0.1, -0.1)


@pytest.mark.asyncio
async def test_pending_release_blocks_new_rebalance():
    queue = CommandQueue()

    assert queue.put(queue.hedge_release(7, manual=True)) is True
    assert queue.put(rebalance(queue)) is False

    command = await queue.get()
    assert command.kind == "HEDGE_RELEASE"
    queue.complete(command)


@pytest.mark.asyncio
async def test_active_release_blocks_new_rebalance_until_complete():
    queue = CommandQueue()
    queue.put(queue.hedge_release(7, manual=True))
    command = await queue.get()

    assert queue.active_hedge_kind == "HEDGE_RELEASE"
    assert queue.put(rebalance(queue)) is False

    queue.complete(command)
    assert queue.active_hedge_kind is None
    assert queue.put(rebalance(queue)) is True


@pytest.mark.asyncio
async def test_release_overtakes_pending_rebalance_and_close_wins():
    queue = CommandQueue()

    assert queue.put(rebalance(queue)) is True
    assert queue.put(queue.hedge_release(7, manual=True)) is True

    release = await queue.get()
    assert release.kind == "HEDGE_RELEASE"
    queue.complete(release)

    stale_rebalance = await queue.get()
    assert stale_rebalance.kind == "HEDGE_REBALANCE"
    queue.complete(stale_rebalance)
