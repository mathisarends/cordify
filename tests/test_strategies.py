import asyncio
import json
from pathlib import Path

import pytest

from harnessed import (
    AgentHandle as Agent,
)
from harnessed import (
    Bundle,
    CacheBreakpoints,
    Harness,
    Inject,
    LatestObservationOnly,
    LLMCompactor,
    LocalSpillStore,
    Message,
    ModelChunk,
    ModelRequest,
    ModelUsage,
    Observation,
    Plugin,
    PreStep,
    SpillLargeResults,
    ThresholdCompaction,
    ToolCall,
    ToolResult,
    hook,
)
from harnessed.core.session.codec import decode_event, encode_event
from harnessed.core.session.events import (
    CacheMetrics,
    CompactionApplied,
    ModelUsageRecorded,
    ObservationPolicyApplied,
    SpillCreated,
    SystemMessage,
    ToolReturned,
    TurnEnd,
    TurnStart,
    UserMessage,
)
from harnessed.core.session.session import Session
from harnessed.plugins._context.providers import compactor, spill_store
from harnessed.plugins.agents.loop import agent_loop
from harnessed.plugins.persistence import session_jsonl
from harnessed.plugins.services import llm
from harnessed.testing import ScriptedLLM


async def test_large_results_spill_to_persistent_file_and_keep_structured_metadata(tmp_path):
    tools = Bundle("large")
    original = "output\n" * 4000

    @tools.tool
    async def large() -> ToolResult:
        return ToolResult(original, "error", exit_code=3)

    model = ScriptedLLM([[ModelChunk(tool_call=ToolCall("large"))], "done"])
    store = LocalSpillStore(tmp_path)
    harness = Harness(
        [
            agent_loop(),
            llm(model),
            tools,
            spill_store(store),
            SpillLargeResults(100, preview_tokens=8),
        ]
    )
    async with harness.session("spill") as agent:
        await agent.run("go")
        result = next(e.result for e in agent.session.events if isinstance(e, ToolReturned))
        assert result.exit_code == 3 and result.status == "error"
        assert result.spill is not None and result.content != original
        assert (
            await asyncio.to_thread(Path(result.spill.path).read_text, encoding="utf-8") == original
        )
        assert "read_spill" in result.content and len(result.content) < len(original)
        assert {t.name for t in model.requests[0].tools} == {"large", "read_spill"}
        assert model.requests[1].messages[-1].text == result.content
        assert result.with_note("extra").spill == result.spill
        events = tuple(
            decode_event(json.loads(json.dumps(encode_event(e)))) for e in agent.session.events
        )
        assert Session("replay", seed=events).messages == agent.session.messages
        assert any(isinstance(e, SpillCreated) for e in events)
        ref = result.spill
    assert await store.read(ref.path, offset=7, limit=6) == "output"


async def test_spill_reads_unicode_character_ranges_reject_escape_and_reuse_content(tmp_path):
    store = LocalSpillStore(tmp_path)
    refs = await asyncio.gather(*(store.put("ä😀abc") for _ in range(20)))
    first = refs[0]
    assert all(ref == first for ref in refs)
    assert await store.read(first.path, offset=1, limit=2) == "😀a"
    assert await store.read(first.path, offset=100) == ""
    with pytest.raises(ValueError, match="outside"):
        await store.read(str(tmp_path.parent / "outside.txt"))
    with pytest.raises(ValueError, match="limit"):
        await store.read(first.path, limit=16001)


async def test_spill_reuses_verified_concurrent_winner_on_windows_replace_race(
    tmp_path, monkeypatch
):
    def racing_replace(source, destination):
        destination.write_bytes(source.read_bytes())
        raise PermissionError("target opened by concurrent writer")

    monkeypatch.setattr("harnessed.adapters.spill.os.replace", racing_replace)
    ref = await LocalSpillStore(tmp_path).put("concurrent content")
    assert await LocalSpillStore(tmp_path).read(ref.path) == "concurrent content"
    assert not list(tmp_path.glob("*.tmp"))


async def test_spill_does_not_hide_unrelated_write_failures(tmp_path, monkeypatch):
    def denied(source, destination):
        raise PermissionError("write denied")

    monkeypatch.setattr("harnessed.adapters.spill.os.replace", denied)
    with pytest.raises(PermissionError, match="write denied"):
        await LocalSpillStore(tmp_path).put("content")
    assert not list(tmp_path.glob("*.tmp"))


async def test_read_spill_tool_returns_bounded_ranges_without_respilling(tmp_path):
    store = LocalSpillStore(tmp_path)
    ref = await store.put("x" * 100)
    model = ScriptedLLM(
        [[ModelChunk(tool_call=ToolCall("read_spill", {"path": ref.path, "limit": 40}))], "done"]
    )
    async with Harness(
        [agent_loop(), llm(model), spill_store(store), SpillLargeResults(1)]
    ).session("read-spill") as agent:
        await agent.run("read")
        result = next(e.result for e in agent.session.events if isinstance(e, ToolReturned))
        assert result.content == "x" * 40 and result.spill is None
        assert not any(isinstance(e, SpillCreated) for e in agent.session.events)


async def test_threshold_compaction_commits_once_and_replays_and_forks(tmp_path):
    tools = Bundle("work")

    @tools.tool
    async def work() -> str:
        return "ok"

    model = ScriptedLLM(["initial reply", [ModelChunk(tool_call=ToolCall("work"))], "done"])
    harness = Harness(
        [
            agent_loop(),
            llm(model),
            tools,
            ThresholdCompaction(at=0.5, keep_turns=0, summary_tokens=24),
            CacheBreakpoints(),
            session_jsonl(tmp_path),
        ],
        context_window=400,
    )
    async with harness.session("compact") as agent:
        await agent.run("old context " + "x" * 1200)
        await agent.run("current task", context={"ticket": "preserve current context"})
        applied = [e for e in agent.session.events if isinstance(e, CompactionApplied)]
        assert len(applied) == 1
        request = model.requests[1]
        texts = [m.text for m in request.messages]
        assert any(text.startswith("Conversation summary:") for text in texts)
        assert "current task" in texts
        assert "Task context [ticket]:\npreserve current context" in texts
        assert "x" * 1200 not in "\n".join(texts)
        assert request.cache_breakpoints and max(request.cache_breakpoints) <= len(request.messages)
        replay = Session("replay", seed=agent.session.events)
        assert replay.messages == agent.session.messages
        assert agent.session.fork("fork").messages == replay.messages
        assert any(
            isinstance(e, UserMessage) and "x" * 1200 in e.message.text
            for e in agent.session.events
        )
    persisted = tuple(
        decode_event(json.loads(line))
        for line in (tmp_path / "compact.jsonl").read_text().splitlines()
    )
    assert any(isinstance(e, CompactionApplied) for e in persisted)


async def test_compaction_preserves_base_prompt_recent_turns_and_replaces_previous_summary():
    session = Session("projection")
    await session.append(SystemMessage("permanent rules"))
    for name in ("one", "two"):
        await session.append(TurnStart(name))
        await session.append(UserMessage(Message("user", name)))
        await session.append(TurnEnd(name))
    await session.append(CompactionApplied(4, "first summary"))
    assert [m.text for m in session.messages] == [
        "permanent rules",
        "Conversation summary:\nfirst summary",
        "two",
    ]
    await session.append(CompactionApplied(7, "combined summary"))
    assert [m.text for m in session.messages] == [
        "permanent rules",
        "Conversation summary:\ncombined summary",
    ]
    with pytest.raises(ValueError, match="advance"):
        await session.append(CompactionApplied(7, "duplicate"))
    with pytest.raises(ValueError, match="closed turn"):
        await session.append(CompactionApplied(2, "invalid"))


async def test_failed_or_cancelled_compactor_does_not_commit_partial_compaction():
    started = asyncio.Event()

    class BlockingCompactor:
        async def summarize(self, messages, *, max_tokens):
            started.set()
            await asyncio.Event().wait()

    model = ScriptedLLM(["first reply"])
    harness = Harness(
        [
            agent_loop(),
            llm(model),
            compactor(BlockingCompactor()),
            ThresholdCompaction(at=0.5, keep_turns=0),
        ],
        context_window=100,
    )
    async with harness.session("cancel-compact") as agent:
        await agent.run("x" * 1000)
        await agent.followup("next")
        await started.wait()
        await agent.cancel()
        assert not any(isinstance(e, CompactionApplied) for e in agent.session.events)
        assert "x" * 1000 in "\n".join(m.text for m in agent.session.messages)


async def test_compactor_error_is_reported_and_history_is_intact():
    class Broken:
        async def summarize(self, messages, *, max_tokens):
            raise ValueError("summary failed")

    model = ScriptedLLM(["reply"])
    harness = Harness(
        [agent_loop(), llm(model), compactor(Broken()), ThresholdCompaction(0.5, keep_turns=0)],
        context_window=100,
    )
    async with harness.session("failed-summary") as agent:
        await agent.run("x" * 1000)
        with pytest.raises(ValueError, match="summary failed"):
            await agent.run("next")
        assert not any(isinstance(e, CompactionApplied) for e in agent.session.events)
        assert len(model.requests) == 1


async def test_keep_turns_retains_recent_work_and_later_compaction_merges_old_summary():
    model = ScriptedLLM(["reply"] * 4)
    harness = Harness(
        [
            agent_loop(),
            llm(model),
            ThresholdCompaction(at=0.5, keep_turns=1, summary_tokens=16),
        ],
        context_window=300,
    )
    async with harness.session("keep") as agent:
        await agent.run("FIRST " + "x" * 800)
        await agent.run("SECOND " + "y" * 800)
        await agent.run("current")
        texts = [m.text for m in model.requests[-1].messages]
        assert any("y" * 800 in text for text in texts)
        assert not any("x" * 800 in text for text in texts)
        await agent.run("next")
        assert sum(isinstance(e, CompactionApplied) for e in agent.session.events) == 2
        assert sum(m.id.startswith("compaction:") for m in agent.session.messages) == 1


async def test_compaction_does_not_commit_a_summary_that_expands_the_request():
    class Verbose:
        async def summarize(self, messages, *, max_tokens):
            return "z" * 2000

    model = ScriptedLLM(["reply", "reply"])
    harness = Harness(
        [
            agent_loop(),
            llm(model),
            compactor(Verbose()),
            ThresholdCompaction(at=0.5, keep_turns=0, summary_tokens=2000),
        ],
        context_window=100,
    )
    async with harness.session("no-growth") as agent:
        await agent.run("original" * 30)
        await agent.run("next")
        assert not any(isinstance(e, CompactionApplied) for e in agent.session.events)


async def test_empty_or_non_advancing_compaction_is_rejected_on_replay():
    seed = (TurnStart("t"), UserMessage(Message("user", "one")), TurnEnd("t"))
    with pytest.raises(ValueError, match="non-empty"):
        Session("invalid", seed=(*seed, CompactionApplied(3, "")))
    with pytest.raises(ValueError, match="existing event prefix"):
        Session("invalid", seed=(*seed, CompactionApplied(99, "summary")))


async def test_latest_observation_policy_limits_old_summaries_per_source_and_unwinds():
    class Observer(Plugin):
        def __init__(self):
            self.index = 0

        @hook(PreStep)
        async def observe(self, event, next_, agent: Inject[Agent]):
            self.index += 1
            agent.observe(Observation("browser", f"full {self.index}", f"brief {self.index}"))
            return await next_()

    model = ScriptedLLM(["one", "two", "three"])
    async with Harness(
        [agent_loop(), llm(model), Observer(), LatestObservationOnly(max_summaries=1)]
    ).session("observations") as agent:
        for text in ("one", "two", "three"):
            await agent.run(text)
        texts = [m.text for m in model.requests[-1].messages]
        assert "brief 1" not in texts and "brief 2" in texts and "full 3" in texts
        assert "full 1" not in texts
        assert Session("replay", seed=agent.session.events).messages == agent.session.messages
    assert isinstance(agent.session.events[-1], ObservationPolicyApplied)
    assert agent.session.events[-1].max_summaries is None


async def test_cache_markers_exclude_current_observation_and_metrics_require_provider_usage():
    class Observer(Plugin):
        @hook(PreStep)
        async def observe(self, event, next_, agent: Inject[Agent]):
            agent.observe(Observation("browser", "FULL", "brief"))
            return await next_()

    usage = ModelUsage(100, 10, 80, 20)
    model = ScriptedLLM(
        [[ModelChunk(text="one", usage=usage)], [ModelChunk(text="two", usage=ModelUsage(5))]]
    )
    async with Harness([agent_loop(), llm(model), Observer(), CacheBreakpoints()]).session(
        "cache"
    ) as agent:
        await agent.run("one")
        request = model.requests[0]
        assert request.messages[request.cache_breakpoints[0]].text == "FULL"
        metrics = [e for e in agent.session.events if isinstance(e, CacheMetrics)]
        assert len(metrics) == 1 and metrics[0].hit_rate == 0.8
        assert decode_event(encode_event(metrics[0])) == metrics[0]
        await agent.run("two")
        assert sum(isinstance(e, ModelUsageRecorded) for e in agent.session.events) == 2
        assert sum(isinstance(e, CacheMetrics) for e in agent.session.events) == 1


async def test_llm_compactor_uses_explicit_model_without_tools_and_closes_stream():
    llm = ScriptedLLM(["summary"])
    assert (
        await LLMCompactor(llm, model="summary-model").summarize(
            (Message("user", "history"),), max_tokens=32
        )
        == "summary"
    )
    assert llm.requests[0].model == "summary-model"
    assert not llm.requests[0].tools and llm.requests[0].max_tokens == 32


async def test_llm_compactor_keeps_tool_names_and_arguments_in_summary_input():
    llm = ScriptedLLM(["summary"])
    messages = (
        Message("assistant", tool_calls=(ToolCall("bash", {"command": "pytest tests/auth"}),)),
    )
    await LLMCompactor(llm, model="summary").summarize(messages, max_tokens=32)
    transcript = llm.requests[0].messages[-1].text
    assert "bash" in transcript and "pytest tests/auth" in transcript


@pytest.mark.parametrize(
    "create",
    [
        lambda: ThresholdCompaction(at=0),
        lambda: ThresholdCompaction(at=1.1),
        lambda: ThresholdCompaction(keep_turns=-1),
        lambda: SpillLargeResults(0),
        lambda: LatestObservationOnly(max_summaries=-1),
        lambda: ModelRequest("test", (), cache_breakpoints=(1,)),
        lambda: ModelUsage(-1),
    ],
)
def test_strategy_configuration_is_validated(create):
    with pytest.raises(ValueError):
        create()
