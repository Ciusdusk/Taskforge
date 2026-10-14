"""Wait for actual resource cleanup even under repeated cancellation."""

import asyncio
from collections.abc import Coroutine
from typing import Any


async def drain[T](task: asyncio.Task[T]) -> T:
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


async def protected[T](operation: Coroutine[Any, Any, T], *, cancel: bool = False) -> T:
    task = asyncio.create_task(operation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if cancel:
            task.cancel()
        try:
            await drain(task)
        except BaseException:
            # Cancellation takes priority after the operation actually ends.
            pass
        raise
