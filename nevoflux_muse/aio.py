"""asyncio helpers that behave the same on every Python we support (>= 3.10).

Python 3.10 and 3.11's asyncio.wait_for drops a cancellation that lands in the
same loop iteration as the inner result: it returns the result and the caller
never sees CancelledError (fixed in 3.12, cpython gh-86296). A loop that is being
taken down then keeps running; HeadConnection._serve did exactly that and left
the bridge's stop waiting forever on 3.10.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

T = TypeVar("T")


async def wait_for(aw: Awaitable[T], timeout: float | None) -> T:
    """asyncio.wait_for, except that cancelling the caller always propagates.

    A timeout cancels the awaitable and raises asyncio.TimeoutError, as
    asyncio.wait_for does; None waits as long as it takes.
    """
    if timeout is None:
        return await aw
    task = asyncio.ensure_future(aw)
    try:
        done, _ = await asyncio.wait({task}, timeout=max(0.0, timeout))
    except asyncio.CancelledError:
        task.cancel()
        await asyncio.wait({task})  # its own cleanup runs before ours
        if not task.cancelled():
            task.exception()  # a result or error we drop is not "never retrieved"
        raise
    if done:
        return task.result()
    task.cancel()
    await asyncio.wait({task})
    if task.cancelled():
        raise asyncio.TimeoutError
    return task.result()  # it finished while being cancelled: keep what it made
