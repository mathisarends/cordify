import hashlib
from collections.abc import Mapping
from dataclasses import field
from typing import Annotated, Literal, Self

from pydantic import ConfigDict, Field, PlainSerializer, TypeAdapter, ValidationError
from pydantic.dataclasses import dataclass

from harnessed.core.assets import AssetRef, AssetValue
from harnessed.core.errors import SessionFormatError
from harnessed.core.messages import (
    AssistantMessage,
    ImageReference,
    Message,
    SpillRef,
    ToolMessage,
    freeze,
)

from .events import ContextLifetime, IncludedObservation
from .files import TextValue

_CONFIG = ConfigDict(
    extra="forbid",
    strict=True,
    allow_inf_nan=False,
    ser_json_bytes="base64",
    val_json_bytes="base64",
    hide_input_in_errors=True,
)


@dataclass(frozen=True, config=_CONFIG)
class HistoryEntry:
    position: Annotated[int, Field(ge=0)]
    message: Message
    turn_id: str | None = None
    lifetime: ContextLifetime = ContextLifetime.SESSION
    observation: IncludedObservation | None = None
    context_name: str | None = None
    resources: tuple[SpillRef, ...] = ()


@dataclass(frozen=True, config=_CONFIG)
class HistoryTurn:
    id: str
    boundary: Annotated[int, Field(ge=1)]


def image_refs(entries: tuple[HistoryEntry, ...]) -> set[AssetRef]:
    return {
        part.asset
        for entry in entries
        if not isinstance(entry.message.content, str)
        for part in entry.message.content
        if isinstance(part, ImageReference)
    }


@dataclass(frozen=True, config=_CONFIG)
class SessionState:
    """An immutable, self-contained conversation at a terminal turn boundary.

    Configuration, permissions, queued work and live resources belong to the
    new session. Importing this value never executes historical tools.
    """

    __hash__ = None

    version: Literal[1] = 1
    history: tuple[HistoryEntry, ...] = ()
    context: Annotated[Mapping[str, str], PlainSerializer(dict, when_used="json")] = field(
        default_factory=dict
    )
    turns: tuple[HistoryTurn, ...] = ()
    turns_since_compaction: Annotated[int, Field(ge=0)] = 0
    compacted_through: Annotated[int, Field(ge=0)] = 0
    preserved_turns: tuple[str, ...] = ()
    observation_limit: Annotated[int, Field(ge=1)] | None = None
    assets: tuple[AssetValue, ...] = ()
    files: tuple[TextValue, ...] = ()

    def __post_init__(self) -> None:
        if type(self.version) is not int:
            raise ValueError("State versions must be integers")
        object.__setattr__(self, "context", freeze(self.context))
        positions = [entry.position for entry in self.history]
        if len(set(positions)) != len(positions):
            raise ValueError("History positions must be unique")
        if any(entry.lifetime != ContextLifetime.SESSION for entry in self.history):
            raise ValueError("A snapshot cannot contain turn-scoped context")
        if len({turn.id for turn in self.turns}) != len(self.turns):
            raise ValueError("Retained turn identities must be unique")
        if not set(self.preserved_turns) <= {turn.id for turn in self.turns}:
            raise ValueError("Pinned turns must be present in the snapshot")
        pending: set[str] = set()
        for entry in self.history:
            message = entry.message
            if message.id == "system" or message.id.startswith("mount-context:"):
                raise ValueError("Agent configuration is not conversation state")
            if isinstance(message, AssistantMessage):
                if pending:
                    raise ValueError("Tool calls must be answered before the next assistant")
                for call in message.tool_calls:
                    if call.id in pending:
                        raise ValueError("A tool batch cannot contain duplicate call identities")
                    pending.add(call.id)
            elif isinstance(message, ToolMessage):
                if message.tool_call_id not in pending:
                    raise ValueError("Tool results must match a retained tool call")
                pending.remove(message.tool_call_id)
        if pending:
            raise ValueError("A snapshot cannot contain unanswered tool calls")
        refs = image_refs(self.history)
        if refs != {value.ref for value in self.assets} or len(refs) != len(self.assets):
            raise ValueError("Snapshot images must exactly match retained references")
        for value in self.assets:
            value.ref.verify(value.data)
        resources = {ref.path: ref for entry in self.history for ref in entry.resources}
        if len({value.key for value in self.files}) != len(self.files):
            raise ValueError("Snapshot text resources must be unique")
        if resources.keys() != {value.key for value in self.files}:
            raise ValueError("Snapshot text resources must match retained references")
        for value in self.files:
            if (
                hashlib.sha256(value.text.encode("utf-8")).hexdigest()
                != resources[value.key].sha256
            ):
                raise ValueError("Snapshot text resource integrity check failed")

    def to_json(self) -> str:
        """Serialize in memory; this method performs no filesystem or network I/O."""
        return _STATE.dump_json(self).decode("utf-8")

    @classmethod
    def from_json(cls, payload: str | bytes) -> Self:
        """Validate the current canonical format, including resource integrity."""
        try:
            return TypeAdapter(cls).validate_json(payload)
        except (ValidationError, ValueError, TypeError) as error:
            raise SessionFormatError("Invalid conversation state") from error


_STATE = TypeAdapter(SessionState)
