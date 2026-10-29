import json
from collections import Counter
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, cast, get_origin

from pydantic import ConfigDict, TypeAdapter, ValidationError, create_model

from harnessed.core.errors import ConfigError
from harnessed.core.messages import freeze, to_dict
from harnessed.core.model import OutputSchema

FINAL_ANSWER = "final_answer"


def _reject_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON constant: {value}")


@dataclass(frozen=True)
class CompiledOutput[T]:
    definition: OutputSchema
    adapter: TypeAdapter[T]
    wire_adapter: TypeAdapter[Any]

    def parse(self, value: str | dict) -> tuple[T, Any]:
        """Return a typed value and the exact accepted JSON input for durable replay."""
        text = value if isinstance(value, str) else json.dumps(to_dict(value), allow_nan=False)
        parsed = self.wire_adapter.validate_json(text, strict=True)
        data = freeze(json.loads(text, parse_constant=_reject_constant))
        return cast(T, parsed.output if self.definition.wrapped else parsed), data


def _signature(definition: OutputSchema) -> str:
    return json.dumps(
        [definition.wrapped, to_dict(definition.json_schema)], sort_keys=True, separators=(",", ":")
    )


@dataclass(frozen=True)
class OutputContract[T]:
    """An application-owned, versioned identity for one Python output validator.

    Change the key when validation semantics change, even if JSON Schema is unchanged.
    Reopening a session requires explicit registration; keys never trigger imports.
    """

    key: str
    output: type[T]

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key.strip():
            raise ValueError("Output contract keys must be non-empty")


def _output_key(output: object) -> str:
    if isinstance(output, type) and get_origin(output) is None:
        return f"{output.__module__}:{output.__qualname__}"
    return str(output)


class OutputTypes:
    """Register Python validators by identity; persisted schemas never load code."""

    def __init__(self) -> None:
        # The registry is heterogeneous; each register() call preserves its own T.
        self._ids: dict[str, CompiledOutput[Any]] = {}
        self._owners: dict[str, object] = {}

    def register[T](self, output: type[T] | OutputContract[T]) -> CompiledOutput[T]:
        key = output.key if isinstance(output, OutputContract) else _output_key(output)
        kind = output.output if isinstance(output, OutputContract) else output
        if known := self._ids.get(key):
            if self._owners[key] != kind:
                raise ConfigError("An output contract key cannot name different Python validators")
            return known
        try:
            adapter = TypeAdapter(kind)
            schema = adapter.json_schema()
            wrapped = schema.get("type") != "object"
            wire_adapter = (
                TypeAdapter(
                    create_model(
                        "StructuredAnswer",
                        __config__=ConfigDict(extra="forbid", strict=True),
                        output=(kind, ...),
                    )
                )
                if wrapped
                else adapter
            )
            definition = OutputSchema(
                key,
                getattr(kind, "__name__", "response"),
                wire_adapter.json_schema(),
                wrapped,
            )
        except Exception as error:
            raise ConfigError(f"Output types must have a Pydantic JSON schema: {error}") from error
        compiled = CompiledOutput(definition, adapter, wire_adapter)
        self._owners[key] = kind
        self._ids[key] = compiled
        return compiled

    def resolve(self, definition: OutputSchema) -> CompiledOutput[Any]:
        if known := self._ids.get(definition.id):
            if _signature(known.definition) != _signature(definition):
                raise ConfigError("An output contract ID cannot change its schema")
            return known
        raise ConfigError(
            f"Restore output contract {definition.id!r} before resuming: "
            "agent.session(id, restore_outputs=[YourOutputTypeOrContract])"
        )


class OutputValues:
    """Keep validated values until active callers consume them; bound abandoned results."""

    def __init__(self) -> None:
        self._values: dict[str, tuple[str, Any]] = {}
        self._active: Counter[str] = Counter()

    def hold(self, schema: OutputSchema | None) -> None:
        """Keep values for this schema until the matching release()."""
        if schema is not None:
            self._active[schema.id] += 1

    def release(self, schema: OutputSchema | None) -> None:
        if schema is not None:
            self._active[schema.id] -= 1
            if not self._active[schema.id]:
                del self._active[schema.id]
        self._prune()

    @contextmanager
    def expect(self, schema: OutputSchema | None) -> Generator[None]:
        self.hold(schema)
        try:
            yield
        finally:
            self.release(schema)

    def put(self, event_id: str, schema_id: str, value: Any) -> None:
        self._values[event_id] = (schema_id, value)
        self._prune()

    def __contains__(self, event_id: str) -> bool:
        return event_id in self._values

    def pop(self, event_id: str) -> Any:
        return self._values.pop(event_id)[1]

    def _prune(self) -> None:
        for event_id, (schema_id, _) in tuple(self._values.items()):
            if len(self._values) <= 128:
                break
            if schema_id not in self._active:
                del self._values[event_id]


def validation_note(error: Exception) -> str:
    """Give the model actionable field locations without echoing rejected input values."""
    if isinstance(error, ValidationError):
        details = [
            f"{'.'.join(map(str, item['loc'])) or 'answer'}: {item['type']}"
            for item in error.errors(include_input=False, include_url=False)
        ]
        return "Invalid structured answer. Correct these fields: " + "; ".join(details[:12])
    return "Invalid structured answer. Return a JSON value matching the declared response schema."
