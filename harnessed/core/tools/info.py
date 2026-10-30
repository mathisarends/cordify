from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from harnessed.core.model import ToolDefinition


class ToolChain(StrEnum):
    CONTINUE = "continue"
    STOP_AFTER = "stop_after"


@dataclass(frozen=True)
class ToolMetadata:
    """Process-local tool annotations, keyed by exact type.

    The collection is immutable; applications must also keep its values immutable.
    Values are neither sent to the model nor persisted automatically.
    """

    values: tuple[object, ...] = ()

    def __post_init__(self) -> None:
        values = tuple(self.values)
        kinds = [type(value) for value in values]
        if len(set(kinds)) != len(kinds):
            raise ValueError("Tool metadata requires one value per exact type")
        object.__setattr__(self, "values", values)

    def get[T](self, kind: type[T]) -> T | None:
        for value in self.values:
            if type(value) is kind:
                # Exact-type lookup is the heterogeneous collection's typed boundary.
                return cast(T, value)
        return None


@dataclass(frozen=True)
class ToolInfo:
    """Public description of the registration captured for a scheduled call."""

    definition: ToolDefinition
    metadata: ToolMetadata
    readonly: bool
    parallel: bool
    chain: ToolChain
