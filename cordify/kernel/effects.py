import inspect
from collections.abc import Awaitable, Callable
from typing import Any

type Disposer = Callable[[], Awaitable[None]]


async def resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def effect(cleanup: Callable[[], Any]) -> Disposer:
    active = True

    async def dispose():
        nonlocal active
        if active:
            active = False
            await resolve(cleanup())

    return dispose
