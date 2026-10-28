"""Changed context values are durable facts, separate from fixed instructions."""

import asyncio

from .events import ContextValueChanged
from .session import Session


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
