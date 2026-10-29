import asyncio
import json

import pytest

from harnessed import Agent as Application
from harnessed import Inject, Observation, Plugin, PolicyDecision, ToolCall, ToolResult, tool
from harnessed import Session as Agent
from harnessed.core.compaction import ModelSummarizer
from harnessed.core.hooks import PreStep
from harnessed.core.messages import Assistant, ToolStatus, User
from harnessed.core.model import ModelChunk, ModelUsage
from harnessed.core.session.codec import EventLogCodec, decode_event, encode_event
from harnessed.core.session.events import (
    CompactionApplied,
    ModelUsageRecorded,
    ObservationPolicyApplied,
    SpillCreated,
    SystemMessage,
    TurnEnd,
    TurnStart,
    UserMessage,
)
from harnessed.core.session.files import SessionFiles
from harnessed.core.session.session import SessionLog as Session
from harnessed.core.tools.registry import ToolError
from harnessed.kernel import hook
from harnessed.plugins import (
    AgentLoop,
    Approvals,
    Compaction,
    JsonlSessions,
    at_ratio,
    every_turns,
)
from harnessed.plugins.observations import LatestObservationOnly
from harnessed.plugins.spill import Spill, SpillStore
from harnessed.testing import ExtractiveSummarizer, ScriptedModel


async def test_large_results_spill_to_session_files_and_keep_structured_metadata(tmp_path):
    original = "output\n" * 4000

    class ToolsPlugin(Plugin):
        @tool
        async def large(self) -> ToolResult:
            return ToolResult(original, ToolStatus.ERROR, exit_code=3)

    tools = ToolsPlugin(name="large")

    model = ScriptedModel([[ModelChunk(tool_call=ToolCall("large"))], "done"])
    app = Application(
        model=model,
        plugins=[
            Approvals(default=PolicyDecision.ALLOW),
            tools,
            JsonlSessions(tmp_path),
            Spill(threshold_tokens=100, preview_tokens=8),
        ],
    )
    async with app.session("spill") as agent:
        result = (await agent.run("go")).tool_calls[0][1]
        assert result.exit_code == 3 and result.status == ToolStatus.ERROR
        assert result.spill is not None and result.content != original
        files = agent.ctx[SessionFiles]
        chunks = [
            await files.read(result.spill.path, offset=offset, limit=16000)
            for offset in range(0, len(original), 16000)
        ]
        assert "".join(chunks) == original
        assert "read_spill" in result.content and len(result.content) < len(original)
        assert {t.name for t in model.requests[0].tools} == {"large", "read_spill"}
        assert model.requests[1].messages[-1].text == result.content
        assert result.with_note("extra").spill == result.spill
        events = tuple(
            decode_event(json.loads(json.dumps(encode_event(e)))) for e in agent.log.events
        )
        assert Session("replay", seed=events).messages == agent.log.messages
        assert any(isinstance(e, SpillCreated) for e in events)
        ref = result.spill
    async with Application(
        model=ScriptedModel([]), plugins=[JsonlSessions(tmp_path), Spill()]
    ).session("spill") as restored:
        assert await restored.ctx[SpillStore].read(ref.path, offset=7, limit=6) == "output"


async def test_session_spill_reads_unicode_ranges_reject_escape_and_reuse_content(tmp_path):
    async with Application(model=ScriptedModel([]), plugins=[Spill()]).session() as session:
        store = session.ctx[SpillStore]
        refs = await asyncio.gather(*(store.put("ä😀abc") for _ in range(20)))
        first = refs[0]
        assert all(ref == first for ref in refs)
        assert await store.read(first.path, offset=1, limit=2) == "😀a"
        assert await store.read(first.path, offset=100) == ""
        with pytest.raises(ToolError, match="does not belong"):
            await store.read(str(tmp_path.parent / "outside.txt"))
        with pytest.raises(ValueError):
            await store.read(first.path, limit=16001)


async def test_session_spill_does_not_hide_unrelated_write_failures(tmp_path, monkeypatch):
    def denied(source, destination):
        raise PermissionError("write denied")

    async with Application(
        model=ScriptedModel([]), plugins=[JsonlSessions(tmp_path), Spill()]
    ).session() as session:
        monkeypatch.setattr("harnessed.plugins.sessions.os.replace", denied)
        with pytest.raises(PermissionError, match="write denied"):
            await session.ctx[SpillStore].put("content")
        assert not list(tmp_path.rglob("*.tmp"))


async def test_read_spill_tool_returns_bounded_ranges_without_respilling():
    model = ScriptedModel([])
    async with Application(model=model, plugins=[Spill(threshold_tokens=1)]).session() as agent:
        ref = await agent.ctx[SpillStore].put("x" * 100)
        model.responses.extend(
            [
                [ModelChunk(tool_call=ToolCall("read_spill", {"path": ref.path, "limit": 40}))],
                "done",
            ]
        )
        result = (await agent.run("read")).tool_calls[0][1]
        assert result.content == "x" * 40 and result.spill is None
        assert not any(isinstance(e, SpillCreated) for e in agent.log.events)


async def test_threshold_compaction_commits_once_and_replays_and_forks(tmp_path):

    class ToolsPlugin(Plugin):
        @tool
        async def work(self) -> str:
            return "ok"

    tools = ToolsPlugin(name="work")

    model = ScriptedModel(["initial reply", [ModelChunk(tool_call=ToolCall("work"))], "done"])
    harness = Application(
        model=model,
        plugins=[
            AgentLoop(context_window=400),
            Approvals(default=PolicyDecision.ALLOW),
            tools,
            Compaction(
                trigger=at_ratio(0.5),
                summarizer=ExtractiveSummarizer(),
                keep_turns=0,
                summary_tokens=24,
            ),
            JsonlSessions(tmp_path),
        ],
    )
    async with harness.session("compact") as agent:
        await agent.run("old context " + "x" * 1200)
        await agent.run("current task", context={"ticket": "preserve current context"})
        applied = [e for e in agent.log.events if isinstance(e, CompactionApplied)]
        assert len(applied) == 1
        request = model.requests[1]
        texts = [m.text for m in request.messages]
        assert any(text.startswith("Conversation summary:") for text in texts)
        assert "current task" in texts
        assert "Task context [ticket]:\npreserve current context" in texts
        assert "x" * 1200 not in "\n".join(texts)
        replay = Session("replay", seed=agent.log.events)
        assert replay.messages == agent.log.messages
        assert agent.log.fork("fork").messages == replay.messages
        assert any(
            isinstance(e, UserMessage) and "x" * 1200 in e.message.text for e in agent.log.events
        )
    codec = EventLogCodec()
    persisted = tuple(
        codec.decode(json.loads(line))
        for line in (tmp_path / "compact.jsonl").read_text().splitlines()
    )
    assert any(isinstance(e, CompactionApplied) for e in persisted)


async def test_compaction_preserves_base_prompt_recent_turns_and_replaces_previous_summary():
    session = Session("projection")
    await session.append(SystemMessage("permanent rules"))
    for name in ("one", "two"):
        await session.append(TurnStart(name))
        await session.append(UserMessage(User(name)))
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

    model = ScriptedModel(["first reply"])
    harness = Application(
        model=model,
        plugins=[
            AgentLoop(context_window=500),
            Approvals(default=PolicyDecision.ALLOW),
            Compaction(trigger=at_ratio(0.5), summarizer=BlockingCompactor(), keep_turns=0),
        ],
    )
    async with harness.session("cancel-compact") as agent:
        await agent.run("x" * 1000)
        await agent.followup("next")
        await started.wait()
        await agent.abort()
        assert not any(isinstance(e, CompactionApplied) for e in agent.log.events)
        assert "x" * 1000 in "\n".join(m.text for m in agent.log.messages)


async def test_compactor_error_is_reported_and_history_is_intact():

    class Broken:
        async def summarize(self, messages, *, max_tokens):
            raise ValueError("summary failed")

    model = ScriptedModel(["reply"])
    harness = Application(
        model=model,
        plugins=[
            AgentLoop(context_window=500),
            Approvals(default=PolicyDecision.ALLOW),
            Compaction(trigger=at_ratio(0.5), summarizer=Broken(), keep_turns=0),
        ],
    )
    async with harness.session("failed-summary") as agent:
        await agent.run("x" * 1000)
        with pytest.raises(ValueError, match="summary failed"):
            await agent.run("next")
        assert not any(isinstance(e, CompactionApplied) for e in agent.log.events)
        assert len(model.requests) == 1


async def test_keep_turns_retains_recent_work_and_later_compaction_merges_old_summary():
    model = ScriptedModel(["reply"] * 4)
    harness = Application(
        model=model,
        plugins=[
            AgentLoop(context_window=600),
            Approvals(default=PolicyDecision.ALLOW),
            Compaction(
                trigger=every_turns(1),
                summarizer=ExtractiveSummarizer(),
                keep_turns=1,
                summary_tokens=16,
            ),
        ],
    )
    async with harness.session("keep") as agent:
        await agent.run("FIRST " + "x" * 800)
        await agent.run("SECOND " + "y" * 800)
        await agent.run("current")
        texts = [m.text for m in model.requests[-1].messages]
        assert any("y" * 800 in text for text in texts)
        assert not any("x" * 800 in text for text in texts)
        await agent.run("next")
        assert sum(isinstance(e, CompactionApplied) for e in agent.log.events) == 2
        assert sum(m.id.startswith("compaction:") for m in agent.log.messages) == 1


async def test_compaction_does_not_commit_a_summary_that_expands_the_request():

    class Verbose:
        async def summarize(self, messages, *, max_tokens):
            return "z" * 2000

    model = ScriptedModel(["reply", "reply"])
    harness = Application(
        model=model,
        plugins=[
            AgentLoop(context_window=100),
            Approvals(default=PolicyDecision.ALLOW),
            Compaction(
                trigger=at_ratio(0.5), summarizer=Verbose(), keep_turns=0, summary_tokens=2000
            ),
        ],
    )
    async with harness.session("no-growth") as agent:
        await agent.run("original" * 30)
        await agent.run("next")
        assert not any(isinstance(e, CompactionApplied) for e in agent.log.events)


async def test_empty_or_non_advancing_compaction_is_rejected_on_replay():
    seed = (TurnStart("t"), UserMessage(User("one")), TurnEnd("t"))
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

    model = ScriptedModel(["one", "two", "three"])
    async with Application(
        model=model,
        plugins=[
            Approvals(default=PolicyDecision.ALLOW),
            Observer(),
            LatestObservationOnly(max_summaries=1),
        ],
    ).session("observations") as agent:
        for text in ("one", "two", "three"):
            await agent.run(text)
        texts = [m.text for m in model.requests[-1].messages]
        assert "brief 1" not in texts and "brief 2" in texts and ("full 3" in texts)
        assert "full 1" not in texts
        assert Session("replay", seed=agent.log.events).messages == agent.log.messages
    assert isinstance(agent.log.events[-1], ObservationPolicyApplied)
    assert agent.log.events[-1].max_summaries is None


async def test_cache_usage_is_preserved_as_provider_reported_usage():
    usage = ModelUsage(100, 10, 80, 20)
    model = ScriptedModel([[ModelChunk(text="one", usage=usage)]])
    async with Application(model=model, plugins=[Approvals(default=PolicyDecision.ALLOW)]).session(
        "cache"
    ) as agent:
        await agent.run("one")
        event = agent.log.last(ModelUsageRecorded)
        assert event.usage.cache_hit_rate == 0.8
        assert decode_event(encode_event(event)) == event


async def test_model_summarizer_uses_explicit_model_without_tools_and_closes_stream():
    llm = ScriptedModel(["summary"])
    summary = await ModelSummarizer(llm, model_name="summary-model").summarize(
        (User("history"),), max_tokens=32
    )
    assert summary.text == "summary"
    assert llm.requests[0].model == "summary-model"
    assert not llm.requests[0].tools and llm.requests[0].max_tokens == 32


async def test_model_summarizer_keeps_tool_names_and_arguments_in_summary_input():
    llm = ScriptedModel(["summary"])
    messages = (Assistant(tool_calls=(ToolCall("bash", {"command": "pytest tests/auth"}),)),)
    await ModelSummarizer(llm, model_name="summary").summarize(messages, max_tokens=32)
    transcript = llm.requests[0].messages[-1].text
    assert "bash" in transcript and "pytest tests/auth" in transcript


@pytest.mark.parametrize(
    "create",
    [
        lambda: Compaction(trigger=at_ratio(0), summarizer=ExtractiveSummarizer()),
        lambda: Compaction(trigger=at_ratio(1.1), summarizer=ExtractiveSummarizer()),
        lambda: Compaction(trigger=at_ratio(0.8), summarizer=ExtractiveSummarizer(), keep_turns=-1),
        lambda: Spill(threshold_tokens=0),
        lambda: LatestObservationOnly(max_summaries=-1),
        lambda: ModelUsage(-1),
    ],
)
def test_strategy_configuration_is_validated(create):
    with pytest.raises(ValueError):
        create()
