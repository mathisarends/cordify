import asyncio
import json

import pytest

from harnessed import Agent as Application
from harnessed import (
    Inject,
    Observation,
    Plugin,
    PolicyDecision,
    Session,
    SystemPrompt,
    ToolCall,
    ToolResult,
    tool,
)
from harnessed.core.compaction import ModelSummarizer
from harnessed.core.hooks import PreStep
from harnessed.core.messages import AssistantMessage, ToolStatus, UserMessage
from harnessed.core.model import ModelChunk, ModelUsage
from harnessed.core.session.codec import decode_event, encode_event
from harnessed.core.session.events import (
    CompactionAppliedEvent,
    ModelUsageRecordedEvent,
    ObservationPolicyAppliedEvent,
    SpillCreatedEvent,
    SystemMessageEvent,
    TurnEndedEvent,
    TurnStartedEvent,
    UserMessageEvent,
)
from harnessed.core.session.files import SessionFiles
from harnessed.core.session.session import SessionLog
from harnessed.core.tools.registry import ToolError
from harnessed.kernel import hook
from harnessed.plugins import AgentLoop, Approvals, Compaction, at_ratio, every_turns
from harnessed.plugins.observations import LatestObservationOnly
from harnessed.plugins.spill import Spill, SpillStore
from harnessed.testing import ExtractiveSummarizer, ScriptedModel
from tests._audit import Audit, AuditProjection


async def test_large_results_spill_to_session_files_and_keep_structured_metadata(tmp_path):
    original = "output\n" * 4000

    class ToolsPlugin(Plugin):
        @tool
        async def large(self) -> ToolResult:
            return ToolResult(original, ToolStatus.ERROR, exit_code=3)

    tools = ToolsPlugin(name="large")

    model = ScriptedModel([[ModelChunk(tool_call=ToolCall("large"))], "done"])
    spill = Spill(threshold_tokens=100, preview_tokens=8)
    app = Application(
        system_prompt=SystemPrompt(sections=(spill.usage,)),
        model=model,
        plugins=[Approvals(default=PolicyDecision.ALLOW), tools, spill],
    )
    async with app.session() as agent:
        agent_audit = Audit(agent)
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
            decode_event(json.loads(json.dumps(encode_event(e)))) for e in agent_audit.events
        )
        assert AuditProjection(events).messages == agent.log.messages
        assert any(isinstance(e, SpillCreatedEvent) for e in events)
        ref = result.spill
        state = agent.snapshot()
    spill_2 = Spill()
    async with Application(
        system_prompt=SystemPrompt(sections=(spill_2.usage,)),
        model=ScriptedModel([]),
        plugins=[spill_2],
    ).session(state=state) as restored:
        assert await restored.ctx[SpillStore].read(ref.path, offset=7, limit=6) == "output"


async def test_session_spill_reads_unicode_ranges_reject_escape_and_reuse_content(tmp_path):
    spill = Spill()
    async with Application(
        system_prompt=SystemPrompt(sections=(spill.usage,)),
        model=ScriptedModel([]),
        plugins=[spill],
    ).session() as session:
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


async def test_read_spill_tool_returns_bounded_ranges_without_respilling():
    model = ScriptedModel([])
    spill_5 = Spill(threshold_tokens=1)
    async with Application(
        system_prompt=SystemPrompt(sections=(spill_5.usage,)), model=model, plugins=[spill_5]
    ).session() as agent:
        agent_audit = Audit(agent)
        ref = await agent.ctx[SpillStore].put("x" * 100)
        model.responses.extend(
            [
                [ModelChunk(tool_call=ToolCall("read_spill", {"path": ref.path, "limit": 40}))],
                "done",
            ]
        )
        result = (await agent.run("read")).tool_calls[0][1]
        assert result.content == "x" * 40 and result.spill is None
        assert not any(isinstance(e, SpillCreatedEvent) for e in agent_audit.events)


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
        ],
    )
    async with harness.session() as agent:
        agent_audit = Audit(agent)
        await agent.run("old context " + "x" * 1200)
        await agent.run("current task", context={"ticket": "preserve current context"})
        applied = [e for e in agent_audit.events if isinstance(e, CompactionAppliedEvent)]
        assert len(applied) == 1
        request = model.requests[1]
        texts = [m.text for m in request.messages]
        assert any(text.startswith("Conversation summary:") for text in texts)
        assert "current task" in texts
        assert "Task context [ticket]:\npreserve current context" in texts
        assert "x" * 1200 not in "\n".join(texts)
        replay = AuditProjection(agent_audit.events)
        assert replay.messages == agent.log.messages
        assert any(
            isinstance(e, UserMessageEvent) and "x" * 1200 in e.message.text
            for e in agent_audit.events
        )


async def test_compaction_preserves_base_prompt_recent_turns_and_replaces_previous_summary():
    session = SessionLog("projection")
    await session.append(SystemMessageEvent("permanent rules"))
    for name in ("one", "two"):
        await session.append(TurnStartedEvent(name))
        await session.append(UserMessageEvent(UserMessage(name)))
        await session.append(TurnEndedEvent(name))
    await session.append(CompactionAppliedEvent(4, "first summary"))
    assert [m.text for m in session.messages] == [
        "permanent rules",
        "Conversation summary:\nfirst summary",
        "two",
    ]
    await session.append(CompactionAppliedEvent(7, "combined summary"))
    assert [m.text for m in session.messages] == [
        "permanent rules",
        "Conversation summary:\ncombined summary",
    ]
    with pytest.raises(ValueError, match="advance"):
        await session.append(CompactionAppliedEvent(7, "duplicate"))
    with pytest.raises(ValueError, match="completed context boundary"):
        await session.append(CompactionAppliedEvent(2, "invalid"))


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
    async with harness.session() as agent:
        agent_audit = Audit(agent)
        await agent.run("x" * 1000)
        await agent.submit("next")
        await started.wait()
        await agent.abort()
        assert not any(isinstance(e, CompactionAppliedEvent) for e in agent_audit.events)
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
    async with harness.session() as agent:
        agent_audit = Audit(agent)
        await agent.run("x" * 1000)
        with pytest.raises(ValueError, match="summary failed"):
            await agent.run("next")
        assert not any(isinstance(e, CompactionAppliedEvent) for e in agent_audit.events)
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
    async with harness.session() as agent:
        agent_audit = Audit(agent)
        await agent.run("FIRST " + "x" * 800)
        await agent.run("SECOND " + "y" * 800)
        await agent.run("current")
        texts = [m.text for m in model.requests[-1].messages]
        assert any("y" * 800 in text for text in texts)
        assert not any("x" * 800 in text for text in texts)
        await agent.run("next")
        assert sum(isinstance(e, CompactionAppliedEvent) for e in agent_audit.events) == 2
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
    async with harness.session() as agent:
        agent_audit = Audit(agent)
        await agent.run("original" * 30)
        await agent.run("next")
        assert not any(isinstance(e, CompactionAppliedEvent) for e in agent_audit.events)


async def test_latest_observation_policy_keeps_one_unchanged_snapshot_and_unwinds():

    class Observer(Plugin, needs=(Session,)):
        def __init__(self):
            self.index = 0

        @hook(PreStep)
        async def observe(self, event, next_, agent: Inject[Session]):
            self.index += 1
            agent.observe(Observation("browser", f"full {self.index}"))
            return await next_()

    model = ScriptedModel(["one", "two", "three"])
    async with Application(
        model=model,
        plugins=[
            Approvals(default=PolicyDecision.ALLOW),
            Observer(),
            LatestObservationOnly(),
        ],
    ).session() as agent:
        agent_audit = Audit(agent)
        for text in ("one", "two", "three"):
            await agent.run(text)
        texts = [m.text for m in model.requests[-1].messages]
        assert "full 1" not in texts and "full 2" not in texts and "full 3" in texts
        assert AuditProjection(agent_audit.events).messages == agent.log.messages
    assert isinstance(agent.log.events[-1], ObservationPolicyAppliedEvent)
    assert agent.log.events[-1].max_observations is None


async def test_cache_usage_is_preserved_as_provider_reported_usage():
    usage = ModelUsage(100, 10, 80, 20)
    model = ScriptedModel([[ModelChunk(text="one", usage=usage)]])
    async with Application(
        model=model, plugins=[Approvals(default=PolicyDecision.ALLOW)]
    ).session() as agent:
        await agent.run("one")
        event = agent.log.last(ModelUsageRecordedEvent)
        assert event.usage.cache_hit_rate == 0.8
        assert decode_event(encode_event(event)) == event


async def test_model_summarizer_uses_explicit_model_without_tools_and_closes_stream():
    llm = ScriptedModel(["summary"])
    summary = await ModelSummarizer(llm).summarize((UserMessage("history"),), max_tokens=32)
    assert summary.text == "summary"
    assert not llm.requests[0].tools and llm.requests[0].settings.max_tokens == 32


async def test_model_summarizer_keeps_tool_names_and_arguments_in_summary_input():
    llm = ScriptedModel(["summary"])
    messages = (AssistantMessage(tool_calls=(ToolCall("bash", {"command": "pytest tests/auth"}),)),)
    await ModelSummarizer(llm).summarize(messages, max_tokens=32)
    transcript = llm.requests[0].messages[-1].text
    assert "bash" in transcript and "pytest tests/auth" in transcript


@pytest.mark.parametrize(
    "create",
    [
        lambda: Compaction(trigger=at_ratio(0), summarizer=ExtractiveSummarizer()),
        lambda: Compaction(trigger=at_ratio(1.1), summarizer=ExtractiveSummarizer()),
        lambda: Compaction(trigger=at_ratio(0.8), summarizer=ExtractiveSummarizer(), keep_turns=-1),
        lambda: Spill(threshold_tokens=0),
        lambda: ObservationPolicyAppliedEvent(0),
        lambda: ModelUsage(-1),
    ],
)
def test_strategy_configuration_is_validated(create):
    with pytest.raises(ValueError):
        create()
