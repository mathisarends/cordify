import asyncio

import pytest

from harnessed import Agent as Application
from harnessed import ApprovalAnswer, Plugin, ToolCall, tool
from harnessed.core.approvals import ApprovalGate, Approver, CallbackApprover
from harnessed.core.errors import ApprovalUnavailable
from harnessed.core.hooks import RequestApproval, ToolPolicy
from harnessed.core.messages import PolicyDecision, ToolStatus
from harnessed.core.model import ModelChunk
from harnessed.core.runtime.options import AgentLoopConfig
from harnessed.core.session.codec import decode_event, encode_event
from harnessed.core.session.events import (
    ApprovalResolvedEvent,
    ApprovalStatus,
    ToolCallFinishedEvent,
    ToolExecutionStartedEvent,
)
from harnessed.core.session.session import SessionLog
from harnessed.kernel import hook
from harnessed.kernel.plugin import PluginStatus
from harnessed.plugins import AgentLoop, Approvals, JsonlSessions, Service
from harnessed.testing import ScriptedModel


async def test_removing_an_approver_never_removes_the_scoped_guard():
    from harnessed import Plugin, tool
    from harnessed.core.approvals import ApprovalGate, CallbackApprover
    from harnessed.plugins import Service
    from harnessed.testing import call

    effects = []

    class Publish(Plugin):
        @tool
        async def publish(self) -> str:
            effects.append("published")
            return "ready"

    model = ScriptedModel(
        [call("publish"), "first", call("publish"), "blocked", call("publish"), "third"]
    )
    async with Application(
        model=model, plugins=[Approvals(default=PolicyDecision.ALLOW), Publish()]
    ).session() as session:
        guard = await session.use(ApprovalGate(risky={"publish"}))
        provider = await session.use(Service(Approver, CallbackApprover(lambda call: True)))
        assert (await session.run("publish")).tool_calls[0][1].status == ToolStatus.OK
        await provider.aclose()
        await session.ctx.settle()
        assert guard.status == PluginStatus.ACTIVE
        assert (await session.run("publish")).tool_calls[0][1].status == ToolStatus.UNAVAILABLE
        await session.use(Service(Approver, CallbackApprover(lambda call: True)))
        assert (await session.run("publish")).tool_calls[0][1].status == ToolStatus.OK
        assert effects == ["published", "published"]


async def test_approval_gate_without_a_ui_fails_closed_and_preserves_unrelated_calls():
    executed = []

    class ToolsPlugin(Plugin):
        @tool
        async def risky(self) -> str:
            pytest.fail("Approval gate disappeared without a UI")

        @tool
        async def safe(self) -> str:
            executed.append("safe")
            return "ok"

    tools = ToolsPlugin(name="headless")

    calls = [ToolCall("risky"), ToolCall("safe")]
    model = ScriptedModel([[ModelChunk(tool_call=call) for call in calls], "done"])
    async with Application(
        model=model,
        plugins=[Approvals(default=PolicyDecision.ALLOW), tools, ApprovalGate(risky={"risky"})],
    ).session("no-ui") as agent:
        await asyncio.wait_for(agent.run("go"), 2)
        results = [
            event.result for event in agent.log.events if isinstance(event, ToolCallFinishedEvent)
        ]
        assert [result.status for result in results] == ["unavailable", "ok"]
        assert "No approval responder" in results[0].content
        assert executed == ["safe"]
        approvals = agent.log.approvals
        assert not approvals.pending and (not approvals.grants)
        assert approvals.records[0].resolution.status == ApprovalStatus.UNAVAILABLE
        assert approvals.records[0].decision is None
        replay = SessionLog(
            "replay", seed=tuple(decode_event(encode_event(e)) for e in agent.log.events)
        )
        assert replay.approvals == approvals


async def test_direct_sdk_approval_raises_typed_unavailable_and_leaves_no_pending_question():
    async with Application(
        model=ScriptedModel([]), plugins=[Approvals(default=PolicyDecision.ALLOW)]
    ).session("direct") as agent:
        with pytest.raises(ApprovalUnavailable, match="No approval responder"):
            await asyncio.wait_for(agent.approvals.request(ToolCall("work"), "permission"), 2)
        record = agent.log.approvals.records[-1]
        assert record.resolution.status == ApprovalStatus.UNAVAILABLE and record.decision is None
        assert not agent.log.approvals.pending


async def test_explicit_approver_then_service_take_precedence_over_request_listeners():

    class Answer:
        def __init__(self, decision):
            self.decision = decision

        async def ask(self, request):
            return self.decision

    async with Application(
        model=ScriptedModel([]), plugins=[Approvals(default=PolicyDecision.ALLOW)]
    ).session("precedence") as agent:
        agent.ctx.provide(Approver, Answer(ApprovalAnswer.DENY))
        agent.on(RequestApproval, lambda ev: pytest.fail("Explicit approver was bypassed"))
        assert (
            await agent.approvals.request(
                ToolCall("work"), "permission", approver=Answer(ApprovalAnswer.ALLOW)
            )
            is ApprovalAnswer.ALLOW
        )
        assert await agent.approvals.request(ToolCall("work"), "permission") is ApprovalAnswer.DENY


async def test_approval_listeners_bail_by_priority_and_can_explicitly_abstain_or_deny():
    asked = []
    async with Application(
        model=ScriptedModel([]), plugins=[Approvals(default=PolicyDecision.ALLOW)]
    ).session("listeners") as agent:
        agent.on(RequestApproval, lambda ev: asked.append("late"), priority=100)

        def answer(event):
            assert event.call.name == "work" and event.reason == "permission"
            assert event.request.request_id in {
                item.request_id for item in event.session.log.approvals.pending
            }
            asked.append("deny")
            return ApprovalAnswer.DENY

        remove_answer = agent.add_hook(RequestApproval, answer, priority=0)
        agent.on(RequestApproval, lambda ev: asked.append("abstain"), priority=-100)
        assert await agent.approvals.request(ToolCall("work"), "permission") is ApprovalAnswer.DENY
        assert asked == ["abstain", "deny"]
        await remove_answer()
        asked.clear()
        with pytest.raises(ApprovalUnavailable):
            await agent.approvals.request(ToolCall("work"), "permission")
        assert asked == ["abstain", "late"]


@pytest.mark.parametrize("transport", ["listener", "service", "callback"])
async def test_unresponsive_approval_uses_the_session_timeout_and_cancels_the_responder(transport):
    stopped = asyncio.Event()

    async def wait(*args):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    class WaitingApprover:
        ask = staticmethod(wait)

    plugins = [Service(Approver, CallbackApprover(wait))] if transport == "callback" else []
    harness = Application(
        model=ScriptedModel([]),
        plugins=[
            AgentLoop(approval_timeout=0.02),
            Approvals(default=PolicyDecision.ALLOW, timeout=0.02),
            *plugins,
        ],
    )
    async with harness.session("timeout") as agent:
        if transport == "listener":
            agent.on(RequestApproval, wait)
        elif transport == "service":
            agent.ctx.provide(Approver, WaitingApprover())
        with pytest.raises(ApprovalUnavailable, match="timed out after 0.02 seconds"):
            await asyncio.wait_for(agent.approvals.request(ToolCall("work"), "permission"), 2)
        assert stopped.is_set()
        assert not agent.log.approvals.pending
        assert agent.log.approvals.records[-1].resolution.status == ApprovalStatus.UNAVAILABLE


@pytest.mark.parametrize("answer", [None, PolicyDecision.ASK, "allow", ValueError("disconnected")])
async def test_responder_abstention_invalid_answers_and_failures_are_unavailable(answer):

    async def respond(event):
        if isinstance(answer, Exception):
            raise answer
        return answer

    async with Application(
        model=ScriptedModel([]), plugins=[Approvals(default=PolicyDecision.ALLOW)]
    ).session("bad-answer") as agent:
        agent.on(RequestApproval, respond)
        with pytest.raises(ApprovalUnavailable):
            await agent.approvals.request(ToolCall("work"), "permission")
        record = agent.log.approvals.records[-1]
        assert record.resolution.status == ApprovalStatus.UNAVAILABLE and record.resolution.reason
        assert not record.pending and (not record.is_grant)


async def test_gate_stays_active_after_approver_service_is_removed():
    executed = []

    class ToolsPlugin(Plugin):
        @tool
        async def work(self) -> str:
            executed.append(True)
            return "ok"

    tools = ToolsPlugin(name="tools")

    class AllowOnce:
        async def ask(self, request):
            return ApprovalAnswer.ALLOW

    model = ScriptedModel(
        [
            [ModelChunk(tool_call=ToolCall("work"))],
            "first",
            [ModelChunk(tool_call=ToolCall("work"))],
            "second",
        ]
    )
    async with Application(
        model=model,
        plugins=[Approvals(default=PolicyDecision.ALLOW), tools, ApprovalGate(risky={"work"})],
    ).session("disconnect") as agent:
        remove = agent.ctx.provide(Approver, AllowOnce())
        await agent.run("first")
        await remove()
        await agent.ctx.settle()
        await agent.run("second")
        assert executed == [True]
        assert agent.log.approvals.records[-1].resolution.status == ApprovalStatus.UNAVAILABLE
        assert len([e for e in agent.log.events if isinstance(e, ToolExecutionStartedEvent)]) == 1


async def test_resume_preserves_unavailable_without_reasking_and_can_accept_a_new_request(tmp_path):
    async with Application(
        model=ScriptedModel([]),
        plugins=[Approvals(default=PolicyDecision.ALLOW), JsonlSessions(tmp_path)],
    ).session("resume") as agent:
        with pytest.raises(ApprovalUnavailable):
            await agent.approvals.request(ToolCall("work"), "permission")
        prefix = agent.log.events
    async with Application(
        model=ScriptedModel([]),
        plugins=[
            Approvals(default=PolicyDecision.ALLOW),
            JsonlSessions(tmp_path),
            Service(Approver, CallbackApprover(lambda call: ApprovalAnswer.ALWAYS)),
        ],
    ).session("resume") as resumed:
        assert resumed.log.events == prefix and (not resumed.log.approvals.pending)
        assert (
            await resumed.approvals.request(ToolCall("work"), "permission") is ApprovalAnswer.ALWAYS
        )
        assert len(resumed.log.approvals.records) == 2
        grant = resumed.log.approvals.grants[0]
    async with Application(
        model=ScriptedModel([]),
        plugins=[Approvals(default=PolicyDecision.ALLOW), JsonlSessions(tmp_path)],
    ).session("resume") as resumed:
        assert (
            await resumed.approvals.request(ToolCall("work"), "permission") is ApprovalAnswer.ALWAYS
        )
        assert resumed.log.approvals.records[-1].request.grant_id == grant.request_id


@pytest.mark.parametrize("gate_first", [True, False])
async def test_approval_priority_places_wait_outside_downstream_policy_timing(gate_first):
    clock, measured, order = ([0], [], [])

    class ToolsPlugin(Plugin):
        @tool
        async def work(self) -> str:
            return "ok"

        @hook(ToolPolicy, priority=0)
        async def timing(self, event, next_):
            order.append("timer")
            before = clock[0]
            result = await next_()
            measured.append(clock[0] - before)
            return result

        @hook(ToolPolicy, priority=100)
        async def policy(self, event, next_):
            order.append("policy")
            clock[0] += 2
            return await next_()

    tools = ToolsPlugin(name="timing")

    async def answer(call):
        order.append("approval")
        clock[0] += 100
        return ApprovalAnswer.ALLOW

    gate = ApprovalGate(risky={"work"}, ask=answer)
    plugins = [gate, tools] if gate_first else [tools, gate]
    model = ScriptedModel([[ModelChunk(tool_call=ToolCall("work"))], "done"])
    async with Application(
        model=model, plugins=[Approvals(default=PolicyDecision.ALLOW), *plugins]
    ).session("timing") as agent:
        await agent.run("go")
    assert order == ["approval", "timer", "policy"]
    assert measured == [2]


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf"), "5"])
def test_approval_timeout_must_be_finite_and_positive(timeout):
    with pytest.raises(ValueError, match="approval_timeout"):
        Application(
            model=ScriptedModel([]),
            plugins=[
                AgentLoop(approval_timeout=timeout),
                Approvals(default=PolicyDecision.ALLOW, timeout=timeout),
            ],
        )
    with pytest.raises(ValueError, match="approval_timeout"):
        AgentLoopConfig(approval_timeout=timeout)


def test_unavailable_resolution_is_not_an_answer_or_a_persistent_permission():
    with pytest.raises(ValueError):
        ApprovalResolvedEvent("request", ApprovalAnswer.ALLOW, status=ApprovalStatus.UNAVAILABLE)
    with pytest.raises(TypeError):
        ApprovalResolvedEvent("request", ApprovalAnswer.ALLOW, reason=123)
