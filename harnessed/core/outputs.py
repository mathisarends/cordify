"""Compile response contracts without importing a provider SDK or executing log data."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, cast
from uuid import uuid4

from pydantic import ConfigDict, TypeAdapter, ValidationError, create_model

from harnessed.core.errors import ConfigError
from harnessed.core.messages import freeze, to_dict
from harnessed.core.model import OutputSchema

FINAL_ANSWER = "final_answer"


def _reject_constant(value: str):
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


class OutputTypes:
    """An application-owned registry; logs contain schemas, never importable class paths."""

    def __init__(self):
        self._types: dict[object, CompiledOutput[Any]] = {}
        self._ids: dict[str, CompiledOutput[Any]] = {}

    def register[T](self, output: type[T]) -> CompiledOutput[T]:
        if output in self._types:
            return self._types[output]
        try:
            adapter = TypeAdapter(output)
            schema = adapter.json_schema()
            wrapped = schema.get("type") != "object"
            wire_adapter = (
                TypeAdapter(
                    create_model(
                        "StructuredAnswer",
                        __config__=ConfigDict(extra="forbid", strict=True),
                        output=(output, ...),
                    )
                )
                if wrapped
                else adapter
            )
            definition = OutputSchema(
                uuid4().hex,
                getattr(output, "__name__", "response"),
                wire_adapter.json_schema(),
                wrapped,
            )
        except Exception as error:
            raise ConfigError(f"Output types must have a Pydantic JSON schema: {error}") from error
        compiled = CompiledOutput(definition, adapter, wire_adapter)
        self._types[output] = compiled
        self._ids[definition.id] = compiled
        return compiled

    def resolve(self, definition: OutputSchema) -> CompiledOutput[Any]:
        if known := self._ids.get(definition.id):
            if _signature(known.definition) != _signature(definition):
                raise ConfigError("An output contract ID cannot change its schema")
            return known
        for known in self._types.values():
            if _signature(known.definition) == _signature(definition):
                restored = replace(known, definition=definition)
                self._ids[definition.id] = restored
                return restored
        raise ConfigError(
            f"Restore the output type {definition.name!r} before resuming: "
            "agent.session(id, restore_outputs=[YourOutputType])"
        )


def validation_note(error: Exception) -> str:
    """Give the model actionable field locations without echoing rejected input values."""
    if isinstance(error, ValidationError):
        details = [
            f"{'.'.join(map(str, item['loc'])) or 'answer'}: {item['type']}"
            for item in error.errors(include_input=False, include_url=False)
        ]
        return "Invalid structured answer. Correct these fields: " + "; ".join(details[:12])
    return "Invalid structured answer. Return a JSON value matching the declared response schema."
