import inspect
import json
from collections.abc import Mapping
from copy import copy
from dataclasses import dataclass
from functools import cached_property
from typing import Annotated, Any, Self, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, create_model
from pydantic.fields import FieldInfo
from pydantic.json_schema import JsonSchemaValue

from harnessed.core.messages import result_content, to_dict
from harnessed.kernel.di import Dependency

_CONFIG = ConfigDict(
    strict=True,
    extra="forbid",
    validate_default=True,
    allow_inf_nan=False,
    hide_input_in_errors=True,
)
_SERIALIZER = TypeAdapter(Any, config=ConfigDict(ser_json_inf_nan="constants"))


@dataclass(frozen=True)
class ToolArguments:
    model: type[BaseModel]
    names: dict[str, str]

    @classmethod
    def from_signature(
        cls,
        name: str,
        signature: inspect.Signature,
        hints: Mapping[str, Any],
        injected: Mapping[str, Dependency],
    ) -> Self:
        # Pydantic accepts runtime type expressions as field definitions.
        fields: dict[str, Any] = {}
        names: dict[str, str] = {}
        for parameter in signature.parameters.values():
            if parameter.kind not in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            ):
                raise TypeError(
                    "Tools must have named parameters (no *args, **kwargs or positional-only)"
                )
            if parameter.name in injected:
                continue
            annotation = hints.get(parameter.name, parameter.annotation)
            if annotation is inspect.Parameter.empty:
                raise TypeError(f"Tool parameter {parameter.name} requires a type annotation")
            if get_origin(annotation) is Annotated:
                base, *metadata = get_args(annotation)
                descriptions = [item for item in metadata if isinstance(item, str)]
                if descriptions:
                    annotation = Annotated[base, *metadata, Field(description=descriptions[0])]
            default = parameter.default
            if isinstance(default, FieldInfo):
                field = copy(default)
                field.alias = field.validation_alias = field.serialization_alias = parameter.name
            else:
                field = Field(
                    default=... if default is inspect.Parameter.empty else default,
                    alias=parameter.name,
                )
            # Aliases preserve Python parameter names, including BaseModel's reserved names.
            internal = f"argument_{len(fields)}"
            fields[internal], names[internal] = (annotation, field), parameter.name
        model = create_model(f"{name}Arguments", __config__=_CONFIG, **fields)
        model.model_json_schema()  # Reject annotations without a JSON schema at registration.
        return cls(model, names)

    @cached_property
    def schema(self) -> JsonSchemaValue:
        return self.model.model_json_schema(by_alias=True)

    def parse(self, arguments: Mapping[str, object]) -> dict[str, object]:
        parsed = self.model.model_validate_json(
            json.dumps(to_dict(arguments), allow_nan=False),
            strict=True,
        )
        return {name: getattr(parsed, internal) for internal, name in self.names.items()}


def tool_result_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    return result_content(_SERIALIZER.dump_python(value, mode="json"))
