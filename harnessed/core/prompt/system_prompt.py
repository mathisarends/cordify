from dataclasses import dataclass

from pydantic import ConfigDict, TypeAdapter

from harnessed.kernel.context import Context, RegistrationState
from harnessed.kernel.effects import Disposer

_TEXT = TypeAdapter(str, config=ConfigDict(strict=True, hide_input_in_errors=True))


@dataclass(frozen=True, eq=False)
class _Snapshot:
    text: str
    registration: RegistrationState


class SystemPromptStore:
    def __init__(self, ctx: Context) -> None:
        self._ctx = ctx
        self._snapshots: list[_Snapshot] = []

    def fixed(self, text: str, *, owner: Context | None = None) -> Disposer:
        """Own an immutable session snapshot; disposal restores the previous owner."""
        owner = owner if owner is not None else Context.current() or self._ctx
        text = _TEXT.validate_python(text)

        def install(registration: RegistrationState):
            snapshot = _Snapshot(text, registration)
            self._snapshots.append(snapshot)
            return lambda: self._snapshots.remove(snapshot)

        return owner.register(install)

    async def render(self) -> str:
        return next(
            (
                snapshot.text
                for snapshot in reversed(self._snapshots)
                if snapshot.registration.active
            ),
            "",
        )
