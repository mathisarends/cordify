import json
from pathlib import Path

import pytest

from harnessed import Context, ToolCall, plugin
from harnessed.core.tools.registry import ToolRegistry
from harnessed.plugins import AgentLoop
from harnessed.plugins.filesystem import FS, fs_tools
from harnessed.plugins.shell import ProcResult, shell_tools
from harnessed.plugins.shell import ShellBackend as Shell


class MemoryFS:
    def __init__(self, label):
        self.label = label

    async def read(self, path: Path, *, offset: int = 0, limit: int | None = None) -> str:
        return self.label[offset : None if limit is None else offset + limit]

    async def write(self, path: Path, text: str) -> None:
        pass


class MemoryShell:
    def __init__(self, label):
        self.label = label

    async def run(self, argv: list[str], *, cwd: Path, timeout: float) -> ProcResult:
        return ProcResult(0, self.label, "")


FEATURES = [
    (fs_tools, FS, MemoryFS, {"read_file", "write_file"}),
    (shell_tools, Shell, MemoryShell, {"run_command"}),
]


async def invoke(registry, key):
    if key is FS:
        return (await registry.execute(ToolCall("read_file", {"path": "example.txt"}))).content
    result = await registry.execute(ToolCall("run_command", {"argv": ["example"]}))
    assert not result.is_error
    return json.loads(result.content)["stdout"]


@pytest.mark.parametrize("feature, key, backend, names", FEATURES)
async def test_builtin_features_declare_lifetime_needs_and_wait_for_the_backend(
    feature,
    key,
    backend,
    names,
):
    spec = feature()
    assert spec.needs == (key, ToolRegistry)

    @plugin
    async def provider(ctx, label):
        ctx.provide(key, backend(label))
        yield

    async with Context() as ctx:
        await ctx.plugin(AgentLoop())
        registry = ctx[ToolRegistry]
        handle = await ctx.plugin(spec)
        assert handle.status == "waiting" and registry.definitions == ()
        first = await ctx.plugin(provider("first"))
        await ctx.settle()
        original = handle.ctx
        assert handle.status == "active"
        assert {definition.name for definition in registry.definitions} == names
        assert await invoke(registry, key) == "first"

        override = ctx.provide(key, backend("override"))
        await ctx.settle()
        assert original._closed and handle.ctx is not original
        assert await invoke(registry, key) == "override"
        await override()
        await ctx.settle()
        assert await invoke(registry, key) == "first"

        await first.aclose()
        assert handle.status == "waiting" and registry.definitions == ()
        await ctx.plugin(provider("second"))
        await ctx.settle()
        assert handle.status == "active"
        assert await invoke(registry, key) == "second"
        assert len(registry.definitions) == len(names)


@pytest.mark.parametrize("feature, key, backend, names", FEATURES)
async def test_registry_replacement_rebuilds_builtin_registrations_without_duplicates(
    feature,
    key,
    backend,
    names,
):
    async with Context() as ctx:
        ctx.provide(key, backend("ready"))
        handle = await ctx.plugin(feature())
        assert handle.status == "waiting"
        first, second = ToolRegistry(ctx), ToolRegistry(ctx)
        ctx.provide(ToolRegistry, first)
        await ctx.settle()
        original = handle.ctx
        assert {definition.name for definition in first.definitions} == names
        remove_override = ctx.provide(ToolRegistry, second)
        await ctx.settle()
        assert handle.ctx is not original and original._closed
        assert first.definitions == ()
        assert {definition.name for definition in second.definitions} == names
        await remove_override()
        await ctx.settle()
        assert second.definitions == ()
        assert {definition.name for definition in first.definitions} == names
        assert await invoke(first, key) == "ready"
