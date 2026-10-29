import asyncio
from dataclasses import dataclass, field
from typing import Any

from harnessed.core.model import OutputSchema
from harnessed.core.session.events import InboxAdded

from .result import RunResult


class RunHandle[T]:
    """One acknowledged queued run; cancelling a waiter never cancels its execution."""

    def __init__(self, input_id: str, completion: asyncio.Future[RunResult[T] | Exception]) -> None:
        self.id = input_id
        self._completion = completion

    def done(self) -> bool:
        return self._completion.done()

    async def result(self) -> RunResult[T]:
        value = await asyncio.shield(self._completion)
        if isinstance(value, Exception):
            raise value
        return value


@dataclass
class SubmittedRun:
    """Session-side state of one submitted run; only it completes the handle."""

    input: InboxAdded
    schema: OutputSchema | None
    start: int | None = None
    turn_id: str | None = None
    handle: RunHandle[Any] = field(init=False)
    _completion: asyncio.Future[RunResult[Any] | Exception] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._completion = asyncio.get_running_loop().create_future()
        self.handle = RunHandle(self.input.message.id, self._completion)

    def finish(self, value: RunResult[Any] | Exception) -> None:
        if not self._completion.done():
            # Store errors as values so an intentionally unobserved handle does
            # not generate an unhandled Future exception warning.
            self._completion.set_result(value)
