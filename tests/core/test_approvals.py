import asyncio
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError

import pytest

from harnessed import Agent as Application
from harnessed import ApprovalAnswer, Inject, Plugin, Session, ToolCall, plugin, tool
from harnessed.core.approvals import ApprovalGate, ApprovalState, Approver, CallbackApprover
from harnessed.core.assets import MemoryAssets
from harnessed.core.hooks import ToolPolicy
from harnessed.core.messages import PolicyDecision
from harnessed.core.model import ModelChunk
from harnessed.core.session.codec import decode_event, encode_event
from harnessed.core.session.events import (
    ApprovalRequestedEvent,
    ApprovalResolvedEvent,
    ApprovalRevokedEvent,
    ApprovalStatus,
    ToolCallFinishedEvent,
    ToolExecutionStartedEvent,
    ToolPolicyAppliedEvent,
)
from harnessed.core.session.files import MemoryFiles
from harnessed.core.session.session import SessionLog
from harnessed.core.session.store import SessionStore
from harnessed.kernel import hook
from harnessed.plugins import Approvals, JsonlSessions, Service
from harnessed.testing import ScriptedModel


def tool_model():
    return ScriptedModel([[ModelChunk(tool_call=ToolCall("work"))], "done"])


async def test_always_grants_and_cached_uses_survive_a_fresh_harness_and_can_be_revoked(tmp_path):
    asked, executed = ([], [])

    class ToolsPlugin(Plugin, needs=(Session,)):
        @tool
        async def work(self, agent: Inject[Session]) -> str:
            assert isinstance(agent.log.events[-1], ToolExecutionStartedEvent)
            assert agent.log.approvals.records[-1].decision is ApprovalAnswer.ALWAYS
            executed.append(True)
            return "ok"

    tools = ToolsPlugin(name="work")

    def answer(request):
        call = request.call
        asked.append(call.id)
        return ApprovalAnswer.ALWAYS

    def harness():
        return Application(
            model=tool_model(),
            plugins=[
                Approvals(default=PolicyDecision.ALLOW),
                tools,
                ApprovalGate(risky={"work"}, ask=answer),
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
        await resumed.approvals.revoke(grant.request_id)
        after_revocation = resumed.log.events
        await resumed.approvals.revoke(grant.request_id)
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
    asked, executed = ([], [])

    class ToolsPlugin(Plugin):
        @tool
        async def work(self) -> str:
            executed.append(True)
            return "ok"

    tools = ToolsPlugin(name="work")

    def answer(request):
        call = request.call
        asked.append(call.id)
        return decision

    for _ in range(2):
        harness = Application(
            model=tool_model(),
            plugins=[
                Approvals(default=PolicyDecision.ALLOW),
                tools,
                ApprovalGate(risky={"work"}, ask=answer),
                JsonlSessions(tmp_path),
            ],
        )
        async with harness.session("single-use") as agent:
            await agent.run("task")
            assert not agent.log.approvals.grants
            assert agent.log.approvals.records[-1].decision is decision
            policy = next(
                e for e in reversed(agent.log.events) if isinstance(e, ToolPolicyAppliedEvent)
            )
        assert policy.decision is PolicyDecision(decision.value)
    assert len(asked) == 2
    assert len(executed) == (2 if decision is ApprovalAnswer.ALLOW else 0)


async def test_gate_scopes_do_not_share_persistent_permissions(tmp_path):
    asked = []

    class ToolsPlugin(Plugin):
        @tool
        async def work(self) -> str:
            return "ok"

    tools = ToolsPlugin(name="work")

    def answer(request):
        call = request.call
        asked.append(call.id)
        return ApprovalAnswer.ALWAYS

    for scope in ("first-policy", "second-policy"):
        async with Application(
            model=tool_model(),
            plugins=[
                Approvals(default=PolicyDecision.ALLOW),
                tools,
                ApprovalGate(risky={"work"}, ask=answer, scope=scope),
                JsonlSessions(tmp_path),
            ],
        ).session("scopes") as agent:
            await agent.run("task")
    assert len(asked) == 2


async def test_tool_policy_ask_uses_the_same_durable_approval_path(tmp_path):
    asked = []

    class ToolsPlugin(Plugin):
        @tool
        async def work(self) -> str:
            return "ok"

        @hook(ToolPolicy)
        async def ask_policy(self, event, next_):
            return PolicyDecision.ASK

    tools = ToolsPlugin(name="work")

    def answer(request):
        call = request.call
        asked.append(call.id)
        return ApprovalAnswer.ALWAYS

    for _ in range(2):
        async with Application(
            model=tool_model(),
            plugins=[
                Approvals(default=PolicyDecision.ALLOW),
                tools,
                Service(Approver, CallbackApprover(answer)),
                JsonlSessions(tmp_path),
            ],
        ).session("policy-ask") as agent:
            await agent.run("task")
            assert agent.log.approvals.grant_for("work", scope="tool-policy") is not None
    assert len(asked) == 1


async def test_cancellation_closes_an_unanswered_approval_and_does_not_execute_the_tool():
    asked = asyncio.Event()

    class ToolsPlugin(Plugin):
        @tool
        async def work(self) -> str:
            pytest.fail("Tool ran without an approval answer")

    tools = ToolsPlugin(name="work")

    async def wait(call):
        asked.set()
        await asyncio.Event().wait()

    async with Application(
        model=tool_model(),
        plugins=[
            Approvals(default=PolicyDecision.ALLOW),
            tools,
            ApprovalGate(risky={"work"}, ask=wait),
        ],
    ).session("cancel-approval") as agent:
        await agent.submit("task")
        await asked.wait()
        assert len(agent.log.approvals.pending) == 1
        await agent.abort()
        assert not agent.log.approvals.pending and (not agent.log.approvals.grants)
        assert agent.log.approvals.records[-1].resolution.status == ApprovalStatus.INTERRUPTED
        assert not any(isinstance(e, ToolExecutionStartedEvent) for e in agent.log.events)
        assert any(
            isinstance(e, ToolCallFinishedEvent) and e.result.is_error for e in agent.log.events
        )


async def test_uncommitted_approval_answer_cannot_start_execution():

    class FailingPersistence:
        async def load(self, session_id):
            return ()

        async def flush(self):
            pass

        async def append(self, session_id, seq, event):
            if isinstance(event, ApprovalResolvedEvent) and event.status == "answered":
                raise OSError("approval could not be saved")

    @plugin(slot="sessions")
    async def persistence(ctx):
        class Store:
            @asynccontextmanager
            async def open(self, session_id):
                yield FailingPersistence()

            def assets(self, session_id):
                return MemoryAssets()

            def files(self, session_id):
                return MemoryFiles()

            async def list(self):
                return ()

            async def delete(self, session_id):
                pass

        ctx.provide(SessionStore, Store())
        yield

    class ToolsPlugin(Plugin):
        @tool
        async def work(self) -> str:
            pytest.fail("Uncommitted approval allowed a tool execution")

    tools = ToolsPlugin(name="work")

    async with Application(
        model=tool_model(),
        plugins=[
            Approvals(default=PolicyDecision.ALLOW),
            tools,
            persistence(),
            ApprovalGate(risky={"work"}, ask=lambda call: ApprovalAnswer.ALWAYS),
        ],
    ).session("failed-answer") as agent:
        await agent.run("task")
        assert not agent.log.approvals.pending and (not agent.log.approvals.grants)
        assert not any(isinstance(e, ToolExecutionStartedEvent) for e in agent.log.events)


@pytest.mark.parametrize(
    "events",
    [
        (ApprovalResolvedEvent("unknown", ApprovalAnswer.ALLOW),),
        (ApprovalRevokedEvent("unknown"),),
        (ApprovalRequestedEvent("id", ToolCall("work"), "reason", "scope", grant_id="missing"),),
        (
            ApprovalRequestedEvent("id", ToolCall("work"), "reason", "scope"),
            ApprovalResolvedEvent("id", ApprovalAnswer.DENY),
            ApprovalRevokedEvent("id"),
        ),
    ],
)
def test_invalid_approval_transitions_are_rejected_by_replay(events):
    with pytest.raises(ValueError):
        SessionLog("invalid", seed=events)
