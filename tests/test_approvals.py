import asyncio
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError

import pytest

from harnessed import Agent as Application
from harnessed import (
    ApprovalAnswer,
    Bundle,
    Inject,
    ModelChunk,
    ToolCall,
    plugin,
)
from harnessed import Session as Agent
from harnessed.core.approvals import ApprovalState
from harnessed.core.hooks import ToolPolicy
from harnessed.core.messages import PolicyDecision
from harnessed.core.session.codec import decode_event, encode_event
from harnessed.core.session.events import (
    ApprovalRequested,
    ApprovalResolved,
    ApprovalRevoked,
    ToolExecutionStarted,
    ToolPolicyApplied,
    ToolReturned,
)
from harnessed.core.session.files import MemoryFiles
from harnessed.core.session.session import SessionLog as Session
from harnessed.core.session.store import SessionStore
from harnessed.plugins import Approvals, JsonlSessions
from harnessed.plugins.permissions.approval import approval_gate, callback_approver
from harnessed.testing import ScriptedModel


def tool_model():
    return ScriptedModel([[ModelChunk(tool_call=ToolCall("work"))], "done"])


async def test_always_grants_and_cached_uses_survive_a_fresh_harness_and_can_be_revoked(tmp_path):
    tools = Bundle("work")
    asked, executed = ([], [])

    @tools.tool
    async def work(agent: Inject[Agent]) -> str:
        assert isinstance(agent.log.events[-1], ToolExecutionStarted)
        assert agent.log.approvals.records[-1].decision is ApprovalAnswer.ALWAYS
        executed.append(True)
        return "ok"

    def answer(call):
        asked.append(call.id)
        return ApprovalAnswer.ALWAYS

    def harness():
        return Application(
            model=tool_model(),
            plugins=[
                Approvals(trust="all"),
                tools,
                approval_gate(risky={"work"}, ask=answer),
                JsonlSessions(tmp_path),
            ],
        )

    async with harness().session("same-session") as agent:
        await agent.run("first")
        grant = agent.log.approvals.grants[0]
        prefix = agent.log.events
        fork = agent.log.fork("new-session")
        assert fork.events[: len(prefix)] == prefix and (not fork.approvals.grants)
        assert agent.log.fork("trusted", inherit_approvals=True).approvals.grants == (grant,)
        with pytest.raises(FrozenInstanceError):
            grant.revoked = True
    async with harness().session("same-session") as resumed:
        assert resumed.log.approvals.grants == (grant,)
        await resumed.run("second")
        cached = resumed.log.approvals.records[-1]
        assert cached.request.grant_id == grant.request_id and (not cached.is_grant)
        assert len(asked) == 1 and len(executed) == 2
        await resumed.revoke_approval(grant.request_id)
        after_revocation = resumed.log.events
        await resumed.revoke_approval(grant.request_id)
        assert resumed.log.events == after_revocation and (not resumed.log.approvals.grants)
    async with harness().session("same-session") as resumed:
        await resumed.run("third")
        assert len(asked) == 2 and len(executed) == 3
        events = tuple(decode_event(encode_event(event)) for event in resumed.log.events)
        assert ApprovalState(events) == resumed.log.approvals


@pytest.mark.parametrize("decision", [ApprovalAnswer.ALLOW, ApprovalAnswer.DENY])
async def test_single_call_answers_are_logged_without_becoming_persistent_grants(
    tmp_path, decision
):
    tools = Bundle("work")
    asked, executed = ([], [])

    @tools.tool
    async def work() -> str:
        executed.append(True)
        return "ok"

    def answer(call):
        asked.append(call.id)
        return decision

    for _ in range(2):
        harness = Application(
            model=tool_model(),
            plugins=[
                Approvals(trust="all"),
                tools,
                approval_gate(risky={"work"}, ask=answer),
                JsonlSessions(tmp_path),
            ],
        )
        async with harness.session("single-use") as agent:
            await agent.run("task")
            assert not agent.log.approvals.grants
            assert agent.log.approvals.records[-1].decision is decision
            policy = next(e for e in reversed(agent.log.events) if isinstance(e, ToolPolicyApplied))
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
        async with Application(
            model=tool_model(),
            plugins=[
                Approvals(trust="all"),
                tools,
                approval_gate(risky={"work"}, ask=answer, scope=scope),
                JsonlSessions(tmp_path),
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
        async with Application(
            model=tool_model(),
            plugins=[
                Approvals(trust="all"),
                tools,
                callback_approver(answer),
                JsonlSessions(tmp_path),
            ],
        ).session("policy-ask") as agent:
            await agent.run("task")
            assert agent.log.approvals.grant_for("work", scope="tool-policy") is not None
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

    async with Application(
        model=tool_model(),
        plugins=[Approvals(trust="all"), tools, approval_gate(risky={"work"}, ask=wait)],
    ).session("cancel-approval") as agent:
        await agent.followup("task")
        await asked.wait()
        assert len(agent.log.approvals.pending) == 1
        await agent.cancel()
        assert not agent.log.approvals.pending and (not agent.log.approvals.grants)
        assert agent.log.approvals.records[-1].resolution.status == "interrupted"
        assert not any(isinstance(e, ToolExecutionStarted) for e in agent.log.events)
        assert any(isinstance(e, ToolReturned) and e.result.is_error for e in agent.log.events)


async def test_uncommitted_approval_answer_cannot_start_execution():

    class FailingPersistence:
        async def load(self, session_id):
            return ()

        async def flush(self):
            pass

        async def append(self, session_id, seq, event):
            if isinstance(event, ApprovalResolved) and event.status == "answered":
                raise OSError("approval could not be saved")

    @plugin(slot="sessions")
    async def persistence(ctx):
        class Store:
            @asynccontextmanager
            async def open(self, session_id):
                yield FailingPersistence()

            def files(self, session_id):
                return MemoryFiles()

            async def list(self):
                return ()

            async def delete(self, session_id):
                pass

        ctx.provide(SessionStore, Store())
        yield

    tools = Bundle("work")

    @tools.tool
    async def work() -> str:
        pytest.fail("Uncommitted approval allowed a tool execution")

    async with Application(
        model=tool_model(),
        plugins=[
            Approvals(trust="all"),
            tools,
            persistence(),
            approval_gate(risky={"work"}, ask=lambda call: ApprovalAnswer.ALWAYS),
        ],
    ).session("failed-answer") as agent:
        await agent.run("task")
        assert not agent.log.approvals.pending and (not agent.log.approvals.grants)
        assert not any(isinstance(e, ToolExecutionStarted) for e in agent.log.events)


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
