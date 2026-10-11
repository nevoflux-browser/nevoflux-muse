import asyncio

import pytest

from nevoflux_muse.aio import wait_for


async def test_a_cancellation_that_races_the_result_still_propagates():
    # Python 3.10/3.11's asyncio.wait_for returns the inner result and drops the
    # cancellation when both land in the same loop iteration; a loop that is being
    # taken down then keeps running (HeadConnection._serve hung the bridge's stop).
    fut = asyncio.get_running_loop().create_future()

    async def inner():
        return await fut

    waiter = asyncio.create_task(wait_for(inner(), 10))
    await asyncio.sleep(0)  # the waiter is now blocked on inner
    await asyncio.sleep(0)
    fut.set_result("item")
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter


async def test_a_cancelled_wait_cancels_the_inner_awaitable():
    started = asyncio.Event()
    cancelled = []

    async def inner():
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    waiter = asyncio.create_task(wait_for(inner(), 10))
    await started.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert cancelled == [True]


async def test_result_timeout_and_no_timeout():
    async def value():
        return 7

    assert await wait_for(value(), 1) == 7
    assert await wait_for(value(), None) == 7
    with pytest.raises(asyncio.TimeoutError):
        await wait_for(asyncio.sleep(10), 0.01)
    with pytest.raises(asyncio.TimeoutError):
        await wait_for(asyncio.sleep(10), 0)


async def test_a_timeout_cancels_a_future():
    fut = asyncio.get_running_loop().create_future()
    with pytest.raises(asyncio.TimeoutError):
        await wait_for(fut, 0.01)
    assert fut.cancelled()


async def test_the_inner_exception_propagates():
    async def boom():
        raise ValueError("x")

    with pytest.raises(ValueError):
        await wait_for(boom(), 1)
