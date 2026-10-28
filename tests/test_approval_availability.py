import asyncio

import pytest

from harnessed import (
    AgentOptions,
    ApprovalAnswer,
    ApprovalRequest,
    ApprovalUnavailable,
    Approver,
    Bundle,
    ModelChunk,
    Session,
    ToolCall,
)
from harnessed.core.events import ApprovalResolved, ToolExecutionStarted, ToolPolicy, ToolReturned
from harnessed.core.messages import PolicyDecision
from harnessed.core.session import decode_event, encode_event
from harnessed.plugins.permissions.approval import approval_gate, callback_approver
from harnessed.plugins.persistence import session_jsonl
from harnessed.presets import headless, minimal
from harnessed.testing import ScriptedLLM


@pytest.mark.parametrize("preset", [minimal, headless])
async def test_approval_gate_without_a_ui_fails_closed_and_preserves_unrelated_calls(preset):
    tools = Bundle("headless")
    executed = []

    @tools.tool
    async def risky() -> str:
        pytest.fail("Approval gate disappeared without a UI")

    @tools.tool
    async def safe() -> str:
        executed.append("safe")
        return "ok"

    calls = [ToolCall("risky"), ToolCall("safe")]
    model = ScriptedLLM([[ModelChunk(tool_call=call) for call in calls], "done"])
    async with preset(model, plugins=[tools, approval_gate(risky={"risky"})]).session(
        "no-ui"
    ) as agent:
        await asyncio.wait_for(agent.run("go"), 2)
        results = [
            event.result for event in agent.session.events if isinstance(event, ToolReturned)
        ]
        assert [result.status for result in results] == ["unavailable", "ok"]
        assert "No approval responder" in results[0].content
        assert executed == ["safe"]
        approvals = agent.session.approvals
        assert not approvals.pending and not approvals.grants
        assert approvals.records[0].resolution.status == "unavailable"
        assert approvals.records[0].decision is None
        replay = Session(
            "replay", seed=tuple(decode_event(encode_event(e)) for e in agent.session.events)
        )
        assert replay.approvals == approvals


async def test_direct_sdk_approval_raises_typed_unavailable_and_leaves_no_pending_question():
    async with minimal(ScriptedLLM([])).session("direct") as agent:
        with pytest.raises(ApprovalUnavailable, match="No approval responder"):
            await asyncio.wait_for(agent.request_approval(ToolCall("work"), "permission"), 2)
        record = agent.session.approvals.records[-1]
        assert record.resolution.status == "unavailable" and record.decision is None
        assert not agent.session.approvals.pending


async def test_explicit_approver_then_service_take_precedence_over_request_listeners():
    class Answer:
        def __init__(self, decision):
            self.decision = decision

        async def ask(self, call, reason):
            return self.decision

    async with minimal(ScriptedLLM([])).session("precedence") as agent:
        agent.ctx.provide(Approver, Answer(ApprovalAnswer.DENY))
        agent.on(ApprovalRequest, lambda ev: pytest.fail("Explicit approver was bypassed"))
        assert (
            await agent.request_approval(
                ToolCall("work"), "permission", approver=Answer(ApprovalAnswer.ALLOW)
            )
            is ApprovalAnswer.ALLOW
        )
        assert await agent.request_approval(ToolCall("work"), "permission") is ApprovalAnswer.DENY


async def test_approval_listeners_bail_by_priority_and_can_explicitly_abstain_or_deny():
    asked = []
    async with minimal(ScriptedLLM([])).session("listeners") as agent:
        agent.on(ApprovalRequest, lambda ev: asked.append("late"), priority=100)

        def answer(event):
            assert event.call.name == "work" and event.reason == "permission"
            assert event.request.request_id in {
                item.request_id for item in event.agent.session.approvals.pending
            }
            asked.append("deny")
            return ApprovalAnswer.DENY

        remove_answer = agent.add_hook(ApprovalRequest, answer, priority=0)
        agent.on(ApprovalRequest, lambda ev: asked.append("abstain"), priority=-100)
        assert await agent.request_approval(ToolCall("work"), "permission") is ApprovalAnswer.DENY
        assert asked == ["abstain", "deny"]
        await remove_answer()
        asked.clear()
        with pytest.raises(ApprovalUnavailable):
            await agent.request_approval(ToolCall("work"), "permission")
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

    plugins = [callback_approver(wait)] if transport == "callback" else []
    harness = minimal(ScriptedLLM([]), plugins=plugins, approval_timeout=10)
    async with harness.session("timeout", approval_timeout=0.02) as agent:
        if transport == "listener":
            agent.on(ApprovalRequest, wait)
        elif transport == "service":
            agent.ctx.provide(Approver, WaitingApprover())
        with pytest.raises(ApprovalUnavailable, match="timed out after 0.02 seconds"):
            await asyncio.wait_for(agent.request_approval(ToolCall("work"), "permission"), 2)
        assert stopped.is_set()
        assert not agent.session.approvals.pending
        assert agent.session.approvals.records[-1].resolution.status == "unavailable"


@pytest.mark.parametrize("answer", [None, PolicyDecision.ASK, "allow", ValueError("disconnected")])
async def test_responder_abstention_invalid_answers_and_failures_are_unavailable(answer):
    async def respond(event):
        if isinstance(answer, Exception):
            raise answer
        return answer

    async with minimal(ScriptedLLM([])).session("bad-answer") as agent:
        agent.on(ApprovalRequest, respond)
        with pytest.raises(ApprovalUnavailable):
            await agent.request_approval(ToolCall("work"), "permission")
        record = agent.session.approvals.records[-1]
        assert record.resolution.status == "unavailable" and record.resolution.reason
        assert not record.pending and not record.is_grant


async def test_gate_stays_active_after_approver_service_is_removed():
    tools = Bundle("tools")
    executed = []

    @tools.tool
    async def work() -> str:
        executed.append(True)
        return "ok"

    class AllowOnce:
        async def ask(self, call, reason):
            return ApprovalAnswer.ALLOW

    model = ScriptedLLM(
        [
            [ModelChunk(tool_call=ToolCall("work"))],
            "first",
            [ModelChunk(tool_call=ToolCall("work"))],
            "second",
        ]
    )
    async with minimal(model, plugins=[tools, approval_gate(risky={"work"})]).session(
        "disconnect"
    ) as agent:
        remove = agent.ctx.provide(Approver, AllowOnce())
        await agent.run("first")
        await remove()
        await agent.ctx.settle()
        await agent.run("second")
        assert executed == [True]
        assert agent.session.approvals.records[-1].resolution.status == "unavailable"
        assert len([e for e in agent.session.events if isinstance(e, ToolExecutionStarted)]) == 1


async def test_resume_preserves_unavailable_without_reasking_and_can_accept_a_new_request(tmp_path):
    async with minimal(ScriptedLLM([]), plugins=[session_jsonl(tmp_path)]).session(
        "resume"
    ) as agent:
        with pytest.raises(ApprovalUnavailable):
            await agent.request_approval(ToolCall("work"), "permission")
        prefix = agent.session.events
    async with minimal(
        ScriptedLLM([]),
        plugins=[session_jsonl(tmp_path), callback_approver(lambda call: ApprovalAnswer.ALWAYS)],
    ).session("resume") as resumed:
        assert resumed.session.events == prefix and not resumed.session.approvals.pending
        assert (
            await resumed.request_approval(ToolCall("work"), "permission") is ApprovalAnswer.ALWAYS
        )
        assert len(resumed.session.approvals.records) == 2
        grant = resumed.session.approvals.grants[0]
    async with minimal(ScriptedLLM([]), plugins=[session_jsonl(tmp_path)]).session(
        "resume"
    ) as resumed:
        assert (
            await resumed.request_approval(ToolCall("work"), "permission") is ApprovalAnswer.ALWAYS
        )
        assert resumed.session.approvals.records[-1].request.grant_id == grant.request_id


@pytest.mark.parametrize("gate_first", [True, False])
async def test_approval_priority_places_wait_outside_downstream_policy_timing(gate_first):
    tools = Bundle("timing")
    clock, measured, order = [0], [], []

    @tools.tool
    async def work() -> str:
        return "ok"

    @tools.on(ToolPolicy, priority=0)
    async def timing(event, next_):
        order.append("timer")
        before = clock[0]
        result = await next_()
        measured.append(clock[0] - before)
        return result

    @tools.on(ToolPolicy, priority=100)
    async def policy(event, next_):
        order.append("policy")
        clock[0] += 2
        return await next_()

    async def answer(call):
        order.append("approval")
        clock[0] += 100
        return ApprovalAnswer.ALLOW

    gate = approval_gate(risky={"work"}, ask=answer)
    plugins = [gate, tools] if gate_first else [tools, gate]
    model = ScriptedLLM([[ModelChunk(tool_call=ToolCall("work"))], "done"])
    async with minimal(model, plugins=plugins).session("timing") as agent:
        await agent.run("go")
    assert order == ["approval", "timer", "policy"]
    assert measured == [2]


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf"), "5"])
def test_approval_timeout_must_be_finite_and_positive(timeout):
    with pytest.raises(ValueError, match="approval_timeout"):
        minimal(ScriptedLLM([]), approval_timeout=timeout)
    with pytest.raises(ValueError, match="approval_timeout"):
        AgentOptions("model", approval_timeout=timeout)


def test_unavailable_resolution_is_not_an_answer_or_a_persistent_permission():
    with pytest.raises(ValueError):
        ApprovalResolved("request", ApprovalAnswer.ALLOW, status="unavailable")
    with pytest.raises(TypeError):
        ApprovalResolved("request", ApprovalAnswer.ALLOW, reason=123)
