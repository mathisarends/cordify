import asyncio
from typing import Annotated, Protocol

import pytest

from harnessed import Agent, Bundle, Context, Inject, Plugin, ToolCall, ToolRegistry, tool
from harnessed.core.execution import ToolContext
from harnessed.core.messages import ToolResult
from harnessed.core.model import ModelChunk
from harnessed.kernel.di import InjectMarker
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
        assert bundle.tool(original) is original
        assert bundle.tool(name="alias", timeout=1)(original) is original
        assert await original(3) == 6
        schema = registry.definitions[0].parameters["properties"]["value"]
        assert schema["description"] == "An integer"


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
    class Inspect(Plugin):
        @tool(readonly=True)
        async def inspect(self, call: Inject[ToolContext]) -> str:
            assert not hasattr(call, "agent")
            assert call.session.id == "tool-context"
            await call.progress("working")
            await call.inbox.inject("The workspace changed.")
            return "ready"

    model = ScriptedModel([[ModelChunk(tool_call=ToolCall("inspect"))], "done"])
    async with Agent(model=model, plugins=[Inspect()]).session("tool-context") as session:
        await session.run("Inspect")
        assert "The workspace changed." in [message.text for message in model.requests[-1].messages]
