import asyncio

from harnessed.core.errors import StreamOverflow


class EventBuffer[T]:
    """Disconnect a slow consumer without blocking producers or losing silently."""

    def __init__(self, capacity: int = 256) -> None:
        if type(capacity) is not int or capacity < 1:
            raise ValueError("buffer_size must be a positive integer")
        self._queue: asyncio.Queue[T | StreamOverflow] = asyncio.Queue(maxsize=capacity)
        self._failed = False

    def put(self, event: T) -> bool:
        if self._failed:
            return False
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._failed = True
            while not self._queue.empty():
                self._queue.get_nowait()
            self._queue.put_nowait(StreamOverflow(self._queue.maxsize))
            return False
        return True

    async def get(self) -> T:
        event = await self._queue.get()
        if isinstance(event, StreamOverflow):
            raise event
        return event
