import asyncio

import pytest

from harnessed import Agent as Application
from harnessed import ApprovalAnswer, Inject, Plugin, Session, ToolCall, ToolContext, plugin, tool
from harnessed.core.approvals import ApprovalGate, Approver, CallbackApprover
from harnessed.core.hooks import ExecuteTool, PreStep, ReviewToolResult, StepDecision, ToolPolicy
from harnessed.core.messages import PolicyDecision, Role, ToolStatus
from harnessed.core.model import ModelChunk, ModelInfo, StopReason
from harnessed.core.session.events import (
    AssistantMessageEvent,
    ModelRequestedEvent,
    ToolCallFinishedEvent,
    ToolProgressEvent,
    TurnEndedEvent,
    TurnOutcome,
    TurnStartedEvent,
)
from harnessed.core.stream import TextDeltaEvent
from harnessed.core.tools.registry import ToolRegistry
from harnessed.kernel import hook
from harnessed.plugins import AgentLoop, Approvals, Service
from harnessed.testing import CallbackModel, ScriptedModel


async def test_streaming_tool_loop_and_exact_recorded_requests():

    class FeaturesPlugin(Plugin):
        @tool
        async def add(self, a: int, b: int, call: Inject[ToolContext]) -> int:
            await call.progress("adding")
            return a + b

    features = FeaturesPlugin(name="math")

    model = ScriptedModel(
        [
            [ModelChunk(tool_call=ToolCall("add", {"a": 1, "b": 2}))],
            [ModelChunk(text="The result "), ModelChunk(text="is 3.")],
        ],
        info=ModelInfo("test-model", context_window=128_000),
    )
    harness = Application(model=model, plugins=[Approvals(default=PolicyDecision.ALLOW), features])
    async with harness.session("math") as agent:
        events = [event async for event in agent.stream("Add 1 and 2")]
        assert (
            "".join(e.text for e in events if isinstance(e, TextDeltaEvent)) == "The result is 3."
        )
        assert [e.text for e in events if isinstance(e, ToolProgressEvent)] == ["adding"]
        assert any(isinstance(e, ToolCallFinishedEvent) and e.result.content == "3" for e in events)
        recorded = [e.request for e in agent.log.events if isinstance(e, ModelRequestedEvent)]
        assert recorded == model.requests
        assert agent.last(ModelRequestedEvent).model == model.info
        assert [m.role for m in model.requests[1].messages] == ["user", "assistant", "tool"]
        assert agent.status == "idle"
    assert agent.status == "closed"


class PausingModel(CallbackModel):
    def __init__(self):
        super().__init__(self._chunks)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def _chunks(self, request):
        if len(self.requests) == 1:
            self.started.set()
            await self.release.wait()
        yield ModelChunk(text=f"response {len(self.requests)}")


async def test_steering_enters_next_step_while_another_run_gets_its_own_turn():
    model = PausingModel()
    async with Application(model=model, plugins=[Approvals(default=PolicyDecision.ALLOW)]).session(
        "inbox"
    ) as agent:
        await agent.submit("first")
        await model.started.wait()
        await agent.steer("steering")
        await agent.submit("second")
        model.release.set()
        await agent.control.join()
        users = [[m.content for m in r.messages if m.role == Role.USER] for r in model.requests]
        assert users == [["first"], ["first", "steering"], ["first", "steering", "second"]]
        assert sum(isinstance(e, TurnStartedEvent) for e in agent.log.events) == 2
        assert not agent.inbox.pending


async def test_denied_policy_never_enters_execution_or_review_regardless_of_order():
    stages = []

    class FeaturesPlugin(Plugin):
        @hook(ReviewToolResult)
        async def review(self, event, next_):
            stages.append("review")
            return await next_()

        @hook(ExecuteTool)
        async def execute(self, event, next_):
            stages.append("execute")
            return await next_()

        @tool
        async def remove(self) -> str:
            stages.append("tool")
            return "removed"

    features = FeaturesPlugin(name="policies")

    model = ScriptedModel([[ModelChunk(tool_call=ToolCall("remove"))], "Denied."])
    harness = Application(
        model=model,
        plugins=[
            Approvals(default=PolicyDecision.ALLOW),
            features,
            ApprovalGate(risky={"remove"}, ask=lambda call: False),
        ],
    )
    async with harness.session("deny") as agent:
        result = await agent.run("remove")
        outcomes = [e.result for e in result.events if isinstance(e, ToolCallFinishedEvent)]
        assert outcomes[0].status == ToolStatus.DENIED
        assert stages == []


async def test_approval_always_is_session_local_and_review_can_steer_nudges():
    asked, stages = ([], [])

    class FeaturesPlugin(Plugin, needs=(Session,)):
        @tool
        async def work(self) -> str:
            stages.append("tool")
            return "done"

        @hook(ToolPolicy)
        async def policy(self, event, next_):
            stages.append("policy")
            return await next_()

        @hook(ReviewToolResult)
        async def review(self, event, next_, agent: Inject[Session]):
            stages.append("review")
            await agent.steer("consider this")
            return (await next_()).with_note("Reviewed")

    features = FeaturesPlugin(name="reviews")

    def approve(request):
        asked.append(request.call.name)
        return ApprovalAnswer.ALWAYS

    model = ScriptedModel(
        [[ModelChunk(tool_call=ToolCall("work")), ModelChunk(tool_call=ToolCall("work"))], "Final."]
    )
    harness = Application(
        model=model,
        plugins=[
            Approvals(default=PolicyDecision.ALLOW),
            features,
            Service(Approver, CallbackApprover(approve)),
            ApprovalGate(risky={"work"}),
        ],
    )
    async with harness.session("approve") as agent:
        await agent.run("work twice")
        assert asked == ["work"]
        assert stages == ["policy", "tool", "review", "policy", "tool", "review"]
        assert any(m.content == "consider this" for m in model.requests[-1].messages)
        results = [e.result for e in agent.log.events if isinstance(e, ToolCallFinishedEvent)]
        assert all(result.content == "done\n\nReviewed" for result in results)


async def test_class_plugin_state_is_fresh_for_every_session_and_methods_support_di():

    class Counter(Plugin, needs=(Session,)):
        def __init__(self):
            self.count = 0

        @tool
        async def count_calls(self, agent: Inject[Session]) -> int:
            self.count += 1
            assert agent.status == "running"
            return self.count

        @hook(ReviewToolResult)
        async def review(self, event, next_, agent: Inject[Session]):
            assert agent.id in ("one", "two")
            return await next_()

    template = Counter()
    model = ScriptedModel(
        [
            [ModelChunk(tool_call=ToolCall("count_calls"))],
            "one",
            [ModelChunk(tool_call=ToolCall("count_calls"))],
            "two",
        ]
    )
    harness = Application(model=model, plugins=[Approvals(default=PolicyDecision.ALLOW), template])
    for session_id in ("one", "two"):
        async with harness.session(session_id) as agent:
            result = await agent.run("count")
            assert (
                next(
                    e.result.content for e in result.events if isinstance(e, ToolCallFinishedEvent)
                )
                == "1"
            )
    assert template.count == 0


async def test_function_plugin_can_inject_current_agent_with_explicit_needs():
    observed = []

    @plugin(needs=(Session, ToolRegistry))
    async def observer(ctx, agent: Inject[Session], registry: Inject[ToolRegistry]):
        observed.append((agent.id, registry))
        yield
        assert agent.status == "closed"

    harness = Application(
        model=ScriptedModel(["done"]), plugins=[Approvals(default=PolicyDecision.ALLOW), observer()]
    )
    async with harness.session("injected-agent") as agent:
        assert observed == [(agent.id, agent.ctx[ToolRegistry])]
        await agent.run("go")


async def test_explicit_and_live_registration():
    class Echo(Plugin):
        @tool
        async def echo(self, text: str) -> str:
            return text

        @hook(ReviewToolResult)
        async def review(self, event, next_):
            return (await next_()).with_note("ok")

    class Alias(Plugin):
        @tool
        async def alias(self, text: str) -> str:
            return text

    class Live(Plugin):
        @tool
        async def live(self, text: str) -> str:
            return text

    model = ScriptedModel([[ModelChunk(tool_call=ToolCall("echo", {"text": "hello"}))], "done"])
    harness = Application(
        model=model,
        plugins=[Approvals(default=PolicyDecision.ALLOW), Echo(), Alias()],
    )
    async with harness.session("explicit") as agent:
        handle = await agent.use(Live())
        seen = []
        agent.add_hook(ToolCallFinishedEvent, lambda event: seen.append(event.result.content))
        await agent.run("echo")
        assert seen == ["hello\n\nok"]
        assert {d.name for d in model.requests[0].tools} == {"echo", "alias", "live"}
        await handle.aclose()
        assert {d.name for d in agent.ctx[ToolRegistry].definitions} == {"echo", "alias"}


async def test_pre_step_rejection_consumes_input_permanently():
    model = ScriptedModel(["second"])
    harness = Application(model=model, plugins=[Approvals(default=PolicyDecision.ALLOW)])
    async with harness.session("reject") as agent:
        reject = True

        @agent.on(PreStep)
        async def gate(event, next_):
            return StepDecision.reject() if reject else await next_()

        result = await agent.run("first")
        assert result.stop_reason == StopReason.REJECTED
        assert not model.requests and not agent.inbox.pending
        reject = False
        await agent.submit("second")
        await agent.control.join()
        assert len(model.requests) == 1 and not agent.inbox.pending
        assert all(message.text != "first" for message in model.requests[0].messages)
        assert len(agent.turns) == 2


async def test_cancel_before_driver_starts_and_reuse_agent():
    model = ScriptedModel(["after cancel"])
    async with Application(model=model, plugins=[Approvals(default=PolicyDecision.ALLOW)]).session(
        "cancel-before-start"
    ) as agent:
        await agent.submit("discard")
        await agent.abort()
        assert agent.status == "idle" and (not agent.inbox.pending)
        assert (await agent.run("new")).text == "after cancel"


async def test_cancel_stream_preserves_visible_prefix_and_closes_turn():
    started = asyncio.Event()

    class PartialModel(CallbackModel):
        def __init__(self):
            super().__init__(self._chunks)

        async def _chunks(self, request):
            yield ModelChunk(text="visible prefix")
            started.set()
            await asyncio.Event().wait()

    async with Application(
        model=PartialModel(), plugins=[Approvals(default=PolicyDecision.ALLOW)]
    ).session("partial") as agent:
        await agent.submit("go")
        await started.wait()
        await agent.abort()
        partial = next(e for e in agent.log.events if isinstance(e, AssistantMessageEvent))
        assert partial.interrupted and partial.message.content == "visible prefix"
        assert isinstance(agent.log.last(TurnEndedEvent), TurnEndedEvent)
        assert agent.log.last(TurnEndedEvent).reason == TurnOutcome.ABORTED


async def test_cancel_tool_answers_outstanding_call_and_sets_signal():
    started = asyncio.Event()
    calls = []

    class BundlePlugin(Plugin):
        @tool
        async def wait(self, call: Inject[ToolContext]) -> str:
            calls.append(call)
            started.set()
            await asyncio.Event().wait()
            return "never"

    bundle = BundlePlugin(name="blocking")

    model = ScriptedModel([[ModelChunk(tool_call=ToolCall("wait"))]])
    async with Application(
        model=model, plugins=[Approvals(default=PolicyDecision.ALLOW), bundle]
    ).session("cancel-tool") as agent:
        await agent.submit("go")
        await started.wait()
        await agent.abort()
        assert calls[0].signal.is_set()
        results = [e.result for e in agent.log.events if isinstance(e, ToolCallFinishedEvent)]
        assert len(results) == 1 and results[0].is_error


async def test_same_harness_rejects_two_live_writers_and_max_steps_bounds_the_loop():

    class BundlePlugin(Plugin):
        @tool
        async def again(self) -> str:
            return "again"

    bundle = BundlePlugin(name="loop")

    model = ScriptedModel([[ModelChunk(tool_call=ToolCall("again"))]] * 2)
    harness = Application(
        model=model,
        plugins=[AgentLoop(max_steps=2), Approvals(default=PolicyDecision.ALLOW), bundle],
    )
    async with harness.session("same") as agent:
        with pytest.raises(RuntimeError, match="already"):
            async with harness.session("same"):
                pytest.fail("A second writer entered")
        await agent.run("loop")
        assert agent.log.events[-1].reason == TurnOutcome.MAX_STEPS


async def test_observer_failure_closes_model_iterator_before_run_returns():
    closed = asyncio.Event()

    class ResourceModel(CallbackModel):
        def __init__(self):
            super().__init__(self._chunks)

        async def _chunks(self, request):
            try:
                yield ModelChunk(text="partial")
                await asyncio.Event().wait()
            finally:
                closed.set()

    async with Application(
        model=ResourceModel(), plugins=[Approvals(default=PolicyDecision.ALLOW)]
    ).session("resource") as agent:

        @agent.on(TextDeltaEvent)
        def broken(event):
            raise ValueError("observer failure")

        with pytest.raises(ValueError, match="observer failure"):
            await agent.run("go")
        assert closed.is_set()
        assert agent.log.messages[-1].content == "partial"


async def test_policy_ask_routes_to_approver_seam():
    asked = []

    class FeaturesPlugin(Plugin):
        @hook(ToolPolicy)
        async def policy(self, event, next_):
            return PolicyDecision.ASK

        @tool
        async def approved(self) -> str:
            return "allowed"

    features = FeaturesPlugin(name="ask")

    def approve(request):
        asked.append(request.call.name)
        return ApprovalAnswer.ALLOW

    model = ScriptedModel([[ModelChunk(tool_call=ToolCall("approved"))], "done"])
    harness = Application(
        model=model,
        plugins=[
            Approvals(default=PolicyDecision.ALLOW),
            features,
            Service(Approver, CallbackApprover(approve)),
        ],
    )
    async with harness.session("ask") as agent:
        await agent.run("ask")
        assert asked == ["approved"]


async def test_canceling_when_idle_waiter_does_not_cancel_the_agent():
    model = PausingModel()
    async with Application(model=model, plugins=[Approvals(default=PolicyDecision.ALLOW)]).session(
        "waiter"
    ) as agent:
        await agent.submit("go")
        await model.started.wait()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(agent.control.join(), timeout=0.01)
        assert agent.status == "running"
        model.release.set()
        await agent.control.join()
        assert agent.log.messages[-1].content == "response 1"
