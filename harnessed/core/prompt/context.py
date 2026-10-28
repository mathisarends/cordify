"""Changed context values are durable facts, separate from fixed instructions."""

import asyncio
import inspect
from collections.abc import Awaitable, Callable

from harnessed.core.hooks import PreStep
from harnessed.core.plugin import Plugin
from harnessed.core.session.events import ContextValueChanged
from harnessed.core.session.session import Session
from harnessed.kernel import Inject, hook


class DynamicContext:
    """Append changed values once; hosts await writes before the next step."""

    def __init__(self, session: Session):
        self.session = session
        self._values: dict[str, str] = {}
        self._lock = asyncio.Lock()
        for event in session.events:
            if isinstance(event, ContextValueChanged):
                if event.value is None:
                    self._values.pop(event.name, None)
                else:
                    self._values[event.name] = event.value

    @property
    def values(self) -> dict[str, str]:
        return dict(self._values)

    async def set(self, name: str, value: str | None) -> None:
        if not name or not isinstance(name, str):
            raise ValueError("Context names must be non-empty strings")
        if value is not None and not isinstance(value, str):
            raise TypeError("Context values must be strings or None")
        async with self._lock:
            if self._values.get(name) == value:
                return
            # The state record and the visible contribution are one durable event.
            event = ContextValueChanged(name, value)
            try:
                await self.session.append(event)
            finally:
                if self.session.last(ContextValueChanged) is event:
                    if value is None:
                        self._values.pop(name, None)
                    else:
                        self._values[name] = value

    async def remove(self, name: str) -> None:
        await self.set(name, None)


class ContextValue(Plugin):
    """Refresh one named fact before each step, appending only actual changes."""

    def __init__(
        self,
        key: str,
        value: str | None | Callable[[], str | None | Awaitable[str | None]],
    ):
        super().__init__()
        if not isinstance(key, str) or not key:
            raise ValueError("Context names must be non-empty strings")
        self.key, self.value = key, value

    @property
    def context_description(self) -> tuple[str, str]:
        return self.key, "<evaluated per step>" if callable(self.value) else repr(self.value)

    @hook(PreStep)
    async def refresh(self, event, next_, context: Inject[DynamicContext]):
        value = self.value() if callable(self.value) else self.value
        if inspect.isawaitable(value):
            value = await value
        if value is not None and not isinstance(value, str):
            raise TypeError("Context providers must return a string or None")
        await context.set(self.key, value)
        return await next_()

    async def teardown(self, context: Inject[DynamicContext]):
        await context.remove(self.key)
