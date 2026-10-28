import asyncio
import json
import sys
from pathlib import Path

import pytest

from harnessed import (
    Bundle,
    ModelChunk,
    ReviewToolResult,
    SpillLargeResults,
    ToolCall,
    ToolOutputLimit,
    ToolRegistry,
    ToolResult,
)
from harnessed.adapters.spill import LocalSpillStore
from harnessed.core.events import ToolReturned
from harnessed.core.session import Session, decode_event, encode_event
from harnessed.core.tools import ToolError
from harnessed.plugins._context.providers import spill_store
from harnessed.plugins.filesystem import fs_local, fs_tools
from harnessed.plugins.persistence import session_jsonl
from harnessed.plugins.tools.shell import shell_local, shell_tools
from harnessed.presets import minimal
from harnessed.testing import ScriptedLLM


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

    model = ScriptedLLM([[ModelChunk(tool_call=ToolCall("cat"))], "done"])
    async with minimal(model, plugins=[features, session_jsonl(tmp_path)]).session(
        "bounded"
    ) as agent:
        await agent.run("inspect")
        result = next(e.result for e in agent.session.events if isinstance(e, ToolReturned))
        assert len(result.content) == 32_000
        assert result.content.startswith("HEAD") and result.content.endswith("TAIL ERROR")
        assert model.requests[1].messages[-1].text == result.content
        assert result.truncation.original_chars == len(source)
        restored = tuple(decode_event(encode_event(e)) for e in agent.session.events)
        assert Session("replay", seed=restored).messages == agent.session.messages
        replayed = next(e.result for e in restored if isinstance(e, ToolReturned))
        assert replayed == result and replayed.exit_code == 3
    # Completion journal, ordered result and model request retain only bounded copies.
    assert (tmp_path / "bounded.jsonl").stat().st_size < 4 * ToolOutputLimit().max_chars


@pytest.mark.parametrize("stage", ["execution", "review", "error"])
async def test_final_guard_also_limits_review_enrichment_and_tool_errors(stage):
    features = Bundle("enrichment")
    text = "ä😀" * 100_000

    @features.tool
    async def large() -> str:
        if stage == "error":
            raise ToolError(text)
        return text if stage == "execution" else "small"

    @features.on(ReviewToolResult)
    async def review(event, next_):
        result = await next_()
        return result.with_note(text) if stage == "review" else result

    model = ScriptedLLM([[ModelChunk(tool_call=ToolCall("large"))], "done"])
    async with minimal(model, plugins=[features], tool_output_limit=ToolOutputLimit(512)).session(
        "enriched"
    ) as agent:
        await agent.run("go")
        result = next(e.result for e in agent.session.events if isinstance(e, ToolReturned))
        assert len(result.content) <= 512 and result.truncation is not None
        assert (result.status == "error") == (stage == "error")
        assert decode_event(
            json.loads(json.dumps(encode_event(ToolReturned("call", result))))
        ) == ToolReturned("call", result)
        assert model.requests[-1].messages[-1].text == result.content


async def test_session_can_override_the_harness_output_budget():
    features = Bundle("budget")

    @features.tool
    async def large() -> str:
        return "x" * 10_000

    model = ScriptedLLM([[ModelChunk(tool_call=ToolCall("large"))], "done"])
    harness = minimal(model, plugins=[features], tool_output_limit=ToolOutputLimit(4096))
    async with harness.session("small-budget", tool_output_limit=ToolOutputLimit(512)) as agent:
        await agent.run("go")
        result = next(e.result for e in agent.session.events if isinstance(e, ToolReturned))
        assert len(result.content) == 512


async def test_spill_retains_full_output_before_the_final_limit_is_applied(tmp_path):
    features = Bundle("spill")
    original = "full output\n" * 200_000

    @features.tool
    async def large() -> str:
        return original

    store = LocalSpillStore(tmp_path)
    model = ScriptedLLM([[ModelChunk(tool_call=ToolCall("large"))], "done"])
    async with minimal(
        model,
        plugins=[features, spill_store(store), SpillLargeResults(max_tokens=100)],
        tool_output_limit=ToolOutputLimit(4096),
    ).session("spill-budget") as agent:
        await agent.run("go")
        result = next(e.result for e in agent.session.events if isinstance(e, ToolReturned))
        assert result.spill is not None and len(result.content) <= 4096
        assert (
            await asyncio.to_thread(Path(result.spill.path).read_text, encoding="utf-8") == original
        )
        assert "read_spill" in result.content


async def test_read_file_supports_bounded_unicode_character_ranges_and_continuation(tmp_path):
    original = "ä😀abc" * 3000
    (tmp_path / "large.txt").write_text(original, encoding="utf-8")
    async with minimal(ScriptedLLM([]), plugins=[fs_local(tmp_path), fs_tools()]).session(
        "ranges"
    ) as agent:
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
        for arguments in ({"offset": -1}, {"limit": 0}, {"limit": 16_001}):
            invalid = await registry.execute(
                ToolCall("read_file", {"path": "large.txt", **arguments})
            )
            assert invalid.is_error


async def test_large_shell_stdout_stderr_and_exit_code_remain_bounded(tmp_path):
    model = ScriptedLLM(
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
    async with minimal(
        model,
        plugins=[shell_local(), shell_tools(tmp_path)],
        tool_output_limit=ToolOutputLimit(1024),
    ).session("shell-budget") as agent:
        await agent.run("go")
        result = next(e.result for e in agent.session.events if isinstance(e, ToolReturned))
        assert len(result.content) == 1024 and "TAIL_ERROR" in result.content
        assert result.status == "error" and result.exit_code == 3
        assert result.truncation.original_chars > 2 * 1024 * 1024
