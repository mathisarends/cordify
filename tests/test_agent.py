import asyncio

import pytest

from harnessed import (
    Agent,
    Bundle,
    Decision,
    ExecuteTool,
    Harness,
    Inject,
    ModelChunk,
    Plugin,
    ReviewToolResult,
    ToolCall,
    ToolCallContext,
    ToolPolicy,
    ToolRegistry,
    hook,
    plugin,
    tool,
)
from harnessed.core.events import (
    AssistantChunk,
    AssistantMessage,
    ModelRequested,
    PreStep,
    StepDecision,
    ToolProgress,
    ToolReturned,
    TurnEnd,
    TurnStart,
)
from harnessed.plugins.agents.loop import agent_loop
from harnessed.plugins.permissions.approval import approval_gate, callback_approver
from harnessed.plugins.services import llm
from harnessed.testing import ScriptedLLM


async def test_streaming_tool_loop_and_exact_recorded_requests():
    features = Bundle("math")

    @features.tool
    async def add(a: int, b: int, call: Inject[ToolCallContext]) -> int:
        await call.progress("adding")
        return a + b

    model = ScriptedLLM(
        [
            [ModelChunk(tool_call=ToolCall("add", {"a": 1, "b": 2}))],
            [ModelChunk(text="The result "), ModelChunk(text="is 3.")],
        ]
    )
    harness = Harness([agent_loop(), llm(model), features], model="test-model")
    async with harness.session("math") as agent:
        events = [event async for event in agent.stream("Add 1 and 2")]
        assert (
            "".join(e.text for e in events if isinstance(e, AssistantChunk)) == "The result is 3."
        )
        assert [e.text for e in events if isinstance(e, ToolProgress)] == ["adding"]
        assert any(isinstance(e, ToolReturned) and e.result.content == "3" for e in events)
        recorded = [e.request for e in agent.session.events if isinstance(e, ModelRequested)]
        assert recorded == model.requests
        assert model.requests[0].model == "test-model"
        assert [m.role for m in model.requests[1].messages] == ["user", "assistant", "tool"]
        assert agent.status == "idle"
    assert agent.status == "disposed"


class PausingLLM:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.requests = []

    async def stream(self, request):
        self.requests.append(request)
        if len(self.requests) == 1:
            self.started.set()
            await self.release.wait()
        yield ModelChunk(text=f"response {len(self.requests)}")


async def test_inject_stays_idle_steering_enters_next_step_followup_gets_own_turn():
    model = PausingLLM()
    async with Harness([agent_loop(), llm(model)]).session("inbox") as agent:
        await agent.inject("quiet")
        await asyncio.sleep(0)
        assert agent.status == "idle" and not model.requests
        await agent.followup("first")
        await model.started.wait()
        await agent.steer("steering")
        await agent.inject("context")
        await agent.followup("second")
        model.release.set()
        await agent.when_idle()
        users = [[m.content for m in r.messages if m.role == "user"] for r in model.requests]
        assert users == [
            ["first", "quiet"],
            ["first", "quiet", "steering", "context"],
            ["first", "quiet", "steering", "context", "second"],
        ]
        assert sum(isinstance(e, TurnStart) for e in agent.session.events) == 2
        assert not agent.inbox.pending


async def test_denied_policy_never_enters_execution_or_review_regardless_of_order():
    stages = []
    features = Bundle("policies")

    @features.on(ReviewToolResult)
    async def review(event, next_):
        stages.append("review")
        return await next_()

    @features.on(ExecuteTool)
    async def execute(event, next_):
        stages.append("execute")
        return await next_()

    @features.tool
    async def remove() -> str:
        stages.append("tool")
        return "removed"

    model = ScriptedLLM([[ModelChunk(tool_call=ToolCall("remove"))], "Denied."])
    harness = Harness(
        [
            agent_loop(),
            llm(model),
            features,
            approval_gate(risky={"remove"}, ask=lambda call: False),
        ]
    )
    async with harness.session("deny") as agent:
        result = await agent.run("remove")
        outcomes = [e.result for e in result.events if isinstance(e, ToolReturned)]
        assert outcomes[0].status == "denied"
        assert stages == []


async def test_approval_always_is_session_local_and_review_can_inject_nudges():
    asked, stages = [], []
    features = Bundle("reviews")

    @features.tool
    async def work() -> str:
        stages.append("tool")
        return "done"

    @features.on(ToolPolicy)
    async def policy(event, next_):
        stages.append("policy")
        return await next_()

    @features.on(ReviewToolResult)
    async def review(event, next_, agent: Inject[Agent]):
        stages.append("review")
        await agent.inject("consider this")
        return (await next_()).with_note("Reviewed")

    def approve(call):
        asked.append(call.name)
        return Decision.ALWAYS

    model = ScriptedLLM(
        [
            [ModelChunk(tool_call=ToolCall("work")), ModelChunk(tool_call=ToolCall("work"))],
            "Final.",
        ]
    )
    harness = Harness(
        [
            agent_loop(),
            llm(model),
            features,
            callback_approver(approve),
            approval_gate(risky={"work"}),
        ]
    )
    async with harness.session("approve") as agent:
        await agent.run("work twice")
        assert asked == ["work"]
        assert stages == ["policy", "tool", "review", "policy", "tool", "review"]
        assert any(m.content == "consider this" for m in model.requests[-1].messages)
        results = [e.result for e in agent.session.events if isinstance(e, ToolReturned)]
        assert all(result.content == "done\n\nReviewed" for result in results)


async def test_class_plugin_state_is_fresh_for_every_session_and_methods_support_di():
    class Counter(Plugin):
        def __init__(self):
            self.count = 0

        @tool
        async def count_calls(self, agent: Inject[Agent]) -> int:
            self.count += 1
            assert agent.status == "running"
            return self.count

        @hook(ReviewToolResult)
        async def review(self, event, next_, agent: Inject[Agent]):
            assert agent.id in ("one", "two")
            return await next_()

    template = Counter()
    model = ScriptedLLM(
        [
            [ModelChunk(tool_call=ToolCall("count_calls"))],
            "one",
            [ModelChunk(tool_call=ToolCall("count_calls"))],
            "two",
        ]
    )
    harness = Harness([agent_loop(), llm(model), template])
    for session_id in ("one", "two"):
        async with harness.session(session_id) as agent:
            result = await agent.run("count")
            assert (
                next(e.result.content for e in result.events if isinstance(e, ToolReturned)) == "1"
            )
    assert template.count == 0


async def test_function_plugin_can_inject_current_agent_and_implicit_dependencies():
    observed = []

    @plugin
    async def observer(ctx, agent: Inject[Agent], registry: Inject[ToolRegistry]):
        observed.append((agent.id, registry))
        yield
        assert agent.status == "disposed"

    harness = Harness([observer(), agent_loop(), llm(ScriptedLLM(["done"]))])
    async with harness.session("injected-agent") as agent:
        assert observed == [(agent.id, agent.ctx[ToolRegistry])]
        await agent.run("go")


async def test_explicit_and_live_registration():
    async def echo(text: str) -> str:
        return text

    async def review(event, next_):
        return (await next_()).with_note("ok")

    model = ScriptedLLM([[ModelChunk(tool_call=ToolCall("echo", {"text": "hello"}))], "done"])
    harness = Harness(
        [
            agent_loop(),
            llm(model),
            Plugin(name="explicit", tools=[echo], hooks=[(ReviewToolResult, review)]),
        ]
    )
    harness.add_tool(echo, name="alias")
    async with harness.session("explicit") as agent:
        agent.add_tool(echo, name="live")
        seen = []
        agent.add_hook(ToolReturned, lambda event: seen.append(event.result.content))
        await agent.run("echo")
        assert seen == ["hello\n\nok"]
        assert {d.name for d in model.requests[0].tools} == {"echo", "alias", "live"}


async def test_pre_step_rejection_keeps_input_until_another_wake():
    model = ScriptedLLM(["accepted", "second"])
    harness = Harness([agent_loop(), llm(model)])
    async with harness.session("reject") as agent:
        reject = True

        @agent.on(PreStep)
        async def gate(event, next_):
            return StepDecision.reject() if reject else await next_()

        await agent.followup("first")
        await agent.when_idle()
        assert not model.requests and len(agent.inbox.next_turn) == 1
        reject = False
        await agent.followup("second")
        await agent.when_idle()
        assert len(model.requests) == 2 and not agent.inbox.pending


async def test_cancel_before_driver_starts_and_reuse_agent():
    model = ScriptedLLM(["after cancel"])
    async with Harness([agent_loop(), llm(model)]).session("cancel-before-start") as agent:
        await agent.followup("discard")
        await agent.cancel()
        assert agent.status == "idle" and not agent.inbox.pending
        assert (await agent.run("new")).text == "after cancel"


async def test_cancel_stream_preserves_visible_prefix_and_closes_turn():
    started = asyncio.Event()

    class PartialLLM:
        async def stream(self, request):
            yield ModelChunk(text="visible prefix")
            started.set()
            await asyncio.Event().wait()

    async with Harness([agent_loop(), llm(PartialLLM())]).session("partial") as agent:
        await agent.followup("go")
        await started.wait()
        await agent.cancel()
        partial = next(e for e in agent.session.events if isinstance(e, AssistantMessage))
        assert partial.interrupted and partial.message.content == "visible prefix"
        assert isinstance(agent.session.events[-1], TurnEnd)
        assert agent.session.events[-1].reason == "interrupted"


async def test_cancel_tool_answers_outstanding_call_and_sets_signal():
    started = asyncio.Event()
    calls = []
    bundle = Bundle("blocking")

    @bundle.tool
    async def wait(call: Inject[ToolCallContext]) -> str:
        calls.append(call)
        started.set()
        await asyncio.Event().wait()
        return "never"

    model = ScriptedLLM([[ModelChunk(tool_call=ToolCall("wait"))]])
    async with Harness([agent_loop(), llm(model), bundle]).session("cancel-tool") as agent:
        await agent.followup("go")
        await started.wait()
        await agent.cancel()
        assert calls[0].signal.is_set()
        results = [e.result for e in agent.session.events if isinstance(e, ToolReturned)]
        assert len(results) == 1 and results[0].is_error


async def test_same_harness_rejects_two_live_writers_and_max_steps_bounds_the_loop():
    bundle = Bundle("loop")

    @bundle.tool
    async def again() -> str:
        return "again"

    model = ScriptedLLM([[ModelChunk(tool_call=ToolCall("again"))]] * 2)
    harness = Harness([agent_loop(), llm(model), bundle])
    async with harness.session("same", max_steps=2) as agent:
        with pytest.raises(RuntimeError, match="live owner"):
            async with harness.session("same"):
                pytest.fail("A second writer entered")
        await agent.run("loop")
        assert agent.session.events[-1].reason == "max_steps"


async def test_observer_failure_closes_model_iterator_before_run_returns():
    closed = asyncio.Event()

    class ResourceLLM:
        async def stream(self, request):
            try:
                yield ModelChunk(text="partial")
                await asyncio.Event().wait()
            finally:
                closed.set()

    async with Harness([agent_loop(), llm(ResourceLLM())]).session("resource") as agent:

        @agent.on(AssistantChunk)
        def broken(event):
            raise ValueError("observer failure")

        with pytest.raises(ValueError, match="observer failure"):
            await agent.run("go")
        assert closed.is_set()
        assert agent.session.messages[-1].content == "partial"


async def test_policy_ask_routes_to_approver_seam():
    features = Bundle("ask")
    asked = []

    @features.on(ToolPolicy)
    async def policy(event, next_):
        return Decision.ASK

    @features.tool
    async def approved() -> str:
        return "allowed"

    def approve(call):
        asked.append(call.name)
        return Decision.ALLOW

    model = ScriptedLLM([[ModelChunk(tool_call=ToolCall("approved"))], "done"])
    harness = Harness([agent_loop(), llm(model), features, callback_approver(approve)])
    async with harness.session("ask") as agent:
        await agent.run("ask")
        assert asked == ["approved"]


async def test_canceling_when_idle_waiter_does_not_cancel_the_agent():
    model = PausingLLM()
    async with Harness([agent_loop(), llm(model)]).session("waiter") as agent:
        await agent.followup("go")
        await model.started.wait()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(agent.when_idle(), timeout=0.01)
        assert agent.status == "running"
        model.release.set()
        await agent.when_idle()
        assert agent.session.messages[-1].content == "response 1"
