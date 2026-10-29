import asyncio
from collections import deque
from collections.abc import Callable

from harnessed.core.errors import StreamOverflow

type Merge[T] = Callable[[T, T], T | None]


class EventBuffer[T]:
    """Disconnect a slow consumer without blocking producers or losing silently.

    An optional ``merge`` folds a new event into the last unread one; a fast
    consumer still sees every event, a slow one receives fewer, larger ones.
    """

    def __init__(self, capacity: int = 256, *, merge: Merge[T] | None = None) -> None:
        if type(capacity) is not int or capacity < 1:
            raise ValueError("buffer_size must be a positive integer")
        self._capacity = capacity
        self._merge = merge
        self._items: deque[T] = deque()
        self._ready = asyncio.Event()
        self._overflow: StreamOverflow | None = None

    def put(self, event: T) -> bool:
        if self._overflow is not None:
            return False
        if self._merge is not None and self._items:
            merged = self._merge(self._items[-1], event)
            if merged is not None:
                self._items[-1] = merged
                return True
        if len(self._items) >= self._capacity:
            self._items.clear()
            self._overflow = StreamOverflow(self._capacity)
            self._ready.set()
            return False
        self._items.append(event)
        self._ready.set()
        return True

    async def get(self) -> T:
        while not self._items:
            if self._overflow is not None:
                raise self._overflow
            self._ready.clear()
            await self._ready.wait()
        return self._items.popleft()
