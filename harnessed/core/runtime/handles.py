import asyncio
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any

from harnessed.core.model import OutputSchema
from harnessed.core.session.events import InboxAdded

from .result import RunResult


class RunHandle[T]:
    """One acknowledged queued run; cancelling a waiter never cancels its execution."""

    def __init__(self, input_id: str) -> None:
        self.id = input_id
        self._completion: asyncio.Future[RunResult[T] | Exception] = (
            asyncio.get_running_loop().create_future()
        )

    def done(self) -> bool:
        return self._completion.done()

    async def result(self) -> RunResult[T]:
        value = await asyncio.shield(self._completion)
        if isinstance(value, Exception):
            raise value
        return value

    def _finish(self, value: RunResult[T] | Exception) -> None:
        if not self._completion.done():
            # Store errors as values so an intentionally unobserved handle does
            # not generate an unhandled Future exception warning.
            self._completion.set_result(value)


@dataclass
class SubmittedRun:
    handle: RunHandle[Any]
    input: InboxAdded
    schema: OutputSchema | None
    expectation: AbstractContextManager
    start: int | None = None
    turn_id: str | None = None
