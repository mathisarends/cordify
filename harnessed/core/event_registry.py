import json
from collections.abc import Iterable
from dataclasses import fields, is_dataclass
from functools import reduce
from operator import or_
from typing import Annotated, Any, Literal, Self, cast, get_args, get_origin, get_type_hints

from pydantic import ConfigDict, Field, TypeAdapter

from harnessed.core.messages import to_dict
from harnessed.core.session.codec import _validate_event, decode_event, encode_event
from harnessed.core.session.events import SESSION_EVENT_TYPES, DurableEvent, SessionEvent
from harnessed.core.stream import CustomTransientEvent, StreamEvent, TransientEvent


class EventRegistry:
    """A session-local event catalog, populated before persistence is opened.

    Event declarations outlive mounted plugins so their stored records remain
    readable after plugin disposal. Different classes may never share a tag.
    """

    def __init__(self, kinds: Iterable[type[SessionEvent | TransientEvent]] = ()):
        live = tuple(
            get_origin(kind) or kind for kind in get_args(get_args(StreamEvent.__value__)[0])
        )
        self._types: dict[str, type] = dict(SESSION_EVENT_TYPES)
        self._types.update(
            {
                cast(str, next(f.default for f in fields(kind) if f.name == "type")): kind
                for kind in live
            }
        )
        self.builtin_types = frozenset(self._types.values())
        self._custom: dict[type[SessionEvent | TransientEvent], TypeAdapter] = {}
        self._adapter: TypeAdapter | None = None
        self.register(kinds)

    def copy(self) -> Self:
        return type(self)(self._custom)

    def register(self, kinds: Iterable[type[SessionEvent | TransientEvent]]) -> None:
        proposed = dict(self._types)
        validators = dict(self._custom)
        for kind in kinds:
            if not isinstance(kind, type) or not issubclass(
                kind, DurableEvent | CustomTransientEvent
            ):
                raise TypeError("Events must derive from DurableEvent or TransientEvent")
            if kind in proposed.values():
                continue
            parameters = getattr(kind, "__dataclass_params__", None)
            if not is_dataclass(kind) or parameters is None or not parameters.frozen:
                raise TypeError("Custom events must be frozen dataclasses")
            if any(issubclass(kind, builtin) for builtin in self.builtin_types):
                raise TypeError("Custom events must not subclass built-in event kinds")
            hint = get_type_hints(kind, include_extras=True).get("type")
            if get_origin(hint) is not Literal or len(get_args(hint)) != 1:
                raise TypeError("Custom events require a single Literal type discriminator")
            tag = get_args(hint)[0]
            if (
                not isinstance(tag, str)
                or "." not in tag
                or any(not part for part in tag.split("."))
            ):
                raise ValueError("Custom event tags must be nonempty namespaced strings")
            tag_field = next((item for item in fields(kind) if item.name == "type"), None)
            if tag_field is None or tag_field.default != tag:
                raise ValueError("Event discriminator default must match its Literal tag")
            if tag in proposed:
                raise ValueError(f"Duplicate event tag: {tag}")
            adapter = TypeAdapter(kind)
            adapter.json_schema()
            validators[kind] = adapter
            proposed[tag] = kind
        self._types, self._custom = proposed, validators
        self._adapter = None

    def validate(self, event: SessionEvent | TransientEvent) -> None:
        _validate_event(event, self._types)
        adapter = self._custom.get(type(event))
        if adapter is not None:
            adapter.validate_json(json.dumps(to_dict(event), allow_nan=False), strict=True)

    @property
    def adapter(self) -> TypeAdapter:
        if self._adapter is None:
            kinds = [kind for kind in self._types.values() if issubclass(kind, SessionEvent)]
            annotation = cast(Any, Annotated)[reduce(or_, kinds), Field(discriminator="type")]
            self._adapter = TypeAdapter(
                annotation,
                config=ConfigDict(
                    strict=True, extra="forbid", allow_inf_nan=False, hide_input_in_errors=True
                ),
            )
        return self._adapter

    def schema(self) -> dict:
        """Return the complete discriminated schema of this catalog's durable events."""
        return self.adapter.json_schema()

    def encode(self, event: SessionEvent) -> dict:
        return encode_event(event, registry=self)

    def decode(self, record: dict) -> SessionEvent:
        return decode_event(record, registry=self)
