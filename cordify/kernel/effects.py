import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any

type Disposer = Callable[[], Awaitable[None]]


async def resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def run_sync[T](
    callback: Callable[[], T], *, on_cancel: Callable[[], None] | None = None
) -> T:
    worker = asyncio.create_task(asyncio.to_thread(callback))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        try:
            if on_cancel is not None:
                on_cancel()
        finally:
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            if not worker.cancelled():
                worker.exception()
        raise


def effect(cleanup: Callable[[], Any]) -> Disposer:
    active = True

    async def dispose():
        nonlocal active
        if active:
            active = False
            await resolve(cleanup())

    return dispose
