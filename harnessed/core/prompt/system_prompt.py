from dataclasses import dataclass

from pydantic import ConfigDict, TypeAdapter

from harnessed.kernel.context import Context
from harnessed.kernel.effects import Disposer

_TEXT = TypeAdapter(str, config=ConfigDict(strict=True, hide_input_in_errors=True))


@dataclass(frozen=True, eq=False)
class _Snapshot:
    text: str
    owner: Context


class SystemPrompt:
    def __init__(self, ctx: Context):
        self._ctx = ctx
        self._snapshots: list[_Snapshot] = []

    def fixed(self, text: str, *, owner: Context | None = None) -> Disposer:
        """Own an immutable session snapshot; disposal restores the previous owner."""
        owner = owner if owner is not None else Context.current() or self._ctx
        owner._check_open()
        snapshot = _Snapshot(_TEXT.validate_python(text), owner)
        self._snapshots.append(snapshot)
        return owner.effect(lambda: self._snapshots.remove(snapshot))

    async def render(self) -> str:
        return next(
            (
                snapshot.text
                for snapshot in reversed(self._snapshots)
                if snapshot.owner._registrations_active()
            ),
            "",
        )
