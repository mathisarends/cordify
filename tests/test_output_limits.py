import asyncio
import json
import sys
from pathlib import Path

import pytest

from harnessed import Agent as Application
from harnessed import ToolCall, ToolResult
from harnessed.core.hooks import ReviewToolResult
from harnessed.core.model import ModelChunk
from harnessed.core.plugin import Bundle
from harnessed.core.session.codec import decode_event, encode_event
from harnessed.core.session.events import ToolReturned
from harnessed.core.session.session import SessionLog as Session
from harnessed.core.tools.output_limit import ToolOutputLimit
from harnessed.core.tools.registry import ToolError, ToolRegistry
from harnessed.plugins import Approvals, JsonlSessions, Service
from harnessed.plugins.filesystem import FS, FilesystemTools, LocalFS
from harnessed.plugins.shell import LocalShell, ShellBackend, ShellTools
from harnessed.plugins.spill import LocalSpillStore, SpillLargeResults, SpillStore
from harnessed.testing import ScriptedModel


def test_output_limit_includes_notice_keeps_head_and_tail_and_is_idempotent():
    limit = ToolOutputLimit(max_chars=512)
    unchanged = ToolResult("x" * 512, exit_code=0)
    assert limit.apply(unchanged) is unchanged
    source = ToolResult("HEAD\n" + "x" * 2000 + "\nTAIL ERROR", "error", exit_code=3)
    result = limit.apply(source)
    assert len(result.content) == 512
    assert result.content.startswith("HEAD\n") and result.content.endswith("\nTAIL ERROR")
    assert "Output truncated" in result.content and "offset/limit" in result.content
    assert result.status == "error" and result.exit_code == 3 and result.is_error
    assert result.truncation.original_chars == len(source.content)
    assert result.truncation.retained_chars < 512
    assert result.truncation.max_chars == 512
    assert result.with_note("reviewed").truncation == result.truncation
    assert limit.apply(result) is result


@pytest.mark.parametrize("max_chars", [0, 255, True, 512.5])
def test_invalid_output_limits_are_rejected(max_chars):
    with pytest.raises(ValueError, match="max_chars"):
        ToolOutputLimit(max_chars)


async def test_two_megabyte_result_is_bounded_in_model_log_and_replay_by_default(tmp_path):
    features = Bundle("large")
    source = "HEAD\n" + "x" * (2 * 1024 * 1024) + "\nTAIL ERROR"

    @features.tool
    async def cat() -> ToolResult:
        return ToolResult(source, "error", exit_code=3)

    model = ScriptedModel([[ModelChunk(tool_call=ToolCall("cat"))], "done"])
    async with Application(
        model=model, plugins=[Approvals(trust="all"), features, JsonlSessions(tmp_path)]
    ).session("bounded") as agent:
        await agent.run("inspect")
        result = next(e.result for e in agent.log.events if isinstance(e, ToolReturned))
        assert len(result.content) == 32000
        assert result.content.startswith("HEAD") and result.content.endswith("TAIL ERROR")
        assert model.requests[1].messages[-1].text == result.content
        assert result.truncation.original_chars == len(source)
        restored = tuple(decode_event(encode_event(e)) for e in agent.log.events)
        assert Session("replay", seed=restored).messages == agent.log.messages
        replayed = next(e.result for e in restored if isinstance(e, ToolReturned))
        assert replayed == result and replayed.exit_code == 3
    assert (tmp_path / "bounded.jsonl").stat().st_size < 4 * ToolOutputLimit().max_chars


@pytest.mark.parametrize("stage", ["execution", "review", "error"])
async def test_final_guard_also_limits_review_enrichment_and_tool_errors(stage):
    features = Bundle("enrichment")
    text = "ä😀" * 100000

    @features.tool
    async def large() -> str:
        if stage == "error":
            raise ToolError(text)
        return text if stage == "execution" else "small"

    @features.on(ReviewToolResult)
    async def review(event, next_):
        result = await next_()
        return result.with_note(text) if stage == "review" else result

    model = ScriptedModel([[ModelChunk(tool_call=ToolCall("large"))], "done"])
    async with Application(
        model=model, plugins=[Approvals(trust="all"), ToolOutputLimit(512), features]
    ).session("enriched") as agent:
        await agent.run("go")
        result = next(e.result for e in agent.log.events if isinstance(e, ToolReturned))
        assert len(result.content) <= 512 and result.truncation is not None
        assert (result.status == "error") == (stage == "error")
        assert decode_event(
            json.loads(json.dumps(encode_event(ToolReturned("call", result))))
        ) == ToolReturned("call", result)
        assert model.requests[-1].messages[-1].text == result.content


async def test_output_budget_is_configured_with_the_plugin_slot():
    features = Bundle("budget")

    @features.tool
    async def large() -> str:
        return "x" * 10000

    model = ScriptedModel([[ModelChunk(tool_call=ToolCall("large"))], "done"])
    harness = Application(
        model=model, plugins=[Approvals(trust="all"), ToolOutputLimit(512), features]
    )
    async with harness.session("small-budget") as agent:
        await agent.run("go")
        result = next(e.result for e in agent.log.events if isinstance(e, ToolReturned))
        assert len(result.content) == 512


async def test_spill_retains_full_output_before_the_final_limit_is_applied(tmp_path):
    features = Bundle("spill")
    original = "full output\n" * 200000

    @features.tool
    async def large() -> str:
        return original

    store = LocalSpillStore(tmp_path)
    model = ScriptedModel([[ModelChunk(tool_call=ToolCall("large"))], "done"])
    async with Application(
        model=model,
        plugins=[
            Approvals(trust="all"),
            ToolOutputLimit(4096),
            features,
            Service(SpillStore, store),
            SpillLargeResults(max_tokens=100),
        ],
    ).session("spill-budget") as agent:
        await agent.run("go")
        result = next(e.result for e in agent.log.events if isinstance(e, ToolReturned))
        assert result.spill is not None and len(result.content) <= 4096
        assert (
            await asyncio.to_thread(Path(result.spill.path).read_text, encoding="utf-8") == original
        )
        assert "read_spill" in result.content


async def test_read_file_supports_bounded_unicode_character_ranges_and_continuation(tmp_path):
    original = "ä😀abc" * 3000
    (tmp_path / "large.txt").write_text(original, encoding="utf-8")
    async with Application(
        model=ScriptedModel([]),
        plugins=[Approvals(trust="all"), Service(FS, LocalFS(tmp_path)), FilesystemTools()],
    ).session("ranges") as agent:
        registry = agent.ctx[ToolRegistry]
        result = await registry.execute(ToolCall("read_file", {"path": "large.txt"}))
        assert result.content.startswith(original[:8000])
        assert "offset=8000, limit=8000" in result.content
        page = await registry.execute(
            ToolCall("read_file", {"path": "large.txt", "offset": 1, "limit": 2})
        )
        assert page.content.startswith("😀a") and "offset=3, limit=2" in page.content
        tail = await registry.execute(
            ToolCall("read_file", {"path": "large.txt", "offset": len(original) - 5, "limit": 5})
        )
        assert tail.content == original[-5:]
        for arguments in ({"offset": -1}, {"limit": 0}, {"limit": 16001}):
            invalid = await registry.execute(
                ToolCall("read_file", {"path": "large.txt", **arguments})
            )
            assert invalid.is_error


async def test_large_shell_stdout_stderr_and_exit_code_remain_bounded(tmp_path):
    model = ScriptedModel(
        [
            [
                ModelChunk(
                    tool_call=ToolCall(
                        "run_command",
                        {
                            "argv": [
                                sys.executable,
                                "-c",
                                "import sys; print('x' * (2 * 1024 * 1024)); "
                                "print('TAIL_ERROR', file=sys.stderr); sys.exit(3)",
                            ]
                        },
                    )
                )
            ],
            "done",
        ]
    )
    async with Application(
        model=model,
        plugins=[
            Approvals(trust="all"),
            ToolOutputLimit(1024),
            Service(ShellBackend, LocalShell()),
            ShellTools(tmp_path),
        ],
    ).session("shell-budget") as agent:
        await agent.run("go")
        result = next(e.result for e in agent.log.events if isinstance(e, ToolReturned))
        assert len(result.content) == 1024 and "TAIL_ERROR" in result.content
        assert result.status == "error" and result.exit_code == 3
        assert 500000 < result.truncation.original_chars < 1048576
        assert "stdout truncated" in result.content
