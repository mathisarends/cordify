import asyncio
from dataclasses import FrozenInstanceError

import pytest

from harnessed import (
    AgentHandle as Agent,
)
from harnessed import (
    ApprovalAnswer,
    Bundle,
    Inject,
    ModelChunk,
    Session,
    SessionPersistence,
    ToolCall,
    plugin,
)
from harnessed.core.approvals import ApprovalState
from harnessed.core.events import (
    ApprovalRequested,
    ApprovalResolved,
    ApprovalRevoked,
    ToolExecutionStarted,
    ToolPolicy,
    ToolPolicyApplied,
    ToolReturned,
)
from harnessed.core.messages import PolicyDecision
from harnessed.core.session import decode_event, encode_event
from harnessed.plugins.permissions.approval import approval_gate, callback_approver
from harnessed.plugins.persistence import session_jsonl
from harnessed.presets import minimal
from harnessed.testing import ScriptedLLM


def tool_model():
    return ScriptedLLM([[ModelChunk(tool_call=ToolCall("work"))], "done"])


async def test_always_grants_and_cached_uses_survive_a_fresh_harness_and_can_be_revoked(tmp_path):
    tools = Bundle("work")
    asked, executed = [], []

    @tools.tool
    async def work(agent: Inject[Agent]) -> str:
        assert isinstance(agent.session.events[-1], ToolExecutionStarted)
        assert agent.session.approvals.records[-1].decision is ApprovalAnswer.ALWAYS
        executed.append(True)
        return "ok"

    def answer(call):
        asked.append(call.id)
        return ApprovalAnswer.ALWAYS

    def harness():
        return minimal(
            tool_model(),
            plugins=[tools, approval_gate(risky={"work"}, ask=answer), session_jsonl(tmp_path)],
        )

    async with harness().session("same-session") as agent:
        await agent.run("first")
        grant = agent.session.approvals.grants[0]
        prefix = agent.session.events
        fork = agent.session.fork("new-session")
        assert fork.events[: len(prefix)] == prefix and not fork.approvals.grants
        assert agent.session.fork("trusted", inherit_approvals=True).approvals.grants == (grant,)
        with pytest.raises(FrozenInstanceError):
            grant.revoked = True
    async with harness().session("same-session") as resumed:
        assert resumed.session.approvals.grants == (grant,)
        await resumed.run("second")
        cached = resumed.session.approvals.records[-1]
        assert cached.request.grant_id == grant.request_id and not cached.is_grant
        assert len(asked) == 1 and len(executed) == 2
        await resumed.revoke_approval(grant.request_id)
        after_revocation = resumed.session.events
        await resumed.revoke_approval(grant.request_id)
        assert resumed.session.events == after_revocation and not resumed.session.approvals.grants
    async with harness().session("same-session") as resumed:
        await resumed.run("third")
        assert len(asked) == 2 and len(executed) == 3
        events = tuple(decode_event(encode_event(event)) for event in resumed.session.events)
        assert ApprovalState(events) == resumed.session.approvals


@pytest.mark.parametrize("decision", [ApprovalAnswer.ALLOW, ApprovalAnswer.DENY])
async def test_single_call_answers_are_logged_without_becoming_persistent_grants(
    tmp_path, decision
):
    tools = Bundle("work")
    asked, executed = [], []

    @tools.tool
    async def work() -> str:
        executed.append(True)
        return "ok"

    def answer(call):
        asked.append(call.id)
        return decision

    for _ in range(2):
        harness = minimal(
            tool_model(),
            plugins=[tools, approval_gate(risky={"work"}, ask=answer), session_jsonl(tmp_path)],
        )
        async with harness.session("single-use") as agent:
            await agent.run("task")
            assert not agent.session.approvals.grants
            assert agent.session.approvals.records[-1].decision is decision
            policy = next(
                e for e in reversed(agent.session.events) if isinstance(e, ToolPolicyApplied)
            )
        assert policy.decision is PolicyDecision(decision.value)
    assert len(asked) == 2
    assert len(executed) == (2 if decision is ApprovalAnswer.ALLOW else 0)


async def test_gate_scopes_do_not_share_persistent_permissions(tmp_path):
    tools = Bundle("work")
    asked = []

    @tools.tool
    async def work() -> str:
        return "ok"

    def answer(call):
        asked.append(call.id)
        return ApprovalAnswer.ALWAYS

    for scope in ("first-policy", "second-policy"):
        async with minimal(
            tool_model(),
            plugins=[
                tools,
                approval_gate(risky={"work"}, ask=answer, scope=scope),
                session_jsonl(tmp_path),
            ],
        ).session("scopes") as agent:
            await agent.run("task")
    assert len(asked) == 2


async def test_tool_policy_ask_uses_the_same_durable_approval_path(tmp_path):
    tools = Bundle("work")
    asked = []

    @tools.tool
    async def work() -> str:
        return "ok"

    @tools.on(ToolPolicy)
    async def ask_policy(event, next_):
        return PolicyDecision.ASK

    def answer(call):
        asked.append(call.id)
        return ApprovalAnswer.ALWAYS

    for _ in range(2):
        async with minimal(
            tool_model(), plugins=[tools, callback_approver(answer), session_jsonl(tmp_path)]
        ).session("policy-ask") as agent:
            await agent.run("task")
            assert agent.session.approvals.grant_for("work", scope="tool-policy") is not None
    assert len(asked) == 1


async def test_cancellation_closes_an_unanswered_approval_and_does_not_execute_the_tool():
    tools = Bundle("work")
    asked = asyncio.Event()

    @tools.tool
    async def work() -> str:
        pytest.fail("Tool ran without an approval answer")

    async def wait(call):
        asked.set()
        await asyncio.Event().wait()

    async with minimal(
        tool_model(), plugins=[tools, approval_gate(risky={"work"}, ask=wait)]
    ).session("cancel-approval") as agent:
        await agent.followup("task")
        await asked.wait()
        assert len(agent.session.approvals.pending) == 1
        await agent.cancel()
        assert not agent.session.approvals.pending and not agent.session.approvals.grants
        assert agent.session.approvals.records[-1].resolution.status == "interrupted"
        assert not any(isinstance(e, ToolExecutionStarted) for e in agent.session.events)
        assert any(isinstance(e, ToolReturned) and e.result.is_error for e in agent.session.events)


async def test_uncommitted_approval_answer_cannot_start_execution():
    class FailingPersistence:
        async def load(self, session_id):
            return ()

        async def flush(self):
            pass

        async def append(self, session_id, seq, event):
            if isinstance(event, ApprovalResolved) and event.status == "answered":
                raise OSError("approval could not be saved")

    @plugin
    async def persistence(ctx):
        ctx.provide(SessionPersistence, FailingPersistence())
        yield

    tools = Bundle("work")

    @tools.tool
    async def work() -> str:
        pytest.fail("Uncommitted approval allowed a tool execution")

    async with minimal(
        tool_model(),
        plugins=[
            tools,
            persistence(),
            approval_gate(risky={"work"}, ask=lambda call: ApprovalAnswer.ALWAYS),
        ],
    ).session("failed-answer") as agent:
        await agent.run("task")
        assert not agent.session.approvals.pending and not agent.session.approvals.grants
        assert not any(isinstance(e, ToolExecutionStarted) for e in agent.session.events)


@pytest.mark.parametrize(
    "events",
    [
        (ApprovalResolved("unknown", ApprovalAnswer.ALLOW),),
        (ApprovalRevoked("unknown"),),
        (ApprovalRequested("id", ToolCall("work"), "reason", "scope", grant_id="missing"),),
        (
            ApprovalRequested("id", ToolCall("work"), "reason", "scope"),
            ApprovalResolved("id", ApprovalAnswer.DENY),
            ApprovalRevoked("id"),
        ),
    ],
)
def test_invalid_approval_transitions_are_rejected_by_replay(events):
    with pytest.raises(ValueError):
        Session("invalid", seed=events)
