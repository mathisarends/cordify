from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Literal

import pytest
from pydantic import Field, TypeAdapter, ValidationError
from pydantic.errors import PydanticInvalidForJsonSchema

from harnessed import Agent, Plugin, ToolContext, events, tool
from harnessed.core.errors import SessionFormatError
from harnessed.core.runtime.result import RunResult
from harnessed.testing import ScriptedModel, call
from tests._audit import Audit


class ProjectEventKind(StrEnum):
    INDEXED = "project.indexed"


@dataclass(frozen=True, slots=True)
class ProjectIndexed(events.DurableEvent):
    files: int
    type: Literal[ProjectEventKind.INDEXED] = field(default=ProjectEventKind.INDEXED, kw_only=True)


@dataclass(frozen=True, slots=True)
class IndexProgress(events.TransientEvent):
    text: str
    type: Literal["project.progress"] = field(default="project.progress", kw_only=True)


class Indexer(Plugin):
    events = (ProjectIndexed, IndexProgress)

    @tool(readonly=True)
    async def index(self, *, ctx: ToolContext) -> str:
        await ctx.emit(IndexProgress("Indexing"))
        await ctx.emit(ProjectIndexed(42))
        return "indexed"


async def test_custom_audit_events_are_separate_from_conversation_state():
    model = ScriptedModel([call(Indexer.index), "done"])
    async with Agent(model=model, plugins=[Indexer()]).session() as session:
        audit = Audit(session)
        live = [event async for event in session.stream("Index")]
        assert next(event for event in live if isinstance(event, IndexProgress)).text == "Indexing"
        assert next(event for event in live if isinstance(event, ProjectIndexed)).files == 42
        assert not any(isinstance(event, IndexProgress) for event in audit.events)
        indexed = next(event for event in audit.events if isinstance(event, ProjectIndexed))
        registry = session.log.registry
        assert registry.decode(registry.encode(indexed)) == indexed
        state = live[-1].result.state
    fresh_model = ScriptedModel([])
    async with Agent(model=fresh_model).session(state=state) as restored:
        assert not restored.events and fresh_model.requests == []
        assert restored.messages[-1].text == "done"


async def test_runtime_declarations_remain_readable_after_plugin_disposal():
    async with Agent(model=ScriptedModel([])).session() as session:
        audit = Audit(session)
        handle = await session.use(Indexer())
        await session.emit(ProjectIndexed(3))
        await handle.aclose()
        await session.emit(ProjectIndexed(4))
        records = [session.log.registry.encode(event) for event in audit.events]
        assert [session.log.registry.decode(record).files for record in records] == [3, 4]


async def test_event_catalogs_are_isolated_between_configurations():
    async with Agent(model=ScriptedModel([]), plugins=[Indexer()]).session() as session:
        record = session.log.registry.encode(ProjectIndexed(1))
    async with Agent(model=ScriptedModel([])).session() as bare:
        with pytest.raises(SessionFormatError, match="Unknown session event"):
            bare.log.registry.decode(record)
        with pytest.raises(TypeError, match="registered"):
            await bare.emit(ProjectIndexed(2))


def test_custom_event_schema_is_strict_and_registry_rejects_collisions_atomically():
    registry = events.EventRegistry([ProjectIndexed])
    value = ProjectIndexed(5)
    assert registry.decode(registry.encode(value)) == value
    with pytest.raises(ValidationError):
        registry.encode(ProjectIndexed("five"))
    with pytest.raises(SessionFormatError):
        registry.decode({"version": 3, "type": "project.indexed", "data": {"files": True}})
    with pytest.raises(SessionFormatError):
        registry.decode({"version": 3, "type": "project.indexed", "data": {"files": 1, "extra": 2}})

    @dataclass(frozen=True)
    class Collision(events.DurableEvent):
        type: Literal[ProjectEventKind.INDEXED] = "project.indexed"

    with pytest.raises(ValueError, match="Duplicate"):
        registry.register([IndexProgress, Collision])
    with pytest.raises(TypeError, match="registered"):
        registry.validate(IndexProgress("not committed"))


def test_full_builtin_and_application_unions_support_schemas_and_terminal_results():
    annotation = Annotated[events.BuiltinEvent | ProjectIndexed, Field(discriminator="type")]
    adapter = TypeAdapter(annotation)
    assert "project.indexed" in adapter.json_schema()["discriminator"]["mapping"]
    assert adapter.validate_json(adapter.dump_json(ProjectIndexed(3))) == ProjectIndexed(3)
    registry = events.EventRegistry([ProjectIndexed])
    value = events.TurnFinishedEvent(RunResult("done", (ProjectIndexed(3),), _output=None))
    restored = adapter.validate_json(adapter.dump_json(value), context={"events": registry})
    assert restored.result.events == (ProjectIndexed(3),)

    stream = TypeAdapter(events.StreamEvent[str])
    assert stream.json_schema()["discriminator"]["propertyName"] == "type"
    result = events.TurnFinishedEvent(RunResult("done", (), _output="typed output"))
    assert stream.validate_json(stream.dump_json(result)).result.output == "typed output"


def test_mutable_untagged_and_builtin_subclass_events_are_not_extension_points():
    @dataclass
    class Mutable(events.DurableEvent):
        type: Literal["project.mutable"] = "project.mutable"

    @dataclass(frozen=True)
    class Untagged(events.DurableEvent):
        type: str = "project.untagged"

    @dataclass(frozen=True)
    class Unsafe(events.ToolCallRequestedEvent, events.DurableEvent):
        type: Literal["project.unsafe"] = field(default="project.unsafe", kw_only=True)

    for kind in (Mutable, Untagged, Unsafe):
        with pytest.raises(TypeError):
            events.EventRegistry([kind])


def test_non_json_event_schemas_fail_before_registration_is_committed():
    @dataclass(frozen=True)
    class CallbackEvent(events.DurableEvent):
        callback: Callable[[], None]
        type: Literal["project.callback"] = field(default="project.callback", kw_only=True)

    registry = events.EventRegistry()
    with pytest.raises(PydanticInvalidForJsonSchema):
        registry.register([IndexProgress, CallbackEvent])
    with pytest.raises(TypeError, match="registered"):
        registry.validate(IndexProgress("not registered"))
