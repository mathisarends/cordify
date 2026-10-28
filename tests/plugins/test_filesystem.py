"""Bounded I/O, edit atomicity and the public filesystem plugin."""

import json
from pathlib import Path

import pytest

from harnessed import Agent, ToolCall
from harnessed.core.model import ModelChunk
from harnessed.core.tools.registry import ToolError, ToolRegistry
from harnessed.plugins.filesystem import Filesystem, LocalFS
from harnessed.testing import ScriptedModel


async def test_ranged_reads_never_read_the_entire_file(tmp_path, monkeypatch):
    content = "ä😀abc" * 40_000
    path = tmp_path / "large.txt"
    path.write_text(content, encoding="utf-8")
    original_open = Path.open
    reads = []

    class BoundedReader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *exc):
            return self.stream.__exit__(*exc)

        def read(self, size=-1):
            assert 0 <= size <= 65_536
            reads.append(size)
            return self.stream.read(size)

    def bounded_open(self, *args, **kwargs):
        return BoundedReader(original_open(self, *args, **kwargs))

    monkeypatch.setattr(Path, "open", bounded_open)
    result = await LocalFS(tmp_path).read(Path("large.txt"), offset=100_001, limit=11)
    assert result == content[100_001:100_012]
    assert reads == [65_536, 34_465, 11]


async def test_edits_are_unambiguous_and_failed_atomic_replace_preserves_original(
    tmp_path, monkeypatch
):
    fs = LocalFS(tmp_path)
    path = Path("example.txt")
    await fs.write(path, "one one")
    with pytest.raises(ToolError, match="found 2"):
        await fs.replace(path, "one", "two")
    assert await fs.read(path) == "one one"

    def fail_replace(*args):
        raise OSError("Disk rejected replace")

    monkeypatch.setattr("harnessed.plugins.filesystem.os.replace", fail_replace)
    with pytest.raises(OSError, match="Disk rejected"):
        await fs.write(path, "lost")
    assert await fs.read(path) == "one one"
    assert list(tmp_path.iterdir()) == [tmp_path / path]


async def test_filesystem_plugin_schema_bounds_search_notices_and_safe_approvals(tmp_path):
    (tmp_path / "large.txt").write_text("match\n" + "x" * 200_001, encoding="utf-8")
    (tmp_path / "other.txt").write_text("match", encoding="utf-8")
    app = Agent(model=ScriptedModel([]), plugins=[Filesystem(tmp_path)])
    async with app.session() as session:
        registry = session.ctx[ToolRegistry]
        schemas = {definition.name: definition.parameters for definition in registry.definitions}
        assert schemas["read_file"]["properties"]["offset"]["minimum"] == 0
        assert schemas["read_file"]["properties"]["limit"]["maximum"] == 16_000
        page = await registry.execute(
            ToolCall("read_file", {"path": "large.txt", "offset": 1, "limit": 3})
        )
        assert page.content.startswith("atc") and "offset=4, limit=3" in page.content
        matches = await registry.execute(ToolCall("glob", {"pattern": "*.txt", "limit": 1}))
        assert json.loads(matches.content)["truncated"] is True
        search = await registry.execute(ToolCall("grep", {"query": "match"}))
        assert "only the first 200000 characters" in search.content
        assert registry.is_readonly("read_file") and registry.is_readonly("grep")
        assert not registry.is_readonly("edit")

    model = ScriptedModel(
        [
            [ModelChunk(tool_call=ToolCall("write_file", {"path": "denied.txt", "text": "no"}))],
            "blocked",
        ]
    )
    result = await Agent(model=model, plugins=[Filesystem(tmp_path)]).run("Write")
    assert result.tool_calls[0][1].status == "unavailable"
    assert not (tmp_path / "denied.txt").exists()
