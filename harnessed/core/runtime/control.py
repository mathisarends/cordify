import asyncio
from collections.abc import AsyncGenerator, Callable, Coroutine
from contextlib import asynccontextmanager

from harnessed.core.errors import SessionClosed, SessionStateError
from harnessed.core.session.control import SessionStatus as SessionStatus
from harnessed.core.session.events import AgentControlChanged, ControlState
from harnessed.core.session.session import SessionLog


class StopAtBoundary(Exception):
    """Internal control flow: a complete step must end its turn."""


_TRANSITIONS = {
    SessionStatus.IDLE: {SessionStatus.RUNNING, SessionStatus.PAUSED, SessionStatus.STOPPING},
    SessionStatus.RUNNING: {SessionStatus.IDLE, SessionStatus.PAUSED, SessionStatus.STOPPING},
    SessionStatus.PAUSED: {SessionStatus.IDLE, SessionStatus.RUNNING, SessionStatus.STOPPING},
    SessionStatus.STOPPING: {SessionStatus.IDLE},
    SessionStatus.CLOSED: set(),
}


class SessionControl:
    """Own the runner and acknowledge controls only at their defined boundaries.

    Plugins can depend on this narrow service without depending on the driver.
    Cancelling a control caller never cancels a step that is finishing gracefully.
    """

    def __init__(
        self,
        log: SessionLog,
        drive: Callable[[], Coroutine[None, None, None]],
        inside_driver: Callable[[], bool],
    ) -> None:
        self.log = log
        self._drive = drive
        self._inside_driver = inside_driver
        self.status = SessionStatus.IDLE
        self.task: asyncio.Task[None] | None = None
        self.error: Exception | None = None
        self.ready = False
        self.finishing = False
        self.closing = False
        self._changed = asyncio.Event()
        self._request_lock = asyncio.Lock()
        self._maintenance = asyncio.Lock()
        self._maintenance_owner: asyncio.Task | None = None
        self._request_owner: asyncio.Task | None = None
        self.synchronize()

    def check_open(self) -> None:
        if self.closing or self.status is SessionStatus.CLOSED:
            raise SessionClosed("This session is closed")

    def _check_external(self) -> None:
        self.check_open()
        if self._inside_driver():
            raise SessionStateError(
                "A driver hook or tool cannot wait for its own control boundary"
            )

    def notify(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()

    @asynccontextmanager
    async def _request(self) -> AsyncGenerator[None]:
        task = asyncio.current_task()
        if self._request_owner is task:
            yield
            return
        async with self._request_lock:
            self._request_owner = task
            try:
                yield
            finally:
                self._request_owner = None

    def synchronize(self) -> None:
        state = self.log.control.state
        if self.status is SessionStatus.CLOSED:
            return
        if state in (ControlState.STOPPING, ControlState.ABORTING, ControlState.STOPPED):
            target = SessionStatus.STOPPING
        elif state == ControlState.PAUSED:
            target = SessionStatus.PAUSED
        elif self.task is not None and not self.task.done() and not self.finishing:
            target = SessionStatus.RUNNING
        else:
            target = SessionStatus.IDLE
        if target != self.status:
            if target not in _TRANSITIONS[self.status]:
                raise SessionStateError(f"Invalid session transition: {self.status} -> {target}")
            self.status = target
        self.notify()

    def start(self) -> None:
        self.ready = True
        self.wake()

    def wake(self) -> None:
        if not self.ready or self.closing or self.log.control.state != ControlState.ACTIVE:
            return
        if not (self.log.has_wakeup or self.log.open_turn is not None):
            return
        if self.task is not None and not self.task.done():
            if self.finishing:
                self.task.add_done_callback(lambda _: self.wake())
            return
        self.error = None
        self.finishing = False
        self.task = asyncio.create_task(self._drive(), name=f"harnessed:{self.log.id}")
        self.task.add_done_callback(lambda _: self.notify())
        self.synchronize()

    async def pause(self) -> None:
        self._check_external()
        async with self._request():
            self.check_open()
            if self.log.control.state in (ControlState.STOPPING, ControlState.ABORTING):
                return
            if not self.log.control.paused:
                running = self.task is not None and not self.task.done() and not self.finishing
                await self.log.append(
                    AgentControlChanged(ControlState.PAUSING if running else ControlState.PAUSED)
                )
        while self.log.control.state == ControlState.PAUSING:
            if self.error is not None:
                raise self.error
            await self._changed.wait()
            self.check_open()

    async def resume(self) -> None:
        self.check_open()
        async with self._request():
            self.check_open()
            if self.log.control.state in (ControlState.STOPPING, ControlState.ABORTING):
                return
            if self.log.control.state != ControlState.ACTIVE:
                await self.log.append(AgentControlChanged(ControlState.ACTIVE))
        self.wake()

    @asynccontextmanager
    async def maintenance(self) -> AsyncGenerator[None]:
        """Reserve an idle/paused boundary while permitting durable queued input."""
        self._check_external()
        task = asyncio.current_task()
        if self._maintenance_owner is task:
            raise SessionStateError(
                "A maintenance hook cannot reserve its own boundary recursively"
            )
        async with self._request(), self._maintenance:
            self.check_open()
            if self.status not in (SessionStatus.IDLE, SessionStatus.PAUSED):
                raise SessionStateError(
                    "Pause the session before changing a running turn's history"
                )
            self._maintenance_owner = task
            try:
                yield
            finally:
                self._maintenance_owner = None

    async def checkpoint(self) -> None:
        while True:
            async with self._maintenance:
                state = self.log.control.state
            if state == ControlState.ACTIVE:
                return
            if state == ControlState.PAUSING:
                await self.log.append(AgentControlChanged(ControlState.PAUSED))
            elif state == ControlState.STOPPING:
                raise StopAtBoundary
            elif state in (ControlState.ABORTING, ControlState.STOPPED):
                raise asyncio.CancelledError
            else:
                await self._changed.wait()

    async def stop(self, *, keep_inbox: bool = False) -> None:
        await self._terminate(immediate=False, keep_inbox=keep_inbox)

    async def abort(self, *, keep_inbox: bool = False) -> None:
        await self._terminate(immediate=True, keep_inbox=keep_inbox)

    async def _terminate(
        self, *, immediate: bool, keep_inbox: bool, during_close: bool = False
    ) -> None:
        if not during_close:
            self._check_external()
        if type(keep_inbox) is not bool:
            raise TypeError("keep_inbox must be a bool")
        async with self._request():
            if not during_close:
                self.check_open()
            task = self.task
            if task is not None and (task.done() or self.finishing):
                task = None
            busy = task is not None
            if not busy and self.log.open_turn is None and not self.log.inbox.pending:
                if self.log.control.state != ControlState.ACTIVE:
                    await self.log.append(AgentControlChanged(ControlState.ACTIVE))
                return
            state = self.log.control.state
            requested = ControlState.ABORTING if immediate else ControlState.STOPPING
            if state not in (requested, ControlState.ABORTING):
                try:
                    await self.log.append(AgentControlChanged(requested, keep_inbox))
                finally:
                    if (
                        immediate
                        and task is not None
                        and self.log.control.state == ControlState.ABORTING
                    ):
                        if not task.cancelling():
                            task.cancel()
            if not busy:
                await self.finish_termination()
        if task is not None:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.cancelled():
                    raise
                await self.finish_termination()
        self.synchronize()

    async def finish_termination(self) -> None:
        self.finishing = True
        await self.log.repair_interrupted()
        self.notify()

    async def finish_driver(self) -> None:
        self.finishing = True
        if self.log.control.stopped:
            await self.finish_termination()
        if self.log.control.state == ControlState.PAUSING:
            await self.log.append(AgentControlChanged(ControlState.PAUSED))
        self.synchronize()

    async def join(self) -> None:
        """Wait for the current driver, including when it leaves deferred inputs pending."""
        self._check_external()
        while self.task is not None and not self.task.done():
            await asyncio.shield(self.task)
        if self.error is not None:
            raise self.error

    async def wait_until_idle(self) -> None:
        self._check_external()
        while True:
            self.check_open()
            if self.error is not None:
                raise self.error
            if self.status is SessionStatus.IDLE and not self.log.inbox.pending:
                return
            await self._changed.wait()

    async def close(self) -> None:
        if self.status is SessionStatus.CLOSED:
            return
        self._check_external()
        async with self._request():
            self.closing = True
        # A durable pause survives closing its live handle and reopening it later.
        if not self.log.control.paused:
            await self._terminate(immediate=True, keep_inbox=False, during_close=True)
        if self.task is not None and not self.task.done():
            self.task.cancel()
            try:
                await asyncio.shield(self.task)
            except asyncio.CancelledError:
                if not self.task.cancelled():
                    raise
        await self.log.flush()
        self.status = SessionStatus.CLOSED
        self.notify()
