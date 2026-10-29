from harnessed import Agent, Plugin, PolicyDecision, ToolCall, ToolContext, events, tool
from harnessed.core.messages import ApprovalAnswer
from harnessed.core.model import ModelChunk
from harnessed.core.session.events import ApprovalRequestedEvent
from harnessed.plugins import Approvals
from harnessed.testing import ScriptedModel


class Cleanup(Plugin):
    def __init__(self):
        super().__init__()
        self.deleted = []

    @tool
    async def cleanup(self, *, call: ToolContext) -> str:
        if not await call.approve("Delete 3 generated files?"):
            return "kept the files"
        self.deleted.append(call.call.id)
        return "deleted"


def _calls(count: int) -> list:
    steps = [[ModelChunk(tool_call=ToolCall("cleanup", {}, f"c{i}"))] for i in range(count)]
    return [*steps, "done"]


async def test_a_tool_asks_the_configured_approver_mid_call():
    tools = Cleanup()
    answers = iter([ApprovalAnswer.DENY, ApprovalAnswer.ALWAYS])
    approvals = Approvals(default=PolicyDecision.ALLOW, approver=lambda request: next(answers))
    model = ScriptedModel(_calls(3))
    async with Agent(model=model, plugins=[tools, approvals]).session() as session:
        result = await session.run("clean up")
        assert [r.text for _, r in result.tool_calls] == ["kept the files", "deleted", "deleted"]
        requests = [e for e in session.events if isinstance(e, ApprovalRequestedEvent)]
        assert [e.scope for e in requests] == ["tool-call"] * 3
        assert requests[-1].grant_id is not None  # the ALWAYS grant answered the third call
        assert tools.deleted == ["c1", "c2"]


async def test_a_stream_consumer_answers_a_tool_approval():
    tools = Cleanup()
    model = ScriptedModel(_calls(1))
    plugins = [tools, Approvals(default=PolicyDecision.ALLOW)]
    async with Agent(model=model, plugins=plugins).session() as session:
        async for event in session.stream("clean up"):
            if isinstance(event, events.ApprovalNeededEvent):
                assert event.reason == "Delete 3 generated files?"
                await session.approvals.answer(event.request_id, ApprovalAnswer.ALLOW)
        assert tools.deleted == ["c0"]
