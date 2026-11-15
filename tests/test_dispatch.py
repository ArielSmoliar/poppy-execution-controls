"""FIFO ordering, bounded concurrency and cancellation at resource-grant boundaries."""

import asyncio

import pytest

from poppy_controls.metered.dispatch import Dispatcher


def job(seq):
    return {"id": f"job-{seq}", "owner": "customer", "seq": seq}


async def test_fifo_selects_oldest_arrived_sequence():
    dispatch = Dispatcher("fifo", 1, 10)
    first = job(1)
    await dispatch.acquire(first)
    order = []

    async def worker(item):
        handle = await dispatch.acquire(item)
        order.append(item["seq"])
        await dispatch.release(item, handle)

    # Both are eligible by the time a slot is free; arrival sequence determines order.
    waiting = [asyncio.create_task(worker(job(seq))) for seq in (3, 2, 4)]
    await asyncio.sleep(0)
    await dispatch.release(first, None)
    await asyncio.wait_for(asyncio.gather(*waiting), timeout=1)
    assert order == [2, 3, 4]
    assert dispatch.active == 0 and not dispatch.waiters


async def test_capacity_bounds_concurrent_execution():
    dispatch = Dispatcher("fifo", 2, 10)
    full, release = asyncio.Event(), asyncio.Event()
    active = peak = 0

    async def worker(seq):
        nonlocal active, peak
        item = job(seq)
        handle = await dispatch.acquire(item)
        active += 1
        peak = max(peak, active)
        if active == 2:
            full.set()
        await release.wait()
        active -= 1
        await dispatch.release(item, handle)

    pending = [asyncio.create_task(worker(seq)) for seq in range(8)]
    await asyncio.wait_for(full.wait(), timeout=1)
    assert dispatch.active == 2 and len(dispatch.waiters) == 6
    release.set()
    await asyncio.wait_for(asyncio.gather(*pending), timeout=1)
    assert peak == 2 and active == 0
    assert dispatch.active == 0 and not dispatch.waiters


@pytest.mark.parametrize("after_grant", [False, True])
async def test_cancellation_before_or_after_grant_does_not_leak_a_slot(after_grant):
    dispatch = Dispatcher("fifo", 1, 2)
    first, cancelled, last = job(1), job(2), job(3)
    await dispatch.acquire(first)
    waiting = asyncio.create_task(dispatch.acquire(cancelled))
    final = asyncio.create_task(dispatch.acquire(last))
    await asyncio.sleep(0)
    if after_grant:
        # release sets the waiter's result synchronously, before its coroutine resumes.
        await dispatch.release(first, None)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    if not after_grant:
        await dispatch.release(first, None)
    handle = await asyncio.wait_for(final, timeout=1)
    assert dispatch.active == 1
    await dispatch.release(last, handle)
    assert dispatch.active == 0 and not dispatch.waiters


async def test_queue_rejection_does_not_consume_capacity():
    dispatch = Dispatcher("fifo", 1, 1)
    await dispatch.acquire(job(1))
    waiting = asyncio.create_task(dispatch.acquire(job(2)))
    await asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="queue is full"):
        await dispatch.acquire(job(3))
    assert dispatch.active == 1 and len(dispatch.waiters) == 1
    await dispatch.release(job(1), None)
    handle = await asyncio.wait_for(waiting, timeout=1)
    await dispatch.release(job(2), handle)
    handle = await asyncio.wait_for(dispatch.acquire(job(4)), timeout=1)
    await dispatch.release(job(4), handle)
    assert dispatch.active == 0 and not dispatch.waiters


@pytest.mark.parametrize(
    "policy,capacity,max_pending",
    [("loco", 1, 1), ("fifo", 0, 1), ("fifo", True, 1), ("fifo", 1, 0), ("fifo", 1, True)],
)
def test_invalid_dispatch_configuration(policy, capacity, max_pending):
    with pytest.raises(ValueError):
        Dispatcher(policy, capacity, max_pending)
