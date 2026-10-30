import asyncio
from typing import Annotated, Protocol

import pytest
from pydantic import Field

from harnessed import Agent, Context, Inject, Plugin, ToolCall
from harnessed.core.messages import ToolResult
from harnessed.core.model import ModelChunk
from harnessed.core.plugin import Bundle
from harnessed.core.prompt.context import DynamicContext
from harnessed.core.tools.context import ToolContext
from harnessed.core.tools.decorator import tool
from harnessed.core.tools.registry import ToolRegistry
from harnessed.kernel.di import InjectMarker
from harnessed.kernel.plugin import PluginStatus
from harnessed.testing import ScriptedModel


class Backend(Protocol):
    async def read(self) -> str: ...


class MemoryBackend:
    def __init__(self, value: str):
        self.value = value

    async def read(self):
        return self.value


async def test_injected_arguments_are_hidden_and_services_resolve_per_call():
    async with Context() as ctx:
        registry = ToolRegistry(ctx)

        @registry.tool
        async def read(path: str, backend: Inject[Backend]) -> str:
            return f"{path}: {await backend.read()}"

        assert registry.definitions == ()
        first = ctx.provide(Backend, MemoryBackend("first"))
        assert list(registry.definitions[0].parameters["properties"]) == ["path"]
        call = ToolCall("read", {"path": "test"})
        assert (await registry.execute(call)).content == "test: first"
        replacement = ctx.provide(Backend, MemoryBackend("second"))
        assert (await registry.execute(call)).content == "test: second"
        await replacement()
        assert (await registry.execute(call)).content == "test: first"
        await first()
        assert registry.definitions == ()
        assert (await registry.execute(call)).is_error


async def test_caller_cannot_supply_injected_parameters():
    async with Context() as ctx:
        registry = ToolRegistry(ctx)
        ctx.provide(int, 42)

        @registry.tool
        async def show(number: Inject[int]) -> int:
            return number

        assert registry.definitions[0].parameters["properties"] == {}
        result = await registry.execute(ToolCall("show", {"number": 99}))
        assert result.is_error
        assert "Extra inputs are not permitted" in result.content
        assert (await registry.execute(ToolCall("show"))).content == "42"


async def test_explicit_annotated_marker_is_supported():
    async with Context() as ctx:
        registry = ToolRegistry(ctx)
        ctx.provide(int, 7)

        @registry.tool()
        async def show(number: Annotated[int, InjectMarker()]) -> int:
            return number

        assert (await registry.execute(ToolCall("show"))).content == "7"


async def test_decorators_preserve_original_functions_and_annotated_descriptions():
    async with Context() as ctx:
        registry = ToolRegistry(ctx)

        async def original(value: Annotated[int, "An integer"]) -> int:
            return value * 2

        assert registry.tool(original) is original
        assert registry.tool(name="alias")(original) is original
        assert tool(original) is original
        assert tool()(original) is original
        bundle = Bundle("test")
        assert not hasattr(bundle, "tool")
        assert not hasattr(bundle, "add_tool")
        assert not hasattr(ctx, "tool")
        assert await original(3) == 6
        schema = registry.definitions[0].parameters["properties"]["value"]
        assert schema["description"] == "An integer"


@pytest.mark.parametrize(
    "description, expected",
    [
        ("Find one record by its exact key.", "Find one record by its exact key."),
        (None, "Developer documentation: resolve the configured backend and query its cache."),
        ("", ""),
    ],
)
async def test_explicit_tool_descriptions_reach_the_model_without_changing_developer_docs(
    description, expected
):
    class Lookups(Plugin):
        @tool(readonly=True, description=description)
        async def lookup(self, key: Annotated[str, Field(description="Exact record key")]) -> str:
            """Developer documentation: resolve the configured backend and query its cache."""
            return key

    original_doc = Lookups.lookup.__doc__
    model = ScriptedModel([[ModelChunk(tool_call=ToolCall("lookup", {"key": "record"}))], "done"])
    async with Agent(model=model, plugins=[Lookups()]).session() as session:
        result = await session.run("Find the record")
        definition = next(tool for tool in model.requests[0].tools if tool.name == "lookup")
        assert definition.description == expected
        assert definition.parameters["properties"]["key"]["description"] == "Exact record key"
        assert result.tool_calls[0][1].content == "record"
    assert Lookups.lookup.__doc__ == original_doc


async def test_registry_description_precedence_and_missing_docstring_fallback():
    def documented() -> str:
        """Developer documentation."""
        return "value"

    def undocumented() -> str:
        return "value"

    async with Context() as ctx:
        registry = ToolRegistry(ctx)
        registry.register(documented, name="direct", description="Direct prompt.")
        registry.tool(name="decorated", description="Decorator prompt.")(documented)
        registry.register(documented, name="fallback")
        registry.register(documented, name="hidden", description="")
        registry.register(undocumented)
        assert {tool.name: tool.description for tool in registry.definitions} == {
            "direct": "Direct prompt.",
            "decorated": "Decorator prompt.",
            "fallback": "Developer documentation.",
            "hidden": "",
            "undocumented": "",
        }


@pytest.mark.parametrize("description", [True, 42, [], {}])
def test_tool_descriptions_reject_non_string_values(description):
    def callback() -> str:
        return "value"

    with pytest.raises(TypeError, match="description must be a string or None"):
        tool(description=description)(callback)
    with pytest.raises(TypeError, match="description must be a string or None"):
        ToolRegistry(Context()).register(callback, description=description)


@pytest.mark.parametrize("arguments", [{}, {"value": True}, {"value": "1"}, {"other": 1}])
async def test_invalid_model_arguments_are_errors_without_running_tool(arguments):
    async with Context() as ctx:
        registry = ToolRegistry(ctx)

        @registry.tool
        async def typed(value: int) -> int:
            pytest.fail("Invalid arguments reached the tool")

        assert (await registry.execute(ToolCall("typed", arguments))).is_error


async def test_nested_types_defaults_sync_execution_and_errors():
    async with Context() as ctx:
        registry = ToolRegistry(ctx)

        @registry.tool
        def count(items: list[dict[str, int]], limit: int | None = None) -> int:
            return sum(sum(item.values()) for item in items[:limit])

        result = await registry.execute(ToolCall("count", {"items": [{"a": 2}, {"b": 3}]}))
        assert result.content == "5"
        assert (await registry.execute(ToolCall("count", {"items": [{"a": "bad"}]}))).is_error
        assert (await registry.execute(ToolCall("missing"))).is_error


async def test_async_tool_timeout_cancels_its_body():
    async with Context() as ctx:
        registry = ToolRegistry(ctx)
        canceled = asyncio.Event()

        @registry.tool(timeout=0.01)
        async def slow() -> str:
            try:
                await asyncio.Event().wait()
            finally:
                canceled.set()

        result = await registry.execute(ToolCall("slow"))
        assert result.is_error and "TimeoutError" in result.content
        assert canceled.is_set()


def test_model_data_is_deeply_detached_and_immutable():
    arguments = {"nested": {"values": [1]}}
    call = ToolCall("test", arguments)
    arguments["nested"]["values"].append(2)
    assert call.arguments["nested"]["values"] == (1,)
    with pytest.raises(TypeError):
        call.arguments["nested"]["new"] = True
    with pytest.raises(TypeError):
        ToolCall("bad", {"value": float("nan")})


async def test_result_converters_follow_their_owner_lifetime():
    class ForeignResult:
        pass

    async with Context() as ctx:
        registry = ToolRegistry(ctx)

        @registry.tool
        def custom() -> str:
            return ForeignResult()

        owner = ctx.extend()
        registry.result_converter(ForeignResult, lambda value: ToolResult("converted"), owner=owner)
        assert (await registry.execute(ToolCall("custom"))).content == "converted"
        await owner.aclose()
        assert (await registry.execute(ToolCall("custom"))).is_error


async def test_tool_context_exposes_log_inbox_and_progress_without_the_driver():
    class Inspect(Plugin, needs=(DynamicContext,)):
        @tool(readonly=True)
        async def inspect(self, call: Inject[ToolContext], context: Inject[DynamicContext]) -> str:
            assert not hasattr(call, "agent")
            assert isinstance(call.log.id, str) and call.log.id
            await call.progress("working")
            assert not call.cancelled
            await context.set("workspace", "The workspace changed.")
            return "ready"

    model = ScriptedModel([[ModelChunk(tool_call=ToolCall("inspect"))], "done"])
    async with Agent(model=model, plugins=[Inspect()]).session() as session:
        await session.run("Inspect")
        assert any("The workspace changed." in m.text for m in model.requests[-1].messages)


async def test_functional_context_tools_follow_explicit_needs_and_temporal_restarts():
    class Tools(Plugin):
        needs = (Backend,)

        @tool(readonly=True)
        async def read(self, *, backend: Inject[Backend]) -> str:
            return await backend.read()

    async with Context() as ctx:
        registry = ToolRegistry(ctx)
        ctx.provide(ToolRegistry, registry)
        handle = await ctx.plugin(Tools())
        assert handle.status == PluginStatus.WAITING
        first = ctx.provide(Backend, MemoryBackend("first"))
        await ctx.settle()
        assert handle.status == PluginStatus.ACTIVE
        assert (await registry.execute(ToolCall("read"))).content == "first"
        await first()
        await ctx.settle()
        assert handle.status == PluginStatus.WAITING and not registry.definitions
        ctx.provide(Backend, MemoryBackend("second"))
        await ctx.settle()
        assert (await registry.execute(ToolCall("read"))).content == "second"
        await handle.aclose()
        assert not registry.definitions


async def test_keyword_only_tool_context_is_implicit_and_never_exposed_in_the_schema():
    seen = []

    class Tools(Plugin):
        @tool(readonly=True)
        async def inspect(self, value: int, *, call: ToolContext) -> int:
            seen.append(call)
            return value

    model = ScriptedModel([[ModelChunk(tool_call=ToolCall("inspect", {"value": 42}))], "done"])
    async with Agent(model=model, plugins=[Tools()]).session() as session:
        await session.run("inspect")
        assert list(model.requests[0].tools[0].parameters["properties"]) == ["value"]
        assert len(seen) == 1 and seen[0].log.id == session.id
        assert not seen[0].cancelled


def test_unmarked_positional_tool_context_requires_the_explicit_marker():
    async def bad(call: ToolContext) -> str:
        return "bad"

    with pytest.raises(TypeError, match="keyword-only"):
        ToolRegistry(Context()).register(bad)
