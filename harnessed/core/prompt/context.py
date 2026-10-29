import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from xml.sax.saxutils import escape, quoteattr

from harnessed.core.hooks import PreStep, StepDecision
from harnessed.core.messages import User
from harnessed.core.plugin import Plugin
from harnessed.core.session.events import ContextValueChangedEvent
from harnessed.core.session.session import SessionLog
from harnessed.kernel import Context, Disposer, Inject, Next, RegistrationState, hook


@dataclass(frozen=True, eq=False)
class _OwnedContext:
    name: str
    value: str
    registration: RegistrationState


class DynamicContext:
    """Append changed values once; hosts await writes before the next step."""

    def __init__(self, session: SessionLog) -> None:
        self.log = session
        self._values: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._owned: list[_OwnedContext] = []
        for event in session.events:
            if isinstance(event, ContextValueChangedEvent):
                if event.value is None:
                    self._values.pop(event.name, None)
                else:
                    self._values[event.name] = event.value

    @property
    def values(self) -> dict[str, str]:
        return {**self._values, **self._owned_values()}

    def _owned_values(self) -> dict[str, str]:
        return {item.name: item.value for item in self._owned if item.registration.active}

    @property
    def owned_messages(self) -> tuple[User, ...]:
        """Active mount instructions, kept out of durable named-fact history."""
        return tuple(
            User(
                f"<context name={quoteattr(name)}>{escape(value)}</context>",
                id=f"mount-context:{name}",
            )
            for name, value in self._owned_values().items()
        )

    def register(self, name: str, value: str, *, owner: Context) -> Disposer:
        """Own runtime guidance; suspension and disposal immediately hide it.

        Model request records retain what was delivered, but reopening a session
        never restores these values without a new mount.
        """
        if not isinstance(name, str) or not name:
            raise ValueError("Context names must be non-empty strings")
        if not isinstance(value, str):
            raise TypeError("Context values must be strings")

        def install(registration: RegistrationState) -> Callable[[], None]:
            item = _OwnedContext(name, value, registration)
            self._owned.append(item)
            return lambda: self._owned.remove(item)

        return owner.register(install)

    async def set(self, name: str, value: str | None) -> None:
        if not name or not isinstance(name, str):
            raise ValueError("Context names must be non-empty strings")
        if value is not None and not isinstance(value, str):
            raise TypeError("Context values must be strings or None")
        async with self._lock:
            if self._values.get(name) == value:
                return
            # The state record and the visible contribution are one durable event.
            event = ContextValueChangedEvent(name, value)
            try:
                await self.log.append(event)
            finally:
                if self.log.last(ContextValueChangedEvent) is event:
                    if value is None:
                        self._values.pop(name, None)
                    else:
                        self._values[name] = value

    async def remove(self, name: str) -> None:
        await self.set(name, None)


class ContextValue(Plugin, needs=(DynamicContext,)):
    """Refresh one named fact before each step, appending only actual changes."""

    def __init__(
        self,
        key: str,
        value: str | None | Callable[[], str | None | Awaitable[str | None]],
    ) -> None:
        super().__init__()
        if not isinstance(key, str) or not key:
            raise ValueError("Context names must be non-empty strings")
        self.key = key
        self.value = value

    @property
    def context_description(self) -> tuple[str, str]:
        return self.key, "<evaluated per step>" if callable(self.value) else repr(self.value)

    @hook(PreStep)
    async def refresh(
        self, event: PreStep, next_: Next[StepDecision], context: Inject[DynamicContext]
    ) -> StepDecision:
        value = self.value() if callable(self.value) else self.value
        if inspect.isawaitable(value):
            value = await value
        if value is not None and not isinstance(value, str):
            raise TypeError("Context providers must return a string or None")
        await context.set(self.key, value)
        return await next_()

    async def teardown(self, context: Inject[DynamicContext]) -> None:
        await context.remove(self.key)
